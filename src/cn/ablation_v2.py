"""CARD-11 §7b — the two innovation ablations that can be measured offline.

§7b requires every innovation to show a "without vs with" comparison, and the
CARD-11 report has always rendered that table as ``TARGET / TO BE MEASURED`` —
not out of laziness but because **nothing in the repository computed the
numbers**. :func:`src.cn.benchmark_v2.build_ablation_table` reads whatever it is
handed; no component ever handed it anything. This module is that missing
measurement, for the two comparisons whose inputs exist offline:

============================  ====================  ==================================
innovation                    baseline              treatment
============================  ====================  ==================================
ACME (CARD-22)                ``parser_only``       ``parser_plus_accounting_constraints``
Claim Passport (CARD-23)      ``citation_only``     ``fpo_verifier``
============================  ====================  ==================================

Four rules keep the result honest:

1. **The corpus declares ground truth, never the answer.** A case says "this
   bundle carries one injected identity residual"; it never says which detector
   should fire. Both detectors run the *real* code paths
   (:func:`~src.cn.acme.validate_multimodal_evidence`,
   :func:`~src.cn.proof.verify_claim`) and the measured value is whatever comes
   back. If a case fails to behave as its author intended, the case is fixed or
   removed — the expectation is not.
2. **Neither arm is a strawman invented for the comparison.** ``parser_only``
   is the extraction stage's own signal — the §7.1 dictionary matcher's
   ``metric_unknown_*`` output, produced here by really calling
   :func:`~src.cn.normalization.metrics.resolve_metric`.
   ``citation_only`` is :func:`~src.cn.graph.propagation.structural_status`:
   the CARD-09 binding rule that the system genuinely shipped with before
   CARD-23 existed. Symmetrically, the **treatment is the whole shipped
   pipeline**, not just the layer under test: the baseline names the *ablated*
   system, so the treatment keeps whatever the baseline had and adds to it
   (:func:`parser_plus_constraint_signals`).
3. **Recall is reported per defect family as well as overall, and the
   false-positive count on clean controls is reported next to it.** A detector
   that flags everything would otherwise post a perfect recall.
4. **Only defect-oriented signals count for the baseline.** The normalizer also
   attaches *informational provenance* notes to perfectly healthy facts (e.g.
   ``scope_declared_consolidated_from_provider_contract`` on every provider
   fact); counting those would make the baseline fire on every case and turn the
   comparison into noise. The exclusion is explicit in
   :func:`extraction_signals` rather than implicit.

Everything here is offline, deterministic and clock-free (the timestamp is a
module constant, so two runs are byte-identical).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .acme import DEFAULT_POLICY, validate_multimodal_evidence
from .acme.models import STATUS_VIOLATED, STATUS_WARNING
from .graph import (
    CLAIM_FACT,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    Claim,
    EvidenceNode,
    build_graph,
)
from .graph.propagation import structural_status
from .normalization.metrics import resolve_metric
from .normalization.models import NormalizedFact, PeriodKind, Scope, Version
from .proof import ProofContext, verify_claim
from .proof.models import STATUS_VERIFIED

#: Combined result file, consumed by ``scripts/run_benchmark_v2.py``.
ABLATION_FILENAME = "innovation_ablation.json"
ABLATION_MARKDOWN = "innovation_ablation.md"

#: Fixed, not ``datetime.now()``: the ablation is a pure function of its inputs,
#: and a clock read would make two identical runs differ for no reason.
MEASURED_AT = "2026-09-27T00:00:00+08:00"

SUITE_ID = "cn_agent_v2"
SECTION = "7b"

#: The two metric ids this module is responsible for, exactly as
#: ``ABLATION_COMPARISONS`` spells them.
METRIC_ACME = "financial_extraction_error_detection"
METRIC_PASSPORT = "unsupported_claim_detection"

#: Where a family's defect is (in)visible. ``extraction_lexical`` marks the
#: defects the extraction stage itself can notice; everything else is semantic.
FAMILY_CLEAN = "clean_control"
FAMILY_EXTRACTION = "extraction_lexical"


# --------------------------------------------------------------------------- #
# Shared result shape
# --------------------------------------------------------------------------- #
def _metric(
    numerator: int, denominator: int, note: str
) -> Dict[str, Any]:
    """Recall with its own denominator kept visible.

    ``build_ablation_table`` only needs ``value``; the counts are what make the
    number auditable, so they travel with it.
    """
    return {
        "value": (numerator / denominator) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "note": note,
    }


def _summarise(
    *,
    innovation: str,
    metric_name: str,
    cases: Sequence[Mapping[str, Any]],
    detector_role: str,
    corpus_note: str,
    extra_metrics: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fold per-case verdicts into one run summary."""
    defects = [c for c in cases if c["defect"] is not None]
    controls = [c for c in cases if c["defect"] is None]
    hits = [c for c in defects if c["detected"]]
    false_positives = [c for c in controls if c["detected"]]

    by_family: Dict[str, Dict[str, int]] = {}
    for case in defects:
        bucket = by_family.setdefault(case["family"], {"detected": 0, "total": 0})
        bucket["total"] += 1
        bucket["detected"] += 1 if case["detected"] else 0

    metrics: Dict[str, Dict[str, Any]] = {
        metric_name: _metric(
            len(hits),
            len(defects),
            f"{detector_role}：{len(hits)}/{len(defects)} 个注入缺陷被检出（含逐族拆解）。",
        ),
        "false_positive_rate_on_clean_controls": _metric(
            len(false_positives),
            len(controls),
            "对照用的无缺陷样本被误报的比例；检出率必须与它一起看。",
        ),
    }
    if extra_metrics:
        metrics.update(dict(extra_metrics))

    return {
        "metrics": metrics,
        "by_family": by_family,
        "case_count": len(cases),
        "cases": list(cases),
        "metadata": {
            "suite_id": SUITE_ID,
            "suite_version": SECTION,
            "innovation": innovation,
            "detector_role": detector_role,
            "corpus": corpus_note,
            "network": False,
            "measured_at": MEASURED_AT,
        },
    }


