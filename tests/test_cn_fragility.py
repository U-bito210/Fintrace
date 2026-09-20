"""Offline tests for CARD-24 Thesis Fragility Engine.

The card's §14 cases are the spine:

* critical dependency counting on hand-built small graphs (who sits on a
  REQUIRED path, who is shared by several theses);
* minimal cut sets with the **reverse assertion** of §13 — remove the set, the
  thesis must go BLOCKED — plus depth limiting and cycle rejection;
* single points of failure (the only path);
* numeric fragility: source whitelist, unit mismatch, the ``at_threshold``
  boundary, and "no thresholds → an honest empty array";
* monitoring plan field completeness.

Plus §15's integration chain CARD-09 ``build_graph`` → CARD-24 analysis on a
synthetic annual report, the "deactivate an evidence then re-analyse" refresh,
the CARD-23 passport rollup, the §13 validator's own rules (including the two
ways to hand it a bad report), and the tool contract.

Everything is offline: synthetic fixtures and hand-built facts only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List

import pytest

from src.cn.analysis import analyze_document
from src.cn.documents.registry import DocumentRegistry
from src.cn.evidence import EvidenceLedger
from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    ROLE_OPTIONAL,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    THESIS_BLOCKED,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    CalculationNode,
    Claim,
    DependencyRef,
    EvidenceNode,
    GraphOperand,
    GraphCycleError,
    Thesis,
    build_graph,
    build_graph_from_analysis,
    deactivate_evidence,
)
from src.cn.normalization import normalize_financials
from src.cn.normalization.evidence_bridge import (
    normalization_jumps,
    register_normalized_evidence,
)
from src.cn.fragility import (
    DEFAULT_MAX_SETS,
    THESIS_SOURCE_DECLARED,
    THESIS_SOURCE_INFERENCES,
    THESIS_SOURCE_NONE,
    THRESHOLD_ANALYST_INPUT,
    THRESHOLD_MODEL_CONSTRAINT,
    THRESHOLD_PUBLIC_ASSUMPTION,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    FragilityError,
    FragilityThreshold,
    build_fragility_report,
    compute_minimal_cut_sets,
    compute_numeric_fragility,
    cut_blocks_thesis,
    detect_single_points_of_failure,
    generate_monitoring_plans,
    identify_critical_nodes,
    identify_fragile_assumptions,
    pivotal_nodes,
    required_ancestry,
    resolve_theses,
    simulate_cut,
    theses_from_declaration,
    theses_from_inferences,
)
from src.validation import validate_fragility, validate_fragility_corpus
from src.validation.checker_validator import check_source_is_model_free

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"
FRAGILITY_PACKAGE = Path(__file__).parents[1] / "src" / "cn" / "fragility"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _evidence(
    evidence_id: str,
    *,
    metric: str = "",
    value: float = 1.0,
    unit: str = "元",
    active: bool = True,
) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        kind="fact",
        document_id="DOC-1",
        page=1,
        metric=metric or None,
        value=value,
        unit=unit,
        period_key="2025FY",
        active=active,
    )


def _calc(
    calculation_id: str,
    *,
    metric: str = "gross_margin",
    value: float = 31.2,
    unit: str = "PERCENT",
    status: str = "ok",
) -> CalculationNode:
    return CalculationNode(
        calculation_id=calculation_id,
        metric=metric,
        formula="gp / rev * 100",
        operation="divide",
        inputs=(GraphOperand(kind="evidence", ref="E-GP", value=value),),
        output={"value": value, "unit": unit, "period_key": "2025FY", "period_basis": "FY"},
        validation_status=status,
    )


def _fact(claim_id: str, text: str, evidence_ids=(), calculation_ids=(), metric=None) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=CLAIM_FACT,
        text=text,
        evidence_ids=tuple(evidence_ids),
        calculation_ids=tuple(calculation_ids),
        metric=metric,
        period_key="2025FY" if metric else None,
    )


def _thesis(thesis_id: str, claim_ids, roles: Dict[str, str] | None = None) -> Thesis:
    return Thesis(
        thesis_id=thesis_id,
        statement=f"{thesis_id} 的陈述",
        claim_ids=tuple(claim_ids),
        dependency_roles=dict(roles or {}),
    )


def _fan_out_graph():
    """T01 需要 C01（依赖 E01 AND E02）与 C02（依赖 E03）。"""
    claims = [
        Claim(
            "C01",
            CLAIM_FACT,
            "毛利率 31.2%",
            evidence_ids=("E01", "E02"),
            dependency_metadata=(
                DependencyRef("E01", TARGET_EVIDENCE, ROLE_REQUIRED),
                DependencyRef("E02", TARGET_EVIDENCE, ROLE_REQUIRED),
            ),
            metric="gross_margin",
        ),
        _fact("C02", "营业收入 31 亿", evidence_ids=("E03",), metric="revenue"),
    ]
    evidence = [
        _evidence("E01", metric="gross_profit", value=9.7e8),
        _evidence("E02", metric="revenue", value=3.1e9),
        _evidence("E03", metric="revenue", value=3.1e9),
    ]
    return build_graph(
        "run-fanout",
        claims=claims,
        theses=[_thesis("T01", ("C01", "C02"))],
        evidence_index=evidence,
    )


def _single_chain_graph():
    """T01 只有一个 REQUIRED 成员 C01 —— 链上任何一处都是单点。"""
    return build_graph(
        "run-chain",
        claims=[_fact("C01", "毛利率 31.2%", evidence_ids=("E01", "E02"), calculation_ids=("CAL1",), metric="gross_margin")],
        theses=[_thesis("T01", ("C01",))],
        calculations=[_calc("CAL1")],
        evidence_index=[
            _evidence("E01", metric="gross_profit", value=9.7e8),
            _evidence("E02", metric="revenue", value=3.1e9),
        ],
    )


# --------------------------------------------------------------------------- #
# §14 A. Critical dependency detection
# --------------------------------------------------------------------------- #

def test_critical_dependencies_list_every_required_ancestor():
    graph = _fan_out_graph()
    ids = {item["node_id"] for item in identify_critical_nodes(graph)}
    assert ids == {"C01", "C02", "E01", "E02", "E03"}


def test_critical_dependency_records_which_theses_need_it():
    graph = _fan_out_graph()
    by_id = {item["node_id"]: item for item in identify_critical_nodes(graph)}
    assert by_id["E02"]["thesis_ids"] == ["T01"]
    assert by_id["E02"]["depth"] == 1
    assert by_id["E02"]["node_type"] == TARGET_EVIDENCE


def test_a_node_shared_by_two_theses_counts_both():
    graph = _fan_out_graph()
    graph.add_thesis(_thesis("T02", ("C02",)))
    by_id = {item["node_id"]: item for item in identify_critical_nodes(graph)}
    assert by_id["C02"]["thesis_ids"] == ["T01", "T02"]
    assert by_id["E02"]["thesis_ids"] == ["T01"]


def test_supporting_and_optional_dependencies_are_not_critical():
    graph = build_graph(
        "run-roles",
        claims=[_fact("C01", "a", evidence_ids=("E01",)), _fact("C02", "b", evidence_ids=("E02",))],
        theses=[_thesis("T01", ("C01", "C02"), {"C01": ROLE_REQUIRED, "C02": ROLE_SUPPORTING})],
        evidence_index=[_evidence("E01"), _evidence("E02")],
    )
    ids = {item["node_id"] for item in identify_critical_nodes(graph)}
    assert ids == {"C01", "E01"}


def test_optional_dependency_never_reaches_the_required_path():
    graph = build_graph(
        "run-optional",
        claims=[
            Claim(
                "C01",
                CLAIM_FACT,
                "a",
                evidence_ids=("E01",),
                dependency_metadata=(DependencyRef("E02", TARGET_EVIDENCE, ROLE_OPTIONAL),),
            )
        ],
        theses=[_thesis("T01", ("C01",))],
        evidence_index=[_evidence("E01"), _evidence("E02")],
    )
    ids = {item["node_id"] for item in identify_critical_nodes(graph)}
    assert "E02" not in ids


def test_depth_limit_prunes_and_flags_it():
    graph = _fan_out_graph()
    depths, pruned, note = pivotal_nodes(graph, "T01", max_depth=0)
    assert note == ""
    assert pruned is True
    # depth 0 只展开到 C01 / C02 那一层
    assert set(depths) == {"C01", "C02"}


def test_ancestry_reports_a_dangling_required_reference():
    graph = build_graph(
        "run-dangling",
        claims=[_fact("C01", "a", evidence_ids=("E01",))],
        theses=[_thesis("T01", ("C01",))],
        evidence_index=[_evidence("E01")],
    )
    graph.claims["C01"] = Claim(
        "C01",
        CLAIM_FACT,
        "a",
        evidence_ids=("E01",),
        dependency_metadata=(DependencyRef("GHOST", TARGET_CLAIM, ROLE_REQUIRED),),
    )
    ancestry, _pruned = required_ancestry(graph, "C01")
    assert ancestry["GHOST"]["node_type"] == "missing"


# --------------------------------------------------------------------------- #
# §14 B. Single points of failure
# --------------------------------------------------------------------------- #

def test_shared_pivot_is_a_single_point():
    graph = build_graph(
        "run-shared",
        claims=[
            _fact("C01", "a", evidence_ids=("E-REV",)),
            _fact("C02", "b", evidence_ids=("E-REV",)),
        ],
        theses=[_thesis("T01", ("C01", "C02"))],
        evidence_index=[_evidence("E-REV")],
    )
    points = detect_single_points_of_failure(identify_critical_nodes(graph))
    assert [item["node_id"] for item in points] == ["E-REV"]
    assert points[0]["single_point_for"] == ["T01"]


def test_no_single_point_when_each_required_claim_has_its_own_support():
    graph = _fan_out_graph()
    assert detect_single_points_of_failure(identify_critical_nodes(graph)) == []


def test_single_point_on_a_single_claim_thesis_is_the_whole_chain():
    graph = _single_chain_graph()
    points = {item["node_id"] for item in detect_single_points_of_failure(identify_critical_nodes(graph))}
    assert points == {"C01", "CAL1", "E01", "E02"}


def test_detect_single_points_of_failure_filters_the_input_list():
    decorated = [{"node_id": "a", "is_single_point": True}, {"node_id": "b", "is_single_point": False}]
    assert detect_single_points_of_failure(decorated) == [{"node_id": "a", "is_single_point": True}]


def test_blocked_thesis_does_not_claim_single_points():
    graph = _single_chain_graph()
    deactivate_evidence(graph, "E01", "来源撤回")
    graph.propagate_state()
    points = detect_single_points_of_failure(identify_critical_nodes(graph))
    assert points == []


# --------------------------------------------------------------------------- #
# §14 C. Minimal cut sets
# --------------------------------------------------------------------------- #

def test_cut_sets_are_the_singleton_pivots():
    graph = _fan_out_graph()
    cuts = compute_minimal_cut_sets(graph, "T01")
    assert cuts == [["C01"], ["C02"], ["E01"], ["E02"], ["E03"]]


def test_every_cut_set_passes_the_reverse_assertion():
    graph = _single_chain_graph()
    cuts = compute_minimal_cut_sets(graph, "T01")
    assert cuts
    for cut in cuts:
        assert cut_blocks_thesis(graph, "T01", cut), cut


def test_an_unrelated_node_does_not_cut_the_thesis():
    graph = _single_chain_graph()
    graph.add_evidence(_evidence("E-UNRELATED"))
    assert cut_blocks_thesis(graph, "T01", ["E-UNRELATED"]) is False


def test_cut_sets_are_empty_when_the_thesis_is_already_blocked():
    graph = _single_chain_graph()
    deactivate_evidence(graph, "E01", "来源撤回")
    graph.propagate_state()
    report = build_fragility_report(graph, ["T01"])
    assert report.minimal_cut_sets == []
    assert "thesis_already_blocked:T01" in report.warnings


def test_cut_sets_are_empty_for_a_supporting_only_thesis():
    graph = build_graph(
        "run-support-only",
        claims=[_fact("C01", "a", evidence_ids=("E01",)), _fact("C02", "b", evidence_ids=("E02",))],
        theses=[_thesis("T01", ("C01", "C02"), {"C01": ROLE_SUPPORTING, "C02": ROLE_SUPPORTING})],
        evidence_index=[_evidence("E01"), _evidence("E02")],
    )
    report = build_fragility_report(graph, ["T01"])
    assert report.minimal_cut_sets == []
    assert "thesis_has_no_required_dependency:T01" in report.warnings
    assert report.status == STATUS_UNAVAILABLE


def test_cut_sets_are_truncated_and_flagged_when_the_budget_is_small():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"], max_sets=2)
    assert len(report.minimal_cut_sets) == 2
    assert report.cut_sets_truncated is True
    assert report.status == STATUS_PARTIAL


def test_default_budget_is_wide_enough_for_the_real_corpus():
    assert DEFAULT_MAX_SETS >= 100


def test_cycle_is_rejected_before_analysis():
    with pytest.raises(GraphCycleError):
        build_graph(
            "run-cycle",
            claims=[
                Claim("A", CLAIM_INFERENCE, "a", derived_from=("B",)),
                Claim("B", CLAIM_INFERENCE, "b", derived_from=("A",)),
            ],
            theses=[_thesis("T01", ("A",))],
        )


def test_simulate_cut_leaves_the_original_graph_untouched():
    graph = _single_chain_graph()
    before = graph.to_dict()
    simulate_cut(graph, ["E01"])
    assert graph.to_dict() == before


# --------------------------------------------------------------------------- #
# §14 D. Numeric fragility
# --------------------------------------------------------------------------- #

def _margin_threshold(**overrides) -> FragilityThreshold:
    payload = dict(
        node_id="CAL1",
        metric="gross_margin",
        comparison=">=",
        threshold=29.4,
        unit="%",
        source_type=THRESHOLD_PUBLIC_ASSUMPTION,
        source_ref="CAL1",
    )
    payload.update(overrides)
    return FragilityThreshold(**payload)


def test_numeric_distance_matches_the_cards_example():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold()])
    assert records[0]["evaluable"] is True
    assert records[0]["distance_to_invalidation"] == pytest.approx(1.8)
    assert records[0]["violated"] is False


def test_at_threshold_marks_zero_distance_and_the_flag():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(threshold=31.2)])
    assert records[0]["at_threshold"] is True
    assert records[0]["distance_to_invalidation"] == 0.0


def test_violated_when_the_condition_no_longer_holds():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(threshold=40.0)])
    assert records[0]["violated"] is True
    assert records[0]["distance_to_invalidation"] < 0


def test_less_than_comparison_flips_the_sign():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(comparison="<=", threshold=40.0)])
    assert records[0]["distance_to_invalidation"] == pytest.approx(8.8)


def test_percent_aliases_are_compatible_units():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(unit="PERCENT")])
    assert records[0]["evaluable"] is True
    assert records[0]["unit_verified"] is True


def test_a_real_unit_mismatch_is_not_evaluated():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(unit="元")])
    assert records[0]["evaluable"] is False
    assert records[0]["eval_reason"] == "unit_mismatch"
    assert records[0]["distance_to_invalidation"] is None


def test_source_type_outside_the_whitelist_cannot_be_constructed():
    with pytest.raises(FragilityError) as excinfo:
        _margin_threshold(source_type="llm_guess")
    assert "白名单" in str(excinfo.value)


def test_threshold_without_a_source_ref_cannot_be_constructed():
    with pytest.raises(FragilityError):
        _margin_threshold(source_ref="")


def test_unknown_comparison_cannot_be_constructed():
    with pytest.raises(FragilityError):
        _margin_threshold(comparison="≈")


def test_current_value_override_without_a_source_is_rejected():
    with pytest.raises(FragilityError):
        _margin_threshold(current_value=31.2, current_value_source="")


def test_unresolvable_source_ref_is_rejected_and_recorded():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"], [_margin_threshold(source_ref="GHOST")])
    assert report.numeric_thresholds == []
    assert report.rejected_thresholds[0]["reason"] == "unresolvable_source_ref"
    assert report.status == STATUS_PARTIAL


def test_claim_node_without_an_explicit_value_is_not_evaluated():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(graph, [_margin_threshold(node_id="C01")])
    assert records[0]["evaluable"] is False
    assert records[0]["eval_reason"] == "current_value_unavailable"


def test_claim_node_with_an_explicit_value_is_evaluated():
    graph = _single_chain_graph()
    records = compute_numeric_fragility(
        graph,
        [_margin_threshold(node_id="C01", current_value=31.2, current_value_source="analyst:note-7")],
    )
    assert records[0]["evaluable"] is True
    assert records[0]["resolved_current_value_source"] == "analyst:note-7"


def test_analyst_input_and_model_constraint_are_accepted_sources():
    graph = _single_chain_graph()
    for source_type in (THRESHOLD_ANALYST_INPUT, THRESHOLD_MODEL_CONSTRAINT):
        records = compute_numeric_fragility(graph, [_margin_threshold(source_type=source_type)])
        assert records[0]["source_resolved"] is True


def test_no_thresholds_yields_an_empty_array_and_a_warning():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"])
    assert report.numeric_thresholds == []
    assert "numeric_fragility_skipped:no_thresholds_declared" in report.warnings


def test_inactive_evidence_is_reported_as_inactive():
    graph = _single_chain_graph()
    deactivate_evidence(graph, "E01", "来源撤回")
    graph.propagate_state()
    records = compute_numeric_fragility(
        graph, [_margin_threshold(node_id="E01", unit="元")]
    )
    assert records[0]["node_active"] is False


# --------------------------------------------------------------------------- #
# §14 E. Assumptions
# --------------------------------------------------------------------------- #

class _AssumptionRegistry:
    """CARD-08 registry, duck-typed on ``has`` / ``get``."""

    def __init__(self, items):
        self._items = dict(items)
        self._sourced = {key: getattr(value, "source", "") for key, value in self._items.items()}

    def has(self, assumption_id: str) -> bool:
        return assumption_id in self._items

    def get(self, assumption_id: str):
        return self._items[assumption_id]


class _Assumption:
    def __init__(self, assumption_id: str, source: str):
        self.assumption_id = assumption_id
        self.value = 1.0
        self.source = source

    @property
    def grade(self) -> str:
        return "assumption" if self.source.startswith("assumption:") else "sourced"

    @property
    def sourced(self) -> bool:
        return self.grade == "sourced"


def _opinion_graph():
    return build_graph(
        "run-opinion",
        claims=[
            Claim("C01", CLAIM_FACT, "营收 31 亿", evidence_ids=("E01",)),
            Claim(
                "C02",
                "opinion",
                "估值仍有提升空间",
                assumption_ids=("assumption:growth",),
            ),
        ],
        theses=[_thesis("T01", ("C01",))],
        evidence_index=[_evidence("E01")],
    )


def test_referenced_assumptions_without_a_registry_are_unverified():
    graph = _opinion_graph()
    records = identify_fragile_assumptions(graph)
    assert records == [
        {
            "assumption_id": "assumption:growth",
            "claims": ["C02"],
            "status": "unverified",
            "grade": "",
            "reason": "no_assumption_registry",
            "value": None,
            "source": "",
        }
    ]


def test_assumption_grade_is_reported_as_fragile():
    graph = _opinion_graph()
    registry = _AssumptionRegistry({"assumption:growth": _Assumption("assumption:growth", "assumption:录入")})
    records = identify_fragile_assumptions(graph, registry)
    assert records[0]["status"] == "assumption_grade"
    assert records[0]["reason"] == "no_sourced_basis"


def test_sourced_assumption_is_excluded_from_the_unresolved_list():
    graph = _opinion_graph()
    registry = _AssumptionRegistry({"assumption:growth": _Assumption("assumption:growth", "E01")})
    report = build_fragility_report(graph, ["T01"], assumptions=registry)
    assert report.unresolved_assumptions == []
    assert "assumptions_sourced:1" in report.warnings


def test_registered_id_that_is_missing_is_reported_as_unresolved():
    graph = _opinion_graph()
    records = identify_fragile_assumptions(graph, _AssumptionRegistry({}))
    assert records[0]["status"] == "unresolved"
    assert records[0]["reason"] == "not_registered"


def test_unresolved_assumptions_key_is_always_present():
    graph = _opinion_graph()
    report = build_fragility_report(graph, ["T01"])
    assert "unresolved_assumptions" in report.to_dict()


# --------------------------------------------------------------------------- #
# §14 F. Monitoring plans
# --------------------------------------------------------------------------- #

def test_monitoring_plan_has_every_card_field():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"])
    plan = report.monitoring_metrics[0]
    for key in (
        "dependency",
        "observable_kpi_event",
        "trigger_condition",
        "affected_claims",
        "revalidation_action",
    ):
        assert key in plan, key
    assert plan["revalidation_action"] == "revalidate_claim_and_refresh_thesis"
    assert plan["trigger_condition"]


def test_monitoring_plan_uses_a_numeric_trigger_when_a_threshold_exists():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"], [_margin_threshold()])
    plan = next(item for item in report.monitoring_metrics if item["dependency"] == "CAL1")
    assert plan["trigger_kind"] == "numeric"
    assert "29.4" in plan["trigger_condition"]
    assert plan["observable_kpi_event"] == "recompute:gross_margin:CAL1"


def test_monitoring_plan_falls_back_to_a_structural_trigger():
    graph = _single_chain_graph()
    report = build_fragility_report(graph, ["T01"])
    plan = next(item for item in report.monitoring_metrics if item["dependency"] == "C01")
    assert plan["trigger_kind"] == "structural"
    assert "BLOCKED" in plan["trigger_condition"]


def test_monitoring_plan_is_unmapped_when_the_node_declares_no_metric():
    graph = build_graph(
        "run-nometric",
        claims=[
            Claim(
                "C01",
                CLAIM_INFERENCE,
                "盈利质量下降",
                derived_from=("C02",),
            ),
            _fact("C02", "营收 31 亿", evidence_ids=("E01",)),
        ],
        theses=[_thesis("T01", ("C01",))],
        evidence_index=[_evidence("E01")],
    )
    report = build_fragility_report(graph, ["T01"])
    plan = next(item for item in report.monitoring_metrics if item["dependency"] == "C01")
    assert plan["mapped"] is False
    assert plan["unmapped_reason"] == "claim_declares_no_metric"
    assert any(item.startswith("monitoring_unmapped:C01") for item in report.warnings)


def test_monitoring_plan_lists_the_affected_claims():
    graph = _single_chain_graph()
    plans = generate_monitoring_plans(graph, build_fragility_report(graph, ["T01"]))
    evidence_plan = next(item for item in plans if item.dependency == "E01")
    assert list(evidence_plan.affected_claims) == ["C01"]


# --------------------------------------------------------------------------- #
# §14 G. Thesis sources
# --------------------------------------------------------------------------- #

def test_declared_thesis_is_validated_against_the_graph():
    graph = _single_chain_graph()
    with pytest.raises(FragilityError) as excinfo:
        theses_from_declaration(
            graph,
            [{"thesis_id": "T9", "statement": "s", "claim_ids": ["C-NOPE"]}],
        )
    assert "图上不存在" in str(excinfo.value)


def test_declared_thesis_rejects_a_duplicate_id():
    graph = _single_chain_graph()
    with pytest.raises(FragilityError):
        theses_from_declaration(
            graph,
            [
                {"thesis_id": "T9", "statement": "s", "claim_ids": ["C01"]},
                {"thesis_id": "T9", "statement": "s", "claim_ids": ["C01"]},
            ],
        )


def test_declared_thesis_requires_a_statement():
    graph = _single_chain_graph()
    with pytest.raises(FragilityError):
        theses_from_declaration(graph, [{"thesis_id": "T9", "claim_ids": ["C01"]}])


def test_declared_thesis_rejects_roles_for_undeclared_claims():
    graph = _single_chain_graph()
    with pytest.raises(FragilityError):
        theses_from_declaration(
            graph,
            [
                {
                    "thesis_id": "T9",
                    "statement": "s",
                    "claim_ids": ["C01"],
                    "dependency_roles": {"C02": ROLE_REQUIRED},
                }
            ],
        )


def test_inferences_become_theses_with_the_inference_as_the_required_member():
    graph = build_graph(
        "run-inference",
        claims=[
            _fact("C01", "营收 31 亿", evidence_ids=("E01",)),
            Claim("I1", CLAIM_INFERENCE, "盈利质量下降", derived_from=("C01",)),
        ],
        evidence_index=[_evidence("E01")],
    )
    theses = theses_from_inferences(graph)
    assert [(item.thesis_id, item.claim_ids) for item in theses] == [
        ("thesis_inference_I1", ("I1",))
    ]
    assert theses[0].role_map == {"I1": ROLE_REQUIRED}


def test_an_unsupported_inference_still_becomes_a_thesis_and_is_reported():
    graph = build_graph(
        "run-orphan-inference",
        claims=[Claim("I1", CLAIM_INFERENCE, "无来源的推论")],
    )
    theses, source = resolve_theses(graph)
    assert source == THESIS_SOURCE_INFERENCES
    report = build_fragility_report(
        graph, [item.thesis_id for item in theses], thesis_source=source
    )
    assert report.warnings[0] == "thesis_already_blocked:thesis_inference_I1"
    assert report.minimal_cut_sets == []


def test_resolve_theses_prefers_the_declaration():
    graph = _single_chain_graph()
    graph.add_thesis(_thesis("T-REGISTERED", ("C01",)))
    theses, source = resolve_theses(
        graph, [{"thesis_id": "T9", "statement": "s", "claim_ids": ["C01"]}]
    )
    assert source == THESIS_SOURCE_DECLARED
    assert [item.thesis_id for item in theses] == ["T9"]
    assert "T9" in graph.theses


def test_resolve_theses_falls_back_to_registered_theses():
    graph = _fan_out_graph()
    theses, source = resolve_theses(graph)
    assert source == THESIS_SOURCE_DECLARED
    assert [item.thesis_id for item in theses] == ["T01"]


def test_resolve_theses_falls_back_to_inferences():
    graph = build_graph(
        "run-inference-only",
        claims=[
            _fact("C01", "营收 31 亿", evidence_ids=("E01",)),
            Claim("I1", CLAIM_INFERENCE, "盈利质量下降", derived_from=("C01",)),
        ],
        evidence_index=[_evidence("E01")],
    )
    theses, source = resolve_theses(graph)
    assert source == THESIS_SOURCE_INFERENCES
    assert [item.thesis_id for item in theses] == ["thesis_inference_I1"]


def test_resolve_theses_reports_none_when_there_is_nothing_to_analyse():
    graph = build_graph("run-empty", claims=[_fact("C01", "a", evidence_ids=("E01",))], evidence_index=[_evidence("E01")])
    theses, source = resolve_theses(graph)
    assert theses == []
    assert source == THESIS_SOURCE_NONE


# --------------------------------------------------------------------------- #
# §14 H. Report assembly
# --------------------------------------------------------------------------- #

def test_report_is_unavailable_without_any_thesis():
    graph = build_graph("run-nothesis", claims=[_fact("C01", "a", evidence_ids=("E01",))], evidence_index=[_evidence("E01")])
    report = build_fragility_report(graph, [])
    assert report.status == STATUS_UNAVAILABLE
    assert report.unavailable_reason == "no_theses_declared"


def test_report_is_unavailable_on_an_empty_graph():
    report = build_fragility_report(build_graph("run-empty"), [])
    assert report.status == STATUS_UNAVAILABLE
    assert report.unavailable_reason == "graph_has_no_claims"


def test_unknown_thesis_id_raises():
    graph = _fan_out_graph()
    with pytest.raises(FragilityError):
        build_fragility_report(graph, ["NOPE"])


def test_verification_view_is_unavailable_without_passports():
    graph = _fan_out_graph()
    report = build_fragility_report(graph, ["T01"])
    assert report.verification["verification_available"] is False


def test_verification_view_rolls_up_with_card23_precedence():
    graph = _fan_out_graph()
    report = build_fragility_report(
        graph,
        ["T01"],
        verification_statuses={"C01": "VERIFIED", "C02": "STALE", "E03": "BLOCKED"},
    )
    view = report.verification["theses"]["T01"]
    assert view["worst_status"] == "STALE"
    assert view["claims"] == {"C01": "VERIFIED", "C02": "STALE"}
    assert view["blocked_by_passport"] == []


def test_passport_block_marks_the_claim_in_the_view():
    graph = _fan_out_graph()
    report = build_fragility_report(
        graph, ["T01"], verification_statuses={"C01": "BLOCKED"}
    )
    view = report.verification["theses"]["T01"]
    assert view["blocked_by_passport"] == ["C01"]
    assert view["unverified_claims"] == ["C02"]


def test_required_dependencies_are_edge_records():
    graph = _fan_out_graph()
    report = build_fragility_report(graph, ["T01"])
    edges = {(item["source"], item["target"]) for item in report.required_dependencies}
    assert edges == {("C01", "E01"), ("C01", "E02"), ("C02", "E03")}
    assert all(item["thesis_id"] == "T01" for item in report.required_dependencies)


# --------------------------------------------------------------------------- #
# §9 Zero-LLM boundary (source-level assertion)
# --------------------------------------------------------------------------- #

def test_fragility_package_is_model_free():
    problems: List[str] = []
    for path in sorted(FRAGILITY_PACKAGE.glob("*.py")):
        problems.extend(check_source_is_model_free(path.read_text(encoding="utf-8")))
        problems.extend(check_source_is_model_free(_strip_docstrings(path.read_text(encoding="utf-8"))))
    assert problems == []


def test_fragility_validator_is_model_free():
    path = Path(__file__).parents[1] / "src" / "validation" / "fragility_validator.py"
    assert check_source_is_model_free(path.read_text(encoding="utf-8")) == []


def _strip_docstrings(source: str) -> str:
    """Drop triple-quoted blocks so prose about "LLM" cannot trip the scanner."""
    out: List[str] = []
    inside = False
    for line in source.splitlines():
        if line.count('"""') % 2 == 1:
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# §13 Validator
# --------------------------------------------------------------------------- #

