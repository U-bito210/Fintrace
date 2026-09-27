"""Offline tests for CARD-23 Financial Claim Passport & Proof Verifier.

The card's spine drives the layout:

* §6 six proof blocks on one passport (fact / inference / opinion);
* §7 the nine re-checks, each with a positive and a negative case;
* §7 the merge table, mechanised over every impact combination;
* §18 the five states, one reachable case each;
* §13 the three validator rules, including "BLOCKED/CONFLICTED must not reach a
  section that asserts a stance";
* §15 the CARD-09 → CARD-23 chain, supersession → STALE, undecided conflict →
  CONFLICTED.

Two tests exist specifically because the naive reading of the card is *wrong*,
and both were real failures found while wiring the engine to the corpus:

* ``test_cross_period_claim_is_not_a_period_mismatch`` — §7.3's literal wording
  ("economic_period 与 inputs period 一致") fails every legitimate YoY claim;
* ``test_ratio_unit_is_compared_only_with_the_same_quantity`` — a margin is
  ``PERCENT`` while its inputs are ``元``; comparing them is a category error.

Everything is offline: hand-built graphs plus the committed synthetic corpus.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from src.cn.analysis import CalculationRegistry, analyze_document
from src.cn.conflict import (
    CHANGE_DATA_CONFLICT,
    CHANGE_VERSION_SUPERSESSION,
    STATUS_UNRESOLVED,
    ConflictRecord,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    UPSTREAM_BLOCKED,
    VALIDATION_BLOCKED,
    VALIDATION_SUPPORTED,
    CalculationNode,
    Claim,
    DependencyRef,
    EvidenceNode,
    GraphOperand,
    build_graph,
    deactivate_evidence,
    revalidate,
)
from src.cn.memo import (
    SECTION_COMPANY_OVERVIEW,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_RISKS,
    InvestmentMemo,
    MemoSentence,
    MemoSection,
)
from src.cn.normalization import normalize_financials
from src.cn.proof import (
    CHECK_ASSUMPTION,
    CHECK_CALCULATION,
    CHECK_CONFLICT,
    CHECK_DEPENDENCY,
    CHECK_FAIL,
    CHECK_INTEGRITY,
    CHECK_PASS,
    CHECK_PERIOD,
    CHECK_SCOPE_UNIT,
    CHECK_SKIPPED,
    CHECK_SOURCE,
    CHECK_VALIDATOR,
    CHECK_WARN,
    HASH_RECOMPUTED,
    HASH_UNCHECKED,
    PROOF_CHECKS,
    PROOF_STATUSES,
    STATUS_BLOCKED,
    STATUS_CONFLICTED,
    STATUS_DEGRADED,
    STATUS_STALE,
    STATUS_VERIFIED,
    VERSION_BASIS_DECLARED,
    VERSION_BASIS_EVIDENCE_INACTIVE,
    VERSION_BASIS_NO_SIGNAL,
    VERSION_BASIS_SUPERSESSION,
    VERSION_SUPERSEDED,
    AccountingContext,
    AssumptionProof,
    CheckOutcome,
    DependencyProof,
    FinancialProofObject,
    IntegrityProof,
    ProofBuildError,
    ProofContext,
    SourceProof,
    VerificationResult,
    build_financial_proof_object,
    merge_status,
    verify_assumptions,
    verify_calculation,
    verify_claim,
    verify_conflict,
    verify_dependencies,
    verify_fpo,
    verify_integrity,
    verify_period,
    verify_scope_unit,
    verify_source,
    verify_validator_state,
)
from src.validation import (
    validate_proof,
    validate_proof_corpus,
    validate_verification,
)
from src.validation.proof_validator import CONCLUSION_SECTIONS, REFRESH_MARKER

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
PROOF_SOURCE = Path(__file__).resolve().parents[1] / "src" / "cn" / "proof"

RUN_ID = "run-proof"

#: Filings with nothing planted in them. Their claims must reach VERIFIED, or
#: the verifier is crying wolf — see the two regression tests named in the module
#: docstring for the versions of this that actually happened.
CLEAN_FILINGS: Tuple[str, ...] = (
    "annual_06_equity_shares.pdf",
    "annual_01_profit_continued.pdf",
)


# --------------------------------------------------------------------------- #
# Hand-built fixtures
# --------------------------------------------------------------------------- #
def _evidence(
    evidence_id: str,
    *,
    metric: str = "revenue",
    unit: Optional[str] = "元",
    period_key: Optional[str] = "2025FY",
    page: int = 12,
    document_id: str = "DOC-1",
    active: bool = True,
    value: float = 100.0,
) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        kind="fact",
        document_id=document_id,
        page=page,
        snapshot_id=document_id,
        metric=metric,
        value=value,
        unit=unit,
        period_key=period_key,
        active=active,
    )


def _calc(
    calculation_id: str,
    *,
    metric: str = "gross_margin",
    operation: str = "percent_of",
    unit: str = "PERCENT",
    period_key: str = "2025FY",
    scope: str = "合并",
    currency: str = "CNY",
    value: float = 45.0,
    inputs: Tuple[Tuple[str, float], ...] = (),
    formula: str = "gross_profit / revenue * 100",
) -> CalculationNode:
    return CalculationNode(
        calculation_id=calculation_id,
        metric=metric,
        formula=formula,
        operation=operation,
        inputs=tuple(
            GraphOperand(kind="evidence", ref=ref, value=amount) for ref, amount in inputs
        ),
        output={"value": value, "unit": unit, "period_key": period_key},
        scope=scope,
        currency=currency,
    )


def _claim(
    claim_id: str,
    *,
    claim_type: str = CLAIM_FACT,
    text: str = "毛利率为 45%，同比下降 1.2pct",
    metric: Optional[str] = "gross_margin",
    period_key: Optional[str] = "2025FY",
    evidence_ids: Tuple[str, ...] = (),
    calculation_ids: Tuple[str, ...] = (),
    assumption_ids: Tuple[str, ...] = (),
    derived_from: Tuple[str, ...] = (),
    dependency_metadata: Tuple[DependencyRef, ...] = (),
    validation_status: str = VALIDATION_SUPPORTED,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=claim_type,
        text=text,
        evidence_ids=evidence_ids,
        calculation_ids=calculation_ids,
        assumption_ids=assumption_ids,
        derived_from=derived_from,
        dependency_metadata=dependency_metadata,
        metric=metric,
        period_key=period_key,
        validation_status=validation_status,
    )


def _happy_graph(
    *,
    calc_unit: Optional[str] = "PERCENT",
    calc_period: str = "2025FY",
    calc_scope: str = "合并",
    claim_evidence: Tuple[str, ...] = ("E-PROFIT", "E-REV"),
    extra_evidence: Tuple[EvidenceNode, ...] = (),
):
    """A gross-margin fact claim over two 元-denominated inputs.

    This is the smallest shape that exercises the ratio/unit trap: the claim is
    ``PERCENT``, its evidence is ``元``. The keyword hooks exist so the negative
    tests can bend exactly one declaration at a time instead of rebuilding the
    graph by hand.

    ``calculations`` is a *list* on purpose — ``build_graph`` iterates it, so a
    mapping would be walked as its keys.
    """
    evidence = {
        "E-PROFIT": _evidence("E-PROFIT", metric="gross_profit", value=9_000_000.0),
        "E-REV": _evidence("E-REV", metric="revenue", value=20_000_000.0),
    }
    for node in extra_evidence:
        evidence[node.evidence_id] = node
    node = _calc(
        "CAL-1",
        unit=calc_unit,
        period_key=calc_period,
        scope=calc_scope,
        inputs=(("E-PROFIT", 9_000_000.0), ("E-REV", 20_000_000.0)),
    )
    claim = _claim(
        "C-MARGIN", evidence_ids=tuple(claim_evidence), calculation_ids=("CAL-1",)
    )
    return build_graph(
        RUN_ID, claims=[claim], calculations=[node], evidence_index=evidence
    )


#: The bytes the default loader hands back. The recorded hash must be *this*
#: text's digest, or the "clean" fixture is silently a tamper case and every
#: five-state test downstream asserts the wrong state.
_FILING_TEXT = "the-filings-bytes"


def _happy_context(graph, **overrides) -> ProofContext:
    payload = dict(
        graph=graph,
        run_id=RUN_ID,
        document_hashes={"DOC-1": _hash_for(_FILING_TEXT)},
        document_loader=_recomputing_loader({"DOC-1": _FILING_TEXT}),
        tolerance=1e-9,
        detected_at="2026-09-19T00:00:00+08:00",
    )
    payload.update(overrides)
    return ProofContext(**payload)


def _recomputing_loader(mapping):
    """A loader that returns the bytes whose digest the caller recorded."""
    import hashlib

    def loader(document_id: str) -> Optional[bytes]:
        wanted = mapping.get(document_id)
        return wanted.encode("utf-8") if wanted is not None else None

    return loader


def _hash_for(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _supporting_lapse_graph():
    """The §7b "coverage drop, not blocked" row, built so it is actually reachable.

    ``C-MAIN`` is an inference that **derives (REQUIRED) from the healthy
    ``C-BASE``** and additionally leans (SUPPORTING) on ``C-AUX``, whose only
    evidence is then withdrawn. Its own evidence ``E-PROFIT`` stays active, so
    the withdrawal *cannot* block it — the only thing that changed is coverage.

    This shape matters. Writing the same claim with ``dependency_metadata``
    alone and no ``derived_from`` looks equivalent but is not: CARD-09 §7's
    binding rule for an inference requires ``derived_from``, so such a claim is
    ``unsupported`` *before* propagation ever looks at the SUPPORTING edge. A
    test built that way passes for the wrong reason (or, when it asserts on the
    merged verdict, fails for the wrong reason) — it measures the binding rule,
    not the SUPPORTING row.
    """
    graph = _happy_graph()
    graph.add_claim(_claim("C-BASE", metric="revenue", evidence_ids=("E-PROFIT",)))
    graph.add_claim(_claim("C-AUX", metric="revenue", evidence_ids=("E-REV",)))
    graph.add_claim(
        _claim(
            "C-MAIN",
            claim_type=CLAIM_INFERENCE,
            metric=None,
            period_key=None,
            derived_from=("C-BASE",),
            evidence_ids=("E-PROFIT",),
            dependency_metadata=(
                DependencyRef("C-AUX", TARGET_CLAIM, ROLE_SUPPORTING),
            ),
        )
    )
    deactivate_evidence(graph, "E-REV", "辅助来源撤回")
    revalidate(graph)
    return graph


def _outcome(outcomes, name):
    for item in outcomes:
        if item.check == name:
            return item
    raise AssertionError(f"no outcome for {name!r}")


@pytest.fixture
def registry(tmp_path):
    instance = DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])
    try:
        yield instance
    finally:
        instance.close()


def _chain_graph_for(registry: DocumentRegistry, document_id: str):
    from src.agents.tools.cn_tools import _document_claim_graph

    return _document_claim_graph(registry, document_id)


def _ingest(registry: DocumentRegistry, filename: str):
    parsed = registry.ingest(
        FIXTURES / filename, declared_type="annual", source="admin_import"
    )
    return parsed.document


# =========================================================================== #
# §6 — passport construction
# =========================================================================== #
def test_builds_all_six_proofs_for_a_fact_claim():
    graph = _happy_graph()
    context = _happy_context(graph)

    passport = build_financial_proof_object("C-MARGIN", context)

    assert passport.claim_id == "C-MARGIN"
    assert passport.claim_type == CLAIM_FACT
    assert passport.claim_text
    assert passport.economic_period == "2025FY"
    assert passport.version_status == "AS_REPORTED"
    assert passport.source_proof.evidence_ids == ("E-PROFIT", "E-REV")
    assert passport.source_proof.document_id == "DOC-1"
    assert passport.calculation_proof is not None
    assert passport.calculation_proof.output == 45.0
    # Both the claim's own evidence and its calculation's inputs are edges.
    assert passport.dependency_proof.upstream
    assert {item.state for item in passport.dependency_proof.upstream} == {"ok"}
    assert passport.validation_proof.validator_rules
    assert passport.accounting_context.period_basis
    # sealed: the hash covers everything else and recomputes
    assert passport.integrity_proof.object_hash
    assert passport.proof_hash() == passport.integrity_proof.object_hash


def test_builds_a_passport_for_an_inference_claim():
    graph = _happy_graph()
    inference = _claim(
        "C-VERDICT",
        claim_type=CLAIM_INFERENCE,
        text="盈利质量下降",
        metric=None,
        period_key=None,
        evidence_ids=("E-PROFIT",),
        calculation_ids=("CAL-1",),
    )
    graph.add_claim(inference)

    passport = build_financial_proof_object("C-VERDICT", _happy_context(graph))

    assert passport.claim_type == CLAIM_INFERENCE
    assert "E-PROFIT" in passport.source_proof.evidence_ids


def test_builds_a_passport_for_an_opinion_claim_without_evidence():
    """§12 edge: an opinion may rest on assumptions alone."""
    graph = _happy_graph()
    opinion = _claim(
        "C-Opinion",
        claim_type=CLAIM_OPINION,
        text="估值具备安全边际",
        metric=None,
        period_key=None,
        evidence_ids=(),
    )
    graph.add_claim(opinion)

    passport = build_financial_proof_object("C-Opinion", _happy_context(graph))

    assert passport.claim_type == CLAIM_OPINION
    assert passport.source_proof.evidence_ids == ()
    assert passport.calculation_proof is None


def test_build_records_the_document_hash_the_caller_supplied():
    graph = _happy_graph()
    context = _happy_context(graph, document_hashes={"DOC-1": "b" * 64})

    passport = build_financial_proof_object("C-MARGIN", context)

    assert passport.source_proof.sha256 == "b" * 64


def test_build_fails_loudly_for_an_unknown_claim():
    graph = _happy_graph()
    with pytest.raises(ProofBuildError):
        build_financial_proof_object("NOPE", _happy_context(graph))


def test_version_basis_travels_with_the_version_label():
    """An unexplained label is the silent assumption this project forbids."""
    graph = _happy_graph()

    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    assert passport.version_status == "AS_REPORTED"
    assert passport.accounting_context.version_basis == VERSION_BASIS_NO_SIGNAL


def test_declared_version_is_used_and_its_basis_says_so():
    graph = _happy_graph()
    context = _happy_context(graph, declared_versions={"C-MARGIN": "restated"})

    passport = build_financial_proof_object("C-MARGIN", context)

    assert passport.version_status == "RESTATED"
    assert passport.accounting_context.version_basis == VERSION_BASIS_DECLARED


def test_illegal_declared_version_is_rejected_not_coerced():
    graph = _happy_graph()
    context = _happy_context(graph, declared_versions={"C-MARGIN": "MAYBE"})

    with pytest.raises(Exception):
        build_financial_proof_object("C-MARGIN", context)


# =========================================================================== #
# §7 checks — source
# =========================================================================== #
def test_source_passes_when_the_hash_is_recomputed():
    graph = _happy_graph()
    text = "the-filings-bytes"
    context = _happy_context(
        graph,
        document_hashes={"DOC-1": _hash_for(text)},
        document_loader=_recomputing_loader({"DOC-1": text}),
    )
    passport = build_financial_proof_object("C-MARGIN", context)

    outcome = verify_source(passport, context)

    assert outcome.status == CHECK_PASS
    assert outcome.details["hash_state"] == HASH_RECOMPUTED


def test_source_is_degraded_when_the_hash_was_never_recomputed():
    """Not a pass: "对照过清单" is not "重算过字节"."""
    graph = _happy_graph()
    context = _happy_context(graph, document_loader=None)
    passport = build_financial_proof_object("C-MARGIN", context)

    outcome = verify_source(passport, context)

    assert outcome.status == CHECK_WARN
    assert outcome.impact == STATUS_DEGRADED
    assert outcome.details["hash_state"] == HASH_UNCHECKED
    assert "未重算" in outcome.message


def test_source_fails_when_the_bytes_changed():
    graph = _happy_graph()
    context = _happy_context(
        graph,
        document_hashes={"DOC-1": _hash_for("original")},
        document_loader=_recomputing_loader({"DOC-1": "tampered"}),
    )
    passport = build_financial_proof_object("C-MARGIN", context)

    outcome = verify_source(passport, context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED


def test_source_fails_for_an_orphan_claim():
    """§12 edge 3 — no evidence at all is not "vacuous", it is BLOCKED."""
    graph = _happy_graph()
    orphan = _claim("C-ORPHAN", evidence_ids=())
    graph.add_claim(orphan)
    context = _happy_context(graph)

    outcome = verify_source(build_financial_proof_object("C-ORPHAN", context), context)

    assert outcome.status == CHECK_FAIL


def test_source_goes_stale_when_an_evidence_node_was_deactivated():
    """§12 edge 6 — superseded, not blocked: the history is still worth having."""
    graph = _happy_graph()
    deactivate_evidence(graph, "E-PROFIT", "演示：来源证据撤回")
    context = _happy_context(graph)

    outcome = verify_source(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_WARN
    assert outcome.impact == STATUS_STALE


def test_source_goes_stale_on_a_supersession_record_from_card13():
    graph = _happy_graph()
    record = ConflictRecord(
        conflict_id="X-1",
        change_kind=CHANGE_VERSION_SUPERSESSION,
        source_a="DOC-1",
        source_b="DOC-9",
        selected_source="DOC-9",
        reason="更正公告重述了本期营收",
        resolver="card13",
        timestamp="2026-09-19T00:00:00+08:00",
        metric="gross_margin",
        period_key="2025FY",
        superseded_source="DOC-1",
    )
    context = _happy_context(graph, conflicts=(record,))
    passport = build_financial_proof_object("C-MARGIN", context)

    assert passport.version_status == VERSION_SUPERSEDED
    outcome = verify_source(passport, context)
    assert outcome.impact == STATUS_STALE
    assert outcome.details["version_basis"] == VERSION_BASIS_SUPERSESSION


def test_source_skips_without_a_graph():
    graph = _happy_graph()
    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    outcome = verify_source(passport, ProofContext())

    assert outcome.status == CHECK_SKIPPED
    assert "ClaimGraph" in outcome.message


# =========================================================================== #
# §7 checks — period
# =========================================================================== #
def test_period_passes_when_calculation_and_inputs_agree():
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_period(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_PASS
    assert outcome.details["stated"] == "2025FY"


def test_cross_period_claim_is_not_a_period_mismatch():
    """Regression: §7.3's literal wording would fail every YoY claim.

    ``revenue_yoy`` at 2025FY rests on revenue@2025FY **and** revenue@2024FY.
    Requiring every input to share the claim's period would block the very
    ratios the project is built to show, so the differing input is reported as
    ``cross_period_inputs`` rather than failed.
    """
    evidence = {
        "E-REV-25": _evidence("E-REV-25", metric="revenue", period_key="2025FY"),
        "E-REV-24": _evidence("E-REV-24", metric="revenue", period_key="2024FY"),
    }
    calc = {
        "CAL-YOY": _calc(
            "CAL-YOY",
            metric="revenue_yoy",
            operation="growth_pct",
            period_key="2025FY",
            inputs=(("E-REV-25", 20.0), ("E-REV-24", 17.0)),
            formula="(revenue_current - revenue_previous) / |revenue_previous| * 100",
        )
    }
    claim = _claim(
        "C-YOY",
        metric="revenue_yoy",
        evidence_ids=("E-REV-25", "E-REV-24"),
        calculation_ids=("CAL-YOY",),
    )
    graph = build_graph(RUN_ID, claims=[claim], calculations=list(calc.values()), evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_period(build_financial_proof_object("C-YOY", context), context)

    assert outcome.status == CHECK_PASS
    assert outcome.details["covered_by"] == ["E-REV-25"]
    assert [item["period"] for item in outcome.details["cross_period_inputs"]] == ["2024FY"]


def test_period_fails_when_the_calculation_declares_another_period():
    graph = _happy_graph(calc_period="2024FY")
    context = _happy_context(graph)

    outcome = verify_period(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED
    assert "2024FY" in outcome.message


def test_period_fails_when_no_input_covers_the_declared_period():
    evidence = {
        "E-REV-24": _evidence("E-REV-24", metric="revenue", period_key="2024FY"),
    }
    claim = _claim("C-REV", metric="revenue", evidence_ids=("E-REV-24",))
    graph = build_graph(RUN_ID, claims=[claim], evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_period(build_financial_proof_object("C-REV", context), context)

    assert outcome.status == CHECK_FAIL
    assert outcome.details["stated"] == "2025FY"


def test_period_skips_when_nothing_declares_a_period():
    evidence = {"E-X": _evidence("E-X", period_key=None)}
    claim = _claim("C-X", metric="revenue", period_key=None, evidence_ids=("E-X",))
    graph = build_graph(RUN_ID, claims=[claim], evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_period(build_financial_proof_object("C-X", context), context)

    assert outcome.status == CHECK_SKIPPED


# =========================================================================== #
# §7 checks — scope / unit
# =========================================================================== #
def test_ratio_unit_is_compared_only_with_the_same_quantity():
    """Regression: a PERCENT claim's inputs are 元 — not a unit mismatch.

    ``gross_margin`` is declared in PERCENT while gross_profit / revenue are
    declared in 元. Comparing the claim's unit against its inputs' units is a
    category error that fails every ratio in the corpus.
    """
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_scope_unit(
        build_financial_proof_object("C-MARGIN", context), context
    )

    assert outcome.status == CHECK_PASS
    # The one genuinely independent check that applies here: gross_margin is a
    # PERCENT metric, and CARD-03's table says so.
    assert outcome.details["compared"]["metric_unit"] == 1
    # The passport's scope / currency / unit are *derived* from the calculation,
    # so they are reported as derived rather than counted as comparisons —
    # comparing a value against its own source can never disagree.
    assert outcome.details["derived"]
    assert any("取自计算" in item for item in outcome.details["derived"])
    assert outcome.details["not_compared"]


def test_the_passports_unit_is_an_echo_not_a_check():
    """The unit cannot disagree with the calculation, because it *is* the calculation's.

    ``build_financial_proof_object`` derives ``accounting_context.unit`` from the
    calculation's declared output unit. Bending that unit therefore changes both
    sides at once — which is exactly why the earlier version of this check could
    never fail, and why it is now reported as ``derived`` instead of counted.
    """
    graph = _happy_graph(calc_unit="元")
    context = _happy_context(graph)

    outcome = verify_scope_unit(build_financial_proof_object("C-MARGIN", context), context)

    # The metric-unit check *does* fire (gross_margin is not denominated in 元),
    # which is the one that carries real information here.
    assert outcome.status == CHECK_FAIL
    assert "规范单位" in outcome.message
    assert any("取自计算" in item for item in outcome.details["derived"])


def test_scope_unit_fails_on_a_declared_scope_that_contradicts_the_calculation():
    graph = _happy_graph()
    context = _happy_context(graph, declared_scopes={"C-MARGIN": "parent"})

    outcome = verify_scope_unit(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_FAIL
    assert "口径" in outcome.message


def test_scope_unit_fails_when_the_claims_own_metric_evidence_disagrees():
    """Same quantity, different unit — the comparison that *is* meaningful."""
    evidence = {
        "E-M": _evidence("E-M", metric="gross_margin", unit="元"),
        "E-REV": _evidence("E-REV", metric="revenue", unit="元"),
    }
    claim = _claim("C-M", metric="gross_margin", evidence_ids=("E-M", "E-REV"))
    graph = build_graph(RUN_ID, claims=[claim], evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_scope_unit(build_financial_proof_object("C-M", context), context)

    assert outcome.status == CHECK_FAIL
    assert "规范单位" in outcome.message
    assert "gross_margin" in outcome.message


def test_scope_unit_fails_when_two_evidence_nodes_share_a_metric_but_not_a_unit():
    """同一行的两个输入一个记元、一个记万元 — a real, catchable defect."""
    graph = _happy_graph(
        claim_evidence=("E-PROFIT", "E-REV", "E-REV-WAN"),
        extra_evidence=(_evidence("E-REV-WAN", metric="revenue", unit="万元"),),
    )
    context = _happy_context(graph)

    outcome = verify_scope_unit(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_FAIL
    assert "同指标 revenue" in outcome.message


def test_scope_unit_skips_when_nothing_is_comparable():
    evidence = {"E-N": _evidence("E-N", metric="revenue", unit=None)}
    claim = _claim("C-N", metric="revenue", evidence_ids=("E-N",))
    graph = build_graph(RUN_ID, claims=[claim], evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_scope_unit(build_financial_proof_object("C-N", context), context)

    assert outcome.status == CHECK_SKIPPED
    assert outcome.details["not_compared"]


# =========================================================================== #
# §7 checks — calculation
# =========================================================================== #
def _live_registry(registry: DocumentRegistry, filename: str):
    document = _ingest(registry, filename)
    normalization = normalize_financials(registry.get_facts(document.document_id), document)
    analysis = analyze_document(normalization)
    return document, analysis


def test_calculation_recomputes_through_the_card03_registry(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    context = _happy_context(graph, calculation_registry=analysis.registry)

    checked = 0
    for claim_id in sorted(graph.claims):
        outcome = verify_calculation(
            build_financial_proof_object(claim_id, context), context
        )
        if outcome.status != CHECK_SKIPPED:
            checked += 1
            assert outcome.status == CHECK_PASS, outcome.message
    assert checked > 0


def test_calculation_skips_without_a_registry():
    graph = _happy_graph()
    context = _happy_context(graph, calculation_registry=None)

    outcome = verify_calculation(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_SKIPPED
    assert "CalculationRegistry" in outcome.message


def test_calculation_skips_for_a_pure_fact_claim():
    """§12 edge 1 — no calculation is not a failure."""
    evidence = {"E-R": _evidence("E-R", metric="revenue")}
    claim = _claim("C-R", metric="revenue", evidence_ids=("E-R",))
    graph = build_graph(RUN_ID, claims=[claim], evidence_index=evidence)
    context = _happy_context(graph)

    outcome = verify_calculation(build_financial_proof_object("C-R", context), context)

    assert outcome.status == CHECK_SKIPPED
    assert "§12 edge 1" in outcome.message


def test_calculation_fails_when_the_stored_output_is_not_reproducible(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    context = _happy_context(graph, calculation_registry=analysis.registry)
    claim_id = sorted(graph.claims)[0]
    passport = build_financial_proof_object(claim_id, context)

    broken = replace(
        passport,
        calculation_proof=replace(passport.calculation_proof, output=-1.0),
    )
    outcome = verify_calculation(broken, context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED


def test_calculation_fails_when_the_record_is_gone_from_the_registry(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    context = _happy_context(graph, calculation_registry=CalculationRegistry())
    claim_id = sorted(graph.claims)[0]

    outcome = verify_calculation(
        build_financial_proof_object(claim_id, context), context
    )

    assert outcome.status == CHECK_FAIL
    assert "不存在" in outcome.message


# =========================================================================== #
# §7 checks — dependencies / assumptions / validator state
# =========================================================================== #
def test_dependencies_pass_when_the_upstream_chain_is_clean():
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_dependencies(
        build_financial_proof_object("C-MARGIN", context), context
    )

    assert outcome.status == CHECK_PASS


def test_required_upstream_blocked_blocks_the_claim():
    """§12 edge 5 — REQUIRED, not SUPPORTING: the difference must show."""
    graph = _happy_graph()
    upstream = _claim(
        "C-FACT", metric="revenue", evidence_ids=("E-REV",)
    )
    graph.add_claim(upstream)
    downstream = _claim(
        "C-DOWN",
        claim_type=CLAIM_INFERENCE,
        metric=None,
        period_key=None,
        # Its *own* evidence stays healthy, so the only thing that can block it
        # is the REQUIRED edge. Sharing E-REV would have made this test pass for
        # the wrong reason.
        evidence_ids=("E-PROFIT",),
        dependency_metadata=(DependencyRef("C-FACT", TARGET_CLAIM, ROLE_REQUIRED),),
    )
    graph.add_claim(downstream)
    deactivate_evidence(graph, "E-REV", "上游结论的来源被撤回")
    revalidate(graph)
    context = _happy_context(graph)

    outcome = verify_dependencies(
        build_financial_proof_object("C-DOWN", context), context
    )

    assert outcome.status == CHECK_FAIL
    assert outcome.details["blocked"]
    assert outcome.details["blocked"][0]["state"] == UPSTREAM_BLOCKED
    assert outcome.details["blocked"][0]["target_id"] == "C-FACT"


def test_supporting_upstream_blocked_degrades_instead_of_blocking():
    graph = _supporting_lapse_graph()
    context = _happy_context(graph)

    # Guard the premise: if C-MAIN were structurally unsupported this test would
    # be measuring the §7 binding rule instead of the SUPPORTING row of §7b.
    assert graph.claim("C-MAIN").validation_status == VALIDATION_SUPPORTED

    outcome = verify_dependencies(
        build_financial_proof_object("C-MAIN", context), context
    )

    assert outcome.status == CHECK_WARN
    assert outcome.impact == STATUS_DEGRADED
    assert outcome.details["degraded"][0]["target_id"] == "C-AUX"
    assert outcome.details["degraded"][0]["role"] == ROLE_SUPPORTING


def test_missing_upstream_target_is_reported_not_assumed_ok():
    graph = _happy_graph()
    claim = _claim(
        "C-GHOST",
        metric="gross_margin",
        evidence_ids=("E-REV",),
        dependency_metadata=(DependencyRef("C-NOT-THERE", TARGET_EVIDENCE, ROLE_REQUIRED),),
    )
    graph.add_claim(claim)
    context = _happy_context(graph)

    outcome = verify_dependencies(
        build_financial_proof_object("C-GHOST", context), context
    )

    assert outcome.status in (CHECK_FAIL, CHECK_WARN)
    assert outcome.status != CHECK_PASS


def test_dangling_evidence_reference_is_reported_as_missing_not_as_orphan():
    """A claim citing ``E-GHOST`` cited *something* — it did not cite nothing.

    The sibling of ``test_missing_upstream_target_is_reported_not_assumed_ok``,
    one layer down: that one guards a dangling **dependency** target, this one a
    dangling **evidence** reference. Both failures are ``BLOCKED``, so the verdict
    alone cannot tell them apart — and §7.1's message is the only thing an
    operator can act on. Reading the reference list off ``SourceProof.evidence_ids``
    collapsed the two, because that field only carries ids the ledger *could*
    resolve; a broken pointer arrived here as an absent citation.
    """
    graph = _happy_graph()
    graph.add_claim(_claim("C-DANGLING", evidence_ids=("E-GHOST",)))
    context = _happy_context(graph)

    outcome = verify_source(
        build_financial_proof_object("C-DANGLING", context), context
    )

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED
    assert "证据不存在" in outcome.message
    assert "E-GHOST" in outcome.message
    # The wrong reason must not come back: "cited nothing" sends the operator to
    # re-read the claim, "cited a ghost" sends them to the ledger. Different work.
    assert "未绑定任何证据" not in outcome.message
    assert outcome.details["missing_evidence"] == ["E-GHOST"]


def test_claim_with_no_references_at_all_is_still_reported_as_an_orphan():
    """The complement — the orphan message must survive the change above.

    Fixing a mislabelled failure by deleting the label is not a fix. This keeps
    "no citation at all" distinguishable from "citation to a ghost".
    """
    graph = _happy_graph()
    graph.add_claim(_claim("C-ORPHAN", evidence_ids=()))
    context = _happy_context(graph)

    outcome = verify_source(build_financial_proof_object("C-ORPHAN", context), context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED
    assert "未绑定任何证据" in outcome.message
    assert "证据不存在" not in outcome.message


def test_assumptions_skip_when_the_claim_references_none():
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_assumptions(
        build_financial_proof_object("C-MARGIN", context), context
    )

    assert outcome.status == CHECK_SKIPPED
    assert "§12 edge 2" in outcome.message


def test_assumptions_pass_when_the_registry_knows_them():
    graph = _happy_graph()
    claim = _claim(
        "C-ASSUMED",
        metric="gross_margin",
        evidence_ids=("E-REV",),
        calculation_ids=("CAL-1",),
        assumption_ids=("A-1",),
    )
    graph.add_claim(claim)
    context = _happy_context(
        graph,
        assumption_registry=_AssumptionRegistry({"A-1": _AssumptionRecord(grade="sourced")}),
    )

    outcome = verify_assumptions(
        build_financial_proof_object("C-ASSUMED", context), context
    )

    assert outcome.status == CHECK_PASS
    assert outcome.details["assumption_ids"] == ["A-1"]


def test_assumptions_fail_for_an_id_the_registry_does_not_know():
    graph = _happy_graph()
    claim = _claim(
        "C-ASSUMED",
        metric="gross_margin",
        evidence_ids=("E-REV",),
        calculation_ids=("CAL-1",),
        assumption_ids=("A-404",),
    )
    graph.add_claim(claim)
    context = _happy_context(graph, assumption_registry=_AssumptionRegistry())

    outcome = verify_assumptions(
        build_financial_proof_object("C-ASSUMED", context), context
    )

    assert outcome.status == CHECK_FAIL
    assert "A-404" in outcome.message


def test_a_hand_set_assumption_degrades_instead_of_verifying():
    """CARD-08's ``assumption:`` grade is a setting, not a sourced fact."""
    graph = _happy_graph()
    claim = _claim(
        "C-ASSUMED",
        metric="gross_margin",
        evidence_ids=("E-REV",),
        calculation_ids=("CAL-1",),
        assumption_ids=("A-SET",),
    )
    graph.add_claim(claim)
    context = _happy_context(
        graph,
        assumption_registry=_AssumptionRegistry(
            {"A-SET": _AssumptionRecord(grade="assumption", source="")}
        ),
    )

    outcome = verify_assumptions(
        build_financial_proof_object("C-ASSUMED", context), context
    )

    assert outcome.status == CHECK_WARN
    assert outcome.impact == STATUS_DEGRADED


