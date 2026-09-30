"""ACME 约束页（CARD-22）—— DEMO_FLOW 步骤 4b，创新展示一。

剧本话术 5 秒版：「传统 OCR 提取完就结束了。FinTrace 多做一步——**用会计约束
自检解析质量**：资产=负债+权益、YTD 与单季可推导、跨期数据连续、正文和表格
矛盾能发现。任何'不可能成立'的解析结果，系统立即定位报警。」

本页必须同时展示**没检查的约束族**：一张只有"违规 0 条"的漂亮列表，可能只是
规则压根没跑。``families_not_checked`` / ``constraints_not_available`` 因此和
违规列表同等显眼 —— 那才是"零静默"。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def _ref_index(violations: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把每条 source_ref 编成跳转索引，键是 ``violation_id::序号``。

    约束违反的取证对象是**表格里的若干单元格**，不是一个 evidence_id；
    所以索引键自己造，值里带 ``document_id`` / ``page`` / ``bbox``。
    """
    index: Dict[str, Dict[str, Any]] = {}
    for violation in violations:
        for position, ref in enumerate(violation.get("source_refs") or ()):
            if not ref.get("page"):
                continue
            index[f"{violation.get('violation_id')}::{position}"] = {
                "document_id": ref.get("document_id"),
                "page": ref.get("page"),
                "bbox": ref.get("bbox"),
                "locator": ref.get("locator"),
            }
    return index


def _violation_view(violation: Dict[str, Any], index: Dict[str, Any], position: int) -> None:
    status = violation.get("status")
    tone = _shared.tone(status)
    title = (
        f"{violation.get('constraint_family_label') or violation.get('constraint_family')}"
        f" · {violation.get('constraint_id')}"
    )
    box = {"error": st.error, "warning": st.warning, "success": st.success}.get(tone, st.info)
    box(f"[{status}] {title}")
    if violation.get("note"):
        st.caption(violation["note"])
    left, right = st.columns(2)
    with left:
        st.markdown(
            f"- 应然值：`{violation.get('expected_value')}`\n"
            f"- 实际值：`{violation.get('actual_value')}`\n"
            f"- 残差：`{violation.get('residual')}`\n"
            f"- 容差：`{violation.get('tolerance')}`"
        )
    with right:
        st.markdown(
            f"- 超容差：`{violation.get('exceeds_tolerance')}`\n"
            f"- 严重度：`{violation.get('severity')}`\n"
            f"- 复核状态：`{violation.get('resolution_status')}`\n"
            f"- 输入项：{len(violation.get('inputs') or ())} 个"
        )
    inputs = list(violation.get("inputs") or ())
    if inputs:
        st.dataframe(
            [
                {
                    "输入": item.get("label") or item.get("ref"),
                    "值": item.get("value"),
                    "单位": item.get("unit"),
                    "期间": item.get("period"),
                    "口径": item.get("scope"),
                    "版本": item.get("version"),
                }
                for item in inputs
            ],
            width="stretch",
            hide_index=True,
        )
    refs = list(violation.get("source_refs") or ())
    if refs:
        st.caption("取证位置（红框落在披露单元格上）：")
        columns = st.columns(min(len(refs), 3))
        for offset, ref in enumerate(refs):
            with columns[offset % len(columns)]:
                if not ref.get("page"):
                    st.caption(f"`{ref.get('locator') or '—'}` · 不可定位")
                    continue
                key = f"{violation.get('violation_id')}::{offset}"
                _shared.jump_button(
                    str(ref.get("document_id") or ""), key,
                    label=f"第 {ref.get('page')} 页 · {ref.get('locator') or '原文'}",
                    why="跳到披露该数值的位置",
                    index=index,
                )


def render() -> None:
    _shared.page_head("案例演示 · 离线演示材料", "ACME 约束", "Accounting-Constrained Multimodal Evidence")
    st.caption(
        "DEMO_FLOW 步骤 4b（创新展示一）。六类会计约束族对**解析质量**自检 —— "
        "「不可能成立」的解析结果当场报警，而不是默默信任。零模型、纯确定性规则。"
    )

    key = _shared.case_picker(key="demo_acme_case", default=demo_service.DEFAULT_ACME_KEY)
    payload = demo_service.acme_view(key)
    if _shared.unavailable(payload, what="ACME 约束检查"):
        return
    _shared.case_brief(payload)

    violations = list(payload.get("violations") or ())
    totals: Dict[str, Any] = payload.get("totals") or {}
    checked = list(payload.get("constraint_families_checked") or ())
    not_checked = list(payload.get("families_not_checked") or ())

    _shared.stat_row(
        [
            ("已检查族", len(checked), "、".join(checked) or "无"),
            ("未检查族", len(not_checked), "、".join(not_checked) or "无"),
            ("违规", totals.get("violated", 0), "超容差，必须进复核队列"),
            ("告警", totals.get("warning", 0), None),
            ("通过", totals.get("passed", 0), None),
            ("不可比", totals.get("not_comparable", 0), "口径不同，不按违规计"),
        ]
    )
    if not_checked:
        st.warning(
            "以下约束族**本次没有检查**：" + "、".join(not_checked)
            + "。这是缺输入，不是「全部通过」。"
        )
    index = _ref_index(violations)

    _shared.sechead("① 约束违反清单（含 passed，不筛选）")
    if not violations:
        st.info("本次未产出任何约束结果。")
    for position, violation in enumerate(violations):
        status = str(violation.get("status") or "")
        label = (
            f"{violation.get('constraint_family_label') or violation.get('constraint_family')}"
            f" · {violation.get('constraint_id')} [{status}]"
        )
        if status in ("violated", "not_comparable"):
            with st.expander(label, expanded=True):
                _violation_view(violation, index, position)
        else:
            with st.expander(label, expanded=False):
                _violation_view(violation, index, position)

    _shared.sechead("② 约束族覆盖情况（哪些规则真的跑了）")
    available = list(payload.get("constraints_not_available") or ())
    if available:
        # 逐字段原样呈现，不给它另起一套列名 —— 缺输入的原因就是引擎的原话。
        st.dataframe(
            [
                {str(key): (value if isinstance(value, (str, int, float, bool, type(None))) else str(value))
                 for key, value in item.items()}
                for item in available
                if isinstance(item, dict)
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("所有已注册约束本次都具备执行条件。")
    st.caption(
        f"复核队列 {len(payload.get('review_queue') or ())} 项 · "
        f"校验器版本 `{payload.get('validator_version')}` · "
        # 引擎自身没带 status 字段时退回信封状态，别把 None 印在页面上。
        f"引擎状态 `{payload.get('acme_status') or payload.get('status')}`"
    )

    cross_modal = list(payload.get("cross_modal") or ())
    if cross_modal:
        with st.expander(f"跨模态核对（{len(cross_modal)} 项）", expanded=False):
            st.dataframe(cross_modal, width="stretch", hide_index=True)

    _shared.render_jump_panel()

    _shared.honesty(
        [
            "本页数据来自**合成语料**（`annual_07_acme_constraint_break.pdf` 等），不是真实年报。",
            "未检查的约束族（如跨期、跨源）需要上一期文档或行情快照，本演示未提供 —— "
            "缺失在页面上显式列出，不按「通过」计。",
            "violated / not_comparable 的结果必须进人工复核队列；系统报警不等于系统判错。",
        ]
    )
