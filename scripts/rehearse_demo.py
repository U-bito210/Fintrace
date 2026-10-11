#!/usr/bin/env python
"""DEMO_FLOW.md 剧本自检 —— 把「5 分钟彩排」从主观走查变成能跑的清单。

用法::

    python scripts/rehearse_demo.py             # 打印自检报告
    python scripts/rehearse_demo.py --strict    # 存在「会卡」步骤则退出码 1
    python scripts/rehearse_demo.py --step 6+7  # 只看某一步

判据只有一条：**每一步「评委看到什么」，现在能不能真的取到数据。**
取不到就是会卡，如实报 BLOCKED，不替它圆。

服务层没有接口的步骤标 MANUAL —— 那是要人工走查的，**不假装查过**。
这是本项目「零静默」的底线：宁可写「没查」，也不写「通过」。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configure_utf8_stdio() -> None:
    """Keep the rehearsal report readable on Windows' default GBK console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Embedded runners may expose a stream that cannot be reconfigured;
            # the checks themselves must remain usable in that environment.
            continue

# --------------------------------------------------------------------------
# 状态。四种，不多不少。
# --------------------------------------------------------------------------

READY = "ready"        # 有数据可讲
DEGRADED = "degraded"  # 能讲，但必须如实声明边界（部分缺失 / 降级 / 替代件）
BLOCKED = "blocked"    # 拿不到数据，现场会卡
MANUAL = "manual"      # 服务层无接口，只能人工走查

_SYMBOL = {
    READY: "[可讲]      ",
    DEGRADED: "[可讲·声明]",
    BLOCKED: "[会卡]      ",
    MANUAL: "[待人工]    ",
}


@dataclass
class Result:
    state: str
    lines: List[str] = field(default_factory=list)

    def add(self, *lines: str) -> "Result":
        self.lines.extend(lines)
        return self


@dataclass
class Step:
    step_id: str
    title: str
    window: str
    page: str
    cards: str
    check: Callable[[], Result]


# --------------------------------------------------------------------------
# 剧本常量（抄自 DEMO_FLOW.md，不自己编）
# --------------------------------------------------------------------------

# §1 演示资产：剧本 §0.1 要求「只用真实材料」
DEMO_ASSETS: Tuple[Path, ...] = (
    PROJECT_ROOT / "data" / "demo" / "annual_report.pdf",
    PROJECT_ROOT / "data" / "demo" / "report_draft.md",
)

# §2 含错草稿 8 处注入清单。右列是 checker 实际输出的 check_type 提示名；
# 名字可能对不上（剧本设计名 vs 实现名），对不上就如实列出来，不去猜等价。
PLANTED_ERROR_PLAN: Tuple[Tuple[str, str, str], ...] = (
    ("E1", "numeric_error", "营业收入写 352,012,554 千元（年报为 362,012,554 千元）"),
    ("E2", "numeric_scale_error", "归母净利润写 31,000,000 亿元（年报为 50,744,682 千元，量级错误）"),
    ("E3", "calculation_error", "2024 年营业收入同比写成增长 15%（年报两期复算约下降 9.70%）"),
    ("E4", "valuation_multiple_error", "目标 PE 用了银行业可比值"),
    ("E5", "wrong_citation", "营业收入引用『年报第 45 页』（对应页由证据定位）"),
    ("E6", "period_error", "把 2023 年研发费用约 183.56 亿元写成 2024 年数值"),
    ("E7", "unsupported_causal_claim", "『利润增长主要因海外订单放量』无证据"),
    ("E8", "scope_error", "母公司口径数据当成合并口径使用"),
)

REQUIRED_PLANTED = len(PLANTED_ERROR_PLAN)  # §5「8/8 检出方可上台」

#: 步骤 6+7 在剧本里占 1:35–2:25（50 秒）。真实材料首次构建要解析整份年报，
#: 超过这个窗口就必须提前预热 —— 这个数字是给「现场要不要等」用的判据。
CORRECTIONS_WINDOW_SECONDS = 50

# §3 创新展示步骤（4b / 7b / 10b / E2）
INNOVATION_STEPS = ("4b", "7b", "10b")

PASSPORT_FIVE_STATES = ("VERIFIED", "DEGRADED", "STALE", "BLOCKED", "CONFLICTED")


def _svc():
    from src.cn import demo_service

    return demo_service


def _status(payload: Dict[str, Any]) -> str:
    return str(payload.get("status") or "")


def _ok(payload: Dict[str, Any]) -> bool:
    return _status(payload) == "ok"


# --------------------------------------------------------------------------
# 各步骤自检
# --------------------------------------------------------------------------


