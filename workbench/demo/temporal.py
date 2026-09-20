"""时间重验证页（CARD-25）—— DEMO_FLOW 失败注入 F9。

剧本 F9：「导入更正公告 → Temporal Revalidation 显示受影响 Fact/Calculation/
Claim/Valuation/Memo 段落 + 增量重算 impact。」

本页的关键不是"有个时间线"，而是**三态分开**：替代（旧值退出口径 → 下游需按新值
刷新，是 stale 而不是 blocked）、未裁决冲突（fail-closed 阻断）、新信息（需刷新但
不阻断）。把三者混成一句"数据变了"，整页就没有信息量。

增量重算刻意与全量对账：``full_vs_incremental`` 就是那次对账的读数 —— 只报增量
而不敢跟全量比，等于宣称自己没算错。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def render() -> None:
    st.subheader("时间重验证 · Temporal Revalidation")
    st.caption(
        "DEMO_FLOW 失败注入 F9。一次「世界变了」的事件进来后，沿版本血缘找出受影响的"
        "Fact / Calculation / Claim，并给出重验证动作。零 LLM。"
    )

    key = _shared.case_picker(key="demo_temporal_case")
    payload = demo_service.temporal_view(key)
    if _shared.unavailable(payload, what="时间重验证"):
        # 这一分支是**正常结论**而不是故障：不是每份语料都存在同指标跨期的两条证据，
        # 而替代事件要求恰好那样一对。缺材料就缺材料。
        st.caption(
            "替代（VERSION_SUPERSESSION）的语义是「同一口径上，新的披露顶掉旧的」，"
            "所以需要同指标跨期的两条证据才构成得了一次事件。缺这一对时引擎拒答 —— "
            "这正是它该做的事。"
        )
        return
    _shared.case_brief(payload)

    engine_status = payload.get("revalidation_status")
    if engine_status and engine_status != "ok":
        st.warning(f"引擎判定本次重验证状态为 **{engine_status}**：{payload.get('unavailable_reason') or '未说明'}")

    event = payload.get("event") or {}
    _shared.stat_row(
        [
            ("事件类型", event.get("event_type")),
            ("变更性质", event.get("event_kind"), "三态之一：替代 / 未裁决冲突 / 新信息"),
            ("事件日期", event.get("event_date") or "（未给，见告警）"),
            ("被替代（旧）", payload.get("old_id")),
            ("替代者（新）", payload.get("new_id")),
            ("引擎状态", engine_status),
        ]
    )
    if event.get("description"):
        st.caption(event["description"])

    st.markdown("#### ① 影响面（哪些东西被这次变化波及）")
    impact: Dict[str, Any] = payload.get("impact") or {}
    _shared.stat_row(
        [
            ("事实被替代", impact.get("facts_superseded")),
            ("计算过期", impact.get("calculations_stale")),
            ("结论失效", impact.get("claims_invalidated")),
            ("Thesis 削弱", impact.get("theses_weakened")),
            ("估值过期", impact.get("valuations_stale"), "CARD-08 域，未提供时如实为 None"),
            ("备忘录段落", impact.get("memo_sections_affected"), "CARD-10 域，未提供时如实为 None"),
        ]
    )
    if impact.get("valuations_stale") is None or impact.get("memo_sections_affected") is None:
        st.caption(
            "`None` 表示该外部域本次没有输入 —— **不等于 0 个受影响**。"
            "把「没查」显示成「没问题」是这一页最容易犯的错。"
        )

    st.markdown("#### ② 受影响子图")
    subgraph: List[str] = list(payload.get("affected_subgraph") or ())
    if subgraph:
        for node in subgraph:
            st.markdown(f"- `{node}`")
    else:
        st.caption("受影响子图为空。")

    st.markdown("#### ③ 状态变化 diff（旧 → 新）")
    diff: List[Dict[str, Any]] = list(payload.get("state_diff") or ())
    if diff:
        st.dataframe(diff, width="stretch", hide_index=True)
    else:
        st.caption("无状态变化记录（本次变更没有改变任何节点的判定状态）。")

    st.markdown("#### ④ 需要做的重验证动作")
    actions: List[Dict[str, Any]] = list(payload.get("revalidation_actions") or ())
    if actions:
        st.dataframe(
            [
                {
                    "动作": item.get("action_type"),
                    "目标": item.get("target_id"),
                    "优先级": item.get("priority"),
                    "原因": item.get("reason"),
                }
                for item in actions
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("本次未产出待办动作。")

    st.markdown("#### ⑤ 增量重算 vs 全量重算（对账读数）")
    comparison: Dict[str, Any] = payload.get("full_vs_incremental") or {}
    _shared.stat_row(
        [
            ("增量重算节点", comparison.get("nodes_recomputed")),
            ("全量节点", comparison.get("full_nodes")),
            ("结果一致", "是" if comparison.get("results_identical") else "**否**"),
            ("回落全量", "是" if comparison.get("fell_back") else "否"),
            ("耗时", comparison.get("latency_ms"), "观测值，不参与判定"),
        ]
    )
    st.caption(
        "增量是对**限深子图**做真有界重算，再与全量**每次现场对账**；不一致就回落全量并记录。"
        "「结果一致=是」才是这条捷径成立的依据。"
    )

    lineage = payload.get("lineage")
    if lineage:
        with st.expander(f"血缘（{len(lineage)} 条）", expanded=False):
            st.dataframe(lineage, width="stretch", hide_index=True)

    warnings = list(payload.get("warnings") or ())
    if warnings:
        with st.expander(f"引擎告警（{len(warnings)} 条，含「哪些域本次没查」）", expanded=True):
            for item in warnings:
                st.markdown(f"- {item}")

    _shared.honesty(
        [
            "**替代事件是本演示构造的一对同指标跨期证据**，不是真实更正公告 —— "
            "现有语料里的公告解析不出正文事实，CARD-25 已记录该缺口。",
            "估值（CARD-08）与备忘录段落（CARD-10）的受影响面需由调用方显式传入；"
            "未传时显示为 None 并在告警里说明，不按 0 计。",
            "本页数字全部来自合成语料的确定性图算法，不得表述为真实公司的重验证结论。",
        ]
    )
