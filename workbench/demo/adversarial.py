"""对抗注入页（CARD-12 七场景 + CARD-25 + CARD-26 FinFuzz）—— DEMO_FLOW 失败注入 F1–F8。

剧本 §3 的话术：「各位评委可以任选一个场景，或者现场给我一个更刁难的输入——
**如果系统不知道自己错了还硬给结论，那才是失败。**」

所以这一页的判定标准不是"系统答对了"，而是"系统在必须拒答时拒答了"。每个场景
的期望行为都印在页面上，结果与期望逐条对照 —— 一个 **ok=False** 的场景会被画成
红条，而不是从列表里消失。

下半页是 FinFuzz 的对抗读数：按错误类型分层报检出率，并区分**抽取器失败**与
**核查器失败**。一个笼统的总召回率可以掩盖某一类错误完全查不出来。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def _render_scenarios() -> None:
    catalog = demo_service.injections_catalog()
    if _shared.unavailable(catalog, what="失败注入场景"):
        return
    items = list(catalog.get("items") or ())
    st.caption(
        f"共 {len(items)} 个注册场景。**每个场景都注入真实故障并断言系统自己的反应** —— "
        "不是断言注入本身生效了。跑不起来的场景会带原因返回，不会被静默从列表里拿掉。"
    )
    for spec in items:
        scenario_id = spec.get("scenario_id")
        title = f"F{scenario_id} · {spec.get('title')}"
        with st.expander(title, expanded=False):
            st.markdown(f"- **注入操作**：{spec.get('injection')}")
            st.markdown(f"- **预期行为**：{spec.get('expected_behavior')}")
            if spec.get("deferred_to"):
                st.caption(f"本场景依赖 {spec['deferred_to']} 落地。")
            if st.button(f"运行 F{scenario_id}", key=f"demo_inject_{scenario_id}"):
                st.session_state[f"demo_inject_result_{scenario_id}"] = demo_service.run_injection(
                    int(scenario_id)
                )
            result = st.session_state.get(f"demo_inject_result_{scenario_id}")
            if not result:
                continue
            if _shared.unavailable(result, what=f"场景 F{scenario_id}"):
                continue
            observed = result.get("observed")
            if result.get("ok"):
                st.success(f"系统反应符合预期：{observed}")
            else:
                st.error(
                    f"**系统未按预期拒答/告警** —— 观察到的行为：{observed}。"
                    "这是缺陷，不是展示省略。"
                )
            evidence = result.get("evidence") or {}
            if evidence:
                with st.expander("场景取证细节", expanded=False):
                    st.json(evidence, expanded=False)


def _render_adversarial() -> None:
    payload = demo_service.adversarial_view()
    if _shared.unavailable(payload, what="FinFuzz 对抗评测"):
        return
    overall: Dict[str, Any] = payload.get("overall") or {}
    _shared.stat_row(
        [
            ("Precision", overall.get("precision")),
            ("Recall", overall.get("recall")),
            ("F1", overall.get("f1")),
            ("误报率 FPR", overall.get("false_positive_rate")),
            ("变异总量", overall.get("n"), f"真变异 {overall.get('planted_n')} / 等值 {overall.get('equivalent_n')}"),
            ("TP", overall.get("true_positive")),
            ("FP", overall.get("false_positive")),
            ("FN", overall.get("false_negative")),
            ("TN", overall.get("true_negative"), "伪变异（等值改写）被正确忽略的条数"),
        ]
    )
    st.caption(
        "两通道分工：`claim` 通道直改字段、绕开提取器，漏检只能归因核查器；"
        "`text` 通道改句重抽，暴露提取器失败。两条通道混在一起统计，"
        "「抽取器没抽出来」就会被误记成「核查器查不出来」。"
    )
    st.caption(
        f"语料案例 {len(payload.get('cases') or ())} 个 · 被剔除的 claim "
        f"{payload.get('excluded_claims')} 条（剔除理由逐条记录）· "
        f"跳过 {payload.get('skipped_count')} 条。"
    )

    st.markdown("**逐错误类型召回（N/A 行保留不删）**")
    rows: List[Dict[str, Any]] = []
    for name, row in (payload.get("per_error_type") or {}).items():
        if not isinstance(row, dict):
            continue
        recall = row.get("recall")
        # 空分母时 CARD-26 的 ``prf1`` 返回字符串 "N/A"（不是 None，也不折叠成 0.0）。
        # 直接把它塞进数值列会让整个表格渲染失败 —— 表格消失比显示 N/A 更糟。
        shown = "N/A" if not isinstance(recall, (int, float)) else f"{recall:.4f}"
        rows.append(
            {
                "错误类型": name,
                "样本数": "N/A" if not row.get("n") else str(row.get("n")),
                "检出": str(row.get("detected")),
                "召回": shown,
                "等值样本": str(row.get("equivalent_n") or 0),
                "状态": "有样本" if row.get("n") else "**N/A（无合格声明）**",
            }
        )
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
        na = [row for row in rows if "N/A（无合格声明）" in row["状态"]]
        if na:
            st.warning(
                "以下错误类型**当前语料上没有样本**：" + "、".join(row["错误类型"] for row in na)
                + "。行保留不删 —— 删掉空行等于假装这类错误不存在。"
            )

    attribution = payload.get("extractor_vs_checker") or {}
    if attribution:
        st.markdown("**漏检归因：抽取器 vs 核查器**")
        st.json(attribution, expanded=False)

    st.markdown("**被剔除的语料（哪些 claim 没资格进入评测）**")
    st.caption(
        "若某个 claim 在**未变异**时就已经会引发 finding，那么它变异后被抓到就证明不了什么 —— "
        "把它计入召回会用既有的争议抬高分数。所以它们被剔除，且逐条记录理由。"
    )


def render() -> None:
    _shared.page_head("案例演示 · 离线演示材料", "对抗注入", "Failure Injection & FinFuzz")
    st.caption(
        "DEMO_FLOW 失败注入 F1–F8 与 §4 创新：**FinFuzz 用金融语义变异持续对抗评测**。"
        "本页的判定标准是「系统在必须拒答时拒答」，不是「系统答对了」。"
    )

    tab_inject, tab_fuzzer = st.tabs(["失败注入（F1–F7）", "FinFuzz 对抗评测"])
    with tab_inject:
        _render_scenarios()
    with tab_fuzzer:
        _render_adversarial()

    _shared.honesty(
        [
            "**所有数字均来自合成语料，不是真实研报上的检测率。** "
            "holdout 集未采集，报告里恒为 `TARGET / TO BE MEASURED` —— 携带数字会被校验器直接判错。",
            "失败注入场景 F5（来源冲突）已由 CARD-13 实现；F8（FinFuzz 量级错误）见右侧标签页；"
            "F9（时间重验证）在「时间重验证」页。",
            "FinFuzz 的 `ytd_vs_single` / `valuation` 两类在现有语料上没有合格声明，"
            "记 N/A 而非 0 —— 没有样本不等于查不出来。",
        ]
    )
