#!/usr/bin/env python3
"""Build a normalized offline A-share snapshot from an existing Tushare probe.

This script is intentionally offline: it reads the raw CSV artifacts produced by
``probe_tushare_cn0.py`` and makes no provider calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Direct script execution places ``scripts/`` rather than the project root on
# sys.path.  Add the root before importing the shared canonical classifier.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.cn.industries import is_financial_institution
from src.cn.providers.tushare_normalizer import (
    NORMALIZATION_VERSION,
    VALID_END_TYPES as _VALID_END_TYPES,
    normalize_bars,
    normalize_statements,
    period as _canonical_period,
)
from src.cn.symbols import normalize_cn_symbol


def _read_csv(path: Path, **kwargs):
    """Read a probe CSV and turn a malformed empty artifact into a clear error."""
    try:
        return pd.read_csv(path, **kwargs)
    except pd.errors.EmptyDataError as exc:
        raise SystemExit(
            f"{path.name} is empty; probe artifact is incomplete. Rerun probe_tushare_cn0.py."
        ) from exc


# Period kinds Tushare exposes for the consolidated primary report.  Anything
# outside this set is treated as unverifiable and dropped (never renamed to a
# ``UNKNOWN`` placeholder) so the snapshot never carries fiscal periods the
# deterministic period engine cannot parse.
def _period(end_date: str, end_type: str, statement_type: str) -> tuple[str, str] | None:
    """Return ``(fiscal_period, period_basis)`` for one Tushare row.

    Returns ``None`` when ``end_type`` is missing or outside the four valid
    quarter codes — a placeholder name like ``"2026UNKNOWN"`` would leak into
    the snapshot and later crash ``FinancialPeriodEngine._parse_period``.  We
    prefer to drop the row entirely: an untyped period is unverifiable data.
    """
    return _canonical_period(end_date, end_type, statement_type)


def _statements(probe_dir: Path, statement_type: str, filename: str, *, research_as_of: str | None = None):
    frame = _read_csv(probe_dir / filename, dtype={"ann_date": "string", "f_ann_date": "string", "end_date": "string", "end_type": "string", "report_type": "string", "comp_type": "string", "update_flag": "string"})
    statements = normalize_statements({statement_type: frame}, research_as_of=research_as_of)
    return [asdict(statement) for statement in statements]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("probe_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--research-as-of", default="2026-08-10T23:00:00+08:00")
    parser.add_argument("--snapshot-id", default=None, help="Optional immutable snapshot ID.")
    args = parser.parse_args()

    profile_row = _read_csv(args.probe_dir / "raw_stock_basic.csv").iloc[0]
    symbol = str(profile_row["ts_code"]).strip().upper()
    if not symbol or symbol == "NAN":
        raise SystemExit("raw_stock_basic.csv does not contain a valid ts_code.")
    date_tag = args.research_as_of[:10].replace("-", "")
    snapshot_id = args.snapshot_id or f"{symbol}_{date_tag}_tushare_v1"
    daily = _read_csv(args.probe_dir / "raw_daily.csv", dtype={"trade_date": "string"})
    bars = []
    for bar in normalize_bars(daily, normalize_cn_symbol(symbol)):
        item = asdict(bar)
        item.pop("symbol", None)
        bars.append(item)
    statements = (
        _statements(args.probe_dir, "income", "raw_income.csv", research_as_of=args.research_as_of)
        + _statements(args.probe_dir, "balance", "raw_balancesheet.csv", research_as_of=args.research_as_of)
        + _statements(args.probe_dir, "cashflow", "raw_cashflow.csv", research_as_of=args.research_as_of)
    )
    payload = {
        "schema_version": "1.0.0",
        "snapshot_id": snapshot_id,
        "provider": "tushare",
        "symbol": symbol,
        "research_as_of": args.research_as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data_quality": "provider_normalized_from_cn0_probe",
        "source_metadata": {"raw_probe_dir": args.probe_dir.name, "normalization_version": NORMALIZATION_VERSION},
        "data": {
            "profile": {
            "name": str(profile_row["name"]), "currency": "CNY", "industry_standard": "tushare_stock_basic",
            "industry_level": "industry", "industry_name": str(profile_row["industry"]),
            # This is a deterministic canonical-symbol classification, not an
            # LLM inference.  It makes the financial-valuation boundary travel
            # with a provider-normalized snapshot and into every report export.
            "entity_type": "financial_institution" if is_financial_institution(symbol[:6]) else "operating_company",
            },
            "bars": bars,
            "statements": statements,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.output} with {len(bars)} RAW bars and {len(statements)} statements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