# --------------------------------------------------------------------------- #
# CARD-22 — ACME
# --------------------------------------------------------------------------- #
def _fact(
    metric: str,
    value: float,
    *,
    period: str = "2025FY",
    statement: str = "balance",
    document_id: str = "DOC-ABL",
    row: int = 1,
    table: str = "合并资产负债表",
    period_kind: PeriodKind = PeriodKind.POINT_IN_TIME,
    scope: Scope = Scope.CONSOLIDATED,
    version: Version = Version.AS_REPORTED,
    raw_value: Optional[str] = None,
    warnings: Tuple[str, ...] = (),
    source_table: Optional[str] = None,
) -> NormalizedFact:
    """One CARD-02 fact, shaped like the normalizer's output."""
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period}",
        metric=metric,
        value=float(value),
        currency="CNY",
        period_key=period,
        period_kind=period_kind,
        scope=scope,
        version=version,
        available_at="2026-04-20",
        source_fact_id=f"src_{document_id}_{metric}",
        document_id=document_id,
        row=row,
        raw_value=raw_value,
        source_table=source_table or table,
        statement_type=statement,
        warnings=warnings,
    )


def _cell(
    text: str,
    value: Optional[str],
    *,
    table: str,
    row: int,
    column: int,
    page: int = 1,
    document_id: str = "DOC-ABL",
) -> Any:
    """One CARD-01 table cell — the extractor's own output shape.

    F2 needs table geometry, which the projection to ``NormalizedFact`` throws
    away, so the case has to carry the cells themselves.
    """

    class _Cell:
        pass

    cell = _Cell()
    cell.raw_text = text
    cell.raw_value = value
    cell.table = table
    cell.row = row
    cell.column = column
    cell.page = page
    cell.document_id = document_id
    cell.kind = "table_cell"
    cell.fact_id = f"f_{table}_{row}_{column}"
    cell.bbox = (float(column), float(row), float(column + 1), float(row + 1))
    return cell


