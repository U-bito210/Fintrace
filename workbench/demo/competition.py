"""竞赛主线页：把已有的可运行能力收束为一次可讲清的投研核查流程。

这不是第二套 checker，也不制造"智能体已经调用模型"的假象。页面只消费
``demo_service.corrections`` 的真实结果：材料、规则执行、finding、证据和 run
目录都仍由既有服务层负责。它的职责只是让现场展示不必在八个诊断页之间跳转。
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from src.cn import demo_service

from . import _shared


def _finding_rows(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为概览保留可复核字段，绝不把 finding 再次判定或改写。"""
    return [
        {
            "类型": item.get("check_label") or item.get("check_type"),
            "严重度": item.get("severity"),
            "草稿值": item.get("original"),
            "披露/应然值": item.get("expected"),
            "期间": item.get("period") or "—",
            "证据数": len(item.get("evidence_ids") or ()),
        }
        for item in findings
    ]


def render() -> None:
    _shared.page_head("北京市大学生金融人工智能竞赛", "投研智能核查主线", "FinTrace-CN")
    st.caption(
        "一条从受控材料到可复核结论的展示主线：材料登记 → 主张结构化 → "
        "确定性核查 → 证据回跳 → 审计导出。数值判断不交给模型猜。"
    )
    doc_id = _shared.draft_picker(key="demo_competition_draft", label="本次演示草稿")
    payload = demo_service.corrections(doc_id)
    if _shared.unavailable(payload, what="竞赛主线案例"):
        return

    findings: List[Dict[str, Any]] = list(payload.get("findings") or ())
    summary: Dict[str, Any] = dict(payload.get("summary") or {})
    coverage: Dict[str, Any] = dict(payload.get("coverage") or {})
    aligned: Dict[str, Any] = dict(payload.get("aligned") or {})
    planted_rows: List[Dict[str, Any]] = list(aligned.get("rows") or ())
    detected = sum(1 for row in planted_rows if row.get("检出"))
    runs = demo_service.runs_catalog()
    run_count = len(runs.get("items") or ()) if runs.get("status") == demo_service.STATUS_OK else 0

    _shared.stat_row(
        [
            ("进入核查的 claim", summary.get("claims_checked"), "实际进入规则判定的结构化主张数"),
            ("核查发现", payload.get("finding_count"), "引擎实际输出，非展示预置"),
            ("植入项对齐", f"{detected}/{len(planted_rows)}" if planted_rows else "不适用", "只对带植入清单的案例统计"),
            ("可回跳证据", sum(len(item.get("evidence_ids") or ()) for item in findings), "finding 引用的证据 ID 数"),
            ("已录制审计 run", run_count, "run 目录内真实存在的记录数"),
        ]
    )
    materials, execution, findings_tab, evidence, audit = st.tabs(
        ["① 案例材料", "② 智能核查", "③ 核查结果", "④ 证据与复算", "⑤ 审计导出"]
    )

    with materials:
        _shared.sechead("受控输入与适用边界")
        st.dataframe(
            [
                {
                    "草稿": payload.get("filename"),
                    "材料来源": payload.get("source"),
                    "参考披露": "、".join(payload.get("reference_documents") or ()) or "—",
                    "截至日": payload.get("as_of") or "未设（时效规则不启用）",
                    "构建耗时": f"{float(payload.get('build_seconds') or 0):.1f}s",
                }
            ],
            width="stretch",
            hide_index=True,
        )
        if payload.get("source") == "data/demo":
            st.success("当前是本地受控真实披露材料；草稿为人为注入错误的测试文本。")
        else:
            st.warning("当前是合成离线案例，只用于回归和现场演练，不代表真实研报表现。")
        st.caption("材料来源、截至日和文件访问范围都来自服务层返回值；页面不补填缺失信息。")

    with execution:
        _shared.sechead("可执行链路与职责分离")
        rules = dict(coverage.get("rules") or {})
        ran = sum(1 for detail in rules.values() if detail.get("ran"))
        st.dataframe(
            [
                {"步骤": "材料登记与解析", "当前状态": "已执行", "输出": "文档、页码、事实与来源定位"},
                {
                    "步骤": "主张结构化",
                    "当前状态": f"已进入 {summary.get('claims_checked') or 0} 条 claim",
                    "输出": f"提取器：{coverage.get('extractor') or '—'}",
                },
                {"步骤": "数值/口径/引用规则核查", "当前状态": f"已执行 {ran}/{len(rules)} 条规则", "输出": "finding + 原值 + 应然值 + 修改建议"},
                {"步骤": "证据定位与复算", "当前状态": "按 finding 输出", "输出": "evidence ID、页码和计算依据"},
                {"步骤": "审计记录与证据包", "当前状态": f"已有 {run_count} 个已录制 run", "输出": "manifest、事件流和可下载证据包"},
            ],
            width="stretch",
            hide_index=True,
        )
        st.info(
            "现场模型接入时，LLM 只负责任务拆解与自然语言到 claim 的结构化；"
            "数值计算、期间/单位校验、判错和证据匹配仍由确定性工具完成。"
        )
        st.caption("当前离线案例不伪装模型调用：本页展示的是已实际执行的解析与规则核查链路。")

    with findings_tab:
        _shared.sechead("核查结论：错误必须逐项可见")
        if findings:
            st.dataframe(_finding_rows(findings), width="stretch", hide_index=True)
        else:
            st.success("当前案例没有发现问题；这表示本次规则输出为空，不等于未覆盖的内容已被验证。")
        unexecuted = [name for name, detail in (coverage.get("rules") or {}).items() if not detail.get("ran")]
        if unexecuted:
            st.warning("未执行规则：" + "、".join(unexecuted) + "。这些范围不能表述为“已核查通过”。")

    with evidence:
        _shared.sechead("证据回跳与复算入口")
        evidence_index = payload.get("evidence_index") or {}
        displayed = 0
        for finding_idx, finding in enumerate(findings, start=1):
            evidence_ids = list(finding.get("evidence_ids") or ())
            if not evidence_ids:
                continue
            displayed += 1
            st.markdown(
                f"**{finding_idx:02d}. {finding.get('check_label') or finding.get('check_type')}** · "
                f"草稿 `{finding.get('original')}` → 应然 `{finding.get('expected')}`"
            )
            columns = st.columns(min(len(evidence_ids), 3))
            for offset, evidence_id in enumerate(evidence_ids):
                owner = (evidence_index.get(evidence_id) or {}).get("document_id") or payload.get("draft_document_id", "")
                with columns[offset % len(columns)]:
                    _shared.jump_button(
                        str(owner), str(evidence_id), label=f"查看原文 · {str(evidence_id)[-10:]}",
                        why="在原始材料中定位支撑该结论的证据", index=evidence_index,
                        token=f"competition:{finding_idx}:{offset}",
                    )
        if not displayed:
            st.caption("当前 finding 没有可定位的数值证据；不猜测页码。")
        _shared.render_jump_panel()

    with audit:
        _shared.sechead("可复现交付")
        if runs.get("status") != demo_service.STATUS_OK:
            st.info("审计 run 目录暂不可用：" + str(runs.get("reason") or "未提供原因"))
        elif not runs.get("items"):
            st.warning("尚未录制审计 run。请到「审计回放」页执行真实管线并导出 Evidence Pack。")
        else:
            latest = list(runs.get("items") or ())[-1]
            st.dataframe(
                [{
                    "最近 run": latest.get("run_id"), "开始": latest.get("started_at"),
                    "执行模式": latest.get("execution_mode"), "模型": latest.get("model") or "未调用模型",
                    "解析器": latest.get("parser"), "git commit": latest.get("git_commit"),
                }],
                width="stretch", hide_index=True,
            )
            st.caption("Evidence Pack 的生成和下载入口保留在「审计回放」页，避免本页把未发生的执行伪造成已记录。")

    _shared.honesty(
        [
            "本页是竞赛展示编排层，不重新计算任何金融数据，也不替代原始服务层的证据与校验。",
            "合成案例的检出结果只能证明固定离线案例的回归表现；真实材料案例须以其材料范围、截至日和规则覆盖为准。",
            "LLM 接入不改变事实、计算和判定的权威来源；模型生成的叙述必须继续带 evidence ID 并通过确定性校验。",
        ]
    )
