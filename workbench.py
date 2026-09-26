"""Read-only FinTrace-CN research workbench for local versioned snapshots.

Run: .venv\\Scripts\\streamlit run workbench.py
"""

from __future__ import annotations

import json
import os
import importlib
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
    ("开始", ("案例演示", "AI Agent 研究")),
    ("研究分析", ("研究总览", "市场与行情", "财务表现", "财务分析", "同行估值")),
    ("证据与交付", ("证据与校验", "文档智能", "研究报告", "Agent 执行轨迹")),
    ("任务与评测", ("研究任务", "每日复盘", "评测与消融")),
    ("实验室", tuple(page.label for page in _DEMO_PAGES)),
]


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
    # external deep links.  It is visually hidden by the CSS in ``style``.
    # The session state is the single source of truth: callbacks set the page
    # before rerun, so passing ``index`` as a second default causes Streamlit's
    # default-value/session-state warning.
    st.radio("工作台", NAVIGATION, label_visibility="collapsed", key="workbench_page")

    for group_name, pages in NAV_GROUPS:
        visible_pages = [page for page in pages if page in NAVIGATION]
        if not visible_pages:
            continue
        is_active_group = current in visible_pages
        # Keep core research open; tuck long demo/diagnostic lists away.
        expanded = is_active_group or group_name in {"开始", "研究分析", "证据与交付"}
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
    return current


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


@st.cache_data(ttl=300, max_entries=32)
def load_research(snapshot_id: str) -> tuple[dict, dict]:
    """Load only versioned local data; the UI remains a presentation layer."""
    snapshot = service.load_snapshot(snapshot_id)
    summary = service.research_summary(snapshot_id)
    return snapshot, summary


@st.cache_data(ttl=300, max_entries=64)
def cached_research_market(snapshot_id: str, adjustment: str = "RAW") -> dict:
    return service.research_market(snapshot_id, adjustment)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_financials(snapshot_id: str) -> dict:
    return service.research_financials(snapshot_id)


@st.cache_data(ttl=300, max_entries=128)
def cached_research_financial_trends(snapshot_id: str, period_basis: str) -> dict:
    return service.research_financial_trends(snapshot_id, period_basis)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_evidence(snapshot_id: str) -> dict:
    return service.research_evidence(snapshot_id)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_validation(snapshot_id: str) -> dict:
    return service.research_validation(snapshot_id)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_readiness(snapshot_id: str) -> dict:
    return service.research_readiness(snapshot_id)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_valuation(snapshot_id: str) -> dict:
    return service.research_valuation(snapshot_id)


@st.cache_data(ttl=300, max_entries=64)
def cached_research_overview_insights(snapshot_id: str) -> dict:
    return service.research_overview_insights(snapshot_id)


@st.cache_data(ttl=300, max_entries=64)
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


