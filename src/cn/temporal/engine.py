"""CARD-25 — 增量重验证引擎（确定性，零 LLM）。

三件事，各有一个明确的判据：

1. **affected_subgraph** —— 从 ``changed_node_ids`` 沿**依赖方向**做限深 BFS
   （卡 §7）。深度受限，故"没走到"与"真的没有"必须分开报（``truncated``）。
2. **子图内有界重算** —— 只重算受影响子图里的 claim/thesis。
   这不是把全量结果裁一裁：**受影响集之外的状态在数学上不可能变**
   （下面 :func:`recompute_within` 的文档串有证明），所以有界重算与全量重算**同结果**。
   这条等式由 :func:`compare_full_vs_incremental` 每次现场验证，
   并由校验器钉成不变式；一旦不成立就**回落全量**并如实记录（卡 §11）。
3. **三态施加不同变更**（卡 §7 表）—— 见 :data:`EVENT_EFFECTS`。

**一个刻意的做法**：本模块**不调用** CARD-09 的 ``mark_stale`` / ``deactivate_evidence``。
那两个函数都会**立刻跑一次全量传播**（CARD-09 §6 要求"撤销后马上读到新状态"）。
若在这里用它们，所谓"增量"就变成了"先全量跑一遍再增量跑一遍"，
卡 §16 要对比的 ``nodes_recomputed`` 也就失去意义。CARD-09 的文档串明确写着
**``stale_claims`` 这个列表**才是留给 CARD-25 的接缝 —— 本模块直接往列表里追加标记，
把传播留给下面这一次有界重算。
"""

from __future__ import annotations

import copy
import dataclasses
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.cn.graph import (
    TARGET_CALCULATION,
    THESIS_BLOCKED,
    THESIS_STALE,
    THESIS_SUPPORTED,
    THESIS_WEAKENED,
    VALIDATION_BLOCKED,
    VALIDATION_STALE,
    VERIFICATION_CONFLICTED,
    ClaimGraph,
)
from src.cn.graph.propagation import (
    claim_status,
    downstream_closure,
    propagate_state,
    resolve_targets,
    thesis_status,
)

from .lineage import VersionLineage
from .models import (
    ACTION_ARBITRATE_CONFLICT,
    ACTION_RECHECK_THESIS,
    ACTION_REFRESH_CALCULATION,
    ACTION_REFRESH_MEMO_SECTION,
    ACTION_REFRESH_VALUATION,
    ACTION_REVALIDATE_CLAIM,
    ACTION_REVIEW_SUPERSESSION,
    CHANGE_DATA_CONFLICT,
    CHANGE_NEW_INFORMATION,
    CHANGE_VERSION_SUPERSESSION,
    DEFAULT_MAX_DEPTH,
    EVENT_CORRECTION,
    EVENT_RESTATEMENT,
    EXTERNAL_IMPACT_KEYS,
    LINEAGE_SUPERSEDED,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    REASON_CORRECTION,
    REASON_DEPRECATION,
    REASON_RESTATEMENT,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    TemporalError,
    TemporalEvent,
    UNAVAILABLE_NO_CLAIMS,
    UNAVAILABLE_UNKNOWN_NODES,
    RevalidationReport,
)

#: 每类事件对图施加什么。三者**必须可区分**（卡 §3："不能全叫 conflict"）。
EVENT_EFFECTS: Dict[str, str] = {
    CHANGE_VERSION_SUPERSESSION: "old_value_superseded_claims_need_refresh",
    CHANGE_DATA_CONFLICT: "unresolved_conflict_blocks_downstream",
    CHANGE_NEW_INFORMATION: "new_period_claims_need_refresh",
}

#: 回落标记（卡 §11）。
FALLBACK_FULL_RECOMPUTATION = "full_recomputation"

REASON_NEEDS_REFRESH = "needs_refresh"
REASON_SUPERSEDED = "superseded"
REASON_NEW_PERIOD = "new_period"


