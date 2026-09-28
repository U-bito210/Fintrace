"""Read-only FinTrace-CN research workbench for local versioned snapshots.

Run: .venv\\Scripts\\streamlit run workbench.py
"""

from __future__ import annotations

import json
import os
import importlib
import re
from html import escape
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from src.cn import agent_service
from src.cn import account_service
from src.cn import document_service
from src.cn import workbench_service as service
from src.cn.demo_data import BLOCKED_DEMO_SNAPSHOT_ID, DEMO_SNAPSHOT_ID
from src.cn.daily_review.report import render_report_html
# Cost figures are only money when their unit of account is known; the registry
# (pure data, no SDK) is the single place that can answer that.
from src.llms.model_registry import cost_currency

# --- CARD-15 挂载：Demo 页面（新增代码全部在 workbench/demo/ 内）-------------
# 既有页面函数一行未动，这里只做挂载。导入失败**不静默**：错误会显示在侧栏，
# 且 tests/test_cn_demo_ui.py 会断言这些标签确实挂上了 —— 悄悄少掉 8 个页面
# 比报错更糟。
try:
    from workbench.demo import mount_demo_pages

    _DEMO_PAGES = mount_demo_pages()
    _DEMO_IMPORT_ERROR = ""
except Exception as _demo_exc:  # noqa: BLE001 - 降级而不是让整个工作台打不开
    _DEMO_PAGES = []
    _DEMO_IMPORT_ERROR = f"{type(_demo_exc).__name__}: {_demo_exc}"

_DEMO_RENDER = {page.label: page.render for page in _DEMO_PAGES}

# Streamlit reruns this script in a fresh execution context.  Do not reload
# these modules manually: they import each other (and the ``src.cn`` package),
# so forcing reloads during a rerun can leave partially initialized modules in
# ``sys.modules`` and trigger circular-import/deadlock errors.
REQUIRED_SERVICE_FUNCTIONS = (
    "research_summary", "research_peer_valuation", "research_readiness",
    "research_evidence", "research_validation", "research_market",
    "research_financials", "research_financial_trends", "research_display_guardrails",
    "research_overview_insights", "research_valuation", "list_evaluations", "evaluation_detail",
    "list_daily_reviews", "daily_review_summary", "daily_review_detail",
    "start_daily_review", "acquire_live_daily_review",
    "refresh_snapshot", "snapshot_freshness", "find_local_baseline_snapshot",
    "peer_sync_preview", "refresh_peer_group",
)
REQUIRED_AGENT_FUNCTIONS = ("start_agent_research", "get_agent_task", "agent_task_events")
REQUIRED_DOCUMENT_FUNCTIONS = (
    "list_documents", "document_detail", "document_facts", "jump_target",
    "document_source", "page_image", "document_analysis",
)


def service_contract_ready() -> bool:
    """Prevent a stale process from exposing a traceback to the user."""
    missing = [name for name in REQUIRED_SERVICE_FUNCTIONS if not callable(getattr(service, name, None))]
    agent_missing = [name for name in REQUIRED_AGENT_FUNCTIONS if not callable(getattr(agent_service, name, None))]
    document_missing = [name for name in REQUIRED_DOCUMENT_FUNCTIONS if not callable(getattr(document_service, name, None))]
    if missing or agent_missing or document_missing:
        st.error("工作台组件版本不一致。请重启本地工作台后重试。")
        st.caption(f"Missing service functions: {', '.join(missing + agent_missing + document_missing)}")
        return False
    return True


def current_account() -> dict | None:
    """Return the browser session's authenticated local account, if any."""
    user_id = st.session_state.get("account_user_id")
    username = st.session_state.get("account_username")
    if not user_id or not username:
        return None
    return {"id": int(user_id), "username": str(username)}


def _set_authenticated_account(account: dict) -> None:
    """Switch account contexts without carrying another user's open task."""
    st.session_state["account_user_id"] = int(account["id"])
    st.session_state["account_username"] = str(account["username"])
    st.session_state.pop("agent_task_id", None)
    st.session_state.pop("agent_intro_reply", None)


def logout_account() -> None:
    """End the local browser session; persisted research remains untouched."""
    st.session_state.pop("account_user_id", None)
    st.session_state.pop("account_username", None)
    st.session_state.pop("agent_task_id", None)
    st.session_state.pop("agent_intro_reply", None)
    st.session_state["account_notice"] = "已退出登录。"


def account_topbar() -> None:
    """Render compact account access immediately left of Streamlit toolbar actions."""
    account = current_account()
    with st.container(key="account_topbar"):
        label = f"{account['username']}" if account else "登录"
        with st.popover(label, use_container_width=True):
            notice = st.session_state.pop("account_notice", None)
            if notice:
                st.info(notice)
            if account:
                st.caption("当前登录账号")
                st.markdown(f"**{escape(account['username'])}**")
                st.button("退出登录", key="account_logout", on_click=logout_account, width="stretch")
            else:
                login_tab, register_tab = st.tabs(["登录", "注册"])
                with login_tab:
                    with st.form("account_login_form"):
                        username = st.text_input("用户名", key="account_login_username")
                        password = st.text_input("密码", type="password", key="account_login_password")
                        submitted = st.form_submit_button("登录", width="stretch")
                    if submitted:
                        try:
                            _set_authenticated_account(account_service.authenticate_user(username, password))
                        except account_service.AccountError as exc:
                            st.error(str(exc))
                        else:
                            st.rerun()
                with register_tab:
                    with st.form("account_register_form"):
                        username = st.text_input("用户名", key="account_register_username", help="3–32 位字母、数字、下划线或连字符")
                        password = st.text_input("设置密码", type="password", key="account_register_password")
                        confirmation = st.text_input("确认密码", type="password", key="account_register_confirmation")
                        submitted = st.form_submit_button("注册并登录", width="stretch")
                    if submitted:
                        if password != confirmation:
                            st.error("两次输入的密码不一致。")
                        else:
                            try:
                                _set_authenticated_account(account_service.register_user(username, password))
                            except account_service.AccountError as exc:
                                st.error(str(exc))
                            else:
                                st.rerun()


ROOT = Path(__file__).resolve().parent

NAVIGATION = [
    "案例演示", "AI Agent 研究", "研究总览", "市场与行情", "财务表现", "财务分析",
    "同行估值", "证据与校验", "文档智能", "Agent 执行轨迹", "研究报告", "研究任务",
    "评测与消融", "每日复盘",
    # CARD-15：清单按 DEMO_FLOW.md v3.2 校正（步骤 4b/6+7/7b/9+10/10b/F9/收尾/F1–F8）
    *[page.label for page in _DEMO_PAGES],
]

# Sidebar information architecture.  The flat list above remains the single
# compatibility contract for tests and deep links; this grouped view is the
# presentation layer users interact with.
NAV_GROUPS = [
    ("研究工作台", ("研究总览", "市场与行情", "财务表现", "财务分析", "同行估值")),
    ("AI 研究", ("AI Agent 研究", "Agent 执行轨迹")),
    ("证据与报告", ("证据与校验", "文档智能", "研究报告")),
    ("任务中心", ("研究任务", "每日复盘", "评测与消融")),
    ("演示与设置", ("案例演示", *[page.label for page in _DEMO_PAGES])),
]

# The sidebar tells the user where they are.  This local route tells them the
# next meaningful research action, even when it lives in another module.
RESEARCH_NEXT_STEPS = {
    "研究总览": (("查看市场与行情", "市场与行情"), ("查看财务表现", "财务表现")),
    "市场与行情": (("查看 RAW 价格证据", "证据与校验"), ("进入同行估值", "同行估值")),
    "财务表现": (("查看财务证据", "证据与校验"), ("让 Agent 解读", "AI Agent 研究")),
    "财务分析": (("查看财务证据", "证据与校验"), ("返回财务表现", "财务表现")),
    "同行估值": (("查看估值证据", "证据与校验"), ("查看研究报告", "研究报告")),
    "证据与校验": (("查看 AI 分析过程", "Agent 执行轨迹"), ("查看研究报告", "研究报告")),
    "Agent 执行轨迹": (("查看数据依据", "证据与校验"), ("打开研究报告", "研究报告")),
    "研究报告": (("查看数据依据", "证据与校验"), ("查看 AI 分析过程", "Agent 执行轨迹")),
}


def _select_page(page: str) -> None:
    """Update the canonical page state from a compact grouped sidebar item."""
    st.session_state["workbench_page"] = page


def sidebar_navigation() -> str:
    """Render a compact, grouped navigation while keeping the legacy radio."""
    current = st.session_state.get("workbench_page")
    if current not in NAVIGATION:
        current = NAVIGATION[0]
        st.session_state["workbench_page"] = current
    # Keep this widget as a stable compatibility surface for AppTest and
    # external deep links.  The session state is the single source of truth:
    # callbacks set the page before rerun, so passing ``index`` as a second
    # default causes Streamlit's default-value/session-state warning.
    st.radio("工作台", NAVIGATION, label_visibility="collapsed", key="workbench_page")

    for group_name, pages in NAV_GROUPS:
        visible_pages = [page for page in pages if page in NAVIGATION]
        if not visible_pages:
            continue
        is_active_group = current in visible_pages
        # Keep core research open; tuck long demo/diagnostic lists away.
        expanded = is_active_group or group_name == "研究工作台"
        with st.expander(group_name, expanded=expanded):
            for page in visible_pages:
                st.button(
                    page,
                    key=f"nav_{page}",
                    width="stretch",
                    type="primary" if page == current else "secondary",
                    on_click=_select_page,
                    args=(page,),
                )
    # Button callbacks update ``workbench_page`` before Streamlit reruns this
    # script.  Read it again here: returning the value captured before the
    # callback would render the previous page for one complete rerun.
    return st.session_state["workbench_page"]


def research_next_steps(page: str) -> None:
    """Render contextual cross-module actions for the current research step."""
    actions = RESEARCH_NEXT_STEPS.get(page)
    if not actions:
        return
    st.markdown('<div class="research-route-label">研究路径 · 下一步</div>', unsafe_allow_html=True)
    columns = st.columns(len(actions) + 1)
    for index, (label, destination) in enumerate(actions):
        columns[index].button(
            label,
            key=f"research_route_{page}_{destination}",
            width="stretch",
            on_click=_select_page,
            args=(destination,),
        )
    columns[-1].caption("跨模块入口仅跳转到同一份版本化快照。")


#: 自动预热开关。默认**关闭**：冒烟脚本与 UI 测试要能在「不解析 229 页年报」的
#: 前提下跑完，而"应用一启动就解析真实材料"恰好是它们的杀手（见
#: ``scripts/smoke_workbench.py`` 顶部那段说明）。现场想连那一次点击都省掉，
#: 就用 ``FINTRACE_DEMO_PREWARM=1 streamlit run workbench.py`` 启动。
PREWARM_ENV = "FINTRACE_DEMO_PREWARM"
_PREWARM_TRUTHY = {"1", "true", "yes", "on"}


def _prewarm_env_enabled() -> bool:
    return os.environ.get(PREWARM_ENV, "").strip().lower() in _PREWARM_TRUTHY


def demo_prewarm_autostart() -> None:
    """启动时按环境变量预热真实材料（只在本进程内，幂等）。

    必须是**本进程**：预热的是进程内存（``demo_service._DRAFT_STATES``），另起一个
    脚本解析一遍，对正在服务观众的这个进程毫无帮助 —— 这也是这里不用命令行脚本的原因。
    """
    if not _prewarm_env_enabled():
        return
    try:
        from src.cn import demo_service

        if demo_service.demo_material_ready():
            demo_service.prewarm_async()
    except Exception:  # noqa: BLE001 - 预热是加速手段，失败不该挡住工作台
        pass


def demo_prewarm_controls() -> None:
    """侧栏「演示预热」：把真实材料的首次解析挪到上台之前。

    数字一律来自 ``demo_service``，页面不自己估耗时 —— 预热的意义就是把
    ``corrections()`` 那个 ``build_seconds`` 变成 0，估出来的数会让这个判据作废。
    """
    from src.cn import demo_service

    if not demo_service.demo_material_ready():
        return
    state = demo_service.prewarm_state()
    with st.expander("演示预热", expanded=bool(state["running"] or state["error"])):
        if state["running"]:
            st.info(
                f"正在后台解析 {demo_service.DEMO_ANNUAL_PAGES} 页真实年报… "
                f"已 {state['elapsed']:.0f}s。完成后本行会自动变绿（任一次交互即刷新）。"
            )
        elif state["error"]:
            st.error(f"预热未成功：{state['error']}")
        elif state["done"]:
            st.success(
                f"真实材料已就绪（后台用时 {state['seconds']:.0f}s）。"
                "步骤 6+7 现场不再需要等待。"
            )
        else:
            st.caption(
                f"未预热。首次打开「研报纠错」要现解析 {demo_service.DEMO_ANNUAL_PAGES} 页年报。"
            )
        if st.button(
            "预热演示材料",
            key="prewarm_demo_material",
            width="stretch",
            disabled=bool(state["running"]),
            icon=":material/bolt:",
            help="在后台把真实年报解析进本进程内存；不写仓库、不改任何产物。",
        ):
            if demo_service.prewarm_async():
                st.rerun()
            else:
                st.caption("已经在预热了，不用再来一次。")

#: CARD-03 vocabulary rendered in Chinese. Kept in the UI layer so the engine's
#: tokens stay stable for the API, the ledger and the benchmark.
_COMPARISON_LABELS = {"yoy": "同比", "qoq": "环比", "level": "水平值", "none": "—"}
_SEMANTIC_LABELS = {
    "normal": "正常（可出百分比）",
    "turnaround": "扭亏为盈（只给绝对变动）",
    "to_loss": "由盈转亏（只给绝对变动）",
    "loss_widening": "亏损扩大（只给绝对变动）",
    "loss_narrowing": "亏损收窄（只给绝对变动）",
    "not_computable": "不可计算",
}

DEMO_CASES = {
    "成功研究：贵州茅台": {
        "snapshot_id": DEMO_SNAPSHOT_ID,
        "expected_allowed": True,
        "summary": "确定性演示快照、完整证据链和同行估值输入允许输出结论；所有数值均明确标记为 illustrative。",
        "next_page": "证据与校验",
    },
    "Validator 阻断：缺少关键估值输入": {
        "snapshot_id": BLOCKED_DEMO_SNAPSHOT_ID,
        "expected_allowed": False,
        "summary": "同一套确定性规则发现总股本证据缺失，因此隐藏估值结论和报告。",
        "next_page": "证据与校验",
    },
}


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def load_research(snapshot_id: str) -> tuple[dict, dict]:
    """Load only versioned local data; the UI remains a presentation layer."""
    snapshot = service.load_snapshot(snapshot_id)
    summary = service.research_summary(snapshot_id)
    return snapshot, summary


@st.cache_data(ttl=10, max_entries=1, show_spinner=False)
def cached_research_catalog() -> list[dict]:
    """Avoid reopening every snapshot file on ordinary UI interactions.

    The explicit refresh control clears this cache, so newly acquired local
    snapshots remain immediately discoverable when the user asks for them.
    """
    return service.list_research()


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_market(snapshot_id: str, adjustment: str = "RAW") -> dict:
    return service.research_market(snapshot_id, adjustment)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_financials(snapshot_id: str) -> dict:
    return service.research_financials(snapshot_id)


