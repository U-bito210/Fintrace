"""CARD-24 Thesis Fragility Engine（投资逻辑脆弱性分析）.

回答的不是"这只票是多是空"，而是：**这套投资逻辑靠什么站着、哪一根最脆、
打断哪几根它就倒**。全部是确定性图算法，零 LLM。

    ClaimGraph + Thesis
        ├── identify_critical_nodes      → 哪些节点位于 REQUIRED 路径上、谁被多条逻辑共用
        ├── detect_single_points_of_failure → 单独失效即可击穿整条逻辑的节点
        ├── compute_minimal_cut_sets     → 使其失效所需破坏的最小 REQUIRED 集合
        ├── identify_fragile_assumptions → 哪些假设没有可追溯依据
        ├── compute_numeric_fragility    → 可选：距离阈值失效还有多远（阈值须有白名单来源）
        └── generate_monitoring_plans    → 依赖 → 可观察 KPI → 触发条件 → 重验证动作

三条贯穿全包的不变式：

* **复用 CARD-09。** Thesis、依赖角色、节点类型、传播语义都用 CARD-09 的，
  本包只在其上做图论；语义冲突以 CARD-09 §7b 为准（详见 engine 模块头）。
* **零 LLM。** 阈值来源是封闭白名单（§7C），割集是枚举 + 反解验证（§13）。
* **零静默。** 判不出来的东西显式记账：``status="unavailable"`` + ``unavailable_reason``、
  ``rejected_thresholds``、``mapped=False``、``pruned`` / ``cut_sets_truncated``。

快速开始::

    from src.cn.fragility import build_fragility_report, resolve_theses

    theses, source = resolve_theses(graph, [{"thesis_id": "T01", "statement": "盈利改善可持续",
                                             "claim_ids": ["claim_fact_gross_margin_2025FY"]}])
    report = build_fragility_report(graph, [t.thesis_id for t in theses])
    report.minimal_cut_sets, report.single_points
"""

from .engine import (
    build_fragility_report,
    canonical_unit_token,
    compute_minimal_cut_sets,
    compute_numeric_fragility,
    cut_blocks_thesis,
    detect_single_points_of_failure,
    generate_monitoring_plans,
    identify_critical_nodes,
    identify_fragile_assumptions,
    node_type_of,
    pivotal_nodes,
    required_ancestry,
    required_edges,
    scope_theses,
    simulate_cut,
    threshold_source_resolves,
)
from .models import (
    COMPARISON_GTE,
    COMPARISON_GT,
    COMPARISON_LTE,
    COMPARISON_LT,
    COMPARISON_OPERATORS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_SETS,
    EVAL_NO_CURRENT,
    EVAL_OK,
    EVAL_UNIT_MISMATCH,
    FRAGILITY_STATUSES,
    REASON_GRAPH_EMPTY,
    REASON_NO_CLAIMS,
    REASON_NO_REQUIRED,
    REASON_NO_THESES,
    REASON_THESIS_ALREADY_BLOCKED,
    REJECT_CURRENT_VALUE,
    REJECT_UNIT_MISMATCH,
    REJECT_UNRESOLVABLE_SOURCE,
    REVALIDATION_ACTIONS,
    REVALIDATION_REFRESH,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    THESIS_SOURCE_DECLARED,
    THESIS_SOURCE_INFERENCES,
    THESIS_SOURCE_NONE,
    THESIS_SOURCES,
    THRESHOLD_ANALYST_INPUT,
    THRESHOLD_MODEL_CONSTRAINT,
    THRESHOLD_PUBLIC_ASSUMPTION,
    THRESHOLD_SOURCE_TYPES,
    TRIGGER_NUMERIC,
    TRIGGER_STRUCTURAL,
    FragilityError,
    FragilityReport,
    FragilityThreshold,
    MonitoringPlan,
)
from .theses import (
    INFERRED_THESIS_PREFIX,
    register_theses,
    resolve_theses,
    theses_from_declaration,
    theses_from_inferences,
)

__all__ = [
    "COMPARISON_GTE", "COMPARISON_GT", "COMPARISON_LTE", "COMPARISON_LT", "COMPARISON_OPERATORS",
    "DEFAULT_MAX_CANDIDATES", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_SETS",
    "EVAL_NO_CURRENT", "EVAL_OK", "EVAL_UNIT_MISMATCH",
    "FRAGILITY_STATUSES",
    "FragilityError", "FragilityReport", "FragilityThreshold",
    "INFERRED_THESIS_PREFIX",
    "MonitoringPlan",
    "REASON_GRAPH_EMPTY", "REASON_NO_CLAIMS", "REASON_NO_REQUIRED", "REASON_NO_THESES",
    "REASON_THESIS_ALREADY_BLOCKED",
    "REJECT_CURRENT_VALUE", "REJECT_UNIT_MISMATCH", "REJECT_UNRESOLVABLE_SOURCE",
    "REVALIDATION_ACTIONS", "REVALIDATION_REFRESH",
    "STATUS_OK", "STATUS_PARTIAL", "STATUS_UNAVAILABLE",
    "THESIS_SOURCE_DECLARED", "THESIS_SOURCE_INFERENCES", "THESIS_SOURCE_NONE", "THESIS_SOURCES",
    "THRESHOLD_ANALYST_INPUT", "THRESHOLD_MODEL_CONSTRAINT", "THRESHOLD_PUBLIC_ASSUMPTION",
    "THRESHOLD_SOURCE_TYPES",
    "TRIGGER_NUMERIC", "TRIGGER_STRUCTURAL",
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
    "pivotal_nodes",
    "register_theses",
    "required_ancestry",
    "required_edges",
    "resolve_theses",
    "scope_theses",
    "simulate_cut",
    "theses_from_declaration",
    "theses_from_inferences",
    "threshold_source_resolves",
]
