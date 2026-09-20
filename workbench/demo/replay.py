"""审计回放 + 证据包导出页（CARD-12）—— DEMO_FLOW 收尾。

剧本收尾话术："整个 run 的 manifest 记录了模型、prompt hash、parser 版本、
git commit、snapshot——同一输入同一配置可重放。最后导出 Evidence Pack：报告、
claims、evidence、calculations、events 全量打包，评委可离线复核。"

两个诚实点，都写进页面而不是藏起来：

* **未记录到的阶段如实显示 ``not_recorded``**，不从时间线上省略 —— 一条只显示
  "成功步骤"的时间线看起来更漂亮，但它证明不了任何事。
* **残包不伪装成完整包**。缺件清单（必需 / 完整性 / 可选）直接列在页面上，
  这正是 CARD-12 的可信度所在。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def render() -> None:
    st.subheader("审计回放 · Audit Replay & Evidence Pack")
    st.caption(
        "DEMO_FLOW 收尾。Run Manifest 17 字段 + 只追加事件流 → 十一阶段时间线 → "
        "一键导出证据包。回放是**视图**，不是重新执行。"
    )

    catalog = demo_service.runs_catalog()
    if _shared.unavailable(catalog, what="run 目录"):
        return
    items = list(catalog.get("items") or ())

    left, right = st.columns([1, 3])
    with left:
        if st.button("录制一次真实 run", key="demo_record_run"):
            result = demo_service.record_demo_run()
            st.session_state["demo_record_result"] = result
        recorded = st.session_state.get("demo_record_result")
        if recorded:
            if _shared.unavailable(recorded, what="录制 run"):
                pass
            else:
                st.success(f"已录制 run `{recorded.get('run_id')}`")
    with right:
        st.caption(
            f"存储目录 `{catalog.get('store_dir')}`（.gitignore，不进仓库）。"
            "「录制」跑的是**同一个确定性管线**，事件流是真实发生过的步骤 —— "
            "不是造一条演示数据。"
        )

    if not items:
        st.info("尚无 run 记录。点左侧按钮录制一次真实 run。")
        st.caption(
            "仓库里目前没有任何 run（`runs/` 目录从未生成）—— 所以这一步不是可选的，"
            "没有 run 就没有可回放的时间线。"
        )
        return

    run_ids = [str(item.get("run_id")) for item in items]
    chosen = st.selectbox("选择 run_id", run_ids, index=len(run_ids) - 1, key="demo_replay_run")
    manifest_row = next((item for item in items if str(item.get("run_id")) == chosen), {})
    _shared.stat_row(
        [
            ("开始", manifest_row.get("started_at")),
            ("结束", manifest_row.get("finished_at")),
            ("执行模式", manifest_row.get("execution_mode")),
            ("模型", manifest_row.get("model")),
            ("解析器", manifest_row.get("parser")),
            ("git commit", manifest_row.get("git_commit")),
        ]
    )

    st.markdown("#### ① 十一阶段时间线")
    timeline = demo_service.replay_view(chosen)
    if _shared.unavailable(timeline, what="回放时间线"):
        return
    not_recorded = list(timeline.get("not_recorded_steps") or ())
    failed = list(timeline.get("failed_steps") or ())
    _shared.stat_row(
        [
            ("阶段数", len(timeline.get("step_ids") or ())),
            ("事件数", timeline.get("event_count")),
            ("未记录阶段", len(not_recorded), "、".join(not_recorded) or "无"),
            ("失败阶段", len(failed), "、".join(failed) or "无"),
            ("截断事件", timeline.get("truncated_events")),
        ]
    )
    if not_recorded:
        st.warning(
            "以下阶段**没有记录到事件**（标为 not_recorded，不从时间线省略）："
            + "、".join(not_recorded)
        )
    if failed:
        st.error("以下阶段以失败状态结束：" + "、".join(failed))
    for step in timeline.get("steps") or ():
        status = step.get("status") or step.get("step_status")
        title = step.get("title") or step.get("step_id")
        label = f"[{status}] {title}"
        tone = _shared.tone(status)
        with st.expander(label, expanded=tone in ("error", "warning")):
            if step.get("note") or step.get("honesty_note"):
                st.caption(step.get("note") or step.get("honesty_note"))
            payload = step.get("detail") or step.get("summary")
            if payload:
                st.json(payload, expanded=False)
            events = list(step.get("events") or ())
            if events:
                st.dataframe(events, width="stretch", hide_index=True)
    if timeline.get("honesty_note"):
        st.caption(timeline["honesty_note"])

    st.markdown("#### ② 导出证据包")
    pack = demo_service.export_pack_view(chosen)
    if _shared.unavailable(pack, what="证据包导出"):
        return
    _shared.stat_row(
        [
            ("包完整", "是" if pack.get("complete") else "**否**"),
            ("校验通过", "是" if pack.get("valid") else "**否**"),
            ("已就位", len(pack.get("present") or ())),
            ("缺必需件", len(pack.get("missing_required") or ())),
            ("缺完整性件", len(pack.get("missing_completeness") or ())),
            ("缺可选项", len(pack.get("missing_optional") or ())),
        ]
    )
    missing_rows: List[Dict[str, Any]] = []
    for level, key in (("必需", "missing_required"), ("完整性", "missing_completeness"), ("可选", "missing_optional")):
        for name in pack.get(key) or ():
            missing_rows.append({"级别": level, "缺失件": name})
    if missing_rows:
        st.warning(
            "证据包**不完整**。缺失清单如下 —— 残包不伪装成完整包，这是 CARD-12 的可信度所在。"
        )
        st.dataframe(missing_rows, width="stretch", hide_index=True)
    else:
        st.success("13 类产物齐备。")
    if pack.get("errors"):
        for error in pack.get("errors") or ():
            st.error(error)
    if pack.get("unresolved_references"):
        st.caption(f"包内未解析的引用：{len(pack['unresolved_references'])} 条")
    zip_bytes = pack.get("zip_bytes")
    if zip_bytes:
        st.download_button(
            "下载 Evidence Pack（zip）",
            data=zip_bytes,
            file_name=f"evidence_pack_{chosen}.zip",
            mime="application/zip",
            icon=":material/download:",
        )
    st.caption(f"包目录 `{pack.get('pack_dir')}` · 引擎状态 `{pack.get('pack_status')}`")

    _shared.honesty(
        [
            "回放是**视图**：它按记录重排事件，不重新执行管线。"
            "同一输入同一配置的确定性重放由 CARD-07 的 `replay()` 负责。",
            "本演示 run 只产生解析/归一化/计算/结论/校验五类产物，不含 report.md 与估值表 —— "
            "所以证据包会显示为不完整。这如实反映了这个 run 实际做了什么。",
            "「重放 UI 呈现完整性人工验收 + 截图入执行备注」需要人工走查，本页交付的是可走查的界面。",
        ]
    )
