"""CARD-24 — 确定性图算法：关键依赖 / 最小割集 / 单点失效 / 数值脆弱性 / 监控计划.

**这一层没有模型。** 全部结论都是图上算出来的，且每一步的理由都能回放。

语义以 **CARD-09 的传播表为准**（§7b 是唯一权威），本模块只在其上做图论分析：

* 一条 claim 被阻断，当且仅当它的**任一** REQUIRED 上游不可用
  （``upstream_state`` 返回 BLOCKED / CONFLICTED / MISSING）。
* Thesis 被阻断，当且仅当它的**任一** REQUIRED 成员被阻断。

  ⚠️ 上面两条要分清**两个空间**，这是本卡最容易读错的地方：

  | 空间 | 问的问题 | 门 |
  |---|---|---|
  | **成立**（success） | 「这个节点算不算可用」 | **AND** —— 全部 REQUIRED 上游都可用才算可用 |
  | **失效传播**（failure） | 「一个上游坏了会不会连累它」 | **OR** —— 任一 REQUIRED 上游坏了它就坏 |

  二者是同一张表的两种读法，不矛盾。**决定割集长短的是后者**（失效空间）。
  本节下文说的"OR 门 / 没有 AND 门"一律指**失效空间**。
* SUPPORTING 边只降覆盖、不改判决；OPTIONAL 边不影响判决。
* 假设（assumption）边**永远不阻断**（§7b 里 ``TARGET_ASSUMPTION`` 恒为 OK），
  所以假设不出现在割集里 —— 它走 :func:`identify_fragile_assumptions` 这条路，
  作为"逻辑站不住"而非"逻辑被打断"来报告。

**由此得出的一条结论，直接决定了本卡"最小割集"长什么样**（施工时被校验器的
"割集非极小"报警逼出来的，记在 §23）：把"失效"沿着依赖边往上看，每个内部节点
都是 **OR 门**（任一子节点失效即失效），整棵树里**没有 AND 门**。于是任何**单个**
位于必需祖先链上的节点失效都会一路传到 Thesis。所以

    最小割集恒为**单元素集**：把 REQUIRED 祖先链上任意一个节点打掉，逻辑即失效。

这不是算法偷懒，而是 CARD-09 那张**fail-closed**传播表的必然推论：它宁可从紧，
"缺一个来源就当作没有来源"，因此这套传播里**不存在冗余路径**。卡 §7B 的示例
「C01 依赖 E01 **OR** E02 → 割集 {E01,E02,C02}」预设了证据层的 OR 语义，
而 CARD-09 里 E01 单点失效就会阻断 C01 —— 按真实语义，那个例子的最小割集是
{C01}、{E01}、{E02} 这些**单元素集**。这条差异两处都记（本文件 + CARD-24 §23）。

同时，本模块另外报出**更细一层**的信息：哪些节点是"一击打断**全部**必需成员"的
（``is_single_point``，即落在所有必需祖先集的交集里）。它是最小割集的真子集，
也是"最省力的那一击"——在有冗余结构的图里，这两者差距越大，逻辑越稳健。
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.cn.graph import (
    ROLE_REQUIRED,
    TARGET_ASSUMPTION,
    TARGET_CALCULATION,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    THESIS_BLOCKED,
    ClaimGraph,
    Thesis,
)
from src.cn.graph.propagation import downstream_closure, resolve_targets, structural_status, thesis_status
from src.cn.normalization.units import canonical_unit
from src.cn.proof.models import (
    STATUS_BLOCKED,
    STATUS_CONFLICTED,
    STATUS_PRECEDENCE,
    STATUS_RANK,
)

from .models import (
    COMPARISON_GTE,
    COMPARISON_GT,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_SETS,
    EVAL_NO_CURRENT,
    EVAL_UNIT_MISMATCH,
    FragilityError,
    FragilityReport,
    FragilityThreshold,
    MonitoringPlan,
    REASON_NO_REQUIRED,
    REASON_THESIS_ALREADY_BLOCKED,
    REJECT_UNIT_MISMATCH,
    REJECT_UNRESOLVABLE_SOURCE,
    REVALIDATION_REFRESH,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    TRIGGER_NUMERIC,
    TRIGGER_STRUCTURAL,
)

_NODE_TYPE_MISSING = "missing"

#: 浮点边界容差：与阈值同量级时算作"正好落在边界上"（卡 §12 边缘 4）。
_TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# 节点索引
# ---------------------------------------------------------------------------
def _node_entry(graph: ClaimGraph, node_id: str) -> Optional[Dict[str, Any]]:
    """``{node_type, label, metric, unit, value, active}`` —— 解析不了返回 None。"""
    evidence = graph.evidence_index.get(node_id)
    if evidence is not None:
        return {
            "node_type": TARGET_EVIDENCE,
            "label": f"{evidence.metric or evidence.evidence_id} = {evidence.value}",
            "metric": evidence.metric,
            "unit": evidence.unit,
            "value": evidence.value,
            "active": bool(evidence.active),
        }
    calculation = graph.calculations.get(node_id)
    if calculation is not None:
        return {
            "node_type": TARGET_CALCULATION,
            "label": f"{calculation.formula} = {calculation.value}",
            "metric": calculation.metric,
            "unit": (calculation.output or {}).get("unit"),
            "value": calculation.value,
            "active": calculation.validation_status == "ok",
        }
    claim = graph.claims.get(node_id)
    if claim is not None:
        return {
            "node_type": TARGET_CLAIM,
            "label": claim.text or claim.claim_id,
            "metric": claim.metric,
            "unit": None,
            "value": None,
            "active": claim.validation_status not in ("BLOCKED", "UNSUPPORTED"),
        }
    return None


def _node_kind(graph: ClaimGraph, node_id: str) -> str:
    entry = _node_entry(graph, node_id)
    return entry["node_type"] if entry else _NODE_TYPE_MISSING


def node_type_of(graph: ClaimGraph, node_id: str) -> str:
    """Public alias of :func:`_node_kind` — ``claim`` / ``evidence`` / ``calculation`` / ``missing``."""
    return _node_kind(graph, node_id)


# ---------------------------------------------------------------------------
# REQUIRED 边与必需祖先集
# ---------------------------------------------------------------------------
def _required_targets(graph: ClaimGraph, node_id: str) -> List[Tuple[str, str, str]]:
    """一个节点发出的 REQUIRED 边。

    只在 claim 上展开：证据与计算节点**不下钻**。理由不是偷懒 —— CARD-09 的
    传播里，计算节点的 ``validation_status`` 由 CARD-03 注册表在构图时定下，
    之后撤掉计算的某个输入证据**不会**改变它，所以"计算 → 输入证据"这条边
    在传播上是**不承重**的。把不承重的边算进割集，得到的会是"纸面上能割、
    实际割不动"的假结论。假设边同样排除：§7b 里它从不阻断。
    """
    claim = graph.claims.get(node_id)
    if claim is None:
        return []
    return [
        (target_type, target_id, role)
        for target_type, target_id, role in resolve_targets(claim)
        if role == ROLE_REQUIRED and target_type != TARGET_ASSUMPTION
    ]


def required_edges(
    graph: ClaimGraph, claim_id: str, *, max_depth: int = DEFAULT_MAX_DEPTH
) -> List[Tuple[str, str, str]]:
    """从 ``claim_id`` 出发沿 REQUIRED 边走到的全部边（``(source, target, target_type)``）. """
    out: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    visited: Set[str] = {claim_id}
    queue: List[Tuple[str, int]] = [(claim_id, 0)]
    while queue:
        node_id, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for target_type, target_id, _role in _required_targets(graph, node_id):
            edge = (node_id, target_id, target_type)
            if edge not in seen:
                seen.add(edge)
                out.append(edge)
            if target_id not in visited:
                visited.add(target_id)
                queue.append((target_id, depth + 1))
    return sorted(out)


def required_ancestry(
    graph: ClaimGraph, claim_id: str, *, max_depth: int = DEFAULT_MAX_DEPTH
) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """``claim_id`` 的必需祖先集：``{node_id: {node_type, label, depth}}`` + 是否被深度截断.

    含 ``claim_id`` 自身（depth 0）—— 把它整条删掉同样会让下游断链，它本来就是
    一个失效点。悬空的 REQUIRED 引用也会被记成 ``node_type="missing"`` 的节点，
    因为"引用了一个不存在的东西"是实打实的结构缺陷，不是可以跳过的空指针。
    """
    out: Dict[str, Dict[str, Any]] = {}
    pruned = False
    visited: Set[str] = set()
    queue: List[Tuple[str, int]] = [(claim_id, 0)]
    while queue:
        node_id, depth = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        entry = _node_entry(graph, node_id)
        if entry is None:
            out[node_id] = {
                "node_type": _NODE_TYPE_MISSING,
                "label": f"悬空引用：{node_id}",
                "depth": depth,
            }
            continue
        out[node_id] = {"node_type": entry["node_type"], "label": entry["label"], "depth": depth}
        children = _required_targets(graph, node_id)
        if not children:
            continue
        if depth >= max_depth:
            # 还有下游但不再展开：如实标注"结果不是全局的"（卡 §11）。
            pruned = True
            continue
        for _target_type, target_id, _role in children:
            queue.append((target_id, depth + 1))
    return out, pruned


# ---------------------------------------------------------------------------
# A. 关键依赖检测（卡 §7A）
# ---------------------------------------------------------------------------
def scope_theses(graph: ClaimGraph, thesis_ids: Optional[Iterable[str]] = None) -> List[Thesis]:
    """Scope 内的 Thesis，按 id 排序；引用不存在的 thesis 直接抛（零静默）。"""
    if thesis_ids is None:
        return sorted(graph.theses.values(), key=lambda item: item.thesis_id)
    out: List[Thesis] = []
    for thesis_id in thesis_ids:
        thesis = graph.theses.get(str(thesis_id))
        if thesis is None:
            raise FragilityError(f"未注册的 thesis: {thesis_id}")
        out.append(thesis)
    return sorted(out, key=lambda item: item.thesis_id)


def identify_critical_nodes(
    graph: ClaimGraph,
    thesis_ids: Optional[Iterable[str]] = None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> List[Dict[str, Any]]:
    """每个位于 REQUIRED 路径上的节点，以及它被哪些 Thesis 需要、是否单点.

    ``is_single_point`` 的判据是**可验证**的：某节点 x 单独失效就能让该 Thesis
    失效 ⟺ x 属于该 Thesis 每一个 REQUIRED 成员的必需祖先集。等价地
    ``{x}`` 本身就是一个割集。已 BLOCKED 的 Thesis 不再标单点 —— 它已经倒了，
    再给它的节点挂"单点失效"会把"当前的脆弱点"和"昨天的事故"混为一谈。
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    for thesis in scope_theses(graph, thesis_ids):
        already_blocked = thesis_status(graph, thesis) == THESIS_BLOCKED
        required = [cid for cid in thesis.required_claims() if cid in graph.claims]
        sets: List[Set[str]] = []
        for claim_id in required:
            ancestry, _pruned = required_ancestry(graph, claim_id, max_depth=max_depth)
            sets.append(set(ancestry))
            for node_id, entry in ancestry.items():
                record = nodes.setdefault(
                    node_id,
                    {
                        "node_id": node_id,
                        "node_type": entry["node_type"],
                        "label": entry["label"],
                        "thesis_ids": [],
                        "depth": entry["depth"],
                        "is_single_point": False,
                        "single_point_for": [],
                    },
                )
                if thesis.thesis_id not in record["thesis_ids"]:
                    record["thesis_ids"].append(thesis.thesis_id)
                if entry["depth"] < record["depth"]:
                    record["depth"] = entry["depth"]
        if already_blocked or not sets:
            continue
        for node_id in sorted(set.intersection(*sets)):
            record = nodes[node_id]
            record["is_single_point"] = True
            if thesis.thesis_id not in record["single_point_for"]:
                record["single_point_for"].append(thesis.thesis_id)
    for record in nodes.values():
        record["thesis_ids"].sort()
        record["single_point_for"].sort()
    return sorted(
        nodes.values(), key=lambda item: (-len(item["thesis_ids"]), item["depth"], item["node_id"])
    )