@st.cache_data(ttl=300, max_entries=128, show_spinner=False)
def cached_research_financial_trends(snapshot_id: str, period_basis: str) -> dict:
    return service.research_financial_trends(snapshot_id, period_basis)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_evidence(snapshot_id: str) -> dict:
    return service.research_evidence(snapshot_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_validation(snapshot_id: str) -> dict:
    return service.research_validation(snapshot_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_readiness(snapshot_id: str) -> dict:
    return service.research_readiness(snapshot_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_valuation(snapshot_id: str) -> dict:
    return service.research_valuation(snapshot_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_overview_insights(snapshot_id: str) -> dict:
    return service.research_overview_insights(snapshot_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def cached_research_display_guardrails(snapshot_id: str) -> dict:
    return service.research_display_guardrails(snapshot_id)


def cn_money(value: float, unit: str = "亿元") -> str:
    divisor = 1e8 if unit == "亿元" else 1e4
    return f"{value / divisor:,.2f} {unit}"


def evidence_id(symbol: str, metric: str, period: str) -> str:
    return f"fact_{symbol.replace('.', '_')}_{metric}_{period}"


def dependency_graph_dot(graph: dict) -> str:
    """Render only dependency-bearing evidence records from the service graph."""
    edges = graph.get("edges", [])
    connected = {item[key] for item in edges for key in ("source", "target")}
    nodes = {item["id"]: item for item in graph.get("nodes", []) if item["id"] in connected}
    lines = ['digraph {', 'rankdir=LR;', 'node [shape=box, style=rounded, color="#7c9bc7", fontname="Arial"];']
    for node_id, node in nodes.items():
        safe_id = node_id.replace('"', '\\"')
        label = f"{node['metric']}\\n{node_id}".replace('"', '\\"')
        lines.append(f'"{safe_id}" [label="{label}"];')
    for edge in edges:
        source = edge["source"].replace('"', '\\"')
        target = edge["target"].replace('"', '\\"')
        lines.append(f'"{source}" -> "{target}";')
    lines.append('}')
    return "\n".join(lines)


def open_evidence(evidence_id_value: str) -> None:
    """Route a KPI evidence action to the read-only evidence detail."""
    st.session_state["selected_evidence_id"] = evidence_id_value
    st.session_state["workbench_page"] = "证据与校验"


def open_demo_case(snapshot_label: str, page: str) -> None:
    """Switch the read-only workbench to one reproducible demonstration case."""
    # The sidebar selectbox stores its visible label, not the snapshot ID.
    st.session_state["snapshot_picker"] = snapshot_label
    st.session_state["workbench_page"] = page


def snapshot_choice_label(item: dict[str, str]) -> str:
    """Make snapshot choices unique when one symbol has multiple versions."""
    snapshot_id = str(item.get("id", ""))
    version = snapshot_id.rsplit("_", 1)[-1] if "_" in snapshot_id else "snapshot"
    as_of = str(item.get("research_as_of", ""))[:10] or "未知日期"
    provider = str(item.get("provider", "snapshot"))
    return f"{item['name']} · {item['symbol']} · 研究截至 {as_of} · {provider} · {version}"


def snapshot_picker_label(label: str) -> str:
    """Keep snapshot choices compact without hiding their version identity."""
    parts = label.split(" · ")
    if len(parts) >= 5:
        # The date and version are the two pieces a user needs to distinguish
        # successive online refreshes.  The full provider identifier remains
        # in state and in the detail surfaces, not in this compact selector.
        research_date = parts[2].removeprefix("研究截至 ")
        return " · ".join((parts[0], parts[1], research_date, parts[-1]))
    return " · ".join(parts[:2]) if len(parts) >= 2 else label


def online_refresh_symbols(catalog: list[dict[str, str]]) -> dict[str, str]:
    """One selector row per canonical symbol for the online refresh control."""
    options: dict[str, str] = {}
    for item in sorted(catalog, key=lambda entry: str(entry.get("symbol", ""))):
        options.setdefault(f"{item['name']} · {item['symbol']}", item["symbol"])
    return options


def describe_refresh_outcome(outcome: dict) -> tuple[str, str]:
    """Turn one refresh outcome into ``(severity, text)`` for the sidebar.

    A failure always names the specific reason *and* the snapshot still in use,
    so the reader can never mistake a failed refresh for an updated one.
    """
    status = str(outcome.get("refresh_status", "failed"))
    if status == "succeeded":
        return "success", "\n\n".join([
            f"已创建新快照 {outcome.get('snapshot_id')}",
            f"研究截至 {outcome.get('research_as_of') or '—'} · Provider {outcome.get('provider') or '—'}"
            f" · 收盘价截至 {outcome.get('latest_trade_date') or '—'}",
            f"版本 v{outcome.get('version')} · 新增 {outcome.get('new_bar_count', 0)} 个交易日",
        ])
    lines = [str(outcome.get("message") or "刷新未完成。")]
    current = outcome.get("current_snapshot") or {}
    if current.get("snapshot_id"):
        lines.append(
            f"当前仍在使用的快照：{current['snapshot_id']}"
            f"（研究截至 {current.get('research_as_of') or '—'}"
            f"，收盘价截至 {current.get('latest_trade_date') or '—'}）"
        )
    if status == "no_new_data":
        return "info", "\n\n".join(lines)
    lines.append("未写入任何快照；旧快照、旧报告与旧证据保持不变。")
    if outcome.get("retryable"):
        lines.append("该失败属于上游限频/网络类，可直接重试。")
    return ("warning" if status == "no_data" else "error"), "\n\n".join(lines)


def scoped_refresh_notice(notice: object, *, current_symbol: str) -> tuple[str, str]:
    """Explain which symbol an online refresh result belongs to.

    The sidebar can refresh a symbol different from the research version that
    is currently open.  Never present that result as if it described the open
    snapshot.
    """
    # A page reload can retain a notice created by the previous UI shape.
    # Preserve it as an explicitly unscoped legacy result instead of crashing
    # or pretending it belongs to the currently selected stock.
    if not isinstance(notice, dict):
        severity, text = notice if isinstance(notice, tuple) and len(notice) == 2 else ("info", "联网更新已结束。")
        return str(severity), f"联网更新结果（未记录标的；当前研究标的：{current_symbol}）\n\n{text}"
    symbol = str(notice.get("symbol") or "未知标的")
    severity = str(notice.get("severity") or "info")
    text = str(notice.get("text") or "联网更新已结束。")
    if symbol == current_symbol:
        prefix = f"联网更新对象：{symbol}（当前研究标的）"
    else:
        prefix = f"联网更新对象：{symbol}；当前研究标的：{current_symbol}。两者为不同数据版本。"
    return severity, f"{prefix}\n\n{text}"


def switch_to_local_baseline(snapshot_id: str) -> None:
    """Callback for the offline baseline switcher in the sidebar.

    Streamlit only permits changing a widget's session-state value before that
    widget is instantiated on the rerun.  Button callbacks run in exactly that
    phase, unlike an assignment immediately after ``st.selectbox``.
    """
    local_baseline = service.find_local_baseline_snapshot(snapshot_id)
    service.load_snapshot.cache_clear()
    cached_research_catalog.clear()
    load_research.clear()
    st.cache_data.clear()
    if local_baseline is not None:
        st.session_state["snapshot_picker"] = snapshot_choice_label(local_baseline)
        st.session_state["local_baseline_notice"] = (
            "已切回本地基准版本；本次操作未联网，也没有覆盖任何数据版本。"
        )
    else:
        st.session_state["local_baseline_notice"] = (
            "当前标的没有可切回的同源本地基准版本；已重新载入现有版本，未发起联网请求。"
        )


def HeroSection() -> None:
    """Lead the landing page with product value, not workbench mechanics."""
    st.markdown(
        """<section class="home-hero">
          <div class="home-nav"><div class="home-wordmark"><span>◈</span> FinTrace-CN</div><div class="home-nav-note">可信 AI 金融研究</div></div>
          <div class="home-hero-grid">
            <div>
              <div class="home-eyebrow"><i></i> A-SHARE RESEARCH AGENT</div>
              <h1>可信金融研究 Agent</h1>
              <p>让 AI 生成的每一个金融结论，都拥有可追溯的数据来源和验证过程。</p>
              <div class="value-pills"><span>数据可追溯</span><span>计算可验证</span><span>研究可复现</span></div>
            </div>
            <div class="home-orbit" aria-hidden="true">
              <div class="orbit-core">可信<br>结论</div><div class="orbit-node n1">数据来源</div>
              <div class="orbit-node n2">验证过程</div><div class="orbit-node n3">研究记录</div>
            </div>
          </div>
        </section>""",
        unsafe_allow_html=True,
    )
    st.button(
        "开始研究",
        key="home_start_research",
        type="primary",
        icon=":material/arrow_forward:",
        on_click=_select_page,
        args=("AI Agent 研究",),
    )
    st.caption("仅展示固定版本的 A 股研究快照；不构成投资建议。")


def ProblemSection() -> None:
    """Make the trust problem immediately legible to a first-time visitor."""
    st.markdown(
        """<section class="home-section problem-section">
          <div class="section-kicker">WHY FINTRACE</div>
          <h2>为什么需要 FinTrace？</h2>
          <p class="section-lead">传统 AI 可以生成答案，却未必能说明答案为什么可信。</p>
          <div class="problem-grid">
            <article><b>✕</b><h3>数据来源不可追踪</h3><p>结论无法回到原始数据与研究时点。</p></article>
            <article><b>✕</b><h3>财务计算无法复核</h3><p>关键口径、单位和推导过程容易被忽略。</p></article>
            <article><b>✕</b><h3>AI 可能产生错误结论</h3><p>缺少关键输入时，回答仍可能看似完整。</p></article>
          </div>
        </section>""",
        unsafe_allow_html=True,
    )


def WorkflowSection() -> None:
    """Show the solution as one clear research flow before technical labels."""
    st.markdown(
        """<section class="home-section workflow-section">
          <div class="section-kicker">THE FINTRACE METHOD</div>
          <h2>把 AI 研究放进可验证的流程</h2>
          <p class="section-lead">每一步留下记录；必要的信息不足时，系统会停止结论，而不是补一个答案。</p>
          <div class="workflow-rail">
            <div><em>01</em><strong>数据快照</strong><span>固定研究时点</span></div><i></i>
            <div><em>02</em><strong>证据链</strong><span>绑定来源与依据</span></div><i></i>
            <div><em>03</em><strong>自动验证</strong><span>核验输入与计算</span></div><i></i>
            <div><em>04</em><strong>研究报告</strong><span>交付或自动阻断</span></div>
          </div>
        </section>""",
        unsafe_allow_html=True,
    )


def FeatureCards() -> None:
    """Introduce the durable technical primitives only after their value."""
    st.markdown(
        """<section class="home-section feature-section">
          <div class="section-kicker">CORE CAPABILITIES</div>
          <h2>让可信不是一句承诺</h2>
          <div class="feature-grid">
            <article><div class="feature-number">01</div><h3>Versioned Snapshot</h3><b>版本化数据快照</b><p>固定研究环境，保证不同时间运行结果一致。</p></article>
            <article><div class="feature-number">02</div><h3>Evidence Ledger</h3><b>证据链系统</b><p>每个研究结论绑定数据来源、计算过程和引用依据。</p></article>
            <article><div class="feature-number">03</div><h3>Validator</h3><b>智能验证机制</b><p>发现关键数据缺失时自动阻断错误结论。</p></article>
          </div>
        </section>""",
        unsafe_allow_html=True,
    )


def CaseStudy(catalog: list[dict[str, str]], *, featured: bool = False) -> None:
    """Show a passing and a blocked path without leading with internal IDs."""
    available = {item["id"] for item in catalog}
    labels_by_id = {item["id"]: snapshot_choice_label(item) for item in catalog}
    section_class = "featured-case" if featured else ""
    st.markdown(
        f'''<section class="home-section case-section {section_class}"><div class="section-kicker">RESEARCH IN PRACTICE</div>
        <h2>研究结果，应该经得起追问</h2><p class="section-lead">同一套规则既支持交付，也能在风险出现时及时止步。</p></section>''',
        unsafe_allow_html=True,
    )
    columns = st.columns(2, gap="large")
    for (name, case), column in zip(DEMO_CASES.items(), columns):
        with column:
            exists = case["snapshot_id"] in available
            allowed = False
            if exists:
                validation = cached_research_validation(case["snapshot_id"])
                allowed = bool(validation["conclusion_allowed"])
            passing = exists and allowed
            status = "✓ 验证通过" if passing else "⚠ 自动阻断"
            status_class = "case-pass" if passing else "case-block"
            title = "贵州茅台估值研究" if case["expected_allowed"] else "数据不足案例"
            checklist = (
                "<li>数据完整</li><li>证据完整</li><li>报告生成</li>"
                if passing else "<li>缺少关键估值输入</li><li>系统拒绝生成错误结论</li>"
            )
            st.markdown(
                f'''<article class="case-card {status_class}"><div class="case-top"><span>案例研究</span><b>{status}</b></div>
                <h3>{title}</h3><p>{case["summary"]}</p><ul>{checklist}</ul></article>''',
                unsafe_allow_html=True,
            )
            with st.expander("查看详情", expanded=False):
                if not exists:
                    st.warning("演示快照尚未就绪，无法展示研究详情。")
                else:
                    st.caption("技术详情：本案例由版本化 Snapshot、Evidence Ledger 与 Validator 驱动。")
                    if allowed != bool(case["expected_allowed"]):
                        st.error("演示快照的实际门禁结果与预期不一致，请先运行测试或重新生成快照。")
                    st.button(
                        "进入研究报告" if allowed else "查看阻断原因",
                        key=f"demo_{case['snapshot_id']}",
                        on_click=open_demo_case,
                        args=(labels_by_id[case["snapshot_id"]], case["next_page"]),
                    )


def ArchitectureSection() -> None:
    """Keep the developer architecture discoverable, but deliberately last."""
    st.markdown(
        """<section class="architecture-section"><div class="section-kicker">FOR DEVELOPERS</div>
          <h2>技术架构</h2><p>面向研发团队的可组合研究基础设施。</p>
          <div class="architecture-flow"><span>LLM Agent</span><i>↓</i><span>Research Tools</span><i>↓</i>
          <span>Evidence Ledger</span><i>↓</i><span>Validator</span><i>↓</i><span>Report Generator</span></div>
        </section>""",
        unsafe_allow_html=True,
    )


def demo_cases(catalog: list[dict[str, str]]) -> None:
    """Render the product landing page while preserving existing demo routes."""
    HeroSection()
    CaseStudy(catalog, featured=True)
    ProblemSection()
    WorkflowSection()
    FeatureCards()
    ArchitectureSection()


# Apple-inspired design tokens.  All colors come from the design system
# page; Chinese rendering uses Sarasa Gothic SC when available, falling back
# to system sans-serif so SF Pro / PingFang / Noto resolve natively.
APPLE_TOKENS = {
    "ink": "#1D1D1F",
    "ink_muted_48": "#7A7A7A",
    "ink_muted_64": "#6E6E73",
    "ink_muted_80": "#535359",
    "primary": "#0066CC",
    "primary_focus": "#0071E3",
    "primary_on_dark": "#2997FF",
    "canvas": "#FFFFFF",
    "parchment": "#F5F5F7",
    "pearl": "#FAFAFC",
    "surface_chip_translucent": "#D2D2D7",
    "divider_soft": "#F0F0F0",
    "hairline": "#E0E0E0",
    "tile_dark": "#272729",
    "success_bg": "#DFF4EB",
    "success_fg": "#1E7E50",
    "warning_bg": "#FFF1CF",
    "warning_fg": "#8A5A00",
    "blocked_bg": "#FDE4E1",
    "blocked_fg": "#A33B32",
    "snapshot_bg": "#E8EFFA",
    "snapshot_fg": "#315D9D",
    # A-share market semantics: 涨红跌绿 (only for quote / context where it
    # matches real trading conventions).  Brand color is never used for these.
    "quote_up": "#C0392B",
    "quote_down": "#1C9A82",
    "evidence": "#0066CC",
}


def _plotly_template() -> go.layout.Template:
    """Return an Apple-inspired Plotly template shared across the workbench."""
    template = go.layout.Template()
    template.layout.paper_bgcolor = APPLE_TOKENS["canvas"]
    template.layout.plot_bgcolor = APPLE_TOKENS["canvas"]
    template.layout.font = dict(
        family="Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Sarasa Gothic SC', sans-serif",
        color=APPLE_TOKENS["ink"],
        size=13,
    )
    template.layout.title = dict(
        font=dict(size=15, color=APPLE_TOKENS["ink"], family="Inter, 'Sarasa Gothic SC', sans-serif"),
        x=0.02, xanchor="left",
    )
    template.layout.margin = dict(l=16, r=16, t=46, b=16)
    template.layout.xaxis = dict(
        showgrid=False, showline=False, zeroline=False,
        tickfont=dict(size=11, color=APPLE_TOKENS["ink_muted_48"]),
    )
    template.layout.yaxis = dict(
        showgrid=True, gridcolor=APPLE_TOKENS["divider_soft"],
        zeroline=False, showline=False,
        tickfont=dict(size=11, color=APPLE_TOKENS["ink_muted_48"]),
    )
    template.layout.colorway = [
        APPLE_TOKENS["primary"],  # Action Blue — first/primary
        "#1C9A82",                # teal accent (was #0F766E in old theme)
        "#6D8FD5",                # muted blue (matches the existing financial bars)
        APPLE_TOKENS["quote_up"],
        APPLE_TOKENS["quote_down"],
        APPLE_TOKENS["warning_fg"],
        "#8E8E93",                # neutral gray
    ]
    template.layout.legend = dict(
        orientation="h", y=1.1, x=0, xanchor="left",
        font=dict(size=12, color=APPLE_TOKENS["ink"]),
        bgcolor="rgba(0,0,0,0)",
    )
    return template


def style() -> None:
    """Inject Apple-inspired CSS, register Plotly template, and set page meta."""
    t = APPLE_TOKENS
    px = importlib.import_module("plotly.io").templates
    px["fintrace_apple"] = _plotly_template()  # type: ignore[index]
    # Make Apple-inspired colors + fonts the workbench default so every
    # fig.update_layout / px.* call inherits them automatically.
    px.default = "fintrace_apple"

    st.set_page_config(
        page_title="FinTrace-CN | 研究工作台",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        f"""<style>
:root {{
  --ink: {t['ink']};
  --muted: {t['ink_muted_48']};
  --line: {t['hairline']};
  --parchment: {t['parchment']};
  --primary: {t['primary']};
  --evidence: {t['evidence']};
  --quote-up: {t['quote_up']};
  --quote-down: {t['quote_down']};
}}
/* Apple-first font stack: SF Pro on macOS/iOS, Inter for cross-platform,
   Sarasa Gothic SC for Chinese characters. */
html, body, .stApp, [data-testid="stMarkdownContainer"], .stMarkdown, p, li, span, label {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text',
               'PingFang SC', 'Sarasa Gothic SC', 'Segoe UI', Roboto,
               'Helvetica Neue', Arial, sans-serif !important;
  -webkit-font-smoothing: antialiased;
  color: var(--ink);
}}
/* Restore Streamlit Material Symbols icon font; the global span override above
   otherwise renders icon ligatures (e.g. expand_more) as literal text. */
span[style*="Material Symbols Rounded"],
span[class*="material"],
span[class*="stIcon"],
span[data-testid*="Icon"] {{
  font-family: 'Material Symbols Rounded' !important;
}}
.stApp {{ background: {t['canvas']}; }}
/* Sub-nav: parchment frosted look (Apple sub-nav-frosted analog). */
section[data-testid="stSidebar"] {{
  background: {t['parchment']};
  border-right: 1px solid {t['divider_soft']};
  min-width: 272px;
  max-width: 300px;
}}
section[data-testid="stSidebar"] * {{ color: {t['ink']} !important; }}
/* The legacy radio remains in the DOM for automation/deep-link compatibility;
   grouped buttons below are the readable interaction surface. */
section[data-testid="stSidebar"] [data-testid="stRadio"] {{ display: none; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] {{
  border: 0 !important;
  border-bottom: 1px solid {t['divider_soft']} !important;
  border-radius: 0 !important;
  background: transparent !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {{
  padding: 0.55rem 0.2rem !important;
  font-size: 0.72rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: {t['ink_muted_64']} !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stVerticalBlock"] {{
  gap: 0.18rem !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button {{
  justify-content: flex-start;
  text-align: left;
  border: 0 !important;
  border-radius: 8px !important;
  padding: 0.42rem 0.62rem !important;
  min-height: 2rem;
  font-size: 0.86rem;
  font-weight: 500 !important;
  background: transparent !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button:hover {{
  background: rgba(0, 102, 204, 0.08) !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button[kind="primary"] {{
  background: rgba(0, 102, 204, 0.12) !important;
  color: {t['primary']} !important;
  font-weight: 700 !important;
}}
section[data-testid="stSidebar"] .stRadio label {{
  border-radius: 8px;
  padding: 8px 10px;
  transition: background 120ms ease;
}}
section[data-testid="stSidebar"] .stRadio label:hover {{
  background: rgba(0, 102, 204, 0.08);
}}
.block-container {{
  padding: 1.6rem 2.4rem 2.4rem;
  max-width: 1480px;
}}
/* Page eyebrows / section heads */
.eyebrow {{
  color: {t['ink_muted_48']};
  font-size: 0.74rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-weight: 700;
  margin-bottom: 0.4rem;
}}
h1 {{
  font-size: 2.0rem !important;
  letter-spacing: -0.035em !important;
  font-weight: 700 !important;
  margin-bottom: 0.2rem !important;
  color: {t['ink']};
}}
h2 {{
  font-size: 1.5rem !important;
  letter-spacing: -0.02em !important;
  font-weight: 600 !important;
  margin-top: 1.5rem !important;
  color: {t['ink']};
}}
h3 {{
  font-size: 1.1rem !important;
  font-weight: 600 !important;
  color: {t['ink']};
  margin-top: 1.2rem !important;
  margin-bottom: 0.4rem !important;
}}
.subline {{ color: var(--muted); font-size: 0.92rem; }}

/* Status bar (capsule pill row) */
.statusbar {{
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem;
  padding: 0.7rem 0.9rem; background: {t['canvas']};
  border: 1px solid {t['divider_soft']}; border-radius: 14px;
  margin: 0.9rem 0 1.1rem;
}}
.badge {{
  display: inline-flex; align-items: center; gap: 0.32rem;
  padding: 0.28rem 0.7rem; border-radius: 9999px;
  font-size: 0.74rem; font-weight: 600;
  border: 1px solid transparent;
}}
.verified {{ background: {t['success_bg']}; color: {t['success_fg']}; }}
.warning {{ background: {t['warning_bg']}; color: {t['warning_fg']}; }}
.blocked {{ background: {t['blocked_bg']}; color: {t['blocked_fg']}; }}
.snapshot {{ background: {t['snapshot_bg']}; color: {t['snapshot_fg']}; }}
.neutral {{ background: {t['parchment']}; color: {t['ink_muted_80']}; border-color: {t['divider_soft']}; }}
.up {{ color: {t['quote_up']}; font-weight: 600; }}
.down {{ color: {t['quote_down']}; font-weight: 600; }}

/* Metric cards (Apple hairline + zero shadow) */
[data-testid="stMetric"] {{
  background: {t['canvas']};
  border: 1px solid {t['divider_soft']};
  border-radius: 14px;
  padding: 1.0rem 1.1rem;
  min-height: 120px;
}}
[data-testid="stMetricLabel"] {{
  color: {t['ink_muted_48']};
  font-weight: 600;
  font-size: 0.78rem;
  letter-spacing: 0.02em;
}}
[data-testid="stMetricValue"] {{
  color: {t['ink']};
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}}
[data-testid="stMetricDelta"] {{ font-size: 0.78rem; }}

/* Container with border → use as utility card */
div[data-testid="stVerticalBlockBorderWrapper"] > div,
.stContainer[data-testid="stContainer"]:has(> div[data-testid="stVerticalBlock"]),
.stContainer[border="True"] {{
  background: {t['canvas']};
  border: 1px solid {t['divider_soft']} !important;
  border-radius: 14px !important;
  padding: 1rem 1.2rem;
  box-shadow: none !important;
}}

/* Evidence id style — link-blue mono, the one place where a true link
   colour is allowed. */
.evidence {{
  color: {t['evidence']};
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem; font-weight: 600;
}}

/* Warning / blocked callout cards: hairline + 3px left border, no shadow */
.warn {{
  border-left: 3px solid {t['warning_fg']};
  background: {t['warning_bg']};
  padding: 0.75rem 0.95rem; border-radius: 8px;
  color: {t['warning_fg']};
}}
.blocked-card {{
  border-left: 3px solid {t['blocked_fg']};
  background: {t['blocked_bg']};
  padding: 0.75rem 0.95rem; border-radius: 8px;
  color: {t['blocked_fg']};
}}

/* Pill buttons — Apple action grammar (radius 9999, blue fill). */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {{
  border-radius: 9999px !important;
  font-weight: 600 !important;
  border: 1px solid {t['hairline']} !important;
  background: {t['canvas']} !important;
  color: {t['ink']} !important;
  padding: 0.45rem 1.1rem !important;
  transition: transform 100ms ease, background 120ms ease;
}}
.stButton > button:hover,
.stDownloadButton > button:hover,
.stFormSubmitButton > button:hover {{
  background: {t['parchment']} !important;
}}
.stButton > button:active,
.stFormSubmitButton > button:active {{
  transform: scale(0.97);
}}
/* Primary (type=primary) → Action Blue pill */
.stButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"] {{
  background: {t['primary']} !important;
  color: {t['canvas']} !important;
  border-color: {t['primary']} !important;
}}
.stButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover {{
  background: {t['primary_focus']} !important;
  border-color: {t['primary_focus']} !important;
}}
/* Dark utility (export / 登录 / 取消 actions) */
.stButton > button.dark-util,
.stDownloadButton > button.dark-util {{
  background: {t['ink']} !important;
  color: {t['canvas']} !important;
  border-color: {t['ink']} !important;
  border-radius: 8px !important;
}}
.stButton > button.dark-util:hover {{
  background: {t['tile_dark']} !important;
}}

/* Inputs / date pickers / selects — pill radius, hairline border. */
.stTextInput input, .stDateInput input, .stTextArea textarea,
.stNumberInput input, [data-baseweb="input"] input {{
  border-radius: 10px !important;
  border: 1px solid {t['hairline']} !important;
  background: {t['canvas']} !important;
  color: {t['ink']} !important;
}}
.stTextInput input:focus, .stDateInput input:focus, .stTextArea textarea:focus {{
  border-color: {t['primary']} !important;
  box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.18) !important;
}}

/* Segmented control */
[data-testid="stSegmentedControl"] [role="radiogroup"] {{
  background: {t['parchment']};
  border-radius: 10px;
  padding: 3px;
}}
[data-testid="stSegmentedControl"] label {{
  border-radius: 8px !important;
}}

/* DataFrames — zero default shadow, hairline border */
.stDataFrame, [data-testid="stDataFrame"] {{
  border: 1px solid {t['divider_soft']} !important;
  border-radius: 10px !important;
  overflow: hidden;
}}

/* Trace timeline (legacy _trace_legacy) */
.trace {{ border-left: 2px solid {t['hairline']}; padding: 0.5rem 0 0.5rem 1rem; margin-left: 0.5rem; }}

/* Sidebar brand line */
.brand {{
  font-size: 1.15rem; font-weight: 700;
  letter-spacing: -0.01em;
  color: {t['ink']};
  margin: 0.2rem 0 1.4rem;
}}

/* -----------------------------------------------------------------------
   FinTrace visual system v3 — a calm research terminal instead of a generic
   Streamlit form. The data model stays unchanged; this layer only improves
   hierarchy, density and scanability. */
.stApp {{
  background: #F2F5F9;
  background-image: radial-gradient(circle at 76% -10%, rgba(47, 137, 255, .10), transparent 32rem);
}}
[data-testid="stAppViewContainer"] > .main {{ background: transparent; }}
.block-container {{
  max-width: 1600px;
  padding: 2.6rem 3.2rem 4.5rem;
}}
section[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, #0B1729 0%, #101F35 56%, #0C192C 100%);
  border-right: 1px solid #243B5B;
  min-width: 306px;
  max-width: 330px;
}}
section[data-testid="stSidebar"] * {{ color: #E7EFFA !important; }}
section[data-testid="stSidebar"] .brand {{
  color: #FFFFFF !important;
  font-size: 1.16rem;
  letter-spacing: .035em;
  margin: .35rem .2rem 1.65rem;
}}
section[data-testid="stSidebar"] .brand-block {{ margin-bottom: .3rem; }}
section[data-testid="stSidebar"] .brand-sub {{
  color: #7792B4 !important; font-size: .58rem; letter-spacing: .14em;
  margin: -.9rem .25rem 1.55rem; white-space: nowrap;
}}
section[data-testid="stSidebar"] .brand-sub span {{ color: #3F628E !important; padding: 0 .2rem; }}
section[data-testid="stSidebar"] .stCaption {{ color: #7F99BA !important; }}
section[data-testid="stSidebar"] .sidebar-guide {{ color: #B8CBE2 !important; font-size: .74rem; line-height: 1.55; padding: .15rem .1rem .35rem; }}
section[data-testid="stSidebar"] .sidebar-guide * {{ color: inherit !important; }}
section[data-testid="stSidebar"] .guide-title {{ color: #FFFFFF !important; font-weight: 750; margin-bottom: .55rem; }}
section[data-testid="stSidebar"] .guide-row {{ display: grid; grid-template-columns: .55rem auto 1fr; gap: .38rem; align-items: center; margin: .42rem 0; }}
section[data-testid="stSidebar"] .guide-row b {{ color: #E9F2FE !important; font-weight: 650; white-space: nowrap; }}
section[data-testid="stSidebar"] .guide-dot {{ width: .48rem; height: .48rem; border-radius: 50%; display: inline-block; }}
section[data-testid="stSidebar"] .guide-green {{ background: #39D3B0; }}
section[data-testid="stSidebar"] .guide-yellow {{ background: #F5C451; }}
section[data-testid="stSidebar"] .guide-red {{ background: #FF7C78; }}
section[data-testid="stSidebar"] .sidebar-guide hr {{ border: 0; border-top: 1px solid rgba(148,177,215,.16); margin: .7rem 0; }}
section[data-testid="stSidebar"] .guide-note {{ color: #9EB5D1 !important; margin: .38rem 0; }}
section[data-testid="stSidebar"] .guide-note b {{ color: #D8E6F7 !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] {{
  border: 0 !important;
  border-top: 1px solid rgba(148, 177, 215, .16) !important;
  border-radius: 0 !important;
  background: transparent !important;
  margin: .2rem 0 0;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] details,
section[data-testid="stSidebar"] [data-testid="stExpander"] details > summary {{
  background: transparent !important;
  color: #C7D7EA !important;
  border: 0 !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {{
  padding: .75rem .25rem .55rem !important;
  color: #B5C9E2 !important;
  font-size: .68rem !important;
  letter-spacing: .14em;
  font-weight: 800 !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
section[data-testid="stSidebar"] [data-testid="stExpander"] summary span {{ color: #B5C9E2 !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
section[data-testid="stSidebar"] [data-testid="stExpander"] summary * {{ opacity: 1 !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details > summary:hover {{
  background: rgba(90, 157, 255, .10) !important;
  color: #FFFFFF !important;
  border-radius: 8px !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] details > summary:hover span {{ color: #FFFFFF !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details > summary:focus-visible {{
  outline: 2px solid #65A8FF !important;
  outline-offset: 2px;
  color: #FFFFFF !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] details > summary:focus-visible span {{ color: #FFFFFF !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details[open] > summary {{ color: #F5F9FF !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details[open] > summary span {{ color: #F5F9FF !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details[open] > summary svg {{ color: #9BC5FF !important; fill: #9BC5FF !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button {{
  color: #C6D6EB !important;
  border: 1px solid transparent !important;
  border-radius: 8px !important;
  background: transparent !important;
  min-height: 2.15rem;
  padding: .44rem .7rem !important;
  font-size: .84rem;
  font-weight: 550 !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button:hover {{
  background: rgba(90, 157, 255, .12) !important;
  border-color: rgba(90, 157, 255, .18) !important;
  color: #FFFFFF !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button[kind="primary"] {{
  background: linear-gradient(90deg, #146EE8, #1E82F8) !important;
  border-color: #318CF4 !important;
  color: #FFFFFF !important;
  box-shadow: 0 7px 18px rgba(12, 92, 201, .32);
}}
/* ── Sidebar select（研究快照）───────────────────────────────────────────
   ⚠️ Streamlit 1.61 换了 widget 实现：baseweb 的 [data-baseweb="select"] /
   [data-baseweb="popover"] 已经**全部消失**（实测 Selectbox.<hash>.js 里
   "baseweb" 出现 0 次，"className:`stSelectbox`" 出现 1 次）。旧规则因此
   整段成为死代码 —— 这正是同一处「白底白字」反复复发的根因：每修一轮都
   在给一个不存在的 DOM 加样式。现在按 1.61 的真实类名重写。

   实测到的结构（打包产物 Selectbox.Cnugupx0.js）：
     · 控件根          div.stSelectbox  →  内含 div[data-testid="stSelectbox"]
     · 值文本/输入框   同根下的 input + 一个显示 label 的节点
     · 下拉浮层        [data-testid="stSelectboxVirtualDropdown"]，
                      经 createPortal 挂到 **document.body**（不在侧栏 DOM 内），
                      因此必须单独写规则，且不能依赖侧栏后代选择器。
   背景与文字**成对**钉死（同一个选择器里同时给 background-color 与 color），
   这样任何单边覆盖都不会再把它变回白底白字。 */
section[data-testid="stSidebar"] .stSelectbox,
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"],
section[data-testid="stSidebar"] .stSelectbox input,
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"] > div {{
  background-color: #162A47 !important;
  background-image: none !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
}}
/* 只有「根 + 内层方框」这一层有边框；写成两条避免在 input 上再画一圈。 */
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"] > div {{
  border: 1px solid #2C476B !important;
  border-radius: 10px !important;
}}
section[data-testid="stSidebar"] .stSelectbox svg {{
  color: #9BC5FF !important; fill: #9BC5FF !important;
}}
/* 选中值那一行：文字与它所在的整块底一起上色。`*` 会把光标、placeholder
   一起刷白，对深色底是正确的，但**不要**再单独给 color 而不给底色。 */
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"] * {{
  background-color: #162A47 !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
}}

/* 下拉浮层：portal 到 body，故用 [data-testid] 直接命中，不写侧栏前缀。
   命中不了就退回 role 语义（listbox/option），两条都留着以免版本漂移。 */
[data-testid="stSelectboxVirtualDropdown"] {{
  background-color: #162A47 !important;
  background-image: none !important;
  border: 1px solid #2C476B !important;
  border-radius: 10px !important;
  box-shadow: 0 18px 42px rgba(4, 12, 26, .45) !important;
}}
[data-testid="stSelectboxVirtualDropdown"] *,
ul[role="listbox"] *,
li[role="option"] * {{
  background-color: #162A47 !important;
  background-image: none !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
}}
ul[role="listbox"] {{
  background-color: #162A47 !important;
  border: 1px solid #2C476B !important;
  border-radius: 10px !important;
}}
li[role="option"]:hover,
[data-testid="stSelectboxVirtualDropdown"] [data-item-hl]:hover {{
  background-color: #1E385B !important;
}}
li[role="option"][aria-selected="true"] {{ background-color: #1E4B85 !important; }}
section[data-testid="stSidebar"] .stButton > button {{
  border-color: #2C476B !important;
  background: #162A47 !important;
  color: #DCE8F7 !important;
  border-radius: 9px !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover {{ background: #1E385B !important; }}
section[data-testid="stSidebar"] .stButton > button[kind="primary"] {{
  background: #147BFF !important;
  border-color: #147BFF !important;
}}
section[data-testid="stSidebar"] hr {{ border-color: rgba(148, 177, 215, .16) !important; }}

.hero-shell {{
  /* Keep the research identity clear of the host application's top toolbar. */
  padding: 1.05rem 0 .2rem;
}}
.hero-kicker {{
  display: flex; align-items: center; gap: .45rem;
  color: #5B7191; font-size: .68rem; font-weight: 800;
  letter-spacing: .16em; text-transform: uppercase;
}}
.hero-dot {{ width: .45rem; height: .45rem; border-radius: 50%; background: #18A999; box-shadow: 0 0 0 4px rgba(24,169,153,.12); }}
.hero-title {{
  margin-top: .48rem; color: #0D1B2E; font-size: 2.35rem;
  letter-spacing: -.055em; line-height: 1.08; font-weight: 780;
}}
.hero-title span {{ color: #8CA0BA; font-weight: 400; padding: 0 .15rem; }}
.hero-subtitle {{ color: #61738D; font-size: .91rem; margin-top: .48rem; }}
.hero-sep {{ color: #AAB8C9; padding: 0 .38rem; }}
.landing-hero {{
  padding: 2.1rem 2.2rem 1.75rem;
  border: 1px solid #D7E2EE; border-radius: 18px;
  background: linear-gradient(130deg, #FFFFFF 0%, #F3F8FF 62%, #EDF8F7 100%);
  box-shadow: 0 14px 38px rgba(30,58,89,.07);
}}
.landing-title {{ margin-top: .5rem; color: #0D1B2E; font-size: 2.55rem; font-weight: 780; letter-spacing: -.06em; line-height: 1.08; }}
.landing-copy {{ max-width: 720px; margin-top: .7rem; color: #647891; font-size: 1rem; line-height: 1.7; }}
.flow-rail {{ display: flex; align-items: center; gap: .55rem; margin: 1.2rem .2rem 1.4rem; color: #5A718D; }}
.flow-rail div {{ display: flex; align-items: center; gap: .42rem; white-space: nowrap; font-size: .77rem; font-weight: 650; }}
.flow-rail b {{ display: grid; place-items: center; width: 1.55rem; height: 1.55rem; border-radius: 50%; background: #E2EEFF; color: #226AC0; font-size: .67rem; }}
.flow-rail i {{ height: 1px; flex: 1; background: #C9D8E8; }}
.case-index {{ color: #6E8AAD; font-size: .66rem; font-weight: 800; letter-spacing: .16em; margin-bottom: .35rem; }}
.case-index + h3 {{ margin-top: .15rem !important; }}
.statusbar {{
  background: rgba(255,255,255,.72); border: 1px solid #D8E2EE;
  border-radius: 12px; box-shadow: 0 8px 28px rgba(28,53,82,.05);
  padding: .62rem .8rem; margin: 1.1rem 0 1.45rem;
}}
.badge {{ padding: .3rem .68rem; font-size: .68rem; letter-spacing: .01em; }}
.snapshot {{ background: #E8F0FF; color: #2767BA; }}
.neutral {{ background: #F4F7FA; color: #64758C; border-color: #E0E7EF; }}
.verified {{ background: #E3F7F1; color: #087F6F; }}
.warning {{ background: #FFF4D9; color: #9A6500; }}
.blocked {{ background: #FFE7E5; color: #A4423A; }}

h1 {{ color: #0D1B2E !important; font-size: 2rem !important; letter-spacing: -.045em !important; }}
h2 {{ color: #11243B !important; font-size: 1.36rem !important; letter-spacing: -.025em !important; margin-top: 1.9rem !important; }}
h3 {{ color: #173250 !important; font-size: 1.03rem !important; }}
.subline {{ color: #6D7E94; }}
[data-testid="stMetric"] {{
  background: rgba(255,255,255,.94);
  border: 1px solid #DCE5EF;
  border-radius: 13px;
  box-shadow: 0 8px 24px rgba(30, 58, 89, .055);
  min-height: 116px;
  padding: .95rem 1rem;
}}
[data-testid="stMetricLabel"] {{ color: #73849A; font-size: .72rem; letter-spacing: .04em; text-transform: uppercase; }}
[data-testid="stMetricValue"] {{ color: #102642; font-size: 1.58rem; font-weight: 760; }}
div[data-testid="stVerticalBlockBorderWrapper"] > div,
.stContainer[data-testid="stContainer"]:has(> div[data-testid="stVerticalBlock"]),
.stContainer[border="True"] {{
  background: rgba(255,255,255,.9);
  border: 1px solid #DCE5EF !important;
  border-radius: 14px !important;
  box-shadow: 0 10px 30px rgba(30,58,89,.045) !important;
}}
.stDataFrame, [data-testid="stDataFrame"] {{ border: 1px solid #D7E2EE !important; border-radius: 12px !important; box-shadow: 0 8px 24px rgba(30,58,89,.04); }}
.stPlotlyChart {{ border: 1px solid #DCE5EF; border-radius: 14px; background: rgba(255,255,255,.76); padding: .35rem; box-shadow: 0 8px 24px rgba(30,58,89,.04); }}
.warn {{ background: #FFF7E3; border-left-color: #D89B26; color: #815600; }}
.blocked-card {{ background: #FFF0EE; border-left-color: #C65A50; color: #8E3831; }}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: 9px !important; font-size: .82rem !important;
  box-shadow: none !important; transition: all 140ms ease;
}}
.stButton > button:hover, .stDownloadButton > button:hover {{ transform: translateY(-1px); box-shadow: 0 5px 12px rgba(21,61,107,.12) !important; }}
.stDownloadButton > button {{ background: #102642 !important; color: #FFFFFF !important; border-color: #102642 !important; }}
.stDownloadButton > button *, .stFormSubmitButton > button[kind="primary"] *, .stButton > button[kind="primary"] * {{
  color: #FFFFFF !important;
}}
.stTextInput input, .stDateInput input, .stTextArea textarea, .stNumberInput input, [data-baseweb="input"] input {{
  background: #FFFFFF !important; border-color: #D5E0EC !important; border-radius: 9px !important;
}}
.trace {{ border-left-color: #8FB1D5; background: rgba(255,255,255,.55); border-radius: 0 10px 10px 0; margin: .35rem 0; }}
/* --- Declared last on purpose: sidebar select（研究快照）legibility. ---
   Streamlit paints widget chrome through emotion, and some builds set
   `-webkit-text-fill-color` — which wins over a plain `color`, so both are
   pinned; `background-image` is cleared in case a gradient fill is what left
   the value text unreadable on the navy shell.
   ⚠️ 这里必须是 `.stSelectbox`（1.61 的真实类名）。旧版写成
   `div[data-baseweb="select"]`，而 1.61 的 baseweb 已经不存在 —— 它是一条
   **永远不会命中**的规则，看着像"最后一层保险"，实际上什么都没做。 */
section[data-testid="stSidebar"] .stSelectbox,
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"],
section[data-testid="stSidebar"] .stSelectbox * {{
  background-color: #162A47 !important;
  background-image: none !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
}}
/* 浮层在 body 下，跨不进侧栏选择器，最后一层保险要单独再钉一次。 */
[data-testid="stSelectboxVirtualDropdown"],
[data-testid="stSelectboxVirtualDropdown"] * {{
  background-color: #162A47 !important;
  background-image: none !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  opacity: 1 !important;
}}
/* Wise-inspired research workspace: quiet white canvas, soft neutral cards,
   deep green typography, and one high-visibility lime action colour. */
:root {{
  --wise-ink: #163300;
  --wise-lime: #9FE870;
  --wise-lime-hover: #8DD95C;
  --wise-surface: #F4F6F1;
  --wise-line: #E3E8DF;
}}
.stApp {{
  background: #FFFFFF !important;
  background-image: none !important;
}}
.block-container {{ max-width: 1560px; padding: 2rem 3.4rem 4.5rem; }}
section[data-testid="stSidebar"] {{
  background: #FFFFFF !important;
  border-right: 1px solid var(--wise-line) !important;
  min-width: 282px;
  max-width: 306px;
}}
section[data-testid="stSidebar"] * {{ color: #244116 !important; }}
section[data-testid="stSidebar"] .brand {{ color: var(--wise-ink) !important; }}
section[data-testid="stSidebar"] .brand-sub,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
section[data-testid="stSidebar"] [data-testid="stExpander"] summary span {{ color: #71816A !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] {{ border-top-color: var(--wise-line) !important; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button {{
  color: #526049 !important;
  border-radius: 999px !important;
  min-height: 2.35rem;
  padding: .48rem .86rem !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button:hover {{
  background: #EEF3E9 !important;
  border-color: transparent !important;
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button[kind="primary"] {{
  background: #EAF1E5 !important;
  color: var(--wise-ink) !important;
  border-color: transparent !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] .stSelectbox,
section[data-testid="stSidebar"] .stSelectbox > div,
section[data-testid="stSidebar"] .stSelectbox [data-testid="stSelectbox"],
section[data-testid="stSidebar"] .stSelectbox * {{
  background-color: #F3F6F0 !important;
  color: var(--wise-ink) !important;
  -webkit-text-fill-color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .stSelectbox > div {{ border-color: var(--wise-line) !important; }}
[data-testid="stSelectboxVirtualDropdown"],
[data-testid="stSelectboxVirtualDropdown"] * {{
  background-color: #FFFFFF !important;
  color: var(--wise-ink) !important;
  -webkit-text-fill-color: var(--wise-ink) !important;
}}
h1, h2, h3, [data-testid="stMetricValue"] {{ color: var(--wise-ink) !important; }}
.hero-kicker {{ color: #547444; }}
.hero-dot {{ background: var(--wise-lime); box-shadow: 0 0 0 4px rgba(159,232,112,.22); }}
.hero-title {{ color: var(--wise-ink); font-size: 2.15rem; }}
.hero-subtitle, .subline {{ color: #72806B; }}
.statusbar {{ background: #FFFFFF; border-color: var(--wise-line); box-shadow: none; border-radius: 16px; }}
.snapshot {{ background: #EEF5E9; color: #386420; }}
.neutral {{ background: #F5F7F3; color: #66735F; border-color: var(--wise-line); }}
.verified {{ background: #E2F7D5; color: #285A14; }}
[data-testid="stMetric"],
div[data-testid="stVerticalBlockBorderWrapper"] > div,
.stContainer[data-testid="stContainer"]:has(> div[data-testid="stVerticalBlock"]),
.stContainer[border="True"] {{
  background: var(--wise-surface) !important;
  border-color: var(--wise-line) !important;
  box-shadow: none !important;
  border-radius: 18px !important;
}}
[data-testid="stMetric"] {{ min-height: 124px; padding: 1.1rem 1.2rem; }}
[data-testid="stMetricLabel"] {{ color: #74806D; font-size: .72rem; letter-spacing: .025em; text-transform: none; }}
[data-testid="stMetricValue"] {{ font-size: 1.72rem; font-weight: 760; }}
.stDataFrame, [data-testid="stDataFrame"], .stPlotlyChart {{
  border-color: var(--wise-line) !important;
  box-shadow: none !important;
  border-radius: 16px !important;
  background: #FFFFFF !important;
}}
.research-route-label {{
  margin: 1.1rem 0 .45rem;
  color: #66735F;
  font-size: .72rem;
  font-weight: 800;
  letter-spacing: .08em;
  text-transform: uppercase;
}}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  border-radius: 999px !important;
  border-color: #DCE5D6 !important;
  background: #FFFFFF !important;
  color: var(--wise-ink) !important;
  box-shadow: none !important;
}}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
.stDownloadButton > button {{
  background: var(--wise-lime) !important;
  border-color: var(--wise-lime) !important;
  color: var(--wise-ink) !important;
}}
.stButton > button[kind="primary"] *, .stFormSubmitButton > button[kind="primary"] *,
.stDownloadButton > button * {{ color: var(--wise-ink) !important; }}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  background: var(--wise-lime-hover) !important;
  border-color: var(--wise-lime-hover) !important;
  box-shadow: none !important;
  transform: none !important;
}}
.stTextInput input, .stDateInput input, .stTextArea textarea, .stNumberInput input,
[data-baseweb="input"] input {{ background: #FFFFFF !important; border-color: #DCE5D6 !important; }}
.stTextInput input:focus, .stDateInput input:focus, .stTextArea textarea:focus {{
  border-color: #6FAA42 !important; box-shadow: 0 0 0 3px rgba(159,232,112,.28) !important;
}}
/* Sidebar snapshot controls need a readable neutral field, independently of
   older utility-button rules above. */
section[data-testid="stSidebar"] .st-key-refresh_snapshot_catalog button {{
  background: #EEF3E9 !important;
  border-color: #DCE5D6 !important;
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_catalog button * {{
  color: var(--wise-ink) !important;
}}
/* The refresh control has a generated wrapper in some Streamlit builds, so
   retain this scoped fallback.  Navigation buttons below have stronger rules. */
section[data-testid="stSidebar"] .stButton > button {{
  background: #EEF3E9 !important;
  border-color: #DCE5D6 !important;
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .stButton > button * {{
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover {{
  background: #DDEDD5 !important;
  border-color: #C9DDBD !important;
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .stButton > button:hover * {{
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button {{
  background: transparent !important;
  border-color: transparent !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button:hover {{
  background: #EEF3E9 !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton > button[kind="primary"] {{
  background: #EAF1E5 !important;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] > div,
section[data-testid="stSidebar"] [data-testid="stSelectbox"] > div > div {{
  background: #FFFFFF !important;
  border-color: #C9D8C0 !important;
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] input {{
  background: #FFFFFF !important;
  color: var(--wise-ink) !important;
  -webkit-text-fill-color: var(--wise-ink) !important;
  font-size: .88rem !important;
  font-weight: 650 !important;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] svg {{
  fill: #5F7B50 !important;
}}
/* Sidebar control refinement: this is intentionally limited to the launch
   controls shown above navigation.  It restores one visual rhythm between
   labels, selects, checkboxes and actions without changing the app palette. */
section[data-testid="stSidebar"] .brand-block {{
  padding: .55rem .25rem 1.15rem;
  border-bottom: 1px solid var(--wise-line);
  margin-bottom: 1rem;
}}
section[data-testid="stSidebar"] .brand {{
  margin: .1rem 0 .9rem !important;
  font-size: 1.08rem !important;
  letter-spacing: .025em !important;
}}
section[data-testid="stSidebar"] .brand-sub {{
  margin: 0 !important;
  color: #71816A !important;
  font-size: .6rem !important;
  letter-spacing: .12em !important;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_catalog button {{
  min-height: 2.65rem !important;
  border-radius: 10px !important;
  background: #EEF3E9 !important;
  border-color: #D7E4CF !important;
  font-size: .88rem !important;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_catalog button:hover {{
  background: #E4EFDC !important;
  border-color: #C8DABC !important;
}}
section[data-testid="stSidebar"] [data-testid="stPopover"] > button {{
  min-height: 2.65rem !important;
  border-radius: 10px !important;
  background: #FFFFFF !important;
  border-color: #D7E4CF !important;
  color: var(--wise-ink) !important;
  font-size: .82rem !important;
}}
section[data-testid="stSidebar"] [data-testid="stPopover"] > button:hover {{
  background: #EEF3E9 !important;
  border-color: #C8DABC !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"]:has(.st-key-refresh_snapshot_catalog) {{
  margin: .9rem 0 1rem !important;
  border: 1px solid #DCE5D6 !important;
  border-radius: 14px !important;
  background: #FAFCF8 !important;
  overflow: hidden;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"]:has(.st-key-refresh_snapshot_catalog) summary {{
  padding: .82rem 1rem !important;
  background: #F2F6EF !important;
  font-size: .92rem !important;
  font-weight: 700 !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"]:has(.st-key-refresh_snapshot_catalog) [data-testid="stVerticalBlock"] {{
  gap: .5rem !important;
  padding: .95rem 1rem 1rem;
}}
section[data-testid="stSidebar"] .snapshot-section-title {{
  margin: .05rem 0 0;
  color: var(--wise-ink) !important;
  font-size: .8rem;
  font-weight: 750;
}}
section[data-testid="stSidebar"] .snapshot-section-copy {{
  margin: .05rem 0 .25rem;
  color: #71816A !important;
  font-size: .72rem;
  line-height: 1.45;
}}
section[data-testid="stSidebar"] .snapshot-online-heading {{
  margin-top: .7rem;
  padding-top: .8rem;
  border-top: 1px solid var(--wise-line);
}}
section[data-testid="stSidebar"] .snapshot-picker-heading {{
  margin-top: .7rem;
  padding-top: .8rem;
  border-top: 1px solid var(--wise-line);
}}
section[data-testid="stSidebar"] .snapshot-field-label {{
  margin: .35rem 0 -.15rem;
  color: #526049 !important;
  font-size: .73rem;
  font-weight: 700;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_symbol [data-testid="stSelectbox"] > div,
section[data-testid="stSidebar"] .st-key-snapshot_picker [data-testid="stSelectbox"] > div {{
  min-height: 2.7rem !important;
  border: 1px solid #C9DDBD !important;
  border-radius: 11px !important;
  background: #FFFFFF !important;
  box-shadow: none !important;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_symbol [data-testid="stSelectbox"] > div *,
section[data-testid="stSidebar"] .st-key-snapshot_picker [data-testid="stSelectbox"] > div * {{
  background: #FFFFFF !important;
  color: var(--wise-ink) !important;
  -webkit-text-fill-color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_symbol [data-testid="stSelectbox"] input,
section[data-testid="stSidebar"] .st-key-snapshot_picker [data-testid="stSelectbox"] input {{
  font-size: .88rem !important;
  font-weight: 700 !important;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_bars_only {{
  margin: -.1rem 0 .15rem;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_bars_only label {{
  align-items: flex-start !important;
  color: #526049 !important;
  font-size: .82rem !important;
  line-height: 1.45 !important;
}}
section[data-testid="stSidebar"] .st-key-online_refresh_bars_only [data-testid="stCheckbox"] {{
  margin-top: .1rem;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_online button {{
  min-height: 2.65rem !important;
  border-radius: 10px !important;
  background: var(--wise-lime) !important;
  border-color: var(--wise-lime) !important;
  color: var(--wise-ink) !important;
  font-size: .87rem !important;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_online button *,
section[data-testid="stSidebar"] .st-key-refresh_snapshot_online button:hover * {{
  color: var(--wise-ink) !important;
}}
section[data-testid="stSidebar"] .st-key-refresh_snapshot_online button:hover {{
  background: var(--wise-lime-hover) !important;
  border-color: var(--wise-lime-hover) !important;
}}
section[data-testid="stSidebar"] .st-key-snapshot_picker {{
  margin-top: .15rem;
  padding-top: 0;
  border-top: 0;
}}
section[data-testid="stSidebar"] .st-key-open_selected_snapshot button {{
  min-height: 2.45rem !important;
  margin: .1rem 0 0;
  border-radius: 10px !important;
  background: #FFFFFF !important;
  border-color: #C9DDBD !important;
  color: var(--wise-ink) !important;
  font-size: .82rem !important;
}}
section[data-testid="stSidebar"] .st-key-open_selected_snapshot button:hover {{
  background: #EEF3E9 !important;
  border-color: #BBD3AC !important;
}}
section[data-testid="stSidebar"] .st-key-open_selected_snapshot button *,
section[data-testid="stSidebar"] .st-key-open_selected_snapshot button:hover * {{
  color: var(--wise-ink) !important;
}}
/* Product landing page — intentionally scoped so research workbench screens
   keep their established visual system.  This layer changes hierarchy only:
   it deliberately reuses the existing white / deep-green / lime palette. */
.home-hero {{
  border: 1px solid var(--wise-line); border-radius: 22px; padding: 1.6rem 2rem 2rem;
  color: var(--wise-ink); background: #FFFFFF;
}}
.home-nav, .home-hero-grid {{ position: relative; z-index: 1; }}
.home-nav {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 2rem; border-bottom: 1px solid var(--wise-line); }}
.home-wordmark {{ font-size: 1.05rem; font-weight: 780; letter-spacing: -.015em; }}
.home-wordmark span {{ color: #6FAA42; margin-right: .25rem; }}
.home-nav-note {{ color: #71816A; font-size: .78rem; }}
.home-hero-grid {{ min-height: 0; display: block; padding-top: 2.25rem; }}
.home-eyebrow, .section-kicker {{ color: #547444; font-size: .68rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }}
.home-eyebrow {{ display: flex; align-items: center; gap: .5rem; }}
.home-eyebrow i {{ width: .5rem; height: .5rem; border-radius: 50%; background: var(--wise-lime); box-shadow: 0 0 0 4px rgba(159,232,112,.22); }}
.home-hero h1 {{ margin: .7rem 0 0 !important; color: var(--wise-ink) !important; font-size: clamp(2.35rem, 5vw, 3.85rem) !important; line-height: 1.08 !important; letter-spacing: -.06em !important; }}
.home-hero p {{ max-width: 650px; margin: 1rem 0 0; color: #71816A; font-size: 1.02rem; line-height: 1.75; }}
.value-pills {{ display: flex; flex-wrap: wrap; gap: .55rem; margin-top: 1.35rem; }}
.value-pills span {{ border: 1px solid #DCE5D6; border-radius: 999px; padding: .42rem .76rem; background: #F4F6F1; color: #526049; font-size: .8rem; font-weight: 650; }}
/* The diagram was decorative rather than actionable; the only first-screen
   action should be the CTA below the message. */
.home-orbit {{ display: none; }}
[class*="st-key-home_start_research"] button {{ margin-top: 1.05rem; min-width: 156px; background: var(--wise-lime) !important; border-color: var(--wise-lime) !important; color: var(--wise-ink) !important; }}
[class*="st-key-home_start_research"] button * {{ color: var(--wise-ink) !important; }}
.home-section {{ margin-top: 5.5rem; }}
.section-kicker {{ color: #547444; }}
.home-section h2, .architecture-section h2 {{ margin: .42rem 0 0 !important; color: var(--wise-ink) !important; font-size: clamp(1.85rem, 3vw, 2.6rem) !important; letter-spacing: -.045em !important; }}
.section-lead {{ max-width: 650px; margin: .7rem 0 0; color: #71816A; font-size: 1rem; line-height: 1.7; }}
.problem-grid, .feature-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1rem; margin-top: 1.7rem; }}
.problem-grid article {{ min-height: 174px; padding: 1.25rem; border: 1px solid var(--wise-line); border-radius: 16px; background: #FFF; }}
.problem-grid article b {{ display: grid; place-items: center; width: 1.7rem; height: 1.7rem; border-radius: 50%; background: #FFF0F0; color: #C95D67; font-size: .9rem; }}
.problem-grid h3, .feature-grid h3, .case-card h3 {{ margin: 1rem 0 .35rem !important; color: var(--wise-ink) !important; font-size: 1.03rem !important; }}
.problem-grid p, .feature-grid p, .case-card p {{ margin: 0; color: #71816A; font-size: .88rem; line-height: 1.65; }}
.workflow-section {{ padding: 2.1rem; border: 1px solid var(--wise-line); border-radius: 20px; background: var(--wise-surface); }}
.workflow-rail {{ display: flex; align-items: stretch; gap: .65rem; margin-top: 1.75rem; }}
.workflow-rail div {{ flex: 1; min-width: 0; }}
.workflow-rail em {{ display: block; color: #547444; font-size: .7rem; font-style: normal; font-weight: 800; letter-spacing: .08em; }}
.workflow-rail strong {{ display: block; margin-top: .4rem; color: var(--wise-ink); font-size: .97rem; }}
.workflow-rail span {{ display: block; margin-top: .3rem; color: #71816A; font-size: .76rem; }}
.workflow-rail > i {{ width: 40px; align-self: 27px; height: 1px; background: #C9DDBD; }}
.feature-grid article {{ min-height: 226px; padding: 1.35rem; border: 1px solid var(--wise-line); border-radius: 17px; background: #FFFFFF; box-shadow: none; }}
.feature-grid article:nth-child(2) {{ background: var(--wise-surface); border-color: var(--wise-line); }}
.feature-grid article:nth-child(2) h3 {{ color: var(--wise-ink) !important; }}
.feature-grid article:nth-child(2) b {{ color: #547444 !important; }}
.feature-grid article:nth-child(2) p {{ color: #71816A; }}
.feature-number {{ color: #547444; font-size: .7rem; font-weight: 800; letter-spacing: .1em; }}
.feature-grid b {{ color: #547444; font-size: .78rem; font-weight: 650; }}
.case-section {{ margin-bottom: 1.6rem; }}
.featured-case {{ margin-top: 2.6rem; }}
.case-card {{ min-height: 245px; padding: 1.45rem; border: 1px solid var(--wise-line); border-radius: 18px 18px 0 0; background: #FFF; }}
.case-card.case-pass {{ border-top: 3px solid #43A980; }} .case-card.case-block {{ border-top: 3px solid #D68471; }}
.case-top {{ display: flex; justify-content: space-between; gap: 1rem; color: #71816A; font-size: .73rem; font-weight: 700; }}
.case-top b {{ border-radius: 999px; padding: .32rem .55rem; font-size: .71rem; }}
.case-pass .case-top b {{ background: #E7F6EE; color: #207653; }} .case-block .case-top b {{ background: #FDF0EC; color: #B55F4D; }}
.case-card ul {{ display: flex; flex-wrap: wrap; gap: .5rem; padding: 0; margin: 1.1rem 0 0; list-style: none; }}
.case-card li {{ border-radius: 6px; padding: .36rem .48rem; background: #F4F6F1; color: #526049; font-size: .73rem; }}
.case-card + div[data-testid="stExpander"] {{ margin-top: 0 !important; border: 1px solid var(--wise-line); border-top: 0; border-radius: 0 0 18px 18px; background: #FFF; }}
.case-card + div[data-testid="stExpander"] summary {{ color: #547444; font-size: .82rem; font-weight: 650; }}
.architecture-section {{ margin-top: 5.5rem; padding: 2.25rem; border: 1px solid var(--wise-line); border-radius: 22px; background: var(--wise-surface); }}
.architecture-section .section-kicker {{ color: #547444; }} .architecture-section h2 {{ color: var(--wise-ink) !important; }} .architecture-section p {{ margin: .55rem 0 0; color: #71816A; }}
.architecture-flow {{ display: flex; flex-wrap: wrap; align-items: center; gap: .65rem; margin-top: 1.5rem; }}
.architecture-flow span {{ border: 1px solid #DCE5D6; border-radius: 9px; padding: .65rem .8rem; background: #FFFFFF; color: var(--wise-ink); font: 600 .78rem ui-monospace, SFMono-Regular, Menlo, monospace; }}
.architecture-flow i {{ color: #6FAA42; font-style: normal; }}
.agent-context {{ margin: .1rem 0 1rem; padding: 1rem 1.1rem; border: 1px solid var(--wise-line); border-radius: 14px; background: #FBFCF9; }}
.agent-context-head {{ display: flex; align-items: center; justify-content: space-between; gap: .8rem; margin-bottom: .8rem; color: var(--wise-ink); font-size: .86rem; font-weight: 750; }}
.agent-context-head span {{ border-radius: 999px; padding: .24rem .5rem; background: #EAF4E3; color: #547444; font-size: .68rem; font-weight: 700; }}
.agent-context-grid {{ display: grid; grid-template-columns: minmax(180px, 1.5fr) minmax(130px, 1fr) minmax(145px, 1fr); gap: 1rem; }}
.agent-context-item {{ min-width: 0; }}
.agent-context-item span {{ display: block; color: #71816A; font-size: .7rem; font-weight: 700; letter-spacing: .04em; }}
.agent-context-item strong {{ display: block; overflow: hidden; margin-top: .25rem; color: var(--wise-ink); font-size: .95rem; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }}
.agent-context-item em {{ color: #547444; font-style: normal; font-weight: 650; }}
.st-key-account_topbar {{ position: fixed !important; top: .7rem; right: 17.8rem; z-index: 2147483647 !important; width: 4.8rem; }}
.st-key-account_topbar [data-testid="stPopover"] > button {{ min-height: 2.25rem; width: 100%; border: 1px solid #D7E4CF !important; border-radius: 10px !important; background: #FFFFFF !important; color: var(--wise-ink) !important; box-shadow: 0 3px 12px rgba(20, 42, 30, .08); font-size: .82rem; font-weight: 700; }}
.st-key-account_topbar [data-testid="stPopover"] > button:hover {{ background: #F2F6EF !important; border-color: #BFD6B3 !important; }}
@media (max-width: 700px) {{ .agent-context-grid {{ grid-template-columns: 1fr; gap: .65rem; }} .agent-context-head {{ align-items: flex-start; flex-direction: column; }} }}
@media (max-width: 900px) {{
  .block-container {{ padding: 1.15rem 1rem 3rem; }}
  .home-hero {{ padding: 1.25rem; }} .home-hero-grid {{ grid-template-columns: 1fr; min-height: 0; }} .home-orbit {{ display: none; }}
  .home-nav {{ padding-bottom: 2rem; }} .problem-grid, .feature-grid {{ grid-template-columns: 1fr; }} .workflow-rail {{ flex-direction: column; gap: .85rem; }} .workflow-rail > i {{ width: 1px; height: 20px; margin-left: .5rem; }}
  .home-section {{ margin-top: 3.5rem; }} .workflow-section, .architecture-section {{ padding: 1.35rem; }}
}}
</style>""",
        unsafe_allow_html=True,
    )


def research_quality_copy(validation: dict, readiness: dict) -> tuple[str, str, str, str]:
    """Translate stored validation states into user-facing research guidance.

    This is presentation only: it never changes the validator result or
    promotes a blocked conclusion.  Raw statuses remain available in the
    advanced details for professional review.
    """
    checks = {str(check.get("code")): check for check in validation.get("checks", [])}
    if validation.get("status") == "blocked" or readiness.get("status") == "blocked":
        reason = "关键数据或计算依据不足，系统已限制相关结论。"
        if checks.get("key_metrics", {}).get("status") == "blocked":
            reason = "关键估值输入不完整，系统不会把缺失信息补成结论。"
        elif checks.get("ledger", {}).get("status") == "blocked":
            reason = "部分数据依据或计算依赖未通过检查，相关结论已被限制。"
        return "数据不足", "red", reason, "不建议将本报告作为完整研究依据。"
    if validation.get("status") == "warning" or readiness.get("status") == "limited":
        if checks.get("snapshot_freshness", {}).get("status") == "warning":
            reason = "当前数据版本较旧，部分指标可能无法反映最新情况。"
        elif checks.get("annual_financial_coverage", {}).get("status") == "warning":
            reason = "部分已披露财务数据覆盖不足，报告的适用范围有限。"
        elif checks.get("peer_coverage", {}).get("status") == "warning":
            reason = "同行数据覆盖有限，相关比较结论需要谨慎参考。"
        else:
            reason = "部分研究条件需要关注，系统已对受影响的结论进行限制。"
        return "数据需要关注", "orange", reason, "可阅读已验证内容，但不宜据此形成强结论。"
    return "数据充分", "green", "数据依据、关键输入和当前研究范围均已完成检查。", "可作为当前数据版本下的研究参考，不构成投资建议。"


def validation_check_copy(check: dict) -> str:
    """Explain a validator result without surfacing implementation tokens."""
    code = str(check.get("code", ""))
    status = str(check.get("status", "unknown"))
    messages = {
        "ledger": "数据依据与计算关系已完成一致性核对。",
        "snapshot_freshness": "当前数据版本较旧，部分指标可能无法反映最新情况。",
        "annual_financial_coverage": "部分已披露财务数据覆盖不足，报告的适用范围有限。",
        "key_metrics": "关键估值输入不足，相关结论已被系统限制。",
        "peer_coverage": "同行数据覆盖有限，相关比较结论需要谨慎参考。",
    }
    if status == "pass":
        return messages.get(code, "该项数据检查已完成。")
    return messages.get(code, "该项数据检查需要关注，请查看处理建议。")


def header(snapshot: dict, metadata: dict) -> None:
    profile = snapshot["data"]["profile"]
    source_metadata = snapshot.get("source_metadata") or {}
    raw_dates = [
        str(bar["trade_date"])
        for bar in snapshot.get("data", {}).get("bars", [])
        if bar.get("adjustment") == "RAW"
        and bar.get("trade_date")
        and str(bar["trade_date"]) <= str(snapshot.get("research_as_of", ""))[:10]
    ]
    origin_label = "联网更新版本" if source_metadata.get("refresh") else "本地基准版本"
    latest_raw_close_date = max(raw_dates) if raw_dates else "未覆盖"
    validation = cached_research_validation(snapshot["snapshot_id"])
    readiness = cached_research_readiness(snapshot["snapshot_id"])
    quality_label, quality_color, _, _ = research_quality_copy(validation, readiness)
    quality_class = {"green": "verified", "orange": "warning", "red": "blocked"}[quality_color]
    left, right = st.columns([7, 2], vertical_alignment="center")
    with left:
        st.markdown(
            f'''<div class="hero-shell">
                <div class="hero-kicker"><span class="hero-dot"></span> FINTRACE / CN · AI 金融研究</div>
                <div class="hero-title">{profile['name']} <span>·</span> {snapshot['symbol']}</div>
                <div class="hero-subtitle">{profile["industry_name"]} <span class="hero-sep">/</span> {profile["currency"]} <span class="hero-sep">/</span> 研究截至 {_display_datetime(snapshot["research_as_of"])}</div>
            </div>''',
            unsafe_allow_html=True,
        )
    with right:
        st.download_button("导出研究信息", json.dumps(metadata, ensure_ascii=False, indent=2), f"{snapshot['symbol']}-research-metadata.json", "application/json", width="stretch")
    st.markdown(
        f'''<div class="statusbar"><span class="badge {quality_class}">● 研究质量：{quality_label}</span>
        <span class="badge neutral">{origin_label} · 收盘数据截至 {latest_raw_close_date}</span></div>''',
        unsafe_allow_html=True,
    )


def security_status(snapshot_id: str) -> None:
    """Display stored trading/security flags without inferring a trade conclusion."""
    flags = cached_research_market(snapshot_id).get("security_flags", {})
    labels = []
    if flags.get("beijing_exchange"):
        labels.append("北交所证券")
    if flags.get("special_treatment_name_flag"):
        labels.append("证券名称含 ST 标识")
    trading_status = flags.get("latest_trading_status")
    if trading_status and str(trading_status).upper() not in {"NORMAL", "TRADING"}:
        labels.append(f"最新交易状态：{trading_status}")
    if labels:
        st.warning(
            " · ".join(labels) + "。状态仅转述固定快照中的证券标识/交易字段，"
            "不推断复牌、价格或交易结论。",
            icon=":material/warning:",
        )


def overview(snapshot: dict, metadata: dict) -> None:
    data, metrics = snapshot["data"], metadata["key_metrics"]
    bars = pd.DataFrame(cached_research_market(snapshot["snapshot_id"])["bars"]).sort_values("trade_date")
    financial_result = cached_research_financials(snapshot["snapshot_id"])
    statements = pd.DataFrame(financial_result["statements"])
    statements = statements[statements["available_as_of"]].copy()
    fy = _annual_income(statements).tail(5)
    validation = cached_research_validation(snapshot["snapshot_id"])
    validation_class = {"pass": "verified", "warning": "warning", "blocked": "blocked"}[validation["status"]]
    security_status(snapshot["snapshot_id"])
    # Freshness remains determined from the snapshot's own RAW close date; the
    # presentation below deliberately explains it without exposing raw tokens.
    freshness = service.snapshot_freshness(snapshot["snapshot_id"])
    readiness = cached_research_readiness(snapshot["snapshot_id"])
    peer = cached_research_valuation(snapshot["snapshot_id"])
    evidence_result = cached_research_evidence(snapshot["snapshot_id"])
    evidence_count = len(evidence_result.get("records", []))
    validation_checks = validation.get("checks", [])
    passed_count = sum(check.get("status") == "pass" for check in validation_checks)
    attention_count = sum(check.get("status") != "pass" for check in validation_checks)
    quality_label, quality_color, quality_reason, report_guidance = research_quality_copy(validation, readiness)

    with st.container(border=True):
        quality_left, quality_right = st.columns([1, 3], vertical_alignment="center")
        with quality_left:
            st.markdown("**AI 研究质量**")
            st.badge(quality_label, color=quality_color)
        with quality_right:
            st.markdown(f"**{report_guidance}**")
            st.caption(f"原因：{quality_reason}")

    st.markdown("### 为什么相信这份报告？")
    trust_columns = st.columns(4)
    trust_columns[0].metric("数据依据", f"{evidence_count} 项")
    trust_columns[0].caption("本次研究引用的数据依据")
    trust_columns[1].metric("数据检查", f"{passed_count} 项已完成")
    trust_columns[1].caption("已完成的关键数据与计算检查")
    trust_columns[2].metric("需要关注", f"{attention_count} 项")
    trust_columns[2].caption("系统已标记并限制受影响结论")
    trust_columns[3].metric("数据更新时间", freshness.get("latest_trade_date") or "未覆盖")
    trust_columns[3].caption("当前版本纳入的最新收盘数据")

    with st.expander("高级验证详情", expanded=False):
        st.caption("以下信息用于复核完整验证链路；不会改变本页展示的研究结论。")
        details_left, details_right = st.columns(2)
        with details_left:
            st.markdown("**数据版本与来源**")
            st.write(f"数据版本：`{snapshot['snapshot_id']}`")
            st.write(f"数据提供方：`{snapshot['provider']}`")
            st.write(f"研究截止：`{_display_datetime(snapshot['research_as_of'])}`")
            st.write("价格口径：`RAW / CNY`")
        with details_right:
            st.markdown("**Evidence Ledger / Validator**")
            st.write(f"Evidence Ledger：`{evidence_count}` 条记录")
            advanced_rows = [
                {"检查项": check.get("label", check.get("code", "检查项")), "状态": {"pass": "通过", "warning": "需要关注", "blocked": "数据不足"}.get(check.get("status"), "未测量"), "说明": validation_check_copy(check)}
                for check in validation_checks
            ]
            st.dataframe(pd.DataFrame(advanced_rows), width="stretch", hide_index=True)
        st.button(
            "查看 AI 分析过程",
            key=f"overview_trace_link_{snapshot['snapshot_id']}",
            icon=":material/account_tree:",
            on_click=_select_page,
            args=("Agent 执行轨迹",),
        )

    report_ready = bool(_report_candidates(snapshot))
    readiness_checks = readiness.get("checks", {})
    delivery_rows = [
        (
            "核心数据",
            readiness_checks.get("key_metrics", {}).get("status", "unknown"),
            "关键研究输入已完成核验" if readiness_checks.get("key_metrics", {}).get("status") == "pass" else "关键研究输入仍需补充",
            "证据与校验",
        ),
        (
            "数据检查",
            validation.get("status", "unknown"),
            f"本次研究使用 {evidence_count} 项数据依据",
            "证据与校验",
        ),
        (
            "同行估值",
            "pass" if peer.get("available") and peer.get("conclusion_allowed") else "warning" if peer.get("available") else "blocked",
            f"已纳入 {peer.get('peer_count', 0)} 家同行" if peer.get("available") else "暂无兼容的本地同行快照",
            "同行估值",
        ),
        (
            "研究报告",
            "pass" if report_ready else "warning",
            "已发现本地版本化报告" if report_ready else "尚未生成报告工件",
            "研究报告",
        ),
    ]
    status_labels = {
        "pass": ("通过", "green"), "ready": ("通过", "green"),
        "warning": ("需要关注", "orange"), "limited": ("需要关注", "orange"),
        "blocked": ("数据不足", "red"), "unknown": ("未测量", "gray"),
    }
    st.markdown("### 研究内容状态")
    delivery_columns = st.columns(4)
    for column, (label, status, detail, target_page) in zip(delivery_columns, delivery_rows):
        status_label, status_color = status_labels.get(status, ("未测量", "gray"))
        with column:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.badge(status_label, color=status_color)
                st.caption(detail)
                st.button(
                    "打开详情",
                    key=f"delivery_{snapshot['snapshot_id']}_{label}",
                    on_click=_select_page,
                    args=(target_page,),
                    width="stretch",
                )
    st.caption("状态只反映当前选中快照的覆盖、校验和交付工件；页面不会用缺失数据补全结论。")
    st.markdown("### 关键研究摘要")
    a, b, c, d = st.columns(4)
    price = metrics.get("price")
    market_cap = metrics.get("market_cap")
    ttm_profit = metrics.get("ttm_net_profit")
    pe_ttm = metrics.get("pe_ttm")
    a.metric("RAW 收盘价", f"¥ {price:,.2f}" if price is not None else "未覆盖", help="研究时点收盘价；未采用前复权或后复权")
    b.metric("市值", cn_money(market_cap) if market_cap is not None else "未覆盖", help="总股本 × RAW 收盘价")
    c.metric("TTM P/E", f"{pe_ttm:.2f}×" if pe_ttm is not None and validation["conclusion_allowed"] else "已阻断", help="市值 ÷ TTM 归母净利润；校验阻断时不展示")
    d.metric("TTM 归母净利润", cn_money(ttm_profit) if ttm_profit is not None else "未覆盖", help="后端确定性计算结果")
    evidence_actions = [
        (a, "收盘价证据", metadata["evidence_ids"].get("price")),
        (b, "市值证据", metadata["evidence_ids"].get("market_cap")),
        (c, "P/E 证据", metadata["evidence_ids"].get("pe_ttm")),
        (d, "TTM 净利润证据", metadata["evidence_ids"].get("ttm_net_profit")),
    ]
    for container, label, item_id in evidence_actions:
        with container:
            st.button(
                label, key=f"overview_{label}_{snapshot['snapshot_id']}",
                icon=":material/account_tree:", disabled=item_id is None,
                on_click=open_evidence if item_id else None, args=(item_id,) if item_id else (),
                width="stretch",
            )
    price_id = metadata["evidence_ids"]["price"] or "未覆盖"
    ttm_id = metadata["evidence_ids"]["ttm_net_profit"] or "未覆盖"
    st.caption(f"关键数值可追溯：收盘价 {price_id} · 市值 {metadata['evidence_ids'].get('market_cap') or '未覆盖'} · P/E {metadata['evidence_ids'].get('pe_ttm') or '未覆盖'} · TTM {ttm_id}")
    if validation["status"] == "blocked":
        st.error("校验已阻断：关键输入或证据链不完整，因此不展示确定性估值结论。请前往“证据与校验”查看修复建议。")
    elif validation["status"] == "warning":
        st.warning("校验存在警告：当前内容可供基础阅读，但同业覆盖或数据时效不足，不应据此形成强结论。")
    st.markdown("### 研究解读")
    st.markdown(f"**报告参考建议：{report_guidance}**")
    with st.expander("查看数据覆盖与限制", expanded=readiness["status"] != "ready"):
        labels = {"snapshot_freshness": "快照时效", "annual_financial_coverage": "年度财报覆盖", "key_metrics": "关键估值输入", "peer_coverage": "同业样本"}
        display_status = {"pass": "通过", "warning": "需要关注", "blocked": "数据不足"}
        rows = [{"检查项": labels[name], "状态": display_status.get(check["status"], "未测量"), "说明": validation_check_copy({"code": name, **check})} for name, check in readiness["checks"].items()]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption("需要关注表示可继续阅读已验证内容；数据不足表示相关结论会被系统限制。")
    peer = cached_research_valuation(snapshot["snapshot_id"])
    st.markdown("### 同行估值摘要")
    if peer.get("method_boundary") == "financial_institution_pe_pb_only":
        st.caption("金融机构仅展示 PE/PB；通用企业 PS 等方法已由服务层隐藏。")
    if not peer.get("available"):
        st.info(f"同行估值暂不可用：{_peer_unavailable_message(peer)}")
        if peer.get("remediation"):
            st.caption(peer["remediation"])
    else:
        st.caption(
            f"同一研究截止时间纳入 {peer.get('peer_count', 0)} 家同行 · "
            f"价格口径 {peer.get('price_basis', 'RAW')} · 期间口径 {peer.get('period_basis', '—')}"
        )
        overview_rows = []
        for method in ("PE", "PB", "PS"):
            multiple = peer.get("multiples", {}).get(method, {})
            interval = peer.get("valuation_ranges", {}).get(method, {})
            target = peer.get("target_multiples", {}).get(method)
            if not any(value is not None for value in (target, multiple.get("median"), interval.get("p25"))):
                continue
            overview_rows.append({
                "方法": method,
                "目标公司": target,
                "同行中位数": multiple.get("median"),
                "P25–P75": (
                    f"{interval['p25']:.2f}–{interval['p75']:.2f}"
                    if interval.get("p25") is not None and interval.get("p75") is not None else "未覆盖"
                ),
                "隐含价格": (
                    multiple.get("implied_price")
                    if peer.get("conclusion_allowed") else None
                ),
            })
        if overview_rows:
            st.dataframe(
                pd.DataFrame(overview_rows), width="stretch", hide_index=True,
                column_config={
                    "目标公司": st.column_config.NumberColumn("目标公司（倍）", format="%.2f×"),
                    "同行中位数": st.column_config.NumberColumn("同行中位数（倍）", format="%.2f×"),
                    "隐含价格": st.column_config.NumberColumn("隐含价格（元）", format="¥ %.2f"),
                },
            )
        if peer.get("conclusion_allowed"):
            st.caption("隐含价格仅是服务层按同行倍数换算的确定性结果，不构成投资建议。")
        else:
            st.warning("Validator 已阻断隐含价格展示；请前往“证据与校验”查看原因。")
    st.button(
        "查看完整同行估值",
        key=f"overview_peer_link_{snapshot['snapshot_id']}",
        on_click=_select_page,
        args=("同行估值",),
        icon=":material/arrow_forward:",
    )
    insights = cached_research_overview_insights(snapshot["snapshot_id"])
    revenue_change = insights["revenue_change_percent"]
    profit_change = insights["net_profit_change_percent"]
    summary_left, summary_middle, summary_right = st.columns(3)
    with summary_left:
        if revenue_change is None or profit_change is None:
            st.info("业绩趋势：可比年度数据不足，暂不作趋势判断。")
        else:
            state = {"improving": "改善", "under_pressure": "承压", "diverging": "分化"}[insights["performance_status"]]
            st.markdown(f"**业绩状态：{state}**\n\n最近可比年度营收变化 **{revenue_change:+.1f}%**，归母净利润变化 **{profit_change:+.1f}%**。这是已披露业绩变化，不是预测。")
    with summary_middle:
        pe = metrics.get("pe_ttm")
        if not validation["conclusion_allowed"]:
            st.error("估值解读已被 Validator 阻断。")
        elif pe is None or pe <= 0:
            st.info("估值解读：盈利或估值输入不足，PE 暂不可用。")
        else:
            st.markdown(f"**估值读法：TTM PE {pe:.1f} 倍**\n\n市场为最近十二个月每 1 元利润支付约 {pe:.1f} 元。没有完整同业和历史分位数据，不能仅凭此判断贵或便宜。")
    with summary_right:
        if insights["latest_period"] is None:
            st.info("下一次关注：缺少完整年度财报，建议先补齐数据。")
        else:
            st.markdown(f"**下一次应验证什么**\n\n关注下一份财报的营收、归母净利润是否延续 {insights['latest_period']} 的趋势；若背离，再查看成本、毛利率和一次性损益。")
    st.caption(f"摘要仅使用截至 {snapshot['research_as_of'][:10]} 的快照和已披露财报自动生成，不构成买卖建议。")
    left, right = st.columns([1.65, 1])
    with left:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=bars.trade_date, y=bars.close, mode="lines+markers", customdata=bars[["evidence_id", "price_basis", "provider"]], hovertemplate="日期: %{x}<br>收盘价: %{y}<br>Evidence ID: %{customdata[0]}<br>价格口径: %{customdata[1]}<br>来源: %{customdata[2]}<extra></extra>", line=dict(color="#0066cc", width=2.5), fill="tozeroy", fillcolor="rgba(0, 102, 204,.08)", name="RAW 收盘价"))
        fig.add_vline(x=snapshot["research_as_of"][:10], line_dash="dot", line_color="#8b9bb0")
        fig.update_layout(title="价格区间（RAW）", height=310, margin=dict(l=15,r=15,t=45,b=15), paper_bgcolor="#fff", plot_bgcolor="#fff", yaxis_title="CNY / 股", xaxis=dict(showgrid=False), yaxis=dict(gridcolor="#F0F0F0"), showlegend=False)
        event = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points", key=f"overview_market_{snapshot['snapshot_id']}")
        if event.selection.points:
            selected_id = event.selection.points[0].get("customdata", [None])[0]
            if selected_id:
                st.session_state["selected_evidence_id"] = selected_id
    with right:
        validation_text = {"pass": "证据与关键输入通过", "warning": "存在非阻断警告", "blocked": "关键结论已阻断"}[validation["status"]]
        st.markdown(f"<div class='panel'><b>可信度状态</b><br><br><span class='badge {validation_class}'>● {validation_text}</span><p class='subline'>状态来自后端 Evidence Ledger 与研究门控；计算仅在服务层完成。</p><div class='card-note'>只读 · 无前端派生计算</div></div>", unsafe_allow_html=True)
        st.markdown("<div class='warn'><b>非实时行情</b><br>本页仅展示固定版本快照，不构成交易或投资建议。</div>", unsafe_allow_html=True)
    chart_financials = fy.dropna(subset=["revenue_billion", "net_profit_billion"])
    chart_financials = chart_financials.copy()
    chart_financials["revenue_evidence_id"] = chart_financials["evidence_ids"].apply(lambda ids: ids.get("revenue"))
    chart_financials["profit_evidence_id"] = chart_financials["evidence_ids"].apply(lambda ids: ids.get("net_profit"))
    revenue_custom = list(zip(chart_financials["revenue_evidence_id"], chart_financials["published_at"], [financial_result["provider"]] * len(chart_financials)))
    profit_custom = list(zip(chart_financials["profit_evidence_id"], chart_financials["published_at"], [financial_result["provider"]] * len(chart_financials)))
    fig2 = go.Figure()
    revenue_text = chart_financials["revenue_billion"].map(lambda value: f"{value:,.1f}")
    fig2.add_bar(
        x=chart_financials.fiscal_period, y=chart_financials["revenue_billion"],
        width=0.42, text=revenue_text, texttemplate="%{text}", textposition="outside",
        cliponaxis=False, customdata=revenue_custom,
        hovertemplate="报告期: %{x}<br>营业收入: %{y:,.1f} 亿元<br>Evidence ID: %{customdata[0]}<br>披露时间: %{customdata[1]}<br>来源: %{customdata[2]}<extra></extra>",
        name="营业收入（蓝色柱）", marker_color="#6d8fd5",
    )
    fig2.add_scatter(
        x=chart_financials.fiscal_period, y=chart_financials["net_profit_billion"],
        customdata=profit_custom,
        hovertemplate="报告期: %{x}<br>归母净利润: %{y:,.1f} 亿元<br>Evidence ID: %{customdata[0]}<br>披露时间: %{customdata[1]}<br>来源: %{customdata[2]}<extra></extra>",
        name="归母净利润（绿色线）", yaxis="y2", mode="lines+markers",
        line=dict(color="#0F766E", width=2.5), marker=dict(size=8),
    )
    fig2.update_layout(
        height=360,
        margin=dict(l=15, r=25, t=55, b=20), paper_bgcolor="#fff", plot_bgcolor="#fff",
        legend=dict(orientation="h", x=0, y=1.12), bargap=0.45,
        yaxis=dict(title="营业收入（亿元）", gridcolor="#F0F0F0", rangemode="tozero"),
        yaxis2=dict(title="归母净利润（亿元）", overlaying="y", side="right", showgrid=False),
    )
    st.caption("年度业绩趋势（单位：亿元）：蓝色柱表示营业收入，绿色折线表示归母净利润；分别读取左、右纵轴。柱顶显示收入数值，悬停绿色点可查看净利润及其证据来源。")
    event2 = st.plotly_chart(fig2, width="stretch", on_select="rerun", selection_mode="points", key=f"overview_financial_{snapshot['snapshot_id']}")
    if event2.selection.points:
        selected_id = event2.selection.points[0].get("customdata", [None])[0]
        if selected_id:
            st.session_state["selected_evidence_id"] = selected_id


def market(snapshot: dict) -> None:
    market_result = cached_research_market(snapshot["snapshot_id"])
    st.markdown("### 市场与行情")
    coverage = market_result["adjustment_coverage"]
    mode = st.segmented_control("复权口径", ["RAW", "QFQ", "HFQ"], default="RAW")
    if not coverage.get(mode, False):
        st.info(f"当前固定快照未覆盖 {mode} 口径；未以任何方式补值。")
        mode = "RAW"
    market_result = cached_research_market(snapshot["snapshot_id"], mode)
    bars = pd.DataFrame(market_result["bars"]).sort_values("trade_date")
    st.caption(" · ".join(f"{basis}：{'已覆盖' if covered else '未覆盖'}" for basis, covered in coverage.items()))
    if bars.empty:
        st.warning(
            f"当前快照在研究截止日 {market_result['research_as_of'][:10]} 前没有可用的 {mode} 行情数据；"
            "页面不会补零或用其他口径替代。"
        )
        return
    latest = bars.iloc[-1]
    market_summary = st.columns(4)
    market_summary[0].metric("最近收盘价", f"¥ {latest.close:,.2f}")
    market_summary[1].metric("最近交易日", str(latest.trade_date))
    market_summary[2].metric("行情记录", f"{len(bars)} 条")
    market_summary[3].metric("研究截止日", market_result["research_as_of"][:10])
    st.caption(
        f"当前展示口径：{mode} · 单位：{market_result['unit']} · 成交量：{market_result['volume_unit']} · "
        f"Provider：{market_result['provider']}"
    )
    hover = [
        f"日期: {row.trade_date}<br>开盘: {row.open:.2f}<br>最高: {row.high:.2f}"
        f"<br>最低: {row.low:.2f}<br>收盘: {row.close:.2f}"
        f"<br>Evidence ID: {row.evidence_id}<br>价格口径: {row.price_basis}<br>来源: {row.provider}"
        for row in bars.itertuples()
    ]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(
        x=bars.trade_date, open=bars.open, high=bars.high, low=bars.low, close=bars.close,
        customdata=bars[["evidence_id", "price_basis", "provider"]],
        hovertext=hover, hoverinfo="text",
        increasing_line_color="#c0392b", decreasing_line_color="#1c9a82", name="RAW",
    ), row=1, col=1)
    fig.add_trace(go.Bar(x=bars.trade_date, y=bars.volume, marker_color="#D2D2D7", name="成交量（手）", hovertemplate="日期: %{x}<br>成交量: %{y:,.0f} 手<extra></extra>"), row=2, col=1)
    fig.add_vline(x=snapshot["research_as_of"][:10], line_dash="dot", line_color="#8b9bb0", annotation_text="研究时点", row=1, col=1)
    fig.update_layout(height=560, title=f"日线与成交量 / {mode}", xaxis_rangeslider_visible=False, paper_bgcolor="#fff", plot_bgcolor="#fff", margin=dict(l=15,r=15,t=45,b=15), showlegend=False)
    fig.update_yaxes(title_text="CNY / 股", row=1, col=1)
    fig.update_yaxes(title_text="手", row=2, col=1)
    event = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points", key=f"market_chart_{snapshot['snapshot_id']}")
    if event.selection.points:
        point = event.selection.points[0]
        selected_id = point.get("customdata", [None])[0]
        if selected_id:
            st.session_state["selected_evidence_id"] = selected_id
    st.dataframe(bars.tail(12).sort_values("trade_date", ascending=False)[["trade_date","open","high","low","close","volume"]], width="stretch", hide_index=True, column_config={"trade_date":"交易日","close":st.column_config.NumberColumn("收盘价", format="¥ %.2f"), "volume":"成交量（手）"})
    st.caption(f"悬浮数据与表格均来自快照 bars[]；最近收盘价证据：{latest.evidence_id}。点击数据点后，Evidence ID 会在跨页面会话中保留。")


def _financial_value(values: object, field: str, divisor: float = 1e8) -> float | None:
    """Return a display-ready financial value without inventing missing data."""
    if not isinstance(values, dict):
        return None
    value = values.get(field)
    if value is None or pd.isna(value):
        return None
    return float(value) / divisor


def _display_date(value: object) -> str:
    """Keep provenance timestamps in the service while showing a readable date in tables."""
    if value is None or pd.isna(value):
        return "—"
    raw = str(value)
    return raw[:10] if len(raw) >= 10 else raw


def _display_datetime(value: object) -> str:
    """Show a compact local timestamp while retaining the full source value."""
    if value is None or pd.isna(value):
        return "—"
    raw = str(value).replace("T", " ")
    return raw[:16] if len(raw) >= 16 else raw


def _annual_income(statements: pd.DataFrame) -> pd.DataFrame:
    annual = statements[
        (statements.statement_type == "income")
        & statements.fiscal_period.astype(str).str.endswith("FY")
    ].copy()
    annual["revenue_billion"] = annual["values"].apply(lambda values: _financial_value(values, "revenue"))
    annual["net_profit_billion"] = annual["values"].apply(lambda values: _financial_value(values, "net_profit"))
    return annual.sort_values("fiscal_period")


def financials(snapshot: dict) -> None:
    guardrails = cached_research_display_guardrails(snapshot["snapshot_id"])
    st.markdown("### 财务表现")
    period_basis = st.segmented_control("期间口径", ["FY", "Q1", "H1", "9M", "TTM"], default="FY")
    metric = st.segmented_control("展示指标", ["收入", "归母净利润", "净利率"], default="收入")
    field = {"收入": "revenue", "归母净利润": "net_profit", "净利率": "net_margin"}[metric]
    financial_result = cached_research_financial_trends(snapshot["snapshot_id"], period_basis)
    frame = pd.DataFrame(financial_result["points"])
    issues = financial_result.get("issues", [])
    future_count = sum(issue.get("code") == "future_disclosure" for issue in issues)
    missing_count = sum(issue.get("code") == "missing_value" for issue in issues)
    financial_summary = st.columns(4)
    financial_summary[0].metric("可用期间", f"{len(frame)} 个")
    financial_summary[1].metric("研究截止日", financial_result["research_as_of"][:10])
    financial_summary[2].metric("未来披露排除", f"{future_count} 条")
    financial_summary[3].metric("缺失字段", f"{missing_count} 项")
    st.caption(
        f"当前期间口径：{period_basis} · Provider：{financial_result['provider']} · "
        "图表只使用 available_as_of 为真的数据。"
    )
    if frame.empty:
        st.info(f"当前快照没有可用的 {period_basis} 财务数据；未进行补零或跨期间推断。")
        if guardrails["missing_data"]["visible"]:
            _render_missing_financial_notice(guardrails)
        if guardrails["future_data"]["visible"]:
            st.error(f"存在 {guardrails['future_data']['count']} 份晚于研究截止日披露的数据；已排除。")
        if guardrails["validation_block"]["visible"]:
            st.error("校验已阻断：不展示确定性结论。")
        return
    frame["value"] = frame[field]
    if field != "net_margin":
        frame["value"] = frame["value"].apply(lambda value: value / 1e8 if pd.notna(value) else None)
    frame["evidence_id"] = frame["evidence_ids"].apply(lambda ids: ids.get(field))
    chart_data = frame.dropna(subset=["value"])
    title = f"{period_basis} · {metric}"
    y_title = "%" if field == "net_margin" else f"亿元 {financial_result['currency']}"
    if chart_data.empty:
        st.info(f"当前快照的 {period_basis} {metric}未覆盖；未进行补零。")
    else:
        chart_data = chart_data.copy()
        fig = px.bar(
            chart_data, x="fiscal_period", y="value", text_auto=".1f",
            custom_data=["evidence_id", "published_at", "provider", "period_basis"],
            color_discrete_sequence=["#0066cc"],
        )
        fig.update_traces(hovertemplate="报告期: %{x}<br>数值: %{y}<br>Evidence ID: %{customdata[0]}<br>披露时间: %{customdata[1]}<br>来源: %{customdata[2]}<br>期间口径: %{customdata[3]}<extra></extra>")
        fig.update_layout(title=title, height=380, paper_bgcolor="#fff", plot_bgcolor="#fff", margin=dict(l=15,r=15,t=45,b=15), yaxis_title=y_title, xaxis_title="报告期")
        event = st.plotly_chart(fig, width="stretch", on_select="rerun", selection_mode="points", key=f"financial_chart_{snapshot['snapshot_id']}_{period_basis}_{metric}")
        if event.selection.points:
            selected_id = event.selection.points[0].get("customdata", [None])[0]
            if selected_id:
                st.session_state["selected_evidence_id"] = selected_id
    table = frame[["fiscal_period", "published_at", "revenue", "net_profit", "net_margin", "evidence_id"]].copy()
    table["published_at"] = table["published_at"].apply(_display_date)
    table["revenue"] = table["revenue"].apply(lambda value: value / 1e8 if pd.notna(value) else None)
    table["net_profit"] = table["net_profit"].apply(lambda value: value / 1e8 if pd.notna(value) else None)
    table = table.sort_values("fiscal_period", ascending=False)
    table_event = st.dataframe(
        table, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
        key=f"financial_table_{snapshot['snapshot_id']}_{period_basis}_{metric}",
        column_config={
            "fiscal_period": "报告期", "published_at": "披露时间",
            "revenue": st.column_config.NumberColumn("营业收入（亿元）", format="%.2f"),
            "net_profit": st.column_config.NumberColumn("归母净利润（亿元）", format="%.2f"),
            "net_margin": st.column_config.NumberColumn("净利率", format="%.2f%%"),
            "evidence_id": "当前指标 Evidence ID",
        },
    )
    if table_event.selection.rows:
        selected_id = table.iloc[table_event.selection.rows[0]]["evidence_id"]
        if selected_id:
            st.session_state["selected_evidence_id"] = selected_id
    selected_id = st.session_state.get("selected_evidence_id")
    if selected_id and selected_id in set(table["evidence_id"].dropna()):
        selected = next(item for item in cached_research_evidence(snapshot["snapshot_id"])["records"] if item["evidence_id"] == selected_id)
        with st.container(border=True):
            st.markdown(f"#### 证据详情 · `{selected_id}`")
            st.write({"报告期": selected.get("period"), "披露时间": selected.get("published_at"), "来源": selected["provider"], "字段路径": selected.get("field_path")})
    if guardrails["missing_data"]["visible"]:
        _render_missing_financial_notice(guardrails)
    if guardrails["future_data"]["visible"]:
        st.error(f"存在 {guardrails['future_data']['count']} 份晚于研究截止日披露的数据；已从图表和证据详情中排除。")
    if guardrails["validation_block"]["visible"]:
            st.error("校验已阻断：不展示确定性结论，请在证据与校验页修复关键输入或依赖。")


def _render_missing_financial_notice(guardrails: dict) -> None:
    """Explain whether missing fields affect core research or optional metrics."""
    breakdown = guardrails.get("missing_data_breakdown", {})
    required_count = int(breakdown.get("required_count", 0))
    optional_count = int(breakdown.get("optional_count", 0))
    optional_metrics = ", ".join(breakdown.get("optional_metrics", [])) or "可选指标"
    total = guardrails["missing_data"]["count"]
    if required_count:
        metrics = ", ".join(breakdown.get("required_metrics", [])) or "核心财务指标"
        st.error(
            f"存在 {required_count} 个核心字段缺失（{metrics}），另有 {optional_count} 个可选字段缺失；"
            "相关页面显示为暂无数据，未进行补值。"
        )
    elif optional_count:
        st.info(
            f"发现 {total} 个缺失值，均属于可选字段（{optional_metrics}）。"
            "核心收入、归母净利润和估值输入仍有覆盖；页面不会用 0 或模型值补齐。"
        )
    else:
        st.warning(f"存在 {total} 个缺失财务字段；界面显示为暂无数据，未进行补值。")


def valuation(snapshot: dict, metadata: dict) -> None:
    m = metadata["key_metrics"]
    st.markdown("### 同行估值")
    st.info("当前多标的工作台尚未为每个快照生成同行估值产物，因此不展示或推导同业中位数。")
    rows = [{"指标": "PE", "目标公司": m["market_cap"] / m["ttm_net_profit"] if m["market_cap"] and m["ttm_net_profit"] else None}, {"指标": "PB", "目标公司": m["market_cap"] / m["book_equity"] if m["market_cap"] and m["book_equity"] else None}, {"指标": "PS", "目标公司": m["market_cap"] / m["ttm_revenue"] if m["market_cap"] and m["ttm_revenue"] else None}]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    peer = service.research_peer_valuation(snapshot["snapshot_id"])
    if not peer.get("available"):
        st.warning(f"当前本地快照没有可用同业参照：{_peer_unavailable_message(peer)}。")
        return
    st.markdown("#### 同业参照（仅限兼容本地快照）")
    reference_rows = []
    for name, value in peer["multiples"].items():
        median = value.get("median")
        if median is None:
            continue
        target_value = next((item["目标公司"] for item in rows if item["指标"] == name), None)
        comparison = "数据不足" if target_value is None else "高于同业中位数" if target_value > median else "低于同业中位数" if target_value < median else "接近同业中位数"
        reference_rows.append({"指标": name, "目标公司": target_value, "同业中位数": median, "对比": comparison, "中位数对应价格": value.get("implied_price"), "样本置信度": value.get("confidence")})
    st.dataframe(pd.DataFrame(reference_rows), width="stretch", hide_index=True)
    peers = "、".join(peer["peer_names"].values())
    low_confidence = "peer_count_below_4_low_confidence" in peer["validation"].get("warnings", [])
    note = "样本少于 4 家，仅作低置信度参考。" if low_confidence else "样本数达到基础可比要求，但不构成投资建议。"
    st.caption(f"已纳入 {peer['peer_count']} 家可比公司：{peers}。{note} 同业仅按同一行业、相近快照日期和 TTM 口径筛选。")


_PEER_UNAVAILABLE_LABELS = {
    "industry_not_classified": "该股票尚未配置申万一级行业，因此无法确定同行",
    "target_snapshot_missing_peer_inputs": "快照缺少可用的收盘价、总股本或财报期间",
}


def _peer_unavailable_message(peer: dict) -> str:
    """Surface the service-layer reason instead of one generic sentence.

    Collecting a brand-new stock via the workbench does not classify it in the
    Shenwan map, so it can never have peers.  Saying only "no compatible local
    peer snapshot" would hide that fixable configuration gap.
    """
    return _PEER_UNAVAILABLE_LABELS.get(
        peer.get("reason"), "当前没有满足期间、行业和证据要求的本地同行快照"
    )


def valuation_safe(snapshot: dict, metadata: dict) -> None:
    """展示服务层生成的同行估值，前端不派生任何倍数。"""
    st.markdown("### 同行估值")
    peer_sync_notice = st.session_state.pop("peer_sync_notice", None)
    if peer_sync_notice:
        if peer_sync_notice["status"] == "completed":
            st.success(
                f"已同步 {peer_sync_notice['synchronized_count']} 个同业数据版本；"
                "已生成统一研究截止日的数据版本，同行估值仍会按财务期间和数据依据进行筛选。"
            )
        elif peer_sync_notice["status"] == "partial":
            st.warning(
                f"已同步 {peer_sync_notice['synchronized_count']} 个版本，"
                f"另有 {peer_sync_notice['failed_count']} 个未能更新，已从本次同行比较中排除。"
            )
        else:
            st.error("同行数据同步未启动；请确认当前股票和同行均有可用的真实数据版本。")
    peer = cached_research_valuation(snapshot["snapshot_id"])
    if peer.get("method_boundary") == "financial_institution_pe_pb_only":
        st.warning("金融机构不适用通用企业估值框架；当前仅展示 PE/PB，隐藏 PS、EV/EBITDA 和通用企业 DCF。")
    if not peer.get("available"):
        st.warning(f"当前无法进行同行估值：{_peer_unavailable_message(peer)}。")
        if peer.get("remediation"):
            st.caption(peer["remediation"])
        preview = service.peer_sync_preview(snapshot["snapshot_id"])
        if preview.get("available"):
            st.info(
                f"发现 {preview['candidate_count']} 个可同步的本地同行版本。"
                "同步后会在同一研究截止日重新尝试同行比较；财务期间不一致的数据仍会被排除。"
            )
            if preview.get("truncated_count"):
                st.caption(
                    f"为控制联网调用，本次最多同步 {service.PEER_SYNC_MAX_SYMBOLS} 个标的；"
                    f"其余 {preview['truncated_count']} 个同行不会参与本次刷新。"
                )
            if st.button(
                "同步更新同行数据",
                key=f"peer_sync_{snapshot['snapshot_id']}",
                icon=":material/sync:",
                type="primary",
            ):
                with st.spinner("正在同步同行收盘价并创建统一版本…"):
                    outcome = service.refresh_peer_group(snapshot["snapshot_id"])
                st.session_state["peer_sync_notice"] = outcome
                if outcome.get("status") in {"completed", "partial"}:
                    service.load_snapshot.cache_clear()
                    cached_research_catalog.clear()
                    load_research.clear()
                    st.cache_data.clear()
                    refreshed_catalog = service.list_research()
                    labels = {item["id"]: snapshot_choice_label(item) for item in refreshed_catalog}
                    target_snapshot_id = outcome.get("snapshot_id")
                    if target_snapshot_id in labels:
                        st.session_state["snapshot_picker"] = labels[target_snapshot_id]
                st.rerun()
        elif preview.get("reason") == "no_provider_backed_peers":
            st.caption("当前没有可同步的真实数据同行快照；请先建立同行的本地数据版本。")
        st.caption("因此不判断估值高低，也不生成替代数字。")
        return
    metric = st.segmented_control("估值指标", list(peer["multiples"]), default=next(iter(peer["multiples"])))
    multiple = peer["multiples"][metric]
    interval = peer["valuation_ranges"][metric]
    target = peer["target_multiples"].get(metric)
    confidence_label = {"high": "高", "medium": "中", "low": "低", "unavailable": "不可用"}.get(multiple.get("confidence"), "未测量")
    with st.container(horizontal=True):
        st.metric("目标公司", f"{target:.2f}×" if target is not None else "未覆盖", border=True)
        st.metric("同行中位数", f"{interval['median']:.2f}×" if interval["median"] is not None else "未覆盖", border=True)
        st.metric("同行区间 P25–P75", f"{interval['p25']:.2f}–{interval['p75']:.2f}×" if interval["p25"] is not None and interval["p75"] is not None else "未覆盖", border=True)
        st.metric("样本置信度", confidence_label, border=True)
    included_count = len(multiple.get("included_symbols", []))
    excluded = multiple.get("excluded", {}) or {}
    candidate_count = len(peer.get("distributions", {}).get(metric, []))
    if excluded:
        excluded_reasons = ", ".join(f"{symbol}: {reason}" for symbol, reason in excluded.items())
        st.info(f"{metric} 候选同行 {candidate_count} 家，实际纳入 {included_count} 家；排除 {len(excluded)} 家（{excluded_reasons}）。")
    else:
        st.caption(f"{metric} 候选同行 {candidate_count} 家，实际纳入 {included_count} 家。")
    if peer["conclusion_allowed"]:
        st.caption(
            f"隐含价格区间：¥{interval['implied_price_low']:.2f}–¥{interval['implied_price_high']:.2f}；"
            f"中位数对应 ¥{interval['implied_price_median']:.2f}。仅为确定性同行倍数换算，不构成投资建议。"
            if interval["implied_price_low"] is not None and interval["implied_price_high"] is not None
            else "当前输入不足，未生成隐含价格区间。"
        )
    else:
        st.error("Validator 已阻断：隐含价格和确定性估值区间已隐藏。")
    distribution = pd.DataFrame(peer["distributions"][metric])
    chart_rows = distribution.dropna(subset=["value"])
    if not chart_rows.empty:
        colors = chart_rows["included"].map({True: "#0066cc", False: "#c78512"})
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=chart_rows["value"], y=chart_rows["name"], mode="markers", marker=dict(size=12, color=colors),
            customdata=chart_rows[["symbol", "included", "exclusion_reason", "snapshot_id", "research_as_of"]],
            hovertemplate="公司: %{y} (%{customdata[0]})<br>倍数: %{x:.2f}<br>纳入: %{customdata[1]}<br>原因: %{customdata[2]}<br>Snapshot: %{customdata[3]}<br>研究时点: %{customdata[4]}<extra></extra>",
            name="同行",
        ))
        if target is not None:
            fig.add_vline(x=target, line_color="#0F766E", line_width=2, annotation_text="目标公司")
        if interval["p25"] is not None and interval["p75"] is not None:
            fig.add_vrect(x0=interval["p25"], x1=interval["p75"], fillcolor="rgba(0, 102, 204,.10)", line_width=0, annotation_text="P25–P75")
        fig.update_layout(height=max(330, 52 * len(chart_rows)), xaxis_title=f"{metric}（倍）", yaxis_title=None, margin=dict(l=15, r=15, t=30, b=15), paper_bgcolor="#fff", plot_bgcolor="#fff", showlegend=False)
        st.plotly_chart(fig, width="stretch", key=f"peer_distribution_{snapshot['snapshot_id']}_{metric}")
    st.markdown("#### 同行纳入与剔除")
    reason_labels = {
        "same_industry_same_period": "同一行业且期间兼容", "industry_mismatch": "行业不一致",
        "period_basis_mismatch": "期间口径不一致", "missing_evidence_ids": "证据缺失",
        "invalid_price_or_shares": "价格或股本无效", "iqr_outlier": "IQR 异常值",
    }
    decisions = []
    metric_rows = {row["symbol"]: row for row in peer["distributions"][metric]}
    for decision in peer["decisions"]:
        row = metric_rows.get(decision["symbol"])
        reason = row.get("exclusion_reason") if row and row.get("exclusion_reason") else decision["reason"]
        decisions.append({"代码": decision["symbol"], "公司": peer["peer_names"].get(decision["symbol"], decision["symbol"]), "纳入": bool(row and row["included"]), "原因": reason_labels.get(reason, reason)})
    for symbol in peer["date_excluded_symbols"]:
        decisions.append({"代码": symbol, "公司": symbol, "纳入": False, "原因": "快照日期差超过 7 天"})
    st.dataframe(pd.DataFrame(decisions), width="stretch", hide_index=True)
    with st.expander("查看公式与证据输入"):
        formula = peer["formulas"][metric]
        st.code(f"目标倍数 = {formula['target_multiple']}\n隐含价格 = {formula['implied_price']}", language=None)
        st.write({"目标公司 Evidence IDs": formula["target_input_evidence_ids"], "同行 Evidence IDs": formula["peer_input_evidence_ids"]})
        with st.container(horizontal=True):
            for item_id in formula["target_input_evidence_ids"]:
                st.button("查看证据", key=f"valuation_evidence_{metric}_{item_id}", on_click=open_evidence, args=(item_id,), icon=":material/account_tree:")
    names = "、".join(peer["peer_names"].values())
    low_confidence = "peer_count_below_4_low_confidence" in peer["validation"].get("warnings", [])
    confidence = "样本少于 4 家，仅作低置信度参考。" if low_confidence else "样本数达到基础比较门槛。"
    st.caption(f"同行（{peer['peer_count']} 家）：{names}。{confidence} 价格口径 {peer['price_basis']}，期间口径 {peer['period_basis']}；仅纳入同一行业和相近快照日期，不构成投资建议。")


