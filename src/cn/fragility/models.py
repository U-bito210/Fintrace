"""CARD-24 — Thesis Fragility 的数据模型.

三条设计约束（都来自卡本身，且是可被测试守住的）：

1. **复用而非重造。** Thesis 的表示是 CARD-09 的 :class:`~src.cn.graph.Thesis`，
   节点类型沿用 CARD-09 的 ``TARGET_*`` 词表。本包不定义第二套 thesis 模型 ——
   两套表示迟早会分叉，而分叉的那一天就是「结论状态」出现两个真相的那一天。
2. **阈值只认白名单来源**（§7C / §13）。:class:`FragilityThreshold` 在**构造期**
   就拒绝非白名单的 ``source_type``：不是"事后校验"，而是"根本构造不出来"。
   数据层面的问题（来源 id 解析不了）则**降级并记账**，不抛异常 —— 见 engine。
3. **零静默。** 判定不了就显式写下来：``unavailable_reason`` /
   ``rejected_thresholds`` / ``mapped=False`` / ``evaluable=False``。
   「结构性脆弱」与「数值性脆弱」是两件事，前者推不出来时**绝不**用后者填补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 比较算子
# ---------------------------------------------------------------------------
COMPARISON_GTE = ">="
COMPARISON_GT = ">"
COMPARISON_LTE = "<="
COMPARISON_LT = "<"
COMPARISON_OPERATORS: Tuple[str, ...] = (COMPARISON_GTE, COMPARISON_GT, COMPARISON_LTE, COMPARISON_LT)

#: 阈值来源类型白名单（卡 §7C / §13）。**这是封闭集合**：LLM 不能新增一种来源。
THRESHOLD_PUBLIC_ASSUMPTION = "public_assumption"
THRESHOLD_ANALYST_INPUT = "analyst_input"
THRESHOLD_MODEL_CONSTRAINT = "model_constraint"
THRESHOLD_SOURCE_TYPES: Tuple[str, ...] = (
    THRESHOLD_PUBLIC_ASSUMPTION,
    THRESHOLD_ANALYST_INPUT,
    THRESHOLD_MODEL_CONSTRAINT,
)

# ---------------------------------------------------------------------------
# 报告状态与原因标记
# ---------------------------------------------------------------------------
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
FRAGILITY_STATUSES: Tuple[str, ...] = (STATUS_OK, STATUS_PARTIAL, STATUS_UNAVAILABLE)

REASON_NO_THESES = "no_theses_declared"
REASON_NO_REQUIRED = "thesis_has_no_required_dependency"
REASON_THESIS_ALREADY_BLOCKED = "thesis_already_blocked"
REASON_NO_CLAIMS = "thesis_has_no_resolvable_claims"
REASON_GRAPH_EMPTY = "graph_has_no_claims"

#: 阈值被剔除的原因（进 ``rejected_thresholds``，不静默丢）。
REJECT_UNRESOLVABLE_SOURCE = "unresolvable_source_ref"
REJECT_CURRENT_VALUE = "current_value_unavailable"
REJECT_UNIT_MISMATCH = "unit_mismatch"

#: 无法评估时的原因（仍进 ``numeric_thresholds``，但 ``evaluable=False``）。
EVAL_OK = ""
EVAL_NO_CURRENT = "current_value_unavailable"
EVAL_UNIT_MISMATCH = "unit_mismatch"

# ---------------------------------------------------------------------------
# 监控计划
# ---------------------------------------------------------------------------
#: 依赖失效后的重验证动作（可枚举，便于断言；不是自由文本）。
REVALIDATION_REFRESH = "revalidate_claim_and_refresh_thesis"
REVALIDATION_ACTIONS: Tuple[str, ...] = (REVALIDATION_REFRESH,)

#: 触发条件的两类：数值触发（有白名单阈值）与结构触发（只有状态变化）。
TRIGGER_NUMERIC = "numeric"
TRIGGER_STRUCTURAL = "structural"

DEFAULT_MAX_DEPTH = 8
#: 输出割集条数上限。卡给的 ``max_sets=20`` 是按**指数级枚举**估的预算；实测
#: （见 engine 模块头）CARD-09 的传播表让最小割集恒为单元素集，条数 = 必需祖先
#: 节点数，真实文档轻松超过 20，于是 20 会让每份报告都落进 ``cut_sets_truncated``。
#: 放宽到 200 并**保留截断标注**：截断照样如实上报，只是不再天天触发。
DEFAULT_MAX_SETS = 200


class FragilityError(ValueError):
    """输入本身不合法（角色未知、阈值来源不在白名单、thesis 引用悬空）。"""


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FragilityThreshold:
    """一条**数值**失效条件，例如「毛利率 >= 29.4%」。

    ``source_type`` 必须在白名单内 —— 这是 §7C「严禁 LLM 凭空创造阈值」在
    类型层面的实现。``source_ref`` 是这条阈值真正站得住的那个东西：
    一条证据 id、一个假设 id、或一个模型约束（计算）id。它**是否解析得了**
    由 engine 判定；解析不了就整条剔除并记账，而不是把阈值留着当真的用。

    ``current_value`` / ``current_value_source`` 是可选覆盖：当节点本身不是
    「一个可寻址的数值」（比如一条 claim）时，调用方可以显式给出当前值，
    但**必须**同时给出它来自哪里。没有来源的数字一律不接受。
    """

    node_id: str
    metric: str
    comparison: str
    threshold: float
    unit: str
    source_type: str
    source_ref: str
    period_key: Optional[str] = None
    label: str = ""
    rationale: str = ""
    current_value: Optional[float] = None
    current_value_source: str = ""

    def __post_init__(self) -> None:
        if self.comparison not in COMPARISON_OPERATORS:
            raise FragilityError(f"未知的比较算子: {self.comparison!r}")
        if self.source_type not in THRESHOLD_SOURCE_TYPES:
            raise FragilityError(
                f"阈值来源类型不在白名单: {self.source_type!r}；"
                f"只接受 {', '.join(THRESHOLD_SOURCE_TYPES)}。"
                "LLM 不得凭空创造阈值来源。"
            )
        if not str(self.source_ref).strip():
            raise FragilityError("阈值必须给出 source_ref：没有依据的阈值不是阈值。")
        if self.current_value is not None and not str(self.current_value_source).strip():
            raise FragilityError(
                "显式给出 current_value 时必须同时给出 current_value_source："
                "无法追溯来源的当前值不予接受。"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "metric": self.metric,
            "comparison": self.comparison,
            "threshold": self.threshold,
            "unit": self.unit,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "period_key": self.period_key,
            "label": self.label,
            "rationale": self.rationale,
            "current_value": self.current_value,
            "current_value_source": self.current_value_source,
        }


# ---------------------------------------------------------------------------
# 监控计划
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonitoringPlan:
    """一个关键依赖 → 可观察事件 → 触发条件 → 受影响结论 → 重验证动作。

    ``mapped`` 是这条计划的诚实度：KPI 事件**必须**来自节点自己声明的 metric
    （证据/计算的 metric 字段）。节点没有声明 metric 时 ``mapped=False`` 且
    给出 ``unmapped_reason``，绝不编一个 KPI 出来。
    """

    dependency: str
    observable_kpi_event: str
    trigger_condition: str
    affected_claims: Tuple[str, ...]
    revalidation_action: str
    trigger_kind: str = TRIGGER_STRUCTURAL
    mapped: bool = True
    unmapped_reason: str = ""
    node_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dependency": self.dependency,
            "observable_kpi_event": self.observable_kpi_event,
            "trigger_condition": self.trigger_condition,
            "affected_claims": list(self.affected_claims),
            "revalidation_action": self.revalidation_action,
            "trigger_kind": self.trigger_kind,
            "mapped": self.mapped,
            "unmapped_reason": self.unmapped_reason,
            "node_type": self.node_type,
        }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
@dataclass
class FragilityReport:
    """一次脆弱性分析的完整结果。

    字段形状对齐卡 §6，并额外携带**可追溯性**所需的三样东西：

    * ``thesis_source`` —— 这些 Thesis 是哪来的（声明式 / 推论式）；
    * ``rejected_thresholds`` —— 被剔除的阈值及其原因（等于零静默的账本）；
    * ``verification`` —— CARD-23 护照状态流入后的逐 claim 视图，
      ``verification_available=False`` 表示**没有**护照，而不是「都通过了」。

    ``critical_dependencies`` 是**节点**清单（跨 thesis 汇总）；
    ``required_dependencies`` 是**边**清单（source → target，逐 thesis），
    两者不重叠，分别对应「哪个节点最脆弱」与「逻辑是怎么搭起来的」。
    """

    run_id: str = ""
    thesis_id: str = ""
    thesis_ids: List[str] = field(default_factory=list)
    thesis_source: str = ""
    critical_dependencies: List[Dict[str, Any]] = field(default_factory=list)
    required_dependencies: List[Dict[str, Any]] = field(default_factory=list)
    minimal_cut_sets: List[List[str]] = field(default_factory=list)
    cut_sets_by_thesis: Dict[str, List[List[str]]] = field(default_factory=dict)
    numeric_thresholds: List[Dict[str, Any]] = field(default_factory=list)
    rejected_thresholds: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_assumptions: List[Dict[str, Any]] = field(default_factory=list)
    monitoring_metrics: List[Dict[str, Any]] = field(default_factory=list)
    verification: Dict[str, Any] = field(
        default_factory=lambda: {"verification_available": False, "claims": {}, "theses": {}}
    )
    status: str = STATUS_OK
    unavailable_reason: str = ""
    pruned: bool = False
    cut_sets_truncated: bool = False
    warnings: List[str] = field(default_factory=list)

    # -- inspection -------------------------------------------------------
    @property
    def single_points(self) -> List[Dict[str, Any]]:
        return [item for item in self.critical_dependencies if item.get("is_single_point")]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thesis_id": self.thesis_id,
            "thesis_ids": list(self.thesis_ids),
            "thesis_source": self.thesis_source,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "critical_dependencies": [dict(item) for item in self.critical_dependencies],
            "required_dependencies": [dict(item) for item in self.required_dependencies],
            "minimal_cut_sets": [list(item) for item in self.minimal_cut_sets],
            "cut_sets_by_thesis": {
                key: [list(item) for item in value] for key, value in self.cut_sets_by_thesis.items()
            },
            "numeric_thresholds": [dict(item) for item in self.numeric_thresholds],
            "rejected_thresholds": [dict(item) for item in self.rejected_thresholds],
            "unresolved_assumptions": [dict(item) for item in self.unresolved_assumptions],
            "monitoring_metrics": [dict(item) for item in self.monitoring_metrics],
            "verification": {
                "verification_available": bool(self.verification.get("verification_available")),
                "claims": dict(self.verification.get("claims") or {}),
                "theses": {k: dict(v) for k, v in (self.verification.get("theses") or {}).items()},
            },
            "pruned": self.pruned,
            "cut_sets_truncated": self.cut_sets_truncated,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Thesis 来源
# ---------------------------------------------------------------------------
THESIS_SOURCE_DECLARED = "declared"
THESIS_SOURCE_INFERENCES = "derived_from_inferences"
THESIS_SOURCE_NONE = "none"
THESIS_SOURCES: Tuple[str, ...] = (
    THESIS_SOURCE_DECLARED,
    THESIS_SOURCE_INFERENCES,
    THESIS_SOURCE_NONE,
)


__all__ = [
    "COMPARISON_GTE",
    "COMPARISON_GT",
    "COMPARISON_LTE",
    "COMPARISON_LT",
    "COMPARISON_OPERATORS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_SETS",
    "EVAL_NO_CURRENT",
    "EVAL_OK",
    "EVAL_UNIT_MISMATCH",
    "FRAGILITY_STATUSES",
    "FragilityError",
    "FragilityReport",
    "FragilityThreshold",
    "MonitoringPlan",
    "REASON_GRAPH_EMPTY",
    "REASON_NO_CLAIMS",
    "REASON_NO_REQUIRED",
    "REASON_NO_THESES",
    "REASON_THESIS_ALREADY_BLOCKED",
    "REJECT_CURRENT_VALUE",
    "REJECT_UNIT_MISMATCH",
    "REJECT_UNRESOLVABLE_SOURCE",
    "REVALIDATION_ACTIONS",
    "REVALIDATION_REFRESH",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "THESIS_SOURCE_DECLARED",
    "THESIS_SOURCE_INFERENCES",
    "THESIS_SOURCE_NONE",
    "THESIS_SOURCES",
    "THRESHOLD_ANALYST_INPUT",
    "THRESHOLD_MODEL_CONSTRAINT",
    "THRESHOLD_PUBLIC_ASSUMPTION",
    "THRESHOLD_SOURCE_TYPES",
    "TRIGGER_NUMERIC",
    "TRIGGER_STRUCTURAL",
]