def demo_cases(catalog: list[dict[str, str]]) -> None:
    """Give an interviewer a short, truthful walkthrough before the full workbench."""
    available = {item["id"] for item in catalog}
    labels_by_id = {item["id"]: snapshot_choice_label(item) for item in catalog}
    st.markdown(
        """<div class="landing-hero">
            <div class="hero-kicker"><span class="hero-dot"></span> THE RESEARCH LOOP</div>
            <div class="landing-title">30 秒看懂 FinTrace-CN</div>
            <div class="landing-copy">从版本化快照开始，把每一个结论拆成可追溯的证据、确定性的校验和可交付的研究报告。</div>
        </div>
        <div class="flow-rail">
            <div><b>01</b><span>快照</span></div><i></i><div><b>02</b><span>证据</span></div><i></i>
            <div><b>03</b><span>Validator</span></div><i></i><div><b>04</b><span>报告 / 阻断</span></div>
        </div>""",
        unsafe_allow_html=True,
    )
    st.caption("所有内容来自本地版本化快照；页面不抓取实时行情，也不在前端计算或补全数据。")
    case_columns = st.columns(2, gap="large")
    for (name, case), column, index in zip(DEMO_CASES.items(), case_columns, ("01", "02")):
        with column:
            with st.container(border=True):
                st.markdown(f'<div class="case-index">CASE {index}</div>', unsafe_allow_html=True)
                st.subheader(name)
                st.write(case["summary"])
                st.caption(f"Snapshot ID: {case['snapshot_id']}")
                if case["snapshot_id"] not in available:
                    st.warning("演示快照生成失败，请运行 python scripts/bootstrap_demo.py 后重试。")
                else:
                    validation = cached_research_validation(case["snapshot_id"])
                    allowed = bool(validation["conclusion_allowed"])
                    status_label = "允许输出" if allowed else "已阻断"
                    st.badge(
                        status_label,
                        icon=":material/check_circle:" if allowed else ":material/block:",
                        color="green" if allowed else "red",
                    )
                    if allowed != bool(case["expected_allowed"]):
                        st.error("演示快照的实际门禁结果与预期不一致，请先运行测试或重新生成快照。")
                    st.button(
                        "打开并查看证据链" if allowed else "查看阻断原因",
                        key=f"demo_{case['snapshot_id']}",
                        on_click=open_demo_case,
                        args=(labels_by_id[case["snapshot_id"]], case["next_page"]),
                    )
    st.info("成功案例用于说明可复现研究如何交付；阻断案例用于说明系统宁可不给结论，也不以缺失数据凑出答案。", icon=":material/shield:")


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
section[data-testid="stSidebar"] [data-baseweb="select"] > div {{
  background: #162A47 !important;
  border: 1px solid #2C476B !important;
  border-radius: 10px !important;
  color: #FFFFFF !important;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"] > div {{
  background: #162A47 !important; border: 1px solid #2C476B !important;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"] * {{ color: #FFFFFF !important; }}
section[data-testid="stSidebar"] [data-baseweb="select"] span {{ color: #FFFFFF !important; }}
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
  padding: .25rem 0 .2rem;
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
</style>""",
        unsafe_allow_html=True,
    )


def header(snapshot: dict, metadata: dict) -> None:
    profile = snapshot["data"]["profile"]
    validation = cached_research_validation(snapshot["snapshot_id"])
    validation_class = {"pass": "verified", "warning": "warning", "blocked": "blocked"}[validation["status"]]
    validation_label = {"pass": "校验通过", "warning": "校验警告", "blocked": "校验阻断"}[validation["status"]]
    left, right = st.columns([7, 2], vertical_alignment="center")
    with left:
        st.markdown(
            f'''<div class="hero-shell">
                <div class="hero-kicker"><span class="hero-dot"></span> FINTRACE / CN · VERSIONED RESEARCH</div>
                <div class="hero-title">{profile['name']} <span>·</span> {snapshot['symbol']}</div>
                <div class="hero-subtitle">{profile["industry_name"]} <span class="hero-sep">/</span> {profile["currency"]} <span class="hero-sep">/</span> 研究截至 {_display_datetime(snapshot["research_as_of"])}</div>
            </div>''',
            unsafe_allow_html=True,
        )
    with right:
        st.download_button("导出研究元数据", json.dumps(metadata, ensure_ascii=False, indent=2), f"{snapshot['symbol']}-research-metadata.json", "application/json", width="stretch")
    st.markdown(f'''<div class="statusbar"><span class="badge snapshot">▣ 版本化快照</span><span class="badge {validation_class}">● {validation_label}</span><span class="badge neutral">Provider · {snapshot["provider"]}</span><span class="badge neutral">Snapshot · {snapshot["snapshot_id"]}</span><span class="badge neutral">口径 · RAW / CNY</span></div>''', unsafe_allow_html=True)


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
    st.markdown("### 研究总览")
    security_status(snapshot["snapshot_id"])
    readiness = cached_research_readiness(snapshot["snapshot_id"])
    peer = cached_research_valuation(snapshot["snapshot_id"])
    evidence_count = len(cached_research_evidence(snapshot["snapshot_id"]).get("records", []))
    report_ready = bool(_report_candidates(snapshot))
    readiness_checks = readiness.get("checks", {})
    delivery_rows = [
        (
            "关键输入",
            readiness_checks.get("key_metrics", {}).get("status", "unknown"),
            readiness_checks.get("key_metrics", {}).get("detail", "未覆盖"),
            "证据与校验",
        ),
        (
            "证据校验",
            validation.get("status", "unknown"),
            f"Evidence Ledger · {evidence_count} 条记录",
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
        "warning": ("有限", "orange"), "limited": ("有限", "orange"),
        "blocked": ("阻断", "red"), "unknown": ("未测量", "gray"),
    }
    st.markdown("### 研究交付状态")
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
    st.markdown("### 新手研究摘要")
    readiness_label = {"ready": "可用于基础研究", "limited": "可用但存在限制", "blocked": "关键数据不足"}[readiness["status"]]
    st.markdown(f"**研究就绪度：{readiness_label}**")
    with st.expander("查看数据覆盖与限制", expanded=readiness["status"] != "ready"):
        labels = {"snapshot_freshness": "快照时效", "annual_financial_coverage": "年度财报覆盖", "key_metrics": "关键估值输入", "peer_coverage": "同业样本"}
        rows = [{"检查项": labels[name], "状态": check["status"], "说明": check["detail"]} for name, check in readiness["checks"].items()]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption("warning 表示可继续阅读但不应据此作强结论；blocked 表示缺少关键数据。")
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
    peer = cached_research_valuation(snapshot["snapshot_id"])
    if peer.get("method_boundary") == "financial_institution_pe_pb_only":
        st.warning("金融机构不适用通用企业估值框架；当前仅展示 PE/PB，隐藏 PS、EV/EBITDA 和通用企业 DCF。")
    if not peer.get("available"):
        st.warning(f"当前无法进行同行估值：{_peer_unavailable_message(peer)}。")
        if peer.get("remediation"):
            st.caption(peer["remediation"])
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
    st.markdown("### 证据与校验中心")
    evidence_result = cached_research_evidence(snapshot["snapshot_id"])
    validation = cached_research_validation(snapshot["snapshot_id"])
    records = evidence_result["records"]
    checks = validation.get("checks", [])
    status_labels = {"pass": "通过", "warning": "有限", "blocked": "阻断"}
    status_value = status_labels.get(validation.get("status"), "未测量")
    passed_count = sum(check.get("status") == "pass" for check in checks)
    attention_count = sum(check.get("status") != "pass" for check in checks)
    summary_columns = st.columns(4)
    summary_columns[0].metric("综合状态", status_value)
    summary_columns[1].metric("Evidence 记录", len(records))
    summary_columns[2].metric("已通过检查", passed_count)
    summary_columns[3].metric("待处理检查", attention_count)
    attention = [check for check in checks if check.get("status") != "pass"]
    if attention:
        with st.container(border=True):
            st.markdown("#### 当前需要关注")
            for check in attention:
                label = status_labels.get(check.get("status"), "未测量")
                st.markdown(
                    f"**{check.get('label', check.get('code', '检查项'))} · {label}**："
                    f"{check.get('detail', '未提供详情')}"
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
        st.markdown("#### Validator 清单")
        for check in validation["checks"]:
            if check["status"] == "pass":
                message = f"**{check['label']}** — 已通过：{check['detail']}"
            elif check["status"] == "warning":
                message = (
                    f"**{check['label']}** — 研究范围限制：{check['detail']}\n\n"
                    f"当前影响：不阻断已验证事实和基础报告；涉及该项的扩展结论会降级或不展示。\n\n"
                    f"如需扩展范围：{check['remediation']}"
                )
            else:
                message = (
                    f"**{check['label']}** — 关键阻断：{check['detail']}\n\n"
                    f"需要处理：{check['remediation']}"
                )
            if check["status"] == "pass":
                st.success(message)
            elif check["status"] == "warning":
                st.warning(message)
            else:
                st.error(message)
        st.caption(f"综合状态：{validation['status']} · 允许确定性结论：{validation['conclusion_allowed']} · 研究截至：{validation['research_as_of']}")


def _trace_legacy(snapshot: dict) -> None:
    st.markdown("### Agent 执行轨迹")
    st.caption("仅呈现已记录的研究流程摘要；外部工具内容始终按数据处理。")
    events = [("01", "解析研究对象", f"{snapshot['symbol']} · A 股实体解析", "成功", "—"), ("02", "加载版本化快照", f"{snapshot.get('provider', 'snapshot')} / {snapshot['snapshot_id']}", "成功", "—"), ("03", "确定性计算", "TTM 与市场价值", "成功", "—"), ("04", "Evidence Ledger", "生成事实与计算依赖", "成功", "—")]
    for n, title, detail, status, duration in events:
        st.markdown(f"<div class='trace'><span class='badge verified'>{n} · {status}</span> <b>{title}</b> <span class='subline'>— {detail} · {duration}</span></div>", unsafe_allow_html=True)


def trace(snapshot: dict) -> None:
    """Render only persisted task events; legacy snapshots remain explicitly empty."""
    result = service.research_trace(snapshot["snapshot_id"])
    st.markdown("### Agent 执行轨迹")
    if not result["available"]:
        st.info("该快照没有已持久化的执行轨迹；不会补造成功步骤。")
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


def report(snapshot: dict) -> None:
    st.markdown("### 中文研究报告")
    candidates = _report_candidates(snapshot)
    if not candidates:
        st.info("该快照尚未生成独立研究报告。请使用受控离线研究流程生成后再查看。")
        return
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


def agent_research() -> None:
    """Real LLM tool-calling agent research: submit, poll, and review the trace,
    Evidence, Validator result and cost — all from the shared CnResearchAgent."""
    st.markdown("### AI Agent 研究")
    st.caption(
        "自然语言问题 → 后台真实执行共享 CnResearchAgent → 展示工具轨迹、Evidence 与 Validator 结果。"
        "快照是版本化历史数据，不是实时行情。未配置所选模型的 API Key 时，系统会明确报错而不会自动降级为 mock。"
    )
    catalog = service.list_research()
    if not catalog:
        st.info("尚无可用快照。")
        return
    labels = {f"{item['name']} · {item['symbol']}": item for item in catalog}
    default_label = next(
        (label for label in labels if label == st.session_state.get("snapshot_picker")),
        list(labels)[0],
    )
    selected_label = st.session_state.get("agent_snapshot_choice", default_label)
    selected_item = labels.get(selected_label, labels[default_label])
    default_cutoff = (selected_item.get("research_as_of") or selected_item.get("as_of") or "")[:19]

    with st.form("agent_research_form", border=True):
        query = st.text_area(
            "研究问题",
            placeholder="例如：分析 601012.SH 最新财报的收入、净利润和总资产变化，并引用证据。",
            help="问题中的日期不会被自动解析为 cutoff；若需要指定研究截止时点，请填写右侧 Research cutoff。",
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            st.selectbox("研究快照", list(labels), index=list(labels).index(default_label), key="agent_snapshot_choice")
        with c2:
            st.text_input("证券代码", value=selected_item["symbol"], disabled=True, key="agent_symbol_display")
        with c3:
            cutoff = st.text_input(
                "Research cutoff",
                value=default_cutoff,
                placeholder="例如 2025-04-30T23:00:00",
                help="留空则使用所选快照自带的研究时点。若问题要求分析某个历史时点，请在此填写该日期。",
            )
        c4, c5, c6 = st.columns(3)
        with c4:
            model_name = st.text_input("模型", value="deepseek-v4-flash")
        with c5:
            max_steps = st.number_input("Max steps", min_value=1, max_value=10, value=6, step=1)
        with c6:
            temperature = st.number_input("Temperature", min_value=0.0, max_value=1.0, value=0.0, step=0.1)
        st.caption(
            "提示：问题里写的日期只给模型阅读，**不会**自动变成系统 cutoff；如需指定时点，请在 Research cutoff 中填写。"
            "快照通常只含最近约 30 个交易日的价格 K 线，若问‘2025-04-30 的市盈率’而快照里没有该日价格，会触发 tool_error。"
        )
        run = st.form_submit_button("运行真实 Agent", icon=":material/play_arrow:", type="primary", width="content")
    if run:
        item = labels[st.session_state["agent_snapshot_choice"]]
        try:
            request = agent_service.AgentResearchRequest(
                query=query.strip(), symbol=item["symbol"], snapshot_id=item["id"],
                research_as_of=(cutoff.strip() or None), model_name=model_name.strip(),
                temperature=float(temperature), max_steps=int(max_steps),
            )
            task = agent_service.start_agent_research(request)
            st.session_state["agent_task_id"] = task["id"]
            st.rerun()
        except agent_service.AgentResearchError as exc:
            st.error(f"无法启动 Agent：{exc}")
        except KeyError as exc:
            st.error(f"快照不存在：{exc}")

    agent_task_view()


@st.fragment(run_every="10s")
def agent_task_view() -> None:
    """Poll one tracked agent task and render its result tabs once terminal."""
    task_id = st.session_state.get("agent_task_id")
    if not task_id:
        st.caption("提交问题后，这里会实时展示 Agent 的执行状态、工具轨迹与验证结果。")
        return
    try:
        public = agent_service.get_agent_task(task_id)
        stream = agent_service.agent_task_events(task_id)
    except KeyError:
        st.info("该任务记录已不存在。")
        return

    status = public.get("status", "unknown")
    st.markdown(f"#### 任务 `{task_id}`")
    st.caption(f"状态：**{status}** · 当前步骤：{public.get('current_step') or '—'} · 更新于 {public.get('updated_at') or '—'}")
    progress = service.get_task_progress(task_id).get("progress_percent")
    st.progress(int(progress or 0), text=public.get("message") or status)

    if status in {"queued", "running"}:
        if st.button("取消任务", icon=":material/cancel:", key=f"cancel_agent_{task_id}"):
            with st.spinner("正在请求协作式停止…"):
                try:
                    st.info(service.cancel_task(task_id)["message"])
                except ValueError as exc:
                    st.error(f"无法取消：{exc}")
            st.rerun()
        return

    tabs = st.tabs(["Final Answer", "Agent Trace", "Evidence", "Validator", "Cost & Metadata"])
    with tabs[0]:
        _agent_final_answer(public, status)
    with tabs[1]:
        _agent_trace(public, stream)
    with tabs[2]:
        _agent_evidence(public)
    with tabs[3]:
        _agent_validator(public)
    with tabs[4]:
        _agent_cost(public)


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
                "Step": index,
                "Tool": trace.get("tool_name"),
                "Arguments": args_text,
                "Status": trace.get("result_status"),
                "Evidence": ", ".join(trace.get("evidence_ids", []) or []),
                "Duration (s)": _seconds_between(trace.get("started_at"), trace.get("ended_at")),
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
    st.caption(f"已注册 Evidence：{len(facts)} 个事实 · {len(calculations)} 个计算")
    if facts:
        st.markdown("**Facts**")
        st.code("\n".join(facts), language=None)
    if calculations:
        st.markdown("**Calculations**")
        st.code("\n".join(calculations), language=None)
    if not facts and not calculations:
        st.caption("没有可展示的 Evidence。")


def _agent_validator(public: dict) -> None:
    validation = public.get("validation") or {}
    valid = bool(validation.get("valid"))
    if public.get("status") == "blocked" or not valid:
        st.error("BLOCKED")
        errors = validation.get("errors") or []
        if errors:
            st.markdown("阻断原因：\n" + "\n".join(f"- `{error}`" for error in errors))
        else:
            st.write("Validator 未通过；无详细错误。")
    else:
        st.success("PASSED")
        st.write("结论已通过确定性 Validator（无需 LLM 复判金融数字）。")


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
        ("Max steps", public.get("max_steps")), ("快照", public.get("snapshot_id")),
        ("Research cutoff", public.get("research_as_of")),
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
    catalog = sorted(
        service.list_research(),
        key=lambda item: (str(item.get("research_as_of", "")), str(item.get("id", ""))),
        reverse=True,
    )
    with st.sidebar:
        st.markdown(
            '<div class="brand-block"><div class="brand">◈ FINTRACE / CN</div><div class="brand-sub">A-SHARE RESEARCH OS <span>·</span> LOCAL SNAPSHOTS</div></div>',
            unsafe_allow_html=True,
        )
        choices = {snapshot_choice_label(item): item["id"] for item in catalog}
        if st.button("刷新本地快照", key="refresh_snapshot_catalog", width="stretch", icon=":material/refresh:"):
            service.load_snapshot.cache_clear()
            load_research.clear()
            st.cache_data.clear()
            st.session_state.pop("snapshot_picker", None)
            st.rerun()
        if st.session_state.get("snapshot_picker") not in choices:
            st.session_state.pop("snapshot_picker", None)
        selected_label = st.selectbox("研究快照", list(choices), key="snapshot_picker")
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
        agent_research()
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
    {"研究总览": lambda: overview(snapshot, metadata), "市场与行情": lambda: market(snapshot), "财务表现": lambda: financials(snapshot), "同行估值": lambda: valuation_safe(snapshot, metadata), "证据与校验": lambda: evidence(snapshot, metadata), "Agent 执行轨迹": lambda: trace(snapshot), "研究报告": lambda: report(snapshot), "研究任务": tasks, "评测与消融": evaluations}[page]()


if __name__ == "__main__":
    main()