def evidence(snapshot: dict, metadata: dict) -> None:
    st.markdown("### 数据依据与检查")
    evidence_result = cached_research_evidence(snapshot["snapshot_id"])
    validation = cached_research_validation(snapshot["snapshot_id"])
    readiness = cached_research_readiness(snapshot["snapshot_id"])
    records = evidence_result["records"]
    checks = validation.get("checks", [])
    status_labels = {"pass": "通过", "warning": "需要关注", "blocked": "数据不足"}
    quality_label, _, quality_reason, _ = research_quality_copy(validation, readiness)
    passed_count = sum(check.get("status") == "pass" for check in checks)
    attention_count = sum(check.get("status") != "pass" for check in checks)
    summary_columns = st.columns(4)
    summary_columns[0].metric("研究质量", quality_label)
    summary_columns[1].metric("数据依据", f"{len(records)} 项")
    summary_columns[2].metric("数据检查", f"{passed_count} 项通过")
    summary_columns[3].metric("需要关注", f"{attention_count} 项")
    st.caption(f"研究质量说明：{quality_reason}")
    attention = [check for check in checks if check.get("status") != "pass"]
    if attention:
        with st.container(border=True):
            st.markdown("#### 当前需要关注")
            for check in attention:
                label = status_labels.get(check.get("status"), "未测量")
                st.markdown(
                    f"**{check.get('label', check.get('code', '检查项'))} · {label}**："
                    f"{validation_check_copy(check)}"
                )
                if check.get("remediation"):
                    st.caption(f"处理建议：{check['remediation']}")
    else:
        st.success("当前没有待处理的校验项；可以继续查看证据明细和计算依赖。")
    filter_left, filter_middle, filter_right = st.columns([1, 1, 2])
    with filter_left:
        kind_filter = st.selectbox(
            "证据类型", ["全部", "事实", "计算"],
            key=f"evidence_kind_filter_{snapshot['snapshot_id']}",
        )
    with filter_middle:
        status_options = ["全部"] + sorted({str(item.get("status", "")) for item in records})
        status_filter = st.selectbox(
            "证据状态", status_options,
            key=f"evidence_status_filter_{snapshot['snapshot_id']}",
        )
    with filter_right:
        metric_filter = st.text_input(
            "指标筛选", placeholder="例如 revenue、market_cap、net_profit",
            key=f"evidence_metric_filter_{snapshot['snapshot_id']}",
        ).strip().lower()
    filtered_records = [
        item for item in records
        if (kind_filter == "全部" or ("计算" if item["kind"] == "calculation" else "事实") == kind_filter)
        and (status_filter == "全部" or str(item.get("status", "")) == status_filter)
        and (not metric_filter or metric_filter in str(item.get("metric", "")).lower())
    ]
    st.caption(f"当前显示 {len(filtered_records)} / {len(records)} 条证据记录。")
    rows = [{
        "Evidence ID": item["evidence_id"], "类型": "计算" if item["kind"] == "calculation" else "事实",
        "指标": item["metric"], "数值": item["value"], "单位": item["unit"],
        "期间": item.get("period") or "—", "来源": item["provider"], "状态": item["status"],
    } for item in filtered_records]
    frame = pd.DataFrame(rows)
    if frame.empty:
        st.info("当前筛选条件没有匹配的证据记录；未隐藏原始数据，可清空筛选后继续查看。")
        event = None
    else:
        event = st.dataframe(
            frame, width="stretch", hide_index=True, height=360, on_select="rerun",
            selection_mode="single-row", key=f"evidence_table_{snapshot['snapshot_id']}",
        )
    selected_id = st.session_state.get("selected_evidence_id")
    if event is not None and event.selection.rows:
        selected_id = frame.iloc[event.selection.rows[0]]["Evidence ID"]
        st.session_state["selected_evidence_id"] = selected_id
    selected = next((item for item in filtered_records if item["evidence_id"] == selected_id), None)
    if selected is not None:
        with st.container(border=True):
            st.markdown(f"#### 证据详情 · `{selected['evidence_id']}`")
            detail_left, detail_right = st.columns(2)
            detail_left.write({
                "类型": selected["kind"], "指标": selected["metric"], "数值": selected["value"],
                "单位": selected["unit"], "期间": selected.get("period"), "来源": selected["provider"],
            })
            detail_right.write({
                "字段路径": selected.get("field_path"), "运算": selected.get("operation"),
                "依赖 Evidence IDs": selected.get("input_ids", []), "披露时间": selected.get("published_at"),
            })
    else:
        st.info("选择任一证据行查看字段路径或计算依赖；也可从研究总览点击关键指标证据。")
    validation_export = {
        "snapshot_id": snapshot["snapshot_id"],
        "research_as_of": validation["research_as_of"],
        "provider": snapshot.get("provider", "snapshot"),
        "validation": validation,
        "evidence_records": records,
        "dependency_graph": evidence_result["dependency_graph"],
    }
    st.download_button(
        "下载全部证据与校验材料（JSON）",
        json.dumps(validation_export, ensure_ascii=False, indent=2),
        f"{snapshot['symbol']}_evidence_validation.json",
        "application/json",
        icon=":material/download:",
    )
    if validation["status"] == "blocked":
        st.error("当前存在关键缺口，系统不会输出确定性结论或研究报告。请按下方阻断项补齐数据后重跑。")
    elif validation["status"] == "warning":
        st.warning("基础证据和关键输入已通过；当前可阅读全部已验证材料与生成基础研究报告。黄色项表示研究范围受限，例如不能据此给出同行估值结论，并不等于已展示的事实数据不可信。")
    else:
        st.success("该快照的证据、关键输入和研究范围均满足当前研究要求，可生成完整研究交付。")

    left, right = st.columns(2)
    with left:
        st.markdown("#### 计算依赖")
        graph = evidence_result["dependency_graph"]
        if graph["edges"]:
            st.graphviz_chart(dependency_graph_dot(graph))
        else:
            st.info("当前证据集中没有计算依赖关系。")
    with right:
        st.markdown("#### 数据检查详情")
        for check in validation["checks"]:
            if check["status"] == "pass":
                message = f"**{check['label']}** — 已通过：{validation_check_copy(check)}"
            elif check["status"] == "warning":
                message = (
                    f"**{check['label']}** — 需要关注：{validation_check_copy(check)}\n\n"
                    f"当前影响：不阻断已验证事实和基础报告；涉及该项的扩展结论会降级或不展示。\n\n"
                    f"如需扩展范围：{check['remediation']}"
                )
            else:
                message = (
                    f"**{check['label']}** — 数据不足：{validation_check_copy(check)}\n\n"
                    f"需要处理：{check['remediation']}"
                )
            if check["status"] == "pass":
                st.success(message)
            elif check["status"] == "warning":
                st.warning(message)
            else:
                st.error(message)
        st.caption(f"当前数据版本的研究截止时间：{validation['research_as_of']}")