def check_upload() -> Result:
    """步骤 1+2：上传真实年报 PDF + 含错研报草稿。"""
    service = _svc()
    missing = [p for p in DEMO_ASSETS if not p.exists()]
    if missing:
        result = Result(
            BLOCKED,
            ["剧本 §0.1「只用真实材料」未满足，以下资产不存在："],
        )
        for path in missing:
            result.add(f"    缺 {path.relative_to(PROJECT_ROOT)}")
        result.add("    → 现场开场无真实材料可上传，这一步直接卡住。")
    else:
        result = Result(READY, ["两份资产就位："])
        for path in DEMO_ASSETS:
            result.add(f"    ✓ {path.relative_to(PROJECT_ROOT)}")
        result.lines.extend(_text_pdf_precheck(DEMO_ASSETS[0]))

    # 兜底：说明当前实际能演示的是什么
    filings = service.filing_cases()
    drafts = service.draft_cases()
    real = [item for item in drafts if item.get("source") == "data/demo"]
    synthetic = [item for item in drafts if item.get("source") != "data/demo"]
    result.add(
        f"    草稿清单：{len(real)} 份真实材料 + {len(synthetic)} 份合成件"
        f"（合成件共 {len(drafts)} 份）"
    )
    if not real:
        result.add(
            f"    ⚠ 真实材料未接入草稿清单：{len(filings)} 份年报 + {len(synthetic)} 份草稿"
            "**全部是合成件**（用它演示即违反 §0.1，必须在话术里声明）"
        )
    unrunnable = [item["key"] for item in drafts if not item.get("runnable", True)]
    if unrunnable:
        result.add(f"    ✗ 列出来但跑不了的草稿：{unrunnable} —— 列表里不该有跑不了的用例")
    return result


def _text_pdf_precheck(pdf: Path) -> List[str]:
    """§1 硬要求：必须是文本型 PDF。扫描版 OCR 不可用 → Demo 降级。"""
    try:
        import pdfplumber  # type: ignore
    except Exception:  # noqa: BLE001
        return ["    ⚠ 无法做文本型预检：pdfplumber 未安装"]

    try:
        with pdfplumber.open(str(pdf)) as doc:
            pages = len(doc.pages)
            sample = ""
            for page in doc.pages[:5]:
                sample += page.extract_text() or ""
    except Exception as exc:  # noqa: BLE001
        return [f"    ⚠ 文本型预检失败：{type(exc).__name__}: {exc}"]

    if not sample.strip():
        return [
            f"    ✗ {pages} 页，前 5 页抽不到文本 —— 扫描版 PDF，"
            f"§1 规定「扫描版 OCR 不可用→Demo 降级」"
        ]
    return [f"    ✓ 文本型 PDF 预检通过：{pages} 页，前 5 页抽出 {len(sample)} 字符"]


def check_parse() -> Result:
    """步骤 3+4：解析 + 提取，评委看 Execution Trace 页的事件流。"""
    service = _svc()
    view = service.replay_view()
    if not _ok(view):
        return Result(
            BLOCKED,
            [f"    回放不可用（{view.get('reason', '无原因')}）—— 步骤 3+4 无事件流可展示"],
        )

    recorded = list(view.get("step_ids") or ())
    not_recorded = list(view.get("not_recorded_steps") or ())
    wanted = ("parse", "extract", "normalize")
    absent = [s for s in wanted if s not in recorded or s in not_recorded]

    lines = [
        f"    run {str(view.get('run_id'))[:8]}｜已记录步骤 {len(recorded)} 个、"
        f"事件 {view.get('event_count')} 条"
    ]
    if not absent:
        state = READY
        lines.append("    ✓ parse → extract → normalize 三段事件流齐备")
    elif len(absent) == len(wanted):
        state = BLOCKED
        lines.append(f"    ✗ 三段全缺：{absent} —— 评委看不到解析过程")
    else:
        state = DEGRADED
        lines.append(
            f"    ⚠ 缺 {absent} —— 事件流不完整，话术不能说『完整链路』，"
            f"要如实说『本 run 未记录该阶段』"
        )
    if view.get("failed_steps"):
        lines.append(f"    ⚠ 失败步骤（如实展示，不美化）：{view['failed_steps']}")

    graph = service.graph_view()
    counts = graph.get("counts") or {}
    if _ok(graph):
        located = counts.get("evidence_located")
        total = counts.get("evidence")
        lines.append(f"    提取结果：evidence {total} 个，已定位到页 {located} 个")
        if total and located is not None and located < total:
            lines.append(
                f"    ⚠ 有 {total - located} 条证据没定位到 page/bbox —— "
                f"§0.3「每个数字可回跳」在这几条上不成立"
            )
    return Result(state, lines)