def _valid_report(graph, **kwargs):
    return build_fragility_report(graph, ["T01"], **kwargs)


def test_validator_accepts_a_freshly_built_report():
    graph = _single_chain_graph()
    result = validate_fragility(graph, _valid_report(graph, thresholds=[_margin_threshold()]))
    assert result.valid is True, result.errors


def test_validator_rejects_a_cut_set_that_does_not_block():
    graph = _fan_out_graph()
    graph.add_evidence(_evidence("E-UNRELATED"))
    report = _valid_report(graph)
    report.cut_sets_by_thesis["T01"] = [["E-UNRELATED"]]
    report.minimal_cut_sets = [["E-UNRELATED"]]
    result = validate_fragility(graph, report)
    assert result.valid is False
    assert any(item.startswith("cut_set_does_not_block") for item in result.errors)


def test_validator_rejects_a_non_minimal_cut_set():
    graph = _fan_out_graph()
    report = _valid_report(graph)
    report.cut_sets_by_thesis["T01"] = [["C01", "C02"]]
    result = validate_fragility(graph, report)
    assert result.valid is False
    assert any(item.startswith("cut_set_not_minimal") for item in result.errors)


def test_validator_rejects_an_incomplete_cut_set_universe():
    graph = _fan_out_graph()
    report = _valid_report(graph)
    report.cut_sets_by_thesis["T01"] = report.cut_sets_by_thesis["T01"][:-1]
    result = validate_fragility(graph, report)
    assert result.valid is False
    assert any(item.startswith("cut_set_universe_mismatch") for item in result.errors)


