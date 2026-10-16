"""Smoke test: load every page of the read-only FinTrace-CN workbench and
ensure no exception escapes.  Drives the live ``streamlit`` runtime via
``streamlit.testing.v1.AppTest`` so any UI / data wiring regression is
caught before the user opens the app.

Run from the project root:

    .venv\\Scripts\\python.exe scripts\\smoke_workbench.py

Exit code is 0 when every page renders cleanly, 1 otherwise.

**Page list is discovered, not duplicated.** The old version kept a hand-written
``PAGES`` list with a "must stay in sync with NAVIGATION" comment — a second
source of truth that silently rots the moment a page is added (which is exactly
what CARD-15 does: + demo pages). The list now comes from the sidebar radio's own
options, i.e. from the running app. A separate assertion pins the CARD-15 labels
so a failed demo import cannot quietly shrink the tested surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = str(ROOT / "workbench.py")
#: The workbench is executed as a script by AppTest, but importing the demo page
#: package directly needs the repo root importable.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: CARD-15 §5 (as corrected against DEMO_FLOW.md v3.2). Their absence means the
#: demo surface shrank, which must fail loudly rather than reduce the page count.
EXPECTED_DEMO_PAGES = (
    "竞赛主线",
    "研报纠错",
    "ACME 约束",
    "结论护照",
    "主张-证据图谱",
    "逻辑脆弱性",
    "时间重验证",
    "审计回放",
    "对抗注入",
)


def smoke_draft_key() -> str:
    """纠错页在这一轮冒烟里用哪份草稿。

    材料齐备时该页默认走 ``data/demo`` 的真实材料 —— 它首次构建要解析 229 页
    年报（分钟级），足以把「22 页能不能渲染」这条冒烟拖成超时。真实材料的端到端
    由 ``scripts/rehearse_demo.py`` 与 tests 的 integration 用例覆盖，这里退回
    合成件，让本脚本仍然是「快而只看渲染」的那一条。
    """
    from src.cn import demo_service

    cases = [
        case for case in demo_service.draft_cases()
        if case.get("source") != "data/demo" and case.get("runnable", True)
    ]
    planted = [case for case in cases if case.get("planted")]
    if planted:
        return max(planted, key=lambda case: case["expected_findings"])["key"]
    return demo_service.default_draft_key()


def _check_snapshots() -> None:
    """Print a heads-up if no research / market snapshots are available.

    Pages will still render, but with empty / "未覆盖" placeholders, which
    is the expected behaviour for a fresh checkout.
    """
    snap_dir = ROOT / "data" / "snapshots" / "cn"
    market_dir = ROOT / "data" / "snapshots" / "market"
    research = len(list(snap_dir.glob("*.json"))) if snap_dir.exists() else 0
    market = len(list(market_dir.glob("*.json"))) if market_dir.exists() else 0
    print(f"  · 快照: research={research}  market={market}  "
          f"(pages still render with 未覆盖 placeholders if 0)")


def discover_pages() -> list[str]:
    """Read the page labels off the app's own sidebar radio."""
    at = AppTest.from_file(WORKBENCH, default_timeout=120)
    at.run()
    for radio in at.radio:
        if radio.key == "workbench_page":
            return [str(item) for item in radio.options]
    return []


def main() -> int:
    print(f"Smoke test: {WORKBENCH}\n")
    _check_snapshots()
    print()

    pages = discover_pages()
    if not pages:
        print("[FAIL] 未能从工作台读到页面清单（侧栏 radio 不存在）")
        return 1

    missing = [label for label in EXPECTED_DEMO_PAGES if label not in pages]
    if missing:
        print(f"[FAIL] CARD-15 Demo 页面缺失：{', '.join(missing)}")
        print("       （挂载失败仍会渲染，但被测页面变少 —— 必须显式失败。）")
        return 1
    print(f"  · 页面 {len(pages)} 个（含 CARD-15 Demo {len(EXPECTED_DEMO_PAGES)} 个）")
    print()

    fails: list[tuple[str, str]] = []
    smoke_draft = smoke_draft_key()
    for page in pages:
        at = AppTest.from_file(WORKBENCH, default_timeout=120)
        at.session_state["workbench_page"] = page
        # 逐页冒烟测的是「页面会不会炸」，不是真实材料的构建耗时（见 smoke_draft_key）。
        at.session_state["demo_corrections_draft"] = smoke_draft
        at.session_state["demo_competition_draft"] = smoke_draft
        at.run()
        if at.exception:
            err = at.exception[0]
            fails.append((page, f"{type(err.value).__name__}: {err.value}"))
            print(f"[FAIL] {page}")
            print(f"        → {type(err.value).__name__}: {err.value}")
        else:
            print(f"[ OK ] {page}")

    total = len(pages)
    passed = total - len(fails)
    print(f"\n{passed}/{total} pages clean")
    if fails:
        print("\nFailures:")
        for page, msg in fails:
            print(f"  - {page}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