def _trace_legacy(snapshot: dict) -> None:
    st.markdown("### AI 分析过程")
    st.caption("仅呈现已记录的研究流程摘要；外部工具内容始终按数据处理。")
    events = [("01", "解析研究对象", f"{snapshot['symbol']} · A 股实体解析", "成功", "—"), ("02", "加载版本化快照", f"{snapshot.get('provider', 'snapshot')} / {snapshot['snapshot_id']}", "成功", "—"), ("03", "确定性计算", "TTM 与市场价值", "成功", "—"), ("04", "Evidence Ledger", "生成事实与计算依赖", "成功", "—")]
    for n, title, detail, status, duration in events:
        st.markdown(f"<div class='trace'><span class='badge verified'>{n} · {status}</span> <b>{title}</b> <span class='subline'>— {detail} · {duration}</span></div>", unsafe_allow_html=True)


def trace(snapshot: dict) -> None:
    """Render only persisted task events; legacy snapshots remain explicitly empty."""
    result = service.research_trace(snapshot["snapshot_id"])
    st.markdown("### AI 分析过程")
    if not result["available"]:
        st.info("该快照没有已持久化的执行轨迹；不会补造成功步骤。")
        return
    # Real Agent tasks persist a richer event stream than the generic task
    # transition trace.  Prefer it when available so this page exposes the
    # actual plan, model/tool calls, evidence registration and Validator gate.
    task_id = result.get("task_id")
    if task_id:
        try:
            agent_task = agent_service.get_agent_task(task_id)
            stream = agent_service.agent_task_events(task_id)
        except KeyError:
            agent_task = None
            stream = None
        if agent_task is not None and stream is not None:
            st.caption(
                f"任务 {task_id} · 模型 {agent_task.get('model_name') or '未记录'} · "
                f"状态 {agent_task.get('status') or '未记录'}"
            )
            for event in stream["events"]:
                with st.container(border=True):
                    st.write(f"**{event.get('event', 'unknown')}** · {event.get('status', 'unknown')}")
                    st.caption(f"{event.get('timestamp') or '—'} · {event.get('detail') or '—'}")
                    evidence_ids = (event.get("metadata") or {}).get("evidence_ids") or []
                    if evidence_ids:
                        st.code(", ".join(evidence_ids), language=None)
            tool_trace = stream.get("tool_trace") or []
            if tool_trace:
                with st.expander("工具调用明细", expanded=False):
                    st.dataframe(tool_trace, width="stretch", hide_index=True)
            return
    st.caption(
        f"任务 {result['task_id']} · Trace {result['trace_id']} · "
        f"模型 {result.get('model') or '未记录'} · Prompt {result.get('prompt_version') or '未记录'}"
    )
    for event in result["events"]:
        with st.container(border=True):
            st.write(f"**{event['event']}** · {event['status']}")
            st.caption(f"{event['at']} · {event['detail']}")
            if event.get("evidence_ids"):
                st.code(", ".join(event["evidence_ids"]), language=None)


