"""CARD-23 §7/§8 — 结论护照的构建与「不依赖 LLM 的重新证明」.

对外的一句话：``verify_claim()`` 是**脱离 LLM 重新回答** —— 它不看模型说过什么，
只看证明对象里记下的东西，以及**当前**的引擎状态，然后给出五态之一。

因此本模块的三条做法是承重的：

1. **能重算的就重算，不信任记下来的值。**
   计算项交给 CARD-03 的 ``CalculationRegistry.recalculate``；依赖状态交给 CARD-09 的
   ``claim_status``/``upstream_state``；冲突交给 CARD-13 的 ``ConflictRecord``。护照里记下的
   那些值是**当时**的快照，用来比对「有没有变」，不是用来当答案。
   记下的状态与重算不一致时会**报出来**（``warn``），不会静默采用任一侧。

2. **零 LLM。** 全部检查项都是查表、重算与比较。§9 的契约是验证结果不得被模型修改，
   那么最稳的保证就是验证路径上根本没有模型 —— 这一点由 ``tests/test_cn_proof.py``
   的源码级断言守住。

3. **不猜。** 任一检查项缺输入时记 ``skipped`` 并写明原因，**不记为通过**。
   ``skipped`` 不影响总状态，所以它不会把「无法验证」伪装成 ``BLOCKED``；
   但报告里「查过」与「没查」始终分得开。

**容差边界（与 CARD-22 的分工，别混用）**：本模块的计算检查是**同式同输入的重算**，
差异只可能来自浮点噪声，故用紧 epsilon（默认 ``1e-9``，与 CARD-03 ``validate`` 一致）。
CARD-22 §7 的 per-unit 容差（元 1000 / PERCENT 0.05 …）管的是**跨来源/恒等式的同值判定**，
把它套到重算上会掩掉真实的计算不一致。两者用途不同，故不是一个容差。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..analysis.metrics import metric_definition
from ..conflict import CHANGE_VERSION_SUPERSESSION, ConflictRecord
from ..graph import (
    CLAIM_TYPES,
    ROLE_OPTIONAL,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    UPSTREAM_BLOCKED,
    UPSTREAM_CONFLICTED,
    UPSTREAM_MISSING,
    UPSTREAM_STALE,
    VALIDATION_BLOCKED,
    VALIDATION_PENDING,
    VALIDATION_STALE,
    VALIDATION_UNSUPPORTED,
    CalculationNode,
    Claim,
    ClaimGraph,
    EvidenceNode,
    GraphError,
    claim_status,
    resolve_targets,
    upstream_state,
)
from .models import (
    SCOPE_BASIS_CALCULATION,
    SCOPE_BASIS_UNDECLARED,
    CHECK_ASSUMPTION,
    CHECK_CALCULATION,
    CHECK_CONFLICT,
    CHECK_DEPENDENCY,
    CHECK_INTEGRITY,
    CHECK_PERIOD,
    CHECK_SCOPE_UNIT,
    CHECK_SOURCE,
    CHECK_VALIDATOR,
    COMMIT_UNKNOWN,
    PROOF_CHECKS,
    PROOF_VERSION,
    STATUS_CONFLICTED,
    STATUS_DEGRADED,
    STATUS_STALE,
    VERSION_AS_REPORTED,
    VERSION_BASIS_DECLARED,
    VERSION_BASIS_EVIDENCE_INACTIVE,
    VERSION_BASIS_NO_SIGNAL,
    VERSION_BASIS_SUPERSESSION,
    HASH_IN_MANIFEST,
    HASH_RECOMPUTED,
    HASH_UNCHECKED,
    HASH_UNREADABLE,
    VERSION_STATUSES,
    VERSION_SUPERSEDED,
    VERIFIER_VERSION,
    AccountingContext,
    AssumptionProof,
    CalculationProof,
    CheckOutcome,
    DependencyProof,
    FinancialProofObject,
    IntegrityProof,
    ProofBuildError,
    ProofError,
    SourceProof,
    UpstreamProof,
    ValidationProof,
    VerificationResult,
    HASH_STATE_LABELS,
    failed,
    merge_status,
    passed,
    skipped,
    warned,
)

#: The `元`-style default; kept one place so a caller cannot pass a different
#: epsilon to one check and not another.
DEFAULT_RECOMPUTE_TOLERANCE = 1e-9

#: 口径 spellings seen in this codebase (CARD-02's ``Scope`` yields 合并/母公司;
#: CARD-03/CARD-09 store whatever the producer wrote). Compared in normalised
#: form so "合并" and "CONSOLIDATED" are the same measurement.
_SCOPE_CONSOLIDATED = "consolidated"
_SCOPE_PARENT = "parent"
_SCOPE_ALIASES: Dict[str, str] = {
    "合并": _SCOPE_CONSOLIDATED,
    "合并报表": _SCOPE_CONSOLIDATED,
    "合并口径": _SCOPE_CONSOLIDATED,
    "consolidated": _SCOPE_CONSOLIDATED,
    "母公司": _SCOPE_PARENT,
    "母公司报表": _SCOPE_PARENT,
    "parent": _SCOPE_PARENT,
}


def normalize_scope(value: Optional[str]) -> str:
    """Canonical 口径 token, or ``""`` when the input declares nothing."""
    if value is None:
        return ""
    token = str(value).strip()
    if not token:
        return ""
    return _SCOPE_ALIASES.get(token, _SCOPE_ALIASES.get(token.lower(), token))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProofContext:
    """Everything a verification needs, injected rather than globalled.

    ``graph`` is the authority for claims / evidence / calculations / dependency
    states; everything else is optional and each absence degrades a *specific*
    check into ``skipped`` with a reason (§22's degradation strategy).
    """

    graph: Optional[ClaimGraph] = None
    conflicts: Tuple[ConflictRecord, ...] = ()
    calculation_registry: Any = None
    assumption_registry: Any = None
    run_manifest: Any = None
    run_id: str = ""
    git_commit: str = ""
    audit_chain_ref: str = ""
    #: ``claim_id -> version`` declared by the caller (e.g. from CARD-04's signals).
    declared_versions: Mapping[str, str] = field(default_factory=dict)
    #: ``claim_id -> 口径`` declared by the caller. Load-bearing: the FPO's own
    #: scope is derived from the calculation node, so comparing it against that
    #: same node would be vacuous. Only a *declared* scope makes §7 check 4 able
    #: to catch "this claim says 母公司 but was computed on 合并 numbers".
    declared_scopes: Mapping[str, str] = field(default_factory=dict)
    #: ``document_id -> sha256`` as recorded when the documents were ingested.
    document_hashes: Mapping[str, str] = field(default_factory=dict)
    #: Best-effort byte loader; when absent the hash is cross-checked against the
    #: manifest instead of being recomputed, and the check says which it did.
    document_loader: Optional[Callable[[str], Optional[bytes]]] = None
    tolerance: float = DEFAULT_RECOMPUTE_TOLERANCE
    detected_at: str = ""

    # -- manifest fallbacks ------------------------------------------------
    @property
    def resolved_run_id(self) -> str:
        if self.run_id:
            return self.run_id
        return str(getattr(self.run_manifest, "run_id", "") or "")

    @property
    def resolved_git_commit(self) -> str:
        if self.git_commit:
            return self.git_commit
        return str(getattr(self.run_manifest, "git_commit", "") or COMMIT_UNKNOWN)

    @property
    def manifest_hashes(self) -> Tuple[str, ...]:
        raw = getattr(self.run_manifest, "input_document_sha256", None) or ()
        return tuple(str(item) for item in raw)

    def evidence(self, evidence_id: str) -> Optional[EvidenceNode]:
        if self.graph is None:
            return None
        return self.graph.evidence_index.get(evidence_id)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _claim_of(context: ProofContext, claim_id: str) -> Claim:
    if context.graph is None:
        raise ProofBuildError("缺少 ClaimGraph，无法构建证明对象")
    try:
        return context.graph.claim(claim_id)
    except GraphError as exc:
        raise ProofBuildError(str(exc)) from exc


def _evidence_nodes(claim: Claim, context: ProofContext,
                    extra: Sequence[str] = ()) -> List[EvidenceNode]:
    """Every evidence node the claim leans on, ledger-backed and calculation-backed."""
    out: List[EvidenceNode] = []
    if context.graph is None:
        return out
    for evidence_id in (*claim.evidence_ids, *extra):
        node = context.graph.evidence_index.get(evidence_id)
        if node is not None:
            out.append(node)
    for calculation_id in claim.calculation_ids:
        node = context.graph.calculations.get(calculation_id)
        if node is None:
            continue
        for evidence_id in node.evidence_ids:
            found = context.graph.evidence_index.get(evidence_id)
            if found is not None:
                out.append(found)
    seen: Dict[str, EvidenceNode] = {}
    for node in out:
        seen.setdefault(node.evidence_id, node)
    return list(seen.values())


def resolve_version_status(
    claim: Claim, evidence_nodes: Sequence[EvidenceNode], context: ProofContext
) -> Tuple[str, str]:
    """``(version_status, basis)`` — §6's version label **plus why we believe it**.

    Order of evidence, strongest first: an explicit caller declaration, then a
    CARD-13 ``VERSION_SUPERSESSION`` record that names one of this claim's own
    sources as superseded, then CARD-09's append-only deactivation (a withdrawal
    is a supersession we did not have to arbitrate).

    Falling through to ``AS_REPORTED`` is not a guess: it is the epistemic state
    of a filing with no supersession signal, and the returned *basis* says
    exactly that (``no_supersession_signal``). A label without a basis would be
    the silent assumption this project forbids.
    """
    declared = context.declared_versions.get(claim.claim_id)
    if declared:
        token = str(declared).strip().upper()
        if token not in VERSION_STATUSES:
            raise ProofError(f"非法 version_status：{declared!r}")
        return token, VERSION_BASIS_DECLARED

    metric = claim.metric
    period_key = claim.period_key
    if metric and period_key:
        source_ids = {
            token
            for node in evidence_nodes
            for token in (node.document_id, node.snapshot_id)
            if token
        }
        for record in context.conflicts:
            if record.change_kind != CHANGE_VERSION_SUPERSESSION:
                continue
            if record.metric != metric or record.period_key != period_key:
                continue
            if record.superseded_source and record.superseded_source in source_ids:
                return VERSION_SUPERSEDED, VERSION_BASIS_SUPERSESSION

    if any(not node.active for node in evidence_nodes):
        return VERSION_SUPERSEDED, VERSION_BASIS_EVIDENCE_INACTIVE

    return VERSION_AS_REPORTED, VERSION_BASIS_NO_SIGNAL


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_financial_proof_object(
    claim_id: str, context: ProofContext
) -> FinancialProofObject:
    """Assemble the passport for one claim (§8).

    Fails loudly (``ProofBuildError``) rather than producing a passport with
    holes: §11 says a failed build marks the *claim* ``proof_build_failed`` and
    leaves the rest of the batch alone, which only works if the failure is
    detectable.
    """
    if not claim_id:
        raise ProofBuildError("build_financial_proof_object 需要 claim_id")
    claim = _claim_of(context, claim_id)
    if claim.claim_type not in CLAIM_TYPES:
        raise ProofBuildError(f"未知 claim_type：{claim.claim_type!r}")

    graph = context.graph
    assert graph is not None  # _claim_of guarantees it

    evidence_nodes = _evidence_nodes(claim, context)
    version_status, version_basis = resolve_version_status(claim, evidence_nodes, context)

    # -- source proof ------------------------------------------------------
    primary = next(
        (node for node in evidence_nodes if node.document_id or node.available_at),
        evidence_nodes[0] if evidence_nodes else None,
    )
    document_id = primary.document_id if primary else None
    sha256 = None
    if document_id:
        sha256 = context.document_hashes.get(document_id)
    source_proof = SourceProof(
        document_id=document_id,
        sha256=sha256,
        page=primary.page if primary else None,
        locator=_locator(primary) if primary else "",
        evidence_ids=tuple(node.evidence_id for node in evidence_nodes),
        available_at=primary.available_at if primary else None,
        snapshot_id=primary.snapshot_id if primary else None,
    )

    # -- accounting context ------------------------------------------------
    calculation_nodes = [
        graph.calculations[cid] for cid in claim.calculation_ids if cid in graph.calculations
    ]
    period_basis = _period_basis(claim, calculation_nodes, evidence_nodes)
    derived_scope, scope_basis = _derived_scope(calculation_nodes)
    declared_scope = normalize_scope(context.declared_scopes.get(claim.claim_id))
    accounting_context = AccountingContext(
        period=claim.period_key or "",
        period_basis=period_basis,
        scope=derived_scope,
        declared_scope=declared_scope,
        scope_basis=scope_basis,
        currency=_first_non_empty([node.currency for node in calculation_nodes]),
        unit=_first_non_empty(
            [str((node.output or {}).get("unit") or "") for node in calculation_nodes]
            + [node.unit for node in evidence_nodes]
        ),
        version_basis=version_basis,
    )

    # -- calculation proof -------------------------------------------------
    calculation_proof = None
    if calculation_nodes:
        node = calculation_nodes[0]
        calculation_proof = CalculationProof(
            calculation_id=node.calculation_id,
            metric=node.metric,
            formula=node.formula,
            operation=node.operation,
            inputs=tuple(node.inputs),
            output=node.value,
            formula_version=node.formula_version,
            code_version=node.code_version,
            engine=node.engine,
            period_key=str((node.output or {}).get("period_key") or "") or None,
            unit=str((node.output or {}).get("unit") or "") or None,
        )

    # -- assumption proof --------------------------------------------------
    assumption_proof = AssumptionProof(
        assumption_ids=tuple(claim.assumption_ids),
        sources=_assumption_sources(claim, context),
        rationale=claim.rationale or "",
        grades=_assumption_grades(claim, context),
    )

    # -- dependency proof --------------------------------------------------
    upstream = [
        UpstreamProof(
            target_id=target_id,
            target_type=target_type,
            role=role,
            state=upstream_state(graph, target_id, target_type),
        )
        for target_type, target_id, role in resolve_targets(claim)
    ]
    dependency_proof = DependencyProof(upstream=tuple(upstream))

    # -- validation proof --------------------------------------------------
    derived_status, _dropped, trigger = claim_status(graph, claim)
    validation_proof = ValidationProof(
        validator_rules=_validator_rules(claim),
        result=derived_status,
        blocked_reasons=(trigger,) if trigger else (),
    )

    # -- integrity proof ---------------------------------------------------
    integrity_proof = IntegrityProof(
        object_hash="",
        audit_chain_ref=context.audit_chain_ref,
        run_id=context.resolved_run_id,
        git_commit=context.resolved_git_commit,
        proof_version=PROOF_VERSION,
    )

    return FinancialProofObject(
        claim_id=claim.claim_id,
        claim_type=claim.claim_type,
        claim_text=claim.text,
        economic_period=claim.period_key or "",
        # The claim's own 口径: what it says it is about. Falls back to the
        # derivation when the caller declared nothing.
        scope=declared_scope or accounting_context.scope,
        version_status=version_status,
        source_proof=source_proof,
        accounting_context=accounting_context,
        calculation_proof=calculation_proof,
        assumption_proof=assumption_proof,
        dependency_proof=dependency_proof,
        validation_proof=validation_proof,
        integrity_proof=integrity_proof,
        claim_metric=claim.metric,
        claim_period_key=claim.period_key,
    ).seal()


def _locator(node: Optional[EvidenceNode]) -> str:
    if node is None:
        return ""
    if node.page is not None:
        return f"{node.document_id or '未知文档'} 第{node.page}页"
    if node.available_at:
        return f"{node.snapshot_id or '快照'} @{node.available_at}"
    return ""


def _first_non_empty(values: Sequence[Optional[str]]) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def _derived_scope(calculation_nodes: Sequence[CalculationNode]) -> Tuple[str, str]:
    """``(scope, basis)`` — where the claim's numbers actually came from.

    Deliberately **ignores** the caller's declaration. The declaration is kept in
    ``AccountingContext.declared_scope`` instead, because §7 check 4 asks whether
    what a claim *says* it is about matches what its numbers *are*; if the
    declaration overwrote the derivation, the check would compare a value with
    itself and could never disagree.
    """
    for node in calculation_nodes:
        scope = getattr(node, "scope", None)
        if scope:
            return normalize_scope(scope), SCOPE_BASIS_CALCULATION
    return "", SCOPE_BASIS_UNDECLARED


def _period_basis(
    claim: Claim,
    calculation_nodes: Sequence[CalculationNode],
    evidence_nodes: Sequence[EvidenceNode],
) -> str:
    for node in calculation_nodes:
        basis = (node.output or {}).get("period_basis")
        if basis:
            return str(basis)
    for node in evidence_nodes:
        basis = getattr(node, "period_basis", None)
        if basis:
            return str(basis)
    return "UNKNOWN"


def _validator_rules(claim: Claim) -> Tuple[str, ...]:
    rules: List[str] = [f"card09.claim_status.{claim.claim_type}"]
    if claim.claim_type != "opinion":
        rules.append("card10.sentence_tier_marking")
    if claim.numeric:
        rules.append("card10.numeric_binding")
    return tuple(rules)


def _assumption_grades(claim: Claim, context: ProofContext) -> Dict[str, str]:
    registry = context.assumption_registry
    if registry is None:
        return {}
    grades: Dict[str, str] = {}
    for assumption_id in claim.assumption_ids:
        try:
            record = registry.get(assumption_id)
        except Exception:  # registry raises its own AssumptionError subclasses
            continue
        grades[assumption_id] = str(getattr(record, "grade", "") or "")
    return grades


def _assumption_sources(claim: Claim, context: ProofContext) -> Tuple[str, ...]:
    registry = context.assumption_registry
    if registry is None:
        return ()
    sources: List[str] = []
    for assumption_id in claim.assumption_ids:
        try:
            record = registry.get(assumption_id)
        except Exception:
            continue
        source = str(getattr(record, "source", "") or "")
        if source:
            sources.append(source)
    return tuple(sources)


# ---------------------------------------------------------------------------
# §7 — the nine checks
# ---------------------------------------------------------------------------
def verify_source(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.1 — document hash + evidence existence (+ active / locatable / version)."""
    graph = context.graph
    if graph is None:
        return skipped(CHECK_SOURCE, "未提供 ClaimGraph，无法核对证据是否存在")

    evidence_ids = fpo.source_proof.evidence_ids
    if not evidence_ids:
        return failed(
            CHECK_SOURCE,
            "claim 未绑定任何证据（孤儿 claim），来源无法追溯（§12 edge 3）",
        )

    missing = [eid for eid in evidence_ids if eid not in graph.evidence_index]
    if missing:
        return failed(
            CHECK_SOURCE,
            f"证据不存在：{', '.join(sorted(missing))}",
            missing_evidence=sorted(missing),
        )

    nodes = [graph.evidence_index[eid] for eid in evidence_ids]
    inactive = [node for node in nodes if not node.active]
    unlocatable = [node for node in nodes if not node.locatable]
    if unlocatable and len(unlocatable) == len(nodes):
        return failed(
            CHECK_SOURCE,
            "全部证据都无法定位（既无页码也无快照时点）",
            unlocatable=[node.evidence_id for node in unlocatable],
        )

    # -- version ------------------------------------------------------------
    if fpo.version_status == VERSION_SUPERSEDED:
        return CheckOutcome(
            CHECK_SOURCE, "warn",
            f"来源版本已 superseded（依据：{fpo.accounting_context.version_basis}），"
            "结论仍有历史价值但不得当作当前值（§12 edge 6）",
            STATUS_STALE,
            {"version_status": fpo.version_status,
             "version_basis": fpo.accounting_context.version_basis,
             "inactive_evidence": [node.evidence_id for node in inactive]},
        )

    # -- document hash ------------------------------------------------------
    document_id = fpo.source_proof.document_id
    recorded = fpo.source_proof.sha256
    if not document_id:
        return warned(
            CHECK_SOURCE, "来源未声明文档编号，无法核对文档哈希", impact=STATUS_DEGRADED
        )
    if not recorded:
        return warned(
            CHECK_SOURCE, f"来源文档 {document_id} 未记录 sha256，无法核对完整性",
            impact=STATUS_DEGRADED, document_id=document_id,
        )
    if context.document_loader is not None:
        payload = context.document_loader(document_id)
        if payload is None:
            hash_state = HASH_UNREADABLE
        else:
            import hashlib

            actual = hashlib.sha256(payload).hexdigest()
            if actual != recorded:
                return failed(
                    CHECK_SOURCE,
                    f"输入文档 {document_id} 的 sha256 已变（{recorded[:12]}… → {actual[:12]}…），"
                    "证明所依据的字节不再是当前字节",
                    document_id=document_id, recorded=recorded, actual=actual,
                )
            hash_state = HASH_RECOMPUTED
    elif context.manifest_hashes:
        if recorded not in context.manifest_hashes:
            return failed(
                CHECK_SOURCE,
                f"来源 sha256 不在本 run 记录的输入文档清单内（{document_id}）",
                document_id=document_id, recorded=recorded,
            )
        hash_state = HASH_IN_MANIFEST
    else:
        hash_state = HASH_UNCHECKED

    if inactive:
        return CheckOutcome(
            CHECK_SOURCE, "warn",
            f"存在已撤销证据：{', '.join(sorted(node.evidence_id for node in inactive))}"
            f"（文档哈希状态：{HASH_STATE_LABELS[hash_state]}）",
            STATUS_STALE,
            {"inactive_evidence": sorted(node.evidence_id for node in inactive),
             "hash_state": hash_state},
        )

    if hash_state == HASH_RECOMPUTED:
        return passed(
            CHECK_SOURCE,
            f"{len(nodes)} 项证据均存在、active、可定位；文档 sha256 已重算并与记录一致",
            document_id=document_id, evidence_count=len(nodes), hash_state=hash_state,
        )
    # The hash was *not* recomputed. Reporting this as a pass would be the silent
    # assumption this project forbids — the passport would claim integrity it
    # never established.
    return warned(
        CHECK_SOURCE,
        f"{len(nodes)} 项证据均存在、active、可定位；但文档 sha256 未重算"
        f"（{HASH_STATE_LABELS[hash_state]}），完整性只是交叉核对，不等于已核验",
        impact=STATUS_DEGRADED,
        document_id=document_id, evidence_count=len(nodes), hash_state=hash_state,
    )


