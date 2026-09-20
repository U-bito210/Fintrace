"""Offline tests for CARD-09 Claim-Evidence Graph.

The card's §11 cases are the spine:

* the instruction's worked example — ``'盈利质量下降' -> C18 -> CFO/NI=0.61 ->
  CAL12 -> CFO E51 (P118) + 净利润 E37 (P96)`` — must build and ``trace()`` all
  the way down to both PDF pages;
* the three binding rules, positive and negative;
* evidence deletion -> blocked propagation;
* cycle rejection and JSON round-trip fidelity;
* the full REQUIRED / SUPPORTING / OPTIONAL propagation table, including the
  ``CONFLICTED -> BLOCKED`` path;
* thesis status derivation (supported / weakened / blocked / stale).

Plus the §12 integration chain CARD-01 -> CARD-02 -> CARD-03 -> CARD-09 on a
synthetic annual report, and the failure-injection scene the demo relies on.

Everything is offline: synthetic fixtures and hand-built facts only.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from src.cn.analysis import (
    CalculationRegistry,
    Operand,
    analyze_document,
    analyze_financials,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.evidence import EvidenceLedger
from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    ROLE_OPTIONAL,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    THESIS_BLOCKED,
    THESIS_STALE,
    THESIS_SUPPORTED,
    THESIS_WEAKENED,
    UPSTREAM_BLOCKED,
    VALIDATION_BLOCKED,
    VALIDATION_STALE,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    VERIFICATION_CONFLICTED,
    CalculationNode,
    Claim,
    ClaimGraph,
    DependencyRef,
    EvidenceNode,
    GraphCycleError,
    GraphError,
    GraphOperand,
    Thesis,
    build_graph,
    build_graph_from_analysis,
    claims_from_analysis,
    deactivate_evidence,
    detect_cycles,
    mark_stale,
    propagate_state,
    revalidate,
    thesis_status,
)
from src.cn.normalization import (
    NormalizedFact,
    PeriodKind,
    Scope,
    Version,
    normalize_financials,
)
from src.cn.normalization.evidence_bridge import (
    normalization_jumps,
    register_normalized_evidence,
)
from src.validation import validate_graph, validate_graph_corpus

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _evidence(
    evidence_id: str,
    *,
    page: int = 1,
    metric: str = "m",
    value: float = 1.0,
    document_id: str = "DOC",
    available_at: str | None = None,
    kind: str = "fact",
) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        kind=kind,
        document_id=document_id,
        page=page,
        bbox=(1.0, 2.0, 30.0, 8.0),
        available_at=available_at,
        metric=metric,
        value=value,
        unit="元",
        provider="test",
    )


def _instruction_graph() -> ClaimGraph:
    """The card's worked example, exactly as written in §3."""
    evidence = [
        _evidence("E51", page=118, metric="cfo", value=6.1e8),
        _evidence("E37", page=96, metric="net_profit_parent", value=1.0e9),
    ]
    calculation = CalculationNode(
        calculation_id="CAL12",
        metric="cfo_to_net_profit",
        formula="CFO / 归母净利润",
        operation="divide",
        inputs=(
            GraphOperand(kind="evidence", ref="E51", value=6.1e8),
            GraphOperand(kind="evidence", ref="E37", value=1.0e9),
        ),
        output={"value": 0.61, "unit": "MULTIPLE", "period_key": "2024FY", "period_basis": "FY"},
    )
    claims = [
        Claim(
            claim_id="C11",
            claim_type=CLAIM_FACT,
            text="2024FY CFO/归母净利润 = 0.61",
            evidence_ids=("E51", "E37"),
            calculation_ids=("CAL12",),
        ),
        Claim(
            claim_id="C18",
            claim_type=CLAIM_INFERENCE,
            text="盈利质量下降",
            derived_from=("C11",),
        ),
    ]
    return build_graph("run-instruction", claims=claims, calculations=[calculation], evidence_index=evidence)


# --------------------------------------------------------------------------- #
# §11 instruction example
# --------------------------------------------------------------------------- #

def test_instruction_example_builds_and_traces_to_both_pages():
    graph = _instruction_graph()
    tree = graph.trace("C18")

    assert tree.node_type == "claim"
    assert tree.detail["claim_type"] == CLAIM_INFERENCE
    assert tree.locatable

    ids = {node.node_id for node in tree.flatten()}
    assert {"C18", "C11", "CAL12", "E51", "E37"} <= ids

    pages = {node.detail.get("page") for node in tree.flatten() if node.node_type == "page"}
    assert pages == {118, 96}
    # Every leaf the trace lands on must be a locator, not a dead end.
    assert all(node.locatable for node in tree.leaves())