def check_acme() -> Result:
    """步骤 4b：ACME 六类约束族自检（创新展示一）。"""
    service = _svc()
    view = service.acme_view()
    if not _ok(view):
        return Result(BLOCKED, [f"    ACME 不可用（{view.get('reason', '无原因')}）"])

    families = list(view.get("constraint_families_checked") or ())
    unchecked = list(view.get("families_not_checked") or ())
    # 注意：`violations` 是**全部已评估约束结果**（含 passed / skipped_missing_field），
    # 真正的违反数在 totals.violated —— 拿 len(violations) 当违反数会把数报错。
    evaluated = list(view.get("violations") or ())
    cross_modal = list(view.get("cross_modal") or ())
    totals = view.get("totals") or {}
    violated = int(totals.get("violated") or 0)
    nonzero = {k: v for k, v in totals.items() if v} or "（全零）"

    lines = [
        f"    已检约束族 {len(families)} 类：{'、'.join(families) or '（无）'}",
        f"    已评估 {len(evaluated)} 条（含 passed/skipped）｜其中**违反 {violated} 条**",
        f"    totals（非零项）：{nonzero}",
    ]
    if not evaluated:
        return Result(
            BLOCKED, lines + ["    ✗ 零约束结果 —— 这一步『点一个违反项看详情』没东西可点"]
        )
    if violated == 0:
        return Result(
            BLOCKED,
            lines + ["    ✗ 违反数为 0 —— 没有可点开的违反项，只能展示『全部通过』"],
        )

    lines.append(f"    ✓ 有 {violated} 个违反项可点开看详情")
    if violated == 1:
        lines.append("    · 只有 1 个违反例 —— 现场没有备选，点开前先确认就是它")
    lines.append(f"    ✓ 正文 vs 表格跨模态矛盾 {len(cross_modal)} 条（剧本 4b 点名要举的例子）")
    if not cross_modal:
        lines.append("    ⚠ 跨模态矛盾 0 条 —— 4b 话术举不出『正文 vs 表格』，需换例子")

    state = READY
    if unchecked:
        state = DEGRADED
        lines.append(
            f"    ⚠ 剧本话术说「六类约束族」，实际有 {len(unchecked)} 类没跑："
            f"{'、'.join(unchecked)}"
        )
        lines.append("      → 话术要改成『本案例命中 N 类』，不能说六类全检")
    unavailable = view.get("constraints_not_available") or ()
    if unavailable:
        reasons = [f"{item.get('constraint_id')}: {item.get('reason')}" for item in unavailable]
        lines.append(f"    ⚠ 不可用约束 {len(reasons)} 条（如实列出）：{reasons[:3]}")
    return Result(state, lines)


def check_financials() -> Result:
    """步骤 5：财务分析（YoY / 单季 QoQ / 盈利质量）。走 legacy 页面。"""
    return Result(
        MANUAL,
        [
            "    服务层无对应接口，这一步在 legacy『财务分析』页，需人工走查",
            "    人工要核对：① 单季换算 100/130/160/150 是否真的算出来",
            "                ② 可比性横幅（CARD-04）是否在有口径问题时原样显示",
            "                ③ 指标表能否展开公式与 Evidence",
        ],
    )