# ---------------------------------------------------------------------------
# 依赖方向
# ---------------------------------------------------------------------------
def dependents_map(graph: ClaimGraph) -> Dict[str, Set[str]]:
    """``target_id -> {依赖它的 claim_id}``。

    与 CARD-09 ``downstream_closure`` 用的是同一个 :func:`resolve_targets`，
    所以"谁依赖谁"只有一处定义。
    """
    dependents: Dict[str, Set[str]] = {}
    for claim in graph.claims.values():
        for _target_type, target_id, _role in resolve_targets(claim):
            dependents.setdefault(target_id, set()).add(claim.claim_id)
    return dependents


@dataclass(frozen=True)
class SubgraphView:
    """限深 BFS 的结果。``truncated`` 是**必须**带出来的信息。

    深度被截断意味着"再往下还有依赖，但我们没走"。把它和"真的没有下游"混为一谈，
    就是 CARD-24 里"纸面能割、实际割不动"那类失真的另一种形态。
    """

    nodes: Tuple[str, ...]
    depths: Dict[str, int]
    truncated: bool
    max_depth: int

    def as_list(self) -> List[str]:
        return list(self.nodes)

    def claims(self, graph: ClaimGraph) -> Tuple[str, ...]:
        return tuple(node for node in self.nodes if node in graph.claims)


