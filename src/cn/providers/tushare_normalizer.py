"""Shared normalization for Tushare responses and saved probe artifacts.

Keep online provider calls and offline snapshot construction on the same field,
period, disclosure-time, and revision rules.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

from ..domain import CompanyProfile, FinancialStatement, MarketBar
from ..symbols import CanonicalSymbol


NORMALIZATION_VERSION = "tushare-cn-fields-1.1.0"
VALID_END_TYPES = {"1", "2", "3", "4"}
STATEMENT_FIELD_MAPS: Dict[str, Dict[str, str]] = {
    "income": {
        "revenue": "revenue", "net_profit": "n_income_attr_p",
        "ebit": "ebit", "ebitda": "ebitda",
    },
    "balance": {
        "total_assets": "total_assets", "total_liabilities": "total_liab",
        "equity": "total_hldr_eqy_exc_min_int", "total_shares": "total_share",
    },
    "cashflow": {
        "operating_cash_flow": "n_cashflow_act", "free_cash_flow": "free_cashflow",
    },
}


def value(row: Any, name: str) -> Optional[float]:
    raw = row.get(name) if hasattr(row, "get") else None
    if raw is None or str(raw) in {"<NA>", "nan", "NaN", "None"}:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _text(value_: Any) -> str:
    if value_ is None:
        return ""
    raw = str(value_)
    return "" if raw in {"<NA>", "nan", "NaN", "None"} else raw


def iso_date(value_: Any) -> Optional[str]:
    raw = _text(value_).strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T00:00:00+08:00"
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw[:10]}T00:00:00+08:00"
    return None


def period(end_date: str, end_type: Any, statement_type: str) -> tuple[str, str] | None:
    kind = "" if end_type is None else str(end_type)
    if kind in {"<NA>", "nan", "NaN", "None"}:
        kind = ""
    kind = kind.replace(".0", "")
    if kind not in VALID_END_TYPES:
        return None
    year = str(end_date)[:4]
    if len(year) != 4 or not year.isdigit():
        return None
    suffix = {"1": "Q1", "2": "H1", "3": "9M", "4": "FY"}[kind]
    fiscal_period = f"{year}{suffix}"
    basis = "_END" if statement_type == "balance" else "_CUMULATIVE"
    return fiscal_period, f"{fiscal_period}{basis}"


def _rows(frame: Any) -> Iterable[Any]:
    if frame is None:
        return ()
    if hasattr(frame, "iterrows"):
        return (row for _, row in frame.iterrows())
    return iter(frame)


def normalize_profile(row: Any, symbol: CanonicalSymbol) -> CompanyProfile:
    name = _text(row.get("name"))
    industry = _text(row.get("industry"))
    return CompanyProfile(
        symbol=symbol,
        name=name,
        industry_standard="tushare_stock_basic",
        industry_level="industry",
        industry_name=industry or None,
    )


def normalize_bars(frame: Any, symbol: CanonicalSymbol) -> list[MarketBar]:
    bars = []
    for row in _rows(frame):
        raw_date = _text(row.get("trade_date"))
        date = iso_date(raw_date)
        if not date:
            continue
        open_price = value(row, "open")
        high_price = value(row, "high")
        low_price = value(row, "low")
        close_price = value(row, "close")
        if None in {open_price, high_price, low_price, close_price}:
            continue
        bars.append(MarketBar(
            symbol=symbol,
            trade_date=date[:10],
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=value(row, "vol"),
            amount=value(row, "amount"),
            adjustment="RAW",
        ))
    return bars


def normalize_statements(
    frames: Mapping[str, Any], *, research_as_of: Optional[str] = None
) -> list[FinancialStatement]:
    """Normalize consolidated reports, applying cutoff before revision selection.

    Applying the point-in-time cutoff before deduplication matters: otherwise a
    later restatement can replace the version known at the requested cutoff and
    then be filtered out, leaving no report for that period.
    """
    statements_by_period: Dict[tuple[str, str], FinancialStatement] = {}
    cutoff = research_as_of or "9999-12-31T23:59:59+08:00"
    for statement_type in ("income", "balance", "cashflow"):
        for row in _rows(frames.get(statement_type)):
            report_type = _text(row.get("report_type")).replace(".0", "")
            comp_type = _text(row.get("comp_type")).replace(".0", "")
            if report_type != "1" or comp_type not in {"1", "2", "3", "4"}:
                continue
            end_date = _text(row.get("end_date"))
            period_info = period(end_date, row.get("end_type"), statement_type)
            if period_info is None:
                continue
            available_at = iso_date(row.get("f_ann_date")) or iso_date(row.get("ann_date"))
            if available_at and available_at > cutoff:
                continue
            fiscal_period, period_basis = period_info
            statement = FinancialStatement(
                statement_type=statement_type,  # type: ignore[arg-type]
                fiscal_period=fiscal_period,
                period_basis=period_basis,
                published_at=iso_date(row.get("ann_date")),
                available_at=available_at,
                currency="CNY",
                unit="CNY",
                values={canonical: value(row, source) for canonical, source in STATEMENT_FIELD_MAPS[statement_type].items()},
                is_restated=str(row.get("update_flag") or "") in {"1", "1.0"},
            )
            key = (statement_type, end_date)
            previous = statements_by_period.get(key)
            if previous is None or (statement.available_at or "") >= (previous.available_at or ""):
                statements_by_period[key] = statement
    return list(statements_by_period.values())