def check_corrections() -> Result:
    """步骤 6+7：研报纠错（高潮一）。§5 硬项：8/8 检出方可上台。"""
    service = _svc()
    view = service.corrections()
    if not _ok(view):
        return Result(BLOCKED, [f"    纠错不可用（{view.get('reason', '无原因')}）"])

    planted = int(view.get("planted_count") or 0)
    findings = list(view.get("findings") or ())
    by_type = dict(view.get("by_check_type") or {})
    summary = view.get("summary") or {}
    aligned = view.get("aligned") or {}
    rows = list(aligned.get("rows") or ())
    unmatched = list(aligned.get("unmatched") or ())
    detected = sum(1 for row in rows if row["检出"])
    missed = [row for row in rows if not row["检出"]]

    lines = [
        f"    草稿：{view.get('filename')}（源 {view.get('source')}，as_of {view.get('as_of')}）",
        f"    证据材料：{view.get('reference_documents') or '（无）'}"
        + (f"，{view.get('reference_pages')} 页" if view.get("reference_pages") else ""),
        f"    植入 {planted} 处 / 检出 {len(findings)} 条 / claim {view.get('claim_count')} 条",
        f"    summary：errors={summary.get('errors')} warnings={summary.get('warnings')} "
        f"unverifiable={summary.get('unverifiable_claims')}",
    ]

    # 真实材料要解析整份年报：这一步的窗口是 1:35–2:25（50s），首次构建超过它就是
    # 现场让评委干等。数字照实报，不提速也不隐藏。
    build = float(view.get("build_seconds") or 0.0)
    lines.append(
        f"    首次构建 {build:.0f}s（步骤窗口 {CORRECTIONS_WINDOW_SECONDS}s）"
        + ("—— **现场必须先预热**" if build > CORRECTIONS_WINDOW_SECONDS else "")
    )

    # 剧本 §2 的注入清单与服务层清单必须对得上：同一份设计写两遍，迟早会漂。
    # 对的是**编号**而不是类型名 —— 剧本名是设计名，实现名可能不同（脚本自己
    # 的注释就是这么写的），类型名对不上要如实列出而不是假装等价。
    service_manifest = list(getattr(service, "DEMO_PLANTED", ()) or ())
    plan_codes = {plan[0] for plan in PLANTED_ERROR_PLAN}
    manifest_codes = {
        match.group(1)
        for item in service_manifest
        for match in (re.match(r"\s*(E\d+)", str(item.get("note") or "")),)
        if match
    }
    if len(service_manifest) != REQUIRED_PLANTED or manifest_codes != plan_codes:
        state = BLOCKED
        lines.append(
            f"    ✗ 服务层注入清单与剧本 §2 对不上：服务层 {sorted(manifest_codes)} "
            f"vs 剧本 {sorted(plan_codes)}"
        )
    elif len(rows) != REQUIRED_PLANTED:
        state = BLOCKED
        lines.append(f"    ✗ 植入清单 {len(rows)} 处 ≠ 剧本 §2 的 {REQUIRED_PLANTED} 处")
    else:
        state = READY

    # 逐条对齐：植入的每一处到底抓没抓住
    lines.append(f"    逐条对齐（按 类型 + 值片段）：检出 {detected}/{len(rows) or planted}")
    for row in rows:
        mark = "✓" if row["检出"] else "✗"
        lines.append(
            f"        {mark} {row['植入类型']}（{row['植入说明']}）→ 检出 {row['检出']} 条"
        )
    if missed:
        state = BLOCKED
        lines.append(
            f"    ✗ §5 硬项「8/8 检出方可上台」未达标：漏检 {len(missed)} 处 —— "
            "话术必须照实说，不能只报检出条数"
        )
    else:
        lines.append(f"    ✓ 植入 {len(rows)} 处全部检出")

    if unmatched:
        lines.append(
            f"    ⚠ 清单之外的检出 {len(unmatched)} 条（既不能算检出率，也不能当噪音丢掉）："
        )
        for item in unmatched[:5]:
            lines.append(
                f"        · {item.get('check_type')} [{item.get('severity')}] "
                f"原值 {item.get('original')}"
            )
        if len(unmatched) > 5:
            lines.append(f"        · …其余 {len(unmatched) - 5} 条见纠错页 ② 表")

    # E1–E8 类型覆盖（对照剧本设计名，与服务层清单相互印证）
    planned = {plan[1] for plan in PLANTED_ERROR_PLAN}
    hit = sorted(planned & set(by_type))
    not_fired = sorted(planned - set(by_type))
    extra = sorted(set(by_type) - planned)
    lines.append(
        f"    E1–E8 类型覆盖（按**剧本设计名**，实现名可能不同）："
        f"命中 {len(hit)}/{REQUIRED_PLANTED} → {'、'.join(hit) or '（无）'}"
    )
    if not_fired:
        lines.append(f"    ✗ 剧本设计了但本材料没检出的类型：{not_fired}")
        for code, kind, desc in PLANTED_ERROR_PLAN:
            if kind in not_fired:
                lines.append(f"        {code} {kind} —— {desc}")
        # 这一行读起来像漏检，但它通常只是「剧本设计名 ≠ checker 实现名」。
        # 判据是上面的「逐条对齐」，不是这个 6/8。此处只指路、不改名、不改口径：
        # 把清单类型名改成实现名来凑分，正是 DEMO_FLOW §2 明文禁止的做法。
        lines.append(
            "    ↑ 若「逐条对齐」已是 8/8，本行不代表漏检。设计名与实现名的差异已登记并"
            "逐条论证，见 DEMO_FLOW.md §2（E2 为何是 numeric_error 而非 unit_error；"
            "E5 为何是 wrong_page 而非 wrong_citation）。"
        )
    if extra:
        lines.append(f"    · 剧本清单外还检出了：{extra}（不是错，如实记录）")

    uncertain = view.get("uncertain") or {}
    if uncertain:
        lines.append(
            f"    ⚠ 抽不出的句子 {uncertain.get('unextracted')} 条、"
            f"抽得出但没 claim 的 {uncertain.get('no_claim')} 条"
        )
    return Result(state, lines)


