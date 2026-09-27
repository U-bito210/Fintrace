"""Offline tests for CARD-11 §7b — the two measurable innovation ablations.

§7b's rule is "every innovation shows a without-vs-with comparison, and nothing
is filled in before it has been run". ``src/cn/ablation_v2.py`` is the runner;
this file is what keeps it honest. Four kinds of assertion, in the order they
matter:

1. **The arms are real.** The baseline must be the component the project
   actually shipped (§7.1's dictionary matcher, CARD-09's binding rule), and the
   treatment must be the *whole* pipeline rather than just the new layer. A
   strawman baseline or a halved treatment would move the ratio without moving
   the system.
2. **The numbers are pinned, not bounded.** Recall is asserted as an exact
   count over an exact corpus. If a case stops being a defect — because the
   dictionary learned a label, or a constraint was relaxed — a test fails
   instead of the baseline quietly shrinking.
3. **False positives travel with recall.** A detector that flagged everything
   would post a perfect recall; the clean controls are asserted too.
4. **Unmeasured stays unmeasured.** The three comparisons with no offline
   inputs must still render ``TARGET / TO BE MEASURED``, and a missing result
   file must read as "not measured", never as zero.

Everything here is offline and clock-free.
"""

from __future__ import annotations

import json

import pytest

from src.cn.ablation_v2 import (
    ABLATION_FILENAME,
    METRIC_ACME,
    METRIC_PASSPORT,
    MEASURED_AT,
    acme_cases,
    ablation_runs,
    coverage_note,
    extraction_signals,
    load_ablation,
    measured_only,
    passport_cases,
    run_acme_ablation,
    run_passport_ablation,
    write_ablation,
)
from src.cn.benchmark_v2 import (
    ABLATION_COMPARISONS,
    NOT_MEASURED,
    build_ablation_table,
)
from src.cn.normalization.metrics import resolve_metric

#: The exact recall each arm posts on the fixed corpus. Written as counts so a
#: corpus change cannot pass by matching a rounded float.
ACME_HITS = {"parser_only": (2, 5), "parser_plus_accounting_constraints": (5, 5)}
PASSPORT_HITS = {"citation_only": (1, 4), "fpo_verifier": (4, 4)}


def _recall(summary, metric):
    entry = summary["metrics"][metric]
    return entry["numerator"], entry["denominator"]


def _false_positives(summary):
    entry = summary["metrics"]["false_positive_rate_on_clean_controls"]
    return entry["numerator"], entry["denominator"]


# --------------------------------------------------------------------------- #
# 1. The arms are real
# --------------------------------------------------------------------------- #
def test_acme_baseline_really_runs_the_dictionary_matcher():
    """``parser_only`` is §7.1's own signal, not a string written for the test.

    The assertion is on the *live* return value of ``resolve_metric``: if the
    baseline were hardcoded, teaching the dictionary a new label would leave the
    signal text unchanged.
    """
    case = next(c for c in acme_cases() if c.case_id == "unknown_line_item_ratio")
    label = case.unresolved_labels[0]
    live_key = resolve_metric(label).key

    signals = extraction_signals(case)

    assert live_key.startswith("metric_unknown")
    assert signals, "the lexical case must produce the parser's own signal"
    assert live_key in signals[0]


def test_acme_treatment_arm_is_the_whole_pipeline_not_just_the_new_layer():
    """The baseline names the *ablated* system, so the treatment must be a superset.

    Measuring the constraint layer alone would report 3/5 — and 0/2 on the
    lexical family against the baseline's 2/2 — i.e. "attaching ACME made
    detection worse". Both arms run over the same corpus, so this is the
    property that keeps that false impression out of the report.
    """
    runs = run_acme_ablation()
    baseline = {
        case["case_id"] for case in runs["parser_only"]["cases"] if case["detected"]
    }
    treatment = {
        case["case_id"]
        for case in runs["parser_plus_accounting_constraints"]["cases"]
        if case["detected"]
    }

    assert baseline < treatment, "treatment must strictly extend the baseline"
    assert all(
        case["detected"] == (case["case_id"] in baseline)
        for case in runs["parser_plus_accounting_constraints"]["cases"]
        if case["case_id"] in baseline
    ), "no defect the ablated system saw may vanish once the innovation is added"


