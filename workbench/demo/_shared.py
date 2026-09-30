"""Demo 页面共用呈现助手。

三条约定，都对应卡里的硬项：

* **状态必须显眼且如实。** ``blocked / violated / fail`` 走红色，``warning /
  degraded / stale`` 走黄色，``skipped / not_checked / N/A`` 走中性灰。卡 §7
  的原话是"不美化系统的不确定性"——那本身就是演示卖点，所以状态用色不软化。
* **拿不到就说拿不到。** :func:`unavailable` 把服务层的 ``unavailable`` 信封
  渲染成一句人话加原因，**不抛异常**：卡 §9「页面崩溃不影响后端任务」，
  §15「浏览器控制台零报错」。
* **数字可回跳。** :func:`jump_button` + :func:`render_jump_panel` 是剧本 §0
  第 3 条「每个重要数字都能点击跳回原始 PDF 页」的实现，定位与渲染全部交回
  CARD-01，页面不自己算坐标。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

import streamlit as st

from src.cn import demo_service

#: 状态 → 呈现语气。``error`` 只留给"系统确实发现了问题"，
#: 而不是"我们没做"——后者走 ``info``，因为缺输入不是缺陷。
_TONE_ERROR = ("blocked", "violated", "fail", "failed", "invalid", "conflicted", "error")
_TONE_WARN = ("warning", "warn", "degraded", "stale", "partial", "pending", "weakened", "opened")
_TONE_OK = ("ok", "pass", "passed", "verified", "supported", "active", "complete", "clean")
_TONE_NEUTRAL = (
    "skipped", "skip", "not_checked", "not_recorded", "unavailable",
    "n/a", "not_comparable", "missing",
)

_JUMP_KEY = "demo_jump_target"


def page_head(eyebrow: str, title: str, tag: str = "") -> None:
    """D 稿页头节奏（眉题 → 衬线标题 → 右对齐 tag）。

    与 ``workbench.page_head`` 同一套标记和全局 CSS（``.eyebrow`` /
    ``.pagetitle``），但在这里自包含 —— demo 模块反向 import workbench 会造成
    循环加载。三个参数都是页面写死的字面量，与 workbench 版本同契约。
    """
    tag_html = f'<span class="tag">{tag}</span>' if tag else ""
    st.markdown(
        f'<div class="eyebrow">{eyebrow}</div>'
        f'<div class="pagetitle"><h2>{title}</h2>{tag_html}</div>',
        unsafe_allow_html=True,
    )


def sechead(title: str, tag: str = "") -> None:
    """D 稿区块标题（细墨底线 + 右侧小 tag），替代 demo 页的裸 ``####``。"""
    tag_html = f'<span class="tag">{tag}</span>' if tag else ""
    st.markdown(
        f'<div class="sechead"><h4>{title}</h4>{tag_html}</div>',
        unsafe_allow_html=True,
    )


def tone(status: object) -> str:
    """把任意引擎状态映射到一种呈现语气。"""
    text = str(status or "").strip().lower()
    if not text:
        return "info"
    if text in _TONE_ERROR:
        return "error"
    if text in _TONE_WARN:
        return "warning"
    if text in _TONE_OK:
        return "success"
    if text in _TONE_NEUTRAL:
        return "info"
    return "info"


def badge(status: object) -> str:
    """一行状态标记；颜色由 :func:`tone` 决定。"""
    return f"{status}"


def unavailable(payload: Dict[str, Any], *, what: str = "该页") -> bool:
    """渲染 ``unavailable`` 信封。返回 True 表示已接管，调用方应停止渲染。

    这不是错误分支 —— "语料里没有这份东西"是完全正常的起点状态，所以用
    ``info`` 而不是 ``error``，并**逐字**给出服务层的 reason。
    """
    if payload.get("status") != demo_service.STATUS_UNAVAILABLE:
        return False
    st.info(f"{what}暂不可用。")
    reason = payload.get("reason")
    if reason:
        st.caption(f"原因：{reason}")
    return True


def case_picker(*, key: str, default: Optional[str] = None, label: str = "演示案例") -> str:
    """语料下拉。缺文件的案例仍然列出，但标注出来 —— 不假装有。"""
    cases = demo_service.filing_cases()
    if not cases:
        return default or demo_service.DEFAULT_FILING_KEY
    options = [item["key"] for item in cases]
    labels = {
        item["key"]: f"{item['label']}（{item['filename']}）" + ("" if item["present"] else " · 缺失")
        for item in cases
    }
    index = options.index(default) if default in options else 0
    chosen = st.selectbox(label, options, index=index, format_func=lambda value: labels[value], key=key)
    chosen_case = next(item for item in cases if item["key"] == chosen)
    st.caption(f"用途：{chosen_case['purpose']}")
    return chosen


def draft_picker(*, key: str, default: Optional[str] = None, label: str = "研报草稿") -> str:
    drafts = demo_service.draft_cases()
    if not drafts:
        return default or ""
    options = [item["key"] for item in drafts]
    labels = {
        item["key"]: (
            f"{item['filename']} · "
            + (f"植入 {item['expected_findings']} 处" if item["planted"] else "干净（无植入）")
            + (" · 真实材料（首次构建较慢）" if item.get("source") == "data/demo" else "")
            + ("" if item.get("runnable", True) else " · 未接通")
        )
        for item in drafts
    }
    if default is None:
        # 默认值只认服务层那一个函数：页面演的和剧本自检的不是同一份草稿，
        # 是这类演示最容易出的岔子。
        default = demo_service.default_draft_key()
    index = options.index(default) if default in options else 0
    chosen = st.selectbox(label, options, index=index, format_func=lambda value: labels[value], key=key)
    return chosen