def _report_candidates(snapshot: dict) -> list[Path]:
    """Find reports for the selected snapshot, including the current CLI naming."""
    output_dir = ROOT / "output"
    symbol = str(snapshot["symbol"])
    snapshot_id = str(snapshot.get("snapshot_id", ""))
    exact = output_dir / f"{symbol}_research_report.md"
    candidates = []
    # The snapshot report CLI writes ``<symbol>_<date>_<provider>_report.md``.
    # Keep discovery local and deterministic; do not infer a report from a
    # different symbol or expose arbitrary Markdown from the output directory.
    for path in output_dir.glob(f"{symbol}_*_report.md"):
        if path.is_file() and _report_matches_snapshot(path, snapshot_id):
            candidates.append(path)
    date_token = snapshot_id.split("_")[1] if "_" in snapshot_id else ""
    candidates.sort(key=lambda path: (
        0 if date_token and date_token in path.name else 1,
        0 if path == exact else 1,
        -path.stat().st_mtime,
        path.name,
    ))
    return candidates


def _report_sidecar(path: Path, snapshot_id: str) -> Path | None:
    """Return a metadata sidecar only when it names the selected snapshot."""
    sidecars = [path.with_suffix(".metadata.json")]
    if path.name == f"{snapshot_id.split('_')[0]}_research_report.md":
        sidecars.append(ROOT / "output" / f"{path.name.split('_')[0]}_cn_report.metadata.json")
    for sidecar in sidecars:
        if not sidecar.exists():
            continue
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("snapshot_id") == snapshot_id:
            return sidecar
    return None