def _balance_table(end_assets: str) -> List[Any]:
    """A 合并资产负债表 whose subtotal either foots or does not."""
    table = "合并资产负债表"
    cells = [
        _cell("项目", None, table=table, row=0, column=0),
        _cell("期末余额", None, table=table, row=0, column=1),
    ]
    rows = [
        ("流动资产合计", "6,000,000"),
        ("非流动资产合计", "4,000,000"),
        ("资产总计", end_assets),
    ]
    for index, (label, value) in enumerate(rows, start=1):
        cells.append(_cell(label, None, table=table, row=index, column=0))
        cells.append(_cell(value, value, table=table, row=index, column=1))
    return cells


@dataclass(frozen=True)
class AcmeCase:
    """One bundle plus the ground truth about what was injected into it.

    ``inputs`` holds *only* what :func:`~src.cn.acme.validate_multimodal_evidence`
    accepts; anything that describes the case rather than feeding the engine
    (e.g. which label failed to resolve) lives on the case itself, so a case can
    never leak a non-parameter into the real call.
    """

    case_id: str
    defect: Optional[str]
    family: str
    visible_to_extraction: bool
    inputs: Mapping[str, Any] = field(repr=False, default_factory=dict)
    unresolved_labels: Tuple[str, ...] = ()


def _identity_bundle(residual: float) -> Dict[str, Any]:
    """资产总计 vs 负债 + 归母权益 (+ 少数股东)."""
    return {
        "facts": [
            _fact("total_assets", 10_000_000.0 + residual),
            _fact("total_liabilities", 6_000_000.0, row=2),
            _fact("equity_parent", 4_000_000.0, row=3),
        ]
    }


def _subtotal_bundle(end_assets: str) -> Dict[str, Any]:
    return {
        "facts": [
            _fact("total_assets", float(end_assets.replace(",", ""))),
            _fact("total_current_assets", 6_000_000.0, row=2),
            _fact("total_non_current_assets", 4_000_000.0, row=3),
        ],
        "raw_facts": _balance_table(end_assets),
    }


def _cross_source_bundle(snapshot_revenue: float) -> Dict[str, Any]:
    return {
        "facts": [
            _fact("revenue", 20_000_000.0, statement="income", table="合并利润表"),
        ],
        "snapshots": [
            _fact(
                "revenue",
                snapshot_revenue,
                statement="income",
                document_id="SNAP-ABL",
                table="snapshot:income",
                source_table="snapshot:income",
            )
        ],
        "snapshot_id": "SNAP-ABL",
    }


def _unknown_line_item_bundle(label: str) -> Dict[str, Any]:
    """A line item the §7.1 dictionary does not know.

    The metric id comes from really running the matcher, so this case cannot
    drift from the dictionary. If someone teaches the dictionary the label, the
    case silently stops being an "unknown line item" — and ``parser_only``'s
    recall would drop on its own, which is why
    ``tests/test_cn_ablation_v2.py`` pins the exact count rather than a bound.
    A drifting corpus must fail a test, not quietly shrink the baseline.
    """
    match = resolve_metric(label)
    return {"facts": [_fact(match.key, 1_234_567.0, statement="income", table="合并利润表")]}


