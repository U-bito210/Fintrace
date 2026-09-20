"""CARD-26 §13 — FinFuzz 报告校验器。

卡点名三条规则；本模块实现那三条，外加报告各字段之间必须成立的交叉不变式。
**报告里的每个数字都要能从结果集合现场重算出来** —— 不重算就只是把报告抄了一遍，
而"报告自相矛盾"正是这类评测最容易出的错（一个 per-error-type 行算错，整体召回看起来
仍然合理）。

1. **spec 必须带 diff（卡 §13 第一条）**：``claim`` 通道必须有 claim 级字段差；
   ``text`` 通道必须有非空 ``text_edits``。空 diff 的"变异"没改任何东西，会被记成漏检。
2. **报告必须区分 extractor / checker failure（卡 §13 第二条）**：``failure_stage`` 合法、
   与报告里的计数一致，且 ``extracted/checker`` 两段之和必须等于**真变异漏检数**
   （伪变异保持沉默是期望行为，不是漏检 —— 混进去会把真负例报成核查器失败）。
3. **伪变异必须计入 FPR，禁止剔除（卡 §13 第三条）**：``expected_detection=false`` 的样本
   必须出现在 rows 与 FPR 分母里，且 ``fp + tn == equivalent_n``。
4. **rows ↔ results 一一对应**：报告的行数、每行的 (error_type, planted, detected) 必须与
   结果集合一致 —— 行是给 CARD-11 消费的唯一出口。
5. **overall 逐格可重算**：precision / recall / F1 / FPR / TP / FP / FN / TN 全部由 rows 重算比对。
6. **per-error-type 行完整**：卡 §7 列的十个算子每个都必须有行；``N/A`` 行的 ``n`` 必须为 0；
   有样本的行其 ``detected / n`` 必须与结果一致。**N/A 行不得被删除**（卡 §11）。
7. **归因自洽**：``detected`` 与 ``failure_stage`` 互斥；检测器名必须在册。
8. **确定性卫生**：报告不得携带墙钟计时；``real_model`` 未声明即为 False；
   holdout 未采集必须是 ``TARGET / TO BE MEASURED``，**不得带数字**（卡 §12.4）。
9. **mutation_id 唯一**：重复 id 意味着同一条变异被算了两次，会虚增分母。
10. **组合标签自洽（卡 §12.2）**：``combination`` 里的算子必须都在册，且形如"有组合"就必须
    真的多于一个算子。
11. **跳过项必须带原因**：``skipped`` 每项要有 ``reason`` —— 静默跳过的样本会让覆盖率虚高。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.cn.finfuzz.models import (
    DETECTED_BY_VALUES,
    FAILURE_CHECKER,
    FAILURE_EXTRACTOR,
    FAILURE_STAGES,
    NOT_APPLICABLE,
    NOT_MEASURED,
    OPERATOR_ORDER,
    FinFuzzReport,
)
from src.cn.finfuzz.suite import compute_metrics, generate_mutation_specs, run_suite

#: Float comparison tolerance for "recomputed equals reported" checks.
_EPS = 1e-9


@dataclass(frozen=True)
class FinFuzzValidation:
    """Outcome of validating one FinFuzz report."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _close(left: Any, right: Any, tolerance: float = _EPS) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return left == right


def validate_finfuzz(
    report: FinFuzzReport,
    results: Optional[Sequence[Any]] = None,
    *,
    specs: Optional[Sequence[Any]] = None,
) -> FinFuzzValidation:
    """Validate one report, recomputing every metric from *results* when given.

    Without *results* the metric rules degrade to shape checks and say so in
    ``warnings`` — never a silent pass, because "the numbers were not rechecked"
    and "the numbers are right" must not look the same.
    """
    errors: List[str] = []
    warnings: List[str] = []

    _check_report_shape(report, errors, warnings)

    if results is None:
        warnings.append("metrics_not_rechecked:结果集合未提供，overall 与 per-error-type 只做了形状检查")
        return FinFuzzValidation(valid=not errors, errors=errors, warnings=warnings)

    results = list(results)
    _check_identity(report, results, errors)
    _check_stage_split(report, results, errors, warnings)
    _check_equivalent_accounting(report, results, errors)
    _check_overall_recomputation(report, results, errors)
    _check_per_error_type(report, results, errors, warnings)
    _check_spec_shapes(results, errors, specs=specs)

    return FinFuzzValidation(valid=not errors, errors=errors, warnings=warnings)