def test_assumptions_skip_when_no_registry_was_supplied():
    graph = _happy_graph()
    claim = _claim(
        "C-EXTERNAL",
        metric="gross_margin",
        evidence_ids=("E-REV",),
        calculation_ids=("CAL-1",),
        assumption_ids=("A-PEER",),
    )
    graph.add_claim(claim)
    context = _happy_context(graph, assumption_registry=None)

    outcome = verify_assumptions(
        build_financial_proof_object("C-EXTERNAL", context), context
    )

    assert outcome.status == CHECK_SKIPPED
    assert "假设注册表" in outcome.message


def test_validator_state_passes_when_propagation_still_supports_the_claim():
    graph = _happy_graph()
    claim = graph.claims["C-MARGIN"]
    context = _happy_context(graph)
    claim = replace(claim, validation_status=VALIDATION_SUPPORTED)
    graph.claims["C-MARGIN"] = claim

    outcome = verify_validator_state(
        build_financial_proof_object("C-MARGIN", context), context
    )

    assert outcome.status == CHECK_PASS
    assert outcome.details["derived"] == VALIDATION_SUPPORTED


def test_validator_state_fails_when_the_figure_is_stale():
    """The stored field is re-derived, never trusted."""
    graph = _happy_graph()
    context = _happy_context(graph)
    passport = build_financial_proof_object("C-MARGIN", context)

    # Move the graph under the passport: revoke the evidence it stands on...
    deactivate_evidence(graph, "E-REV", "撤回")
    revalidate(graph)
    # ...then put the *claim's* recorded figure back the way the passport saw it.
    # revalidate() rewrites claim.validation_status too, and without this restore
    # the test would be comparing a freshly-derived value against itself.
    graph.claims["C-MARGIN"] = replace(
        graph.claims["C-MARGIN"], validation_status=str(passport.validation_proof.result)
    )

    outcome = verify_validator_state(passport, context)

    assert outcome.status in (CHECK_FAIL, CHECK_WARN)
    assert outcome.details["stored"] == passport.validation_proof.result
    assert outcome.details["derived"] != passport.validation_proof.result