def test_validator_rejects_a_cut_set_node_that_does_not_resolve():
    graph = _fan_out_graph()
    report = _valid_report(graph)
    report.cut_sets_by_thesis["T01"] = [["GHOST"]]
    result = validate_fragility(graph, report)
    assert any(item.startswith("cut_set_node_unresolved") for item in result.errors)


def test_validator_rejects_a_missing_unresolved_assumptions_key():
    graph = _single_chain_graph()
    report = _valid_report(graph)
    payload = report.to_dict()
    payload.pop("unresolved_assumptions")

    class _Fake:
        def to_dict(self):
            return payload

        cut_sets_by_thesis = report.cut_sets_by_thesis
        critical_dependencies = report.critical_dependencies
        numeric_thresholds = report.numeric_thresholds
        rejected_thresholds = report.rejected_thresholds
        monitoring_metrics = report.monitoring_metrics
        status = report.status
        unavailable_reason = report.unavailable_reason
        pruned = report.pruned
        cut_sets_truncated = report.cut_sets_truncated
        thesis_id = report.thesis_id
        thesis_ids = report.thesis_ids

    result = validate_fragility(graph, _Fake())
    assert "unresolved_assumptions_key_missing" in result.errors


def test_validator_rejects_an_unknown_threshold_source_type():
    graph = _single_chain_graph()
    report = _valid_report(graph, thresholds=[_margin_threshold()])
    report.numeric_thresholds[0]["source_type"] = "llm_guess"
    result = validate_fragility(graph, report)
    assert any(item.startswith("threshold_source_type_not_whitelisted") for item in result.errors)


