"""主张-证据图谱页（CARD-09）—— DEMO_FLOW 步骤 9+10 的「点击结论 → 逐层展开」。

剧本现场操作：「点击'盈利质量下降'结论 → 图谱逐层展开：Claim C18 →
CFO/NI 计算 CAL12 → Evidence E51/E37 → 年报 P118/P96。」

本页把那句话做成**四层可点**的结构：结论 → 它用到的计算（含公式与逐项输入）
→ 输入证据 → 原始页码与红框。每一层都允许为空，而且**空要说出来**：一条没有
证据的 claim 在页面上必须看起来像问题，而不是被悄悄缩掉。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def _evidence_index(nodes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """证据节点 → 跳转所需的位置信息。没有页码的也进索引（页面据此显示"不可定位"）。"""
    index: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if node.get("type") != "evidence":
            continue
        index[str(node.get("id"))] = {
            "document_id": node.get("document_id"),
            "page": node.get("page"),
        }
    return index


def _render_claim(
    claim: Dict[str, Any],
    calculations: Dict[str, Dict[str, Any]],
    evidence: Dict[str, Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
) -> None:
    status = claim.get("propagated_status")
    tone = _shared.tone(status)
    box = {"error": st.error, "warning": st.warning, "success": st.success}.get(tone, st.info)
    box(f"状态 **{status}** · validation `{claim.get('validation_status')}` · "
        f"structural `{claim.get('structural_status')}`")
    for reason in claim.get("reasons") or ():
        st.markdown(f"- {reason}")

    st.markdown("**第 1 层 · 结论**")
    st.markdown(f"> {claim.get('text')}")

    st.markdown("**第 2 层 · 依赖的计算**")
    calc_ids = list(claim.get("calculation_ids") or ())
    if not calc_ids:
        st.caption("这条结论不依赖任何计算节点（水平值直接来自披露）。")
    for calc_id in calc_ids:
        node = calculations.get(calc_id) or {}
        with st.expander(f"`{calc_id}` · {node.get('metric') or ''} [{node.get('validation_status')}]"):
            st.code(str(node.get("formula") or "（未记录公式）"), language=None)
            inputs = list(node.get("inputs") or ())
            if inputs:
                st.dataframe(
                    [
                        {
                            "输入": item.get("label") or item.get("ref"),
                            "种类": item.get("kind"),
                            "值": item.get("value"),
                        }
                        for item in inputs
                    ],
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("该计算节点没有记录输入 —— 无输入的计算不可复算，这里如实显示。")

    st.markdown("**第 3 层 · 输入证据**")
    evidence_ids = list(claim.get("evidence_ids") or ())
    if not evidence_ids:
        st.warning("这条结论**没有绑定任何证据**。它不该被当作事实使用。")
    for evidence_id in evidence_ids:
        node = evidence.get(evidence_id) or {}
        page = node.get("page")
        st.markdown(
            f"- `{evidence_id}` · {node.get('label') or ''} · "
            + (f"**第 {page} 页**" if page else "**无页码（不可定位）**")
            + ("" if node.get("status") == "active" else f" · 状态 `{node.get('status')}`")
        )
        if page:
            _shared.jump_button(
                str(node.get("document_id") or ""), evidence_id,
                label=f"跳到第 {page} 页",
                why="在原始 PDF 上高亮该证据",
                index=index,
            )

    derived = list(claim.get("derived_from") or ())
    if derived:
        st.markdown("**第 4 层 · 推论来源**（这条结论从哪几条上游推出来）")
        for parent in derived:
            st.markdown(f"- `{parent}`")


def render() -> None:
    _shared.page_head("案例演示 · 离线演示材料", "主张-证据图谱", "Claim-Evidence Graph")
    st.caption(
        "DEMO_FLOW 步骤 9+10。四层结构：结论 → 计算 → 证据 → 原始页码。"
        "依赖语义分 REQUIRED / SUPPORTING / OPTIONAL，REQUIRED 上游失效会 fail-closed 阻断下游。"
    )

    key = _shared.case_picker(key="demo_graph_case")
    payload = demo_service.graph_view(key)
    if _shared.unavailable(payload, what="主张-证据图谱"):
        return
    _shared.case_brief(payload)

    counts = payload.get("counts") or {}
    _shared.stat_row(
        [
            ("结论", counts.get("claims")),
            ("计算节点", counts.get("calculations")),
            ("证据节点", counts.get("evidence")),
            ("可定位证据", counts.get("evidence_located"), "有页码、可跳回原始 PDF"),
            ("Thesis", counts.get("theses")),
            ("run_id", payload.get("run_id")),
        ]
    )

    _shared.sechead("① 结论清单（按状态）")
    claim_rows = list(payload.get("claim_rows") or ())
    if claim_rows:
        st.dataframe(
            [
                {
                    "claim_id": row.get("claim_id"),
                    "类型": row.get("claim_type"),
                    "正文": row.get("text"),
                    "指标": row.get("metric"),
                    "期间": row.get("period_key"),
                    "传播状态": row.get("propagated_status"),
                    "校验状态": row.get("validation_status"),
                    "证据数": len(row.get("evidence_ids") or ()),
                }
                for row in claim_rows
            ],
            width="stretch",
            hide_index=True,
        )
        blocked = [row for row in claim_rows if _shared.tone(row.get("propagated_status")) == "error"]
        if blocked:
            st.error(
                f"有 {len(blocked)} 条结论处于阻断/失效状态："
                + "、".join(f"`{row['claim_id']}`" for row in blocked)
                + "。它们不得进入对外结论。"
            )
    else:
        st.info("该文档未产出任何 claim。")

    _shared.sechead("② 逐层展开（点一条结论看它靠什么站着）")
    selectable = [row["claim_id"] for row in claim_rows]
    if selectable:
        chosen = st.selectbox("选择结论", selectable, key="demo_graph_claim")
        row = next(item for item in claim_rows if item["claim_id"] == chosen)
        calculations = {
            node["id"]: node for node in payload.get("nodes") or () if node.get("type") == "calculation"
        }
        evidence = {
            node["id"]: node for node in payload.get("nodes") or () if node.get("type") == "evidence"
        }
        _render_claim(row, calculations, evidence, _evidence_index(payload.get("nodes") or []))
    else:
        st.caption("没有可展开的结论。")

    warnings = list(payload.get("warnings") or ())
    if warnings:
        with st.expander(f"图谱构建告警（{len(warnings)} 条）", expanded=False):
            for item in warnings:
                st.markdown(f"- {item}")

    _shared.render_jump_panel()

    _shared.honesty(
        [
            "本页图谱来自**合成语料**的确定性构建（CARD-01 → 02 → 03 → 09），不是真实公司图谱。",
            "现有语料解析出的 claim **全部是 fact 级**，没有 inference —— 所以「按推论逐层展开」"
            "这条路当前无材料，Thesis 数为 0 属预期。",
            "没有页码的证据会显示为「不可定位」：它仍然出现在页面上，只是跳不过去 —— "
            "把不可定位的证据藏起来才是错的。",
        ]
    )
