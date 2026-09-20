"""Generate / verify the CARD-05 planted-error research-draft corpus.

CARD-05 §11: "``tests/test_cn_checker.py`` + ``tests/fixtures/report_drafts/``（含
planted errors 的草稿集）". Like CARD-01's corpus, the fixtures are committed and the
generator is in the repo so they stay reproducible.

Usage
-----
    python scripts/gen_report_draft_fixtures.py            # write the corpus
    python scripts/gen_report_draft_fixtures.py --check    # verify byte-reproducibility
    python scripts/gen_report_draft_fixtures.py --report   # run the checker over the
                                                           # gold set and print P/R/F1

The corpus is synthetic. No real research report, filing, or copyrighted text is
written here; real drafts belong in ``data/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cn.checker.draft_corpus import (  # noqa: E402
    default_fixtures_dir,
    verify_corpus,
    write_corpus,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate / verify the CARD-05 planted-error draft corpus."
    )
    parser.add_argument("--out", default=str(default_fixtures_dir()),
                        help="Corpus output directory (default: tests/fixtures/report_drafts).")
    parser.add_argument("--check", action="store_true",
                        help="Re-render in memory and verify the committed manifest sha256 values.")
    parser.add_argument("--report", action="store_true",
                        help="Also run the checker over the gold set and print Precision/Recall/F1.")
    parser.add_argument("--report-out", default=str(ROOT / "output" / "benchmark" / "cn_agent_v2"),
                        help="Directory for the checker benchmark report (git-ignored).")
    args = parser.parse_args(argv)

    out = Path(args.out)

    if args.check:
        verdict = verify_corpus(out)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        if not verdict["ok"]:
            print(
                "草稿语料不可复现：请重新运行本脚本（不带 --check）后提交 fixtures。",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {verdict['checked']} 份草稿与清单 sha256 完全一致。")
        return 0

    manifest = write_corpus(out)
    print(f"已生成 {len(manifest['drafts'])} 份合成研报草稿 -> {out}")
    for entry in manifest["drafts"]:
        print(
            f"  {entry['document_id']:20s} {entry['filename']:38s} "
            f"{entry['bytes']:>7d} B  claims={entry['expected_claims']} "
            f"findings={entry['expected_findings']}  {entry['sha256'][:12]}"
        )

    if args.report:
        from src.cn.checker.benchmark import evaluate_corpus, format_markdown, write_report

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            score = evaluate_corpus(out, Path(tmp) / "drafts")
        paths = write_report(score, Path(args.report_out))
        print()
        print(format_markdown(score))
        print(f"Checker 基准报告已写入: {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