def acme_cases() -> List[AcmeCase]:
    """The CARD-22 corpus: three clean controls, five injected defects.

    Families covered: ``accounting_identity`` (F1), ``table_structural`` (F2),
    ``cross_source`` (F5), and ``extraction_lexical`` (what the extraction stage
    notices on its own). The remaining ACME families — period relations (F3),
    cross-period continuity (F4), cross-modal (F6) — are **not** in this corpus;
    see :func:`coverage_note`.
    """
    return [
        # -- clean controls: nothing injected, so any detection is a false alarm
        AcmeCase(
            "clean_identity",
            None,
            FAMILY_CLEAN,
            False,
            _identity_bundle(residual=0.0),
        ),
        AcmeCase(
            "clean_subtotal",
            None,
            FAMILY_CLEAN,
            False,
            _subtotal_bundle("10,000,000"),
        ),
        AcmeCase(
            "clean_cross_source",
            None,
            FAMILY_CLEAN,
            False,
            _cross_source_bundle(20_000_000.0),
        ),
        # -- semantic defects: internally inconsistent, lexically spotless
        AcmeCase(
            "identity_residual",
            "balance sheet does not foot (资产 10.4M vs 负债+权益 10.0M)",
            "accounting_identity",
            False,
            _identity_bundle(residual=400_000.0),
        ),
        AcmeCase(
            "table_subtotal_residual",
            "小计勾稽不成立（明细合计 10.0M vs 印出的资产总计 11.0M）",
            "table_structural",
            False,
            _subtotal_bundle("11,000,000"),
        ),
        AcmeCase(
            "cross_source_disagreement",
            "同一指标两个来源差 60 万（文档 20.0M vs 快照 20.6M）",
            "cross_source",
            False,
            _cross_source_bundle(20_600_000.0),
        ),
        # -- lexical defects: the extraction stage can see these itself
        AcmeCase(
            "unknown_line_item_ratio",
            "§7.1 词典不认识的比率类行项目",
            FAMILY_EXTRACTION,
            True,
            _unknown_line_item_bundle("毛利率"),
            unresolved_labels=("毛利率",),
        ),
        AcmeCase(
            "unknown_line_item_allowance",
            "§7.1 词典不认识的备抵类行项目",
            FAMILY_EXTRACTION,
            True,
            _unknown_line_item_bundle("商誉减值准备特别科目"),
            unresolved_labels=("商誉减值准备特别科目",),
        ),
    ]


def extraction_signals(case: AcmeCase) -> List[str]:
    """What the extraction stage alone can see — the ``parser_only`` detector.

    Defect-oriented signals only:

    * a fact whose metric is the §7.1 dictionary's ``metric_unknown_*``
      placeholder (the label could not be resolved to a canonical key);

    Deliberately **excluded**, because they are not defects:
    ``scope_declared_consolidated_from_provider_contract`` and friends — the
    provenance notes the normalizer attaches to healthy provider facts. A
    detector that counted those would flag every provider-sourced fact and the
    comparison would measure noise instead of detection.
    """
    signals: List[str] = []
    for fact in case.inputs.get("facts", ()):
        if str(fact.metric).startswith("metric_unknown"):
            label = case.unresolved_labels[0] if case.unresolved_labels else ""
            signals.append(
                f"§7.1 词典不认识行项目 {label!r}，只能记为 {fact.metric}"
                "（无法确定口径与含义）"
            )
    return signals


def constraint_signals(case: AcmeCase) -> List[str]:
    """The constraint layer's own findings — the real ACME engine."""
    report = validate_multimodal_evidence(
        **case.inputs, policy=DEFAULT_POLICY, detected_at=MEASURED_AT
    )
    signals: List[str] = []
    for violation in report.violations:
        if violation.status in (STATUS_VIOLATED, STATUS_WARNING):
            signals.append(
                f"{violation.constraint_family}/{violation.constraint_id}"
                f"={violation.status} residual={violation.residual}"
            )
    for item in report.cross_modal:
        signals.append(f"cross_modal/{item.conflict_type}")
    return signals


def parser_plus_constraint_signals(case: AcmeCase) -> List[str]:
    """The treatment arm — the *whole* pipeline, not just its new layer.

    ``parser_only`` names the **ablated** system (the one with ACME removed), not
    a rival detector. So the treatment is the union: a pipeline that has ACME in
    it still runs the parser, and the parser's findings do not evaporate when the
    constraint layer is attached.

    Measuring the constraint layer alone would be a third configuration nobody
    ships, and on this corpus it scores *below* the baseline (3/5 vs 2/5 overall
    but 0/2 vs 2/2 on the lexical family) — which reads as "attaching ACME made
    detection worse". It did not. The two layers are complementary: the parser
    owns the lexical family, ACME owns the semantic one, and the system that
    ships both sees all five injected defects. Halving the treatment to make the
    delta look tidy would be exactly the kind of number-fudging §7b forbids.
    """
    return [*extraction_signals(case), *constraint_signals(case)]


