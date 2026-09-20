"""逻辑脆弱性页（CARD-24）—— DEMO_FLOW 步骤 10b，创新展示三。

剧本话术："FinTrace 还能回答一个传统研报无法回答的问题——**你的投资逻辑最依赖
什么？最少破坏哪些节点会让它失效？** 系统自动识别关键 REQUIRED 依赖、单点依赖、
脆弱假设，并生成 Monitoring Plan。"

**Thesis 的来源必须在页面上说清楚。** 现有语料解析出的 claim 全是 ``fact``，
没有一条 ``inference``（CARD-24 已记录该语料缺口），所以推论式 Thesis 这条路
当前没有材料；本页改用**显式声明**的一条 Thesis（依赖图上全部指标事实，REQUIRED）。
``thesis_source`` 如实显示为 ``declared`` —— "分析对象是哪来的"不能是隐含信息，
否则整页结论都站在一句没交代过的前提上。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def render() -> None:
    st.subheader("逻辑脆弱性 · Thesis Fragility")
    st.caption(
        "DEMO_FLOW 步骤 10b（创新展示三）。关键依赖 / 单点失效 / 最小割集全部是"
        "**图算法**结果，每个割集都经过「移除后 Thesis 是否 BLOCKED」的反解验证。零 LLM。"
    )

    key = _shared.case_picker(key="demo_fragility_case")
    payload = demo_service.fragility_view(key)
    if _shared.unavailable(payload, what="脆弱性分析"):
        return
    _shared.case_brief(payload)

    engine_status = payload.get("fragility_status")
    if engine_status and engine_status != "ok":
        st.warning(
            f"引擎判定本次脆弱性分析状态为 **{engine_status}**，原因："
            f"{payload.get('unavailable_reason') or '未说明'}。下面各项按引擎实际产出的内容展示。"
        )

    _shared.stat_row(
        [
            ("Thesis 来源", payload.get("thesis_source"), payload.get("thesis_source_note")),
            ("Thesis 数", len(payload.get("thesis_ids") or ())),
            ("关键依赖", len(payload.get("critical_dependencies") or ())),
            ("最小割集", len(payload.get("minimal_cut_sets") or ())),
            ("监控指标", len(payload.get("monitoring_metrics") or ())),
            ("割集截断", payload.get("cut_sets_truncated") or payload.get("pruned"), "为真则结果不是全局最优"),
        ]
    )

    st.markdown("#### ① 关键依赖（哪些节点一旦失效，逻辑就翻）")
    critical: List[Dict[str, Any]] = list(payload.get("critical_dependencies") or ())
    if critical:
        st.dataframe(
            [
                {
                    "节点": item.get("node_id"),
                    "类型": item.get("node_type"),
                    "标签": item.get("label"),
                    "深度": item.get("depth"),
                    "单点依赖": "是" if item.get("is_single_point") else "",
                    "归属 Thesis": "、".join(item.get("thesis_ids") or ()),
                }
                for item in critical
            ],
            width="stretch",
            hide_index=True,
        )
        singles = [item for item in critical if item.get("is_single_point")]
        if singles:
            st.warning(
                "单点依赖（没有替代来源，失效即断）："
                + "、".join(f"`{item.get('node_id')}`" for item in singles)
            )
    else:
        st.info("引擎未识别出关键依赖。这可能意味着依赖结构单薄，而不是「很稳健」。")

    st.markdown("#### ② 最小割集（最少破坏哪些节点会让 Thesis 失效）")
    cut_sets: List[Any] = list(payload.get("minimal_cut_sets") or ())
    if cut_sets:
        for position, cut in enumerate(cut_sets, start=1):
            nodes = cut if isinstance(cut, (list, tuple)) else [cut]
            st.markdown(f"- **割集 {position}**（{len(nodes)} 个节点）：" + "、".join(f"`{n}`" for n in nodes))
        st.caption(
            "每个割集都做过反解验证：真的移除这些节点，Thesis 会变 BLOCKED。"
            "若 §16 标注了 pruned / truncated，则说明结果不是全局最优 —— 已如实标出。"
        )
    else:
        st.info("未产出最小割集。")

    st.markdown("#### ③ Monitoring Plan（什么信号触发重验证）")
    plans: List[Dict[str, Any]] = list(payload.get("monitoring_metrics") or ())
    if plans:
        st.dataframe(
            [
                {
                    "依赖": item.get("dependency"),
                    "可观测 KPI 事件": item.get("observable_kpi_event"),
                    "触发条件": item.get("trigger_condition"),
                    "受影响结论": "、".join(item.get("affected_claims") or ()),
                    "重验证动作": item.get("revalidation_action"),
                }
                for item in plans
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("未产出监控计划。")

    thresholds = list(payload.get("numeric_thresholds") or ())
    rejected = list(payload.get("rejected_thresholds") or ())
    assumptions = list(payload.get("unresolved_assumptions") or ())
    with st.expander(
        f"阈值与假设（已采用 {len(thresholds)} · 已剔除 {len(rejected)} · 未解假设 {len(assumptions)}）",
        expanded=False,
    ):
        st.markdown(
            "阈值来源是**封闭白名单**：解析不了来源的阈值在构造期就被剔除并记入 "
            "`rejected_thresholds`，不会被「尽量解析」成一条来路不明的数字。"
        )
        for title, items in (("已采用", thresholds), ("已剔除", rejected), ("未解的假设", assumptions)):
            st.markdown(f"**{title}**")
            if not items:
                st.caption("（空）")
            for item in items:
                st.markdown(f"- `{item}`")

    warnings = list(payload.get("warnings") or ())
    if warnings:
        with st.expander(f"引擎告警（{len(warnings)} 条）", expanded=False):
            for item in warnings:
                st.markdown(f"- {item}")

    _shared.render_jump_panel()

    _shared.honesty(
        [
            "**Thesis 是本演示显式声明的**（`thesis_source=declared`），不是从研报叙事里解析出来的 —— "
            "把研报叙事解析成 Thesis 属 LLM 职责，不在本卡范围。",
            "现有语料没有 inference claim，所以「由推论自动提升为 Thesis」这条路暂无材料；"
            "CARD-24 §23 已记录该语料缺口。",
            "零阈值输入时只做结构脆弱性，`distance` 严格为 None —— 系统不猜一个阈值来完成演示。",
            "本页数字全部来自合成语料的确定性图算法，不得表述为真实研报的脆弱性结论。",
        ]
    )
