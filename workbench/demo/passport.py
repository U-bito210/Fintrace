"""结论护照页（CARD-23）—— DEMO_FLOW 步骤 7b，★核心动作。

剧本话术："普通 RAG 只能告诉你'这句话来自第几页'——无法回答数字对不对、依赖
还有效吗。FinTrace 把关键结论做成**金融AI结论护照**：完整证明链（Source /
Accounting Context / Calculation / Assumption / Dependency / Validation /
Integrity）。现在点击 VERIFY CLAIM——**系统脱离 LLM，重新执行整套证明**。"

所以这一页有两个必须分开的动作：**看护照**（证明链长什么样）与**点验证**
（现场重跑一遍证明）。二者分开是本页的设计要点 —— 如果验证结果只是页面加载时
算好的一份回放，"重新执行"就成了台词而不是事实。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared

#: 护照的证明块 → 中文名。顺序即剧本列出的证明链顺序。
_PROOF_BLOCKS = (
    ("source_proof", "Source · 来源"),
    ("accounting_context", "Accounting Context · 会计口径"),
    ("calculation_proof", "Calculation · 计算"),
    ("assumption_proof", "Assumption · 假设"),
    ("dependency_proof", "Dependency · 依赖"),
    ("validation_proof", "Validation · 校验"),
    ("integrity_proof", "Integrity · 完整性"),
)


def _render_verification(payload: Dict[str, Any]) -> None:
    overall = payload.get("overall_status")
    tone = _shared.tone(overall)
    box = {"error": st.error, "warning": st.warning, "success": st.success}.get(tone, st.info)
    box(f"VERIFY CLAIM 结果：**{overall}**")
    reasons = list(payload.get("status_reasons") or ())
    if reasons:
        for reason in reasons:
            st.markdown(f"- {reason}")
    checks: List[Dict[str, Any]] = list(payload.get("checks") or ())
    if checks:
        st.dataframe(
            [
                {
                    "检查项": item.get("check"),
                    "状态": item.get("status"),
                    "说明": item.get("message"),
                    "影响": item.get("impact"),
                }
                for item in checks
            ],
            width="stretch",
            hide_index=True,
        )
    st.caption(
        f"验证时刻 `{payload.get('verified_at')}` · 引擎 `{payload.get('engine')}`。"
        "状态为 skipped 的检查不等于通过 —— 它表示该项所需输入缺失，已在说明里写明。"
    )


def render() -> None:
    st.subheader("结论护照 · Financial Claim Passport")
    st.caption(
        "DEMO_FLOW 步骤 7b（★核心动作）。结论 → 六个证明块 → 一键 VERIFY CLAIM。"
        "验证路径**零 LLM**：重查文档哈希、重算公式、检查依赖状态。"
    )

    key = _shared.case_picker(key="demo_passport_case")
    probe = demo_service.passport_view(key)
    if _shared.unavailable(probe, what="结论护照"):
        return
    _shared.case_brief(probe)

    selectable = list(probe.get("selectable") or ())
    claim_id = st.selectbox(
        "选择一条结论", selectable,
        index=selectable.index(probe["claim_id"]) if probe.get("claim_id") in selectable else 0,
        key="demo_passport_claim",
    )
    payload = demo_service.passport_view(key, claim_id)
    if _shared.unavailable(payload, what="该结论的护照"):
        return
    claim = payload.get("claim") or {}
    passport = payload.get("passport") or {}

    st.markdown("#### ① 结论本身")
    st.markdown(f"> {claim.get('text')}")
    _shared.stat_row(
        [
            ("claim_id", claim.get("claim_id")),
            ("类型", claim.get("claim_type"), "fact / inference / opinion 三级标注"),
            ("指标", claim.get("metric")),
            ("期间", claim.get("period_key")),
            ("时态", claim.get("temporal_status")),
            ("护照号", passport.get("proof_id")),
        ]
    )

    st.markdown("#### ② 证明链（六个证明块）")
    st.caption(
        f"口径 `{passport.get('scope')}` · 版本 `{passport.get('version_status')}` · "
        f"经济期间 `{passport.get('economic_period')}`"
    )
    for field, label in _PROOF_BLOCKS:
        block = passport.get(field)
        with st.expander(label, expanded=field == "source_proof"):
            if not block:
                st.caption("该证明块为空 —— 护照不为它编造内容。")
                continue
            if isinstance(block, dict):
                st.json(block, expanded=False)
            else:
                st.write(block)

    st.markdown("#### ③ VERIFY CLAIM —— 现场重新执行整套证明")
    st.caption(
        "点击后系统**重新跑一遍**验证：重算文档 sha256、重算公式、重查依赖状态。"
        "这不是页面加载时的结果回放。"
    )
    if st.button("VERIFY CLAIM", type="primary", key="demo_verify_claim"):
        st.session_state["demo_verify_result"] = demo_service.verify_view(key, claim_id)
    result = st.session_state.get("demo_verify_result")
    if result:
        if _shared.unavailable(result, what="VERIFY CLAIM"):
            pass
        else:
            _render_verification(result)
    else:
        st.info("尚未执行。按上面的按钮运行 —— 演示里这一步必须是现场产生的。")

    _shared.render_jump_panel()

    _shared.honesty(
        [
            "**这是合成语料上的演示，不是真实公司的结论。** 数值来自提交在仓库里的示例文档。",
            "演示时若要点出「一个更正公告进入后转 STALE」的对照，需要一份真实更正公告；"
            "现有语料的公告解析不出正文事实，该对照暂无材料（CARD-25 已记录同一缺口）。",
            "五态（VERIFIED / DEGRADED / STALE / BLOCKED / CONFLICTED）由 CARD-23 的验证器"
            "产生，模型不得改写；本页只负责显示。",
        ]
    )
