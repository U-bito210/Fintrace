"""CARD-25 §13 — Temporal Revalidation 报告校验器。

卡点名三条规则；本模块实现那三条，外加报告各字段之间必须成立的交叉不变式。
每条都是**真检查** —— 其中两条会**现场重跑引擎**，而不是相信报告里写的数。

1. **append-only（卡 §13 第一条，核心）**：被替代的事实**不得被物理覆盖**。
   对每条 ``SUPERSEDED`` 条目，血缘历史里必须仍能找到它**被替代之前**的那条记录。
   这条是"旧值永不覆盖、历史可回放"的唯一屏障 —— 血缘写错成"就地改状态"时它会立刻响。
2. **可回溯（卡 §13 第二条）**：``event → state_diff`` 必须一一对应。
   逐条 diff 必须有合法的 ``before``/``after`` 且**两者不同**（相同即噪音，说明重算把
   没变的东西也报了出来）。反向的：被替代的旧 id 必须出现在 ``affected_subgraph`` 里。
3. **超集（卡 §13 第三条）**：``affected_subgraph`` 必须是 ``state_diff`` 的**超集**，
   且必须包含 ``changed_nodes``。
4. **增量 == 全量（卡 §16 的 same deterministic result）**：报告若自认
   ``results_identical=False``，则必须**已经回落全量**（``fallback``）并留下 warning；
   回落时 ``nodes_recomputed`` 必须等于 ``full_nodes``。**不一致却按 ok 交付**是最危险的形态。
5. **affected_subgraph 可复现**（给图时）：用引擎按 ``changed_nodes`` + 被替代旧 id
   现场重跑一次限深 BFS，结果必须与报告一致 —— 报告里的子图不能是手写的。
6. **血缘引用完整、无环**：``supersedes`` / ``superseded_by`` 必须指向在册节点，链必须终止。
7. **三态未被压成一个**（卡 §3）：``event_kind`` 合法，且三态的传播语义两两可区分。
8. **impact 六键齐全**：卡 §4 列的六个字段一个不能少 —— 少一个就是报告没回答那个问题。
9. **动作词表合法**：``action_type`` / ``priority`` 必须在册（无自由文本）。
10. **状态自洽**：``unavailable`` 必带原因；``ok`` 不得带 fallback。
11. **缺输入 → warning，绝不静默通过**：图外域（估值/备忘段）为 ``None``、子图被深度截断，
    都必须在 warnings 里留下痕迹。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.cn.graph import (
    THESIS_BLOCKED,
    THESIS_STALE,
    THESIS_SUPPORTED,
    THESIS_WEAKENED,
    VALIDATION_BLOCKED,
    VALIDATION_PENDING,
    VALIDATION_STALE,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    ClaimGraph,
)
from src.cn.temporal import (
    ACTION_TYPES,
    CHANGE_KINDS,
    EVENT_EFFECTS,
    EXTERNAL_IMPACT_KEYS,
    IMPACT_KEYS,
    LINEAGE_SUPERSEDED,
    PRIORITIES,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    RevalidationReport,
    TemporalError,
    VersionLineage,
    subgraph_view,
)

#: 报告 ``after`` 字段允许出现的状态（claim 四态 + pending + thesis 四态）。
KNOWN_CLAIM_STATUSES = (
    VALIDATION_SUPPORTED,
    VALIDATION_STALE,
    VALIDATION_BLOCKED,
    VALIDATION_UNSUPPORTED,
    VALIDATION_PENDING,
)
KNOWN_THESIS_STATUSES = (THESIS_SUPPORTED, THESIS_WEAKENED, THESIS_BLOCKED, THESIS_STALE)

#: 子图重算的规模闸门：超过就不做可复现性重算（留 warning），避免大图上无谓开销。
_REPRODUCE_NODE_LIMIT = 400


@dataclass(frozen=True)
class TemporalValidation:
    """一份（或一批）时间重验证报告的校验结论。"""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _impact_of(report: RevalidationReport) -> Dict[str, Any]:
    impact = report.impact if isinstance(report.impact, dict) else {}
    return impact


def _superseded_old_ids(report: RevalidationReport) -> List[str]:
    """报告血缘里被替代的旧 id（用于重建 impact seeds）。"""
    out: List[str] = []
    for entry in report.lineage:
        if not isinstance(entry, dict):
            continue
        if entry.get("version_status") == LINEAGE_SUPERSEDED:
            fact_id = entry.get("fact_id")
            if fact_id:
                out.append(str(fact_id))
    return out


def validate_temporal(
    report: RevalidationReport,
    *,
    lineage: Optional[VersionLineage] = None,
    graph: Optional[ClaimGraph] = None,
) -> TemporalValidation:
    """校验一份报告；给了血缘/图就额外跑依赖血缘/图的那几条规则。"""
    errors: List[str] = []
    warnings: List[str] = []

    impact = _impact_of(report)
    state_diff = list(report.state_diff)
    affected = {str(node) for node in report.affected_subgraph}
    changed = {str(node) for node in report.changed_nodes}

    # -- 8. impact 六键 ---------------------------------------------------
    for key in IMPACT_KEYS:
        if key not in impact:
            errors.append(
                f"impact_key_missing:{key}（卡 §4 的六个字段少一个就是没回答那个问题）"
            )

    # -- 3. 超集 ----------------------------------------------------------
    for item in state_diff:
        node_id = str(item.get("node_id"))
        if node_id not in affected:
            errors.append(
                f"subgraph_not_superset_of_state_diff:{node_id}"
                "（卡 §13：affected_subgraph 必须是 state_diff 的超集）"
            )
    for node_id in sorted(changed):
        if node_id not in affected:
            errors.append(
                f"subgraph_missing_changed_node:{node_id}（改动的节点自己当然属于受影响）"
            )
    diff_nodes = {str(item.get("node_id")) for item in state_diff}
    for old_id in _superseded_old_ids(report):
        if old_id not in affected:
            errors.append(
                f"superseded_id_absent_from_subgraph:{old_id}"
                "（被替代的旧值才是下游依赖的那个，它不在子图里说明传播起点选错了）"
            )

    # -- 2. 可回溯 --------------------------------------------------------
    for item in state_diff:
        node_id = str(item.get("node_id"))
        node_type = str(item.get("node_type"))
        before = item.get("before")
        after = item.get("after")
        if not node_id:
            errors.append("state_diff_entry_without_node_id")
            continue
        if before == after:
            errors.append(
                f"state_diff_no_op:{node_id}:{before}"
                "（before == after 的条目是噪音：重算把没变的东西也报了）"
            )
        allowed = KNOWN_CLAIM_STATUSES if node_type == "claim" else KNOWN_THESIS_STATUSES
        if after not in allowed:
            errors.append(f"state_diff_unknown_after:{node_id}:{after!r}")
        if before is not None and before not in allowed:
            errors.append(f"state_diff_unknown_before:{node_id}:{before!r}")

    # -- 4. 增量 == 全量 ---------------------------------------------------
    comparison = report.full_vs_incremental if isinstance(report.full_vs_incremental, dict) else {}
    identical = comparison.get("results_identical")
    fell_back = comparison.get("fell_back")
    full_nodes = comparison.get("full_nodes")
    recomputed = comparison.get("nodes_recomputed")

    if identical is None or full_nodes is None or recomputed is None:
        # `unavailable` 本来就没做传播（种子全都解析不了 / 图上没 claim），
        # 对它要求"增量与全量对账"是无意义的——那会把一个**合法结果**判成缺陷。
        if report.status != STATUS_UNAVAILABLE:
            errors.append(
                "full_vs_incremental_incomplete"
                "（卡 §16 要求 nodes_recomputed / full_nodes / same result 三个都有）"
            )
    else:
        if not identical:
            if not fell_back:
                errors.append(
                    "incremental_full_mismatch_not_fell_back"
                    "（增量与全量不一致却没回落全量 —— 卡 §11 要求回落并记录）"
                )
            if report.fallback != "full_recomputation":
                errors.append(
                    "mismatch_without_fallback_marker"
                    "（不一致时 report.fallback 必须是 full_recomputation）"
                )
            if not any("mismatch" in warning for warning in report.warnings):
                errors.append(
                    "mismatch_without_warning（不一致必须留下 warning，不能只写在字段里）"
                )
        if fell_back:
            if recomputed != full_nodes:
                errors.append(
                    f"fallback_nodes_recomputed_wrong:{recomputed}!={full_nodes}"
                    "（回落全量后 nodes_recomputed 必须等于 full_nodes）"
                )
            if report.status == STATUS_OK:
                errors.append("fallback_but_status_ok（回落过全量就不该报 ok）")
        if full_nodes == 0:
            errors.append("full_nodes_is_zero（图上没有 claim 时不该产出常规报告）")

    # -- 7. 三态未被压成一个 ----------------------------------------------
    event = report.event if isinstance(report.event, dict) else {}
    kind = event.get("event_kind")
    if kind is not None and kind not in CHANGE_KINDS:
        errors.append(f"unknown_event_kind:{kind!r}")
    if sorted(EVENT_EFFECTS) != sorted(CHANGE_KINDS):
        errors.append("event_effects_incomplete（三态必须各有传播语义）")
    if len(set(EVENT_EFFECTS.values())) != len(EVENT_EFFECTS):
        errors.append(
            "event_effects_collapsed（卡 §3：三类变化的传播规则不能相同，不能全叫 conflict）"
        )

    # -- 9. 动作词表 ------------------------------------------------------
    for action in report.revalidation_actions:
        action_type = str(action.get("action_type"))
        priority = str(action.get("priority"))
        if action_type not in ACTION_TYPES:
            errors.append(f"unknown_action_type:{action_type!r}")
        if priority not in PRIORITIES:
            errors.append(f"unknown_action_priority:{priority!r}")
        if not str(action.get("target_id") or "").strip():
            errors.append(f"action_without_target:{action_type}")

    # -- 10. 状态自洽 -----------------------------------------------------
    if report.status == STATUS_UNAVAILABLE and not report.unavailable_reason:
        errors.append("unavailable_without_reason（不可用必须写明原因）")
    if report.status == STATUS_OK and report.fallback:
        errors.append("status_ok_but_fallback_set")
    if report.status == STATUS_UNAVAILABLE and state_diff:
        errors.append("unavailable_but_has_state_diff（不可用还报了一堆变更，自相矛盾）")

    # -- 11. 缺输入 → warning --------------------------------------------
    for key in EXTERNAL_IMPACT_KEYS:
        if impact.get(key) is None:
            if not any(key in warning for warning in report.warnings):
                errors.append(
                    f"external_domain_silent:{key}"
                    "（图外域没算就必须留 warning，不能安静地给 None）"
                )
    truncated = any("affected_subgraph_truncated" in warning for warning in report.warnings)
    if truncated and report.status == STATUS_OK:
        warnings.append(
            "subgraph_truncated_but_status_ok：子图被深度截断，建议核对是否漏算下游"
        )
    if not state_diff and not changed:
        warnings.append("empty_report：本次事件不影响任何节点（卡 §14 的空报告）")

    # -- 1. append-only（需要血缘）----------------------------------------
    if lineage is not None:
        for entry in lineage.entries():
            if entry.version_status != LINEAGE_SUPERSEDED:
                continue
            history = lineage.history(entry.fact_id)
            pre = [
                record
                for record in history
                if record.version_status != LINEAGE_SUPERSEDED
            ]
            if not pre:
                errors.append(
                    f"superseded_fact_overwritten:{entry.fact_id}"
                    "（血缘里找不到它被替代之前的状态 —— 旧值被物理覆盖了，"
                    "卡 §13 第一条禁止这样做）"
                )

        # -- 6. 引用完整 + 无环 ------------------------------------------
        for entry in lineage.records:
            for edge_name in ("supersedes", "superseded_by"):
                edge = getattr(entry, edge_name)
                if edge is not None and edge not in lineage:
                    errors.append(
                        f"lineage_dangling_{edge_name}:{entry.fact_id}->{edge}"
                        "（血缘边指向图外节点）"
                    )
        try:
            lineage.assert_acyclic()
        except TemporalError as exc:
            errors.append(f"lineage_cycle:{exc}")

        # 报告血缘片段必须与传入血缘一致（不能自己编状态）
        for snippet in report.lineage:
            if not isinstance(snippet, dict):
                continue
            fact_id = str(snippet.get("fact_id"))
            actual = lineage.get(fact_id)
            if actual is None:
                errors.append(f"report_lineage_unknown_fact:{fact_id}")
                continue
            if actual.version_status != snippet.get("version_status"):
                errors.append(
                    f"report_lineage_status_mismatch:{fact_id}:"
                    f"{snippet.get('version_status')}!={actual.version_status}"
                )

    # -- 5. affected_subgraph 可复现（需要图）------------------------------
    if graph is not None:
        if len(graph.claims) > _REPRODUCE_NODE_LIMIT:
            warnings.append(
                f"subgraph_reproduce_skipped:claims={len(graph.claims)}"
                f">{_REPRODUCE_NODE_LIMIT}"
            )
        else:
            seeds = sorted(changed | set(_superseded_old_ids(report)))
            if seeds:
                max_depth = _max_depth_from(report)
                try:
                    reproduced = set(subgraph_view(graph, seeds, max_depth=max_depth).nodes)
                except TemporalError as exc:
                    errors.append(f"subgraph_reproduce_failed:{exc}")
                else:
                    missing = sorted(
                        (reproduced | diff_nodes) - affected
                    )
                    if missing:
                        errors.append(
                            f"affected_subgraph_not_reproducible:{missing}"
                            "（按 changed_nodes + 被替代旧 id 现场重算得到的节点不在报告子图里）"
                        )
                    extra = sorted(affected - reproduced - diff_nodes)
                    if extra:
                        warnings.append(
                            f"affected_subgraph_has_extra_nodes:{extra[:5]}"
                            "（报告子图比现场重算更大；可能是传播起点更宽，供人工核对）"
                        )

    return TemporalValidation(
        valid=not errors, errors=errors, warnings=warnings
    )


def _max_depth_from(report: RevalidationReport) -> int:
    """从报告的 ``max_depth`` 参数还原（未记录时用引擎默认值）。"""
    from src.cn.temporal import DEFAULT_MAX_DEPTH

    for warning in report.warnings:
        if warning.startswith("affected_subgraph_truncated:max_depth="):
            token = warning.split("=", 1)[1].split("（", 1)[0].strip()
            try:
                return int(token)
            except ValueError:
                return DEFAULT_MAX_DEPTH
    return DEFAULT_MAX_DEPTH


def validate_temporal_corpus(
    reports: Iterable[RevalidationReport],
    *,
    lineages: Optional[Sequence[Optional[VersionLineage]]] = None,
    graphs: Optional[Sequence[Optional[ClaimGraph]]] = None,
) -> TemporalValidation:
    """对一批报告逐个校验并汇总（任何一条不过，整批不过）。"""
    errors: List[str] = []
    warnings: List[str] = []
    report_list = list(reports)
    lineage_list = list(lineages) if lineages is not None else [None] * len(report_list)
    graph_list = list(graphs) if graphs is not None else [None] * len(report_list)
    if lineages is not None and len(lineage_list) != len(report_list):
        raise TemporalError("lineages 与 reports 数量不一致")
    if graphs is not None and len(graph_list) != len(report_list):
        raise TemporalError("graphs 与 reports 数量不一致")

    for index, report in enumerate(report_list):
        outcome = validate_temporal(
            report,
            lineage=lineage_list[index] if index < len(lineage_list) else None,
            graph=graph_list[index] if index < len(graph_list) else None,
        )
        warnings.extend(f"[{index}] {item}" for item in outcome.warnings)
        if not outcome.valid:
            errors.extend(f"[{index}] {item}" for item in outcome.errors)

    return TemporalValidation(valid=not errors, errors=errors, warnings=warnings)


__all__ = [
    "KNOWN_CLAIM_STATUSES",
    "KNOWN_THESIS_STATUSES",
    "TemporalValidation",
    "validate_temporal",
    "validate_temporal_corpus",
]