def run_acme_ablation() -> Dict[str, Dict[str, Any]]:
    """Measure both configurations over the same corpus.

    One process, one corpus, one variable (the detector) — the project's rule for
    any performance claim.
    """
    cases = acme_cases()
    runs: Dict[str, Dict[str, Any]] = {}
    for label, detector, role in (
        (
            "parser_only",
            extraction_signals,
            "Parser only：只有抽取阶段自己的信号，没有约束层",
        ),
        (
            "parser_plus_accounting_constraints",
            parser_plus_constraint_signals,
            "Parser + accounting constraints：完整流水线（抽取信号 + ACME 六族约束）",
        ),
    ):
        verdicts = []
        for case in cases:
            signals = detector(case)
            verdicts.append(
                {
                    "case_id": case.case_id,
                    "defect": case.defect,
                    "family": case.family,
                    "visible_to_extraction": case.visible_to_extraction,
                    "detected": bool(signals),
                    "signals": signals,
                }
            )
        runs[label] = _summarise(
            innovation="ACME (CARD-22)",
            metric_name=METRIC_ACME,
            cases=verdicts,
            detector_role=role,
            corpus_note=coverage_note(),
        )
    return runs


def coverage_note() -> str:
    """Which ACME families this corpus exercises — stated, not implied."""
    return (
        "语料覆盖 accounting_identity(F1) / table_structural(F2) / cross_source(F5) "
        "与抽取层词表缺陷；period_relations(F3) / cross_period(F4) / cross_modal(F6) "
        "未进本语料（它们的输入在离线夹具里还没有可信构造），不冒充已覆盖。"
    )


# --------------------------------------------------------------------------- #
# CARD-23 — Claim Passport
# --------------------------------------------------------------------------- #
#: The filing bytes whose digest the context records. The clean case has to pass
#: the hash re-check, so the recorded hash is computed from these bytes rather
#: than written down.
_FILING_TEXT = "ablation-filing-bytes"


def _loads(document_id: str) -> Optional[bytes]:
    if document_id != "DOC-ABL":
        return None
    return _FILING_TEXT.encode("utf-8")


def _digest() -> str:
    return hashlib.sha256(_FILING_TEXT.encode("utf-8")).hexdigest()


def _evidence(
    evidence_id: str,
    *,
    metric: str = "revenue",
    value: float = 20_000_000.0,
    active: bool = True,
) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        kind="fact",
        document_id="DOC-ABL",
        page=12,
        snapshot_id="DOC-ABL",
        metric=metric,
        value=value,
        unit="元",
        period_key="2025FY",
        active=active,
    )


def _claim(
    claim_id: str,
    *,
    evidence_ids: Tuple[str, ...],
    metric: str = "revenue",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=CLAIM_FACT,
        text="2025 年营业收入为 2,000 万元",
        evidence_ids=evidence_ids,
        metric=metric,
        period_key="2025FY",
        validation_status=VALIDATION_SUPPORTED,
    )


@dataclass(frozen=True)
class PassportCase:
    """One claim plus the ground truth about its support."""

    case_id: str
    defect: Optional[str]
    family: str
    claim: Claim
    evidence: Mapping[str, EvidenceNode]
    context_overrides: Mapping[str, Any] = field(default_factory=dict, repr=False)