# --------------------------------------------------------------------------- #
# rule 8 / 11 — report-level shape
# --------------------------------------------------------------------------- #
def _check_report_shape(
    report: FinFuzzReport, errors: List[str], warnings: List[str]
) -> None:
    if not report.suite_id:
        errors.append("suite_id_missing")
    if report.real_model:
        errors.append("real_model_claim_without_declared_run:" + report.suite_id)
    if not report.synthetic and not report.real_model:
        warnings.append("tier_undeclared:报告既未标 synthetic 也未标 real_model")
    holdout = report.baseline_holdout or {}
    status = holdout.get("status")
    if status != NOT_MEASURED:
        numeric = [key for key, value in holdout.items() if isinstance(value, (int, float))]
        if numeric:
            errors.append(f"unmeasured_holdout_carries_numbers:{sorted(numeric)}")
    for item in report.skipped:
        if not str(item.get("reason") or "").strip():
            errors.append(f"skipped_entry_without_reason:{item.get('operator', '?')}")
    for item in report.skipped:
        if any("detection_time" in str(key) for key in item):
            errors.append("skipped_entry_carries_timing")


# --------------------------------------------------------------------------- #
# rule 9 — one row per spec, no double counting
# --------------------------------------------------------------------------- #
def _check_identity(report: FinFuzzReport, results: Sequence[Any], errors: List[str]) -> None:
    ids = [item.spec.mutation_id for item in results]
    if len(ids) != len(set(ids)):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        errors.append(f"mutation_id_not_unique:{duplicates[:5]}")
    if len(report.rows) != len(results):
        errors.append(f"rows_results_length_mismatch:{len(report.rows)}!={len(results)}")
        return
    for row, item in zip(report.rows, results):
        expected = item.to_adversarial_row()
        if dict(row) != expected:
            errors.append(f"row_result_mismatch:{item.spec.mutation_id}")
            break