def detect_single_points_of_failure(
    critical_nodes: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """从关键依赖里筛出单点失效（卡 §8 签名）。"""
    return [dict(item) for item in critical_nodes if item.get("is_single_point")]


# ---------------------------------------------------------------------------
# B. 最小割集（卡 §7B）
# ---------------------------------------------------------------------------
def pivotal_nodes(
    graph: ClaimGraph,
    thesis_id: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Tuple[Dict[str, int], bool, str]:
    """该 Thesis 的必需祖先节及其最小深度：``({node_id: depth}, pruned, note)``.

    ``note`` 非空表示**没有割集可谈**：要么 Thesis 已经倒了（再列割集是把
    "昨天的事故"当成"当前的结构"），要么它没有 REQUIRED 成员（卡 §12 边缘 1：
    只有 SUPPORTING 的 Thesis 没有单点、没有割集，报告如实说明）。
    """
    thesis = graph.theses.get(thesis_id)
    if thesis is None:
        raise FragilityError(f"未注册的 thesis: {thesis_id}")
    if thesis_status(graph, thesis) == THESIS_BLOCKED:
        return {}, False, REASON_THESIS_ALREADY_BLOCKED
    required = [cid for cid in thesis.required_claims() if cid in graph.claims]
    if not required:
        return {}, False, REASON_NO_REQUIRED
    depths: Dict[str, int] = {}
    pruned = False
    for claim_id in required:
        ancestry, was_pruned = required_ancestry(graph, claim_id, max_depth=max_depth)
        pruned = pruned or was_pruned
        for node_id, entry in ancestry.items():
            depth = entry["depth"]
            if node_id not in depths or depth < depths[node_id]:
                depths[node_id] = depth
    return depths, pruned, ""


def _cut_sets_detail(
    graph: ClaimGraph,
    thesis_id: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_sets: int = DEFAULT_MAX_SETS,
) -> Dict[str, Any]:
    """``{"cuts", "truncated", "pruned", "note"}``（见模块头：割集恒为单元素集）."""
    depths, pruned, note = pivotal_nodes(graph, thesis_id, max_depth=max_depth)
    if note:
        return {"cuts": [], "truncated": False, "pruned": pruned, "note": note}
    ordered = sorted(depths, key=lambda node_id: (depths[node_id], node_id))
    truncated = len(ordered) > max_sets
    return {
        "cuts": [[node_id] for node_id in ordered[:max_sets]],
        "truncated": truncated,
        "pruned": pruned,
        "note": "",
    }


def compute_minimal_cut_sets(
    graph: ClaimGraph,
    thesis_id: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_sets: int = DEFAULT_MAX_SETS,
) -> List[List[str]]:
    """使该 Thesis 失效的最小 REQUIRED 依赖集合（卡 §8 签名）.

    返回的每个割集都是单元素集 —— 理由见模块头（CARD-09 的传播表里没有 AND 门，
    也就没有冗余路径）。``max_sets`` 是**输出条数**上限：必需祖先链很长时只给出
    最近、最中心的那些，并置 ``cut_sets_truncated``。
    """
    return _cut_sets_detail(graph, thesis_id, max_depth=max_depth, max_sets=max_sets)["cuts"]


def simulate_cut(graph: ClaimGraph, node_ids: Iterable[str]) -> ClaimGraph:
    """在**副本**上施加破坏并重算传播（卡 §13 反解断言的执行体）.

    破坏语义与 CARD-09 的节点状态一一对应：claim 被删 → MISSING；
    证据被撤 → 失效（append-only，不物理删除）；计算被标 mismatch → BLOCKED。
    """
    clone = ClaimGraph.from_dict(graph.to_dict())
    for node_id in node_ids:
        node_id = str(node_id)
        if node_id in clone.claims:
            del clone.claims[node_id]
        elif node_id in clone.evidence_index:
            clone.evidence_index[node_id] = _deactivate(clone.evidence_index[node_id])
        elif node_id in clone.calculations:
            node = clone.calculations[node_id]
            clone.calculations[node_id] = _mismatch(node)
    clone.propagate_state()
    return clone


def cut_blocks_thesis(graph: ClaimGraph, thesis_id: str, node_ids: Iterable[str]) -> bool:
    """移除 ``node_ids`` 后该 Thesis 是否 BLOCKED。"""
    clone = simulate_cut(graph, node_ids)
    thesis = clone.theses.get(thesis_id)
    if thesis is None:
        return False
    return thesis_status(clone, thesis) == THESIS_BLOCKED


def _deactivate(node: Any) -> Any:
    import dataclasses

    return dataclasses.replace(node, active=False, deactivated_reason="card24.simulate_cut")


def _mismatch(node: Any) -> Any:
    import dataclasses

    return dataclasses.replace(node, validation_status="mismatch")


# ---------------------------------------------------------------------------
# 阈值：来源白名单与单位
# ---------------------------------------------------------------------------
_PERCENT_TOKENS = frozenset({"%", "percent", "pct", "百分点", "百分比", "比率"})


def canonical_unit_token(value: Any) -> str:
    """把单位写法归一到一个可比较的 token；无法识别时原样返回（不猜）. """
    token = str(value or "").strip()
    if not token:
        return ""
    if token.lower() in _PERCENT_TOKENS:
        return "PERCENT"
    try:
        canonical = canonical_unit(token)
    except Exception:
        canonical = None
    return canonical or token


def threshold_source_resolves(
    graph: ClaimGraph,
    source_ref: str,
    assumption_registry: Any = None,
    *,
    extra_evidence_ids: Iterable[str] = (),
) -> Tuple[bool, str]:
    """阈值依据是否在图上/注册表里真的存在 —— 不存在就不是阈值，是编的."""
    ref = str(source_ref or "")
    if not ref:
        return False, "empty_source_ref"
    if ref in graph.claims:
        return True, "claim"
    if ref in graph.evidence_index:
        return True, "evidence"
    if ref in graph.calculations:
        return True, "calculation"
    if ref in {str(item) for item in extra_evidence_ids}:
        return True, "declared_evidence"
    if assumption_registry is not None:
        has = getattr(assumption_registry, "has", None)
        try:
            if callable(has) and has(ref):
                return True, "assumption"
        except Exception:  # pragma: no cover - defensive
            pass
    return False, "unresolved"


def _resolve_current_value(
    graph: ClaimGraph, item: FragilityThreshold
) -> Tuple[Optional[float], str, str, Optional[bool]]:
    """``(value, source, reason, node_active)``；判不了就返回 None + 原因."""
    evidence = graph.evidence_index.get(item.node_id)
    if evidence is not None and evidence.value is not None:
        return float(evidence.value), f"evidence:{item.node_id}", "", bool(evidence.active)
    calculation = graph.calculations.get(item.node_id)
    if calculation is not None and calculation.value is not None:
        return (
            float(calculation.value),
            f"calculation:{item.node_id}",
            "",
            calculation.validation_status == "ok",
        )
    if item.current_value is not None:
        return float(item.current_value), item.current_value_source, "", None
    if item.node_id in graph.claims:
        # claim 本身不是一个可寻址的数值，指到它上面就是没指清 —— 不猜。
        return None, "", EVAL_NO_CURRENT, None
    return None, "", EVAL_NO_CURRENT, None


def _distance(comparison: str, current: float, threshold: float) -> float:
    if comparison in (COMPARISON_GTE, COMPARISON_GT):
        return current - threshold
    return threshold - current


def compute_numeric_fragility(
    graph: ClaimGraph,
    thresholds: Sequence[FragilityThreshold],
    *,
    assumption_registry: Any = None,
    extra_evidence_ids: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """数值脆弱性（卡 §7C，**可选**）.

    每一项都带着"能不能评估"的结论。``evaluable=False`` 时 ``distance_to_invalidation``
    为 ``None`` —— 卡 §7C 末句「无可靠阈值 → 只做 structural fragility，**不伪造
    quantitative fragility**」，所以这里没有"默认 0"，也没有"用 0 表示未知"。
    """
    out: List[Dict[str, Any]] = []
    for item in thresholds:
        record: Dict[str, Any] = dict(item.to_dict())
        resolved, note = threshold_source_resolves(
            graph, item.source_ref, assumption_registry, extra_evidence_ids=extra_evidence_ids
        )
        record["source_resolved"] = resolved
        record["source_resolution_note"] = note
        current, current_source, reason, active = _resolve_current_value(graph, item)
        record["resolved_current_value"] = current
        record["resolved_current_value_source"] = current_source
        record["node_active"] = active
        node_unit = _node_entry(graph, item.node_id)
        record["node_unit"] = (node_unit or {}).get("unit")
        record["evaluable"] = False
        record["eval_reason"] = ""
        record["unit_verified"] = False
        record["distance_to_invalidation"] = None
        record["at_threshold"] = False
        record["violated"] = False
        record["rejected_reason"] = ""
        if not resolved:
            record["rejected_reason"] = REJECT_UNRESOLVABLE_SOURCE
            out.append(record)
            continue
        declared = canonical_unit_token(item.unit)
        actual = canonical_unit_token(record["node_unit"])
        if declared and actual and declared != actual:
            record["eval_reason"] = EVAL_UNIT_MISMATCH
            record["rejected_reason"] = REJECT_UNIT_MISMATCH
            out.append(record)
            continue
        record["unit_verified"] = bool(declared and actual)
        if current is None:
            record["eval_reason"] = reason or EVAL_NO_CURRENT
            out.append(record)
            continue
        distance = _distance(item.comparison, current, float(item.threshold))
        if abs(distance) <= _TOLERANCE * max(1.0, abs(float(item.threshold))):
            distance = 0.0
        record["evaluable"] = True
        record["distance_to_invalidation"] = distance
        record["at_threshold"] = distance == 0.0
        record["violated"] = distance < 0
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# 假设脆弱性
# ---------------------------------------------------------------------------
def identify_fragile_assumptions(
    graph: ClaimGraph,
    assumptions: Any = None,
) -> List[Dict[str, Any]]:
    """每条被引用的假设的**来源成色**；没有注册表就明确写"未核实".

    ``assumptions`` 是 CARD-08 的 ``AssumptionRegistry``（鸭子类型：``has`` / ``get``）。
    不传注册表**不等于**假设都没问题 —— 返回的每一条都会是 ``status="unverified"``。
    """
    referenced: Dict[str, List[str]] = {}
    for claim in graph.claims.values():
        ids = list(claim.assumption_ids) + [
            ref.target_id
            for ref in claim.dependency_metadata
            if ref.target_type == TARGET_ASSUMPTION
        ]
        for assumption_id in dict.fromkeys(ids):
            referenced.setdefault(str(assumption_id), []).append(claim.claim_id)

    out: List[Dict[str, Any]] = []
    for assumption_id in sorted(referenced):
        claims = sorted(referenced[assumption_id])
        record: Dict[str, Any] = {
            "assumption_id": assumption_id,
            "claims": claims,
            "status": "unverified",
            "grade": "",
            "reason": "no_assumption_registry",
            "value": None,
            "source": "",
        }
        if assumptions is not None:
            has = getattr(assumptions, "has", None)
            registered = False
            try:
                registered = bool(callable(has) and has(assumption_id))
            except Exception:  # pragma: no cover - defensive
                registered = False
            if not registered:
                record.update(status="unresolved", reason="not_registered")
            else:
                item = assumptions.get(assumption_id)
                sourced = bool(getattr(item, "sourced", False))
                record.update(
                    status="sourced" if sourced else "assumption_grade",
                    grade=str(getattr(item, "grade", "")),
                    reason="" if sourced else "no_sourced_basis",
                    value=getattr(item, "value", None),
                    source=str(getattr(item, "source", "")),
                )
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# D. Monitoring Plan 生成（卡 §7D）
# ---------------------------------------------------------------------------
_STRUCTURAL_TRIGGER = "该依赖被撤回 / 被取代 / 无法解析 → CARD-09 传播判定为 BLOCKED 或 STALE"


def _observable_event(graph: ClaimGraph, node_id: str, node_type: str) -> Tuple[str, bool, str]:
    """可观察 KPI 事件只能来自节点**自己声明**的 metric；没声明就如实说没映射."""
    entry = _node_entry(graph, node_id)
    if entry is None:
        return "", False, "unresolvable_node"
    metric = entry.get("metric")
    if metric:
        if node_type == TARGET_CALCULATION:
            return f"recompute:{metric}:{node_id}", True, ""
        return f"metric_update:{metric}", True, ""
    if node_type == TARGET_EVIDENCE:
        evidence = graph.evidence_index.get(node_id)
        if evidence is not None and evidence.snapshot_id:
            return f"snapshot_refresh:{evidence.snapshot_id}", True, ""
        return "", False, "evidence_declares_no_metric"
    return "", False, f"{node_type}_declares_no_metric"


def generate_monitoring_plans(
    graph: ClaimGraph, report: FragilityReport
) -> List[MonitoringPlan]:
    """关键依赖 → 监控计划（卡 §7D）.

    触发条件分两类且**不混用**：有白名单阈值时为 ``numeric``（带当前值与距离），
    否则为 ``structural``（只有状态变化）。没有阈值就用一个编出来的数字当触发线，
    正是本卡要禁止的那件事。
    """
    by_node: Dict[str, List[Dict[str, Any]]] = {}
    for item in report.numeric_thresholds:
        by_node.setdefault(str(item.get("node_id")), []).append(item)

    plans: List[MonitoringPlan] = []
    for dependency in report.critical_dependencies:
        node_id = str(dependency.get("node_id"))
        node_type = str(dependency.get("node_type") or "")
        event, mapped, reason = _observable_event(graph, node_id, node_type)
        entries = by_node.get(node_id) or []
        if entries:
            item = entries[0]
            trigger_kind = TRIGGER_NUMERIC
            trigger = (
                f"{item.get('metric') or node_id} {item.get('comparison')} "
                f"{item.get('threshold')}{item.get('unit') or ''}"
                f"（当前 {item.get('resolved_current_value')}；"
                f"距离失效 {item.get('distance_to_invalidation')}）"
            )
        else:
            trigger_kind = TRIGGER_STRUCTURAL
            trigger = _STRUCTURAL_TRIGGER
        affected = tuple(sorted(downstream_closure(graph, {node_id})))
        plans.append(
            MonitoringPlan(
                dependency=node_id,
                observable_kpi_event=event,
                trigger_condition=trigger,
                affected_claims=affected,
                revalidation_action=REVALIDATION_REFRESH,
                trigger_kind=trigger_kind,
                mapped=mapped,
                unmapped_reason=reason,
                node_type=node_type,
            )
        )
    return plans


# ---------------------------------------------------------------------------
# CARD-23 护照状态流入
# ---------------------------------------------------------------------------
def _worst_status(statuses: Sequence[str]) -> str:
    """最严重的那个状态。

    CARD-23 的 ``STATUS_RANK`` 把 ``rank=0`` 给了**最严重**的 BLOCKED
    （``STATUS_PRECEDENCE`` 是从严重到宽松排列的），所以这里取 **最小** rank。
    取反了会把 BLOCKED 的护照汇总成 VERIFIED —— 正好是这类汇总量最危险的错法。
    """
    if not statuses:
        return ""
    ranked = [str(item) for item in statuses if item in STATUS_RANK]
    if not ranked:
        return ""
    return min(ranked, key=lambda item: STATUS_RANK[item])


def _verification_view(
    graph: ClaimGraph, theses: Sequence[Thesis], statuses: Optional[Mapping[str, str]]
) -> Dict[str, Any]:
    """CARD-23 五态流入的**只读**视图（卡 §10）.

    没给状态就 ``verification_available=False``。**不能**把"没验过"渲染成
    "都验过" —— 这正是 CARD-23 §13 要防的那类静默。
    """
    if not statuses:
        return {
            "verification_available": False,
            "claims": {},
            "theses": {},
            "precedence": list(STATUS_PRECEDENCE),
        }
    claims = {cid: str(statuses[cid]) for cid in sorted(graph.claims) if cid in statuses}
    theses_view: Dict[str, Any] = {}
    for thesis in theses:
        members = [cid for cid in thesis.claim_ids]
        member_status = {cid: claims[cid] for cid in members if cid in claims}
        theses_view[thesis.thesis_id] = {
            "claims": member_status,
            "worst_status": _worst_status(list(member_status.values())),
            "blocked_by_passport": sorted(
                cid
                for cid, status in member_status.items()
                if status in (STATUS_BLOCKED, STATUS_CONFLICTED)
            ),
            "unverified_claims": sorted(cid for cid in members if cid not in claims),
        }
    return {
        "verification_available": True,
        "claims": claims,
        "theses": theses_view,
        "precedence": list(STATUS_PRECEDENCE),
    }


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------
def build_fragility_report(
    graph: ClaimGraph,
    thesis_ids: Optional[Iterable[str]] = None,
    thresholds: Optional[Sequence[FragilityThreshold]] = None,
    *,
    assumptions: Any = None,
    verification_statuses: Optional[Mapping[str, str]] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_sets: int = DEFAULT_MAX_SETS,
    thesis_source: str = "",
) -> FragilityReport:
    """一次完整的脆弱性分析（卡 §8 的汇总入口）."""
    reports_theses = scope_theses(graph, thesis_ids)
    report = FragilityReport(
        run_id=graph.run_id,
        thesis_ids=[thesis.thesis_id for thesis in reports_theses],
        thesis_id=reports_theses[0].thesis_id if len(reports_theses) == 1 else "",
        thesis_source=thesis_source,
    )
    report.verification = _verification_view(graph, reports_theses, verification_statuses)

    if not graph.claims:
        report.status = STATUS_UNAVAILABLE
        report.unavailable_reason = "graph_has_no_claims"
        return report
    if not reports_theses:
        report.status = STATUS_UNAVAILABLE
        report.unavailable_reason = "no_theses_declared"
        return report

    report.critical_dependencies = identify_critical_nodes(
        graph, [thesis.thesis_id for thesis in reports_theses], max_depth=max_depth
    )

    required_edges_out: List[Dict[str, Any]] = []
    cut_sets_by_thesis: Dict[str, List[List[str]]] = {}
    unified: List[List[str]] = []
    truncated = False
    pruned = False
    for thesis in reports_theses:
        for claim_id in thesis.required_claims():
            if claim_id not in graph.claims:
                continue
            for source, target, target_type in required_edges(graph, claim_id, max_depth=max_depth):
                required_edges_out.append(
                    {
                        "thesis_id": thesis.thesis_id,
                        "source": source,
                        "source_type": _node_kind(graph, source),
                        "target": target,
                        "target_type": target_type,
                        "target_label": (_node_entry(graph, target) or {}).get("label", ""),
                    }
                )
        detail = _cut_sets_detail(
            graph,
            thesis.thesis_id,
            max_depth=max_depth,
            max_sets=max_sets,
        )
        cut_sets_by_thesis[thesis.thesis_id] = detail["cuts"]
        truncated = truncated or bool(detail["truncated"])
        pruned = pruned or bool(detail["pruned"])
        if detail["note"]:
            report.warnings.append(f"{detail['note']}:{thesis.thesis_id}")
        for cut in detail["cuts"]:
            if cut not in unified:
                unified.append(cut)
    report.required_dependencies = sorted(
        required_edges_out,
        key=lambda item: (item["thesis_id"], item["source"], item["target"]),
    )
    report.cut_sets_by_thesis = cut_sets_by_thesis
    report.minimal_cut_sets = sorted(unified, key=lambda item: (len(item), item))
    report.cut_sets_truncated = truncated
    report.pruned = pruned

    # -- 数值脆弱性 ------------------------------------------------------
    if thresholds:
        evaluated = compute_numeric_fragility(graph, list(thresholds), assumption_registry=assumptions)
        for item in evaluated:
            if item.get("rejected_reason"):
                report.rejected_thresholds.append(
                    {
                        "node_id": item.get("node_id"),
                        "metric": item.get("metric"),
                        "reason": item["rejected_reason"],
                        "source_type": item.get("source_type"),
                        "source_ref": item.get("source_ref"),
                        "threshold": item.get("threshold"),
                        "unit": item.get("unit"),
                        "eval_reason": item.get("eval_reason", ""),
                    }
                )
            else:
                report.numeric_thresholds.append(item)
    else:
        report.warnings.append("numeric_fragility_skipped:no_thresholds_declared")

    # -- 假设 ------------------------------------------------------------
    assumption_records = identify_fragile_assumptions(graph, assumptions)
    report.unresolved_assumptions = [
        item for item in assumption_records if item.get("status") != "sourced"
    ]
    if assumptions is None and assumption_records:
        report.warnings.append("assumption_registry_absent:unverified")
    if len(report.unresolved_assumptions) < len(assumption_records):
        sourced = len(assumption_records) - len(report.unresolved_assumptions)
        report.warnings.append(f"assumptions_sourced:{sourced}")

    # -- 监控计划 --------------------------------------------------------
    report.monitoring_metrics = [
        plan.to_dict() for plan in generate_monitoring_plans(graph, report)
    ]
    for plan in report.monitoring_metrics:
        if not plan["mapped"]:
            report.warnings.append(f"monitoring_unmapped:{plan['dependency']}:{plan['unmapped_reason']}")

    if not any(item["thesis_ids"] for item in report.critical_dependencies):
        report.status = STATUS_UNAVAILABLE
        report.unavailable_reason = REASON_NO_REQUIRED
    elif pruned or truncated or report.rejected_thresholds:
        report.status = STATUS_PARTIAL
    else:
        report.status = STATUS_OK
    return report


__all__ = [
    "build_fragility_report",
    "canonical_unit_token",
    "compute_minimal_cut_sets",
    "compute_numeric_fragility",
    "cut_blocks_thesis",
    "detect_single_points_of_failure",
    "generate_monitoring_plans",
    "identify_critical_nodes",
    "identify_fragile_assumptions",
    "node_type_of",
    "required_ancestry",
    "required_edges",
    "scope_theses",
    "simulate_cut",
    "threshold_source_resolves",
]
