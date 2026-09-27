#!/usr/bin/env python3
"""CARD-11 — run the ``cn_agent_v2`` three-tier benchmark and write its report.

```
python scripts/run_benchmark_v2.py --set synthetic --out output/benchmark/cn_agent_v2
python scripts/run_benchmark_v2.py --set real --rows data/benchmark_real/real_rows.json \
    --out output/benchmark/cn_agent_v2/real
```

``synthetic`` aggregates the component baselines that the individual cards
already publish (CARD-01 extraction, CARD-05 checker, and any card that writes
cn_agent_v2 metric ids). It is fully offline and deterministic.

``real`` / ``holdout`` consume *captured* rows and therefore belong to the
integration tier: without ``--rows`` the script refuses to run rather than
produce a report full of invented numbers. The output directory must carry the
``real_`` prefix — ``isolate_payload`` enforces that, because a mixed directory
is how a synthetic number becomes a real headline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cn.ablation_v2 import load_ablation  # noqa: E402
from src.cn.benchmark_v2 import (  # noqa: E402
    TIER_HOLDOUT,
    TIER_REAL,
    TIER_SYNTHETIC,
    UNMEASURED_OFFLINE,
    build_report,
    tier_payload_from_dict,
    write_report,
)

DEFAULT_OUT = PROJECT_ROOT / "output" / "benchmark" / "cn_agent_v2"

#: The component baselines that already exist. Each is (component_name, path,
#: case_count), where a ``None`` count means "the baseline file declares its own"
#: — missing files are reported as skipped, never treated as zero.
COMPONENT_BASELINES = (
    ("card01_document_extraction", "document_extraction_baseline.json", 10),
    ("card05_report_checker", "checker_baseline.json", 3),
    # CARD-26 §15. Written by `python -m src.cn.finfuzz --baseline-dir <这里>`; the
    # file name comes from src.cn.finfuzz.baseline so a rename cannot orphan it.
    # The count is ``None`` on purpose: FinFuzz's sample count moves with the corpus
    # and the seed, so hardcoding it here would silently drift away from the file
    # sitting next to it.
    ("card26_finfuzz", "finfuzz_baseline.json", None),
)


def git_commit() -> str:
    """Best-effort commit id; ``unknown`` rather than an invented hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown" if result.returncode == 0 else "unknown"


def component_case_count(name: str, payload: Mapping[str, object], fallback) -> int:
    """How many cases underlie one component's baseline.

    A component that measures its own corpus size knows it better than this table
    does, so a ``case_count`` in the payload wins over the registry's fallback.
    FinFuzz registers ``None`` for exactly that reason: its sample count moves with
    the corpus and the seed.
    """
    declared = payload.get("case_count")
    if isinstance(declared, int) and declared > 0:
        return declared
    return int(fallback or 0)


def load_components(source_dir: Path) -> Dict[str, Dict[str, object]]:
    components: Dict[str, Dict[str, object]] = {}
    for name, filename, _count in COMPONENT_BASELINES:
        path = source_dir / filename
        if not path.exists():
            print(f"[SKIP] 组件基线缺失：{path}（该组件指标将记 N/A，不按 0 计）", file=sys.stderr)
            continue
        components[name] = json.loads(path.read_text(encoding="utf-8"))
    return components


def load_ablation_runs(source_dir: Path) -> Dict[str, Dict[str, object]]:
    """Read the §7b ablation artifact, or report why the table stays empty.

    The ablation is *measured*, not re-derived here: ``src.cn.ablation_v2`` owns
    that, and the report cites the file it produced. A silent fallback would let
    the §7b table read ``TARGET / TO BE MEASURED`` while looking complete, so a
    missing artifact prints the command that produces it.
    """
    runs = load_ablation(source_dir)
    if not runs:
        print(
            f"[SKIP] §7b 消融结果缺失：{source_dir}（该表将记 TARGET / TO BE MEASURED）。"
            "生成：python -m src.cn.ablation_v2 --out <该目录>",
            file=sys.stderr,
        )
    return runs


def ablation_progress(table: Sequence[Mapping[str, object]]) -> str:
    """How much of §7b is actually measured — read off the rendered table.

    Counted from the table rather than from the loaded runs, so the line cannot
    disagree with what the report says.
    """
    measured = [row for row in table if row.get("status") == "MEASURED"]
    rest = [row["innovation"] for row in table if row.get("status") != "MEASURED"]
    line = f"{len(measured)}/{len(table)} 行已实测"
    if rest:
        line += f"（未测：{'、'.join(str(name) for name in rest)}）"
    return line


def report_kwargs(args) -> Dict[str, object]:
    return {
        "ablation_runs": load_ablation_runs(Path(args.ablation_dir)),
        "extra_fingerprint": {"git_commit": git_commit(), "generated_by": "scripts/run_benchmark_v2.py"},
    }


def run_synthetic(args) -> int:
    source = Path(args.components_dir)
    components = load_components(source)
    if not components:
        print(f"[FAIL] {source} 下没有任何组件基线，无法聚合。", file=sys.stderr)
        return 2

    case_count = sum(
        component_case_count(name, components[name], fallback)
        for name, _file, fallback in COMPONENT_BASELINES
        if name in components
    )
    report = build_report(
        components=components,
        component_case_count=case_count,
        **report_kwargs(args),
    )
    paths = write_report(args.out, report)
    tier = report["tiers"][0]
    print(
        f"[OK] synthetic：已测 {tier['measured_metrics']} 项 / 未测 {tier['unmeasured_metrics']} 项"
        f"（未测原因 {UNMEASURED_OFFLINE}）"
    )
    print(f"     §7b 消融：{ablation_progress(report['ablation'])}")
    print(f"     {paths['json']}")
    print(f"     {paths['markdown']}")
    return 0


def run_captured(args, tier: str) -> int:
    if not args.rows:
        print(
            f"[FAIL] --set {tier} 需要 --rows（已捕获的结果行）。"
            "没有真实运行结果时本脚本拒绝生成报告——缺数据标 N/A 是诚实，"
            "凭空造数是造假。",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    report = build_report(
        [tier_payload_from_dict(payload, tier=tier)],
        **report_kwargs(args),
    )
    paths = write_report(args.out, report)
    tier_report = report["tiers"][0]
    print(
        f"[OK] {tier}：已测 {tier_report['measured_metrics']} 项 / "
        f"未测 {tier_report['unmeasured_metrics']} 项"
    )
    print(f"     §7b 消融：{ablation_progress(report['ablation'])}")
    print(f"     {paths['json']}")
    print(f"     {paths['markdown']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 cn_agent_v2 三级基准")
    parser.add_argument("--set", dest="tier", default=TIER_SYNTHETIC,
                        choices=[TIER_SYNTHETIC, TIER_REAL, TIER_HOLDOUT])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--components-dir", default=str(DEFAULT_OUT),
                        help="组件基线所在目录（synthetic 用）")
    parser.add_argument("--ablation-dir", default=str(DEFAULT_OUT / "ablation"),
                        help="§7b 消融结果目录；缺失时该表记 TARGET / TO BE MEASURED")
    parser.add_argument("--rows", default="", help="已捕获结果行 JSON（real/holdout 用）")
    args = parser.parse_args(argv)

    if args.tier == TIER_SYNTHETIC:
        return run_synthetic(args)
    return run_captured(args, args.tier)


if __name__ == "__main__":
    raise SystemExit(main())