def passport_cases() -> List[PassportCase]:
    """The CARD-23 corpus: one clean control, four support defects.

    The four defects are the ways a citation can look present and still not
    support anything — which is exactly the gap ``citation_only`` cannot see.
    """
    return [
        PassportCase(
            "clean_supported",
            None,
            FAMILY_CLEAN,
            _claim("C-CLEAN", evidence_ids=("E-REV",)),
            {"E-REV": _evidence("E-REV")},
        ),
        PassportCase(
            "no_evidence_at_all",
            "claim 未绑定任何证据（孤儿 claim）",
            "orphan_claim",
            _claim("C-ORPHAN", evidence_ids=()),
            {},
        ),
        PassportCase(
            "ghost_evidence_id",
            "引用了账本里不存在的证据 id（E-GHOST）",
            "ghost_reference",
            _claim("C-GHOST", evidence_ids=("E-GHOST",)),
            {},
        ),
        PassportCase(
            "withdrawn_evidence",
            "唯一证据已被撤销（active=False），claim 却未随之降级",
            "withdrawn_evidence",
            _claim("C-WITHDRAWN", evidence_ids=("E-OLD",)),
            {"E-OLD": _evidence("E-OLD", active=False)},
        ),
        PassportCase(
            "superseded_period",
            "期间已被取代（SUPERSEDED），claim 仍按当期陈述",
            "superseded_period",
            _claim("C-SUPERSEDED", evidence_ids=("E-REV2",)),
            {"E-REV2": _evidence("E-REV2")},
            # `declared_versions` is CARD-13's supersession record reaching the
            # verifier; the claim's own period is untouched.
            {"declared_versions": {"C-SUPERSEDED": "SUPERSEDED"}},
        ),
    ]


def citation_only_signals(case: PassportCase) -> List[str]:
    """The ``citation_only`` detector — CARD-09's binding rule as it shipped.

    ``structural_status`` answers "does this claim carry a citation at all?".
    It never opens the ledger, so a citation to a non-existent id, to a
    withdrawn id, or to a superseded period all read as *supported*.
    """
    graph = build_graph(
        "RUN-ABL", claims=[case.claim], calculations=[], evidence_index=dict(case.evidence)
    )
    status = structural_status(graph, case.claim)
    if status == VALIDATION_UNSUPPORTED:
        return [f"structural_status={status}（claim 没有任何引用）"]
    assert status == VALIDATION_SUPPORTED, status
    return []


def fpo_signals(case: PassportCase) -> List[str]:
    """The ``fpo_verifier`` detector — the real five-state verifier."""
    graph = build_graph(
        "RUN-ABL", claims=[case.claim], calculations=[], evidence_index=dict(case.evidence)
    )
    context = ProofContext(
        graph=graph,
        run_id="RUN-ABL",
        document_hashes={"DOC-ABL": _digest()},
        document_loader=_loads,
        detected_at=MEASURED_AT,
        **dict(case.context_overrides),
    )
    result = verify_claim(case.claim.claim_id, context)
    if result.overall_status != STATUS_VERIFIED:
        detail = "; ".join(result.status_reasons) or "no reason recorded"
        return [f"overall_status={result.overall_status}：{detail}"]
    return []


def run_passport_ablation() -> Dict[str, Dict[str, Any]]:
    cases = passport_cases()
    runs: Dict[str, Dict[str, Any]] = {}
    for label, detector, role in (
        (
            "citation_only",
            citation_only_signals,
            "Citation only：CARD-09 的绑定规则 structural_status（只看有没有引用）",
        ),
        (
            "fpo_verifier",
            fpo_signals,
            "FPO verifier：verify_claim 的九项检查 / 五态合并",
        ),
    ):
        verdicts = []
        for case in cases:
            signals = detector(case)
            verdicts.append(
                {
                    "case_id": case.case_id,
                    "defect": case.defect,
                    "family": case.family,
                    "visible_to_extraction": False,
                    "detected": bool(signals),
                    "signals": signals,
                }
            )
        runs[label] = _summarise(
            innovation="Claim Passport (CARD-23)",
            metric_name=METRIC_PASSPORT,
            cases=verdicts,
            detector_role=role,
            corpus_note=(
                "语料覆盖 orphan claim / 僵尸引用 / 已撤销证据 / 已被取代期间四类"
                "“引用在、支撑不在”的支撑缺陷，外加一个健康对照。"
                "数字重算一致性（calculation reproducibility）与陈旧度（stale claim）"
                "两条指标不在此语料内，不冒充已测。"
            ),
        )
    return runs


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def ablation_runs() -> Dict[str, Dict[str, Any]]:
    """Everything measured, keyed exactly as ``ABLATION_COMPARISONS`` names it."""
    return {**run_acme_ablation(), **run_passport_ablation()}


