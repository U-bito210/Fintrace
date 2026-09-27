"""审计回放 + 证据包导出页（CARD-12）—— DEMO_FLOW 收尾。

剧本收尾话术："整个 run 的 manifest 记录了模型、prompt hash、parser 版本、
git commit、snapshot——同一输入同一配置可重放。最后导出 Evidence Pack：报告、
claims、evidence、calculations、events 全量打包，评委可离线复核。"

两个诚实点，都写进页面而不是藏起来：

* **未记录到的阶段如实显示 ``not_recorded``**，不从时间线上省略 —— 一条只显示
  "成功步骤"的时间线看起来更漂亮，但它证明不了任何事。
* **残包不伪装成完整包**。缺件清单（必需 / 完整性 / 可选）直接列在页面上，
  这正是 CARD-12 的可信度所在。缺件还分两种，页面分开说：**口径性缺失**（run 自己
  声明过、理由写在 `report.json` 的 `declared_absences` 里，例如文档级 run 没有估值表）
  与**真缺口**（既不进包、也没声明）。把后者说成前者是粉饰，把前者说成后者是无谓认错。
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
        _render_prompt_registry()
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
            # CARD-14: this cell existed in the manifest before the registry did.
            # It reads the recorded value, and says why it is empty when it is.
            ("Prompt hash", manifest_row.get("prompt_hash") or manifest_row.get("prompt_hash_note")),
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
            ("run 范围", str(pack.get("run_scope") or "未知")),
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
        # 「缺件」分两种，混为一谈就会把口径说成缺陷：run 自己声明过的（口径性缺失，
        # 理由在 report.json 的 declared_absences 里）vs 谁也没解释的（真缺口）。
        declared = dict(pack.get("declared_absences") or {})
        for name, reason in sorted(declared.items()):
            st.info(f"`{name}` 是**口径性缺失**，不是漏写：{reason}")
        unexplained = [
            name for name in (pack.get("missing_completeness") or ()) if name not in declared
        ]
        if unexplained:
            st.caption(
                f"未给出归因的缺件（真缺口）：{unexplained} —— "
                "这些既不进包、也没在 run 产物里声明为结构缺失。"
            )
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

    _render_prompt_registry()

    _shared.honesty(
        [
            "回放是**视图**：它按记录重排事件，不重新执行管线。"
            "同一输入同一配置的确定性重放由 CARD-07 的 `replay()` 负责。",
            "run 目录里的产物由 run 自己写（`report.md` / `report.json` / `claims.json` / "
            "`evidence.json` / `calculations.json` / `validation.json` / `financials.xlsx`），"
            "本页只是把它们收集进包 —— 少一次「读成字符串再写回去」，就少一次让包与 run 目录"
            "说不同话的机会。",
            "本 run 是**文档级**的（输入是一份年报，没有 symbol），所以包里没有 `valuation.xlsx`："
            "估值要 symbol 级行情，该 run 的账本里没有市价证据。这条缺席的理由由 run 自己写进 "
            "`report.json` 的 `declared_absences` 并贴在上面 —— 是口径，不是导出器漏写，"
            "也**不补一个空表**凑满 13 件。要让包完整，得换一条 symbol 级的 run（CARD-08）。",
            "「重放 UI 呈现完整性人工验收 + 截图入执行备注」需要人工走查，本页交付的是可走查的界面。",
            "上述 run 的 `prompt_hash` 是**空**的，而这不是缺件：这个 run 只做解析/归一化/"
            "计算/校验，一个字都没发给模型 —— 空值是对该 run 的真实陈述。prompt 真正被发送的"
            "路径（research agent / 提取器 / 备忘录叙述）在 ③ 里列出，其 hash 由注册表算出。",
        ]
    )


def _render_prompt_registry() -> None:
    """③ Prompt 注册表（CARD-14 §14）。

    刻意放在两个分支都会走到的地方：注册表是否可审计，和"这个 run 目录里有没有东西"
    无关，所以没有 run 时这一节也照样渲染 —— 空目录不该顺手藏掉一段证据。
    """
    st.markdown("#### ③ Prompt 注册表（CARD-14）")
    st.caption(
        "源代码清单点名的 Prompt 模块：文本只有 `src/agents/prompts/templates/` 一处，"
        "注册表按 `name@version` 寻址，`prompt_hash` 进 manifest —— 所以一个 run 能被指回"
        "它实际用过的那几段提示词字节。"
    )
    registry = demo_service.prompt_registry_view()
    if _shared.unavailable(registry, what="Prompt 注册表"):
        return
    recorded_runs = list(registry.get("recorded_runs") or ())
    _shared.stat_row(
        [
            ("已登记 prompt", registry.get("count")),
            ("lock 同步", "是" if registry.get("lock_in_sync") else "**否**"),
            ("本次进程使用记录", len(registry.get("usage") or ())),
            ("manifest 已记录 hash", sum(1 for row in recorded_runs if row.get("prompt_hash"))),
        ]
    )
    prompt_rows: List[Dict[str, Any]] = [
        {
            "name": row.get("name"),
            "version": row.get("version"),
            "variables": "、".join(row.get("variables") or ()) or "（无变量）",
            "prompt_hash": str(row.get("prompt_hash") or "")[:16],
            "model": row.get("model") or "—",
            "source_file": row.get("source_file"),
        }
        for row in registry.get("prompts") or ()
    ]
    st.dataframe(prompt_rows, width="stretch", hide_index=True)

    bundle_rows: List[Dict[str, Any]] = [
        {
            "流程": bundle.get("run_kind"),
            "使用的 prompt": " + ".join(bundle.get("keys") or ()),
            "组合 prompt_hash": str(bundle.get("prompt_hash") or "")[:16],
        }
        for bundle in registry.get("bundles") or ()
    ]
    st.dataframe(bundle_rows, width="stretch", hide_index=True)

    lock = registry.get("lock") or {}
    if not registry.get("lock_in_sync"):
        st.error(
            "注册表与 `registry.lock.json` 不一致（同版本内容被改过或版本被删）："
            f"missing={lock.get('missing')} changed={lock.get('changed')} added={lock.get('added')}。"
            "这会让已发布的结论失去可复现性。"
        )
    usage_rows: List[Dict[str, Any]] = [
        {
            "prompt": row.get("prompt_name"),
            "版本": row.get("prompt_version"),
            "模型": row.get("model") or "—",
            "温度": row.get("temperature"),
        }
        for row in list(registry.get("usage") or ())[-8:]
    ]
    if usage_rows:
        st.caption("本次进程内最近 8 条使用记录（prompt / 版本 / 模型 / 温度）：")
        st.dataframe(usage_rows, width="stretch", hide_index=True)
    else:
        st.caption("本次进程还没有记录到 prompt 使用 —— 上面这些页面走的是确定性管线，没有发送提示词。")
    if recorded_runs:
        st.caption(
            "已录制 run 的 manifest prompt_hash："
            + "；".join(
                f"`{row.get('run_id', '')[:8]}` → {row.get('prompt_hash') or row.get('prompt_hash_note')}"
                for row in recorded_runs[:5]
            )
        )