def verify_period(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.3 — the claim's period must be the period it is actually about.

    The card writes "``economic_period`` 与 inputs period 一致", but taken
    literally that fails **every legitimate cross-period claim**: ``revenue_yoy``
    at 2025FY rests on revenue@2025FY *and* revenue@2024FY, and 平均净资产 rests
    on two balance-sheet dates. Requiring all inputs to share one period would
    make the verifier cry wolf on exactly the ratios this project exists to
    show — and a verifier that blocks clean filings is worse than none.

    So the check is the *intent* behind the wording, in two parts:

    * the claim's period must equal the **calculation's own declared period** —
      CARD-03 already decided which period the output is about, and this is a
      genuine cross-check of the claim against that decision;
    * at least one input must actually carry that period — a claim cannot be
      about a period none of its evidence touches.

    Inputs from other periods are reported (``cross_period_inputs``) but not
    failed. Whether a two-period input set is legal is CARD-02's period engine's
    business (and CARD-22 F3/F4's); re-deriving it here would be a second
    implementation of period arithmetic, which §7.3 exists to avoid.
    """
    stated = fpo.economic_period or fpo.accounting_context.period
    if not stated:
        return skipped(CHECK_PERIOD, "claim 未声明经济期间，期间一致性无法核对")

    calc_period = str(fpo.calculation_proof.period_key or "") if fpo.calculation_proof else ""
    if calc_period and calc_period != stated:
        return failed(
            CHECK_PERIOD,
            f"期间不一致：claim 声明 {stated}，但计算 {fpo.calculation_proof.calculation_id} "
            f"声明其输出期间为 {calc_period}",
            stated=stated, calculation_period=calc_period,
            calculation_id=fpo.calculation_proof.calculation_id,
        )

    observed: List[Tuple[str, str]] = []
    for evidence_id in fpo.source_proof.evidence_ids:
        node = context.evidence(evidence_id)
        if node is not None and node.period_key:
            observed.append((node.evidence_id, node.period_key))

    if not observed and not calc_period:
        return skipped(
            CHECK_PERIOD, f"claim 声明期间 {stated}，但输入项都未声明期间，无法核对"
        )

    covering = [ref for ref, period in observed if period == stated]
    cross = sorted((ref, period) for ref, period in observed if period != stated)
    if observed and not covering:
        return failed(
            CHECK_PERIOD,
            f"claim 声明期间 {stated}，但没有任何输入落在该期间（输入期间："
            + "、".join(f"{ref}={period}" for ref, period in cross)
            + "）",
            stated=stated,
            input_periods=[{"ref": ref, "period": period} for ref, period in cross],
        )

    note = f"claim 期间 {stated} 由 {len(covering)} 项输入覆盖"
    if calc_period:
        note += "，计算声明的输出期间与之相同"
    else:
        note += "，计算未声明期间"
    if cross:
        periods = "、".join(sorted({period for _ref, period in cross}))
        note += f"；另有 {len(cross)} 项输入来自 {periods}（跨期输入，按跨期计算处理，不判失败）"
    return passed(
        CHECK_PERIOD, note,
        stated=stated,
        covered_by=covering,
        cross_period_inputs=[{"ref": ref, "period": period} for ref, period in cross],
    )


def verify_scope_unit(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.4/§7.5 — 口径、单位、币种必须与**独立声明**一致.

    Three things decide how this check must be written.

    **1. A unit belongs to a quantity, not to a claim.** ``gross_margin`` is
    declared in ``PERCENT`` while its inputs (gross_profit, revenue) are declared
    in ``元``; comparing the claim's unit against its inputs' units is a category
    error that fails every ratio in the corpus.

    **2. 从派生源再比一次不是核对，是回声.** The passport's scope / currency /
    unit are *derived* — from the calculation node when there is one, otherwise
    from the first evidence node. Comparing a derived value against its own
    source can never disagree, and counting that as a "comparison" would make the
    check look stricter than it is. So the derived values are listed in
    ``derived`` (with their source) and kept **out** of ``compared``.

    **3. What is left is still real.** Three genuinely independent checks:

    * the caller's ``declared_scope`` vs the derived 口径 — this is what catches
      "结论说是母公司口径，数却是合并算出来的";
    * the claim's metric's unit vs **CARD-03's canonical unit for that metric**
      (``metric_definition(metric).unit``) — independent, and it catches "gross_margin
      记成了元";
    * evidence nodes that share a metric must agree with each other — that is how
      "同一行的两个输入一个记元、一个记万元" is caught.

    Everything that could not be compared is named in ``not_compared``.
    """
    problems: List[str] = []
    independent = {"declared_scope": 0, "metric_unit": 0, "evidence_unit_agreement": 0}
    derived: List[str] = []
    not_compared: List[str] = []

    stated_scope = normalize_scope(fpo.scope or fpo.accounting_context.scope)
    stated_unit = fpo.accounting_context.unit
    stated_currency = fpo.accounting_context.currency
    metric = fpo.claim_metric

    node = None
    if fpo.calculation_proof is not None and context.graph is not None:
        node = context.graph.calculations.get(fpo.calculation_proof.calculation_id)
    if fpo.calculation_proof is not None:
        if node is None:
            not_compared.append("计算节点不在图中，派生来源无从核对")
        else:
            for label, value in (
                ("口径", stated_scope), ("币种", stated_currency), ("单位", stated_unit),
            ):
                if value:
                    derived.append(f"{label} {value} 取自计算 {node.calculation_id}")
    elif stated_unit:
        derived.append(f"单位 {stated_unit} 取自首个证据（该 claim 无计算项）")

    # (1) the caller's own 口径 declaration vs the derivation ----------------
    #     ``AccountingContext`` keeps these two values apart precisely so this
    #     comparison is possible; ``fpo.scope`` alone cannot answer it, because
    #     it is the declaration when there is one.
    derived_scope_stated = normalize_scope(fpo.accounting_context.scope)
    declared_scope = normalize_scope(
        fpo.accounting_context.declared_scope or context.declared_scopes.get(fpo.claim_id)
    )
    if declared_scope:
        independent["declared_scope"] += 1
        if not derived_scope_stated:
            not_compared.append(
                "调用方声明了口径，但没有计算节点可提供派生口径，未能核对"
            )
        elif declared_scope != derived_scope_stated:
            problems.append(
                f"口径：调用方声明 {declared_scope} vs 计算派生 {derived_scope_stated}"
            )
    elif derived_scope_stated:
        not_compared.append("调用方未声明口径，claim 口径只能与派生源回声，未作独立核对")

    # (2) CARD-03's canonical unit for this metric --------------------------
    if not metric:
        not_compared.append("claim 未声明指标，单位未与指标规范单位核对")
    else:
        definition = metric_definition(metric)
        if definition is None:
            not_compared.append(f"CARD-03 指标表里没有 {metric}，单位未与规范单位核对")
        elif not stated_unit:
            not_compared.append(f"{metric} 有规范单位但 claim 未声明单位，未能核对")
        else:
            independent["metric_unit"] += 1
            expected = str(getattr(definition, "unit", "") or "")
            if expected and expected != stated_unit:
                problems.append(
                    f"单位：claim={stated_unit} vs CARD-03 指标 {metric} 的规范单位={expected}"
                )

    # (3) evidence of one metric must agree with itself ---------------------
    same_metric: List[Tuple[str, str]] = []
    by_metric: dict = {}
    for evidence_id in fpo.source_proof.evidence_ids:
        evidence = context.evidence(evidence_id)
        if evidence is None:
            not_compared.append(f"证据 {evidence_id} 不在图中，单位未核对")
            continue
        if not evidence.unit:
            continue
        if metric and evidence.metric == metric:
            same_metric.append((evidence.evidence_id, evidence.unit))
        if evidence.metric:
            by_metric.setdefault(evidence.metric, []).append(
                (evidence.evidence_id, evidence.unit)
            )

    for name, entries in sorted(by_metric.items()):
        units = sorted({unit for _ref, unit in entries})
        if len(units) > 1:
            independent["evidence_unit_agreement"] += 1
            detail = "、".join(f"{ref}={unit}" for ref, unit in sorted(entries))
            problems.append(f"同指标 {name} 的证据单位不一致（{detail}）")
    if metric and not same_metric:
        not_compared.append(
            f"没有同名指标（{metric}）的证据节点，claim 单位未与同名证据交叉核对"
        )

    total = sum(independent.values())
    if problems:
        return failed(
            CHECK_SCOPE_UNIT, "；".join(problems), problems=problems,
            compared=independent, derived=derived, not_compared=not_compared,
        )
    if total == 0:
        return skipped(
            CHECK_SCOPE_UNIT,
            "无可独立核对项：口径/单位/币种只能与派生源回声（如实跳过，不记通过）",
            compared=independent, derived=derived, not_compared=not_compared,
        )
    summary = "、".join(f"{name}×{count}" for name, count in sorted(independent.items()) if count)
    note = f"共 {total} 处独立核对一致（{summary}）"
    if stated_scope or stated_unit:
        note += f"（派生口径 {stated_scope}，派生单位 {stated_unit}）"
    if not_compared:
        note += f"；{len(not_compared)} 项未独立核对，原因已列在 not_compared"
    return passed(
        CHECK_SCOPE_UNIT, note,
        compared=independent, derived=derived, not_compared=not_compared,
    )


def verify_calculation(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.6 — re-execute the formula through CARD-03 and compare the output."""
    proof = fpo.calculation_proof
    if proof is None:
        return skipped(CHECK_CALCULATION, "纯 fact 无计算项，该检查跳过（§12 edge 1）")
    if context.calculation_registry is None:
        return skipped(
            CHECK_CALCULATION,
            "未提供 CARD-03 CalculationRegistry，无法重新执行公式（不记通过）",
            calculation_id=proof.calculation_id,
        )
    registry = context.calculation_registry
    if not _registry_has(registry, proof.calculation_id):
        return failed(
            CHECK_CALCULATION,
            f"计算记录 {proof.calculation_id} 在当前注册表中不存在，公式无法复核",
            calculation_id=proof.calculation_id,
        )
    if not proof.inputs:
        return failed(
            CHECK_CALCULATION,
            f"计算 {proof.calculation_id} 没有输入项，无法重算（§12 edge 4）",
            calculation_id=proof.calculation_id,
        )
    if proof.output is None:
        return failed(
            CHECK_CALCULATION,
            f"计算 {proof.calculation_id} 没有记录输出值，无从比对",
            calculation_id=proof.calculation_id,
        )
    try:
        redone = float(registry.recalculate(proof.calculation_id))
    except Exception as exc:  # CalculationError and friends
        return failed(
            CHECK_CALCULATION,
            f"重算 {proof.calculation_id} 失败：{type(exc).__name__}: {exc}",
            calculation_id=proof.calculation_id,
        )

    delta = abs(redone - float(proof.output))
    if delta > context.tolerance:
        return failed(
            CHECK_CALCULATION,
            f"公式重算不一致：记录 {proof.output} vs 重算 {redone}（差值 {delta:g}；"
            f"公式 {proof.formula}）",
            calculation_id=proof.calculation_id, recorded=proof.output, recomputed=redone,
        )
    return passed(
        CHECK_CALCULATION,
        f"{proof.formula} 重算一致（{redone}，{len(proof.inputs)} 个输入）",
        calculation_id=proof.calculation_id, recomputed=redone,
    )


def _registry_has(registry: Any, calculation_id: str) -> bool:
    has = getattr(registry, "has", None)
    if callable(has):
        return bool(has(calculation_id))
    records = getattr(registry, "records", None)
    if isinstance(records, list):
        return any(getattr(item, "calculation_id", None) == calculation_id for item in records)
    return True  # cannot tell — let recalculate() raise and report honestly


def verify_dependencies(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.7 — REQUIRED upstream blocked ⇒ this claim is BLOCKED (CARD-09 §7b)."""
    graph = context.graph
    if graph is None:
        return skipped(CHECK_DEPENDENCY, "未提供 ClaimGraph，依赖状态无法重算")
    claim = graph.claims.get(fpo.claim_id)
    if claim is None:
        return failed(
            CHECK_DEPENDENCY,
            f"claim {fpo.claim_id} 已不在图中，依赖关系无法复核",
        )

    required_blocked: List[Dict[str, str]] = []
    stale_seen: List[str] = []
    degraded: List[Dict[str, str]] = []
    recorded = {item.target_id: item.state for item in fpo.dependency_proof.upstream}
    drifted: List[Dict[str, str]] = []

    for target_type, target_id, role in resolve_targets(claim):
        state = upstream_state(graph, target_id, target_type)
        if recorded.get(target_id) not in (None, state):
            drifted.append({"target_id": target_id,
                            "recorded": recorded[target_id], "current": state})
        if role == ROLE_OPTIONAL:
            continue
        blocking = state in (UPSTREAM_BLOCKED, UPSTREAM_CONFLICTED, UPSTREAM_MISSING)
        if role == ROLE_REQUIRED:
            if blocking:
                required_blocked.append({"target_id": target_id, "state": state,
                                         "target_type": target_type})
            elif state == UPSTREAM_STALE:
                stale_seen.append(target_id)
        elif role == ROLE_SUPPORTING and (blocking or state == UPSTREAM_STALE):
            degraded.append({"target_id": target_id, "state": state, "role": ROLE_SUPPORTING})

    if required_blocked:
        return failed(
            CHECK_DEPENDENCY,
            "REQUIRED 依赖未通过："
            + "、".join(f"{item['target_id']}({item['state']})" for item in required_blocked),
            blocked=required_blocked, drifted=drifted,
        )
    if stale_seen:
        return CheckOutcome(
            CHECK_DEPENDENCY, "warn",
            f"REQUIRED 依赖已过期：{'、'.join(sorted(stale_seen))}，结论需刷新",
            STATUS_STALE,
            {"stale": sorted(stale_seen), "drifted": drifted},
        )
    if degraded:
        return warned(
            CHECK_DEPENDENCY,
            "SUPPORTING 依赖失效（覆盖下降，不阻断）："
            + "、".join(f"{item['target_id']}({item['state']})" for item in degraded),
            impact=STATUS_DEGRADED, degraded=degraded, drifted=drifted,
        )
    if drifted:
        return warned(
            CHECK_DEPENDENCY,
            "护照记录的依赖状态与当前图不一致（护照需要重新生成）："
            + "、".join(f"{item['target_id']}:{item['recorded']}→{item['current']}"
                        for item in drifted),
            impact=STATUS_DEGRADED, drifted=drifted,
        )
    return passed(CHECK_DEPENDENCY, "全部依赖当前均正常（REQUIRED 无阻断）")


def verify_assumptions(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7.8 — referenced assumptions must exist, and their grade must show."""
    ids = fpo.assumption_proof.assumption_ids
    if not ids:
        return skipped(CHECK_ASSUMPTION, "该 claim 不引用假设，检查跳过（§12 edge 2）")
    if context.assumption_registry is None:
        return skipped(
            CHECK_ASSUMPTION,
            f"claim 引用 {len(ids)} 条假设，但未提供 CARD-08 假设注册表，无法核对（不记通过）",
            assumption_ids=list(ids),
        )
    registry = context.assumption_registry
    missing = [aid for aid in ids if not _registry_has_assumption(registry, aid)]
    if missing:
        return failed(
            CHECK_ASSUMPTION,
            f"引用的假设不在注册表中：{', '.join(sorted(missing))}",
            missing=sorted(missing),
        )
    unsourced = sorted(
        aid for aid in ids if fpo.assumption_proof.grades.get(aid) == "assumption"
    )
    if unsourced:
        return warned(
            CHECK_ASSUMPTION,
            "结论依赖非 sourced 假设（CARD-08 的 `assumption:` 前缀）："
            + "、".join(unsourced)
            + "；数值链完整但依据是设定值，按 DEGRADED 呈现而非 VERIFIED",
            impact=STATUS_DEGRADED, unsourced=unsourced,
        )
    return passed(
        CHECK_ASSUMPTION, f"{len(ids)} 条假设均在注册表中且均为 sourced 依据",
        assumption_ids=list(ids),
    )


def _registry_has_assumption(registry: Any, assumption_id: str) -> bool:
    has = getattr(registry, "has", None)
    if callable(has):
        return bool(has(assumption_id))
    return True


def verify_validator_state(
    fpo: FinancialProofObject, context: ProofContext
) -> CheckOutcome:
    """§7.9 — re-derive the claim's validator state instead of trusting the field."""
    graph = context.graph
    if graph is None:
        return skipped(CHECK_VALIDATOR, "未提供 ClaimGraph，validator 状态无法重算")
    claim = graph.claims.get(fpo.claim_id)
    if claim is None:
        return failed(CHECK_VALIDATOR, f"claim {fpo.claim_id} 已不在图中")

    derived, dropped, trigger = claim_status(graph, claim)
    details: Dict[str, Any] = {
        "derived": derived,
        "stored": claim.validation_status,
        "coverage_dropped": dropped,
        "trigger": trigger,
    }
    if derived in (VALIDATION_BLOCKED, VALIDATION_UNSUPPORTED):
        return failed(
            CHECK_VALIDATOR,
            f"CARD-09 传播判定为 {derived}（{trigger or '依赖未通过'}），"
            "结论不得进入 memo 结论段",
            **details,
        )
    if derived == VALIDATION_STALE:
        return CheckOutcome(
            CHECK_VALIDATOR, "warn", f"CARD-09 传播判定为 {derived}，结论需刷新",
            STATUS_STALE, details,
        )
    if derived == VALIDATION_PENDING:
        return warned(
            CHECK_VALIDATOR, f"CARD-09 传播判定为 {derived}（尚有未决项）",
            impact=STATUS_DEGRADED, **details,
        )
    messages = [f"CARD-09 传播判定为 {derived}"]
    if dropped:
        messages.append(f"SUPPORTING 覆盖下降 {len(dropped)} 处")
    if claim.validation_status != derived:
        messages.append(
            f"figures 记录的状态是 {claim.validation_status}，与重算的 {derived} 不一致"
        )
        return warned(
            CHECK_VALIDATOR,
            "；".join(messages) + "（护照需重新生成）",
            impact=STATUS_DEGRADED,
            **details,
        )
    return passed(CHECK_VALIDATOR, "；".join(messages), **details)


def verify_integrity(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§12 edge 7 + §13 rule 1 — the proof hash must recompute.

    Scope, deliberately narrow. §13's own rule is "每个 passport 的 proof_hash
    可重算验证", and §12 edge 7 defines the failure as a hash failure — so those
    two are the only things that move the verdict. ``run_id`` is checked because
    it is the join key to CARD-09/CARD-12 (without it the passport cannot be
    found again), but a missing ``run_id`` is an identity gap, not a false
    conclusion, so it lands on DEGRADED rather than BLOCKED.

    ``audit_chain_ref`` and ``git_commit`` are **recorded, not escalated**. They
    come from CARD-07/CARD-12 inputs that §4 lists as optional, and escalating
    their absence would mean a clean filing could never reach VERIFIED — which
    would make the five-state vocabulary decorative and train users to ignore
    DEGRADED. They travel in ``provenance_gaps`` so nothing is hidden; what is
    being kept separate is "这条结论还站得住吗" from "这个构建可复现吗".
    """
    recomputed = fpo.proof_hash()
    if not fpo.integrity_proof.object_hash:
        return failed(
            CHECK_INTEGRITY,
            f"护照未封存（object_hash 为空），重算得到 {recomputed[:16]}…",
            recomputed=recomputed,
        )
    if recomputed != fpo.integrity_proof.object_hash:
        return failed(
            CHECK_INTEGRITY,
            "object_hash 重算不一致：护照已被改动或序列化不稳定"
            f"（记录 {fpo.integrity_proof.object_hash[:16]}… vs 重算 {recomputed[:16]}…）",
            recorded=fpo.integrity_proof.object_hash, recomputed=recomputed,
        )

    gaps: List[str] = []
    if not fpo.integrity_proof.audit_chain_ref:
        gaps.append("未关联 CARD-12 审计包（audit_chain_ref 为空）")
    if fpo.integrity_proof.git_commit in ("", COMMIT_UNKNOWN):
        gaps.append("未记录 git_commit，代码版本不可复现")
    if fpo.integrity_proof.proof_version != PROOF_VERSION:
        gaps.append(
            f"护照版本 {fpo.integrity_proof.proof_version} 与当前验证器 {PROOF_VERSION} 不同"
        )

    if not fpo.integrity_proof.run_id:
        return warned(
            CHECK_INTEGRITY,
            "object_hash 可重算，但护照未关联 run_id，无法与 CARD-09/12 关联"
            + (f"；{'；'.join(gaps)}" if gaps else ""),
            impact=STATUS_DEGRADED, provenance_gaps=gaps, run_id="",
        )

    note = f"object_hash 可重算，run {fpo.integrity_proof.run_id}"
    if gaps:
        note += "；" + "；".join(gaps) + "（已记录，不改变结论状态）"
    return passed(CHECK_INTEGRITY, note, provenance_gaps=gaps)


def verify_conflict(fpo: FinancialProofObject, context: ProofContext) -> CheckOutcome:
    """§7 merge rule — an undecided CARD-13 conflict makes the claim CONFLICTED."""
    metric = fpo.claim_metric
    period_key = fpo.claim_period_key or fpo.economic_period
    if not metric or not period_key:
        return skipped(
            CHECK_CONFLICT,
            "claim 未声明指标/期间，无法与 CARD-13 的冲突记录匹配（不记通过）",
        )
    scope = normalize_scope(fpo.scope)

    hits: List[ConflictRecord] = []
    for record in context.conflicts:
        if record.resolved:
            continue
        if record.metric != metric:
            continue
        if record.period_key and record.period_key != period_key:
            continue
        record_scope = normalize_scope(record.scope)
        if record_scope and scope and record_scope != scope:
            continue
        hits.append(record)

    if not hits:
        return passed(
            CHECK_CONFLICT,
            f"{metric}@{period_key} 无未决冲突（已核对 {len(context.conflicts)} 条记录）",
        )
    return CheckOutcome(
        CHECK_CONFLICT, "fail",
        f"{metric}@{period_key} 存在 {len(hits)} 条未决冲突："
        + "、".join(
            f"{record.conflict_id}（{record.value_a} vs {record.value_b}，{record.reason}）"
            for record in hits
        )
        + "；ACME/CARD-13 未裁决前该结论不得采用",
        STATUS_CONFLICTED,
        {"conflict_ids": [record.conflict_id for record in hits],
         "metric": metric, "period_key": period_key, "scope": scope},
    )


# ---------------------------------------------------------------------------
# §8 — verify_fpo / verify_claim
# ---------------------------------------------------------------------------
_CHECK_FUNCTIONS: Dict[str, Callable[[FinancialProofObject, ProofContext], CheckOutcome]] = {
    CHECK_SOURCE: verify_source,
    CHECK_PERIOD: verify_period,
    CHECK_SCOPE_UNIT: verify_scope_unit,
    CHECK_CALCULATION: verify_calculation,
    CHECK_DEPENDENCY: verify_dependencies,
    CHECK_ASSUMPTION: verify_assumptions,
    CHECK_VALIDATOR: verify_validator_state,
    CHECK_INTEGRITY: verify_integrity,
    CHECK_CONFLICT: verify_conflict,
}

assert tuple(_CHECK_FUNCTIONS) == PROOF_CHECKS, "检查函数表与 PROOF_CHECKS 必须同序同集"


def verify_fpo(fpo: FinancialProofObject, context: ProofContext) -> VerificationResult:
    """Run every §7 check against a passport and merge the verdict.

    A raising check is *not* swallowed into ``pass``: it becomes a ``fail`` with
    the exception named, because §11 forbids "验证器永远通过".
    """
    outcomes: List[CheckOutcome] = []
    for name in PROOF_CHECKS:
        function = _CHECK_FUNCTIONS[name]
        try:
            outcomes.append(function(fpo, context))
        except Exception as exc:  # noqa: BLE001 - deliberately broad, reported not hidden
            outcomes.append(
                failed(name, f"检查自身抛出 {type(exc).__name__}: {exc}")
            )
    checks = tuple(outcomes)
    overall, reasons = merge_status(checks)
    return VerificationResult(
        proof_id=fpo.proof_id,
        claim_id=fpo.claim_id,
        overall_status=overall,
        status_reasons=reasons,
        checks=checks,
        verify_source_version=VERIFIER_VERSION,
        verified_at=context.detected_at,
    )


def verify_claim(claim_id: str, context: ProofContext) -> VerificationResult:
    """§8 ★ 不依赖 LLM 重新回答「这条结论现在还算数吗」.

    Builds a fresh passport (so the answer reflects the *current* graph) and
    verifies it. Use :func:`verify_fpo` directly when you want to check a
    *stored* passport rather than regenerate one.
    """
    fpo = build_financial_proof_object(claim_id, context)
    return verify_fpo(fpo, context)


__all__ = [
    "DEFAULT_RECOMPUTE_TOLERANCE",
    "ProofContext",
    "build_financial_proof_object",
    "normalize_scope",
    "resolve_version_status",
    "verify_claim",
    "verify_conflict",
    "verify_calculation",
    "verify_dependencies",
    "verify_assumptions",
    "verify_fpo",
    "verify_integrity",
    "verify_period",
    "verify_scope_unit",
    "verify_source",
    "verify_validator_state",
]
