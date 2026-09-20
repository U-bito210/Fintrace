"""CARD-26 — FinFuzz 测试。

分组对应卡的各节：§14 单测（十个算子各自的确定性用例 / 指标计算器 / seed 复现）、
§12 边界（伪变异、组合、去重、holdout）、§13 校验器（含故意破坏的报告）、
§15 集成（CARD-05 作为被测目标、CARD-11 消费 adversarial_rows）、§9 零 LLM。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.cn.benchmark_v2 import ADVERSARIAL_METRICS, TierPayload, build_report, evaluate_tier
from src.cn.checker.models import ClaimType, DraftClaim
from src.cn.checker.sentences import DraftSentence
from src.cn.checker.rule_extractor import extract_claims_rules
from src.cn.finfuzz import (
    CHANNEL_CLAIM,
    CHANNEL_TEXT,
    FAILURE_CHECKER,
    FAILURE_EXTRACTOR,
    NOT_APPLICABLE,
    NOT_MEASURED,
    OPERATOR_ORDER,
    BASELINE_FILENAME,
    COMPONENT,
    FinFuzzError,
    MutationOperators,
    MutationResult,
    MutationSpec,
    OperatorContext,
    baseline_metrics,
    baseline_payload,
    compute_metrics,
    combine_specs,
    generate_mutation_specs,
    load_clean_claims,
    load_corpus,
    render_markdown,
    run_corpus,
    run_suite,
    write_baseline,
)
from src.cn.finfuzz.baseline import format_markdown as format_baseline_markdown
from src.cn.finfuzz.operators import (
    OPERATORS,
    mutate_causal,
    mutate_citation,
    mutate_numeric,
    mutate_period,
    mutate_scope,
    mutate_sign,
    mutate_unit,
    mutate_valuation_basis,
    mutate_version,
    mutate_ytd_vs_single,
)
from src.validation import validate_finfuzz, validate_finfuzz_corpus
from src.validation.checker_validator import check_source_is_model_free

REPO_ROOT = Path(__file__).resolve().parents[1]
FINFUZZ_SOURCES = sorted((REPO_ROOT / "src" / "cn" / "finfuzz").glob("*.py"))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def cases():
    return load_clean_claims()


@pytest.fixture(scope="module")
def corpus_report(cases):
    report, _skipped = run_corpus(cases)
    return report


def _case(cases, doc_id):
    return next(item for item in cases if item.doc_id == doc_id)


def _plain_claim(**kwargs) -> DraftClaim:
    payload = dict(
        claim_id="probe#s0001-c1",
        sentence="公司2025年营业收入为110万元。（来源：annual_04_restated_comparative.pdf#p1）",
        page=1,
        metric="revenue",
        metric_label="营业收入",
        period="2025FY",
        value=110.0,
        value_literal="110",
        unit="万元",
        source_reference="annual_04_restated_comparative.pdf#p1",
        source_references=("annual_04_restated_comparative.pdf#p1",),
        claim_type=ClaimType.FACTUAL.value,
        document_id="probe",
    )
    payload.update(kwargs)
    return DraftClaim(**payload)


# =========================================================================== #
# §14 — one deterministic case per operator
# =========================================================================== #
def test_numeric_operator_scales_value_and_literal():
    outcomes = mutate_numeric(_plain_claim(), OperatorContext())
    planted = [item for item in outcomes if not item.equivalent]
    assert len(planted) == 1
    assert planted[0].mutated_claim.value == 1100.0
    assert planted[0].mutated_claim.value_literal == "1100"
    assert planted[0].text_edits == (("110", "1100"),)


def test_numeric_equivalent_variant_is_semantically_identical():
    outcomes = mutate_numeric(_plain_claim(), OperatorContext())
    equivalent = [item for item in outcomes if item.equivalent]
    assert len(equivalent) == 1
    # The value must NOT move — the only change is how the digits are written.
    assert equivalent[0].mutated_claim.value == 110.0
    assert equivalent[0].text_edits == (("110", "１１０"),)


def test_unit_operator_two_variants_are_opposite_in_meaning():
    outcomes = mutate_unit(_plain_claim(), OperatorContext(unit_words=("元", "万元", "亿元")))
    planted = [item for item in outcomes if not item.equivalent]
    equivalent = [item for item in outcomes if item.equivalent]
    assert len(planted) == 1 and len(equivalent) == 1
    # Magnitude error: 110 万元 read as 110 亿元.
    assert planted[0].mutated_claim.unit == "亿元"
    assert planted[0].mutated_claim.value == 110.0
    # Equal-value rewrite: 110 万元 = 1,100,000 元. The canonical amount is fixed.
    assert equivalent[0].mutated_claim.unit == "元"
    assert equivalent[0].mutated_claim.value == 1_100_000.0


def test_sign_operator_flips_growth_direction():
    claim = _plain_claim(growth=10.0, growth_literal="10.0")
    outcomes = mutate_sign(claim, OperatorContext())
    assert len(outcomes) == 1
    assert outcomes[0].mutated_claim.growth == -10.0


def test_sign_operator_declines_without_a_recomputable_period():
    # CARD-05's growth rule returns early without metric/period, so a spec here
    # could only produce a miss the checker never had the material to avoid.
    claim = _plain_claim(period=None, growth=10.0, growth_literal="10.0")
    assert mutate_sign(claim, OperatorContext()) == ()
    assert mutate_sign(_plain_claim(growth=None), OperatorContext()) == ()


def test_period_operator_shifts_the_year_from_the_canonical_key():
    outcomes = mutate_period(_plain_claim(), OperatorContext())
    assert outcomes[0].mutated_claim.period == "2024FY"
    assert outcomes[0].text_edits == (("2025年", "2024年"),)


def test_period_operator_declines_without_a_period():
    assert mutate_period(_plain_claim(period=None), OperatorContext()) == ()


def test_ytd_vs_single_labels_a_cumulative_claim_as_one_quarter():
    claim = _plain_claim(
        claim_id="probe#s0002-c1",
        sentence="示例公司 2025 概览（草稿）其中年初至报告期末累计营业收入为200万元。（来源：annual_01_profit_continued.pdf#p1）",
        metric="revenue",
        period=None,
        value=200.0,
        value_literal="200",
        source_reference="annual_01_profit_continued.pdf#p1",
        source_references=("annual_01_profit_continued.pdf#p1",),
    )
    outcomes = mutate_ytd_vs_single(claim, OperatorContext())
    assert len(outcomes) == 1
    assert outcomes[0].mutated_claim.period == "2025Q4"
    assert outcomes[0].text_edits == (("年初至报告期末累计", "第四季度"),)


def test_ytd_vs_single_declines_when_a_period_is_already_stated():
    assert mutate_ytd_vs_single(_plain_claim(), OperatorContext()) == ()


def test_scope_operator_swaps_only_a_declared_scope():
    outcomes = mutate_scope(_plain_claim(scope="合并"), OperatorContext())
    assert outcomes[0].mutated_claim.scope == "母公司"
    # A claim that declares no 口径 cannot be mislabelled.
    assert mutate_scope(_plain_claim(), OperatorContext()) == ()


def test_version_operator_quotes_the_superseded_disclosure(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    claim = case.claims[0]
    outcomes = mutate_version(claim, case.context)
    assert len(outcomes) == 1
    # 110 万元 is the current figure; 90 万元 is the 调整前 column, which the same
    # table's 调整后 column replaced. Picking the *restated* 100 万元 instead — the
    # first version of this operator did — produces a sentence that is correct.
    assert outcomes[0].mutated_claim.value == 90.0


def test_version_operator_declines_without_a_superseded_value(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-02")
    claim = next(item for item in case.claims if item.metric == "net_profit_parent")
    assert mutate_version(claim, case.context) == ()


def test_valuation_operator_flags_a_wrong_multiple_and_labels_the_swap():
    claim = _plain_claim(
        metric=None,
        metric_label=None,
        value=None,
        value_literal=None,
        unit=None,
        period="2025FY",
        valuation_multiple="市盈率 10倍",
        sentence="公司当前市盈率为10倍。（来源：SYN-MARKET-01#p1）",
        source_reference="SYN-MARKET-01#p1",
        source_references=("SYN-MARKET-01#p1",),
        claim_type=ClaimType.VALUATION.value,
    )
    outcomes = mutate_valuation_basis(claim, OperatorContext())
    descriptions = [item.description for item in outcomes]
    assert any("同一基数" in item for item in descriptions)
    assert any("基数互换" in item for item in descriptions)


def test_valuation_operator_declines_without_a_multiple():
    assert mutate_valuation_basis(_plain_claim(), OperatorContext()) == ()


def test_citation_operator_uses_the_page_inside_the_reference():
    # ``DraftClaim.page`` is where the *sentence* sits; the checker reads the page
    # from the citation string. Mutating the former changed nothing at all.
    outcomes = mutate_citation(_plain_claim(), OperatorContext())
    wrong_page = next(item for item in outcomes if "页码" in item.description)
    assert wrong_page.mutated_claim.page == 1
    assert wrong_page.mutated_claim.source_reference.endswith("#p5")


def test_citation_operator_offers_absent_and_wrong_documents():
    outcomes = mutate_citation(
        _plain_claim(),
        OperatorContext(registered_filenames=("annual_04_restated_comparative.pdf", "annual_01_profit_continued.pdf")),
    )
    descriptions = "；".join(item.description for item in outcomes)
    assert "不存在文档" in descriptions
    assert "引用对象错" in descriptions


def test_citation_operator_declines_without_a_reference():
    assert mutate_citation(
        _plain_claim(source_reference=None, source_references=()), OperatorContext()
    ) == ()


def test_causal_operator_rewrites_a_plain_statement_into_an_assertion():
    outcomes = mutate_causal(_plain_claim(), OperatorContext())
    assert len(outcomes) == 1
    assert outcomes[0].mutated_claim.claim_type == ClaimType.CAUSAL.value
    # The connective has to satisfy CARD-05's causal pattern, which anchors on
    # 因/由于/受 — otherwise every causal spec would be an extractor-stage miss
    # wearing the costume of a checker defect.
    assert outcomes[0].text_edits
    assert "受" in outcomes[0].text_edits[0][1]


def test_causal_operator_declines_on_an_already_causal_claim():
    assert mutate_causal(_plain_claim(claim_type=ClaimType.CAUSAL.value), OperatorContext()) == ()


def test_every_card_operator_has_an_entry_in_the_table():
    assert set(OPERATORS) == set(OPERATOR_ORDER)
    assert len(OPERATOR_ORDER) == 10
    assert {member.value for member in MutationOperators} == set(OPERATOR_ORDER)


def test_operators_are_deterministic():
    first = mutate_numeric(_plain_claim(), OperatorContext())
    second = mutate_numeric(_plain_claim(), OperatorContext())
    assert [item.mutated_claim.to_dict() for item in first] == [
        item.mutated_claim.to_dict() for item in second
    ]


# =========================================================================== #
# §7 — end-to-end detection on the real corpus, per operator
# =========================================================================== #
@pytest.mark.parametrize("operator", OPERATOR_ORDER)
def test_operator_either_produces_samples_or_declines_with_a_reason(cases, operator):
    generated = generate_mutation_specs(
        [claim for case in cases for claim in case.claims],
        [operator],
        context=cases[0].context,
        targets=cases[0].targets,
    )
    if generated.specs:
        assert all(spec.operator == operator for spec in generated.specs)
    else:
        reasons = [item["reason"] for item in generated.skipped if item["operator"] == operator]
        assert reasons and all(reason for reason in reasons)


def test_real_corpus_detects_the_core_operators(corpus_report):
    rows = corpus_report.per_error_type
    for operator in ("numeric", "unit", "scope", "version", "citation", "causal"):
        assert rows[operator]["n"] > 0, operator
        assert rows[operator]["recall"] == 1.0, (operator, rows[operator])


def test_the_masked_period_tamper_is_attributed_to_the_extractor(corpus_report):
    # CLEAN-01's first sentence carries a dated heading, so CARD-05's period table
    # resolves the period from the heading and the body-year tamper disappears.
    # The report must say "extractor", not "checker" — that distinction is the
    # whole reason §7 asks for two channels.
    assert corpus_report.extractor_vs_checker["extractor_failures"] >= 1
    assert corpus_report.extractor_vs_checker["checker_failures"] == 0


def test_no_source_claim_is_mutated_without_a_clean_baseline(cases):
    for case in cases:
        for claim in case.claims:
            for name, target in case.targets.items():
                assert not target.detect([claim]).detected, (case.doc_id, claim.claim_id, name)


def test_corpus_keeps_individually_clean_claims_from_planted_drafts():
    loaded = load_corpus()
    by_id = {case.doc_id: case for case in loaded.cases}
    # draft_07 plants a scope error on one line and carries a correct 合并 line too.
    assert "SYN-DRAFT-VALUATION" in by_id
    assert by_id["SYN-DRAFT-VALUATION"].claims
    assert by_id["SYN-DRAFT-VALUATION"].excluded
    # Drafts with nothing clean left are dropped by name, never silently.
    skipped = {item["doc_id"] for item in loaded.skipped}
    assert "SYN-DRAFT-FIGURES" in skipped


def test_validator_target_corroborates_structural_mutations(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(
        list(case.claims), ["causal"], context=case.context, targets=case.targets
    )
    results = run_suite(generated.specs, case.targets)
    assert results
    corroborated = [item for item in results if "validator" in item.detected_by_all]
    assert corroborated, "the graph validator should also reject an inference with no derivation"


# =========================================================================== #
# §14 — metric calculator
# =========================================================================== #
def _result(operator, *, planted, detected, stage=None, channel=CHANNEL_CLAIM):
    spec = MutationSpec(
        mutation_id=f"{operator}:{channel}:c:{detected}:{planted}",
        operator=operator,
        source_claim={"a": 1},
        mutated_claim={"a": 2},
        description="d",
        expected_detection=planted,
        channel=channel,
        expected_check_types=("numeric_error",) if planted else (),
        claim_id="c",
    )
    if detected:
        return MutationResult(spec=spec, detected=True, detected_by="checker", error_type_classified="x")
    return MutationResult(spec=spec, detected=False, failure_stage=stage or FAILURE_CHECKER)


def test_metric_calculator_matches_hand_computed_values():
    results = (
        _result("numeric", planted=True, detected=True),
        _result("numeric", planted=True, detected=True),
        _result("numeric", planted=True, detected=False),
        _result("unit", planted=True, detected=True),
        _result("unit", planted=False, detected=True),      # false positive
        _result("unit", planted=False, detected=False),     # true negative
    )
    report = compute_metrics(results)
    assert report.overall["true_positive"] == 3
    assert report.overall["false_negative"] == 1
    assert report.overall["false_positive"] == 1
    assert report.overall["true_negative"] == 1
    assert report.overall["precision"] == pytest.approx(0.75)
    assert report.overall["recall"] == pytest.approx(0.75)
    assert report.overall["f1"] == pytest.approx(0.75)
    assert report.overall["false_positive_rate"] == pytest.approx(0.5)
    assert report.per_error_type["numeric"]["recall"] == pytest.approx(2 / 3)
    assert report.per_error_type["unit"]["recall"] == pytest.approx(1.0)
    assert report.per_error_type["sign"]["recall"] == NOT_APPLICABLE


def test_metric_calculator_reports_na_instead_of_zero_for_empty_categories():
    report = compute_metrics(())
    assert report.overall["precision"] is None
    assert report.overall["recall"] is None
    assert report.overall["false_positive_rate"] is None
    assert not report.measured
    assert all(
        row["recall"] == NOT_APPLICABLE for row in report.per_error_type.values()
    )


def test_adversarial_rows_carry_exactly_what_card11_reads(corpus_report):
    rows = corpus_report.adversarial_rows()
    assert rows
    assert all(set(row) == {"error_type", "planted", "detected"} for row in rows)


def test_false_positive_samples_are_counted_not_dropped(corpus_report):
    assert corpus_report.overall["equivalent_n"] > 0
    assert corpus_report.overall["false_positive_rate"] is not None


# =========================================================================== #
# §12 — edge cases
# =========================================================================== #
def test_seed_controls_sampling_and_is_reproducible(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    first = generate_mutation_specs(
        list(case.claims), context=case.context, targets=case.targets, seed=7, max_per_operator=2
    )
    second = generate_mutation_specs(
        list(case.claims), context=case.context, targets=case.targets, seed=7, max_per_operator=2
    )
    other = generate_mutation_specs(
        list(case.claims), context=case.context, targets=case.targets, seed=8, max_per_operator=2
    )
    assert [item.mutation_id for item in first.specs] == [item.mutation_id for item in second.specs]
    assert len(first.specs) <= 2 * len(OPERATOR_ORDER)
    assert any(item["reason"].startswith("sampled") for item in first.skipped) or other.specs


def test_duplicate_mutations_are_deduplicated(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(
        list(case.claims), ["numeric", "numeric"], context=case.context, targets=case.targets
    )
    keys = [(spec.channel, spec.claim_id, json.dumps(spec.mutated_claim, sort_keys=True)) for spec in generated.specs]
    assert len(keys) == len(set(keys))
    assert any(item["reason"].startswith("duplicate_mutation") for item in generated.skipped)


def test_combination_specs_keep_a_combination_label(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(
        list(case.claims), ["numeric", "period"], context=case.context, targets=case.targets
    )
    numeric = next(item for item in generated.specs if item.operator == "numeric" and item.channel == CHANNEL_CLAIM)
    period = next(item for item in generated.specs if item.operator == "period" and item.channel == CHANNEL_CLAIM)
    merged = combine_specs(numeric, period)
    assert merged.is_combination
    assert set(merged.operators) == {"numeric", "period"}
    assert "combination" in merged.note
    assert "numeric_error" in merged.expected_check_types
    assert "period_error" in merged.expected_check_types


def test_combination_refuses_cross_claim_and_cross_channel():
    a = _result("numeric", planted=True, detected=True).spec
    b = dataclasses.replace(a, claim_id="other")
    with pytest.raises(FinFuzzError):
        combine_specs(a, b)
    c = dataclasses.replace(a, channel=CHANNEL_TEXT, text_edits=(("x", "y"),))
    with pytest.raises(FinFuzzError):
        combine_specs(a, c)


def test_holdout_is_never_read(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    from src.cn.finfuzz import run_finfuzz

    report, _generated = run_finfuzz(
        "holdout-probe", list(case.claims), ["numeric"], case.targets, holdout=["secret"]
    )
    assert report.baseline_holdout["status"] == NOT_MEASURED
    assert any("holdout_provided_but_not_read" in note for note in report.notes)


def test_text_specs_always_change_the_sentence(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(list(case.claims), context=case.context, targets=case.targets)
    for spec in generated.specs:
        if spec.channel == CHANNEL_TEXT:
            assert spec.text_edits
            for old, _new in spec.text_edits:
                assert old in spec.source_sentence


def test_claim_specs_always_carry_a_real_diff(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(list(case.claims), context=case.context, targets=case.targets)
    for spec in generated.specs:
        if spec.channel == CHANNEL_CLAIM:
            assert spec.diff()


def test_miss_attribution_uses_the_extractor_stage_only_for_text_specs(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(list(case.claims), ["numeric"], context=case.context, targets=case.targets)
    claim_spec = next(item for item in generated.specs if item.channel == CHANNEL_CLAIM)
    # Force a miss by removing the tamper while keeping the expectation: the
    # sentence is back to being correct, so the miss can only be the checker's.
    neutered = dataclasses.replace(claim_spec, mutated_claim=dict(claim_spec.source_claim))
    result = run_suite([neutered], case.targets)[0]
    assert not result.detected
    assert result.failure_stage == FAILURE_CHECKER


def test_text_channel_can_produce_an_extractor_stage_miss(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(list(case.claims), ["period"], context=case.context, targets=case.targets)
    text_spec = next(item for item in generated.specs if item.channel == CHANNEL_TEXT)
    result = run_suite([text_spec], case.targets)[0]
    assert result.failure_stage == FAILURE_EXTRACTOR
    assert result.extraction_carried is False
    assert "tamper_not_present_in_extracted_claim" in result.notes


def test_result_rejects_an_unattributed_miss():
    spec = _result("numeric", planted=True, detected=True).spec
    with pytest.raises(FinFuzzError):
        MutationResult(spec=spec, detected=False)
    with pytest.raises(FinFuzzError):
        MutationResult(spec=spec, detected=False, failure_stage="nowhere")


# =========================================================================== #
# §13 — validator
# =========================================================================== #
def test_validator_accepts_the_real_corpus(cases):
    validation = validate_finfuzz_corpus(cases)
    assert validation.valid, validation.errors
    assert not validation.errors


def test_validator_warns_instead_of_passing_when_results_are_absent(corpus_report):
    validation = validate_finfuzz(corpus_report)
    assert validation.valid
    assert any("metrics_not_rechecked" in item for item in validation.warnings)


def _valid_pair(cases):
    case = _case(cases, "SYN-DRAFT-CLEAN-01")
    generated = generate_mutation_specs(list(case.claims), context=case.context, targets=case.targets)
    results = run_suite(generated.specs, case.targets)
    report = compute_metrics(results, suite_id=case.suite_id)
    report.skipped = list(generated.skipped)
    return case, generated, results, report


def test_validator_catches_a_report_that_hides_extractor_failures(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.extractor_vs_checker = {
        "detected": report.extractor_vs_checker["detected"],
        "extractor_failures": 0,
        "checker_failures": 0,
        "by_detector": {},
    }
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("stage_count_not_recomputable" in item for item in errors)


def test_validator_catches_dropped_false_positive_samples(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.overall = dict(report.overall, equivalent_n=0, false_positive=0, true_negative=0)
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("equivalent_samples_dropped" in item for item in errors)


def test_validator_catches_a_tampered_overall_metric(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.overall = dict(report.overall, recall=0.99)
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("overall_not_recomputable:recall" in item for item in errors)


def test_validator_catches_a_deleted_per_error_type_row(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.per_error_type = {key: value for key, value in report.per_error_type.items() if key != "causal"}
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("per_error_type_rows_missing" in item for item in errors)


def test_validator_catches_an_empty_type_that_is_not_marked_na(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.per_error_type = dict(report.per_error_type)
    report.per_error_type["valuation"] = dict(report.per_error_type["valuation"], recall=0.0)
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("empty_type_not_marked_na" in item for item in errors)


def test_validator_catches_a_spec_without_a_diff(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    broken = dataclasses.replace(generated.specs[0], mutated_claim=dict(generated.specs[0].source_claim))
    errors = validate_finfuzz(report, results, specs=[broken, *generated.specs[1:]]).errors
    assert any("claim_spec_without_diff" in item or "text_spec_without_edits" in item for item in errors)


def test_validator_catches_duplicate_spec_ids(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    errors = validate_finfuzz(report, results, specs=[generated.specs[0], generated.specs[0]]).errors
    assert any("spec_id_not_unique" in item for item in errors)


def test_validator_catches_an_unmeasured_holdout_with_numbers(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.baseline_holdout = {"status": "pending", "recall": 0.9}
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("unmeasured_holdout_carries_numbers" in item for item in errors)


def test_validator_catches_a_skipped_entry_without_a_reason(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    report.skipped = [{"operator": "valuation"}]
    errors = validate_finfuzz(report, results, specs=generated.specs).errors
    assert any("skipped_entry_without_reason" in item for item in errors)


def _spec_like_naming_an_unregistered_member(inner):
    """A spec-like object whose ``combination`` names an operator nobody registered.

    ``MutationSpec.__post_init__`` refuses such a value outright, so the only way
    one reaches the validator is through its ``specs`` parameter — declared
    ``Sequence[Any]`` precisely because the validator is the trust boundary and must
    not assume the caller handed it a well-formed spec. Testing the rule with a
    real ``MutationSpec`` would prove nothing: the constructor already ran.
    """

    class _Spec:
        mutation_id = inner.mutation_id
        operator = inner.operator
        description = inner.description
        channel = inner.channel
        text_edits = inner.text_edits
        expected_detection = False
        expected_check_types: tuple = ()
        combination = ("made_up_operator",)
        operators = (inner.operator, "made_up_operator")
        is_combination = True

        @staticmethod
        def diff():
            return {"value": (1, 2)}

    return _Spec()


def test_validator_catches_a_combination_that_is_not_a_combination(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    broken = _spec_like_naming_an_unregistered_member(generated.specs[0])
    errors = validate_finfuzz(report, results, specs=[broken, *generated.specs[1:]]).errors
    assert any("combination_member_not_listed" in item for item in errors)


def test_validator_accepts_a_genuinely_labelled_combination(cases):
    _case_obj, generated, results, report = _valid_pair(cases)
    numeric = next(
        item for item in generated.specs
        if item.operator == "numeric" and item.channel == CHANNEL_CLAIM
    )
    period = next(
        item for item in generated.specs
        if item.operator == "period" and item.channel == CHANNEL_CLAIM
    )
    merged = combine_specs(numeric, period)
    specs = [merged, *[item for item in generated.specs if item.mutation_id != numeric.mutation_id]]
    errors = validate_finfuzz(report, results, specs=specs).errors
    assert not any("combination" in item for item in errors)


# =========================================================================== #
# §9 — deterministic, no model client
# =========================================================================== #
def test_finfuzz_sources_do_not_reach_for_a_model_client():
    assert FINFUZZ_SOURCES
    for path in FINFUZZ_SOURCES:
        problems = check_source_is_model_free(
            path.read_text(encoding="utf-8"), label=path.name
        )
        assert not problems, (path.name, problems)


def test_report_is_reproducible(cases):
    first, _ = run_corpus(cases, seed=42)
    second, _ = run_corpus(cases, seed=42)
    assert first.to_dict() == second.to_dict()
    assert first.adversarial_rows() == second.adversarial_rows()


def test_report_never_carries_wall_clock_timing(cases):
    report, _ = run_corpus(cases)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "detection_time" not in payload
    assert "elapsed" not in payload


# =========================================================================== #
# §15 — CARD-11 integration
# =========================================================================== #
def test_card11_consumes_the_finfuzz_rows(corpus_report):
    payload = TierPayload(tier="synthetic", adversarial_rows=corpus_report.adversarial_rows())
    result = evaluate_tier(payload)
    metrics = result["metrics"]  # tier_report emits serialisable dicts, one per metric
    assert set(ADVERSARIAL_METRICS) <= set(metrics)
    assert metrics["mutation_detection_recall"]["value"] == pytest.approx(
        corpus_report.overall["recall"]
    )
    assert metrics["mutation_detection_precision"]["value"] == pytest.approx(
        corpus_report.overall["precision"]
    )
    assert metrics["mutation_detection_false_positive_rate"]["value"] == pytest.approx(
        corpus_report.overall["false_positive_rate"]
    )
    # Per-error-type recall has to survive in the note, or a strong average could
    # hide a blind spot.
    assert "numeric=" in metrics["mutation_detection_recall"]["note"]
    # The adversarial group is its own bucket, so one tier's benchmark report can
    # show it beside the base metrics without a second pass.
    assert set(result["groups"]["adversarial"]) == set(ADVERSARIAL_METRICS)


def test_card11_marks_the_adversarial_group_as_unmeasured_without_rows():
    payload = TierPayload(tier="synthetic")
    metrics = evaluate_tier(payload)["metrics"]
    assert all(metrics[name]["value"] is None for name in ADVERSARIAL_METRICS)
    assert all(metrics[name]["note"] == "no_adversarial_rows" for name in ADVERSARIAL_METRICS)


# --- §15 component baseline: the "--set synthetic 包含 finfuzz 报告"路径 ---------- #
def test_baseline_component_and_filename_match_the_registry():
    """注册表与生产者必须指向同一个文件名，否则基线会静默变成孤儿。

    这条不变量只能靠"两边都 import 同一个常量"实现，所以生产者侧导出
    ``COMPONENT`` / ``BASELINE_FILENAME``，注册表引用的就是它们的字面值。
    """
    from scripts.run_benchmark_v2 import COMPONENT_BASELINES

    registered = {name: filename for name, filename, _count in COMPONENT_BASELINES}
    assert registered[COMPONENT] == BASELINE_FILENAME


def test_baseline_metrics_are_card11_definitions_not_a_second_implementation(
    corpus_report,
):
    """四项数值必须与 CARD-11 的 calculator 逐格相同。

    组件用自己的算术重新发布一个已有指标，正是 CARD-11 归并闸门要拦的事；
    所以这里不是"看起来差不多"，是要求出自同一个函数。
    """
    from src.cn.benchmark_v2 import adversarial_metrics

    expected = {
        name: metric.to_dict()
        for name, metric in adversarial_metrics(corpus_report.adversarial_rows()).items()
    }
    assert baseline_metrics(corpus_report) == expected
    assert set(expected) == set(ADVERSARIAL_METRICS)

    payload = baseline_payload(corpus_report)
    assert payload["component"] == COMPONENT
    assert payload["synthetic"] is True and payload["real_model"] is False
    # The disclaimer travels with the numbers; a bare 0.98 without it is how a
    # synthetic detection rate gets quoted as a real-world one.
    assert "真实研报" in payload["disclaimer"]
    assert payload["case_count"] == len(corpus_report.rows)
    assert payload["operators_run"] == list(corpus_report.operators_run)


def test_synthetic_tier_aggregates_the_finfuzz_baseline(corpus_report, tmp_path):
    """真走一遍聚合器：写文件 → 载入 → build_report，指标必须带上来源。"""
    from scripts.run_benchmark_v2 import component_case_count, load_components

    paths = write_baseline(corpus_report, tmp_path)
    assert Path(paths["json"]).name == BASELINE_FILENAME

    components = load_components(tmp_path)
    assert COMPONENT in components
    assert component_case_count(COMPONENT, components[COMPONENT], None) == len(
        corpus_report.rows
    )

    report = build_report(components=components, component_case_count=len(corpus_report.rows))
    tier = report["tiers"][0]
    measured = {
        name: entry
        for name, entry in tier["groups"]["adversarial"].items()
        if entry["value"] is not None
    }
    assert set(measured) == set(ADVERSARIAL_METRICS)
    # Attributed to the component, not to "computed": an unattributed number is one
    # nobody can trace back to a corpus.
    assert all(entry["source"] == COMPONENT for entry in measured.values())
    assert measured["mutation_detection_recall"]["value"] == pytest.approx(
        corpus_report.overall["recall"]
    )


def test_baseline_markdown_names_the_unmeasured_operators(corpus_report):
    text = format_baseline_markdown(corpus_report)
    assert "N/A" in text  # the placeholder stays visible
    for name in corpus_report.per_error_type:
        if corpus_report.per_error_type[name].get("recall") is None:
            assert f"`{name}`" in text
    assert "真实研报" in text


def test_report_renders_and_serialises(corpus_report):
    text = render_markdown(corpus_report)
    assert "Per-error-type recall" in text
    assert "Extractor vs checker" in text
    assert NOT_MEASURED in text
    assert corpus_report.synthetic and not corpus_report.real_model


def test_outputs_declare_synthetic_corpus(cases, tmp_path):
    from src.cn.finfuzz import write_report

    report, _ = run_corpus(cases)
    paths = write_report(report, tmp_path)
    payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert payload["synthetic"] is True
    assert payload["real_model"] is False
    assert payload["adversarial_rows"]