def test_validator_rejects_a_distance_that_does_not_recompute():
    graph = _single_chain_graph()
    report = _valid_report(graph, thresholds=[_margin_threshold()])
    report.numeric_thresholds[0]["distance_to_invalidation"] = 99.0
    result = validate_fragility(graph, report)
    assert any(item.startswith("threshold_distance_not_reproducible") for item in result.errors)


def test_validator_rejects_an_unevaluable_threshold_with_a_distance():
    graph = _single_chain_graph()
    # 指到 claim 上、又没有显式当前值 → 保持不可评估（而不是被拒收）。
    report = _valid_report(graph, thresholds=[_margin_threshold(node_id="C01")])
    assert report.numeric_thresholds[0]["evaluable"] is False
    report.numeric_thresholds[0]["distance_to_invalidation"] = 1.0
    result = validate_fragility(graph, report)
    assert any(item.startswith("threshold_unevaluable_but_has_distance") for item in result.errors)


def test_validator_rejects_a_single_point_without_a_singleton_cut():
    graph = _single_chain_graph()
    report = _valid_report(graph)
    report.critical_dependencies[0]["is_single_point"] = True
    report.critical_dependencies[0]["single_point_for"] = ["T01"]
    report.cut_sets_by_thesis["T01"] = [["CAL1"]]
    result = validate_fragility(graph, report)
    assert any(item.startswith("single_point_without_singleton_cut") for item in result.errors)


