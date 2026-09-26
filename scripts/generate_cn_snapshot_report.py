"""Render a reproducible FinTrace-CN report from a local snapshot only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cn.providers.snapshot import SnapshotProvider
from src.cn.report import CnResearchReportBuilder
from src.cn.symbols import normalize_cn_symbol


def _peer_valuation(snapshot_id: str) -> dict:
    """Read deterministic local peer valuation when the snapshot is registered."""
    try:
        from src.cn import workbench_service

        return workbench_service.research_peer_valuation(snapshot_id)
    except (KeyError, OSError, ValueError) as exc:
        return {
            "snapshot_id": snapshot_id,
            "available": False,
            "reason": f"peer_valuation_unavailable:{type(exc).__name__}",
        }


def _render_peer_section(peer: dict) -> str:
    lines = ["## 同行估值", ""]
    if not peer.get("available"):
        reason = peer.get("reason") or ", ".join(peer.get("validation", {}).get("warnings", [])) or "同行覆盖不足"
        lines.extend([f"- 状态：不可用（{reason}）", "- 本报告未使用不同研究截止时间的同行快照。", ""])
        return "\n".join(lines)

    lines.extend([
        f"- 同行业：{peer.get('industry_code', '—')}",
        f"- 同期同行数：{peer.get('peer_count', 0)}",
        f"- 价格口径：{peer.get('price_basis', 'RAW')}",
        f"- 期间口径：{peer.get('period_basis', '—')}",
        "",
        "| 方法 | 同行中位数 | P25 | P75 | 隐含价格中位数 |",
        "|---|---:|---:|---:|---:|",
    ])
    ranges = peer.get("valuation_ranges", {})
    multiples = peer.get("multiples", {})
    for method in ("PE", "PB", "PS"):
        interval = ranges.get(method, {})
        multiple = multiples.get(method, {})
        fmt = lambda value: "—" if value is None else f"{float(value):,.2f}"
        lines.append(
            f"| {method} | {fmt(multiple.get('median'))} | {fmt(interval.get('p25'))} | "
            f"{fmt(interval.get('p75'))} | {fmt(multiple.get('implied_price'))} |"
        )
    included = sorted({
        row.get("symbol")
        for rows in (peer.get("distributions") or {}).values()
        for row in rows
        if row.get("included") and row.get("symbol")
    })
    lines.extend([
        "",
        f"- 纳入同行：{', '.join(included) if included else '—'}",
        "- 结果来自同一研究截止时间的本地版本化快照；不代表实时行情或投资建议。",
        "",
    ])
    return "\n".join(lines)


def render_report(snapshot_path: Path, symbol_text: str, output_path: Path, research_as_of: str | None = None) -> dict:
    """Write Markdown plus provenance metadata; never calls an online provider."""
    provider = SnapshotProvider(snapshot_path)
    report = CnResearchReportBuilder(provider).build(
        normalize_cn_symbol(symbol_text), research_as_of=research_as_of,
    )
    peer = _peer_valuation(provider._payload["snapshot_id"])
    markdown = report.markdown.rstrip() + "\n\n" + _render_peer_section(peer)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    peer_path = output_path.with_suffix(".peer_valuation.json")
    peer_path.write_text(json.dumps(peer, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {
        "snapshot_id": provider._payload["snapshot_id"],
        "research_as_of": research_as_of or provider._payload["research_as_of"],
        "symbol": symbol_text,
        "validation": {"valid": report.validation.valid, "errors": report.validation.errors},
        "report_path": str(output_path),
        "peer_valuation_path": str(peer_path),
        "peer_valuation": {
            "available": bool(peer.get("available")),
            "peer_count": peer.get("peer_count", 0),
            "validation": peer.get("validation", {}),
        },
    }
    output_path.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an offline, traceable A-share snapshot report.")
    parser.add_argument("--snapshot", type=Path, required=True, help="Versioned local snapshot JSON.")
    parser.add_argument("--symbol", default="600519.SH", help="Canonical A-share symbol.")
    parser.add_argument("--output", type=Path, required=True, help="Markdown output path.")
    parser.add_argument("--research-as-of", default=None, help="Optional ISO-8601 cutoff.")
    args = parser.parse_args()
    metadata = render_report(args.snapshot, args.symbol, args.output, args.research_as_of)
    print(json.dumps(metadata, ensure_ascii=False))
    return 0 if metadata["validation"]["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
