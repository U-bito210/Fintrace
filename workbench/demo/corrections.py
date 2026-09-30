"""研报纠错页（CARD-05）—— DEMO_FLOW 步骤 6+7，★高潮一。

剧本原话："LLM 只做一件事：把自然语言句子结构化成 Claim。之后全部由确定性
Checker 判断对错——错误类型、原始值、正确值、证据、建议修改。"

所以本页的**核心不是"找到了几个错"**，而是逐条对齐：草稿里**植入的**每一处，
是不是都被**指名道姓**地抓出来，并给出可回跳的原始页。找不到的必须显形 ——
把检出数说得漂亮而植入项漏掉，恰好是这一页最该避免的事。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def render() -> None:
    _shared.page_head("案例演示 · 离线演示材料", "研报纠错", "Report Corrections")
    st.caption(
        "DEMO_FLOW 步骤 6+7（★高潮一）。提取器只把句子结构化成 Claim，"
        "判定、复算与证据匹配全部走 CARD-05 的确定性规则引擎 —— 零模型。"
    )

    doc_id = _shared.draft_picker(key="demo_corrections_draft")
    heavy = doc_id == demo_service.DEMO_DRAFT_ID and not demo_service.demo_case_built()
    if heavy:
        # 真实材料要解析整份年报。让它「悄悄转圈」比慢本身更糟：侧栏「演示预热」
        # 就是那个动作 —— 页面这里只负责在**没预热就上台**时把代价如实说清楚。
        st.warning(
            f"本次进程里还没构建过这份真实材料：首次要解析 {demo_service.DEMO_ANNUAL_PAGES} 页年报，"
            "本机实测约 40 秒（冷机首次最长约 85 秒），而剧本给步骤 6+7 的窗口只有 50 秒。"
            "**上台前请在左侧「演示预热」点一次**，现场就不必等这一下。"
        )
        # 用可见的进度替代「静默等待」：现场看到有人在干活才不会去点重跑，
        # 而中途打断一次导入会把这个进程的模块状态弄坏（页面会一直重复报同一个错）。
        with st.spinner("正在解析真实年报并跑确定性核查（本机实测约 40 秒）…"):
            payload = demo_service.corrections(doc_id)
    else:
        payload = demo_service.corrections(doc_id)
    if _shared.unavailable(payload, what="研报纠错"):
        return

    findings: List[Dict[str, Any]] = list(payload.get("findings") or ())
    planted: List[Dict[str, Any]] = list(payload.get("planted") or ())
    summary: Dict[str, Any] = payload.get("summary") or {}
    aligned: Dict[str, Any] = payload.get("aligned") or {}
    rows: List[Dict[str, Any]] = list(aligned.get("rows") or ())
    unmatched: List[Dict[str, Any]] = list(aligned.get("unmatched") or ())
    detected = sum(1 for row in rows if row["检出"])
    missed = [row for row in rows if not row["检出"]]

    _shared.stat_row(
        [
            ("植入错误", payload.get("planted_count"), "语料规格里声明的应检出项"),
            ("实际检出", payload.get("finding_count"), "Checker 给出的 finding 条数"),
            ("严重度 error", (summary.get("by_severity") or {}).get("error", 0), None),
            ("已核 claim", summary.get("claims_checked"), "进入判定流程的 claim 数"),
            ("干净", "是" if summary.get("clean") else "否", "clean=True 表示无 error 级问题"),
        ]
    )
    _shared.stat_row(
        [
            ("植入项已检出", f"{detected}/{len(rows)}" if rows else "—", "按 check_type + 值片段逐条对齐"),
            ("漏检", len(missed), "植入清单里一条都没命中的项"),
            ("清单外检出", len(unmatched), "不在植入清单里的 finding：须逐条判定真伪"),
            ("构建耗时", f"{float(payload.get('build_seconds') or 0):.1f}s", "本进程内首次解析，之后走缓存"),
        ]
    )
    st.caption(
        f"草稿 `{payload.get('filename')}` · as_of {payload.get('as_of') or '（未设，stale 规则不启用）'}"
        f" · 提取器 `{(payload.get('coverage') or {}).get('extractor') or '—'}`"
    )

    # --- 逐条对齐：植入 vs 检出 ---------------------------------------------
    _shared.sechead("① 植入清单 ↔ 实际检出（逐条对齐）")
    if planted:
        st.dataframe(rows, width="stretch", hide_index=True)
        if missed:
            st.error(
                "有植入项未被检出："
                + "、".join(f"{row['植入类型']}（{row['植入说明']}）" for row in missed)
                + "。这就是漏检，不是「展示省略」。"
            )
        else:
            st.success(f"植入的 {len(planted)} 类错误全部被检出，无遗漏。")
        st.caption(
            "对齐口径写在最后一列：清单给了值片段的按「类型 + 值」对齐（同一类型可能命中多条，"
            "只按类型对齐会把命中数摊到每一处植入上，那是虚报）；没给的沿用按类型对齐。"
        )
    else:
        st.info("这份草稿是干净语料（没有植入错误）。检出应为 0 —— 那才是正确结果，不是「没做事」。")

    # --- 清单之外的检出 -----------------------------------------------------
    _shared.sechead("② 植入清单之外的检出（须逐条判定）")
    if not unmatched:
        st.caption("没有清单之外的检出。")
    else:
        st.caption(
            "下面每一条都**不在**植入清单里。它要么是草稿里还有的别的问题（真阳性），"
            "要么是引擎误报 —— 两种都只能由人判定，所以这里不做筛选、不做汇总。"
        )
        st.dataframe(
            [
                {
                    "类型": item.get("check_type"),
                    "严重度": item.get("severity"),
                    "原句": str(item.get("sentence") or "")[:80],
                    "草稿写的值": item.get("original"),
                    "应然值": item.get("expected"),
                }
                for item in unmatched
            ],
            width="stretch",
            hide_index=True,
        )

    # --- 逐条纠错详情 -------------------------------------------------------
    _shared.sechead("③ 纠错明细（错误类型 / 原值 / 正确值 / 建议 / 证据）")
    if not findings:
        st.success("本次未发现任何问题。")
    for finding_idx, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "")
        label = finding.get("check_label") or finding.get("check_type") or "finding"
        tone = _shared.tone(severity)
        head = f"{finding_idx:02d}. [{label}] {finding.get('check_type')} · {severity}"
        box = {"error": st.error, "warning": st.warning, "success": st.success}.get(tone, st.info)
        box(head)
        left, right = st.columns(2)
        with left:
            st.markdown("**原文（逐字）**")
            st.code(str(finding.get("sentence") or ""), language=None)
            st.markdown(f"**草稿写的值**：`{finding.get('original')}`")
        with right:
            st.markdown(f"**披露/应然值**：`{finding.get('expected')}`")
            st.markdown(f"**指标/期间**：`{finding.get('metric') or '—'}` / `{finding.get('period') or '—'}`")
            st.markdown(f"**建议**：{finding.get('suggestion') or '（无）'}")
            st.caption(
                f"claim `{finding.get('claim_id')}` · 置信 {finding.get('confidence')} · "
                f"草稿页 {finding.get('claim_page')}"
            )
        evidence_ids = list(finding.get("evidence_ids") or ())
        if evidence_ids:
            evidence_index = payload.get("evidence_index") or {}
            st.caption("证据：" + "、".join(f"`{item}`" for item in evidence_ids))
            columns = st.columns(min(len(evidence_ids), 4))
            for offset, evidence_id in enumerate(evidence_ids):
                owner = (evidence_index.get(evidence_id) or {}).get("document_id") or payload.get(
                    "draft_document_id", ""
                )
                with columns[offset % len(columns)]:
                    _shared.jump_button(
                        str(owner), evidence_id,
                        label=f"跳到原文 · {evidence_id[-12:]}",
                        why="在原始 PDF 上高亮该证据所在位置",
                        index=evidence_index,
                        # 同一证据可能被多条 finding 引用，按钮 key 必须唯一。
                        token=f"{finding_idx}:{offset}",
                    )
        elif finding.get("claim_page"):
            # 没有可解析数值证据的 finding（如引用类错误）也该能跳回**草稿原文页** ——
            # 剧本 §0 第 3 条要的是"每个重要数字都能点击跳回原始 PDF 页"。
            page = int(finding["claim_page"])
            if st.button(
                f"跳到草稿第 {page} 页",
                key=f"draftpage::{finding.get('claim_id')}",
                help="回到写出该数字的那一页",
            ):
                _shared.request_page_view(
                    str(payload.get("draft_document_id") or ""), page,
                    fact_id=str(finding.get("claim_id") or ""),
                    raw_text=str(finding.get("sentence") or ""),
                    filename=str(payload.get("filename") or ""),
                    locator="草稿页（该 claim 所在页）",
                )

    # --- 覆盖范围：哪些规则真的跑了 -----------------------------------------
    _shared.sechead("④ 检测覆盖范围（哪些规则真的执行了）")
    coverage = payload.get("coverage") or {}
    rules = coverage.get("rules") or {}
    rule_rows = [
        {
            "规则": name,
            "已执行": "是" if info.get("ran") else "**否**",
            "未执行原因": info.get("reason") or "",
        }
        for name, info in sorted(rules.items())
    ]
    st.dataframe(rule_rows, width="stretch", hide_index=True)
    st.caption(
        "「没报错」和「这条规则没跑」是两件事。上面每一行 not ran 都写明了原因 —— "
        "一条从未执行的规则给出的「干净」不是证据。"
    )
    st.caption(
        f"事实库 {coverage.get('fact_count')} 条 · 参考披露 "
        f"{len(coverage.get('reference_documents') or ())} 份 · "
        f"as_of {coverage.get('as_of') or '（未设）'}"
    )

    # --- 抽取器的存疑部分 ----------------------------------------------------
    uncertain = payload.get("uncertain") or {}
    if uncertain.get("unextracted") or uncertain.get("no_claim"):
        with st.expander("抽取器没把它变成 claim 的句子（如实列出，不算干净）", expanded=False):
            st.markdown(
                f"- 未抽取出 claim 的句子：**{uncertain.get('unextracted', 0)}** 句\n"
                f"- 判定为无需 claim（如纯观点）：**{uncertain.get('no_claim', 0)}** 句"
            )
            st.caption("这些句子既没被证实也没被证伪，只是没进入判定 —— 边界在这里。")
            for item in (payload.get("uncertain_detail") or ())[:20]:
                st.code(str(item.get("sentence") or item), language=None)

    _shared.render_jump_panel()

    is_demo = payload.get("source") == "data/demo"
    gate_ok = bool(rows) and not missed
    lines = []
    if is_demo:
        references = "、".join(payload.get("reference_documents") or ())
        pages = payload.get("reference_pages")
        lines.append(
            f"参考披露是**真实年报全文** `{references}`"
            + (f"（{pages} 页，巨潮资讯）" if pages else "")
            + f"，草稿 `{payload.get('filename')}` 是人为注入错误的文本件；"
            "下面的检出率只代表这份材料。"
        )
    else:
        lines.append(
            f"草稿语料为**合成件**（`{payload.get('filename')}`），不是真实研报 —— "
            "检出率不得表述为真实研报上的表现。"
        )
    if rows:
        lines.append(
            f"§5 硬项「8/8 检出方可上台」：本次植入 {len(rows)} 处、**检出 {detected} 处**"
            + ("，达标。" if gate_ok else f"、漏检 {len(missed)} 处 —— **未达标**，话术必须照实说。")
        )
    if unmatched:
        lines.append(
            f"清单外另有 {len(unmatched)} 条检出：既不能算检出率，也不能当噪音丢掉 —— "
            "判定结果见 ②，逐条给出。"
        )
    lines.append(
        "stale_citation 规则只在设置了 as_of 时启用；未设时该规则在覆盖范围表里如实标为未执行。"
    )
    _shared.honesty(lines)