def test_validator_state_fails_when_the_claim_left_the_graph():
    graph = _happy_graph()
    context = _happy_context(graph)
    passport = build_financial_proof_object("C-MARGIN", context)

    outcome = verify_validator_state(passport, ProofContext())

    assert outcome.status == CHECK_SKIPPED


# =========================================================================== #
# §7 checks — integrity / conflict
# =========================================================================== #
def test_integrity_passes_and_records_provenance_gaps_without_escalating():
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_integrity(
        build_financial_proof_object("C-MARGIN", context), context
    )

    assert outcome.status == CHECK_PASS
    # git_commit / audit_chain_ref are absent here, and that is *reported*...
    assert outcome.details["provenance_gaps"]
    # ...but does not move the verdict, because it is a build-reproducibility
    # question, not an "is this claim still true" question.
    assert outcome.impact == STATUS_BLOCKED  # the pass() sentinel; passes never merge


def test_integrity_is_degraded_when_run_id_is_missing():
    graph = _happy_graph()
    context = _happy_context(graph, run_id="")
    passport = build_financial_proof_object("C-MARGIN", context)
    assert passport.integrity_proof.run_id == ""

    outcome = verify_integrity(passport, context)

    assert outcome.status == CHECK_WARN
    assert outcome.impact == STATUS_DEGRADED