def test_instruction_example_reads_like_the_card():
    """Trace payload must answer 'why does the system say this'."""
    graph = _instruction_graph()
    tree = graph.trace("C18").to_dict()
    calculation = next(
        node for node in _walk(tree) if node["node_type"] == "calculation"
    )
    assert calculation["node_id"] == "CAL12"
    assert calculation["detail"]["formula"] == "CFO / 归母净利润"
    assert [item["ref"] for item in calculation["detail"]["inputs"]] == ["E51", "E37"]


def _walk(node: dict) -> List[dict]:
    out = [node]
    for child in node.get("children", ()):
        out.extend(_walk(child))
    return out


def test_instruction_example_is_detection_clean():
    report = _instruction_graph().detect()
    assert report.clean, report.to_dict()
    assert report.orphan_claims == ()
    assert report.missing_evidence == ()


# --------------------------------------------------------------------------- #
# §11 three binding rules — positives and negatives
# --------------------------------------------------------------------------- #

def test_fact_needs_evidence():
    graph = build_graph("r", claims=[Claim("C1", CLAIM_FACT, "营收 1 亿")])
    assert graph.claim("C1").validation_status == VALIDATION_UNSUPPORTED
    assert graph.detect_orphan_claims() == ["C1"]
    result = validate_graph(graph)
    assert not result.valid
    assert "orphan_claim:C1" in result.errors


def test_inference_needs_derived_from():
    graph = build_graph("r", claims=[Claim("C1", CLAIM_INFERENCE, "盈利质量下降")])
    assert graph.detect_orphan_claims() == []  # not a fact, so not an orphan
    result = validate_graph(graph)
    assert "inference_without_derivation:C1" in result.errors


def test_opinion_needs_an_assumption():
    graph = build_graph("r", claims=[Claim("C1", CLAIM_OPINION, "估值便宜")])
    result = validate_graph(graph)
    assert "opinion_without_assumption:C1" in result.errors


def test_opinion_with_assumption_is_accepted():
    claim = Claim("C1", CLAIM_OPINION, "估值便宜", assumption_ids=("assumption: 参考行业中枢",))
    graph = build_graph("r", claims=[claim])
    assert graph.claim("C1").validation_status == VALIDATION_SUPPORTED
    assert validate_graph(graph).valid