def test_validator_rejects_an_unavailable_report_without_a_reason():
    graph = _fan_out_graph()
    report = _valid_report(graph)
    report.status = STATUS_UNAVAILABLE
    report.unavailable_reason = ""
    result = validate_fragility(graph, report)
    assert "unavailable_without_reason" in result.errors


def test_validator_warns_when_the_numeric_layer_is_absent():
    graph = _single_chain_graph()
    result = validate_fragility(graph, _valid_report(graph))
    assert "numeric_fragility_absent" in result.warnings


def test_validator_corpus_invariant_over_every_thesis():
    graph = _fan_out_graph()
    reports = [build_fragility_report(graph, ["T01"])]
    graph.add_thesis(_thesis("T02", ("C02",)))
    reports.append(build_fragility_report(graph, ["T02"]))
    result = validate_fragility_corpus(graph, reports)
    assert result.valid is True, result.errors


# --------------------------------------------------------------------------- #
# §15 Integration: CARD-01 → 02 → 03 → 09 → 24
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def _chain(registry: DocumentRegistry):
    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(registry.get_facts(parsed.document.document_id), parsed.document)
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=parsed.document.document_id)
    register_normalized_evidence(parsed.document, normalization, ledger=ledger)
    graph = build_graph_from_analysis(
        analysis,
        run_id="run-fragility",
        document_id=parsed.document.document_id,
        ledger=ledger,
        jumps=normalization_jumps(parsed.document, normalization),
    )
    return parsed, analysis, graph