def subgraph_view(
    graph: ClaimGraph,
    changed_node_ids: Iterable[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> SubgraphView:
    """从种子出发沿依赖方向做限深 BFS，返回受影响节点及各自深度。

    种子本身深度 0 且**包含在结果里** —— 被改动的节点当然属于"受影响"。
    这样卡 §13 的"``affected_subgraph`` 是 ``state_diff`` 的超集"就是自然成立的，
    而不是靠额外补一条规则去凑。
    """
    if max_depth < 0:
        raise TemporalError(f"max_depth 不能为负：{max_depth}")
    seeds = tuple(sorted({str(item) for item in changed_node_ids}))
    depths: Dict[str, int] = {seed: 0 for seed in seeds}
    frontier: List[str] = list(seeds)
    truncated = False
    dependents = dependents_map(graph)

    while frontier:
        current = frontier.pop(0)
        depth = depths[current]
        for dependent in sorted(dependents.get(current, ())):
            if dependent in depths:
                continue
            if depth + 1 > max_depth:
                truncated = True
                continue
            depths[dependent] = depth + 1
            frontier.append(dependent)

    return SubgraphView(
        nodes=tuple(sorted(depths)),
        depths=depths,
        truncated=truncated,
        max_depth=max_depth,
    )


def compute_affected_subgraph(
    graph: ClaimGraph,
    changed_node_ids: Iterable[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Tuple[str, ...]:
    """卡 §8 的签名：返回受影响节点 id 列表（含种子）。"""
    return subgraph_view(graph, changed_node_ids, max_depth=max_depth).nodes


# ---------------------------------------------------------------------------
# 状态快照与重算
# ---------------------------------------------------------------------------
def snapshot_statuses(graph: ClaimGraph) -> Dict[str, Dict[str, str]]:
    """改动前的状态快照（claims 与 theses 分开存，便于各自对账）。"""
    return {
        "claims": {cid: claim.validation_status for cid, claim in graph.claims.items()},
        "theses": {tid: thesis.status for tid, thesis in graph.theses.items()},
    }


def _claim_reason(status: str, trigger: Optional[str]) -> str:
    if status == VALIDATION_BLOCKED:
        return f"REQUIRED 上游不可用（{trigger}）→ 阻断" if trigger else "REQUIRED 上游不可用 → 阻断"
    if status == VALIDATION_STALE:
        return "REQUIRED 上游已过期（stale）→ 下游标记 stale"
    return "状态更新"


def recompute_within(
    graph: ClaimGraph,
    claim_scope: Iterable[str],
    *,
    baseline: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    """**只在 ``claim_scope`` 内**重算 claim/thesis 状态，返回 state_diff。

    **为什么有界重算与全量重算必然同结果**：一个 claim 的状态只由它的上游决定
    （``claim_status`` 只读 ``resolve_targets`` 指到的节点）。若 claim ``X`` 不在
    种子的下游闭包内，则 ``X`` 依赖链上的任何节点也都不在闭包内 —— 否则 ``X``
    自己就在闭包内了。于是 ``X`` 的全部上游状态不变，``X`` 的状态也不变。
    对闭包取子集（限深截断）时这条推理不再成立，故**必须**现场与全量对账，
    不等就回落（卡 §11）。这正是 :func:`compare_full_vs_incremental` 存在的理由。

    状态单调退化，故不动点最多 ``len(scope)`` 轮收敛；``+2`` 只为兜住环（构图时本应已拒）。
    """
    scope = sorted({str(item) for item in claim_scope if str(item) in graph.claims})
    for _ in range(len(scope) + 2):
        changed = False
        for claim_id in scope:
            claim = graph.claims[claim_id]
            status, _dropped, _trigger = claim_status(graph, claim)
            if status != claim.validation_status:
                graph.claims[claim_id] = claim.with_status(status)
                changed = True
        if not changed:
            break
    else:  # pragma: no cover - 防御；环应在 CARD-09 构图期被拒
        raise TemporalError("子图内状态传播未收敛（疑似依赖环）")

    _recompute_affected_theses(graph, scope)
    return _diff(graph, scope, baseline)


def recompute_full(
    graph: ClaimGraph,
    *,
    baseline: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    """全量重算（委托 CARD-09 ``propagate_state``），返回同样的 state_diff 形状。"""
    changes = propagate_state(graph, baseline=baseline["claims"])
    diff: List[Dict[str, Any]] = [
        {
            "node_id": item["claim_id"],
            "node_type": "claim",
            "before": item["old"],
            "after": item["new"],
            "reason": item["reason"],
            "trigger": item.get("trigger"),
        }
        for item in changes
    ]
    for thesis_id in sorted(graph.theses):
        thesis = graph.theses[thesis_id]
        thesis.status = thesis_status(graph, thesis)
        before = baseline["theses"].get(thesis_id)
        if before is not None and before != thesis.status:
            diff.append(
                {
                    "node_id": thesis_id,
                    "node_type": "thesis",
                    "before": before,
                    "after": thesis.status,
                    "reason": "Thesis 成员状态变化 → 重新判定",
                    "trigger": None,
                }
            )
    return diff


def _recompute_affected_theses(graph: ClaimGraph, claim_scope: Sequence[str]) -> None:
    scope = set(claim_scope)
    for thesis_id in sorted(graph.theses):
        thesis = graph.theses[thesis_id]
        if not scope.intersection(thesis.claim_ids):
            continue
        thesis.status = thesis_status(graph, thesis)


def _diff(
    graph: ClaimGraph,
    claim_scope: Sequence[str],
    baseline: Dict[str, Dict[str, str]],
) -> List[Dict[str, Any]]:
    diff: List[Dict[str, Any]] = []
    for claim_id in sorted(claim_scope):
        before = baseline["claims"].get(claim_id)
        after = graph.claims[claim_id].validation_status
        if before == after:
            continue
        _status, _dropped, trigger = claim_status(graph, graph.claims[claim_id])
        diff.append(
            {
                "node_id": claim_id,
                "node_type": "claim",
                "before": before,
                "after": after,
                "reason": _claim_reason(after, trigger),
                "trigger": trigger,
            }
        )
    scope = set(claim_scope)
    for thesis_id in sorted(graph.theses):
        thesis = graph.theses[thesis_id]
        if not scope.intersection(thesis.claim_ids):
            continue
        before = baseline["theses"].get(thesis_id)
        if before is None or before == thesis.status:
            continue
        diff.append(
            {
                "node_id": thesis_id,
                "node_type": "thesis",
                "before": before,
                "after": thesis.status,
                "reason": "Thesis 成员状态变化 → 重新判定",
                "trigger": None,
            }
        )
    return diff


def compare_full_vs_incremental(
    graph: ClaimGraph,
    incremental_diff: Sequence[Dict[str, Any]],
    *,
    baseline: Dict[str, Dict[str, str]],
    nodes_recomputed: int,
    measure_latency: bool = True,
) -> Dict[str, Any]:
    """在**副本**上跑全量重算，与增量结果逐节点比对（卡 §16 的 same deterministic result）。

    判据取最强的一条：``(node_type, node_id) -> after`` 两张映射**完全相等**。
    增量少算了会缺键，多算了会多键，键同值不同也算不等 —— 三种都判为不一致。
    """
    started = time.perf_counter() if measure_latency else None
    clone = copy.deepcopy(graph)
    full_diff = recompute_full(clone, baseline=baseline)
    latency_ms = (time.perf_counter() - started) * 1000.0 if started else None

    def _map(diff: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], Any]:
        return {
            (str(item["node_type"]), str(item["node_id"])): item["after"] for item in diff
        }

    incremental_map = _map(incremental_diff)
    full_map = _map(full_diff)
    results_identical = incremental_map == full_map

    return {
        "nodes_recomputed": int(nodes_recomputed),
        "full_nodes": len(graph.claims),
        "nodes_changed": len(incremental_map),
        "results_identical": bool(results_identical),
        "incremental_only": sorted(
            f"{kind}:{node}" for kind, node in set(incremental_map) - set(full_map)
        ),
        "full_only": sorted(
            f"{kind}:{node}" for kind, node in set(full_map) - set(incremental_map)
        ),
        # 延迟是**观测值**，不进确定性判定（测试不对它做断言）。
        "latency_ms": latency_ms,
        "latency_is_observational": True,
    }


# ---------------------------------------------------------------------------
# 事件施加
# ---------------------------------------------------------------------------
def _append_stale(graph: ClaimGraph, claim_id: str, reason: str) -> bool:
    """往 ``stale_claims`` 追加一条标记（append-only）。已在则不动。"""
    if claim_id not in graph.claims:
        return False
    if claim_id in graph.stale_claim_ids:
        return False
    graph.stale_claims.append({"claim_id": claim_id, "reason": reason})
    return True


def _append_stale_markers(
    graph: ClaimGraph, claim_ids: Iterable[str], reason: str
) -> List[str]:
    marked: List[str] = []
    for claim_id in sorted({str(item) for item in claim_ids}):
        if _append_stale(graph, claim_id, reason):
            marked.append(claim_id)
    return marked


def _mark_conflicted(graph: ClaimGraph, claim_ids: Iterable[str]) -> List[str]:
    """把 claim 的 ``verification_status`` 置为 CONFLICTED（未裁决冲突）。"""
    marked: List[str] = []
    for claim_id in sorted({str(item) for item in claim_ids}):
        claim = graph.claims.get(claim_id)
        if claim is None or claim.verification_status == VERIFICATION_CONFLICTED:
            continue
        graph.claims[claim_id] = dataclasses.replace(
            claim, verification_status=VERIFICATION_CONFLICTED
        )
        marked.append(claim_id)
    return marked


def _supersession_reason(event: TemporalEvent) -> str:
    if event.event_type == EVENT_CORRECTION:
        return REASON_CORRECTION
    if event.event_type == EVENT_RESTATEMENT:
        return REASON_RESTATEMENT
    return REASON_DEPRECATION


# ---------------------------------------------------------------------------
# 影响与动作
# ---------------------------------------------------------------------------
def impact_analysis(
    graph: ClaimGraph,
    *,
    state_diff: Sequence[Dict[str, Any]],
    lineage: Optional[VersionLineage] = None,
    newly_superseded: Sequence[str] = (),
    valuations: Optional[Sequence[str]] = None,
    memo_sections: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """卡 §4 的六个 impact 字段，返回 ``(impact, warnings)``。

    图上算得出来的四个直接算。**图外的两个（估值/备忘段）不猜**：
    它们的依赖关系分别属于 CARD-08 / CARD-10，本卡看不到那张映射表，
    所以要么由调用方给出**确定受影响**的 id（本卡只计数），要么如实报 ``None``。
    报 0 会把"没有估值受影响"和"我们根本没看估值"说成同一件事。
    """
    warnings: List[str] = []
    superseded_count = 0
    if lineage is not None:
        # 本事件**新**造成的替代：``newly_superseded`` 装的是被替代的**旧** id，
        # 所以要比的是条目的 ``fact_id``。比 ``superseded_by`` 会一条都数不到
        # （那个字段存的是替代者，方向正好相反）——施工时这里真错过一次。
        targets = {str(item) for item in newly_superseded}
        superseded_count = sum(
            1
            for entry in lineage.entries()
            if entry.version_status == LINEAGE_SUPERSEDED
            and (not targets or str(entry.fact_id) in targets)
        )
    elif newly_superseded:
        superseded_count = len(set(newly_superseded))

    # 计算 stale = **受影响 claim 所依赖的**计算。
    # 不用"子图里可达的计算节点"：CARD-24 已经查明，计算节点的输入证据在传播上
    # 不承重（CARD-03 在构图期就定死了它的 validation_status），所以
    # 证据 → 计算这条边在图里根本不存在，按可达性数永远是 0。
    # 反过来从 claim 出发（claim → calculation_ids 是真实存在的边）才数得到东西。
    stale_calculations: Set[str] = set()
    for item in state_diff:
        if item["node_type"] != "claim":
            continue
        claim = graph.claims.get(str(item["node_id"]))
        if claim is None:
            continue
        for target_type, target_id, _role in resolve_targets(claim):
            if target_type == TARGET_CALCULATION:
                stale_calculations.add(target_id)
    calculations_stale = len(stale_calculations)
    claims_invalidated = sum(
        1 for item in state_diff if item["node_type"] == "claim" and item["after"] == VALIDATION_BLOCKED
    )
    theses_weakened = sum(
        1
        for item in state_diff
        if item["node_type"] == "thesis"
        and item["after"] in (THESIS_WEAKENED, THESIS_BLOCKED, THESIS_STALE)
    )

    impact: Dict[str, Any] = {
        "facts_superseded": superseded_count,
        "calculations_stale": calculations_stale,
        "claims_invalidated": claims_invalidated,
        "theses_weakened": theses_weakened,
        "valuations_stale": None,
        "memo_sections_affected": None,
    }

    # 一件事都没变 ⇒ 图外域不可能受影响。这是**可证的 0**（没有任何状态迁移，
    # 就没有任何下游依赖被触碰），与"没去算"是两回事，所以这里给 0 且不留 warning。
    nothing_changed = not state_diff and not superseded_count
    for key, supplied in (
        ("valuations_stale", valuations),
        ("memo_sections_affected", memo_sections),
    ):
        if supplied is None:
            if nothing_changed:
                impact[key] = 0
                continue
            warnings.append(
                f"external_domain_not_supplied:{key}"
                "（估值属 CARD-08、备忘段属 CARD-10；本卡不持有它们的依赖映射，"
                "给 None 而不是 0）"
            )
            continue
        impact[key] = len({str(item) for item in supplied})

    return impact, warnings


def build_actions(
    graph: ClaimGraph,
    *,
    event: TemporalEvent,
    state_diff: Sequence[Dict[str, Any]],
    affected: SubgraphView,
    newly_superseded: Sequence[str] = (),
    valuations: Optional[Sequence[str]] = None,
    memo_sections: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """按优先级给出重验证动作（卡 §6 ``revalidation_actions``）。

    图外的两个域（估值/备忘段）只在调用方**给出了确定受影响 id** 时才产出动作。
    没有映射就编不出目标 id —— 编一个出来会让动作清单指向不存在的对象。
    """
    actions: List[Dict[str, Any]] = []

    for item in state_diff:
        if item["node_type"] != "claim":
            continue
        if item["after"] == VALIDATION_BLOCKED:
            actions.append(
                {
                    "action_type": ACTION_REVALIDATE_CLAIM,
                    "target_id": item["node_id"],
                    "priority": PRIORITY_HIGH,
                    "reason": item["reason"],
                }
            )
        elif item["after"] == VALIDATION_STALE:
            actions.append(
                {
                    "action_type": ACTION_REVALIDATE_CLAIM,
                    "target_id": item["node_id"],
                    "priority": PRIORITY_MEDIUM,
                    "reason": item["reason"],
                }
            )
    for item in state_diff:
        if item["node_type"] == "thesis" and item["after"] != THESIS_SUPPORTED:
            actions.append(
                {
                    "action_type": ACTION_RECHECK_THESIS,
                    "target_id": item["node_id"],
                    "priority": PRIORITY_HIGH,
                    "reason": item["reason"],
                }
            )

    for node in affected.nodes:
        if node in graph.calculations:
            actions.append(
                {
                    "action_type": ACTION_REFRESH_CALCULATION,
                    "target_id": node,
                    "priority": PRIORITY_MEDIUM,
                    "reason": "输入证据受本次事件影响，计算需重算",
                }
            )

    if event.event_kind == CHANGE_VERSION_SUPERSESSION:
        for fact_id in sorted({str(item) for item in newly_superseded}):
            actions.append(
                {
                    "action_type": ACTION_REVIEW_SUPERSESSION,
                    "target_id": fact_id,
                    "priority": PRIORITY_HIGH,
                    "reason": "旧值已被替代，需确认下游结论改用新值",
                }
            )
    elif event.event_kind == CHANGE_DATA_CONFLICT and not event.resolved_by:
        for node in affected.claims(graph):
            actions.append(
                {
                    "action_type": ACTION_ARBITRATE_CONFLICT,
                    "target_id": node,
                    "priority": PRIORITY_HIGH,
                    "reason": "同期间同口径取值冲突且未裁决 → 交 CARD-13 裁决",
                }
            )

    # 图外域：调用方给了受影响清单才产出动作（没给就是不产出，不编 id）。
    for target_id in sorted({str(item) for item in (valuations or ())}):
        actions.append(
            {
                "action_type": ACTION_REFRESH_VALUATION,
                "target_id": target_id,
                "priority": PRIORITY_HIGH,
                "reason": "估值输入受本次事件影响，需重算",
            }
        )
    for target_id in sorted({str(item) for item in (memo_sections or ())}):
        actions.append(
            {
                "action_type": ACTION_REFRESH_MEMO_SECTION,
                "target_id": target_id,
                "priority": PRIORITY_LOW,
                "reason": "备忘段所引事实受本次事件影响，需刷新",
            }
        )

    return actions


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def ingest_temporal_event(
    graph: ClaimGraph,
    event: TemporalEvent,
    *,
    lineage: Optional[VersionLineage] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    valuations: Optional[Sequence[str]] = None,
    memo_sections: Optional[Sequence[str]] = None,
    measure_latency: bool = True,
) -> RevalidationReport:
    """施加一次时间事件并返回重验证报告（卡 §8）。

    流程固定为：**解析种子 → 施加变更（只写标记/字段）→ 限深求子图 → 子图内有界重算
    → 与全量对账（不等则回落全量并记录）→ 出影响与动作**。
    """
    warnings: List[str] = []

    if not graph.claims:
        return RevalidationReport(
            event_id=event.event_id,
            event=event.to_dict(),
            status=STATUS_UNAVAILABLE,
            unavailable_reason=UNAVAILABLE_NO_CLAIMS,
            warnings=("graph_has_no_claims：图上没有 claim 可供重验证",),
        )

    seeds = tuple(sorted({str(item) for item in event.changed_source_ids}))
    known = set(graph.claims) | set(graph.evidence_index) | set(graph.calculations)
    resolvable = tuple(seed for seed in seeds if seed in known)
    unresolved_seeds = [seed for seed in seeds if seed not in known]
    if unresolved_seeds:
        warnings.append(
            "unknown_changed_nodes:" + ",".join(unresolved_seeds)
            + "（这些 id 不在图内，未纳入传播）"
        )
    if seeds and not resolvable:
        return RevalidationReport(
            event_id=event.event_id,
            event=event.to_dict(),
            status=STATUS_UNAVAILABLE,
            unavailable_reason=UNAVAILABLE_UNKNOWN_NODES,
            changed_nodes=seeds,
            warnings=tuple(warnings),
        )

    baseline = snapshot_statuses(graph)
    newly_superseded: List[str] = []
    if not str(event.event_date or "").strip():
        warnings.append(
            "event_date_missing：事件没有日期，无法按时间顺序与其他事件排序（卡 §12 边缘 4）；"
            "本次按调用顺序单独传播"
        )
    # 传播的起点。默认是事件声明改动的节点；**替代场景还要加上被替代的旧节点** ——
    # 依赖旧值的是既有的 claim，而新值刚进来还没有任何东西依赖它。
    # 只用新 id 当种子，会把"受影响子图"算成空集，而 state_diff 却有内容，
    # 直接违反卡 §13 的"affected_subgraph 是 state_diff 的超集"。
    impact_seeds: Set[str] = set(resolvable)

    # -- 施加变更（只写标记/字段，传播留给下面那一次有界重算）----------------
    if event.event_kind == CHANGE_VERSION_SUPERSESSION:
        old_id = event.supersedes
        if old_id is None:
            raise TemporalError(
                "VERSION_SUPERSESSION 必须给 supersedes（被替代的旧 fact_id），"
                "否则无法知道哪一条退出了当前口径"
            )
        if old_id not in known:
            raise TemporalError(f"supersedes 指向图外节点：{old_id}")
        new_ids = [seed for seed in resolvable if seed != old_id]
        if not new_ids:
            raise TemporalError(
                "VERSION_SUPERSESSION 需要至少一个 changed_source_ids 里的新 fact_id "
                "作为替代者；只给旧 id 无法建立替代方向"
            )
        new_id = new_ids[0]
        if lineage is not None:
            if old_id not in lineage:
                raise TemporalError(f"血缘里没有这个旧值：{old_id}")
            if new_id not in lineage:
                raise TemporalError(
                    f"血缘里没有这个新值：{new_id}（新值必须先作为披露登记进血缘）"
                )
            lineage.mark_superseded(
                old_id, superseded_by=new_id, reason=_supersession_reason(event)
            )
        newly_superseded.append(old_id)
        impact_seeds.add(old_id)
        # 旧值退出当前口径 → 用它的结论过期，需要按新值刷新（不是"没有来源"）。
        dependents = downstream_closure(graph, sorted(impact_seeds))
        marked = _append_stale_markers(
            graph, dependents, f"{REASON_SUPERSEDED}:{old_id}->{new_id}"
        )
        if not marked:
            warnings.append(
                f"supersession_without_claim_dependents:{old_id}（该证据不被任何 claim 直接依赖）"
            )
    elif event.event_kind == CHANGE_DATA_CONFLICT:
        if event.resolved_by:
            warnings.append(
                f"conflict_resolved_by:{event.resolved_by}"
                "（已裁决 → 不阻断；未裁决才 fail-closed 阻断）"
            )
        else:
            impacted = downstream_closure(graph, sorted(impact_seeds))
            marked = _mark_conflicted(graph, impacted)
            if not marked:
                warnings.append(
                    "conflict_without_claim_dependents：冲突节点不被任何 claim 依赖"
                )
    elif event.event_kind == CHANGE_NEW_INFORMATION:
        impacted = downstream_closure(graph, sorted(impact_seeds))
        marked = _append_stale_markers(
            graph, impacted, f"{REASON_NEW_PERIOD}:{event.event_date}"
        )
        if not marked:
            warnings.append(
                "new_information_without_claim_dependents：新数据不影响任何现有 claim"
            )
    else:  # pragma: no cover - 词表已在 TemporalEvent 校验
        raise TemporalError(f"未实现的事件类型：{event.event_kind}")

    # -- 子图 + 有界重算 ---------------------------------------------------
    view = subgraph_view(graph, sorted(impact_seeds), max_depth=max_depth)
    if view.truncated:
        warnings.append(
            f"affected_subgraph_truncated:max_depth={max_depth}"
            "（更深的下游未纳入，已按 §11 回落全量对账）"
        )
    scope = view.claims(graph)
    incremental_diff = recompute_within(graph, scope, baseline=baseline)
    comparison = compare_full_vs_incremental(
        graph,
        incremental_diff,
        baseline=baseline,
        nodes_recomputed=len(scope),
        measure_latency=measure_latency,
    )

    fallback: Optional[str] = None
    if not comparison["results_identical"]:
        # 卡 §11：不一致 → 回落全量重算并记录。绝不"以增量为准"了事。
        warnings.append(
            "incremental_full_mismatch：子图内重算与全量重算结果不一致，"
            f"已回落全量。缺的节点={comparison['full_only']} 多的节点={comparison['incremental_only']}"
        )
        fallback = FALLBACK_FULL_RECOMPUTATION
        state_diff = recompute_full(graph, baseline=baseline)
        comparison["nodes_recomputed"] = comparison["full_nodes"]
        comparison["fell_back"] = True
    else:
        comparison["fell_back"] = False
        state_diff = incremental_diff

    impact, impact_warnings = impact_analysis(
        graph,
        state_diff=state_diff,
        lineage=lineage,
        newly_superseded=newly_superseded,
        valuations=valuations,
        memo_sections=memo_sections,
    )
    warnings.extend(impact_warnings)

    actions = build_actions(
        graph,
        event=event,
        state_diff=state_diff,
        affected=view,
        newly_superseded=newly_superseded,
        valuations=valuations,
        memo_sections=memo_sections,
    )

    # 受影响子图必须包含**被改动的 Thesis**。子图是由 claim→依赖 的边算出来的，
    # 而 Thesis 是 claim 的消费者、不在那张邻接表里，所以它天然不会被 BFS 走到。
    # 不补进来的话 ``state_diff`` 里有 Thesis 而 ``affected_subgraph`` 里没有，
    # 就直接违反卡 §13 的"超集"（施工时正是被校验器当场抓出来的）。
    thesis_nodes = {
        str(item["node_id"]) for item in state_diff if item["node_type"] == "thesis"
    }
    affected_nodes = tuple(sorted(set(view.nodes) | thesis_nodes))

    status = STATUS_OK
    if fallback or any(impact.get(key) is None for key in EXTERNAL_IMPACT_KEYS):
        status = STATUS_PARTIAL

    lineage_payload: Tuple[Dict[str, Any], ...] = ()
    if lineage is not None:
        touched = set(newly_superseded) | {
            str(item) for item in resolvable
        }
        lineage_payload = tuple(
            entry.to_dict() for entry in lineage.entries() if entry.fact_id in touched
        )

    return RevalidationReport(
        event_id=event.event_id,
        event=event.to_dict(),
        status=status,
        impact=impact,
        changed_nodes=resolvable,
        affected_subgraph=affected_nodes,
        state_diff=tuple(state_diff),
        revalidation_actions=tuple(actions),
        full_vs_incremental=comparison,
        lineage=lineage_payload,
        fallback=fallback,
        warnings=tuple(warnings),
    )


__all__ = [
    "EVENT_EFFECTS",
    "FALLBACK_FULL_RECOMPUTATION",
    "SubgraphView",
    "build_actions",
    "compare_full_vs_incremental",
    "compute_affected_subgraph",
    "dependents_map",
    "impact_analysis",
    "ingest_temporal_event",
    "recompute_full",
    "recompute_within",
    "snapshot_statuses",
    "subgraph_view",
]