def test_numeric_fact_must_bind_a_number():
    """A fact that asserts a number must bind a calculation or numeric evidence."""
    evidence = _evidence("E1", value=1.0)
    claim = Claim("C1", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    graph = build_graph("r", claims=[claim], evidence_index=[evidence])
    assert graph.claim("C1").validation_status == VALIDATION_SUPPORTED
    assert validate_graph(graph).valid


def test_numeric_fact_without_any_numeric_binding_is_rejected():
    """Evidence that carries no value cannot back a numeric assertion."""
    evidence = EvidenceNode(evidence_id="E1", kind="fact", document_id="DOC", page=1, value=None)
    claim = Claim("C1", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    graph = build_graph("r", claims=[claim], evidence_index=[evidence])
    result = validate_graph(graph)
    assert "numeric_fact_without_binding:C1" in result.errors


def test_dangling_evidence_is_reported_not_dropped():
    claim = Claim("C1", CLAIM_FACT, "营收 1 亿", evidence_ids=("E-MISSING",))
    graph = build_graph("r", claims=[claim])
    assert graph.detect_missing_evidence() == ["E-MISSING"]
    result = validate_graph(graph)
    assert "missing_evidence:E-MISSING" in result.errors


# --------------------------------------------------------------------------- #
# §11 evidence deletion -> blocked propagation
# --------------------------------------------------------------------------- #

def test_deactivating_evidence_blocks_the_fact_and_every_inference():
    graph = _instruction_graph()
    impacted = deactivate_evidence(graph, "E51", "来源公告被更正")

    assert "C11" in impacted and "C18" in impacted
    assert graph.claim("C11").validation_status == VALIDATION_BLOCKED
    assert graph.claim("C18").validation_status == VALIDATION_BLOCKED


def test_deactivation_is_an_audit_record_not_a_delete():
    graph = _instruction_graph()
    deactivate_evidence(graph, "E51", "来源公告被更正")

    # The node still exists — it is marked inactive, never removed (§6).
    assert "E51" in graph.evidence_index
    assert graph.evidence_index["E51"].active is False
    assert graph.evidence_index["E51"].deactivated_reason == "来源公告被更正"
    assert len(graph.deactivations) == 1
    assert graph.deactivations[0]["sequence"] == 0


def test_deactivation_requires_a_reason():
    graph = _instruction_graph()
    with pytest.raises(GraphError):
        deactivate_evidence(graph, "E51", "   ")
    with pytest.raises(GraphError):
        deactivate_evidence(graph, "E-NOPE", "reason")


def test_revalidate_returns_old_to_new_diff():
    graph = _instruction_graph()
    assert graph.claim("C18").validation_status == VALIDATION_SUPPORTED

    deactivate_evidence(graph, "E37", "净利润改用重述后数据，原披露值失效")
    report = revalidate(graph)

    changes = {item["claim_id"]: item for item in report.status_changes}
    assert changes["C11"]["old"] == VALIDATION_SUPPORTED
    assert changes["C11"]["new"] == VALIDATION_BLOCKED
    assert changes["C18"]["new"] == VALIDATION_BLOCKED
    assert report.blocked_claims == ("C11", "C18")
    assert report.changed_evidence_ids == ("E37",)
    assert set(report.impacted_claims) == {"C11", "C18"}


def test_revalidation_report_round_trips():
    graph = _instruction_graph()
    deactivate_evidence(graph, "E51", "x")
    payload = revalidate(graph).to_dict()
    assert payload["run_id"] == "run-instruction"
    assert payload["blocked_claims"] == ["C11", "C18"]
    assert all({"claim_id", "old", "new"} <= set(item) for item in payload["status_changes"])


# --------------------------------------------------------------------------- #
# §11 cycle rejection + serialisation fidelity
# --------------------------------------------------------------------------- #

def test_dependency_cycle_is_rejected_at_build_time():
    claims = [
        Claim("A", CLAIM_INFERENCE, "a", derived_from=("B",)),
        Claim("B", CLAIM_INFERENCE, "b", derived_from=("A",)),
    ]
    with pytest.raises(GraphCycleError):
        build_graph("r", claims=claims)


def test_detect_cycles_exposes_the_cycle_path():
    graph = ClaimGraph(run_id="r")
    graph.add_claim(Claim("A", CLAIM_INFERENCE, "a", derived_from=("B",)))
    graph.add_claim(Claim("B", CLAIM_INFERENCE, "b", derived_from=("A",)))
    cycles = detect_cycles(graph)
    assert cycles and set(cycles[0]) == {"A", "B"}


def test_graph_json_round_trip_is_faithful():
    graph = _instruction_graph()
    payload = graph.export_json()
    restored = ClaimGraph.from_dict(payload)

    assert restored.run_id == graph.run_id
    assert set(restored.claims) == set(graph.claims)
    assert set(restored.calculations) == set(graph.calculations)
    assert set(restored.evidence_index) == set(graph.evidence_index)
    assert restored.trace("C18").to_dict() == graph.trace("C18").to_dict()
    assert restored.export_json() == payload
    assert restored.detect().to_dict() == graph.detect().to_dict()


# --------------------------------------------------------------------------- #
# §11 propagation table
# --------------------------------------------------------------------------- #

def _dependent_graph(role: str):
    """UP depends on E1 with ``role``; DOWN depends on UP as REQUIRED."""
    upstream = Claim(
        "UP",
        CLAIM_FACT,
        "营收 1 亿",
        evidence_ids=("E1",),
        dependency_metadata=(DependencyRef("E1", TARGET_EVIDENCE, role),),
    )
    downstream = Claim(
        "DOWN",
        CLAIM_INFERENCE,
        "增长在放缓",
        derived_from=("UP",),
        dependency_metadata=(DependencyRef("UP", TARGET_CLAIM, ROLE_REQUIRED),),
    )
    graph = build_graph(
        "r", claims=[upstream, downstream], evidence_index=[_evidence("E1", value=1.0)]
    )
    deactivate_evidence(graph, "E1", "源数据失效")
    return graph


def test_required_evidence_death_blocks_the_claim_and_its_dependents():
    """REQUIRED upstream BLOCKED -> downstream blocked (card §7b row 1)."""
    graph = _dependent_graph(ROLE_REQUIRED)
    assert graph.claim("UP").validation_status == VALIDATION_BLOCKED
    assert graph.claim("DOWN").validation_status == VALIDATION_BLOCKED


def test_supporting_evidence_death_degrades_coverage_without_blocking():
    """SUPPORTING loss drops coverage but never blocks the claim on its own."""
    graph = _dependent_graph(ROLE_SUPPORTING)
    assert graph.claim("UP").validation_status == VALIDATION_SUPPORTED
    assert any("coverage_dropped:UP" in warning for warning in graph.warnings)
    # UP still holds, so its REQUIRED dependent is untouched.
    assert graph.claim("DOWN").validation_status == VALIDATION_SUPPORTED


def test_optional_evidence_death_is_ignored_entirely():
    """OPTIONAL never blocks and never even records a coverage drop."""
    graph = _dependent_graph(ROLE_OPTIONAL)
    assert graph.claim("UP").validation_status == VALIDATION_SUPPORTED
    assert not any("coverage_dropped:UP" in warning for warning in graph.warnings)
    assert graph.claim("DOWN").validation_status == VALIDATION_SUPPORTED


def test_required_and_supporting_differ_on_a_secondary_edge():
    """A claim with one healthy REQUIRED edge and one broken SUPPORTING edge."""
    upstream = Claim("UP", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    supporting_only = Claim(
        "S1",
        CLAIM_FACT,
        "行业数据",
        evidence_ids=("E2",),
    )
    mixed = Claim(
        "M1",
        CLAIM_INFERENCE,
        "份额提升",
        derived_from=("UP",),
        dependency_metadata=(
            DependencyRef("UP", TARGET_CLAIM, ROLE_REQUIRED),
            DependencyRef("S1", TARGET_CLAIM, ROLE_SUPPORTING),
        ),
    )
    graph = build_graph(
        "r",
        claims=[upstream, supporting_only, mixed],
        evidence_index=[_evidence("E1", value=1.0), _evidence("E2", value=2.0)],
    )
    deactivate_evidence(graph, "E2", "行业数据来源撤回")
    propagate_state(graph, changed_ids=["E2"])

    # SUPPORTING loss degrades coverage but must NOT block the claim.
    assert graph.claim("M1").validation_status == VALIDATION_SUPPORTED
    assert any("coverage_dropped:M1" in warning for warning in graph.warnings)


def test_optional_edge_never_blocks_and_never_drops_coverage():
    upstream = Claim("UP", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    optional = Claim("O1", CLAIM_FACT, "旁证", evidence_ids=("E2",))
    main = Claim(
        "M1",
        CLAIM_INFERENCE,
        "份额提升",
        derived_from=("UP",),
        dependency_metadata=(
            DependencyRef("UP", TARGET_CLAIM, ROLE_REQUIRED),
            DependencyRef("O1", TARGET_CLAIM, ROLE_OPTIONAL),
        ),
    )
    graph = build_graph(
        "r",
        claims=[upstream, optional, main],
        evidence_index=[_evidence("E1", value=1.0), _evidence("E2", value=2.0)],
    )
    deactivate_evidence(graph, "E2", "旁证撤回")
    propagate_state(graph, changed_ids=["E2"])

    assert graph.claim("M1").validation_status == VALIDATION_SUPPORTED
    assert not any("coverage_dropped:M1" in warning for warning in graph.warnings)


def test_conflicted_upstream_blocks_a_required_downstream():
    """CONFLICTED -> BLOCKED is an explicit row of the §7b table."""
    upstream = Claim(
        "UP",
        CLAIM_FACT,
        "营收 1 亿",
        evidence_ids=("E1",),
        verification_status=VERIFICATION_CONFLICTED,
    )
    downstream = Claim("DOWN", CLAIM_INFERENCE, "增长放缓", derived_from=("UP",))
    graph = build_graph(
        "r", claims=[upstream, downstream], evidence_index=[_evidence("E1", value=1.0)]
    )
    assert graph.claim("UP").validation_status == VALIDATION_SUPPORTED
    assert graph.claim("DOWN").validation_status == VALIDATION_BLOCKED


def test_stale_required_upstream_marks_the_downstream_stale():
    upstream = Claim("UP", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    downstream = Claim("DOWN", CLAIM_INFERENCE, "增长放缓", derived_from=("UP",))
    graph = build_graph(
        "r", claims=[upstream, downstream], evidence_index=[_evidence("E1", value=1.0)]
    )
    mark_stale(graph, "UP", "该期数据已被更正公告取代")
    assert graph.claim("UP").validation_status == VALIDATION_STALE
    assert graph.claim("DOWN").validation_status == VALIDATION_STALE


def test_mark_stale_requires_known_claim_and_reason():
    graph = build_graph("r", claims=[Claim("C1", CLAIM_FACT, "x", evidence_ids=("E1",))])
    with pytest.raises(GraphError):
        mark_stale(graph, "NOPE", "reason")
    with pytest.raises(GraphError):
        mark_stale(graph, "C1", "")


def test_explicit_role_overrides_the_required_default():
    """An explicit dependency_metadata role wins over the REQUIRED default."""
    from src.cn.graph import resolve_targets

    claim = Claim(
        "C1",
        CLAIM_INFERENCE,
        "x",
        derived_from=("UP",),
        dependency_metadata=(DependencyRef("UP", TARGET_CLAIM, ROLE_OPTIONAL),),
    )
    assert ("claim", "UP", ROLE_OPTIONAL) in resolve_targets(claim)


# --------------------------------------------------------------------------- #
# §11 thesis status
# --------------------------------------------------------------------------- #

def _thesis_graph(*, stale: bool = False, break_supporting: bool = False):
    evidence = [_evidence("E1", value=1.0), _evidence("E2", value=2.0)]
    required_claim = Claim("R1", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    supporting_claim = Claim("S1", CLAIM_FACT, "行业数据", evidence_ids=("E2",))
    thesis = Thesis(
        thesis_id="T1",
        statement="盈利质量改善",
        claim_ids=("R1", "S1"),
        dependency_roles={"R1": ROLE_REQUIRED, "S1": ROLE_SUPPORTING},
    )
    graph = build_graph("r", claims=[required_claim, supporting_claim], theses=[thesis], evidence_index=evidence)
    if break_supporting:
        deactivate_evidence(graph, "E2", "行业数据撤回")
    if stale:
        mark_stale(graph, "R1", "被更正公告取代")
    propagate_state(graph)
    return graph


def test_thesis_is_supported_when_required_claims_hold():
    graph = _thesis_graph()
    assert thesis_status(graph, graph.theses["T1"]) == THESIS_SUPPORTED


def test_thesis_is_weakened_when_only_a_supporting_claim_fails():
    graph = _thesis_graph(break_supporting=True)
    assert thesis_status(graph, graph.theses["T1"]) == THESIS_WEAKENED


def test_thesis_is_blocked_when_a_required_claim_fails():
    graph = _thesis_graph()
    deactivate_evidence(graph, "E1", "核心来源失效")
    propagate_state(graph)
    assert thesis_status(graph, graph.theses["T1"]) == THESIS_BLOCKED


def test_thesis_is_stale_when_a_required_claim_is_stale():
    graph = _thesis_graph(stale=True)
    assert thesis_status(graph, graph.theses["T1"]) == THESIS_STALE


def test_thesis_status_disagreement_is_a_validation_error():
    graph = _thesis_graph()
    graph.theses["T1"].status = THESIS_BLOCKED  # a caller asserting a stale status
    result = validate_graph(graph)
    assert any(item.startswith("thesis_status_stale:T1") for item in result.errors)


# --------------------------------------------------------------------------- #
# §10 validator
# --------------------------------------------------------------------------- #

def test_clean_graph_validates_and_carries_the_detection_report():
    result = validate_graph(_instruction_graph())
    assert result.valid, result.errors
    assert result.detection["clean"] is True
    assert set(result.detection) >= {"orphan_claims", "unsupported_calculations", "missing_evidence"}


def test_unsupported_calculation_is_detected_and_validated():
    calculation = CalculationNode(
        calculation_id="CALX",
        metric="ratio",
        formula="a / b",
        operation="divide",
        inputs=(
            GraphOperand(kind="evidence", ref="E-MISSING", value=1.0),
            GraphOperand(kind="evidence", ref="E2", value=2.0),
        ),
        output={"value": 0.5, "unit": "MULTIPLE"},
    )
    graph = build_graph(
        "r",
        calculations=[calculation],
        evidence_index=[_evidence("E2", value=2.0)],
    )
    assert graph.detect_unsupported_calculations() == ["CALX"]
    assert "missing_evidence:E-MISSING" in validate_graph(graph).errors


def test_unlocatable_evidence_is_a_warning_not_a_silent_pass():
    evidence = EvidenceNode(evidence_id="E1", kind="fact", document_id="DOC", page=None, value=1.0)
    claim = Claim("C1", CLAIM_FACT, "营收 1 亿", evidence_ids=("E1",))
    graph = build_graph("r", claims=[claim], evidence_index=[evidence])
    result = validate_graph(graph)
    assert "unlocatable_evidence:E1" in result.warnings
    assert "unreachable_evidence:C1" in result.errors


def test_validator_reports_the_reproducibility_check_as_skipped_without_a_registry():
    graph = _instruction_graph()
    result = validate_graph(graph)  # no registry passed
    assert "no_registry_calculation_recheck_skipped" in result.warnings


def test_validator_rechecks_calculations_against_the_registry():
    registry = CalculationRegistry()
    record = registry.register(
        metric="cfo_to_net_profit",
        formula="CFO / 归母净利润",
        operation="divide",
        operands=(
            Operand(kind="evidence", operand_id="E51", value=6.1e8),
            Operand(kind="evidence", operand_id="E37", value=1.0e9),
        ),
        value=0.61,
        period_key="2024FY",
        period_basis="FY",
    )
    graph = build_graph(
        "r",
        claims=[
            Claim(
                "C1",
                CLAIM_FACT,
                "CFO/净利润 = 0.61",
                evidence_ids=("E51", "E37"),
                calculation_ids=(record.calculation_id,),
            ),
        ],
        calculations=registry,
        evidence_index=[_evidence("E51", page=118, value=6.1e8), _evidence("E37", page=96, value=1.0e9)],
    )
    result = validate_graph(graph, registry=registry)
    assert result.valid, result.errors


def test_validator_corpus_aggregation():
    result = validate_graph_corpus([_instruction_graph()])
    assert result.valid
    assert result.detection["graph_count"] == 1
    assert result.detection["clean_graphs"] == 1


# --------------------------------------------------------------------------- #
# §12 CARD-01 → CARD-02 → CARD-03 → CARD-09 全链
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def _chain(registry: DocumentRegistry):
    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(parsed.facts, parsed.document)
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=parsed.document.document_id)
    register_normalized_evidence(parsed.document, normalization, ledger=ledger)
    jumps = normalization_jumps(parsed.document, normalization)
    graph = build_graph_from_analysis(
        analysis,
        run_id="run-chain",
        document_id=parsed.document.document_id,
        ledger=ledger,
        jumps=jumps,
    )
    return parsed, normalization, analysis, ledger, graph


def test_full_chain_graph_has_zero_orphans_unsupported_and_missing(registry):
    parsed, normalization, analysis, ledger, graph = _chain(registry)

    assert graph.claims, "the chain must produce claims"
    assert graph.facts, "metric rows must become fact claims"

    report = graph.detect()
    assert report.clean, report.to_dict()

    result = validate_graph(graph, registry=analysis.registry)
    assert result.valid, result.errors
    assert result.detection["clean"] is True


def test_full_chain_every_fact_traces_to_a_pdf_page(registry):
    _parsed, _normalization, _analysis, _ledger, graph = _chain(registry)
    for claim in graph.facts:
        tree = graph.trace(claim.claim_id)
        assert tree.locatable, claim.claim_id
        pages = [node.detail.get("page") for node in tree.flatten() if node.node_type == "page"]
        assert pages and all(isinstance(page, int) for page in pages)


def test_full_chain_json_export_is_serialisable(registry):
    import json

    _parsed, _normalization, _analysis, _ledger, graph = _chain(registry)
    payload = graph.export_json()
    # Must survive a real JSON round-trip — this is what the UI/evidence pack sends.
    text = json.dumps(payload, ensure_ascii=False, default=str)
    restored = ClaimGraph.from_dict(json.loads(text))
    assert restored.export_json()["claims"].keys() == payload["claims"].keys()


def test_full_chain_metrics_are_classified_by_what_the_registry_knows(registry):
    """A calculation id in ``MetricResult.inputs`` must not become 'evidence'."""
    _parsed, _normalization, analysis, _ledger, graph = _chain(registry)
    known = {record.calculation_id for record in analysis.registry.records()}
    for claim in graph.claims.values():
        assert not (set(claim.evidence_ids) & known)
        assert not (set(claim.calculation_ids) & (set(claim.evidence_ids)))


def test_full_chain_derived_metrics_still_bind_evidence_directly(registry):
    """A derived metric (free cash flow) must expose its operands as evidence."""
    _parsed, _normalization, _analysis, _ledger, graph = _chain(registry)
    with_calculation = [claim for claim in graph.facts if claim.calculation_ids]
    assert with_calculation, "the chain must record calculations"
    assert all(claim.evidence_ids for claim in with_calculation), [
        claim.claim_id for claim in with_calculation if not claim.evidence_ids
    ]


# --------------------------------------------------------------------------- #
# §12 engine-signal chain: CARD-03 signals → inference claims → failure injection
# --------------------------------------------------------------------------- #

_POINT = PeriodKind.POINT_IN_TIME
_YTD = PeriodKind.YTD_CUMULATIVE


def _nf(
    metric: str,
    value: float,
    period_key: str,
    *,
    kind: PeriodKind = _YTD,
    statement: str = "income",
    page: int = 1,
    document_id: str = "DOC-G",
) -> NormalizedFact:
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period_key}_{page}",
        metric=metric,
        value=float(value),
        currency="CNY",
        period_key=period_key,
        period_kind=kind,
        scope=Scope.CONSOLIDATED,
        version=Version.AS_REPORTED,
        available_at="2025-04-30T00:00:00+08:00",
        source_fact_id=f"x_{metric}_{period_key}",
        document_id=document_id,
        page=page,
        row=1,
        column=1,
        raw_value=str(value),
        source_unit="元",
        source_table="合并报表",
        statement_type=statement,
        metric_known=True,
    )


def _rich_facts() -> List[NormalizedFact]:
    """Two annual years with enough inputs to fire three CARD-03 signals."""
    facts: List[NormalizedFact] = []
    for year, revenue, parent, deducted, cost in (
        (2023, 1000.0, 100.0, 90.0, 600.0),
        (2024, 1200.0, 120.0, 80.0, 700.0),
    ):
        facts += [
            _nf("revenue", revenue, f"{year}FY"),
            _nf("net_profit_parent", parent, f"{year}FY"),
            _nf("net_profit_deducted", deducted, f"{year}FY"),
            _nf("operating_cost", cost, f"{year}FY"),
        ]
    for year, cfo, capex in ((2023, 150.0, 60.0), (2024, 100.0, 70.0)):
        facts += [
            _nf("cfo", cfo, f"{year}FY", statement="cashflow"),
            _nf("capex", capex, f"{year}FY", statement="cashflow"),
        ]
    for year, equity, receivables, inventory in (
        (2023, 800.0, 200.0, 100.0),
        (2024, 900.0, 260.0, 105.0),
    ):
        facts += [
            _nf("equity_parent", equity, f"{year}FY", kind=_POINT, statement="balance"),
            _nf("receivables", receivables, f"{year}FY", kind=_POINT, statement="balance"),
            _nf("inventory", inventory, f"{year}FY", kind=_POINT, statement="balance"),
        ]
    facts.append(_nf("contract_liabilities", 50.0, "2024FY", kind=_POINT, statement="balance"))
    return facts


def _engine_graph():
    """CARD-02-shaped facts → CARD-03 engine → CARD-09 graph (signals included)."""
    facts = _rich_facts()
    analysis = analyze_financials(facts, document_id="DOC-G", report_period="2024FY")
    evidence = [
        EvidenceNode(
            evidence_id=fact.normalized_fact_id,
            kind="fact",
            document_id=fact.document_id,
            page=fact.page,
            bbox=fact.bbox,
            metric=fact.metric,
            value=fact.value,
            unit="元",
            available_at=fact.available_at,
        )
        for fact in facts
    ]
    graph = build_graph_from_analysis(
        analysis, run_id="run-engine", document_id="DOC-G", evidence_index=evidence
    )
    return analysis, graph


def test_engine_signals_enter_the_graph_as_inferences_not_facts():
    analysis, graph = _engine_graph()
    assert analysis.signals, "the fixture must fire at least one diagnostic signal"

    signal_claims = [claim for claim in graph.claims.values() if claim.signal_ids]
    assert signal_claims
    assert all(claim.claim_type == CLAIM_INFERENCE for claim in signal_claims)
    for claim in signal_claims:
        assert claim.derived_from
        for upstream in claim.derived_from:
            assert graph.claim(upstream).claim_type == CLAIM_FACT
        assert claim.validation_status == VALIDATION_SUPPORTED


def test_engine_graph_is_detection_clean_and_valid():
    analysis, graph = _engine_graph()
    report = graph.detect()
    assert report.clean, report.to_dict()
    result = validate_graph(graph, registry=analysis.registry)
    assert result.valid, result.errors


def test_failure_injection_6_deleting_evidence_blocks_the_conclusion():
    """The demo's closing scene, reproduced offline (§15)."""
    analysis, graph = _engine_graph()
    inference = graph.inferences[0]
    upstream_fact = inference.derived_from[0]
    evidence_id = graph.claim(upstream_fact).evidence_ids[0]

    assert graph.claim(inference.claim_id).validation_status == VALIDATION_SUPPORTED
    deactivate_evidence(graph, evidence_id, "失败注入 #6：来源证据被撤回")
    report = revalidate(graph)

    assert upstream_fact in report.impacted_claims
    assert inference.claim_id in report.blocked_claims
    assert graph.claim(inference.claim_id).validation_status == VALIDATION_BLOCKED
    # The graph is still fully serialisable after the failure — no half-built graph.
    assert graph.export_json()["run_id"] == "run-engine"
    # A *blocked* conclusion is not a *malformed* graph: detection stays clean,
    # which is exactly why status and structure are reported separately.
    later = validate_graph(graph, registry=analysis.registry)
    assert later.valid, later.errors
    assert later.detection["clean"] is True


# --------------------------------------------------------------------------- #
# §6 agent tools
# --------------------------------------------------------------------------- #

def test_trace_tool_walks_a_claim_to_its_page(registry):
    import asyncio
    import json

    from src.agents.tools.cn_tools import TraceCnClaimTool

    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    document_id = parsed.document.document_id
    # Discover a fact claim id from the same deterministic chain the tool builds.
    graph, _analysis, _document = _chain_graph_for(registry, document_id)
    claim_id = sorted(graph.facts, key=lambda item: item.claim_id)[0].claim_id

    payload = json.loads(asyncio.run(TraceCnClaimTool(registry).execute(document_id=document_id, claim_id=claim_id)))
    assert payload["status"] == "ok"
    assert payload["claim"]["claim_id"] == claim_id
    assert payload["locatable"] is True
    assert "trace_tree" in payload
    assert payload["detection"]["clean"] is True


def test_trace_tool_reports_an_unknown_claim(registry):
    import asyncio
    import json

    from src.agents.tools.cn_tools import TraceCnClaimTool

    registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    document_id = registry.list()[0].document_id
    payload = json.loads(asyncio.run(TraceCnClaimTool(registry).execute(document_id=document_id, claim_id="NOPE")))
    assert payload["status"] == "error"
    assert payload["error_code"] == "claim_not_found"


def test_deactivate_tool_blocks_the_downstream_claim(registry):
    import asyncio
    import json

    from src.agents.tools.cn_tools import DeactivateCnEvidenceTool

    registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    document_id = registry.list()[0].document_id
    graph, _analysis, _doc = _chain_graph_for(registry, document_id)
    # Pick an evidence id that a fact claim actually depends on.
    claim = sorted(graph.facts, key=lambda item: item.claim_id)[0]
    evidence_id = claim.evidence_ids[0]

    payload = json.loads(
        asyncio.run(
            DeactivateCnEvidenceTool(registry).execute(
                document_id=document_id, evidence_id=evidence_id, reason="演示：来源证据撤回"
            )
        )
    )
    assert payload["status"] == "ok"
    assert payload["audit_mode"] == "append_only_no_physical_delete"
    assert claim.claim_id in payload["impacted_claims"]
    assert claim.claim_id in payload["blocked_claims"]
    assert payload["status_after"][claim.claim_id] == VALIDATION_BLOCKED


def test_deactivate_tool_requires_a_reason(registry):
    import asyncio
    import json

    from src.agents.tools.cn_tools import DeactivateCnEvidenceTool

    payload = json.loads(
        asyncio.run(
            DeactivateCnEvidenceTool(registry).execute(
                document_id="X", evidence_id="E", reason="  "
            )
        )
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "deactivation_missing_reason"


def _chain_graph_for(registry: DocumentRegistry, document_id: str):
    from src.agents.tools.cn_tools import _document_claim_graph

    return _document_claim_graph(registry, document_id)