def test_integration_report_matches_manual_expectation(registry):
    parsed, _analysis, graph = _chain(registry)
    claim_ids = sorted(graph.claims)
    theses = theses_from_declaration(
        graph,
        [
            {
                "thesis_id": "T-PROFIT",
                "statement": "盈利改善具有持续性",
                "claim_ids": [
                    "claim_fact_gross_margin_2025FY",
                    "claim_fact_net_margin_2025FY",
                    "claim_fact_revenue_yoy_2025FY",
                ],
            }
        ],
    )
    graph.add_thesis(theses[0])
    report = build_fragility_report(graph, ["T-PROFIT"], thesis_source=THESIS_SOURCE_DECLARED)

    assert report.status == "ok"
    # 每个必需祖先都是单点割集：这条逻辑没有冗余路径（见 engine 模块头）。
    cut_nodes = {cut[0] for cut in report.minimal_cut_sets}
    assert {"claim_fact_gross_margin_2025FY", "claim_fact_net_margin_2025FY"} <= cut_nodes
    # 营业收入 2025FY 同时支撑毛利率、净利率与收入同比三条结论 → 它才是
    # 「一击同时打断多条依赖」的那个节点（is_single_point 的判据）。
    revenue = sorted(node for node in cut_nodes if "revenue_2025FY" in node)
    assert len(revenue) == 1, cut_nodes
    single_points = {item["node_id"] for item in report.single_points}
    assert revenue[0] in single_points
    assert single_points <= cut_nodes
    for cut in report.minimal_cut_sets:
        assert cut_blocks_thesis(graph, "T-PROFIT", cut)
    assert claim_ids
    result = validate_fragility(graph, report)
    assert result.valid is True, result.errors