def _report_matches_snapshot(path: Path, snapshot_id: str) -> bool:
    return _report_sidecar(path, snapshot_id) is not None


def _latest_validated_agent_report(snapshot_id: str) -> dict | None:
    """Return the newest passing Agent narrative for one pinned snapshot."""
    for item in service.list_tasks():
        if (item.get("kind") != "agent_research" or item.get("snapshot_id") != snapshot_id
                or item.get("status") != "succeeded"):
            continue
        try:
            task = agent_service.get_agent_task(item["id"])
        except KeyError:
            continue
        validation = task.get("validation") or {}
        answer = str(task.get("answer") or "").strip()
        if validation.get("valid") and answer:
            return task
    return None


def report(snapshot: dict) -> None:
    st.markdown("### 中文研究报告")
    agent_report = _latest_validated_agent_report(snapshot["snapshot_id"])
    candidates = _report_candidates(snapshot)
    if agent_report:
        task_id = agent_report["id"]
        answer = str(agent_report["answer"])
        st.success("该报告由真实 Agent 生成，且本轮已通过 Validator 校验。")
        st.caption(
            f"任务 {task_id} · 模型 {agent_report.get('model_name') or '未记录'} · "
            f"研究截至 {agent_report.get('research_as_of') or '未记录'}"
        )
        st.download_button(
            "下载 Agent 研究报告",
            data=answer,
            file_name=f"{snapshot['symbol']}_{snapshot['snapshot_id']}_agent_report.md",
            mime="text/markdown",
            key=f"download_agent_report_{task_id}",
        )
        st.markdown(answer)
    if not candidates and not agent_report:
        st.info("该快照尚未生成独立研究报告。请使用受控离线研究流程生成后再查看。")
        return
    if not candidates:
        return
    if agent_report and candidates:
        st.divider()
        st.caption("以下是同一快照的独立离线研究报告。")
    labels = {path.name: path for path in candidates}
    selected_name = st.selectbox(
        "报告版本", list(labels), key=f"report_picker_{snapshot['snapshot_id']}"
    )
    report_path = labels[selected_name]
    report_text = report_path.read_text(encoding="utf-8")
    metadata_path = _report_sidecar(report_path, str(snapshot["snapshot_id"]))
    peer_path = report_path.with_suffix(".peer_valuation.json")
    report_metadata = None
    if metadata_path is not None and metadata_path.exists():
        try:
            report_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report_metadata = None
    report_validation = (report_metadata or {}).get("validation", {})
    peer_metadata = (report_metadata or {}).get("peer_valuation", {})
    report_status = "通过" if report_validation.get("valid") else "未验证" if report_metadata else "未登记"
    peer_status = (
        f"{peer_metadata.get('peer_count', 0)} 家"
        if peer_metadata.get("available") else "不可用"
    )
    report_summary = st.columns(4)
    report_summary[0].metric("报告校验", report_status)
    report_summary[1].metric("同行样本", peer_status)
    report_summary[2].metric("Metadata", "已生成" if metadata_path is not None else "缺失")
    report_summary[3].metric("同行 JSON", "已生成" if peer_path.exists() else "缺失")
    st.caption(f"本地版本化工件 · {report_path.name}")
    with st.container(horizontal=True):
        st.download_button("下载 Markdown 报告", report_text, report_path.name, "text/markdown")
        if metadata_path is not None and metadata_path.exists():
            st.download_button(
                "下载报告 metadata",
                metadata_path.read_text(encoding="utf-8"),
                metadata_path.name,
                "application/json",
            )
        if peer_path.exists():
            st.download_button(
                "下载同行估值 JSON",
                peer_path.read_text(encoding="utf-8"),
                peer_path.name,
                "application/json",
            )
    st.markdown(report_text)
    with st.expander("查看原始 Markdown", expanded=False):
        st.text_area("只读报告源文本", report_text, height=560, disabled=True)


def documents() -> None:
    """CARD-01 反向跳转：报告数字 → document_id → 原始 PDF 页（bbox 高亮）。"""
    st.markdown("#### 文档智能 · 报告数字 → 原始 PDF 页")
    st.caption(
        "每一行都来自真实解析坐标（页码 + bbox），界面不重新推导、不猜测数值。"
        "点击左侧任一数字行，右侧跳转到它所在的原始页面并高亮该单元格。"
    )
    listing = document_service.list_documents()
    if not listing["items"]:
        st.info(
            "尚未注册任何文档。可在项目根目录运行 `python scripts/bootstrap_documents.py` "
            "载入随仓库提供的合成语料，或用 `ingest_cn_document` 工具摄入真实年报 PDF。"
        )
        return

    labels: dict[str, str] = {}
    for item in listing["items"]:
        flags = []
        if item["status"] != "ok":
            flags.append(item["status"])
        if item["suspected_injection"]:
            flags.append("疑似注入")
        suffix = f" · {'/'.join(flags)}" if flags else ""
        labels[f"{item['filename']}{suffix}"] = item["document_id"]
    picked = st.selectbox("已注册文档", list(labels), key="document_picker")
    document_id = labels[picked]
    if st.session_state.get("document_fact_document") != document_id:
        # A fact ID is only meaningful inside its own document.
        st.session_state["document_fact_document"] = document_id
        st.session_state.pop("document_fact_id", None)
    detail = document_service.document_detail(document_id)

    top = st.columns(5)
    top[0].metric("解析状态", detail["status"])
    top[1].metric("页数", detail["page_count"] or "—")
    top[2].metric("事实总数", detail["fact_count"])
    top[3].metric("可反向定位", detail["jumpable_fact_count"])
    top[4].metric("解析器", detail["parser"])

    guards = st.columns(3)
    guards[0].write({"类型": detail["document_type"], "公司": detail["company"] or "—", "报告期": detail["report_period"] or "—"})
    guards[1].write({"来源": detail["source"], "披露日": detail["publication_date"] or "—", "登记时间": detail["ingested_at"]})
    integrity = detail["integrity"]
    guards[2].write({
        "原文存在": integrity["file_exists"], "全文哈希一致": not integrity["tampered"],
        "失效事实数": integrity["facts_invalidated"],
    })

    if detail["suspected_injection"]:
        st.warning(
            f"该文档命中 {len(detail['injection_segments'])} 处疑似注入特征，已记录审计；"
            "文档文本始终只作为数据，不改变系统行为。"
        )
    if detail["type_mismatch_warning"]:
        st.warning(detail["type_mismatch_warning"])
    if detail["status"] == "degraded":
        st.error("该文档解析失败页比例过高：仅供参考，不可作为事实来源。")
        usable_label = next(
            (
                label for label, candidate_id in labels.items()
                if (candidate := next(item for item in listing["items"] if item["document_id"] == candidate_id))
                and candidate["status"] in {"ok", "partial"}
                and candidate["fact_count"] > 0
            ),
            None,
        )
        if usable_label:
            def _show_usable_document() -> None:
                st.session_state["document_picker"] = usable_label
                st.session_state.pop("document_fact_id", None)

            st.button(
                "查看可解析的示例年报",
                help="当前扫描页没有任何可识别文字；切换到已登记且含可定位数值的年报。",
                on_click=_show_usable_document,
            )

    with st.expander("解析摘要（逐页）", expanded=False):
        events = detail["parse_events"]
        if events:
            st.dataframe(
                pd.DataFrame(events).rename(columns={
                    "page": "页码", "parser": "解析器", "status": "状态", "note": "说明",
                }),
                width="stretch", hide_index=True,
            )
        else:
            st.caption("该文档未记录逐页事件。")
        if detail["notes"]:
            for note in detail["notes"]:
                st.caption(f"· {note}")

    facts_payload = document_service.document_facts(document_id)
    fact_rows = facts_payload["facts"]
    left, right = st.columns([1.2, 1])

    with left:
        st.markdown("##### 结构化事实（可点击数字）")
        if not fact_rows:
            st.caption("该文档没有可数值化的事实（可能是扫描页或纯文本公告）。")
            selected_fact = None
        else:
            frame = pd.DataFrame([{
                "页码": row["page"] or "—",
                "项目": row["row_label"],
                "列": row["column_label"] or "—",
                "列语义": row["column_role"],
                "数值": row["raw_value"],
                "单位": row["unit"] or "—",
                "标准指标": row["metric"] or "未收录",
                "所在表": row["table"] or "—",
            } for row in fact_rows])
            event = st.dataframe(
                frame, width="stretch", hide_index=True, height=420, on_select="rerun",
                selection_mode="single-row", key=f"document_facts_{document_id}",
            )
            if event.selection.rows:
                st.session_state["document_fact_id"] = fact_rows[event.selection.rows[0]]["fact_id"]
            selected_fact = st.session_state.get("document_fact_id")
            unknown = sum(1 for row in fact_rows if not row["metric"])
            if unknown:
                st.caption(f"{unknown}/{len(fact_rows)} 行不在 CARD-02 指标字典内，保持原始名称，未强行映射。")

    with right:
        st.markdown("##### 原始页面定位")
        if not fact_rows:
            st.caption("无可定位事实。")
            return
        if not selected_fact:
            st.info("点击左侧任一数字行，这里会显示页码、bbox 与该数值的原文切片。")
            return
        try:
            target = document_service.jump_target(document_id, selected_fact)
        except (KeyError, ValueError) as exc:
            st.warning(f"该事实无法定位：{exc}")
            return

        st.write({
            "项目": target["row_label"], "列": target["column_label"] or "—",
            "列语义": target["column_role"], "页码": f"第 {target['page']} 页",
        })
        st.write({
            "标准指标": target["metric"] or "未收录", "原文切片": repr(target["raw_text"]),
            "披露单位": target["unit"] or "—", "币种": target["currency"] or "—",
            "bbox": target["bbox"],
        })
        if not target["metric_known"] and target["metric_note"]:
            st.caption(f"指标未映射：{target['metric_note']}")
        try:
            image = document_service.page_image(document_id, target["page"], highlights=[target["bbox"]])
            st.image(image, caption=f"第 {target['page']} 页 · 红框即该数值所在 bbox", width="stretch")
        except ValueError as exc:
            st.caption(f"页面渲染不可用：{exc}")

        _document_source_actions(document_id, target["page"], detail["filename"])


def _document_source_actions(document_id: str, page: int, filename: str) -> None:
    """Offer the original file without adding a viewer dependency.

    The page image above renders the *actual* page and outlines the exact bbox,
    so the locator needs no third-party component. The download button is how a
    reviewer opens the whole filing to check the surrounding context.
    """
    try:
        payload, media_type = document_service.document_source(document_id)
    except (KeyError, ValueError) as exc:
        st.caption(f"原文下载不可用：{exc}")
        return
    st.caption(f"该数值位于第 {page} 页；以下是原文全文，供核对上下文。")
    st.download_button(
        f"下载原文 · {filename}（{len(payload) / 1e3:.1f} KB）",
        data=payload,
        file_name=filename or f"{document_id}.pdf",
        mime=media_type,
        key=f"document_download_{document_id}",
    )


def _analysis_picker_order(items: list[dict]) -> list[dict]:
    """Order the picker for the analysis page only — the document list is unchanged.

    Financial statements come first (newest report period first), because those
    are the documents this page can actually analyse; announcements and research
    drafts carry no statement structure and will honestly report 0 metrics. This
    is a presentation default, not a filter: nothing is hidden.

    Implemented as three stable sorts because the group and the period sort in
    opposite directions, which a single composite key cannot express cleanly.
    """
    statement_types = {"annual", "quarterly", "semiannual", "interim"}
    ordered = sorted(items, key=lambda item: item["filename"])
    ordered.sort(key=lambda item: str(item.get("report_period") or ""), reverse=True)
    ordered.sort(key=lambda item: 0 if item["document_type"] in statement_types else 1)
    return ordered


def financial_analysis() -> None:
    """CARD-03：指标表（每行可展开公式与输入证据）+ 诊断信号徽章。

    The page owns no arithmetic. Every number it renders is either a level the
    engine produced or a comparison the engine recorded, and the expandable
    detail is the *registry's* formula + operands + its own re-derived value.
    """
    st.markdown("#### 财务分析 · 指标与诊断信号")
    st.caption(
        "全部数值由确定性引擎产出（CARD-02 规范化事实 → 计算注册表），不是模型输出。"
        "每一行都可展开公式、输入证据 id 与引擎重算值；亏损基期的同比只给绝对变动，"
        "缺失输入登记在「未产出」中，绝不插值或替代。"
    )
    listing = document_service.list_documents()
    if not listing["items"]:
        st.info(
            "尚未注册任何文档。可在项目根目录运行 `python scripts/bootstrap_documents.py` "
            "载入随仓库提供的合成语料，或用 `ingest_cn_document` 工具摄入真实年报 PDF。"
        )
        return

    labels: dict[str, str] = {}
    for item in _analysis_picker_order(listing["items"]):
        flags = [item["status"]] if item["status"] != "ok" else []
        if item["suspected_injection"]:
            flags.append("疑似注入")
        suffix = f" · {'/'.join(flags)}" if flags else ""
        labels[f"{item['filename']}{suffix}"] = item["document_id"]
    picked = st.selectbox("已注册文档", list(labels), key="analysis_document_picker")
    document_id = labels[picked]
    if st.session_state.get("analysis_fact_document") != document_id:
        st.session_state["analysis_fact_document"] = document_id
        st.session_state.pop("analysis_metric_row", None)

    try:
        payload = document_service.document_analysis(document_id)
    except KeyError as exc:
        st.error(f"无法分析该文档：{exc}")
        return

    top = st.columns(5)
    top[0].metric("报告期", payload["report_period"] or "—")
    top[1].metric("口径", payload["scope"] or "—")
    top[2].metric("指标数", payload["metric_count"])
    top[3].metric("缺失（阻断）", sum(1 for row in payload["missing"] if row["blocking"]))
    top[4].metric("数据完整", "是" if payload["data_complete"] else "否")

    _analysis_comparability(payload)

    if payload["validation_errors"]:
        st.error(
            "计算复核未通过，以下记录无法重算，结论不应采信：\n\n"
            + "\n".join(f"- {item}" for item in payload["validation_errors"])
        )
    elif not payload["metrics"]:
        st.warning(
            "该文档产不出任何指标：没有形成可计算的报表结构"
            "（缺单位声明、期间语义不可判定，或事实不在标准指标字典内）。"
        )
    elif not payload["data_complete"]:
        st.warning("部分指标因缺失输入未产出；已产出的指标仍然可逐行复核。")

    _analysis_signals(payload["signals"])

    if payload["metrics"]:
        left, right = st.columns([1.35, 1])
        with left:
            st.markdown("##### 指标表（点击行查看计算过程）")
            frame = pd.DataFrame([_analysis_row(row) for row in payload["metrics"]])
            event = st.dataframe(
                frame, width="stretch", hide_index=True, height=430, on_select="rerun",
                selection_mode="single-row", key=f"analysis_metrics_{document_id}",
            )
            if event.selection.rows:
                st.session_state["analysis_metric_row"] = event.selection.rows[0]
            selected = st.session_state.get("analysis_metric_row")
        with right:
            st.markdown("##### 计算过程")
            _analysis_calculation(payload["metrics"], st.session_state.get("analysis_metric_row"))

    _analysis_gaps(payload)
    _analysis_warnings(payload)


def _analysis_comparability(payload: dict) -> None:
    """CARD-04 §14：可比性警告横幅（可展开到公告/页/期间/决策）。

    The banner is deliberately the *first* thing under the summary counters,
    because everything below it — including the percentage column — has to be
    read in its light. It renders three states and never conflates them:

    * a decision (横幅 + 可展开的依据);
    * detected-but-no-signal (一条 caption，说明"未命中 ≠ 不存在");
    * not assessed (no CARD-04 payload at all) — shown, not hidden.
    """
    signals = payload.get("scope_signals") or []
    banner = payload.get("comparability_banner")
    if banner is None:
        if payload.get("comparability") is None:
            st.caption(
                "可比性：CARD-04 未命中任何会计口径/重述规则 —— 这是"
                "『未匹配到种子关键词表与表格版本列』，不等于文档不存在口径变化；"
                "覆盖范围见 `src/cn/analysis/scope_signals.detection_coverage()`。"
            )
        return

    decision = banner["decision"]
    headline = (
        f"可比性警告 · {banner['decision_label']}（{decision}）：{banner['message']}"
    )
    if decision == "block_yoy":
        st.error(headline)
    else:
        st.warning(headline)

    with st.expander("展开：哪个公告、哪一页、影响哪些期间、系统做了什么决策", expanded=False):
        st.write({
            "文档": payload.get("filename") or payload.get("document_id"),
            "文档类型": payload.get("document_type") or "—",
            "决策": f"{banner['decision_label']}（{decision}）",
            "受影响期间": "、".join(banner["periods_affected"]) or "文档未指明具体期间",
            "涉及页面": "、".join(f"第 {page} 页" for page in banner["pages"]) or "不可定位",
            "信号类型": "、".join(banner["signal_types"]) or "—",
        })
        st.caption(
            "决策由确定性规则表得出（`scope_signals.DECISION_RULES`，多信号取最严），"
            "模型不参与判定。"
            + (
                "本次未输出任何同比百分比：水平值仍然展示，因为它们是事实；"
                "但两端不可比时给出百分比即为误算。"
                if banner["blocked"]
                else "比较值已按该决策取调整后/重述后版本。"
            )
        )

        st.markdown("###### 信号明细与证据")
        for index, signal in enumerate(signals):
            pages = "、".join(f"第 {page} 页" for page in signal["pages"]) or "不可定位"
            st.write(
                f"**{index + 1}. {signal['label']}** · {signal['decision_label']} · "
                f"严重度 {signal['severity']} · 页 {pages} · 规则 `{signal['detector_rule']}`"
            )
            st.caption(
                f"来源面：{'、'.join(signal['surfaces']) or '—'} ｜ "
                f"影响期间：{'、'.join(signal['periods_affected']) or '文档未指明'} ｜ "
                f"调整后数据可得：{'是' if signal['adjusted_data_available'] else '否'}"
            )
            st.caption(f"原文：{signal['detail']}")
            evidence = signal["evidence"]
            if not evidence:
                st.caption("该信号未绑定证据 —— 不应出现，请核对 `validation_errors`。")
                continue
            for item in evidence:
                page_label = f"第 {item['page']} 页" if item["page"] else "无页码"
                locatable = "可定位" if item["jumpable"] else "不可定位（已失效）"
                st.caption(f"· `{item['fact_id']}` {page_label} · {locatable}")


def _analysis_row(row: dict) -> dict:
    """Flatten one metric payload for the table (display only, no arithmetic)."""
    return {
        "指标": row["label"] or row["metric"],
        "期间": row["period_key"] or "—",
        "口径": row["period_basis"] or "—",
        "数值": row["value"],
        "单位": row["unit"],
        "比较": _COMPARISON_LABELS.get(row["comparison"] or "", row["comparison"] or "—"),
        "语义": _SEMANTIC_LABELS.get(row["comparison_semantic"] or "", "—"),
        "绝对变动": row["delta_abs"],
        "变动%": row["delta_pct"],
        "备注": row["note"] or "—",
    }


def _analysis_signals(signals: list[dict]) -> None:
    """Diagnostic signals as badges, each expandable to its triggering numbers."""
    st.markdown("##### 诊断信号")
    if not signals:
        st.caption("本次分析未触发任何诊断信号（信号只表达背离，不表达因果）。")
        return
    for signal in signals:
        badge, detail = st.columns([0.28, 0.72])
        with badge:
            st.badge(signal["label"], color="orange")
        with detail:
            st.write(signal["detail"])
        with st.expander(f"触发依据 · {signal['signal_id']}", expanded=False):
            st.write({
                "期间": "、".join(signal["periods"]) or "—",
                "阈值": signal["threshold"],
                "观测值": signal["observed"],
            })
            st.caption(
                "指标计算记录："
                + ("、".join(signal["metric_calculation_ids"]) or "—")
            )
            st.caption(
                "信号只说明数值出现背离，需要进一步核查；报告不得把它写成确定因果，"
                "因果表述必须另有独立证据。"
            )


def _analysis_calculation(metrics: list[dict], index: int | None) -> None:
    if not metrics:
        return
    if index is None or index >= len(metrics):
        st.info("点击左侧任一指标行，这里显示公式、输入证据与引擎重算值。")
        return
    row = metrics[index]
    calculation = row.get("calculation")
    st.write({
        "指标": row["label"], "期间": row["period_key"], "口径": row["period_basis"],
        "数值": row["value"], "单位": row["unit"],
    })
    if not calculation:
        st.caption("该行是水平值，没有记录计算过程。")
        return
    st.code(calculation["formula"], language="text")
    st.write({
        "运算": calculation["operation"], "记录数值": calculation["value"],
        "引擎重算": calculation["recalculated"],
        "口径": calculation["period_basis"], "来源": calculation["source"],
        "计算 id": row["calculation_id"],
    })
    st.dataframe(
        pd.DataFrame([{
            "输入类型": {"evidence": "证据", "calculation": "中间计算", "constant": "公式常量"}.get(
                operand["kind"], operand["kind"]
            ),
            "输入 id": operand["id"],
            "数值": operand["value"],
            "说明": operand["label"] or "—",
        } for operand in calculation["operands"]]),
        width="stretch", hide_index=True,
    )
    if calculation.get("note"):
        st.caption(calculation["note"])
    if row.get("note"):
        st.caption(f"口径说明：{row['note']}")


def _analysis_gaps(payload: dict) -> None:
    """Missing metrics, split by *why* they are missing."""
    blocking = [row for row in payload["missing"] if row["blocking"]]
    not_applicable = [row for row in payload["missing"] if not row["blocking"]]
    if not payload["missing"]:
        return
    with st.expander(
        f"未产出的指标（阻断 {len(blocking)} · 不适用 {len(not_applicable)}）", expanded=False
    ):
        if blocking:
            st.markdown("**缺失输入（阻断，使 data_complete=False）**")
            st.dataframe(
                pd.DataFrame([{
                    "指标": row["metric"], "期间": row["period_key"] or "—",
                    "需要输入": "、".join(row["required_inputs"]) or "—", "原因": row["reason"],
                } for row in blocking]),
                width="stretch", hide_index=True,
            )
        if not_applicable:
            st.markdown("**结构性不适用（不阻断）**")
            st.dataframe(
                pd.DataFrame([{
                    "指标": row["metric"], "期间": row["period_key"] or "—", "原因": row["reason"],
                } for row in not_applicable]),
                width="stretch", hide_index=True,
            )


def _analysis_warnings(payload: dict) -> None:
    messages = list(payload["warnings"]) + list(payload["normalization_warnings"])
    if not messages:
        return
    with st.expander(f"警告与口径提示（{len(messages)}）", expanded=False):
        for message in messages:
            st.caption(f"· {message}")