def measured_only(runs: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Drop any run whose headline metric came back ``None``.

    ``build_ablation_table`` treats a half-measured comparison as
    ``TARGET / TO BE MEASURED``, which is the honest state — but it can only do
    that if the missing side is *absent* rather than ``0``.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, summary in runs.items():
        metrics = summary.get("metrics", {})
        values = [m.get("value") for m in metrics.values() if isinstance(m, Mapping)]
        if any(value is not None for value in values):
            out[name] = dict(summary)
    return out


def write_ablation(directory: Path | str) -> Dict[str, Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    runs = ablation_runs()
    payload = {
        "suite_id": SUITE_ID,
        "section": SECTION,
        "measured_at": MEASURED_AT,
        "runs": runs,
    }
    json_path = directory / ABLATION_FILENAME
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = directory / ABLATION_MARKDOWN
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def load_ablation(directory: Path | str) -> Dict[str, Dict[str, Any]]:
    """Read a previously written ablation, or ``{}`` when there is none.

    Missing file means "not measured yet", never "measured as zero": the report
    then falls back to ``TARGET / TO BE MEASURED``.
    """
    path = Path(directory) / ABLATION_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    runs = payload.get("runs")
    return dict(runs) if isinstance(runs, Mapping) else {}


def render_markdown(payload: Mapping[str, Any]) -> str:
    runs: Mapping[str, Any] = payload.get("runs", {})
    lines: List[str] = [
        f"# §7b 消融实测 · {payload.get('suite_id')}",
        "",
        f"测量时间戳：`{payload.get('measured_at')}`（固定值，非当前钟，两次运行逐字节一致）",
        "",
    ]
    for name in ("parser_only", "parser_plus_accounting_constraints", "citation_only", "fpo_verifier"):
        summary = runs.get(name)
        if not summary:
            continue
        meta = summary.get("metadata", {})
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- 创新：{meta.get('innovation')} ｜ 角色：{meta.get('detector_role')}")
        for metric_name, entry in summary.get("metrics", {}).items():
            value = entry.get("value")
            shown = "N/A" if value is None else f"{float(value):.4f}"
            lines.append(
                f"- `{metric_name}` = **{shown}**"
                f"（{entry.get('numerator')}/{entry.get('denominator')}）"
            )
        lines.append("")
        lines.append("| 用例 | 缺陷族 | 有缺陷 | 检出 | 信号 |")
        lines.append("|---|---|---|---|---|")
        for case in summary.get("cases", []):
            defect = case.get("defect") or "—（对照）"
            signals = "；".join(case.get("signals") or []) or "—"
            signals = signals.replace("|", "/")
            lines.append(
                f"| `{case['case_id']}` | {case['family']} | {defect} | "
                f"{'是' if case['detected'] else '否'} | {signals} |"
            )
        lines.append("")
        lines.append(f"> 语料：{meta.get('corpus')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CARD-11 §7b 消融实测（离线）")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parents[2] / "output" / "benchmark" / SUITE_ID / "ablation"),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    paths = write_ablation(args.out)
    for name, summary in ablation_runs().items():
        metrics = summary["metrics"]
        headline = metrics[next(iter(metrics))]
        fp = metrics["false_positive_rate_on_clean_controls"]
        print(
            f"{name}: {headline['numerator']}/{headline['denominator']}"
            f" = {headline['value']!r} ｜ 误报 {fp['numerator']}/{fp['denominator']}"
        )
    print(f"     {paths['json']}")
    print(f"     {paths['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
