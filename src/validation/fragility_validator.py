"""CARD-24 §13 — validator for the Thesis Fragility report.

The card names three rules; this module implements those plus the invariants the
report's own cross-fields depend on. Every rule is a *real* check — the
cut-set rule in particular **re-runs the engine's propagation** rather than
trusting the enumeration that produced the set.

1. **反解断言**（卡 §13 第一条，核心）：对每个最小割集，在图的**副本**上施加
   破坏并重算传播，该 Thesis 必须变成 BLOCKED。枚举算法写错时这一条会立刻响 ——
   它是"纸面割集"与"真的割得动"之间的唯一屏障。
2. **极小性**：割集的任何**真子集**都不得同样是割集。卡写的是"最小割集"，
   只验"能割断"是不够的：一组多余的节点同样能割断，但那不是最小。
3. **阈值来源白名单**（卡 §13 第二条）：``numeric_thresholds`` 每项必须有
   在册的 ``source_type`` 与非空的 ``source_ref``，且 ``source_ref`` 必须在图上
   （或假设注册表里）**真的解析得了** —— 顶层类型限制已经由构造期保证，这条查的是
   数据层面的依据是否真的存在。
4. **`unresolved_assumptions` 必须存在**（卡 §13 第三条，允许为空数组）。
5. **单点一致性**：标了 ``is_single_point`` 的节点，对它 ``single_point_for`` 的
   每个 Thesis，必须真的存在一个单元素割集 —— 否则"单点失效"就成了一句口号。
6. **割集完整性**：割集必须**恰好**是"单独移除即可击穿该 Thesis"的全部节点
   （真值由引擎当场跑传播算出，不看报告里任何字段）。这条把"最小割集 = 单节点
   击穿集"这个由 CARD-09 传播表推出的结论钉成不变式。
7. **可解析性**：割集里的每个节点必须能在图上解析；关键依赖里的悬空节点降级为
   warning（它本来就是 CARD-09 检测器该报的东西，不该在这里当错误）。
8. **数值项逐格可重算**（沿用 CARD-08 的做法）：``distance_to_invalidation``
   必须能从 ``resolved_current_value`` + ``comparison`` + ``threshold`` 重算出来，
   且 ``at_threshold`` / ``violated`` 与距离符号一致；不可评估的项必须给出原因。
9. **报告状态自洽**：``unavailable`` 必须带原因；``pruned`` / ``cut_sets_truncated``
   为真时状态不得是 ``ok``。
10. **缺输入 → warning，绝不静默通过**：没有阈值、没有假设注册表，都要在
    warnings 里留下痕迹（并与报告自己的 warnings 对齐）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.cn.fragility import (
    COMPARISON_GTE,
    COMPARISON_GT,
    FragilityReport,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    THRESHOLD_SOURCE_TYPES,
    cut_blocks_thesis,
    threshold_source_resolves,
)
from src.cn.graph import THESIS_BLOCKED, ClaimGraph, Thesis
from src.cn.graph.propagation import thesis_status

#: 允许的相对误差（距离重算时），与引擎同量级。
_TOLERANCE = 1e-9


@dataclass(frozen=True)
class FragilityValidation:
    """Outcome of validating one fragility report (or a corpus of them)."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_fragility(
    graph: ClaimGraph,
    report: FragilityReport,
    *,
    assumptions: Any = None,
    extra_evidence_ids: Iterable[str] = (),
) -> FragilityValidation:
    """Validate one report against the graph it was built from."""
    errors: List[str] = []
    warnings: List[str] = []
    payload = report.to_dict()

    _check_cut_sets(graph, report, errors)
    _check_cut_completeness(graph, report, errors, warnings)
    _check_thresholds(graph, report, errors, warnings, assumptions=assumptions,
                      extra_evidence_ids=extra_evidence_ids)
    _check_unresolved_key(payload, errors)
    _check_single_point_consistency(report, errors)
    _check_node_resolution(graph, report, errors, warnings)
    _check_status_consistency(report, errors)
    _check_input_transparency(graph, report, warnings, assumptions=assumptions)

    return FragilityValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_fragility_corpus(
    graph: ClaimGraph,
    reports: Sequence[FragilityReport],
    *,
    assumptions: Any = None,
) -> FragilityValidation:
    """Validate several reports against one graph — a free end-to-end invariant."""
    errors: List[str] = []
    warnings: List[str] = []
    for report in reports:
        result = validate_fragility(graph, report, assumptions=assumptions)
        label = report.thesis_id or ",".join(report.thesis_ids) or "<no-thesis>"
        errors.extend(f"{label}:{item}" for item in result.errors)
        warnings.extend(f"{label}:{item}" for item in result.warnings)
    return FragilityValidation(valid=not errors, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _check_cut_sets(graph: ClaimGraph, report: FragilityReport, errors: List[str]) -> None:
    by_thesis = report.cut_sets_by_thesis or {}
    for thesis_id, cuts in sorted(by_thesis.items()):
        thesis = graph.theses.get(thesis_id)
        if thesis is None:
            errors.append(f"cut_sets_for_unknown_thesis:{thesis_id}")
            continue
        already_blocked = thesis_status(graph, thesis) == THESIS_BLOCKED
        for cut in cuts:
            if not cut:
                errors.append(f"empty_cut_set:{thesis_id}")
                continue
            if not cut_blocks_thesis(graph, thesis_id, cut):
                errors.append(f"cut_set_does_not_block:{thesis_id}:{'+'.join(cut)}")
                continue
            for size in range(1, len(cut)):
                for subset in _combinations(cut, size):
                    if cut_blocks_thesis(graph, thesis_id, subset):
                        errors.append(
                            f"cut_set_not_minimal:{thesis_id}:{'+'.join(cut)}"
                            f"（真子集 {'+'.join(subset)} 已可割断）"
                        )
                        break
        if (
            not cuts
            and not already_blocked
            and thesis.required_claims()
            and not report.cut_sets_truncated
        ):
            errors.append(f"no_cut_set_for_live_thesis:{thesis_id}")


def _combinations(items: Sequence[str], size: int) -> List[List[str]]:
    import itertools

    return [list(item) for item in itertools.combinations(list(items), size)]


#: 完整性格子检查最多在多少个节点的图上逐节点试割；超过就跳过并留 warning。
_COMPLETENESS_NODE_BUDGET = 80


def _check_cut_completeness(
    graph: ClaimGraph, report: FragilityReport, errors: List[str], warnings: List[str]
) -> None:
    """割集**既不多也不少**：恰好是"单独移除即可击穿该 Thesis"的全部节点.

    这条规则是施工时最值钱的一条 —— 它把"最小割集 = 单节点击穿集"这个由
    CARD-09 传播表推出的结论**钉成不变式**：一旦有人把枚举规则改回"命中集"
    那类错误形式，或者漏掉某条链上的节点，这里立刻会响。节点的真值由引擎
    自己跑传播算出来，不依赖报告里任何字段。
    """
    universe_all = sorted(set(graph.claims) | set(graph.evidence_index) | set(graph.calculations))
    for thesis_id, cuts in sorted((report.cut_sets_by_thesis or {}).items()):
        thesis = graph.theses.get(thesis_id)
        if thesis is None:
            continue
        if thesis_status(graph, thesis) == THESIS_BLOCKED:
            continue
        if report.cut_sets_truncated:
            warnings.append(f"cut_completeness_skipped:{thesis_id}:truncated")
            continue
        if len(universe_all) > _COMPLETENESS_NODE_BUDGET:
            warnings.append(f"cut_completeness_skipped:{thesis_id}:graph_too_large")
            continue
        emitted = {node for cut in cuts for node in cut}
        expected = {
            node for node in universe_all if cut_blocks_thesis(graph, thesis_id, [node])
        }
        if emitted != expected:
            errors.append(
                f"cut_set_universe_mismatch:{thesis_id}"
                f":missing={sorted(expected - emitted)}:extra={sorted(emitted - expected)}"
            )


def _check_thresholds(
    graph: ClaimGraph,
    report: FragilityReport,
    errors: List[str],
    warnings: List[str],
    *,
    assumptions: Any,
    extra_evidence_ids: Iterable[str],
) -> None:
    for item in report.numeric_thresholds:
        node_id = str(item.get("node_id") or "")
        source_type = str(item.get("source_type") or "")
        source_ref = str(item.get("source_ref") or "")
        if source_type not in THRESHOLD_SOURCE_TYPES:
            errors.append(f"threshold_source_type_not_whitelisted:{node_id}:{source_type or '<empty>'}")
        if not source_ref:
            errors.append(f"threshold_missing_source_ref:{node_id}")
        else:
            resolved, _note = threshold_source_resolves(
                graph, source_ref, assumptions, extra_evidence_ids=extra_evidence_ids
            )
            if not resolved:
                errors.append(f"threshold_source_unresolved:{node_id}:{source_ref}")
        if not str(item.get("unit") or "").strip():
            warnings.append(f"threshold_unit_missing:{node_id}")
        _check_threshold_evaluation(item, node_id, errors)

    for item in report.rejected_thresholds:
        warnings.append(
            f"threshold_rejected:{item.get('node_id')}:{item.get('reason')}"
        )


def _check_threshold_evaluation(item: Dict[str, Any], node_id: str, errors: List[str]) -> None:
    evaluable = bool(item.get("evaluable"))
    distance = item.get("distance_to_invalidation")
    if not evaluable:
        if distance is not None:
            errors.append(f"threshold_unevaluable_but_has_distance:{node_id}")
        if not str(item.get("eval_reason") or "").strip():
            errors.append(f"threshold_unevaluable_without_reason:{node_id}")
        return
    if distance is None:
        errors.append(f"threshold_evaluable_without_distance:{node_id}")
        return
    current = item.get("resolved_current_value")
    threshold = item.get("threshold")
    comparison = str(item.get("comparison") or "")
    if current is None or threshold is None:
        errors.append(f"threshold_missing_recompute_inputs:{node_id}")
        return
    expected = (
        float(current) - float(threshold)
        if comparison in (COMPARISON_GTE, COMPARISON_GT)
        else float(threshold) - float(current)
    )
    if abs(expected) <= _TOLERANCE * max(1.0, abs(float(threshold))):
        expected = 0.0
    if abs(float(distance) - expected) > _TOLERANCE * max(1.0, abs(expected)):
        errors.append(
            f"threshold_distance_not_reproducible:{node_id}:{distance}!={expected}"
        )
    if bool(item.get("at_threshold")) != (float(distance) == 0.0):
        errors.append(f"threshold_at_threshold_inconsistent:{node_id}")
    if bool(item.get("violated")) != (float(distance) < 0):
        errors.append(f"threshold_violated_inconsistent:{node_id}")


def _check_unresolved_key(payload: Dict[str, Any], errors: List[str]) -> None:
    if "unresolved_assumptions" not in payload:
        errors.append("unresolved_assumptions_key_missing")
    elif not isinstance(payload["unresolved_assumptions"], list):
        errors.append("unresolved_assumptions_not_a_list")


def _check_single_point_consistency(report: FragilityReport, errors: List[str]) -> None:
    by_thesis = report.cut_sets_by_thesis or {}
    for item in report.critical_dependencies:
        if not item.get("is_single_point"):
            continue
        node_id = str(item.get("node_id"))
        for thesis_id in item.get("single_point_for") or ():
            cuts = by_thesis.get(str(thesis_id))
            if cuts is None:
                errors.append(f"single_point_for_unknown_thesis:{node_id}:{thesis_id}")
                continue
            if [node_id] not in [list(cut) for cut in cuts]:
                errors.append(f"single_point_without_singleton_cut:{node_id}:{thesis_id}")


def _check_node_resolution(
    graph: ClaimGraph, report: FragilityReport, errors: List[str], warnings: List[str]
) -> None:
    for thesis_id, cuts in sorted((report.cut_sets_by_thesis or {}).items()):
        for cut in cuts:
            for node_id in cut:
                if _resolves(graph, node_id):
                    continue
                errors.append(f"cut_set_node_unresolved:{thesis_id}:{node_id}")
    for item in report.critical_dependencies:
        if str(item.get("node_type")) == "missing" or not _resolves(graph, str(item.get("node_id"))):
            warnings.append(f"critical_dependency_unresolved:{item.get('node_id')}")
    for plan in report.monitoring_metrics:
        if not _resolves(graph, str(plan.get("dependency"))):
            warnings.append(f"monitoring_dependency_unresolved:{plan.get('dependency')}")


def _resolves(graph: ClaimGraph, node_id: str) -> bool:
    return (
        node_id in graph.claims
        or node_id in graph.evidence_index
        or node_id in graph.calculations
    )


def _check_status_consistency(report: FragilityReport, errors: List[str]) -> None:
    if report.status == STATUS_UNAVAILABLE and not str(report.unavailable_reason).strip():
        errors.append("unavailable_without_reason")
    if report.status == STATUS_OK and (report.pruned or report.cut_sets_truncated):
        errors.append("status_ok_but_results_truncated")


def _check_input_transparency(
    graph: ClaimGraph, report: FragilityReport, warnings: List[str], *, assumptions: Any
) -> None:
    if not report.numeric_thresholds and not report.rejected_thresholds:
        warnings.append("numeric_fragility_absent")
    referenced = sorted(
        {
            str(assumption_id)
            for thesis in graph.theses.values()
            for claim_id in thesis.claim_ids
            if claim_id in graph.claims
            for assumption_id in graph.claims[claim_id].assumption_ids
        }
    )
    if assumptions is None and referenced:
        warnings.append("assumption_registry_absent")
    if referenced and not report.unresolved_assumptions:
        warnings.append("assumptions_referenced_but_none_reported")


__all__ = [
    "FragilityValidation",
    "validate_fragility",
    "validate_fragility_corpus",
]