def _currency_of(detail_or_model) -> str | None:
    """Unit of account a cost is denominated in, or ``None`` when unknown.

    Accepts either a model name or an evaluation artifact (a stored
    ``cost_currency`` wins, then the ``model`` it records).  ``None`` is a real
    answer here: it is what an unpriced model, or an artifact that predates cost
    currency tracking, actually knows.  Rendering it as "USD" or as ``$0.00``
    would turn "unknown" into a number, which is the one thing a cost column
    must never do.
    """
    if isinstance(detail_or_model, str):
        try:
            return cost_currency(detail_or_model)
        except KeyError:
            return None
    detail = detail_or_model or {}
    currency = detail.get("cost_currency")
    if currency is None:
        currency = (detail.get("metadata") or {}).get("cost_currency")
    if currency is None:
        model = detail.get("model") or (detail.get("metadata") or {}).get("model")
        if model:
            try:
                currency = cost_currency(str(model))
            except KeyError:
                currency = None
    return currency


def evaluations() -> None:
    st.markdown("### 评测与消融")
    st.caption("仅展示本地已落盘的评测产物；未测量指标不会以 0 代替。")
    items = service.list_evaluations()
    if not items:
        st.info("尚无可展示的 benchmark 或 ablation 产物。")
        return
    labels = {f"{item['kind']} · {item['id']}": item["id"] for item in items}
    evaluation_id = labels[st.selectbox("评测产物", list(labels), key="evaluation_picker")]
    detail = service.evaluation_detail(evaluation_id)
    st.caption(f"版本：{detail.get('metadata', {}).get('benchmark_version') or '未记录'} · 运行时间：{detail.get('metadata', {}).get('run_at') or '未记录'}")
    if detail["kind"] == "ablation":
        rows = detail.get("variants", [])
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            _ablation_currency = _currency_of(detail)
            st.caption(
                f"成本单位：{_ablation_currency}"
                if _ablation_currency
                else "成本单位未知（该模型未定价）：成本列不可作为金额解读。"
            )
        missing = detail.get("missing_variants", [])
        if missing:
            st.warning(f"未测量配置：{', '.join(missing)}")
        detailed_results = detail.get("detailed_results", {})
        if detailed_results:
            variant = st.selectbox("查看消融案例", list(detailed_results), key="ablation_variant_picker")
            cases = detailed_results[variant]
            st.dataframe(pd.DataFrame([{key: row.get(key) for key in (
                "case_id", "passed", "tool_f1", "parameters_ok", "evidence_coverage",
                "validator_intercepted", "error_conclusion_leakage", "latency_seconds", "cost_usd",
            )} for row in cases]), width="stretch", hide_index=True)
            case_ids = [row.get("case_id", "unknown") for row in cases]
            selected_case = st.selectbox("案例详情", case_ids, key="ablation_case_picker")
            selected = next(row for row in cases if row.get("case_id") == selected_case)
            st.json({key: value for key, value in selected.items() if key not in {"report"}})
        return
    summary = detail.get("summary", {})
    currency = _currency_of(detail)
    cost_label = f"总成本（{currency}）" if currency else "总成本（币种未知）"
    metric_labels = {
        "tool_f1": "工具路由准确率", "parameter_accuracy": "参数准确率",
        "evidence_coverage": "Evidence 覆盖率", "e2e_success_rate": "任务成功率",
        "avg_latency_seconds": "平均延迟（秒）", "total_cost_usd": cost_label,
        "numeric_accuracy": "数字正确率", "conclusion_leakage_rate": "结论泄漏率",
        "failure_recovery_rate": "失败恢复率",
    }
    rows = [{"指标": label, "值": summary.get(key) if summary.get(key) is not None else "未测量"}
            for key, label in metric_labels.items()]
    st.dataframe(pd.DataFrame(rows).astype({"值": str}), width="stretch", hide_index=True)
    regression = detail.get("regression")
    if regression:
        (st.success if regression.get("passed") else st.error)(
            "CI 回归检查通过" if regression.get("passed") else f"CI 回归失败：{regression.get('regressions', [])}"
        )
    results = detail.get("results", [])
    if results:
        st.dataframe(pd.DataFrame([{key: row.get(key) for key in ("case_id", "passed", "tool_f1", "parameters_ok", "evidence_coverage", "latency_seconds", "cost_usd")} for row in results]), width="stretch", hide_index=True)
        case_ids = [row.get("case_id", "unknown") for row in results]
        selected_case = st.selectbox("查看案例轨迹", case_ids, key="evaluation_case_picker")
        selected = next(row for row in results if row.get("case_id") == selected_case)
        if selected.get("research_state"):
            st.json(selected["research_state"])