def test_passport_treatment_keeps_the_citation_rule_it_builds_on():
    """Same rule for CARD-23: the orphan check lives *inside* the passport."""
    runs = run_passport_ablation()
    citation_hits = {
        case["case_id"] for case in runs["citation_only"]["cases"] if case["detected"]
    }
    verifier_hits = {
        case["case_id"] for case in runs["fpo_verifier"]["cases"] if case["detected"]
    }

    assert citation_hits == {"no_evidence_at_all"}
    assert citation_hits < verifier_hits


# --------------------------------------------------------------------------- #
# 2. The numbers are pinned
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("arm,expected", sorted(ACME_HITS.items()))
def test_acme_recall_is_the_pinned_count(arm, expected):
    summary = run_acme_ablation()[arm]

    assert _recall(summary, METRIC_ACME) == expected


@pytest.mark.parametrize("arm,expected", sorted(PASSPORT_HITS.items()))
def test_passport_recall_is_the_pinned_count(arm, expected):
    summary = run_passport_ablation()[arm]

    assert _recall(summary, METRIC_PASSPORT) == expected


def test_acme_gain_comes_from_the_semantic_families_the_parser_cannot_see():
    """Where the delta comes from, per family — the part a bare ratio hides."""
    runs = run_acme_ablation()

    assert runs["parser_only"]["by_family"] == {
        "accounting_identity": {"detected": 0, "total": 1},
        "table_structural": {"detected": 0, "total": 1},
        "cross_source": {"detected": 0, "total": 1},
        "extraction_lexical": {"detected": 2, "total": 2},
    }
    assert runs["parser_plus_accounting_constraints"]["by_family"] == {
        "accounting_identity": {"detected": 1, "total": 1},
        "table_structural": {"detected": 1, "total": 1},
        "cross_source": {"detected": 1, "total": 1},
        "extraction_lexical": {"detected": 2, "total": 2},
    }


def test_passport_gain_covers_the_families_the_citation_rule_misses():
    runs = run_passport_ablation()

    assert runs["citation_only"]["by_family"] == {
        "orphan_claim": {"detected": 1, "total": 1},
        "ghost_reference": {"detected": 0, "total": 1},
        "withdrawn_evidence": {"detected": 0, "total": 1},
        "superseded_period": {"detected": 0, "total": 1},
    }
    assert all(
        bucket["detected"] == bucket["total"]
        for bucket in runs["fpo_verifier"]["by_family"].values()
    )


def test_the_ghost_reference_defect_is_reported_as_a_missing_evidence_not_an_orphan():
    """The corpus reads the verifier's *reason*, so a mislabelled failure shows.

    ``verify_source`` used to read the reference list off ``SourceProof.evidence_ids``,
    which only carries ids the ledger could resolve — so a citation to a
    non-existent ``E-GHOST`` arrived as "cited nothing". Both verdicts are
    ``BLOCKED``, so only the message distinguishes them.
    """
    case = next(
        c for c in run_passport_ablation()["fpo_verifier"]["cases"]
        if c["case_id"] == "ghost_evidence_id"
    )

    assert case["detected"]
    joined = "；".join(case["signals"])
    assert "证据不存在" in joined
    assert "E-GHOST" in joined


# --------------------------------------------------------------------------- #
# 3. False positives travel with recall
# --------------------------------------------------------------------------- #
def test_every_arm_posts_its_false_positive_count_next_to_the_recall():
    """A detector that fires on everything would otherwise look perfect."""
    for summary in ablation_runs().values():
        metrics = summary["metrics"]
        assert "false_positive_rate_on_clean_controls" in metrics
        numerator, denominator = _false_positives(summary)
        assert denominator > 0, "a corpus with no clean control cannot report an FPR"
        assert numerator == 0, "the shipped detectors must stay quiet on clean inputs"


# --------------------------------------------------------------------------- #
# 4. Unmeasured stays unmeasured
# --------------------------------------------------------------------------- #
def test_the_measured_runs_fill_exactly_the_two_comparable_rows():
    """The other three innovations have no offline runner and must say so."""
    table = build_ablation_table(ablation_runs())
    by_innovation = {row["innovation"]: row for row in table}

    assert len(table) == len(ABLATION_COMPARISONS) == 5
    assert by_innovation["ACME (CARD-22)"]["status"] == "MEASURED"
    assert by_innovation["ACME (CARD-22)"]["baseline_value"] == pytest.approx(0.4)
    assert by_innovation["ACME (CARD-22)"]["treatment_value"] == pytest.approx(1.0)
    assert by_innovation["Claim Passport (CARD-23)"]["status"] == "MEASURED"
    assert by_innovation["Claim Passport (CARD-23)"]["baseline_value"] == pytest.approx(0.25)
    assert by_innovation["Claim Passport (CARD-23)"]["treatment_value"] == pytest.approx(1.0)

    for innovation in (
        "Thesis Fragility (CARD-24)",
        "Temporal Revalidation (CARD-25)",
        "FinFuzz (CARD-26)",
    ):
        row = by_innovation[innovation]
        assert row["status"] == NOT_MEASURED, innovation
        assert row["baseline_value"] is None
        assert row["treatment_value"] is None