def test_integration_refresh_after_evidence_is_withdrawn(registry):
    _parsed, _analysis, graph = _chain(registry)
    graph.add_thesis(
        theses_from_declaration(
            graph,
            [
                {
                    "thesis_id": "T-PROFIT",
                    "statement": "盈利改善具有持续性",
                    "claim_ids": ["claim_fact_gross_margin_2025FY"],
                }
            ],
        )[0]
    )
    before = build_fragility_report(graph, ["T-PROFIT"])
    assert before.minimal_cut_sets

    victim = before.cut_sets_by_thesis["T-PROFIT"][0][0]
    if victim in graph.evidence_index:
        deactivate_evidence(graph, victim, "来源撤回")
    else:
        graph.claims.pop(victim)
    graph.propagate_state()

    after = build_fragility_report(graph, ["T-PROFIT"])
    assert graph.theses["T-PROFIT"].status == THESIS_BLOCKED
    assert after.minimal_cut_sets == []
    assert any(item.startswith("thesis_already_blocked") for item in after.warnings)


def test_integration_numeric_fragility_on_a_real_calculation(registry):
    _parsed, _analysis, graph = _chain(registry)
    calculation = next(
        node for node in graph.calculations.values() if node.metric == "gross_margin"
    )
    graph.add_thesis(
        theses_from_declaration(
            graph,
            [
                {
                    "thesis_id": "T-MARGIN",
                    "statement": "毛利率维持在 30% 以上",
                    "claim_ids": ["claim_fact_gross_margin_2025FY"],
                }
            ],
        )[0]
    )
    threshold = FragilityThreshold(
        node_id=calculation.calculation_id,
        metric="gross_margin",
        comparison=">=",
        threshold=30.0,
        unit="%",
        source_type=THRESHOLD_PUBLIC_ASSUMPTION,
        source_ref=next(iter(sorted(calculation.evidence_ids))),
    )
    report = build_fragility_report(graph, ["T-MARGIN"], [threshold])
    assert report.numeric_thresholds[0]["evaluable"] is True
    assert report.numeric_thresholds[0]["distance_to_invalidation"] > 0
    plan = next(
        item for item in report.monitoring_metrics if item["dependency"] == calculation.calculation_id
    )
    assert plan["trigger_kind"] == "numeric"
    assert validate_fragility(graph, report).valid is True