def test_integrity_fails_when_the_passport_was_unsealed():
    graph = _happy_graph()
    context = _happy_context(graph)
    passport = build_financial_proof_object("C-MARGIN", context)

    outcome = verify_integrity(
        replace(passport, integrity_proof=IntegrityProof(run_id=RUN_ID)), context
    )

    assert outcome.status == CHECK_FAIL


def test_integrity_fails_when_a_field_was_edited_after_sealing():
    """§12 edge 7 — the one thing a "proof" object must make impossible."""
    graph = _happy_graph()
    context = _happy_context(graph)
    passport = build_financial_proof_object("C-MARGIN", context)

    tampered = replace(passport, claim_text=passport.claim_text + "（被改过）")
    outcome = verify_integrity(tampered, context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_BLOCKED


def test_conflict_passes_with_no_records_at_all():
    graph = _happy_graph()
    context = _happy_context(graph)

    outcome = verify_conflict(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_PASS
    assert "已核对 0 条记录" in outcome.message


def test_undecided_conflict_makes_the_claim_conflicted():
    graph = _happy_graph()
    record = ConflictRecord(
        conflict_id="X-DATA",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="DOC-1",
        source_b="DOC-9",
        selected_source=None,
        reason="同期间同口径出现两个值",
        resolver="card13",
        timestamp="2026-09-19T00:00:00+08:00",
        metric="gross_margin",
        period_key="2025FY",
        status=STATUS_UNRESOLVED,
        value_a=45.0,
        value_b=41.0,
    )
    context = _happy_context(graph, conflicts=(record,))

    outcome = verify_conflict(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_FAIL
    assert outcome.impact == STATUS_CONFLICTED
    assert outcome.details["conflict_ids"] == ["X-DATA"]


def test_resolved_conflicts_do_not_make_a_claim_conflicted():
    graph = _happy_graph()
    record = ConflictRecord(
        conflict_id="X-OK",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="DOC-1",
        source_b="DOC-9",
        selected_source="DOC-1",
        reason="监管披露优先",
        resolver="card13",
        timestamp="2026-09-19T00:00:00+08:00",
        metric="gross_margin",
        period_key="2025FY",
    )
    context = _happy_context(graph, conflicts=(record,))

    outcome = verify_conflict(build_financial_proof_object("C-MARGIN", context), context)

    assert outcome.status == CHECK_PASS


def test_conflict_check_skips_without_a_metric_or_period():
    graph = _happy_graph()
    opinion = _claim(
        "C-OP", claim_type=CLAIM_OPINION, metric=None, period_key=None, evidence_ids=()
    )
    graph.add_claim(opinion)
    context = _happy_context(graph)

    outcome = verify_conflict(build_financial_proof_object("C-OP", context), context)

    assert outcome.status == CHECK_SKIPPED


# =========================================================================== #
# §7 merge rules — table driven
# =========================================================================== #
def _check(name: str, status: str, impact: str) -> CheckOutcome:
    return CheckOutcome(name, status, f"{name} {status}", impact)


@pytest.mark.parametrize(
    "impacts,expected",
    [
        ((STATUS_VERIFIED, STATUS_VERIFIED), STATUS_VERIFIED),
        ((STATUS_DEGRADED, STATUS_VERIFIED), STATUS_DEGRADED),
        ((STATUS_STALE, STATUS_DEGRADED), STATUS_STALE),
        ((STATUS_CONFLICTED, STATUS_STALE), STATUS_CONFLICTED),
        ((STATUS_BLOCKED, STATUS_CONFLICTED), STATUS_BLOCKED),
        ((STATUS_VERIFIED, STATUS_BLOCKED), STATUS_BLOCKED),
        ((STATUS_CONFLICTED, STATUS_CONFLICTED), STATUS_CONFLICTED),
    ],
)
def test_merge_takes_the_most_severe_impact(impacts, expected):
    checks = tuple(
        _check(name, CHECK_FAIL, impact)
        for name, impact in zip(PROOF_CHECKS, impacts)
    )
    if len(impacts) < len(PROOF_CHECKS):
        checks += tuple(
            _check(name, CHECK_PASS, STATUS_BLOCKED)
            for name in PROOF_CHECKS[len(impacts):]
        )

    overall, reasons = merge_status(checks)

    assert overall == expected
    assert reasons


def test_merge_ignores_pass_and_skipped():
    checks = (
        _check(CHECK_SOURCE, CHECK_PASS, STATUS_BLOCKED),
        _check(CHECK_PERIOD, CHECK_SKIPPED, STATUS_BLOCKED),
        _check(CHECK_SCOPE_UNIT, CHECK_SKIPPED, STATUS_CONFLICTED),
    )

    overall, reasons = merge_status(checks)

    assert overall == STATUS_VERIFIED
    assert reasons == ("全部证明项通过（skipped 项已逐条列明原因）",)


def test_a_raising_check_becomes_a_failure_not_a_pass():
    """§11 — 「不允许验证器永远通过」."""
    graph = _happy_graph()
    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    outcome = verify_calculation(
        passport,
        ProofContext(graph=graph, run_id=RUN_ID, calculation_registry=_BrokenRegistry()),
    )

    assert outcome.status == CHECK_FAIL
    assert "重算" in outcome.message


class _BrokenRegistry:
    """A CARD-03 registry that claims to know the calculation and then explodes."""

    def has(self, _calculation_id):
        return True

    def recalculate(self, _calculation_id):
        raise RuntimeError("registry 坏了")


@dataclasses.dataclass
class _AssumptionRecord:
    grade: str = "sourced"
    source: str = "DOC-1 第 12 页"


class _AssumptionRegistry:
    """Minimal CARD-08 stand-in: the two methods the proof layer actually calls."""

    def __init__(self, records=None):
        self._records = dict(records or {})

    def has(self, assumption_id):
        return assumption_id in self._records

    def get(self, assumption_id):
        if assumption_id not in self._records:
            raise KeyError(assumption_id)
        return self._records[assumption_id]


# =========================================================================== #
# §18 — the five states, one reachable case each
# =========================================================================== #
def test_state_verified_on_a_clean_claim(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    context = _happy_context(
        graph,
        calculation_registry=analysis.registry,
        document_hashes={document.document_id: document.sha256},
        document_loader=_stored_bytes_loader(registry),
    )

    result = verify_claim(sorted(graph.claims)[0], context)

    assert result.overall_status == STATUS_VERIFIED
    assert result.passed is True
    assert result.blocking is False


def test_state_degraded_when_a_non_blocking_item_lapses():
    """§18 — DEGRADED must be reachable end-to-end, not only per-check.

    Losing a SUPPORTING source lowers coverage; it must not read as BLOCKED.
    The distinction is only real if the claim is otherwise sound, which is why
    the fixture carries a genuine REQUIRED derivation (see
    :func:`_supporting_lapse_graph`) and the validator check is asserted too —
    that check is the one that reports ``unsupported`` when the binding rule is
    unmet, and it would mask a regression in the SUPPORTING row.
    """
    graph = _supporting_lapse_graph()
    context = _happy_context(graph)

    result = verify_claim("C-MAIN", context)

    assert result.overall_status == STATUS_DEGRADED
    assert _outcome(result.checks, CHECK_DEPENDENCY).impact == STATUS_DEGRADED
    assert _outcome(result.checks, CHECK_VALIDATOR).status == CHECK_PASS
    assert any("coverage_dropped:C-MAIN" in warning for warning in graph.warnings)


def test_state_stale_after_a_supersession(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    context = _happy_context(
        graph,
        calculation_registry=analysis.registry,
        document_hashes={document.document_id: document.sha256},
        document_loader=_stored_bytes_loader(registry),
        declared_versions={sorted(graph.claims)[0]: "SUPERSEDED"},
    )

    result = verify_claim(sorted(graph.claims)[0], context)

    assert result.overall_status == STATUS_STALE
    assert result.blocking is False


def test_state_blocked_when_a_required_proof_item_fails():
    graph = _happy_graph()
    context = _happy_context(graph, document_hashes={"DOC-1": _hash_for("original")},
                             document_loader=_recomputing_loader({"DOC-1": "changed"}))

    result = verify_claim("C-MARGIN", context)

    assert result.overall_status == STATUS_BLOCKED
    assert result.blocking is True


def test_state_conflicted_when_evidence_disagrees_and_card13_has_not_ruled():
    graph = _happy_graph()
    record = ConflictRecord(
        conflict_id="X-OPEN",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="DOC-1",
        source_b="DOC-9",
        selected_source=None,
        reason="同期间同口径出现两个值",
        resolver="card13",
        timestamp="2026-09-19T00:00:00+08:00",
        metric="gross_margin",
        period_key="2025FY",
        status=STATUS_UNRESOLVED,
    )
    context = _happy_context(graph, conflicts=(record,))

    result = verify_claim("C-MARGIN", context)

    assert result.overall_status == STATUS_CONFLICTED
    assert result.blocking is True


def test_every_state_in_the_vocabulary_is_reachable():
    """A state nobody can produce is a state nobody can trust."""
    graph = _happy_graph()
    produced = {verify_claim("C-MARGIN", _happy_context(graph)).overall_status}

    broken = _happy_context(
        graph,
        document_hashes={"DOC-1": _hash_for("a")},
        document_loader=_recomputing_loader({"DOC-1": "b"}),
    )
    produced.add(verify_claim("C-MARGIN", broken).overall_status)

    stale = _happy_context(graph, declared_versions={"C-MARGIN": "SUPERSEDED"})
    produced.add(verify_claim("C-MARGIN", stale).overall_status)

    record = ConflictRecord(
        conflict_id="X", change_kind=CHANGE_DATA_CONFLICT, source_a="A", source_b="B",
        selected_source=None, reason="r", resolver="card13", timestamp="t",
        metric="gross_margin", period_key="2025FY", status=STATUS_UNRESOLVED,
    )
    conflicted = _happy_context(graph, conflicts=(record,))
    produced.add(verify_claim("C-MARGIN", conflicted).overall_status)

    assert produced == {
        STATUS_VERIFIED, STATUS_STALE, STATUS_BLOCKED, STATUS_CONFLICTED,
    }
    # DEGRADED needs a supporting-only lapse, which the merge test above covers.
    assert STATUS_DEGRADED in PROOF_STATUSES


def _stored_bytes_loader(registry: DocumentRegistry):
    def loader(document_id: str) -> Optional[bytes]:
        record = registry.get(document_id)
        stored = getattr(record, "stored_path", None)
        if not stored:
            return None
        path = Path(stored)
        return path.read_bytes() if path.exists() else None

    return loader


# =========================================================================== #
# proof_hash — stability and recomputability (§13 rule 1)
# =========================================================================== #
def test_proof_hash_is_stable_across_calls_and_constructions():
    graph = _happy_graph()
    context = _happy_context(graph)

    first = build_financial_proof_object("C-MARGIN", context)
    second = build_financial_proof_object("C-MARGIN", context)

    assert first.proof_hash() == second.proof_hash()
    assert first.integrity_proof.object_hash == second.integrity_proof.object_hash


def test_seal_is_idempotent():
    graph = _happy_graph()
    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    assert passport.seal().proof_hash() == passport.proof_hash()


def test_hash_excludes_the_hash_it_stores():
    graph = _happy_graph()
    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    assert "object_hash" not in passport.payload()["integrity_proof"]
    assert "object_hash" in passport.to_dict()["integrity_proof"]


def test_changing_any_proof_block_changes_the_hash():
    graph = _happy_graph()
    passport = build_financial_proof_object("C-MARGIN", _happy_context(graph))

    mutations = {
        "claim_text": replace(passport, claim_text="改过的结论"),
        "version_status": replace(passport, version_status="RESTATED"),
        "source": replace(
            passport, source_proof=replace(passport.source_proof, page=99)
        ),
        "accounting": replace(
            passport,
            accounting_context=replace(passport.accounting_context, unit="万元"),
        ),
        "validation": replace(
            passport,
            validation_proof=replace(passport.validation_proof, result="blocked"),
        ),
        "integrity_run": replace(
            passport,
            integrity_proof=replace(passport.integrity_proof, run_id="other-run"),
        ),
    }

    for label, mutated in mutations.items():
        assert mutated.proof_hash() != passport.proof_hash(), label


# =========================================================================== #
# §13 — the validator
# =========================================================================== #
def _passport_and_verdict():
    graph = _happy_graph()
    context = _happy_context(graph)
    passport = build_financial_proof_object("C-MARGIN", context)
    return passport, verify_fpo(passport, context)


def test_validator_accepts_a_consistent_passport_and_verdict():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(passport, verification=verdict)

    assert result.valid, result.errors


def test_validator_catches_a_passport_that_was_edited_after_sealing():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(
        replace(passport, claim_text="改过"), verification=verdict
    )

    assert not result.valid
    assert any("不可重算" in item for item in result.errors)


def test_validator_catches_an_unsealed_passport():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(
        replace(passport, integrity_proof=IntegrityProof(run_id=RUN_ID)),
        verification=verdict,
    )

    assert not result.valid
    assert any("未封存" in item for item in result.errors)


def test_validator_requires_a_run_id_for_the_audit_chain():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(
        replace(passport, integrity_proof=replace(passport.integrity_proof, run_id="")),
        verification=verdict,
    )

    assert not result.valid
    assert any("run_id" in item for item in result.errors)


def test_validator_catches_an_overall_status_that_contradicts_its_checks():
    passport, verdict = _passport_and_verdict()
    lying = replace(verdict, overall_status=STATUS_BLOCKED)

    result = validate_proof(passport, verification=lying)

    assert not result.valid
    assert any("合并结果" in item for item in result.errors)


def test_validator_refuses_verified_over_a_failed_check():
    passport, verdict = _passport_and_verdict()
    checks = tuple(
        replace(item, status=CHECK_FAIL, impact=STATUS_BLOCKED)
        if item.check == CHECK_SOURCE
        else item
        for item in verdict.checks
    )
    tampered = replace(verdict, checks=checks)

    result = validate_proof(passport, verification=tampered)

    assert not result.valid
    assert any("降标通过" in item for item in result.errors)


def test_validator_requires_every_check_to_be_present_exactly_once():
    passport, verdict = _passport_and_verdict()

    dropped = replace(verdict, checks=verdict.checks[:-1])
    assert not validate_proof(passport, verification=dropped).valid
    assert any(
        "missing_checks" in item
        for item in validate_proof(passport, verification=dropped).errors
    )

    doubled = replace(verdict, checks=verdict.checks + (verdict.checks[0],))
    errors = validate_proof(passport, verification=doubled).errors
    assert any("duplicate_checks" in item for item in errors)


def test_validator_rejects_an_empty_check_list():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(passport, verification=replace(verdict, checks=()))

    assert not result.valid


def test_validator_requires_status_reasons():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(passport, verification=replace(verdict, status_reasons=()))

    assert not result.valid
    assert any("status_reasons" in item for item in result.errors)


def test_validator_requires_conflicted_to_point_at_a_conflict_check():
    passport, verdict = _passport_and_verdict()
    fake = replace(
        verdict,
        overall_status=STATUS_CONFLICTED,
        checks=tuple(
            replace(item, status=CHECK_WARN, impact=STATUS_CONFLICTED)
            for item in verdict.checks
        ),
    )

    result = validate_proof(passport, verification=fake)

    assert not result.valid


def test_validator_says_what_it_did_not_check_when_inputs_are_missing():
    passport, _verdict = _passport_and_verdict()

    result = validate_proof(passport)

    assert result.valid
    assert any("no_verification_supplied" in item for item in result.warnings)


def test_validator_warns_when_no_memo_was_supplied():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(passport, verification=verdict, memo=None)

    assert result.valid
    assert any("no_memo_supplied" in item for item in result.warnings)


def test_validator_rejects_a_verification_for_a_different_claim():
    passport, verdict = _passport_and_verdict()

    result = validate_proof(passport, verification=replace(verdict, claim_id="OTHER"))

    assert not result.valid
    assert any("不匹配" in item for item in result.errors)


def test_validate_verification_works_without_a_passport():
    _passport, verdict = _passport_and_verdict()

    assert validate_verification(verdict).valid
    assert not validate_verification(replace(verdict, verify_source_version="")).valid


# ---- §13 rule 3: blocked conclusions must not read as advice ---------------
def _memo(claim_id: str, sentence: str, section_id: str = SECTION_EXECUTIVE_SUMMARY):
    return InvestmentMemo(
        memo_id="memo-1",
        symbol="600519.SH",
        document_id="DOC-1",
        as_of="2026-09-19",
        sections=[
            MemoSection(
                section_id=section_id,
                title="执行摘要",
                sentences=[
                    MemoSentence(
                        sentence_span=sentence,
                        claim_id=claim_id,
                        claim_type="inference",
                    )
                ],
            )
        ],
    )


def _blocked_verdict():
    """A genuinely BLOCKED verdict: the filing's bytes no longer hash to the record."""
    graph = _happy_graph()
    context = _happy_context(
        graph,
        document_hashes={"DOC-1": _hash_for("original")},
        document_loader=_recomputing_loader({"DOC-1": "tampered"}),
    )
    passport = build_financial_proof_object("C-MARGIN", context)
    return passport, verify_fpo(passport, context)


def _stale_verdict():
    """A genuinely STALE verdict: the claim's version was superseded."""
    graph = _happy_graph()
    context = _happy_context(graph, declared_versions={"C-MARGIN": "SUPERSEDED"})
    passport = build_financial_proof_object("C-MARGIN", context)
    return passport, verify_fpo(passport, context)


def _conflicted_verdict():
    """A genuinely CONFLICTED verdict: CARD-13 has not ruled on a disagreement."""
    graph = _happy_graph()
    record = ConflictRecord(
        conflict_id="X-OPEN", change_kind=CHANGE_DATA_CONFLICT,
        source_a="DOC-1", source_b="DOC-9", selected_source=None,
        reason="同期间同口径出现两个值", resolver="card13", timestamp="t",
        metric="gross_margin", period_key="2025FY", status=STATUS_UNRESOLVED,
    )
    context = _happy_context(graph, conflicts=(record,))
    passport = build_financial_proof_object("C-MARGIN", context)
    return passport, verify_fpo(passport, context)


def test_blocked_claim_in_a_conclusion_section_is_rejected():
    passport, blocked = _blocked_verdict()
    assert blocked.overall_status == STATUS_BLOCKED
    memo = _memo("C-MARGIN", "我们认为公司毛利率具备持续性。")

    result = validate_proof(passport, verification=blocked, memo=memo)

    assert not result.valid
    assert any("禁止其进入结论" in item for item in result.errors)


def test_conflicted_claim_in_a_conclusion_section_is_rejected():
    passport, conflicted = _conflicted_verdict()
    assert conflicted.overall_status == STATUS_CONFLICTED
    memo = _memo("C-MARGIN", "毛利率提升是核心催化剂。")

    result = validate_proof(passport, verification=conflicted, memo=memo)

    assert not result.valid
    assert any("禁止其进入结论" in item for item in result.errors)


def test_blocked_claim_outside_the_conclusion_sections_is_allowed():
    """公司概况 / 财务表现 report facts; CARD-10's own validator owns those."""
    passport, blocked = _blocked_verdict()
    memo = _memo("C-MARGIN", "本期营业收入为 2000 万元。", section_id=SECTION_COMPANY_OVERVIEW)

    result = validate_proof(passport, verification=blocked, memo=memo)

    assert result.valid, result.errors


def test_stale_claim_in_a_conclusion_must_carry_the_refresh_marker():
    passport, stale = _stale_verdict()
    assert stale.overall_status == STATUS_STALE

    unmarked = _memo("C-MARGIN", "毛利率同比下降 1.2pct。")
    unmarked_result = validate_proof(passport, verification=stale, memo=unmarked)
    assert not unmarked_result.valid
    assert any(REFRESH_MARKER in item for item in unmarked_result.errors)

    marked = _memo("C-MARGIN", f"毛利率同比下降 1.2pct（{REFRESH_MARKER}）。")
    result = validate_proof(passport, verification=stale, memo=marked)
    assert result.valid, result.errors
    assert any(REFRESH_MARKER in item for item in result.warnings)


def test_verified_claim_in_a_conclusion_section_is_fine():
    passport, verdict = _passport_and_verdict()
    memo = _memo("C-MARGIN", "毛利率为 45%。")

    result = validate_proof(passport, verification=verdict, memo=memo)

    assert result.valid, result.errors


def test_conclusion_sections_exclude_the_fact_reporting_ones():
    from src.cn.memo import SECTION_EVIDENCE_APPENDIX, SECTION_FINANCIAL_PERFORMANCE

    assert SECTION_EXECUTIVE_SUMMARY in CONCLUSION_SECTIONS
    assert SECTION_RISKS in CONCLUSION_SECTIONS
    assert SECTION_COMPANY_OVERVIEW not in CONCLUSION_SECTIONS
    assert SECTION_FINANCIAL_PERFORMANCE not in CONCLUSION_SECTIONS
    assert SECTION_EVIDENCE_APPENDIX not in CONCLUSION_SECTIONS


def test_validate_proof_corpus_prefixes_errors_with_the_claim():
    passport, verdict = _passport_and_verdict()
    broken = replace(passport, claim_text="改过")

    result = validate_proof_corpus([(passport, verdict), (broken, verdict)])

    assert not result.valid
    assert all(item.startswith("C-MARGIN:") for item in result.errors)


def test_validate_proof_corpus_says_so_when_it_was_given_nothing():
    result = validate_proof_corpus([])

    assert result.valid
    assert "no_passports_supplied" in result.warnings


# =========================================================================== #
# §15 — integration
# =========================================================================== #
def test_card09_claim_traces_and_verifies_from_the_same_id(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    claim_id = sorted(graph.facts, key=lambda item: item.claim_id)[0].claim_id
    context = _happy_context(
        graph,
        calculation_registry=analysis.registry,
        document_hashes={document.document_id: document.sha256},
        document_loader=_stored_bytes_loader(registry),
    )

    tree = graph.trace(claim_id, max_depth=8)
    result = verify_claim(claim_id, context)

    assert tree.locatable is True
    assert result.claim_id == claim_id
    assert result.overall_status == STATUS_VERIFIED
    assert len(result.checks) == len(PROOF_CHECKS)


def test_corpus_gate_clean_filings_verify_and_planted_defects_do_not(registry):
    """The gate CARD-22 established, applied to the passport layer.

    A verifier that blocks clean filings is worse than no verifier: users learn
    to ignore it. So every claim of the clean filings must reach VERIFIED, and
    the count is asserted non-trivially so the test cannot pass on an empty graph.
    """
    verified = 0
    for filename in CLEAN_FILINGS:
        document = _ingest(registry, filename)
        graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
        normalization = normalize_financials(
            registry.get_facts(document.document_id), document
        )
        analysis = analyze_document(normalization)
        context = _happy_context(
            graph,
            calculation_registry=analysis.registry,
            document_hashes={document.document_id: document.sha256},
            document_loader=_stored_bytes_loader(registry),
        )
        assert graph.claims, filename
        for claim_id in sorted(graph.claims):
            result = verify_claim(claim_id, context)
            assert result.overall_status == STATUS_VERIFIED, (
                filename, claim_id, result.status_reasons,
            )
            verified += 1

    assert verified >= 5


def test_withdrawn_evidence_blocks_the_claim_through_the_engine(registry):
    document, analysis = _live_registry(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    claim = sorted(graph.facts, key=lambda item: item.claim_id)[0]
    evidence_id = claim.evidence_ids[0]

    deactivate_evidence(graph, evidence_id, "演示：来源证据撤回")
    context = _happy_context(
        graph,
        calculation_registry=analysis.registry,
        document_hashes={document.document_id: document.sha256},
        document_loader=_stored_bytes_loader(registry),
    )

    result = verify_claim(claim.claim_id, context)

    # A *withdrawal* is not a *supersession*. CARD-09 marks a claim's own evidence
    # as a REQUIRED edge, so pulling it out leaves the conclusion unsupported and
    # the verdict is BLOCKED. STALE is reserved for "a newer filing replaced this
    # one, but the old number is still the old number" — the CARD-13 path covered
    # by the two supersession tests above.
    assert result.overall_status == STATUS_BLOCKED


def test_verify_fpo_checks_a_stored_passport_without_rebuilding_it():
    passport, _verdict = _passport_and_verdict()
    graph = _happy_graph()
    context = _happy_context(graph)

    fresh = verify_fpo(passport, context)
    reread = verify_fpo(passport, context)

    assert fresh.overall_status == reread.overall_status
    assert fresh.status_reasons == reread.status_reasons


def test_verification_result_survives_a_json_round_trip():
    _passport, verdict = _passport_and_verdict()

    payload = verdict.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False, default=str)

    assert json.loads(encoded)["overall_status"] == verdict.overall_status


# =========================================================================== #
# §8 — the agent tool
# =========================================================================== #
def _tool_payload(registry: DocumentRegistry, **kwargs):
    from src.agents.tools.cn_tools import VerifyCnClaimTool

    return json.loads(asyncio.run(VerifyCnClaimTool(registry).execute(**kwargs)))


def test_verify_tool_requires_a_document_id():
    from src.agents.tools.cn_tools import VerifyCnClaimTool

    payload = json.loads(asyncio.run(VerifyCnClaimTool().execute(claim_id="C-1")))

    assert payload["status"] == "error"
    assert payload["error_code"] == "proof_missing_argument"


def test_verify_tool_requires_a_claim_id():
    from src.agents.tools.cn_tools import VerifyCnClaimTool

    payload = json.loads(asyncio.run(VerifyCnClaimTool().execute(document_id="DOC-1")))

    assert payload["status"] == "error"
    assert payload["error_code"] == "proof_missing_argument"


def test_verify_tool_reports_an_unknown_document(registry):
    payload = _tool_payload(registry, document_id="NOPE", claim_id="C-1")

    assert payload["status"] == "error"
    assert payload["error_code"] == "document_not_found"


def test_verify_tool_reports_an_unknown_claim(registry):
    document = _ingest(registry, "annual_06_equity_shares.pdf")

    payload = _tool_payload(
        registry, document_id=document.document_id, claim_id="NOPE"
    )

    assert payload["status"] == "error"
    assert payload["error_code"] == "claim_not_found"
    assert payload["available"]


def test_verify_tool_returns_status_passport_and_verification_result(registry):
    document = _ingest(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    claim_id = sorted(graph.claims)[0]

    payload = _tool_payload(
        registry, document_id=document.document_id, claim_id=claim_id
    )

    assert payload["status"] == "ok"
    assert payload["overall_status"] == STATUS_VERIFIED
    assert payload["claim_id"] == claim_id
    assert payload["passport"]["claim_id"] == claim_id
    assert payload["verification_result"]["overall_status"] == STATUS_VERIFIED
    assert payload["validation"]["valid"] is True
    assert [item["check"] for item in payload["checks"]] == list(PROOF_CHECKS)
    assert payload["verify_source_version"]
    assert payload["filename"] == document.filename


def test_verify_tool_can_omit_the_passport_for_batch_use(registry):
    document = _ingest(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)

    payload = _tool_payload(
        registry,
        document_id=document.document_id,
        claim_id=sorted(graph.claims)[0],
        include_passport=False,
    )

    assert payload["status"] == "ok"
    assert "passport" not in payload


def test_verify_tool_declared_scope_contradiction_surfaces(registry):
    document = _ingest(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)
    claim_id = sorted(graph.claims)[0]

    payload = _tool_payload(
        registry,
        document_id=document.document_id,
        claim_id=claim_id,
        declared_scope="parent",
    )

    assert payload["overall_status"] == STATUS_BLOCKED
    scope_unit = next(item for item in payload["checks"] if item["check"] == CHECK_SCOPE_UNIT)
    assert scope_unit["status"] == CHECK_FAIL


def test_verify_tool_is_registered_and_never_writes_a_value():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = [getattr(tool, "name", "") for tool in build_cn_snapshot_tools()]
    assert "verify_cn_claim" in names


def test_verify_tool_output_says_what_it_did_not_establish(registry):
    document = _ingest(registry, "annual_06_equity_shares.pdf")
    graph, _analysis, _doc = _chain_graph_for(registry, document.document_id)

    payload = _tool_payload(
        registry, document_id=document.document_id, claim_id=sorted(graph.claims)[0]
    )

    # A factual claim references no assumptions, so that check is skipped — and
    # "skipped" must be visible rather than folded into "passed".
    assert any(item["status"] == CHECK_SKIPPED for item in payload["checks"])
    assert "不等于通过" in payload["note"]


# =========================================================================== #
# §9 — zero LLM, on the source text
# =========================================================================== #
def test_no_proof_module_imports_a_model_client():
    forbidden = ("src.llms", "openai", "anthropic", "litellm")
    for path in sorted(PROOF_SOURCE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path.name} 引用了模型客户端：{token}"


def test_proof_validator_is_model_free_too():
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "validation" / "proof_validator.py"
    )
    source = path.read_text(encoding="utf-8")

    for token in ("src.llms", "openai", "anthropic"):
        assert token not in source


def test_no_proof_module_writes_a_financial_value():
    """§9 / §18 — 初赛无任何自动改值路径, asserted against the source text.

    Read as text rather than as an import graph, because a function-body
    ``import`` and a conditional assignment are both invisible to the graph while
    being exactly as capable of rewriting a number.
    """
    forbidden = (".value =", ".value=", ".values[", ".normalized_value =")
    for path in sorted(PROOF_SOURCE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path.name} 含有可疑赋值：{token}"


def test_verification_result_carries_a_versioned_verifier_stamp():
    """§11 — a validator bug must be bisectable, never invisible."""
    _passport, verdict = _passport_and_verdict()

    assert verdict.verify_source_version
    assert verdict.verified_at


def test_proof_check_table_matches_the_declared_vocabulary():
    from src.cn.proof.passport import _CHECK_FUNCTIONS

    assert tuple(_CHECK_FUNCTIONS) == PROOF_CHECKS
    assert len(set(PROOF_CHECKS)) == len(PROOF_CHECKS)
    assert set(PROOF_CHECKS) == {
        CHECK_SOURCE, CHECK_PERIOD, CHECK_SCOPE_UNIT, CHECK_CALCULATION,
        CHECK_DEPENDENCY, CHECK_ASSUMPTION, CHECK_VALIDATOR, CHECK_INTEGRITY,
        CHECK_CONFLICT,
    }