def jump_button(
    target: str,
    evidence_id: str,
    *,
    label: str = "跳到原文",
    why: str = "",
    index: Optional[Dict[str, Any]] = None,
    token: str = "",
) -> None:
    """记下一条待跳转的证据。``target`` 是 ``document_id`` 或演示案例 key。

    ``index`` 传当前页已经拿到的那份 evidence index —— 规范化事实 id 只在那儿
    查得到页码。``token`` 用来让同一证据在页面上出现多次时按钮 key 仍然唯一
    （同一个 evidence_id 可能出现在多条 finding 里，Streamlit 的 key 必须唯一）。

    真正的渲染发生在 :func:`render_jump_panel`：一个按钮换一次页面重绘，
    不值得在同一屏里内联渲染 PDF。
    """
    if st.button(label, key=f"jump::{target}::{evidence_id}::{token}", help=why or None):
        st.session_state[_JUMP_KEY] = {
            "target": target,
            "fact_id": evidence_id,
            "located": demo_service.jump(target, evidence_id, index=index),
        }


def request_page_view(
    target: str,
    page: int,
    *,
    fact_id: str,
    raw_text: str = "",
    filename: str = "",
    locator: str = "",
    bbox: Optional[Sequence[float]] = None,
) -> None:
    """直接登记一个"看这一页"的请求（没有证据 id 可解析时用）。

    引用类 finding 本来就没有可解析数值证据，但它照样得能跳回**草稿原文页** ——
    剧本 §0 第 3 条说的是"每个重要数字都能点击跳回原始 PDF 页"，草稿页也是原始页。
    """
    st.session_state[_JUMP_KEY] = {
        "target": target,
        "fact_id": fact_id,
        "located": {
            "status": demo_service.STATUS_OK,
            "page": page,
            "bbox": list(bbox) if bbox else None,
            "raw_text": raw_text,
            "filename": filename,
            "document_id": target,
            "locator": locator,
            "highlight": bool(bbox),
        },
    }


def render_jump_panel() -> None:
    """渲染待跳转的证据：页码 + 坐标 + 逐字原文 + 高亮后的页面图像。"""
    target = st.session_state.get(_JUMP_KEY)
    if not target:
        return
    located = target.get("located") or {}
    with st.container(border=True):
        st.markdown(f"**原文定位** · `{target.get('fact_id')}`")
        if located.get("status") != demo_service.STATUS_OK:
            st.warning(f"这条证据定位不到：{located.get('reason') or '未提供原因'}")
            st.caption("定位不到就说定位不到 —— 不猜一个页码。")
            return
        page = located.get("page")
        st.caption(
            f"文件 `{located.get('filename')}` · 第 **{page}** 页 · "
            f"坐标 `{located.get('bbox')}` · 定位 `{located.get('locator')}`"
        )
        raw = located.get("raw_text")
        if raw:
            st.code(str(raw), language=None)
        if page:
            highlights: Sequence[Sequence[float]] = (
                [located["bbox"]] if located.get("bbox") else ()
            )
            image, reason = demo_service.render_page(
                str(located.get("document_id") or target.get("target")), int(page), highlights
            )
            if image:
                st.image(image, caption=f"{located.get('filename')} 第 {page} 页（红框为证据位置）")
            else:
                st.caption(f"页面预览不可用：{reason}")


def case_brief(payload: Dict[str, Any]) -> None:
    """页面顶部的一行"这份数据是什么"。"""
    case = payload.get("case") or {}
    if not case:
        return
    parts = [
        f"**{case.get('label', '')}**",
        f"`{case.get('filename', '')}`",
        f"公司 {case.get('company') or '—'}",
        f"期间 {case.get('report_period') or '—'}",
        f"document_id `{case.get('document_id', '')}`",
    ]
    st.caption(" · ".join(parts))
    sha = case.get("sha256")
    if sha:
        st.caption(f"sha256 `{sha[:16]}…`（CARD-01 登记，重放时可复核）")


def stat_row(items: Iterable[Sequence[Any]]) -> None:
    """一行 ``(标签, 值, 说明)`` 指标。"""
    rows = [item for item in items if item]
    if not rows:
        return
    columns = st.columns(len(rows))
    for column, item in zip(columns, rows):
        label, value = item[0], item[1]
        note = item[2] if len(item) > 2 else None
        column.metric(str(label), "—" if value is None else str(value), help=note)


def counts_table(rows: List[Dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        st.caption("（空）")
        return
    st.dataframe(
        [{key: row.get(key) for key in columns} for row in rows],
        width="stretch",
        hide_index=True,
    )


def honesty(lines: Iterable[str]) -> None:
    """页面底部的"这份展示里有什么是没做到的"。卡 §7：不美化不确定性。"""
    items = [line for line in lines if line]
    if not items:
        return
    with st.expander("本页的边界（如实声明）", expanded=False):
        for line in items:
            st.markdown(f"- {line}")


__all__ = [
    "badge",
    "case_brief",
    "case_picker",
    "counts_table",
    "draft_picker",
    "honesty",
    "jump_button",
    "page_head",
    "render_jump_panel",
    "sechead",
    "stat_row",
    "tone",
    "unavailable",
]