def test_integration_passports_flow_into_the_report(registry):
    _parsed, _analysis, graph = _chain(registry)
    graph.add_thesis(
        theses_from_declaration(
            graph,
            [
                {
                    "thesis_id": "T-PROFIT",
                    "statement": "盈利改善具有持续性",
                    "claim_ids": ["claim_fact_gross_margin_2025FY"],
                }
            ],
        )[0]
    )
    report = build_fragility_report(
        graph,
        ["T-PROFIT"],
        verification_statuses={"claim_fact_gross_margin_2025FY": "VERIFIED"},
    )
    assert report.verification["verification_available"] is True
    assert report.verification["theses"]["T-PROFIT"]["worst_status"] == "VERIFIED"


# --------------------------------------------------------------------------- #
# §8 Tool contract
# --------------------------------------------------------------------------- #

def _tool(registry: DocumentRegistry):
    from src.agents.tools.cn_tools import AnalyzeCnThesisFragilityTool

    return AnalyzeCnThesisFragilityTool(registry)


def _run_tool(registry: DocumentRegistry, **kwargs) -> Dict:
    return json.loads(asyncio.run(_tool(registry).execute(**kwargs)))


def test_tool_requires_a_document_id(registry):
    payload = _run_tool(registry)
    assert payload["error_code"] == "fragility_missing_argument"


def test_tool_rejects_an_unknown_document(registry):
    payload = _run_tool(registry, document_id="DOC-NOPE")
    assert payload["error_code"] == "document_not_found"


def test_tool_reports_unavailable_when_no_thesis_can_be_resolved(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(registry, document_id=parsed.document.document_id, include_verification=False)
    assert payload["status"] == "unavailable"
    assert payload["unavailable_reason"] == "no_theses_declared"
    assert payload["thesis_source"] == THESIS_SOURCE_NONE


def test_tool_returns_a_validated_report(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        theses=[
            {
                "thesis_id": "T-PROFIT",
                "statement": "盈利改善具有持续性",
                "claim_ids": ["claim_fact_gross_margin_2025FY", "claim_fact_net_margin_2025FY"],
            }
        ],
        include_verification=False,
    )
    assert payload["status"] == "ok"
    assert payload["thesis_source"] == THESIS_SOURCE_DECLARED
    assert payload["validation"]["valid"] is True
    assert payload["fragility_report"]["minimal_cut_sets"]


def test_tool_rejects_an_unknown_thesis_id(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        thesis_id="T-NOPE",
        theses=[{"thesis_id": "T1", "statement": "s", "claim_ids": ["claim_fact_gross_margin_2025FY"]}],
        include_verification=False,
    )
    assert payload["error_code"] == "thesis_not_found"


def test_tool_rejects_a_threshold_with_an_out_of_whitelist_source(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        theses=[{"thesis_id": "T1", "statement": "s", "claim_ids": ["claim_fact_gross_margin_2025FY"]}],
        thresholds=[
            {
                "node_id": "claim_fact_gross_margin_2025FY",
                "metric": "gross_margin",
                "comparison": ">=",
                "threshold": 30.0,
                "unit": "%",
                "source_type": "model_guess",
                "source_ref": "claim_fact_gross_margin_2025FY",
            }
        ],
        include_verification=False,
    )
    assert payload["error_code"] == "fragility_invalid_threshold"


def test_tool_rejects_a_declared_thesis_referencing_a_missing_claim(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        theses=[{"thesis_id": "T1", "statement": "s", "claim_ids": ["C-NOPE"]}],
        include_verification=False,
    )
    assert payload["error_code"] == "fragility_invalid_thesis"


def test_tool_pulls_card23_passport_statuses(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        theses=[{"thesis_id": "T1", "statement": "s", "claim_ids": ["claim_fact_gross_margin_2025FY"]}],
    )
    assert payload["verification_note"].startswith("verified=1")
    view = payload["fragility_report"]["verification"]["theses"]["T1"]
    assert view["claims"]["claim_fact_gross_margin_2025FY"] == "VERIFIED"


def test_tool_can_omit_the_monitoring_plan(registry):
    parsed, _analysis, _graph = _chain(registry)
    payload = _run_tool(
        registry,
        document_id=parsed.document.document_id,
        theses=[{"thesis_id": "T1", "statement": "s", "claim_ids": ["claim_fact_gross_margin_2025FY"]}],
        include_verification=False,
        include_monitoring=False,
    )
    assert payload["fragility_report"]["monitoring_metrics"] == []


def test_tool_is_registered_for_both_snapshot_branches():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = {tool.name for tool in build_cn_snapshot_tools()}
    assert "analyze_cn_thesis_fragility" in names