def check_passport() -> Result:
    """步骤 7b：Financial Claim Passport + VERIFY CLAIM（核心技术动作）。"""
    service = _svc()
    passport = service.passport_view()
    if not _ok(passport):
        return Result(BLOCKED, [f"    护照不可用（{passport.get('reason', '无原因')}）"])

    selectable = list(passport.get("selectable") or ())
    claim_id = passport.get("claim_id") or ""
    proofs = passport.get("passport") or {}

    verified = service.verify_view(claim_id=claim_id)
    overall = str(verified.get("overall_status") or "")
    checks = list(verified.get("checks") or ())

    lines = [
        f"    可切换的 claim {len(selectable)} 条，当前 {claim_id}",
        f"    护照证明块 {len([k for k in proofs if k.endswith('_proof')])} 个",
        f"    VERIFY CLAIM → {overall}（{len(checks)} 项逐条检查）",
        f"    引擎：{verified.get('engine', '')}",
    ]
    if not selectable:
        return Result(BLOCKED, lines + ["    ✗ 没有可点的 claim，这一步做不了"])
    state = READY
    if overall not in PASSPORT_FIVE_STATES:
        state = DEGRADED
        lines.append(f"    ⚠ 验证态 {overall!r} 不在五态 {PASSPORT_FIVE_STATES} 内")
    if not checks:
        state = BLOCKED if state != BLOCKED else state
        lines.append("    ✗ 零检查项 —— 展示不出『逐检查项明细』")
    reasons = list(verified.get("status_reasons") or ())
    if reasons:
        lines.append(f"    原因：{reasons}")
    return Result(state, lines)


def check_valuation() -> Result:
    """步骤 8：相对估值 + 敏感性分析。"""
    snapshots = sorted((PROJECT_ROOT / "data" / "snapshots" / "cn").glob("*.json"))
    tushare = [p for p in snapshots if p.stem.endswith("_tushare_v1")]
    lines = [
        "    服务层无对应接口，这一步在 legacy『估值』页，需人工走查",
        f"    快照现状：{len(snapshots)} 个（其中真实 tushare 快照 {len(tushare)} 个）",
    ]
    # 快照是否重建是环境状态，逐次判定，不写死结论——否则补完材料后脚本还在报错。
    if tushare:
        lines.append(f"    ✓ 真实行情快照已重建（{len(tushare)} 个），同业可比数据齐备")
    else:
        lines.append("    ✗ 无真实 tushare 快照（.env 的 TUSHARE_TOKEN 未配置或采集未跑）")
    lines.append(
        "    ⚠ 剧本 §1 要求『演示公司对应的行情/股本 snapshot』："
        "现有 tushare 快照对应真实 A 股，演示公司的 snapshot 仍缺"
    )
    lines.append("    → 若走合成快照，话术必须声明『数字为示例，非真实行情』")
    return Result(MANUAL, lines)


def check_memo_graph() -> Result:
    """步骤 9+10：备忘录 + Claim→Calculation→Evidence→Page 逐层回溯。"""
    service = _svc()
    view = service.graph_view()
    if not _ok(view):
        return Result(BLOCKED, [f"    图谱不可用（{view.get('reason', '无原因')}）"])

    claims = list(view.get("claim_rows") or ())
    nodes = list(view.get("nodes") or ())
    edges = list(view.get("edges") or ())
    counts = view.get("counts") or {}

    lines = [
        f"    节点 {len(nodes)} / 边 {len(edges)}｜"
        f"claims {counts.get('claims')} calculations {counts.get('calculations')} "
        f"evidence {counts.get('evidence')}",
    ]
    if not claims:
        return Result(BLOCKED, lines + ["    ✗ 无 claim 可点，『点击结论逐层展开』做不了"])

    lines.append(f"    ✓ 可点开的 claim {len(claims)} 条")
    located = counts.get("evidence_located") or 0
    total = counts.get("evidence") or 0
    state = READY
    if total and located < total:
        state = DEGRADED
        lines.append(
            f"    ⚠ {total - located}/{total} 条证据没定位到页 —— "
            f"回溯到第 4 层（PDF 页）时这些链会断，需如实说明"
        )
    if view.get("stale_claims"):
        lines.append(f"    ⚠ stale claim {len(view['stale_claims'])} 条（如实展示）")
    if view.get("deactivations"):
        lines.append(f"    ⚠ 已失效节点 {len(view['deactivations'])} 条（如实展示）")
    return Result(state, lines)