def test_an_absent_ablation_file_means_not_measured_never_zero(tmp_path):
    """Missing data is "we did not run it", which is not the same as 0.0."""
    assert load_ablation(tmp_path) == {}


def test_a_corrupt_ablation_file_is_not_laundered_into_a_measurement(tmp_path):
    (tmp_path / ABLATION_FILENAME).write_text("{not json", encoding="utf-8")

    assert load_ablation(tmp_path) == {}


def test_a_half_measured_group_is_dropped_so_the_row_falls_back_to_target():
    """``build_ablation_table`` only treats a side as missing when it is absent.

    A summary whose headline is ``None`` must be dropped rather than handed over
    as a value, or the row would read as measured. The side that *was* measured
    still shows its number — half a comparison, labelled as half a comparison,
    which is more useful than erasing it.
    """
    runs = {
        "parser_only": {"metrics": {METRIC_ACME: {"value": None}}},
        "fpo_verifier": {"metrics": {METRIC_PASSPORT: {"value": 1.0}}},
    }

    kept = measured_only(runs)

    assert set(kept) == {"fpo_verifier"}
    table = {row["innovation"]: row for row in build_ablation_table(kept)}
    assert table["ACME (CARD-22)"]["status"] == NOT_MEASURED
    assert table["ACME (CARD-22)"]["baseline_value"] is None
    assert table["ACME (CARD-22)"]["treatment_value"] is None

    passport = table["Claim Passport (CARD-23)"]
    assert passport["status"] == NOT_MEASURED, "one side is still missing"
    assert passport["baseline_value"] is None
    assert passport["treatment_value"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Determinism, coverage honesty, and the on-disk artifact
# --------------------------------------------------------------------------- #
def test_two_runs_are_byte_identical():
    """A clock read anywhere in here would make reruns differ for no reason."""
    first = json.dumps(ablation_runs(), ensure_ascii=False, sort_keys=True)
    second = json.dumps(ablation_runs(), ensure_ascii=False, sort_keys=True)

    assert first == second


def test_the_timestamp_is_a_constant_and_the_corpus_declares_no_network():
    for summary in ablation_runs().values():
        metadata = summary["metadata"]
        assert metadata["measured_at"] == MEASURED_AT
        assert metadata["network"] is False


def test_the_acme_note_admits_the_families_its_corpus_does_not_cover():
    """Claiming F1–F6 while the corpus only reaches F1/F2/F5 would be the lie."""
    note = coverage_note()

    assert "accounting_identity" in note
    assert "table_structural" in note
    assert "cross_source" in note
    for uncovered in ("period_relations", "cross_period", "cross_modal"):
        assert uncovered in note, uncovered
    assert "不冒充已覆盖" in note


def test_every_case_carries_its_own_ground_truth_and_nothing_else():
    """The corpus states what was injected; it never states the expected answer."""
    for case in acme_cases():
        assert case.defect is None or case.defect, case.case_id
        if case.defect is not None:
            assert case.family != "clean_control", case.case_id
        else:
            assert case.family == "clean_control", case.case_id
    for case in passport_cases():
        assert case.defect is None or case.defect, case.case_id


def test_write_then_load_round_trips_and_writes_both_a_json_and_a_markdown(tmp_path):
    paths = write_ablation(tmp_path)

    assert paths["json"].name == ABLATION_FILENAME
    assert paths["markdown"].is_file()
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["section"] == "7b"
    assert payload["measured_at"] == MEASURED_AT
    assert load_ablation(tmp_path) == ablation_runs()

    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "TARGET" not in markdown, "nothing in this corpus should be unmeasured"
    for arm in (*ACME_HITS, *PASSPORT_HITS):
        assert arm in markdown
