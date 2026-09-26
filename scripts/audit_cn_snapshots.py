#!/usr/bin/env python3
"""Audit local A-share snapshots and write a provenance-aware quality manifest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Direct script execution puts ``scripts/`` on sys.path; make project imports
# behave the same as the other repository CLI scripts.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cn.providers.snapshot import SnapshotProvider
from src.cn.report import CnResearchReportBuilder
from src.cn.symbols import normalize_cn_symbol


DEFAULT_SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots" / "cn"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "cn_snapshot_manifest.json"
OPTIONAL_STATEMENT_METRICS = frozenset({"ebit", "ebitda", "free_cash_flow"})


def _audit(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data", {})
    bars = list(data.get("bars", []))
    statements = list(data.get("statements", []))
    cutoff = str(payload.get("research_as_of", ""))
    raw_bars = [item for item in bars if item.get("adjustment", "RAW") == "RAW"]
    future_bars = [item for item in bars if cutoff and str(item.get("trade_date", "")) > cutoff[:10]]
    future_statements = [
        item for item in statements
        if item.get("available_at") and cutoff and item["available_at"] > cutoff
    ]
    missing_by_metric: dict[str, int] = {}
    for item in statements:
        for metric, value in (item.get("values") or {}).items():
            if value is None:
                missing_by_metric[metric] = missing_by_metric.get(metric, 0) + 1
    missing_fields = sum(missing_by_metric.values())
    missing_optional = sum(
        count for metric, count in missing_by_metric.items()
        if metric in OPTIONAL_STATEMENT_METRICS
    )
    missing_required = missing_fields - missing_optional
    errors: list[str] = []
    try:
        report = CnResearchReportBuilder(SnapshotProvider(path)).build(
            normalize_cn_symbol(payload["symbol"]), research_as_of=cutoff
        )
        validation = {
            "valid": report.validation.valid,
            "errors": list(report.validation.errors),
        }
        errors.extend(report.validation.errors)
    except (KeyError, TypeError, ValueError) as exc:
        validation = {"valid": False, "errors": [f"audit_failed:{type(exc).__name__}"]}
        errors.append(str(exc))
    warnings = []
    if future_bars:
        warnings.append("future_bars_present")
    if future_statements:
        warnings.append("future_statements_present")
    if missing_fields:
        warnings.append("missing_statement_values")
    status = "blocked" if errors else "warning" if warnings else "pass"
    return {
        "path": str(path),
        "snapshot_id": payload.get("snapshot_id"),
        "symbol": payload.get("symbol"),
        "provider": payload.get("provider", "snapshot"),
        "research_as_of": cutoff,
        "data_quality": payload.get("data_quality"),
        "counts": {
            "bars_total": len(bars), "bars_raw": len(raw_bars),
            "statements_total": len(statements),
            "future_bars": len(future_bars), "future_statements": len(future_statements),
            "missing_statement_values": missing_fields,
            "missing_optional_statement_values": missing_optional,
            "missing_required_statement_values": missing_required,
            "missing_by_metric": dict(sorted(missing_by_metric.items())),
        },
        "validation": validation,
        "status": status,
        "warnings": warnings,
    }


def _markdown(manifest: dict) -> str:
    lines = [
        "# FinTrace-CN A 股快照质量清单", "",
        f"生成时间：{manifest['generated_at']}", "",
        f"快照总数：{manifest['summary']['total']} · 通过：{manifest['summary']['pass']} · "
        f"警告：{manifest['summary']['warning']} · 阻断：{manifest['summary']['blocked']}", "",
        "| 股票 | Provider | 研究截止日 | RAW 行情 | 财报 | 未来披露 | 必要缺失 | 可选缺失 | 状态 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in manifest["snapshots"]:
        counts = row["counts"]
        lines.append(
            f"| {row.get('symbol', '—')} | {row.get('provider', '—')} | {row.get('research_as_of', '—')} "
            f"| {counts['bars_raw']} | {counts['statements_total']} | "
            f"{counts['future_bars'] + counts['future_statements']} | "
            f"{counts['missing_required_statement_values']} | {counts['missing_optional_statement_values']} | {row['status']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--markdown", default=None, help="optional Markdown summary path")
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)
    paths = sorted(snapshot_dir.glob("*.json"))
    snapshots = [_audit(path) for path in paths]
    summary = {
        "total": len(snapshots),
        "pass": sum(row["status"] == "pass" for row in snapshots),
        "warning": sum(row["status"] == "warning" for row in snapshots),
        "blocked": sum(row["status"] == "blocked" for row in snapshots),
    }
    manifest = {
        "schema_version": "cn-snapshot-manifest-1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_dir": str(snapshot_dir),
        "summary": summary,
        "snapshots": snapshots,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        markdown = Path(args.markdown)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(_markdown(manifest), encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
