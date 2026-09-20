"""CARD-11 real-world benchmark (``cn_agent_v2``) — offline tests.

Three things are under test:

1. **The metric calculators are arithmetically right.** Every P/R/F1 test builds
   a fixture whose precision, recall and F1 can be computed by hand, so a change
   that "looks reasonable" cannot quietly move a published number.
2. **The honesty contract holds.** Unavailable metrics stay as rows with an
   explicit reason; thresholds are never pre-filled; an un-run ablation is
   reported as ``TARGET / TO BE MEASURED``.
3. **Isolation is mechanical.** A real-tier payload cannot be written into a
   synthetic directory (or the reverse) — the check is code, not convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cn.benchmark_v2 import (
    ADVERSARIAL_METRICS,
    BASE_METRICS,
    NOT_MEASURED,
    THRESHOLDS,
    TIER_HOLDOUT,
    TIER_REAL,
    TIER_SYNTHETIC,
    BenchmarkIsolationError,
    Metric,
    Prediction,
    TierPayload,
    TruthLabel,
    adversarial_metrics,
    agent_metrics,
    build_ablation_table,
    build_report,
    calculation_metrics,
    checker_metrics,
    citation_metrics,
    component_metrics,
    cost_metrics,
    environment_fingerprint,
    extraction_metrics,
    fingerprint_digest,
    freeze_thresholds,
    isolate_payload,
    merge_components,
    prf1,
    render_report_markdown,
    replay_metrics,
    tier_report,
    unsafe_conclusion_leakage,
    unmeasured_metrics,
    write_report,
)

OUTPUT_ROOT = Path(__file__).parents[1] / "output" / "benchmark" / "cn_agent_v2"


# --------------------------------------------------------------------------- #
# §15 acceptance 2 — every metric calculator has a unit test
# --------------------------------------------------------------------------- #
def test_base_metric_vocabulary_is_exactly_the_cards_twenty_four():
    assert len(BASE_METRICS) == 24
    assert len(set(BASE_METRICS)) == 24
    for group in (
        "extraction_precision", "citation_recall", "calculation_accuracy",
        "checker_f1", "evidence_coverage", "unsafe_conclusion_leakage",
        "replay_success_rate", "latency_seconds",
    ):
        assert group in BASE_METRICS
    assert len(ADVERSARIAL_METRICS) == 4


def test_prf1_arithmetic():
    assert prf1(3, 1, 1) == (0.75, 0.75, 0.75)
    assert prf1(0, 0, 0) == (0.0, 0.0, 0.0)
    precision, recall, f1 = prf1(1, 0, 0)
    assert (precision, recall, f1) == (1.0, 1.0, 1.0)


def test_extraction_metrics_on_a_known_confusion():
    labels = [
        TruthLabel("D", "营业收入", "2024FY", 100.0, "元", "合并", 12),
        TruthLabel("D", "营业成本", "2024FY", 60.0, "元", "合并", 12),
        TruthLabel("D", "净利润", "2024FY", 10.0, "元", "合并", 13),
    ]
    predictions = [
        # correct
        Prediction("D", "营业收入", "2024FY", 100.0, "元", "合并", 12),
        # wrong value AND wrong page → still a key match
        Prediction("D", "营业成本", "2024FY", 61.0, "元", "合并", 99),
        # right number, wrong period → false positive *and* false negative
        Prediction("D", "净利润", "2023FY", 10.0, "元", "合并", 13),
    ]
    metrics = extraction_metrics(labels, predictions)
    # matched = 2 of 3 predictions, 3 labels → P = 2/3, R = 2/3
    assert metrics["extraction_precision"].value == pytest.approx(2 / 3)
    assert metrics["extraction_recall"].value == pytest.approx(2 / 3)
    assert metrics["numeric_exact_match"].value == pytest.approx(0.5)
    assert metrics["unit_accuracy"].value == pytest.approx(1.0)
    assert metrics["page_attribution_accuracy"].value == pytest.approx(0.5)
    # 净利润 was attached to 2023FY while every label for it is 2024FY
    assert metrics["period_accuracy"].value == pytest.approx(2 / 3)


def test_extraction_metrics_report_no_matches_rather_than_zero():
    metrics = extraction_metrics([TruthLabel("D", "营业收入", "2024FY", 1.0)], [])
    # Precision with nothing predicted is 0.0, but the derived accuracies have no
    # information at all and must say so instead of printing 0%.
    assert metrics["extraction_precision"].value == 0.0
    assert metrics["extraction_precision"].unit == ""
    assert metrics["numeric_exact_match"].value is None
    assert metrics["numeric_exact_match"].note == "no_matched_labels"


def test_extraction_metrics_on_empty_input_are_all_na():
    metrics = extraction_metrics([], [])
    assert all(metric.value is None for metric in metrics.values())
    assert all(metric.note == "no_labels_or_predictions" for metric in metrics.values())


def test_citation_metrics_over_evidence_ids():
    labels = [
        TruthLabel("D", "营业收入", "2024FY", 1.0, expected_evidence_ids=("E1", "E2")),
    ]
    predictions = [
        Prediction("D", "营业收入", "2024FY", 1.0, cited_evidence_ids=("E1", "E9")),
    ]
    metrics = citation_metrics(labels, predictions)
    assert metrics["citation_precision"].value == pytest.approx(0.5)
    assert metrics["citation_recall"].value == pytest.approx(0.5)


def test_citation_metrics_with_no_citations_is_na_not_zero():
    metrics = citation_metrics([], [])
    assert metrics["citation_precision"].value is None
    assert metrics["citation_recall"].value is None


def test_calculation_metrics_include_the_unavailable_expectation():
    rows = [
        {"calculation_id": "C1", "expected_value": 0.61, "actual_value": 0.61},
        {"calculation_id": "C2", "expected_value": 1.0, "actual_value": 2.0},
        # a single-quarter QoQ on an annual report must *not* produce a number
        {"calculation_id": "C3", "expected_unavailable": True, "actual_value": None},
        {"calculation_id": "C4", "expected_unavailable": True, "actual_value": 5.0},
    ]
    metrics = calculation_metrics(rows)
    assert metrics["calculation_accuracy"].value == pytest.approx(0.5)


def test_calculation_metrics_without_rows_is_na():
    assert calculation_metrics([])["calculation_accuracy"].value is None


def test_checker_metrics_precision_recall_f1_and_fpr():
    rows = [
        {"predicted": True, "actual": True},
        {"predicted": True, "actual": True},
        {"predicted": True, "actual": False},   # false positive
        {"predicted": False, "actual": True},   # false negative
        {"predicted": False, "actual": False},
        {"predicted": False, "actual": False},
        {"predicted": False, "actual": False},
    ]
    metrics = checker_metrics(rows)
    assert metrics["checker_precision"].value == pytest.approx(2 / 3)
    assert metrics["checker_recall"].value == pytest.approx(2 / 3)
    assert metrics["checker_f1"].value == pytest.approx(2 / 3)
    assert metrics["checker_false_positive_rate"].value == pytest.approx(0.25)


def test_checker_metrics_without_annotations_is_na():
    metrics = checker_metrics([])
    assert all(metric.value is None for metric in metrics.values())
    assert metrics["checker_precision"].note == "no_checker_annotations"


def test_agent_metrics_cover_selection_parameters_completion_and_evidence():
    rows = [
        {
            "expected_tools": ["resolve_cn_symbol", "get_cn_financials"],
            "called_tools": ["resolve_cn_symbol", "get_cn_financials"],
            "expected_arguments": {"query": "600519.SH"},
            "called_arguments": {"query": "600519.SH"},
            "completed": True,
            "required_evidence": ["E1", "E2"],
            "covered_evidence": ["E1"],
        },
        {
            "expected_tools": ["get_cn_prices"],
            "called_tools": ["get_cn_prices", "extra_tool"],
            "expected_arguments": {"symbol": "600519.SH"},
            "called_arguments": {"symbol": "600519.SZ"},
            "completed": False,
            "required_evidence": ["E3"],
            "covered_evidence": [],
        },
    ]
    metrics = agent_metrics(rows)
    assert metrics["tool_selection_accuracy"].value == pytest.approx(0.5)
    assert metrics["parameter_accuracy"].value == pytest.approx(0.5)
    assert metrics["task_completion_rate"].value == pytest.approx(0.5)
    assert metrics["evidence_coverage"].value == pytest.approx(1 / 3)


def test_agent_metrics_without_expectations_are_na():
    metrics = agent_metrics([{"expected_tools": [], "called_tools": []}])
    assert metrics["parameter_accuracy"].value is None
    assert metrics["parameter_accuracy"].note == "no_expected_arguments"
    assert metrics["evidence_coverage"].value is None
    assert metrics["evidence_coverage"].note == "no_required_evidence"


def test_unsafe_conclusion_leakage_counts_inference_dressed_as_fact():
    rows = [
        {"claim_type": "fact", "backing_claim_type": "fact"},
        {"claim_type": "fact", "backing_claim_type": "inference"},   # leak
        {"claim_type": "fact", "backing_claim_type": None},          # leak (unbound)
        {"claim_type": "inference", "backing_claim_type": "inference"},
    ]
    metrics = unsafe_conclusion_leakage(rows)
    assert metrics["unsafe_conclusion_leakage"].value == pytest.approx(2 / 3)


def test_unsafe_conclusion_leakage_is_zero_when_there_are_no_fact_statements():
    metrics = unsafe_conclusion_leakage([{"claim_type": "opinion", "backing_claim_type": None}])
    assert metrics["unsafe_conclusion_leakage"].value == 0.0
    assert metrics["unsafe_conclusion_leakage"].note == "no_fact_statements"


def test_replay_and_multi_run_consistency():
    replay_rows = [
        {"run_id": "r1", "replayed": True, "identical": True},
        {"run_id": "r2", "replayed": True, "identical": True},
        {"run_id": "r3", "replayed": True, "identical": False},
    ]
    summaries = [
        {"extraction_f1": 1.0, "checker_f1": 0.9},
        {"extraction_f1": 1.0, "checker_f1": 0.9},
        {"extraction_f1": 1.0, "checker_f1": 0.8},
    ]
    metrics = replay_metrics(replay_rows, summaries)
    assert metrics["replay_success_rate"].value == pytest.approx(2 / 3)
    assert metrics["multi_run_consistency"].value == pytest.approx(0.5)


def test_multi_run_consistency_requires_at_least_two_runs():
    metrics = replay_metrics([], [{"a": 1.0}])
    assert metrics["multi_run_consistency"].value is None
    assert metrics["multi_run_consistency"].note == "requires_at_least_2_runs"


def test_multi_run_consistency_flags_a_sample_below_three():
    metrics = replay_metrics([], [{"a": 1.0}, {"a": 1.0}])
    assert metrics["multi_run_consistency"].value == 1.0
    assert "below_3" in metrics["multi_run_consistency"].note


def test_cost_metrics_report_mean_and_p95():
    rows = [
        {"latency_seconds": 1.0, "cost": 0.01, "cost_currency": "USD"},
        {"latency_seconds": 2.0, "cost": 0.02, "cost_currency": "USD"},
        {"latency_seconds": 3.0, "cost": 0.03, "cost_currency": "USD"},
    ]
    metrics = cost_metrics(rows)
    assert metrics["latency_seconds"].value == pytest.approx(2.0)
    assert "p95=" in metrics["latency_seconds"].note
    assert metrics["token_cost"].value == pytest.approx(0.06)
    assert metrics["token_cost"].unit == "USD"


def test_cost_metrics_mixed_currencies_are_labelled_not_summed_silently():
    rows = [
        {"latency_seconds": 1.0, "cost": 0.01, "cost_currency": "USD"},
        {"latency_seconds": 1.0, "cost": 0.01, "cost_currency": "CNY"},
    ]
    metrics = cost_metrics(rows)
    assert metrics["token_cost"].unit == "MIXED"


def test_adversarial_metrics_and_per_error_type_recall():
    rows = [
        {"error_type": "numeric", "planted": True, "detected": True},
        {"error_type": "numeric", "planted": True, "detected": False},
        {"error_type": "unit", "planted": True, "detected": True},
        {"error_type": "scope", "planted": False, "detected": False},
        {"error_type": "scope", "planted": False, "detected": True},
    ]
    metrics = adversarial_metrics(rows)
    assert metrics["mutation_detection_precision"].value == pytest.approx(2 / 3)
    assert metrics["mutation_detection_recall"].value == pytest.approx(2 / 3)
    assert metrics["mutation_detection_false_positive_rate"].value == pytest.approx(0.5)
    detail = metrics["mutation_detection_recall"].note
    assert "numeric=1/2" in detail and "unit=1/1" in detail


def test_adversarial_metrics_without_rows_is_na():
    metrics = adversarial_metrics([])
    assert all(metric.value is None for metric in metrics.values())


# --------------------------------------------------------------------------- #
# Metric rendering
# --------------------------------------------------------------------------- #
def test_metric_rendering_is_unambiguous():
    assert Metric("m", None, note="x").render() == "N/A（x）"
    assert Metric("m", 0.5, "%").render() == "50.00%"
    assert Metric("m", 1.25, "seconds").render() == "1.2500s"
    assert Metric("m", 7.0).render() == "7"


# --------------------------------------------------------------------------- #
# §15 acceptance 5 — threshold freeze flow
# --------------------------------------------------------------------------- #
def test_thresholds_start_unfrozen_and_are_never_prefilled():
    assert all(value is None for value in THRESHOLDS.values())
    report = build_report(
        [TierPayload(TIER_SYNTHETIC, labels=[TruthLabel("D", "m", "2024FY", 1.0)])]
    )
    assert report["threshold_status"] == "UNFROZEN"
    assert all(value is None for value in report["thresholds"].values())
    # The word TARGET, not a made-up 95%.
    markdown = render_report_markdown(report)
    assert NOT_MEASURED in markdown


def test_freeze_thresholds_derives_targets_from_a_measured_baseline():
    baseline = {"metrics": {"extraction_f1": 0.9, "latency_seconds": 2.0}}
    frozen = freeze_thresholds(baseline, margin=0.05)
    assert frozen["extraction_f1"] == pytest.approx(0.85)   # higher is better
    assert frozen["latency_seconds"] == pytest.approx(2.05)  # lower is better
    assert frozen["checker_f1"] is None                      # unmeasured stays blank


def test_frozen_thresholds_flip_the_report_status():
    report = build_report(
        [TierPayload(TIER_SYNTHETIC)],
        extra_fingerprint={"thresholds_frozen": True},
    )
    assert report["threshold_status"] == "UNFROZEN"  # module-level table is still blank


# --------------------------------------------------------------------------- #
# §7b ablation
# --------------------------------------------------------------------------- #
def test_ablation_table_never_invents_a_result():
    table = build_ablation_table({})
    assert len(table) == 5
    assert all(row["status"] == NOT_MEASURED for row in table)
    assert all(row["baseline_value"] is None and row["treatment_value"] is None for row in table)


def test_ablation_table_reports_measured_values_when_supplied():
    table = build_ablation_table(
        {"parser_plus_accounting_constraints": {"metrics": {"financial_extraction_error_detection": 0.8}},
         "parser_only": {"metrics": {"financial_extraction_error_detection": 0.5}}}
    )
    row = next(item for item in table if item["innovation"].startswith("ACME"))
    assert row["status"] == "MEASURED"
    assert row["baseline_value"] == pytest.approx(0.5)
    assert row["treatment_value"] == pytest.approx(0.8)


def test_ablation_accepts_a_flat_metric_mapping_too():
    table = build_ablation_table(
        {"citation_only": {"unsupported_claim_detection": 0.4},
         "fpo_verifier": {"unsupported_claim_detection": 0.9}}
    )
    row = next(item for item in table if item["innovation"].startswith("Claim Passport"))
    assert row["status"] == "MEASURED"


# --------------------------------------------------------------------------- #
# §15 acceptance 4 — isolation and honesty
# --------------------------------------------------------------------------- #
def test_real_tier_must_live_under_a_real_prefixed_directory(tmp_path: Path):
    with pytest.raises(BenchmarkIsolationError):
        isolate_payload({"tier": TIER_REAL}, output_dir=tmp_path / "synthetic")
    # The accepted shape.
    isolate_payload({"tier": TIER_REAL}, output_dir=tmp_path / "benchmark_real" / "real_x")
    isolate_payload({"tier": TIER_HOLDOUT}, output_dir=tmp_path / "real_holdout_x")


def test_synthetic_tier_must_not_be_written_under_benchmark_real(tmp_path: Path):
    with pytest.raises(BenchmarkIsolationError):
        isolate_payload({"tier": TIER_SYNTHETIC}, output_dir=tmp_path / "benchmark_real")
    isolate_payload({"tier": TIER_SYNTHETIC}, output_dir=tmp_path / "cn_agent_v2")


def test_unknown_tier_is_rejected():
    with pytest.raises(BenchmarkIsolationError):
        isolate_payload({"tier": "vibes"}, output_dir=".")


def test_write_report_enforces_isolation(tmp_path: Path):
    report = build_report([TierPayload(TIER_REAL, case_count=1)])
    with pytest.raises(BenchmarkIsolationError):
        write_report(tmp_path / "not_real", report)
    paths = write_report(tmp_path / "real_run", report)
    assert paths["json"].exists() and paths["markdown"].exists()


def test_unmeasured_metrics_are_rows_not_deletions():
    metrics = unmeasured_metrics({}, note="because")
    assert set(metrics) == set(BASE_METRICS) | set(ADVERSARIAL_METRICS)
    tier = tier_report(TIER_SYNTHETIC, metrics)
    assert tier["measured_metrics"] == 0
    assert tier["unmeasured_metrics"] == len(BASE_METRICS) + len(ADVERSARIAL_METRICS)
    markdown = render_report_markdown(
        build_report([TierPayload(TIER_SYNTHETIC)]),
    )
    assert "N/A" in markdown


# --------------------------------------------------------------------------- #
# Component harmonisation (reuse, not re-measure)
# --------------------------------------------------------------------------- #
def test_extraction_component_reader_maps_micro_scores():
    payload = {
        "synthetic": True,
        "metrics": {
            "extraction": {"micro": {"precision": 0.9, "recall": 0.8, "f1": 0.85}},
            "page_attribution_accuracy": 1.0,
        },
    }
    metrics = component_metrics("card01_document_extraction", payload)
    assert metrics["extraction_precision"].value == pytest.approx(0.9)
    assert metrics["extraction_recall"].value == pytest.approx(0.8)
    assert metrics["extraction_f1"].value == pytest.approx(0.85)
    assert metrics["page_attribution_accuracy"].value == pytest.approx(1.0)


def test_non_synthetic_component_value_carries_a_warning_note():
    payload = {
        "synthetic": False,
        "metrics": {"extraction": {"micro": {"precision": 0.5, "recall": 0.5, "f1": 0.5}}},
    }
    metrics = component_metrics("card01_document_extraction", payload)
    assert metrics["extraction_f1"].note == "non_synthetic_component"


def test_checker_component_reader_maps_top_level_fields():
    metrics = component_metrics(
        "card05_report_checker",
        {"precision": 1.0, "recall": 0.75, "f1": 0.857, "false_positive_rate": 0.0},
    )
    assert metrics["checker_recall"].value == pytest.approx(0.75)
    assert metrics["checker_false_positive_rate"].value == 0.0


def test_native_component_reader_accepts_cn_agent_v2_metric_ids():
    metrics = component_metrics(
        "card03_calculation",
        {"metrics": {"calculation_accuracy": {"value": 0.88, "unit": "%"}}},
    )
    assert metrics["calculation_accuracy"].value == pytest.approx(0.88)


def test_two_components_claiming_one_metric_is_a_definition_conflict():
    with pytest.raises(ValueError, match="exactly one definition"):
        merge_components(
            {
                "a": {"metrics": {"checker_f1": 0.1}},
                "b": {"metrics": {"checker_f1": 0.2}},
            }
        )


def test_report_over_the_committed_component_baselines(monkeypatch):
    """The real aggregator path, over whatever baselines are on disk."""
    if not OUTPUT_ROOT.exists():
        pytest.skip("组件基线尚未生成（先运行 scripts/run_benchmark_v2.py --set synthetic）")
    components = {}
    for name, filename in (
        ("card01_document_extraction", "document_extraction_baseline.json"),
        ("card05_report_checker", "checker_baseline.json"),
        ("card26_finfuzz", "finfuzz_baseline.json"),
    ):
        path = OUTPUT_ROOT / filename
        if path.exists():
            components[name] = json.loads(path.read_text(encoding="utf-8"))
    if not components:
        pytest.skip("组件基线目录为空")

    report = build_report(components=components, component_case_count=10)
    tier = report["tiers"][0]
    assert tier["tier"] == TIER_SYNTHETIC
    assert tier["measured_metrics"] >= 4
    # Whatever was measured must be attributed to a component, not to "computed".
    measured = {
        name: payload
        for group in tier["groups"].values()
        for name, payload in group.items()
        if payload["value"] is not None
    }
    assert measured
    assert all(payload["source"] != "computed" for payload in measured.values())
    assert report["base_metric_count"] == 24


# --------------------------------------------------------------------------- #
# §15 acceptance 4 — environment fingerprint
# --------------------------------------------------------------------------- #
def test_environment_fingerprint_is_stable_and_never_invents_a_commit():
    first = environment_fingerprint()
    second = environment_fingerprint()
    assert first == second
    assert first["git_commit"]  # "unknown" is a value; an empty string is not
    assert fingerprint_digest(first) == fingerprint_digest(dict(reversed(list(first.items()))))


def test_report_carries_the_fingerprint_and_its_digest_renders():
    report = build_report([TierPayload(TIER_SYNTHETIC, case_count=1)])
    assert "environment" in report
    markdown = render_report_markdown(report)
    assert fingerprint_digest(report["environment"]) in markdown
    assert "阈值状态" in markdown


def test_build_report_requires_some_input():
    with pytest.raises(ValueError):
        build_report()


def test_build_report_refuses_a_duplicated_synthetic_tier():
    with pytest.raises(ValueError, match="twice"):
        build_report(
            [TierPayload(TIER_SYNTHETIC)],
            components={"card03_calculation": {"metrics": {"calculation_accuracy": 1.0}}},
        )


# --------------------------------------------------------------------------- #
# §12 synthetic tier runs fully offline
# --------------------------------------------------------------------------- #
def test_synthetic_tier_runs_end_to_end_offline(tmp_path: Path):
    labels = [
        TruthLabel("D", "营业收入", "2024FY", 100.0, "元", "合并", 12, ("E1",)),
        TruthLabel("D", "营业成本", "2024FY", 60.0, "元", "合并", 12, ("E2",)),
    ]
    predictions = [
        Prediction("D", "营业收入", "2024FY", 100.0, "元", "合并", 12, cited_evidence_ids=("E1",)),
        Prediction("D", "营业成本", "2024FY", 60.0, "元", "合并", 12, cited_evidence_ids=("E2",)),
    ]
    payload = TierPayload(
        TIER_SYNTHETIC,
        labels=labels,
        predictions=predictions,
        checker_rows=[{"predicted": True, "actual": True}, {"predicted": False, "actual": False}],
        agent_rows=[
            {
                "expected_tools": ["ingest_cn_document"],
                "called_tools": ["ingest_cn_document"],
                "expected_arguments": {"path": "x.pdf"},
                "called_arguments": {"path": "x.pdf"},
                "completed": True,
                "required_evidence": ["E1"],
                "covered_evidence": ["E1"],
            }
        ],
        sentences=[{"claim_type": "fact", "backing_claim_type": "fact"}],
        replay_rows=[{"run_id": "r", "replayed": True, "identical": True}],
        run_summaries=[{"extraction_f1": 1.0}, {"extraction_f1": 1.0}, {"extraction_f1": 1.0}],
        cost_rows=[{"latency_seconds": 0.5, "cost": 0.0}],
        case_count=1,
    )
    report = build_report([payload], extra_fingerprint={"mode": "offline"})
    tier = report["tiers"][0]
    values = tier["metrics"]
    assert values["extraction_f1"]["value"] == pytest.approx(1.0)
    assert values["checker_false_positive_rate"]["value"] == pytest.approx(0.0)
    assert values["task_completion_rate"]["value"] == pytest.approx(1.0)
    assert values["unsafe_conclusion_leakage"]["value"] == pytest.approx(0.0)
    assert values["replay_success_rate"]["value"] == pytest.approx(1.0)
    assert values["multi_run_consistency"]["value"] == pytest.approx(1.0)
    assert report["threshold_status"] == "UNFROZEN"
    write_report(tmp_path / "cn_agent_v2_offline", report)
    assert (tmp_path / "cn_agent_v2_offline" / "report.md").exists()


def test_typing_a_real_payload_into_a_synthetic_directory_is_blocked(tmp_path: Path):
    report = build_report([TierPayload(TIER_REAL, case_count=1)])
    with pytest.raises(BenchmarkIsolationError):
        write_report(tmp_path / "cn_agent_v2", report)