def check_fragility() -> Result:
    """步骤 10b：Thesis Fragility Map + Monitoring Plan（创新展示三）。"""
    service = _svc()
    view = service.fragility_view()
    if not _ok(view):
        return Result(BLOCKED, [f"    脆弱性不可用（{view.get('reason', '无原因')}）"])

    cuts = list(view.get("minimal_cut_sets") or ())
    monitoring = list(view.get("monitoring_metrics") or ())
    required = list(view.get("required_dependencies") or ())
    thesis_source = view.get("thesis_source") or ""
    thresholds = list(view.get("numeric_thresholds") or ())

    lines = [
        f"    thesis_source = {thesis_source}",
        f"    最小割集 {len(cuts)} 个｜REQUIRED 依赖 {len(required)} 条｜"
        f"Monitoring Plan {len(monitoring)} 条",
    ]
    if not cuts or not monitoring:
        return Result(
            BLOCKED, lines + ["    ✗ 割集或 Monitoring Plan 为空，这一步没东西可展示"]
        )

    lines.append("    ✓ Fragility Map 与 Monitoring Plan 都有内容可点")
    state = READY
    if thesis_source == "declared":
        state = DEGRADED
        lines.append(
            f"    ⚠ Thesis 是**显式声明**的，不是从语料推断出来的："
            f"{view.get('thesis_source_note', '')}"
        )
        lines.append(
            "      → 话术要说『本案例显式声明投资逻辑』，"
            "不能说『系统自动从研报里识别出投资逻辑』"
        )
    if not thresholds:
        lines.append(
            f"    ⚠ 数值型阈值 0 条（引擎已给 warning："
            f"{'; '.join(view.get('warnings') or []) or '无'}）—— "
            f"Monitoring Plan 只有结构性触发，没有 KPI 阈值触发"
        )
    rejected = view.get("rejected_thresholds") or ()
    if rejected:
        lines.append(f"    ⚠ 被拒阈值 {len(rejected)} 条（如实列出）")
    return Result(state, lines)


def check_validator() -> Result:
    """步骤 11：删掉关键 Evidence → Validator 阻断（高潮三，剧本终点）。"""
    service = _svc()
    view = service.graph_view()
    evidence = (view.get("counts") or {}).get("evidence") if _ok(view) else None
    lines = [
        "    这是**现场交互动作**（手动删一条 evidence），服务层无只读接口，需人工走查",
    ]
    if evidence:
        lines.append(f"    可操作的证据节点：{evidence} 条 —— 有料可删")
    lines.extend(
        [
            "    人工要核对：① 删掉后对应 Claim 是否变为 invalid（不是悄悄保留）",
            "                ② 备忘录对应结论是否被阻断",
            "                ③ 话术『错误的时候知道自己不能下结论』与画面一致",
        ]
    )
    return Result(MANUAL, lines)


def check_replay_export() -> Result:
    """收尾：Audit Replay 时间线 + 导出 Evidence Pack。"""
    service = _svc()
    replay = service.replay_view()
    pack = service.export_pack_view()

    lines: List[str] = []
    state = READY
    if _ok(replay):
        lines.append(
            f"    回放 run {str(replay.get('run_id'))[:8]}｜"
            f"{len(replay.get('steps') or ())} 步 / {replay.get('event_count')} 事件"
        )
        not_recorded = list(replay.get("not_recorded_steps") or ())
        if not_recorded:
            state = DEGRADED
            lines.append(
                f"    ⚠ 未记录步骤 {not_recorded} —— "
                f"话术『manifest 记录全部阶段，可重放』要收窄为『记录了的阶段可重放』"
            )
    else:
        state = BLOCKED
        lines.append(f"    ✗ 回放不可用（{replay.get('reason', '无原因')}）")

    if _ok(pack):
        lines.append(
            f"    证据包：valid={pack.get('valid')} complete={pack.get('complete')} "
            f"pack_status={pack.get('pack_status')} ｜ run 范围={pack.get('run_scope')}"
        )
        zip_bytes = pack.get("zip_bytes") or b""
        lines.append(f"    zip 已生成：{len(zip_bytes)} 字节 → {pack.get('zip_path')}")
        missing_required = list(pack.get("missing_required") or ())
        missing_complete = list(pack.get("missing_completeness") or ())
        declared = dict(pack.get("declared_absences") or {})
        if missing_required:
            state = BLOCKED
            lines.append(f"    ✗ 缺必需件 {missing_required} —— 包不成立")

        # 「缺件」分两种，混为一谈就会把口径说成缺陷：
        #   ① 结构上产不出（run 里声明了原因）—— 口径，如实声明即可；
        #   ② 本该有却没写（既不在包里、也没声明）—— 真缺口，话术不成立。
        from src.cn.evidence_pack import CATEGORY_COMPLETENESS, PACK_FILES

        total_completeness = sum(1 for _n, cat, _d in PACK_FILES if cat == CATEGORY_COMPLETENESS)
        unexplained = [name for name in missing_complete if name not in declared]
        for name, reason in sorted(declared.items()):
            lines.append(f"    · 结构上不产出：{name} —— {reason}")
        if missing_complete:
            state = DEGRADED if state == READY else state
            lines.append(
                f"    ⚠ 完整性缺件 {len(missing_complete)}/{total_completeness} 项：{missing_complete}"
            )
        if unexplained:
            lines.append(
                f"      → {unexplained} 是**真缺口**（包里没有、run 里也没声明为结构缺失）："
                "『报告、claims、evidence、calculations、events 全量打包』不成立，必须补齐"
            )
        elif missing_complete:
            lines.append(
                f"      → 其余 {total_completeness - len(missing_complete)}/{total_completeness} "
                "类完整性件齐全；缺的那些已在 run 产物里声明为口径性缺失，"
                "现场话术按『文档级 run 不含估值表』说，不要念成『证据包不全』"
            )
        if pack.get("warnings"):
            lines.append(f"    ⚠ 包告警：{pack['warnings']}")
    else:
        state = BLOCKED
        lines.append(f"    ✗ 导出不可用（{pack.get('reason', '无原因')}）")
    return Result(state, lines)


