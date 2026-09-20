"""Generate the synthetic CARD-01 document corpus (committed fixtures).

CARD-01 §13: "tests/fixtures/documents/ 合成样例（年报/公告/研报草稿各 ≥3 份，
含缺失字段与扫描页样例），生成脚本入库保证可复现。"

Usage
-----
    python scripts/gen_document_fixtures.py            # write the corpus
    python scripts/gen_document_fixtures.py --check    # verify byte-reproducibility
    python scripts/gen_document_fixtures.py --benchmark # baseline P/R/F1 report

The corpus is synthetic. No real filing, and no paid or copyrighted data, is
written here; real PDFs belong in ``data/documents/`` (git-ignored).
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

from src.cn.documents.extraction_benchmark import (  # noqa: E402
    default_fixtures_dir,
    evaluate_corpus,
    format_markdown,
    write_report,
)
from src.cn.documents.financial_benchmark import evaluate_financial_corpus  # noqa: E402
from src.cn.documents.synthetic_corpus import verify_corpus, write_corpus  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate / verify the CARD-01 synthetic document corpus.")
    parser.add_argument("--out", default=str(default_fixtures_dir()),
                        help="Corpus output directory (default: tests/fixtures/documents).")
    parser.add_argument("--check", action="store_true",
                        help="Re-render in memory and verify the committed manifest sha256 values.")
    parser.add_argument("--benchmark", action="store_true",
                        help="Also run the extraction and financial-layer baselines and write reports to output/.")
    parser.add_argument("--report-out", default=str(ROOT / "output" / "benchmark" / "cn_agent_v2"),
                        help="Directory for the benchmark report (git-ignored).")
    args = parser.parse_args(argv)

    out = Path(args.out)

    if args.check:
        verdict = verify_corpus(out)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        if not verdict["ok"]:
            print("语料不可复现：请重新运行本脚本（不带 --check）后提交 fixtures。", file=sys.stderr)
            return 1
        print(f"OK: {verdict['checked']} 个文档与清单 sha256 完全一致。")
        return 0

    manifest = write_corpus(out)
    print(f"已生成 {len(manifest['documents'])} 个合成文档 -> {out}")
    for entry in manifest["documents"]:
        gold = entry["gold_records"]
        print(f"  {entry['document_id']:16s} {entry['filename']:44s} "
              f"{entry['bytes']:>8d} B  gold={gold}  {entry['sha256'][:12]}")

    if args.benchmark:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            score = evaluate_corpus(out, Path(tmp) / "documents")
        paths = write_report(score, Path(args.report_out))
        print()
        print(format_markdown(score))
        print(f"报告已写入: {paths['json']}")
        print(f"           {paths['markdown']}")
        financial = evaluate_financial_corpus(out, Path(tmp) / "financial_documents")
        financial_path = Path(args.report_out) / "financial_layer_baseline.json"
        financial_path.parent.mkdir(parents=True, exist_ok=True)
        financial_path.write_text(
            json.dumps({
                "suite": "cn_agent_v2", "set": "synthetic",
                "component": "CARD-02/03/04 financial layers",
                "synthetic": True,
                "disclaimer": "Synthetic fixtures only; no real filing data. Not a real-model benchmark.",
                "metrics": financial.to_dict(),
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"财务层基准已写入: {financial_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