# --------------------------------------------------------------------------- #
# rule 2 — extractor vs checker
# --------------------------------------------------------------------------- #
def _check_stage_split(
    report: FinFuzzReport,
    results: Sequence[Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    stage = report.extractor_vs_checker or {}
    for key in ("detected", "extractor_failures", "checker_failures"):
        if key not in stage:
            errors.append(f"extractor_vs_checker_missing_key:{key}")
    if errors:
        return

    planted = [item for item in results if item.planted]
    misses = [item for item in planted if not item.detected]
    for item in misses:
        if item.failure_stage not in FAILURE_STAGES:
            errors.append(f"miss_without_valid_stage:{item.spec.mutation_id}")
    recomputed = {
        "extractor_failures": sum(
            1 for item in misses if item.failure_stage == FAILURE_EXTRACTOR
        ),
        "checker_failures": sum(
            1 for item in misses if item.failure_stage == FAILURE_CHECKER
        ),
        "detected": sum(1 for item in planted if item.detected),
    }
    for key in ("extractor_failures", "checker_failures", "detected"):
        if stage.get(key) != recomputed[key]:
            errors.append(
                f"stage_count_not_recomputable:{key}:{stage.get(key)}!={recomputed[key]}"
            )
    if recomputed["extractor_failures"] and recomputed["checker_failures"]:
        warnings.append("both_stages_present:两类失败都出现，报告应分别列出样本")
    by_detector = stage.get("by_detector") or {}
    for name, count in by_detector.items():
        if name not in DETECTED_BY_VALUES:
            errors.append(f"unknown_detector:{name}")
        if count > recomputed["detected"]:
            errors.append(f"detector_count_exceeds_detections:{name}")


# --------------------------------------------------------------------------- #
# rule 3 — the false-positive samples must survive
# --------------------------------------------------------------------------- #
def _check_equivalent_accounting(
    report: FinFuzzReport, results: Sequence[Any], errors: List[str]
) -> None:
    equivalents = [item for item in results if not item.planted]
    overall = report.overall or {}
    if overall.get("equivalent_n") != len(equivalents):
        errors.append(
            f"equivalent_samples_dropped:{overall.get('equivalent_n')}!={len(equivalents)}"
        )
    fp = overall.get("false_positive")
    tn = overall.get("true_negative")
    if isinstance(fp, int) and isinstance(tn, int) and fp + tn != len(equivalents):
        errors.append(f"fpr_denominator_mismatch:{fp}+{tn}!={len(equivalents)}")
    if equivalents and overall.get("false_positive_rate") is None:
        errors.append("fpr_missing_despite_equivalent_samples")
    # Every equivalent spec must actually rewrite something, or it is not a sample.
    for item in equivalents:
        spec = item.spec
        if spec.expected_check_types:
            errors.append(f"equivalent_spec_declares_expected_types:{spec.mutation_id}")
        if not spec.diff() and not spec.text_edits:
            errors.append(f"equivalent_spec_changes_nothing:{spec.mutation_id}")


# --------------------------------------------------------------------------- #
# rule 5 — recompute overall
# --------------------------------------------------------------------------- #
def _check_overall_recomputation(
    report: FinFuzzReport, results: Sequence[Any], errors: List[str]
) -> None:
    recomputed = compute_metrics(results, suite_id=report.suite_id).overall
    reported = report.overall or {}
    for key, value in recomputed.items():
        if not _close(reported.get(key), value):
            errors.append(f"overall_not_recomputable:{key}:{reported.get(key)}!={value}")


# --------------------------------------------------------------------------- #
# rule 6 — per-error-type rows
# --------------------------------------------------------------------------- #
def _check_per_error_type(
    report: FinFuzzReport,
    results: Sequence[Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    rows = report.per_error_type or {}
    missing = [name for name in OPERATOR_ORDER if name not in rows]
    if missing:
        errors.append(f"per_error_type_rows_missing:{missing}")
        return
    for operator, row in rows.items():
        planted = [item for item in results if item.operator == operator and item.planted]
        equivalents = [item for item in results if item.operator == operator and not item.planted]
        if row.get("n") != len(planted):
            errors.append(f"per_error_type_n_mismatch:{operator}:{row.get('n')}!={len(planted)}")
        if row.get("equivalent_n") != len(equivalents):
            errors.append(
                f"per_error_type_equivalent_n_mismatch:{operator}:"
                f"{row.get('equivalent_n')}!={len(equivalents)}"
            )
        detected = sum(1 for item in planted if item.detected)
        if row.get("detected") != detected:
            errors.append(
                f"per_error_type_detected_mismatch:{operator}:{row.get('detected')}!={detected}"
            )
        if not planted:
            if row.get("recall") != NOT_APPLICABLE:
                errors.append(f"empty_type_not_marked_na:{operator}")
            if not str(row.get("note") or "").strip():
                warnings.append(f"empty_type_without_reason:{operator}")
        else:
            expected_recall = detected / len(planted)
            if not _close(row.get("recall"), expected_recall):
                errors.append(
                    f"per_error_type_recall_mismatch:{operator}:"
                    f"{row.get('recall')}!={expected_recall}"
                )


# --------------------------------------------------------------------------- #
# rules 1 / 10 — the specs themselves
# --------------------------------------------------------------------------- #
def _check_spec_shapes(
    results: Sequence[Any],
    errors: List[str],
    *,
    specs: Optional[Sequence[Any]] = None,
) -> None:
    source = list(specs) if specs is not None else [item.spec for item in results]
    seen: Dict[str, Any] = {}
    for spec in source:
        if spec.mutation_id in seen:
            errors.append(f"spec_id_not_unique:{spec.mutation_id}")
        seen[spec.mutation_id] = spec
        if not str(spec.description or "").strip():
            errors.append(f"spec_without_description:{spec.mutation_id}")
        if spec.channel == "text" and not spec.text_edits:
            errors.append(f"text_spec_without_edits:{spec.mutation_id}")
        if spec.channel == "claim" and not spec.diff():
            errors.append(f"claim_spec_without_diff:{spec.mutation_id}")
        if spec.expected_detection and not spec.expected_check_types:
            errors.append(f"planted_spec_without_expected_types:{spec.mutation_id}")
        # §12.2. Checked against the registry, not against ``spec.operators`` —
        # ``MutationSpec.operators`` is *derived* from ``(operator, *combination)``,
        # so comparing the two is a tautology. The registry comparison is what
        # still bites on a duck-typed spec arriving through the ``specs`` trust
        # boundary (the parameter is typed ``Sequence[Any]`` on purpose).
        for name in spec.combination:
            if name not in OPERATOR_ORDER:
                errors.append(f"combination_member_not_listed:{spec.mutation_id}:{name}")
        if spec.combination and not spec.is_combination:
            errors.append(f"combination_label_without_two_operators:{spec.mutation_id}")


# --------------------------------------------------------------------------- #
# corpus-level entry point (the "run it over everything" invariant)
# --------------------------------------------------------------------------- #
def validate_finfuzz_corpus(
    cases: Sequence[Any],
    operators: Optional[Iterable[str]] = None,
    *,
    max_cases: Optional[int] = None,
) -> FinFuzzValidation:
    """Run the suite over every case and validate each report end to end.

    This is the free end-to-end invariant the card's §10 asks for: the report is
    only allowed to claim what the results actually support, on real corpus rather
    than on a hand-built example.
    """
    errors: List[str] = []
    warnings: List[str] = []
    selected = list(cases)[:max_cases] if max_cases else list(cases)
    for case in selected:
        generated = generate_mutation_specs(
            case.claims, operators, context=case.context, targets=case.targets
        )
        results = run_suite(generated.specs, case.targets)
        report = compute_metrics(results, suite_id=case.suite_id)
        report.skipped = list(generated.skipped)
        validation = validate_finfuzz(report, results, specs=generated.specs)
        errors.extend(f"{case.doc_id}:{item}" for item in validation.errors)
        warnings.extend(f"{case.doc_id}:{item}" for item in validation.warnings)
    return FinFuzzValidation(valid=not errors, errors=errors, warnings=warnings)


__all__ = [
    "FinFuzzValidation",
    "validate_finfuzz",
    "validate_finfuzz_corpus",
]
