"""§8 CLI — ``python -m src.cn.finfuzz --operator numeric,unit --out <dir>``.

Default run is the whole ten-operator suite over the committed clean corpus, and
it writes ``report.json`` + ``report.md``. Exit status is non-zero only when the
suite could not run at all: a low recall is a *result*, not an error, and turning
it into a failing exit code would tempt someone to tune the corpus until it went
away — which is exactly the overfitting §12.4 warns about.

``--baseline-dir <CARD-11 组件目录>`` additionally publishes the four metrics as a
component baseline, which is what makes ``scripts/run_benchmark_v2.py --set
synthetic`` include FinFuzz numbers (§15). Without that flag the baseline is not
written, and the aggregator reports the group as unmeasured rather than as zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .baseline import write_baseline
from .corpus import load_corpus, run_corpus
from .models import NOT_APPLICABLE, OPERATOR_ORDER
from .suite import render_markdown, write_report

#: The operator names this CLI accepts. Mirrors :data:`OPERATOR_ORDER` so the help
#: text and the validation cannot drift apart.
OPERATORS_NAMES = tuple(OPERATOR_ORDER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cn.finfuzz",
        description="FinFuzz — 金融语义对抗变异测试（确定性，离线）",
    )
    parser.add_argument(
        "--operator",
        default="",
        help=f"逗号分隔的算子；留空=全部。可选：{','.join(OPERATOR_ORDER)}",
    )
    parser.add_argument(
        "--set",
        dest="corpus_set",
        default="synthetic",
        choices=("synthetic",),
        help="语料集。当前仅合成集可用（真实研报需授权且不入库）",
    )
    parser.add_argument("--case", default="", help="逗号分隔的语料 case id（默认全部）")
    parser.add_argument("--seed", type=int, default=42, help="采样种子（报告可复现）")
    parser.add_argument("--max-per-operator", type=int, default=None, help="每个算子最多保留多少条 spec")
    parser.add_argument("--out", default="output/benchmark/finfuzz", help="报告输出目录")
    parser.add_argument(
        "--baseline-dir",
        default="",
        help="额外写入 CARD-11 组件基线（finfuzz_baseline.json/.md）；"
        "指向基准的组件目录即可被 --set synthetic 聚合",
    )
    parser.add_argument("--json", action="store_true", help="只打印 JSON 报告")
    return parser


def _parse_operators(raw: str) -> Optional[list]:
    names = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    unknown = [name for name in names if name not in OPERATORS_NAMES]
    if unknown:
        raise SystemExit(f"未知算子：{sorted(unknown)}；可选：{list(OPERATORS_NAMES)}")
    return names or None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    operators = _parse_operators(args.operator)
    loaded = load_corpus(
        doc_ids=[item.strip() for item in args.case.split(",") if item.strip()] or None
    )
    if not loaded.cases:
        print("[finfuzz] 没有可用语料 case；未生成任何报告（不写占位结果）", file=sys.stderr)
        for item in loaded.skipped:
            print(f"[finfuzz]   跳过 {item.get('doc_id')}: {item.get('reason')}", file=sys.stderr)
        return 1

    report, _skipped = run_corpus(
        loaded.cases, operators, seed=args.seed, max_per_operator=args.max_per_operator
    )
    report.skipped = [*loaded.skipped, *report.skipped]
    report.notes.append(f"drafts_skipped={len(loaded.skipped)}")

    if args.json:
        payload = report.to_dict()
        payload["adversarial_rows"] = report.adversarial_rows()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))

    paths = write_report(report, Path(args.out))
    overall = report.overall
    recall = overall.get("recall")
    printable = NOT_APPLICABLE if recall is None else f"{recall:.4f}"
    print(
        f"\n[finfuzz] suite={report.suite_id} specs={overall.get('n')} "
        f"recall={printable} fpr="
        f"{NOT_APPLICABLE if overall.get('false_positive_rate') is None else format(overall['false_positive_rate'], '.4f')}"
    )
    print(f"[finfuzz] report: {paths['json']} | {paths['markdown']}")
    if args.baseline_dir:
        # §15 — 组件基线。与报告分开写：报告是给人读的全量产物，基线是给
        # CARD-11 聚合器读的四项指标；同一份东西两种用途，别混成一个文件。
        baseline_paths = write_baseline(report, Path(args.baseline_dir))
        print(f"[finfuzz] baseline: {baseline_paths['json']} | {baseline_paths['markdown']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