def check_temporal() -> Result:
    """失败注入 F9：导入更正公告 → Temporal Revalidation。"""
    service = _svc()
    view = service.temporal_view()
    if not _ok(view):
        return Result(
            BLOCKED,
            [
                f"    时间重验证不可用（{view.get('reason', view.get('unavailable_reason') or '无原因')}）",
                "    → F9 是四个创新之一（Temporal Revalidation），不可用时该创新演示不了",
            ],
        )

    impact = view.get("impact") or {}
    warnings = list(view.get("warnings") or ())
    status = view.get("revalidation_status") or ""
    full_vs_inc = view.get("full_vs_incremental") or {}

    sized = {k: (len(v) if isinstance(v, list) else v) for k, v in impact.items()}
    lines = [
        f"    事件 {view.get('event_id')}｜revalidation_status = {status}",
        f"    影响面：{sized}",
        f"    增量重算：{full_vs_inc.get('nodes_recomputed')} 节点 / "
        f"与全量一致 = {full_vs_inc.get('results_identical')}",
    ]
    if not any(impact.values()):
        return Result(
            BLOCKED,
            lines + ["    ✗ 影响面全空 —— F9『显示受影响 Fact/Calc/Claim/Valuation/Memo』无内容"],
        )

    lines.append(f"    ✓ 受影响节点可展示，重验证动作 {len(view.get('revalidation_actions') or ())} 条")
    state = READY
    if status != "ok":
        state = DEGRADED
        lines.append(f"    ⚠ 状态 {status!r} 非 ok —— 如实展示，不要说『全部重验证通过』")
    if warnings:
        state = DEGRADED if state == READY else state
        lines.append(f"    ⚠ 引擎告警 {len(warnings)} 条：")
        for item in warnings:
            lines.append(f"        {item}")
    return Result(state, lines)


def check_injections() -> Result:
    """失败注入 F1–F8（+ E2 的 FinFuzz 覆盖）。"""
    service = _svc()
    catalog = service.injections_catalog()
    items = list(catalog.get("items") or ())
    if not items:
        return Result(BLOCKED, ["    ✗ 注入场景目录为空"])

    lines = [f"    场景 {len(items)} 个，逐个实跑："]
    failed: List[str] = []
    for item in items:
        outcome = service.run_injection(int(item["scenario_id"]))
        mark = "✓" if _ok(outcome) else "✗"
        if not _ok(outcome):
            failed.append(f"{item['scenario_id']}({item['slug']})")
        deferred = item.get("deferred_to")
        lines.append(
            f"      {mark} F{item['scenario_id']} {item['slug']}"
            + (f"｜deferred→{deferred}" if deferred else "")
        )
    if failed:
        return Result(BLOCKED, lines + [f"    ✗ 跑不起来的场景：{failed}"])

    # E2（量级错误）是 §5 硬项里点名的第四个创新展示步骤
    adversarial = service.adversarial_view()
    # 引擎没自带 status 字段时退回信封状态，别把 None 印出来当结论。
    lines.append(
        f"    FinFuzz 总状态：{adversarial.get('finfuzz_status') or _status(adversarial)}"
    )
    per_type = adversarial.get("per_error_type") or {}
    scale_keys = [k for k in per_type if "scale" in str(k).lower()]
    if not scale_keys:
        lines.append(
            "    ⚠ FinFuzz 的 per-error-type 里**没有 numeric_scale 这一行** —— "
            "E2（量级错误）这一创新展示步骤无对应评测数据"
        )
        state = DEGRADED
    else:
        state = READY
        for key in scale_keys:
            row = per_type[key]
            lines.append(
                f"    ✓ {key}: recall={row.get('recall')} n={row.get('n')}"
            )
    na_rows = [k for k, row in per_type.items() if str(row.get("recall")) == "N/A"]
    if na_rows:
        state = DEGRADED if state == READY else state
        lines.append(
            f"    ⚠ 召回为 N/A（语料无该错误类型样本，不是 0）的类型：{na_rows}"
            f" —— 必须说『无样本』，不能说『检出率 0』"
        )
    return Result(state, lines)


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------