@st.fragment(run_every="10s")
def tasks() -> None:
    st.markdown("### 受控研究任务")
    st.caption("仅对已有版本化快照生成研究；没有快照时，只记录采集需求，不生成任何数值或结论。")
    with st.form("new_research_task", border=True):
        symbol = st.text_input("A 股代码", placeholder="例如：600519.SH")
        submitted = st.form_submit_button("启动受控研究", icon=":material/play_arrow:", width="content")
    if submitted:
        try:
            task = service.start_research(symbol)
        except ValueError as exc:
            st.error(f"代码格式无效：{exc}")
        else:
            if task["status"] in {"queued", "running"}:
                st.info(f"任务已进入 {task['status']} 状态：{task['message']}")
            elif task["status"] in {"completed", "succeeded"}:
                st.success(f"{task['symbol']} 的研究已生成；可从侧边栏切换到对应快照查看。")
                load_research.clear()
            elif task["status"] == "needs_snapshot":
                st.warning(task.get("message") or "未找到本地版本化快照。")
                st.code(task.get("next_step", ""), language=None)
            else:
                st.error(f"任务状态：{task.get('status', '?')} · {task.get('message') or '—'}")
    st.markdown("#### 新公司数据采集")
    st.caption("采集仅在服务端执行。Tushare Token 只从服务器 `.env` 读取；每次采集固定最多 6 次调用，且第一次失败即停止。")
    with st.form("snapshot_acquisition", border=True):
        acquisition_symbol = st.text_input("待采集 A 股代码", placeholder="例如：601318.SH", key="acquisition_symbol")
        start_date, end_date = st.columns(2)
        with start_date:
            start = st.date_input("起始日期", value=date.today() - timedelta(days=35), key="acquisition_start")
        with end_date:
            end = st.date_input("截止日期", value=date.today(), key="acquisition_end")
        acquire = st.form_submit_button("采集并固化快照", icon=":material/cloud_download:", width="content")
    if acquire:
        if not acquisition_symbol or not acquisition_symbol.strip():
            st.error("请输入待采集的 A 股代码。")
        else:
            try:
                task = service.collect_snapshot(acquisition_symbol, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
            except ValueError as exc:
                st.error(f"无法启动采集：{exc}")
            else:
                if task["status"] in {"queued", "running"}:
                    st.info(f"采集任务已进入 {task['status']} 状态：{task['message']}")
                elif task["status"] in {"completed", "succeeded"}:
                    st.success("快照已创建。现在可输入该代码启动受控研究。")
                    load_research.clear()
                elif task["status"] == "blocked":
                    st.warning(task.get("message") or "任务被阻断。")
                else:
                    st.error(f"采集未完成：{task.get('message') or '—'}")
    history = service.list_tasks()
    status_options = sorted({item.get("status", "unknown") for item in history})
    selected_statuses = st.multiselect(
        "按状态筛选任务", status_options, default=status_options,
        key="task_status_filter",
    )
    if selected_statuses:
        history = [item for item in history if item.get("status") in selected_statuses]
    if not history:
        st.info("尚无任务记录。")
        return
    rows = []
    for task in history:
        rows.append({"任务 ID": task["id"], "标的": task["symbol"], "状态": task.get("status", "?"),
                     "创建时间": task.get("created_at", ""),
                     "说明": task.get("message") or "—"})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with st.expander("任务详情、诊断与操作"):
        task_ids = [item["id"] for item in history]
        selected_task_id = st.selectbox("任务 ID", task_ids, key="task_history_picker")
        progress = service.get_task_progress(selected_task_id)
        diagnostics = service.task_diagnostics(selected_task_id)
        st.progress(int(progress.get("progress_percent") or 0), text=f"{progress['status']} · {progress.get('current_step') or 'waiting'}")
        st.json(diagnostics)
        if diagnostics["status"] in {"queued", "running"}:
            if st.button("取消任务", icon=":material/cancel:", key=f"cancel_{selected_task_id}"):
                st.info(service.cancel_task(selected_task_id)["message"])
                st.rerun()
        if diagnostics["kind"] in {"offline_research", "snapshot_acquisition"} and diagnostics["status"] in {"failed", "blocked"}:
            if st.button("安全重跑此任务", icon=":material/replay:", key=f"rerun_{selected_task_id}"):
                st.success(f"已创建重跑任务 {service.rerun_task(selected_task_id)['id']}。")
                st.rerun()


def agent_research(selected_snapshot_id: str) -> None:
    """A chat-first, auditable interface to the shared CnResearchAgent.

    A conversation is pinned to one versioned snapshot.  That is deliberately
    stricter than a generic chatbot: changing the snapshot halfway through a
    thread would make an earlier answer's evidence ambiguous.
    """
    st.markdown("### AI Agent 研究")
    st.caption(
        "直接输入问题即可开始对话。每一轮都会重新调用工具并通过 Validator；快照是版本化历史数据，不是实时行情。"
    )
    catalog = cached_research_catalog()
    selected_item = next((item for item in catalog if item["id"] == selected_snapshot_id), None)
    if selected_item is None:
        st.error("侧边栏当前选择的数据版本已不存在；请重新选择后再启动 Agent。")
        return
    cutoff = str(selected_item.get("research_as_of") or selected_item.get("as_of") or "")[:19]
    task_id = st.session_state.get("agent_task_id")
    active = False
    if task_id:
        try:
            active = agent_service.get_agent_task(task_id).get("status") in agent_service.ACTIVE_AGENT_STATUSES
        except KeyError:
            st.session_state.pop("agent_task_id", None)
            task_id = None

    controls, description = st.columns([1, 4])
    with controls:
        st.button(
            "新建对话",
            icon=":material/add_comment:",
            key="agent_new_conversation",
            on_click=start_new_agent_conversation,
        )
    with description:
        if task_id:
            try:
                task_snapshot_id = agent_service.get_agent_task(task_id).get("snapshot_id")
            except KeyError:
                task_snapshot_id = None
            if task_snapshot_id and task_snapshot_id != selected_snapshot_id:
                st.warning("当前会话已锁定在另一份数据版本。新建对话后，Agent 才会使用侧边栏当前选择。")
            else:
                st.caption("本会话已锁定侧边栏当前选择的数据版本与模型。")
        else:
            st.caption("本轮将直接使用侧边栏当前选择的数据版本；无需再次选择股票。")

    new_conversation_notice = st.session_state.pop("agent_new_conversation_notice", None)
    if new_conversation_notice:
        severity, message = new_conversation_notice
        {"success": st.success, "warning": st.warning}[severity](message)
    agent_research_archive(task_id)

    settings_disabled = bool(task_id)
    with st.expander("研究设置", expanded=not bool(task_id)):
        origin = "联网更新版本" if selected_item.get("snapshot_origin") == "online_refresh" else "本地基准版本"
        display_cutoff = cutoff.replace("T", " ") if cutoff else "未记录"
        st.markdown(
            f'''<div class="agent-context">
                <div class="agent-context-head">当前研究上下文 <span>已同步侧边栏</span></div>
                <div class="agent-context-grid">
                    <div class="agent-context-item"><span>研究对象</span><strong>{escape(str(selected_item['name']))} <em>· {escape(str(selected_item['symbol']))}</em></strong></div>
                    <div class="agent-context-item"><span>数据版本</span><strong>{origin}</strong></div>
                    <div class="agent-context-item"><span>研究截止</span><strong>{escape(display_cutoff)}</strong></div>
                </div>
            </div>''',
            unsafe_allow_html=True,
        )
        st.markdown("##### 分析参数")
        model_name = st.text_input("模型", value="deepseek-v4-flash", key="agent_model", disabled=settings_disabled)
        with st.expander("高级参数（默认：6 轮 · 随机性 0）", expanded=False):
            st.caption("默认设置优先保证研究过程稳定、可复现，并限制单轮执行时间。")
            c1, c2 = st.columns(2)
            with c1:
                max_steps = st.number_input("最大工具轮次", min_value=1, max_value=10, value=6, step=1,
                                            key="agent_max_steps", disabled=settings_disabled,
                                            help="单轮研究最多可进行多少次模型决策和工具调用。")
            with c2:
                temperature = st.number_input("回答随机性", min_value=0.0, max_value=1.0, value=0.0, step=0.1,
                                              key="agent_temperature", disabled=settings_disabled,
                                              help="0 表示更稳定、可复现的输出；数值越高，回答措辞越多样。")
        st.caption("研究截止日固定为侧边栏选定版本的截止日；问题中的日期不会改写它。历史价格覆盖不足时，工具会明确返回数据缺口。")

    _render_agent_conversation(task_id)
    agent_task_view()

    chat_disabled = active
    if task_id and not active:
        try:
            current = agent_service.get_agent_task(task_id)
            thread = agent_service.agent_conversation_thread(task_id)
            if current.get("status") != "succeeded":
                chat_disabled = True
                st.caption("上一轮未通过 Validator；请点击“新建对话”后继续研究。")
            elif len(thread) >= agent_service.MAX_FOLLOWUP_DEPTH:
                chat_disabled = True
                st.caption(f"本会话已达 {agent_service.MAX_FOLLOWUP_DEPTH} 轮上限；请点击“新建对话”继续。")
        except KeyError:
            chat_disabled = True

    prompt = st.chat_input(
        "例如：分析最新财报的收入、净利润和总资产变化，并引用证据。",
        disabled=chat_disabled,
        key="agent_chat_input",
    )
    if prompt:
        _submit_agent_chat_turn(
            prompt, selected_item, cutoff, model_name, temperature, max_steps, task_id,
        )


def start_new_agent_conversation() -> None:
    """Reset the visible thread without discarding its persisted audit trail."""
    previous_task_id = st.session_state.get("agent_task_id")
    if previous_task_id:
        try:
            active = agent_service.get_agent_task(previous_task_id).get("status") in agent_service.ACTIVE_AGENT_STATUSES
        except KeyError:
            active = False
        if active:
            st.session_state["agent_new_conversation_notice"] = (
                "warning", "当前 Agent 仍在运行；请先取消或等待完成，避免丢失正在生成的研究记录。"
            )
            return
    st.session_state.pop("agent_task_id", None)
    st.session_state.pop("agent_intro_reply", None)
    st.session_state["agent_new_conversation_notice"] = (
        "success", "已准备新的研究对话；历史研究仍保留在下方档案中。"
    )


def open_agent_archive(task_id: str) -> None:
    """Open an existing persisted Agent thread with its evidence and audit."""
    st.session_state["agent_task_id"] = task_id
    st.session_state["agent_archive_notice"] = "已打开历史研究；下方保留原始对话、证据数量与执行审计。"


def agent_research_archive(current_task_id: str | None) -> None:
    """Render leaf Agent threads as a compact, traceable research archive."""
    account = current_account()
    with st.expander("研究档案", expanded=False):
        if account is None:
            st.info("登录后可查看仅属于该账号的可追溯研究档案。")
            return
        owned_task_ids = account_service.agent_task_ids_for_user(account["id"])
        tasks = [
            item for item in service.list_tasks()
            if item.get("kind") == "agent_research" and item.get("id") in owned_task_ids
        ]
        parent_ids = {str(item.get("parent_task_id")) for item in tasks if item.get("parent_task_id")}
        conversations = [item for item in tasks if item.get("id") not in parent_ids]
        archive_notice = st.session_state.pop("agent_archive_notice", None)
        if archive_notice:
            st.info(archive_notice)
        st.caption("每条记录均绑定原始数据版本、研究截止日、对话内容、证据数量和执行审计。")
        if not conversations:
            st.info("尚无已保存的 Agent 研究。完成第一轮研究后会自动归档到当前账号。")
            return
        status_labels = {
            "succeeded": "已验证", "blocked": "已阻断", "failed": "失败",
            "cancelled": "已取消", "running": "进行中", "queued": "排队中",
        }
        for item in conversations[:20]:
            status = str(item.get("status") or "unknown")
            created_at = str(item.get("created_at") or "—").replace("T", " ")[:19]
            cutoff = str(item.get("research_as_of") or "—").replace("T", " ")[:19]
            query = str(item.get("query") or "未记录研究问题").strip()
            with st.container(border=True):
                main, action = st.columns([6, 1], vertical_alignment="center")
                with main:
                    st.markdown(f"**{item.get('symbol') or '未知标的'} · {status_labels.get(status, status)}**")
                    st.caption(f"研究截止 {cutoff} · 创建于 {created_at}")
                    st.write(query[:120] + ("…" if len(query) > 120 else ""))
                with action:
                    st.button(
                        "打开",
                        key=f"open_agent_archive_{item['id']}",
                        disabled=item["id"] == current_task_id,
                        on_click=open_agent_archive,
                        args=(item["id"],),
                    )
        if len(conversations) > 20:
            st.caption(f"仅显示最近 20 条研究档案；共有 {len(conversations)} 条已保存记录。")


def _render_agent_conversation(task_id: str | None) -> None:
    """Render the persisted, evidence-aware conversation as chat messages."""
    if not task_id:
        st.info("输入你的第一个研究问题开始对话。")
        intro_reply = st.session_state.get("agent_intro_reply")
        if intro_reply:
            with st.chat_message("assistant"):
                st.markdown(intro_reply)
        return
    try:
        thread = agent_service.agent_conversation_thread(task_id)
    except KeyError:
        return
    for turn in thread:
        with st.chat_message("user"):
            st.write(turn["query"])
        with st.chat_message("assistant"):
            if turn["status"] in agent_service.ACTIVE_AGENT_STATUSES:
                st.status("正在检索证据并运行确定性校验…", state="running")
            elif turn["status"] == "succeeded":
                st.markdown(turn["answer"] or "本轮未返回可展示的结论。")
                st.caption(f"已验证 · 证据 {turn['evidence_count']} 条 · 工具调用 {turn['tool_calls']} 次")
            elif turn["status"] == "blocked":
                st.error("本轮结论未通过 Validator，已阻断，不能作为研究结论。")
            else:
                st.warning(f"本轮未完成：{turn['status']}。")


def _submit_agent_chat_turn(
    prompt: str, item: dict, cutoff: str, model_name: str, temperature: float,
    max_steps: int, parent_task_id: str | None,
) -> None:
    """Queue a first question or a validated follow-up from the chat composer."""
    text = (prompt or "").strip()
    if _is_agent_small_talk(text):
        st.session_state["agent_intro_reply"] = (
            "我是 A 股研究助手，可以基于选定的历史快照完成：\n\n"
            "- 财务表现分析：收入、净利润、利润率及其变化。\n"
            "- 行情与估值分析：RAW 价格、PE、PB 和同行比较。\n"
            "- 数据可信度检查：研究截止时点、证据 ID、缺失项和校验结果。\n\n"
            "请选择研究快照后，可以直接问：\n\n"
            "- `分析这家公司的收入、净利润和利润率变化，并引用证据。`\n"
            "- `比较它的 PE、PB 与同行，并说明数据限制。`\n"
            "- `这份快照的截止时间和数据覆盖情况如何？`\n\n"
            "我会从所选快照中的工具结果取证；没有证据的结论会明确阻断。"
        )
        st.rerun()
        return
    if len(text) < 3:
        st.error("请输入至少 3 个字符的问题。")
        return
    account = current_account()
    if account is None:
        st.warning("请先在左侧账户区登录或注册；登录后才能创建可追溯的个人 Agent 研究档案。")
        return
    try:
        if parent_task_id:
            parent = agent_service.get_agent_task(parent_task_id)
            if parent.get("status") != "succeeded":
                st.error("上一轮未通过 Validator；请新建对话后再提问。")
                return
            request = agent_service.AgentResearchRequest(
                query=text, symbol=parent["symbol"], snapshot_id=parent["snapshot_id"],
                research_as_of=parent.get("research_as_of"), model_name=parent.get("model_name") or "",
                temperature=float(parent.get("temperature") or 0.0),
                max_steps=int(parent.get("max_steps") or 6), parent_task_id=parent_task_id,
            )
        else:
            request = agent_service.AgentResearchRequest(
                query=text, symbol=item["symbol"], snapshot_id=item["id"],
                research_as_of=(cutoff.strip() or None), model_name=model_name.strip(),
                temperature=float(temperature), max_steps=int(max_steps),
            )
        task = agent_service.start_agent_research(request)
        account_service.assign_agent_task(account["id"], task["id"])
        st.session_state.pop("agent_intro_reply", None)
        st.session_state["agent_task_id"] = task["id"]
        st.rerun()
    except agent_service.AgentResearchError as exc:
        st.error(f"无法启动 Agent：{exc}")
    except account_service.AccountError as exc:
        st.error(f"研究已创建但未能写入个人档案：{exc}")
    except KeyError as exc:
        st.error(f"快照或任务不存在：{exc}")


def _is_agent_small_talk(text: str) -> bool:
    """Keep greetings out of the evidence-gated research execution path."""
    normalized = re.sub(r"[\s，,。.!！?？]", "", text).lower()
    return normalized in {
        "hi", "hello", "hey", "你好", "你好啊", "你好呀", "嗨", "哈喽", "在吗", "你是谁",
        "帮助", "help", "功能", "有什么功能", "你有什么功能", "你能做什么", "你可以做什么",
        "你有哪些功能", "怎么使用", "如何使用", "使用方法", "使用帮助",
    }


def agent_task_view() -> None:
    """Render an audit once, or delegate active-task polling to a fragment."""
    task_id = st.session_state.get("agent_task_id")
    if not task_id:
        return
    try:
        public = agent_service.get_agent_task(task_id)
    except KeyError:
        st.info("该任务记录已不存在。")
        return

    status = public.get("status", "unknown")
    if status in agent_service.ACTIVE_AGENT_STATUSES:
        _agent_task_poll(task_id)
        return

    st.markdown(f"#### 任务 `{task_id}`")
    st.caption(f"状态：**{status}** · 当前步骤：{public.get('current_step') or '—'} · 更新于 {public.get('updated_at') or '—'}")
    st.progress(100, text=public.get("message") or status)
    if not st.checkbox("显示本轮审计记录", key=f"show_agent_audit_{task_id}"):
        return

    audit_view = st.radio(
        "审计内容", ["工具轨迹", "证据", "校验结果", "成本与运行信息"],
        horizontal=True, key=f"agent_audit_view_{task_id}",
    )
    if audit_view == "工具轨迹":
        _agent_trace(public, agent_service.agent_task_events(task_id))
    elif audit_view == "证据":
        _agent_evidence(public)
    elif audit_view == "校验结果":
        _agent_validator(public)
    else:
        _agent_cost(public)


@st.fragment(run_every="3s")
def _agent_task_poll(task_id: str) -> None:
    """Show the Agent's persisted decision trail while its task is running.

    The panel does not invent a plan or a tool result: every row comes from the
    event callback emitted by ``CnResearchAgent``. This gives the user a live
    view of the actual ReAct loop instead of a generic loading spinner.
    """
    try:
        public = agent_service.get_agent_task(task_id)
    except KeyError:
        st.info("该任务记录已不存在。")
        return
    status = public.get("status", "unknown")
    if status not in agent_service.ACTIVE_AGENT_STATUSES:
        st.rerun(scope="app")
        return
    st.markdown(f"#### Agent 正在研究 · `{task_id}`")
    st.caption(
        f"状态：**{status}** · 当前动作：{_agent_event_label(public.get('current_step'))} · "
        f"更新于 {public.get('updated_at') or '—'}"
    )
    progress = service.get_task_progress(task_id).get("progress_percent")
    st.progress(int(progress or 0), text=public.get("message") or status)
    stream = agent_service.agent_task_events(task_id)
    events = stream.get("events") or []
    with st.status("实时执行轨迹（仅展示已发生的动作）", state="running", expanded=True):
        st.caption("Agent 会先固定 A 股代码和研究截止时点，再按问题决定需要调用哪些工具；没有证据的结论会被 Validator 阻断。")
        if not events:
            st.write("已入队，等待执行器开始。")
        for event in events[-12:]:
            label = _agent_event_label(event.get("event"))
            detail = str(event.get("detail") or "")
            tool_name = (event.get("metadata") or {}).get("tool_name")
            suffix = f" · `{tool_name}`" if tool_name else ""
            suffix += f" · {detail}" if detail else ""
            st.write(f"{_agent_event_icon(event.get('status'))} **{label}**{suffix}")
    if st.button("取消任务", icon=":material/cancel:", key=f"cancel_agent_{task_id}"):
        with st.spinner("正在请求协作式停止…"):
            try:
                st.info(service.cancel_task(task_id)["message"])
            except ValueError as exc:
                st.error(f"无法取消：{exc}")
        st.rerun(scope="app")


def _agent_event_label(event: object) -> str:
    """Human-readable labels for persisted, machine-originated Agent events."""
    labels = {
        "queued": "任务已入队",
        "agent_started": "开始研究",
        "research_plan": "已确定本轮覆盖计划",
        "llm_call_started": "Agent 正在决定下一步",
        "llm_call_completed": "Agent 已作出下一步决策",
        "tool_arguments_pinned": "工具参数已固定",
        "tool_call_started": "正在调用工具",
        "tool_call_completed": "工具返回结果",
        "evidence_registered": "已登记证据",
        "validation_started": "开始确定性校验",
        "validation_passed": "校验通过",
        "validation_blocked": "校验阻断结论",
        "task_completed": "任务完成",
        "task_cancelled": "任务已取消",
        "task_failed": "任务失败",
    }
    return labels.get(str(event or ""), str(event or "等待执行"))


def _agent_event_icon(status: object) -> str:
    normalized = str(status or "").lower()
    if normalized in {"error", "failed", "blocked"}:
        return "✕"
    if normalized in {"running", "queued"}:
        return "…"
    return "✓"


def _agent_final_answer(public: dict, status: str) -> None:
    validation = public.get("validation") or {}
    if status == "blocked" and not validation.get("valid"):
        st.warning("BLOCKED — Validator 未通过，模型原始结论未经验证，不能作为结果展示。")
        errors = validation.get("errors") or []
        if errors:
            st.markdown("阻断原因：\n" + "\n".join(f"- `{error}`" for error in errors))
        diagnostic = (public.get("research_state") or {}).get("report")
        if diagnostic:
            with st.expander("内部诊断（仅供排查，不作为用户结论）"):
                st.caption(diagnostic)
        return
    if status == "cancelled":
        st.info(public.get("answer") or "研究任务已取消。")
        return
    if status == "failed":
        st.error(f"任务失败：{public.get('error') or '未知异常'}")
        return
    answer = public.get("answer") or (public.get("research_state") or {}).get("report") or ""
    st.markdown(answer)


def _agent_trace(public: dict, stream: dict) -> None:
    traces = (public.get("research_state") or {}).get("tool_trace", []) or []
    if not traces:
        st.caption("尚无工具执行记录。")
    else:
        rows = []
        for index, trace in enumerate(traces, start=1):
            args_text = json.dumps(trace.get("arguments", {}), ensure_ascii=False)
            if len(args_text) > 200:
                args_text = args_text[:200] + "…"
            rows.append({
                "步骤": index,
                "工具": trace.get("tool_name"),
                "参数": args_text,
                "结果状态": trace.get("result_status"),
                "证据 ID": ", ".join(trace.get("evidence_ids", []) or []),
                "耗时（秒）": _seconds_between(trace.get("started_at"), trace.get("ended_at")),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with st.expander("事件流（已脱敏）"):
        event_rows = [
            {"时间": event.get("timestamp"), "事件": event.get("event"),
             "状态": event.get("status"), "说明": event.get("detail", "")}
            for event in (stream.get("events") or [])
        ]
        if event_rows:
            st.dataframe(pd.DataFrame(event_rows), width="stretch", hide_index=True)
        else:
            st.caption("暂无事件。")


def _agent_evidence(public: dict) -> None:
    research_state = public.get("research_state") or {}
    facts = research_state.get("facts", []) or []
    calculations = research_state.get("calculations", []) or []
    st.caption(f"已登记证据：{len(facts)} 个事实 · {len(calculations)} 个计算")
    if facts:
        st.markdown("**事实证据**")
        st.code("\n".join(facts), language=None)
    if calculations:
        st.markdown("**计算证据**")
        st.code("\n".join(calculations), language=None)
    if not facts and not calculations:
        st.caption("没有可展示的证据。")


def _agent_validator(public: dict) -> None:
    validation = public.get("validation") or {}
    valid = bool(validation.get("valid"))
    if public.get("status") == "blocked" or not valid:
        st.error("已阻断")
        errors = validation.get("errors") or []
        if errors:
            st.markdown("阻断原因：\n" + "\n".join(f"- `{error}`" for error in errors))
        else:
            st.write("Validator 未通过；无详细错误。")
    else:
        st.success("已通过")
        st.write("结论已通过确定性校验器（无需 LLM 复判金融数字）。")


def _agent_cost(public: dict) -> None:
    usage = public.get("usage") or {}
    currency = _currency_of(public.get("model_name") or "")
    raw_cost = usage.get("cost_usd")
    if currency is None:
        # Unpriced model: the number exists but is not money, so it must not be
        # dressed up with a currency symbol.
        cost_value = "未定价" if raw_cost is None else f"{raw_cost}（未知单位）"
        cost_label = "成本（未定价）"
    else:
        symbol = {"USD": "$", "CNY": "¥"}.get(str(currency).upper(), f"{currency} ")
        cost_value = f"{symbol}{raw_cost or 0.0}"
        cost_label = f"成本 ({currency})"
    metric_cols = st.columns(4)
    metric_cols[0].metric("LLM 调用", usage.get("llm_calls") or 0)
    metric_cols[1].metric("工具调用", usage.get("tool_calls") or 0)
    metric_cols[2].metric(cost_label, cost_value)
    metric_cols[3].metric("延迟 (s)", usage.get("latency_seconds") or 0.0)
    metadata = [
        ("任务 ID", public.get("id")), ("状态", public.get("status")),
        ("模型", public.get("model_name")), ("温度", public.get("temperature")),
        ("最大工具轮次", public.get("max_steps")), ("快照", public.get("snapshot_id")),
        ("研究截止时点", public.get("research_as_of")),
        ("创建时间", public.get("created_at")), ("更新时间", public.get("updated_at")),
    ]
    st.table(pd.DataFrame([{"项": key, "值": value} for key, value in metadata]).astype({"值": str}))


def _seconds_between(start, end) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def daily_review() -> None:
    """Read-only market breadth review built from versioned local snapshots,
    with an optional on-demand live acquisition button."""
    st.markdown("### 每日复盘 · 市场广度")
    st.caption("默认离线演示；点击下方按钮可按需拉取 Tushare 实时数据。缺数据标注「未覆盖」，离线演示快照明确标记 synthetic_demo。")

    # ---- On-demand live acquisition bar ----
    acq_col1, acq_col2, acq_col3 = st.columns([3, 1.5, 1.5])
    with acq_col1:
        # Default: today in YYYYMMDD format; user can override.
        today_str = date.today().strftime("%Y%m%d")
        live_date = st.text_input(
            "采集日期（YYYYMMDD）",
            value=today_str,
            key="dr_live_date",
            help="仅支持已过去的交易日。周末/节假日/盘中时段会拦截并提示；建议填最近完整交易日（如上周五）。",
        )
    with acq_col2:
        if st.button("📡 获取实时数据", key="dr_acquire_btn", type="primary",
                     help="混合实时采集：指数 Tushare + 涨停/板块/广度/资金流 AkShare（免费源）。\n需要 .env 中已配置 TUSHARE_TOKEN；任一源不可用对应模块留空，绝不编数据。"):
            # --- Date validation: block future / same-day during market hours ---
            _date_err = None
            try:
                ld = datetime.strptime(live_date, "%Y%m%d").date()
                now = datetime.now()
                today_val = now.date()
                if ld > today_val:
                    _date_err = f"日期 {live_date} 是未来日期，无法采集。请填已过去的交易日。"
                elif ld == today_val:
                    # Chinese A-share market: 9:30-11:30, 13:00-15:00 Mon-Fri
                    _hour = now.hour
                    _wd = now.weekday()  # 0=Mon ... 6=Sun
                    if _wd >= 5:
                        _date_err = f"今天 {live_date} 是周末，休市。请填最近交易日（如上周五）。"
                    elif _hour < 16:
                        _date_err = (
                            f"今天 {live_date} 尚在交易时段内（当前 {_hour}:{now.minute:02d}），"
                            f"Tushare 盘后数据尚未更新。建议填最近完整交易日（如上周五），"
                            f"或等到 16:00 后再试。"
                        )
            except ValueError:
                _date_err = f"日期格式错误：'{live_date}' 不是有效的 YYYYMMDD 格式。"

            if _date_err:
                st.warning(_date_err, icon=":material/event_busy:")
            else:
                with st.spinner(f"正在混合采集（Tushare + AkShare + LLM润色）{live_date} 的实时数据…"):
                    try:
                        result = service.acquire_live_daily_review(live_date)
                        if result["status"] == "ok":
                            st.success(result["message"], icon=":material/cloud_done:")
                            st.session_state["dr_auto_select_snapshot"] = result.get("snapshot_id")
                            st.rerun()
                        elif result["status"] in ("partial_error", "partial"):
                            st.warning(result["message"], icon=":material/warning:")
                            st.session_state["dr_auto_select_snapshot"] = result.get("snapshot_id")
                            st.rerun()
                        else:
                            st.error(result["message"], icon=":material/error:")
                    except Exception as exc:
                        st.error(f"采集过程出错：{type(exc).__name__}: {str(exc)[:200]}", icon=":material/error:")
    with acq_col3:
        st.markdown("")
        st.markdown("")
        st.caption("💡 默认离线，按需实时；热点由 DeepSeek 润色")

    st.divider()

    reviews = service.list_daily_reviews()
    if not reviews:
        st.info("尚未发现本地市场复盘快照。可运行 `scripts\\collect_daily_review.py` 生成离线演示快照，"
                "或通过上方「获取实时数据」按钮触发。")
        return

    labels = {f"{item['review_as_of']} · {item['id']}": item["id"] for item in reviews}
    label_list = list(labels)

    # ---- 防御性取值工具（防止单点 None 导致整页渲染中止）----
    def _safe_get(d, *keys, default=None):
        cur = d
        for k in keys:
            if cur is None:
                return default
            if isinstance(cur, dict):
                cur = cur.get(k, default)
            elif isinstance(cur, (list, tuple)) and isinstance(k, int):
                try:
                    cur = cur[k]
                except (IndexError, TypeError):
                    return default
            else:
                return default
        return cur if cur is not None else default

    def _safe_detail(sid, kind):
        try:
            return service.daily_review_detail(sid, kind) or {}
        except Exception:
            return {}

    # Determine default index: auto-select newly acquired snapshot if available.
    auto_id = st.session_state.pop("dr_auto_select_snapshot", None)
    new_label = None
    if auto_id:
        for lbl, sid in labels.items():
            if sid == auto_id:
                new_label = lbl
                break
        if new_label is not None:
            # 关键修复：清除持久化的 widget state，使新的 index 真正生效
            st.session_state.pop("daily_review_picker", None)

    default_index = 0
    if new_label is not None:
        for idx, lbl in enumerate(label_list):
            if lbl == new_label:
                default_index = idx
                break

    selected = st.selectbox("复盘快照", label_list, index=default_index, key="daily_review_picker")
    snapshot_id = labels.get(selected) if selected in labels else None
    if not snapshot_id:
        # 选择已过期（label 与 id 映射错位）：重置后重跑，避免后续对 None 深度索引
        st.session_state.pop("daily_review_picker", None)
        st.warning("复盘快照选择已过期，正在重新加载列表…")
        st.rerun()

    try:
        summary = service.daily_review_summary(snapshot_id) or {}
    except Exception:
        summary = {}
    panorama = _safe_detail(snapshot_id, "panorama")
    hotspots = _safe_detail(snapshot_id, "hotspots")

    # 顶层字段全部经 _safe_get，杜绝 summary["validation"]["status"] 这类 KeyError/TypeError
    synth = bool(_safe_get(summary, "is_synthetic_demo", default=False))
    validation = _safe_get(summary, "validation", default={}) or {}
    gate_status = validation.get("status", "unknown")
    conclusion_allowed = bool(validation.get("conclusion_allowed", False))
    st.markdown(
        f'<div class="statusbar">'
        f'<span class="badge snapshot">复盘时点 {_safe_get(summary, "review_as_of", default="?")}</span>'
        f'<span class="badge {"warning" if synth else "verified"}">'
        f'{"离线演示 synthetic_demo" if synth else "来源 " + str(_safe_get(summary, "provider", default="?"))}</span>'
        f'<span class="badge {"blocked" if gate_status == "blocked" else "verified"}">校验 {gate_status}</span>'
        f'<span class="badge neutral">确定性结论 {"允许" if conclusion_allowed else "阻断"}</span>'
        f'<span class="badge neutral">指数 {_safe_get(summary, "index_count", default=0)} · '
        f'板块 {_safe_get(summary, "sector_count", default=0)} · 涨停 {_safe_get(summary, "limit_up_count", default=0)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if synth:
        st.warning("当前为离线演示快照（synthetic_demo），数值仅供架构演示，不代表真实行情，绝不用于实盘决策。", icon=":material/shield:")

    # ---- Full HTML report: render locally inline (no API server dependency) ----
    try:
        snapshot = service.load_market_review(snapshot_id)
        html_bytes = render_report_html(snapshot).encode("utf-8")
    except Exception as exc:
        html_bytes = None
        st.error(f"完整报告渲染失败：{exc}", icon=":material/error:")
    if html_bytes is not None:
        with st.expander("📄 在页面内完整查看报告 ↓（点击展开，无需额外服务）", expanded=False):
            st.caption(
                f"选中快照：`{snapshot_id}` · 大小 "
                f"{len(html_bytes)//1024} KB · 数据全部带 evidence_id 并标注 synthetic_demo。"
            )
            st.iframe(html_bytes.decode("utf-8"), height=1800)
        d_col1, d_col2 = st.columns([1, 3])
        with d_col1:
            st.download_button(
                "⬇️ 下载 HTML 报告",
                data=html_bytes,
                file_name=f"{snapshot_id}.html",
                mime="text/html",
                help="下载到本地后任意浏览器打开（同样不依赖 API）。",
            )
        with d_col2:
            st.caption(
                "页内查看与下载均在 streamlit 进程内完成，不依赖 uvicorn。"
                "如启 `uvicorn api:app` 也可通过 `"
                f"{os.getenv('FINTRACE_API_BASE', 'http://localhost:8000')}"
                f"/api/daily-review/{snapshot_id}/report` 获取同一份 HTML。"
            )

    # 各渲染小节独立容错：单点模块缺失/异常 -> 仅显示「未覆盖」，不崩整页
    st.markdown("#### 指数收盘")
    try:
        indices = _safe_get(panorama, "sections", "index_closing", "indices", default=[]) or []
        if not indices:
            st.info("指数收盘数据未覆盖。")
        else:
            cols = st.columns(min(len(indices), 3) or 1)
            for idx, item in enumerate(indices):
                with cols[idx % len(cols)]:
                    with st.container(border=True):
                        st.markdown(f"**{item.get('index_name', '?')}**")
                        close = item.get("close")
                        close_txt = f"{close:.2f}" if isinstance(close, (int, float)) else "—"
                        st.markdown(f"<div style='font-size:1.4rem'>{close_txt}</div>", unsafe_allow_html=True)
                        pc = item.get("pct_change") or 0
                        color = "#c0392b" if pc >= 0 else "#1c9a82"
                        sign = "+" if pc >= 0 else ""
                        chg = item.get("change")
                        amt = item.get("amount_yi")
                        if isinstance(chg, (int, float)) and isinstance(amt, (int, float)):
                            st.markdown(
                                f"<span style='color:{color};font-weight:650'>{sign}{pc:.2f}% "
                                f"({sign}{chg:.2f})</span> · 额 {amt:.0f}亿",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                f"<span style='color:{color};font-weight:650'>{sign}{pc:.2f}%</span>",
                                unsafe_allow_html=True,
                            )
                        st.caption(f"Evidence: {item.get('evidence_id', '?')}")
    except Exception:
        st.info("指数收盘数据渲染失败（未覆盖）。")

    st.markdown("#### 板块涨幅")
    try:
        sectors = _safe_get(hotspots, "sections", "hot_sectors", "items", default=[]) or []
        if sectors:
            sdf = pd.DataFrame(sectors)
            fig = px.bar(
                sdf, x="sector_name", y="pct_change", color="pct_change",
                color_continuous_scale=["#1c9a82", "#c9d3e0", "#c0392b"],
                custom_data=["sector_type", "leading_stock", "net_inflow_yi", "evidence_id"],
            )
            fig.update_traces(hovertemplate=(
                "板块: %{x}<br>涨幅: %{y:.2f}%<br>类型: %{customdata[0]}<br>领涨: %{customdata[1]}"
                "<br>净流入: %{customdata[2]:.1f}亿<br>Evidence: %{customdata[3]}<extra></extra>"))
            fig.update_layout(height=360, paper_bgcolor="#fff", plot_bgcolor="#fff",
                              margin=dict(l=15, r=15, t=30, b=80), yaxis_title="%", xaxis_title="")
            fig.update_coloraxes(showscale=False)
            st.plotly_chart(fig, width="stretch", key=f"dr_sector_{snapshot_id}")
        else:
            st.info("板块数据未覆盖。")
    except Exception:
        st.info("板块涨幅渲染失败（未覆盖）。")

    left, right = st.columns(2)
    with left:
        st.markdown("#### 连板天梯")
        try:
            ladder = _safe_get(panorama, "sections", "stock_dynamics", "ladder", default=[]) or []
            if ladder:
                ldf = pd.DataFrame(ladder)[["name", "board_days", "close", "pct_change", "limit_order_yi", "logic"]]
                st.dataframe(ldf, width="stretch", hide_index=True,
                             column_config={"name": "名称", "board_days": "连板", "close": st.column_config.NumberColumn("价", format="%.2f"),
                                            "pct_change": "涨%", "limit_order_yi": "封单(亿)", "logic": "逻辑"})
            else:
                st.info("涨停天梯未覆盖。")
        except Exception:
            st.info("涨停天梯渲染失败（未覆盖）。")
        st.markdown("#### 主力净流入 TOP")
        try:
            inflow = _safe_get(hotspots, "sections", "top_net_inflow", "items", default=[]) or []
            if inflow:
                idf = pd.DataFrame(inflow)
                st.dataframe(idf, width="stretch", hide_index=True,
                             column_config={"name": "名称", "net_inflow_yi": st.column_config.NumberColumn("净流入(亿)", format="%.1f"),
                                            "main_inflow_yi": "主买(亿)", "evidence_id": "Evidence"})
            else:
                st.info("主力净流入未覆盖。")
        except Exception:
            st.info("主力净流入渲染失败（未覆盖）。")
    with right:
        st.markdown("#### 全景复盘结论")
        try:
            st.write(_safe_get(panorama, "sections", "index_closing", "summary", default="（无结论）"))
            chars = _safe_get(panorama, "sections", "market_characteristics", default={}) or {}
            if chars.get("up_ratio_percent") is not None:
                st.write(
                    f"红盘占比 **{chars['up_ratio_percent']}%**，涨跌家数 {chars['up_count']}/{chars['down_count']}，"
                    f"涨停 {chars['limit_up_count']} / 跌停 {chars['limit_down_count']}，成交额 {chars['total_amount_yi']:.0f} 亿。"
                )
            dev = _safe_get(panorama, "sections", "deviation_review", default={}) or {}
            if dev.get("supported"):
                st.write(dev["observation"])
            st.caption("次日推演 / 宏观快照：离线快照未覆盖，不输出确定性预测。")
        except Exception:
            st.info("全景复盘结论渲染失败（未覆盖）。")
        st.markdown("#### 热点题材")
        try:
            themes = _safe_get(panorama, "sections", "core_drivers", "themes", default=[]) or []
            if not themes:
                st.info("热点题材未覆盖。")
            for theme in themes:
                with st.container(border=True):
                    st.markdown(f"**{theme.get('theme_name', '?')}**")
                    if theme.get("ai_generated") and theme.get("ai_summary"):
                        st.markdown(f"{theme.get('ai_summary', '')}  `AI 润色`")
                        with st.popover("查看原始新闻"):
                            st.write(theme.get("driver", ""))
                            links = theme.get("news_links") or []
                            if links:
                                st.markdown("来源：" + "  ".join(f"[链接]({u})" for u in links if u))
                    else:
                        st.write(theme.get("driver", ""))
                    st.caption("相关：" + "、".join(theme.get("related_stocks") or []) + f" · <span class='evidence'>{theme.get('evidence_id', '?')}</span>", unsafe_allow_html=True)
        except Exception:
            st.info("热点题材渲染失败（未覆盖）。")

    with st.expander("证据抽屉 · Evidence", expanded=False):
        try:
            recs = panorama.get("evidence_records", []) or []
            if recs:
                rdf = pd.DataFrame([
                    {"Evidence ID": r.get("evidence_id"), "指标": r.get("metric"), "数值": r.get("value"),
                     "单位": r.get("unit"), "期间": r.get("period"), "来源": r.get("source")}
                    for r in recs
                ])
                st.dataframe(rdf, width="stretch", hide_index=True, height=360,
                             column_config={"Evidence ID": "Evidence ID",
                                            "数值": st.column_config.NumberColumn("数值", format="%.4f")})
                export = {"snapshot_id": snapshot_id, "research_as_of": panorama.get("research_as_of"),
                          "validation": panorama.get("validation"), "evidence_records": recs}
                st.download_button("下载全部证据（JSON）", json.dumps(export, ensure_ascii=False, indent=2),
                                   f"{snapshot_id}_evidence.json", "application/json", icon=":material/download:")
            else:
                st.info("该快照暂无可导出数值证据。")
        except Exception:
            st.info("证据抽屉渲染失败（未覆盖）。")



def main() -> None:
    style()
    if not service_contract_ready():
        return
    demo_prewarm_autostart()
    account_topbar()
    catalog = sorted(
        cached_research_catalog(),
        key=lambda item: (str(item.get("research_as_of", "")), str(item.get("id", ""))),
        reverse=True,
    )
    with st.sidebar:
        st.markdown(
            '<div class="brand-block"><div class="brand">◈ FINTRACE / CN</div><div class="brand-sub">A-SHARE RESEARCH OS <span>·</span> LOCAL SNAPSHOTS</div></div>',
            unsafe_allow_html=True,
        )
        choices = {snapshot_choice_label(item): item["id"] for item in catalog}
        # Keep local reload, online refresh and version selection in one visual
        # group.  They all answer the same user question: which snapshot is
        # the research workspace currently using?
        with st.expander("快照管理", expanded=True):
            autoselect_id = st.session_state.pop("online_refresh_autoselect", None)
            if autoselect_id:
                labels_by_snapshot_id = {item["id"]: snapshot_choice_label(item) for item in catalog}
                if autoselect_id in labels_by_snapshot_id:
                    st.session_state["snapshot_picker"] = labels_by_snapshot_id[autoselect_id]

            if st.session_state.get("snapshot_picker") not in choices:
                st.session_state.pop("snapshot_picker", None)
            selected_label = st.selectbox(
                "当前研究版本",
                list(choices),
                key="snapshot_picker",
                format_func=snapshot_picker_label,
            )
            selected_item = next(item for item in catalog if item["id"] == choices[selected_label])
            selected_origin = (
                "联网更新版本" if selected_item.get("snapshot_origin") == "online_refresh" else "本地基准版本"
            )
            selected_close_date = selected_item.get("latest_raw_close_date") or "未覆盖"
            st.caption(
                f"当前：{selected_origin} · 收盘数据截至 {selected_close_date} · "
                f"研究截至 {str(selected_item.get('research_as_of', ''))[:10]}"
            )
            st.button(
                "切回本地基准",
                key="refresh_snapshot_catalog",
                width="stretch",
                icon=":material/history:",
                on_click=switch_to_local_baseline,
                args=(choices[selected_label],),
            )
            # The detailed network controls stay available without making the
            # ordinary sidebar state a long form.
            with st.popover("联网更新", use_container_width=True):
                st.caption("仅更新最新 RAW 收盘价；成功后自动创建新版本。")
                online_options = online_refresh_symbols(catalog)
                if not online_options:
                    st.info("暂无可更新的标的。")
                else:
                    online_label = st.selectbox("更新标的", list(online_options), key="online_refresh_symbol")
                    online_bars_only = st.checkbox(
                        "仅更新收盘价（复用已披露财务）", value=True, key="online_refresh_bars_only",
                        help="勾选时只拉取最新 RAW 日线收盘；财务仍使用截至研究日已披露的数据。",
                    )
                    if st.button("获取最新收盘价", key="refresh_snapshot_online", width="stretch", icon=":material/cloud_download:"):
                        with st.spinner("正在联网获取最新收盘行情…"):
                            try:
                                outcome = service.refresh_snapshot(
                                    online_options[online_label], bars_only=online_bars_only,
                                )
                            except Exception as refresh_exc:  # 结构化上报，不静默吞掉
                                outcome = {
                                    "refresh_status": "failed", "reason_code": "unexpected_error",
                                    "message": f"刷新调用异常：{type(refresh_exc).__name__}: {refresh_exc}",
                                    "retryable": False, "current_snapshot": None,
                                }
                        severity, notice_text = describe_refresh_outcome(outcome)
                        st.session_state["online_refresh_notice"] = {
                            "symbol": outcome.get("symbol") or online_options[online_label],
                            "severity": severity,
                            "text": notice_text,
                        }
                        if outcome.get("refresh_status") == "succeeded" and outcome.get("snapshot_id"):
                            service.load_snapshot.cache_clear()
                            cached_research_catalog.clear()
                            load_research.clear()
                            st.cache_data.clear()
                            st.session_state["online_refresh_autoselect"] = outcome["snapshot_id"]
                        st.rerun()

            online_notice = st.session_state.pop("online_refresh_notice", None)
            if online_notice:
                severity, notice_text = scoped_refresh_notice(
                    online_notice, current_symbol=selected_item["symbol"]
                )
                {"success": st.success, "info": st.info,
                 "warning": st.warning, "error": st.error}[severity](notice_text)
            local_baseline_notice = st.session_state.pop("local_baseline_notice", None)
            if local_baseline_notice:
                st.info(local_baseline_notice)
            st.button(
                "查看研究总览",
                key="open_selected_snapshot",
                width="stretch",
                icon=":material/visibility:",
                on_click=_select_page,
                args=("研究总览",),
            )
        page = sidebar_navigation()
        demo_prewarm_controls()
        with st.expander("怎么判断有没有问题", expanded=False):
            st.markdown(
                """<div class="sidebar-guide">
                <div class="guide-title">先看颜色，再看原因</div>
                <div class="guide-row"><span class="guide-dot guide-green"></span><b>绿色 / 通过</b><span>可以继续阅读和交付。</span></div>
                <div class="guide-row"><span class="guide-dot guide-yellow"></span><b>黄色 / 警告</b><span>数据可读，但不要形成强结论。</span></div>
                <div class="guide-row"><span class="guide-dot guide-red"></span><b>红色 / 阻断</b><span>关键输入不足，估值或报告不能使用。</span></div>
                <hr>
                <div class="guide-note"><b>可选字段缺失</b>：例如 EBIT、EBITDA、FCF，不影响当前核心估值。</div>
                <div class="guide-note"><b>核心字段缺失</b>：例如收入、归母净利润、总股本，会影响结论，需要处理。</div>
                <div class="guide-note"><b>未来披露</b>：晚于研究截止时间的数据，系统会自动排除。</div>
                </div>""",
                unsafe_allow_html=True,
            )
        st.divider()
        st.caption("RESEARCH TERMINAL v0.3 · 多快照 · 只读工作台")
    if page == "案例演示":
        demo_cases(catalog)
        return
    if page == "AI Agent 研究":
        agent_research(choices[selected_label])
        return
    if page == "文档智能":
        documents()
        return
    if page == "财务分析":
        financial_analysis()
        return
    if page == "每日复盘":
        daily_review()
        return
    if _DEMO_IMPORT_ERROR:
        st.warning(f"CARD-15 Demo 页面未能挂载：{_DEMO_IMPORT_ERROR}")
    if page in _DEMO_RENDER:
        # Demo 页面自带语料选择器，不走快照选择器 —— 它们分析的是文档与 run，
        # 不是行情快照。所以在这里早返回，不调用 load_research。
        _DEMO_RENDER[page]()
        return
    snapshot, metadata = load_research(choices[selected_label])
    header(snapshot, metadata)
    # The overview begins with research quality and its plain-language
    # explanation.  Cross-module navigation stays available on detail pages.
    if page != "研究总览":
        research_next_steps(page)
    {"研究总览": lambda: overview(snapshot, metadata), "市场与行情": lambda: market(snapshot), "财务表现": lambda: financials(snapshot), "同行估值": lambda: valuation_safe(snapshot, metadata), "证据与校验": lambda: evidence(snapshot, metadata), "Agent 执行轨迹": lambda: trace(snapshot), "研究报告": lambda: report(snapshot), "研究任务": tasks, "评测与消融": evaluations}[page]()


if __name__ == "__main__":
    main()