STEPS: Tuple[Step, ...] = (
    Step("1+2", "上传年报 + 含错草稿", "0:00–0:25", "Upload Task", "CARD-01", check_upload),
    Step("3+4", "解析与提取", "0:25–0:55", "Execution Trace", "CARD-01/02", check_parse),
    Step("4b", "ACME 约束自检 ★创新一", "0:55–1:00", "ACME Constraint", "CARD-22", check_acme),
    Step("5", "财务分析", "1:00–1:35", "Financial Analysis", "CARD-03/04", check_financials),
    Step("6+7", "研报纠错 ★高潮一", "1:35–2:25", "Report Corrections", "CARD-05", check_corrections),
    Step("7b", "结论护照 + VERIFY ★核心", "2:25–2:35", "Claim Passport", "CARD-23", check_passport),
    Step("8", "估值", "2:35–3:00", "Valuation", "CARD-08", check_valuation),
    Step("9+10", "备忘录 + 图谱回溯 ★高潮二", "3:00–3:35", "Memo / Graph", "CARD-10/09", check_memo_graph),
    Step("10b", "逻辑脆弱性 ★创新三", "3:35–3:45", "Thesis Fragility", "CARD-24", check_fragility),
    Step("11", "删证据阻断 ★高潮三", "3:45–4:15", "Validator", "CARD-09", check_validator),
    Step("收尾", "审计回放 + 导出证据包", "4:15–5:00", "Replay / Export", "CARD-12", check_replay_export),
    Step("F9", "时间重验证（创新四）", "挑战环节", "Temporal", "CARD-25", check_temporal),
    Step("F1–F8", "失败注入 7 场景 + E2", "挑战环节", "Adversarial", "CARD-12/26", check_injections),
)


def run(only: Optional[str] = None) -> List[Tuple[Step, Result]]:
    results: List[Tuple[Step, Result]] = []
    for step in STEPS:
        if only and step.step_id != only:
            continue
        try:
            outcome = step.check()
        except Exception as exc:  # noqa: BLE001
            # 自检脚本自己炸了也要说出来 —— 静默跳过比报错更糟
            outcome = Result(
                BLOCKED,
                [f"    ✗ 自检脚本自身异常：{type(exc).__name__}: {exc}"],
            )
        results.append((step, outcome))
    return results


def report(results: Sequence[Tuple[Step, Result]]) -> int:
    print()
    print("=" * 78)
    print("DEMO_FLOW.md 剧本自检 —— 每一步「评委看到什么」现在能不能真的取到数据")
    print("=" * 78)

    for step, outcome in results:
        print()
        print(f"{_SYMBOL[outcome.state]} 步骤 {step.step_id}｜{step.title}")
        print(f"              {step.window}｜{step.page}｜{step.cards}")
        for line in outcome.lines:
            print(line)

    tally = {READY: 0, DEGRADED: 0, BLOCKED: 0, MANUAL: 0}
    for _, outcome in results:
        tally[outcome.state] += 1

    print()
    print("-" * 78)
    print(
        f"合计 {len(results)} 步：可讲 {tally[READY]}｜"
        f"可讲需声明 {tally[DEGRADED]}｜会卡 {tally[BLOCKED]}｜待人工 {tally[MANUAL]}"
    )

    blocked = [s for s, r in results if r.state == BLOCKED]
    if blocked:
        print()
        print("会卡的步骤（按剧本判据，这些不解决不能上台）：")
        for step in blocked:
            print(f"  ✗ 步骤 {step.step_id}｜{step.title}")

    innovation = [s for s, r in results if s.step_id in INNOVATION_STEPS and r.state == BLOCKED]
    if innovation:
        print()
        print("★ 创新展示步骤会卡（§4 要求四项全部可演示）：")
        for step in innovation:
            print(f"  ✗ 步骤 {step.step_id}｜{step.title}")

    manual = [s for s, r in results if r.state == MANUAL]
    if manual:
        print()
        print("需人工走查（服务层无接口，本脚本**没查**，别当成通过）：")
        for step in manual:
            print(f"  · 步骤 {step.step_id}｜{step.title}")

    print()
    print(
        "注：MANUAL 不是「查过没问题」，是「本脚本查不了」。\n"
        "    可讲需声明 = 有数据但有缺口，现场话术必须按上面 ⚠ 行收窄，不许照念剧本原文。"
    )
    print("=" * 78)
    return 1 if blocked else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DEMO_FLOW.md 剧本自检")
    parser.add_argument("--strict", action="store_true", help="存在会卡的步骤则退出码 1")
    parser.add_argument("--step", default=None, help="只看某一步（如 6+7）")
    args = parser.parse_args(argv)

    results = run(args.step)
    if not results:
        print(f"没有步骤 {args.step!r}；可用：{', '.join(s.step_id for s in STEPS)}")
        return 2
    code = report(results)
    return code if args.strict else 0


if __name__ == "__main__":
    _configure_utf8_stdio()
    raise SystemExit(main())
