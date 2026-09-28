"""On-demand, provider-backed refresh of one versioned A-share research snapshot.

The project's research input contract is a set of *immutable, versioned* local
snapshots.  This module keeps that contract and adds one operation on top of it:
pull the latest available **daily close** from the existing Tushare provider and
materialise it as a *brand new* snapshot.  An existing snapshot file is never
edited, so every historical snapshot, report, and evidence record stays exactly
as it was.

Layering rules this module obeys:

* Only :func:`fetch_latest_bars` touches the network, and even that is fully
  injectable, so every decision that shapes a new snapshot is a pure function
  and can be unit-tested offline.
* Financial statements are *never* invented.  By default (``bars_only=True``)
  they are reused from the base snapshot after re-applying the point-in-time
  cutoff; with ``bars_only=False`` they are re-fetched from the same provider.
* A refresh either *succeeds and writes a new snapshot* or returns an explicit
  failure state.  There is no silent fallback that would let stale data pose as
  a fresh update.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from . import errors as provider_errors
from .industries import is_financial_institution
from .providers.base import FinancialDataProvider
from .providers.tushare import TushareCallBudget, TushareProvider
from .providers.tushare_normalizer import NORMALIZATION_VERSION
from .symbols import normalize_cn_symbol

SCHEMA_VERSION = "1.0.0"

#: Every refresh returns exactly one of these states.  ``succeeded`` is the only
#: state that creates a snapshot; the others must never be rendered as a
#: successful update.
REFRESH_SUCCEEDED = "succeeded"
REFRESH_NO_NEW_DATA = "no_new_data"
REFRESH_NO_DATA = "no_data"
REFRESH_BLOCKED = "blocked"
REFRESH_FAILED = "failed"

#: How far back to ask the provider for daily bars.  A generous window makes the
#: pull robust across weekends and multi-day holidays: the provider simply
#: returns the most recent trading day it has.
BARS_LOOKBACK_DAYS = 45

#: ``bars_only`` costs one endpoint call (``daily``); a full refresh adds
#: ``stock_basic`` + ``income`` + ``balancesheet`` + ``cashflow``.
CALL_BUDGET_BARS_ONLY = 1
CALL_BUDGET_FULL = 5

#: China Standard Time — the exchange's own clock, and the one every
#: ``research_as_of`` in this repository is written in.
CST = timezone(timedelta(hours=8))
# A daily bar is an official close only after the A-share continuous session
# ends.  We deliberately do not infer lunch breaks, auctions, or exchange
# holiday calendars from a weekday-only helper.
A_SHARE_CLOSE_TIME = time(15, 0)

LOGGER = logging.getLogger(__name__)

_SNAPSHOT_VERSION_RE = re.compile(r"_v(\d+)$")

#: Message fragments the Tushare SDK raises as plain exceptions.  They are the
#: only way to tell a frequency limit apart from a network fault.
_RATE_LIMIT_HINTS = ("每分钟", "频率", "rate limit", "too many requests", "limit exceeded", "积分不足")
_NETWORK_HINTS = ("timed out", "timeout", "connection", "proxy", "ssl", "temporary failure", "max retries", "unreachable")

#: Fields added by this module that must not be inherited verbatim from a base
#: snapshot.  Everything else the base carries (schema_version, extra metadata)
#: is preserved so a refresh never silently drops provenance.
_REFRESH_OWNED_KEYS = frozenset({
    "schema_version", "snapshot_id", "snapshot_version", "provider", "symbol",
    "research_as_of", "fetched_at", "generated_at", "data_quality",
    "source_metadata", "data",
})


# ---------------------------------------------------------------------------
# Time helpers (pure — the caller owns "now")
# ---------------------------------------------------------------------------
def _coerce_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


def coerce_research_as_of(value: Optional[str], now: datetime) -> str:
    """Return a canonical ``+08:00`` research cutoff.

    A date-only value (``YYYY-MM-DD``) is read as *that trading day's* cutoff
    (23:00 CST), matching ``scripts/build_cn_snapshot_from_probe.py``.
    """
    if value is None or not str(value).strip():
        return now.isoformat(timespec="seconds")
    text = str(value).strip()
    if len(text) == 10:
        text = f"{text}T23:00:00+08:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:  # surfaced to the API as a 422
        raise ValueError("research_as_of must be an ISO-8601 date or datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST).isoformat(timespec="seconds")


def _business_days_between(start: date, end: date) -> int:
    """Count weekdays in ``(start, end]``.

    A deterministic, calendar-free approximation of "trading days".  Exchange
    holidays are not modelled here (that would need a network calendar), which is
    why the result is reported together with ``calendar_basis`` so the UI can
    label it honestly instead of implying exchange precision.
    """
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def describe_freshness(latest_trade_date: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """Describe how current a snapshot's newest RAW close is.

    The label is *computed* from the snapshot's own latest trade date and the
    supplied clock — "最新" is never hard-coded.
    """
    reference = _coerce_now(now)
    trade_day = date.fromisoformat(str(latest_trade_date)[:10])
    today = reference.date()
    calendar_days = max((today - trade_day).days, 0)
    trading_days = _business_days_between(trade_day, today)
    if trade_day > today:
        # This should already be prevented by the cutoff.  Preserve an explicit
        # label if a caller asks this pure display helper about a future date.
        status, label = "future_date", f"数据日期晚于当前时间（{trade_day.isoformat()}）"
    elif trade_day == today and reference.timetz().replace(tzinfo=None) < A_SHARE_CLOSE_TIME:
        # Some providers can expose a same-day row before the close is final.
        # Never describe it as a closing price until the exchange close passes.
        status, label = "awaiting_close", "当日尚未收盘，收盘价待确认"
    elif trade_day == today:
        status, label = "updated_today", "今日收盘后更新"
    elif trading_days == 0:
        status = "non_trading_day"
        label = f"非交易日，使用最近交易日（{trade_day.isoformat()}）"
    else:
        status, label = "recent", f"距今 {trading_days} 个交易日"
    return {
        "latest_trade_date": trade_day.isoformat(),
        "as_of": reference.isoformat(timespec="seconds"),
        "status": status,
        "label": label,
        "calendar_days_since": calendar_days,
        "trading_days_since": trading_days,
        "stale": status == "future_date" or trading_days > 5 or calendar_days > 7,
        "calendar_basis": "weekday_approximation",
    }


# ---------------------------------------------------------------------------
# Snapshot-directory helpers
# ---------------------------------------------------------------------------
def _is_snapshot_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and {"snapshot_id", "symbol", "research_as_of", "data"}.issubset(payload)


def iter_snapshots(snapshot_dir: Path | str) -> Iterator[dict[str, Any]]:
    """Yield every valid snapshot payload in a directory, newest-agnostic.

    Unreadable files (report sidecars, partial writes) are skipped rather than
    crashing a refresh.
    """
    directory = Path(snapshot_dir)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _is_snapshot_payload(payload):
            yield payload


def is_provider_backed(payload: dict[str, Any]) -> bool:
    """Whether a snapshot's facts came from a real provider.

    An illustrative/demo fixture must never be used as the base of a refresh:
    merging real bars onto fixture financials would present synthetic numbers as
    sourced facts.
    """
    if str(payload.get("provider", "")).strip().lower() != "tushare":
        return False
    metadata = payload.get("source_metadata") or {}
    if metadata.get("synthetic_demo"):
        return False
    quality = str(payload.get("data_quality") or "").lower()
    return "illustrative" not in quality and "demo" not in quality


def find_base_snapshot(symbol: str, snapshot_dir: Path | str) -> Optional[dict[str, Any]]:
    """Return the newest provider-backed snapshot for one canonical symbol."""
    candidates = [
        payload for payload in iter_snapshots(snapshot_dir)
        if payload.get("symbol") == symbol and is_provider_backed(payload)
    ]
    return max(candidates, key=lambda item: str(item.get("research_as_of", "")), default=None)


def next_snapshot_version(symbol: str, snapshot_dir: Path | str) -> int:
    """Next monotonic per-symbol version, parsed from existing ``_vN`` ids."""
    highest = 0
    for payload in iter_snapshots(snapshot_dir):
        if payload.get("symbol") != symbol:
            continue
        match = _SNAPSHOT_VERSION_RE.search(str(payload.get("snapshot_id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _unique_snapshot_id(symbol: str, date_tag: str, version: int, snapshot_dir: Path | str) -> tuple[str, int]:
    directory = Path(snapshot_dir)
    candidate_version = version
    while True:
        snapshot_id = f"{symbol}_{date_tag}_tushare_v{candidate_version}"
        if not (directory / f"{snapshot_id}.json").exists():
            return snapshot_id, candidate_version
        candidate_version += 1


# ---------------------------------------------------------------------------
# Pure snapshot construction
# ---------------------------------------------------------------------------
def merge_bars(
    base_bars: Sequence[dict[str, Any]],
    fetched_bars: Sequence[dict[str, Any]],
    cutoff_date: str,
) -> list[dict[str, Any]]:
    """Merge base and freshly fetched RAW bars, newest first.

    Same-day bars from the provider win (they are the authoritative fact); the
    base contributes the older history a chart needs.  Nothing at or after the
    research cutoff is ever included — a bar dated *after* the cutoff would be a
    future fact.
    """
    merged: dict[str, dict[str, Any]] = {}
    for bar in base_bars:
        day = bar.get("trade_date")
        if day and str(day) <= cutoff_date:
            merged[str(day)] = bar
    for bar in fetched_bars:
        day = bar.get("trade_date")
        if day and str(day) <= cutoff_date:
            merged[str(day)] = bar
    return [merged[day] for day in sorted(merged, reverse=True)]


def _latest_trade_date(bars: Sequence[dict[str, Any]]) -> Optional[str]:
    return max((str(bar["trade_date"]) for bar in bars if bar.get("trade_date")), default=None)


def build_refreshed_snapshot(
    base_payload: dict[str, Any],
    *,
    fetched_bars: Sequence[dict[str, Any]],
    research_as_of: str,
    generated_at: str,
    snapshot_id: str,
    version: int,
    bars_only: bool,
    fetch_meta: dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
    statements: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Assemble the payload of a new snapshot from a base plus fetched facts.

    Pure: it neither reads nor writes the filesystem.
    """
    cutoff_date = research_as_of[:10]
    base_data = base_payload.get("data") or {}
    bars = merge_bars(base_data.get("bars", []), fetched_bars, cutoff_date)

    data = dict(base_data)
    data["bars"] = bars
    if profile is not None:
        data["profile"] = profile
    if statements is not None:
        # Re-apply the point-in-time cutoff: a statement disclosed after the new
        # research cutoff must not leak into the refreshed snapshot.
        data["statements"] = [
            item for item in statements
            if not item.get("available_at") or item["available_at"] <= research_as_of
        ]

    payload = {key: value for key, value in base_payload.items() if key not in _REFRESH_OWNED_KEYS}
    payload.update({
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_version": version,
        "provider": "tushare",
        "symbol": base_payload["symbol"],
        "research_as_of": research_as_of,
        "fetched_at": generated_at,
        "generated_at": generated_at,
        "data_quality": "provider_refreshed_bars" if bars_only else "provider_refreshed_full",
        "source_metadata": {
            "refresh": True,
            "bars_only": bars_only,
            "base_snapshot_id": base_payload.get("snapshot_id"),
            "base_research_as_of": base_payload.get("research_as_of"),
            "generated_at": generated_at,
            "normalization_version": NORMALIZATION_VERSION,
            "price_basis": "RAW",
            "latest_trade_date": _latest_trade_date(bars),
            **(fetch_meta or {}),
        },
        "data": data,
    })
    return payload


# ---------------------------------------------------------------------------
# Provider interop
# ---------------------------------------------------------------------------
def _bar_to_dict(bar: Any) -> dict[str, Any]:
    item = asdict(bar)
    item.pop("symbol", None)
    return item


def _profile_to_dict(profile: Any) -> dict[str, Any]:
    item = asdict(profile)
    # ``asdict`` recursively turns the nested ``CanonicalSymbol`` into a mapping.
    symbol_value = item.pop("symbol", None)
    if isinstance(symbol_value, dict):
        code = str(symbol_value.get("code", ""))
    else:
        code = str(symbol_value or "").split(".")[0]
    # ``normalize_profile`` cannot know whether an issuer is a bank/insurer; that
    # is a deterministic canonical-symbol classification, and dropping it would
    # silently re-enable PE/PB/PS methods for a financial institution.
    item["entity_type"] = "financial_institution" if is_financial_institution(code) else "operating_company"
    return item


def classify_provider_error(exc: Exception) -> tuple[str, str, bool]:
    """Map a provider exception to ``(refresh_status, reason_code, retryable)``."""
    if isinstance(exc, provider_errors.AuthenticationError):
        return REFRESH_FAILED, "credentials_invalid", False
    if isinstance(exc, provider_errors.PermissionError):
        return REFRESH_FAILED, "call_budget_exhausted", False
    if isinstance(exc, provider_errors.RateLimitError):
        return REFRESH_FAILED, "rate_limited", True
    if isinstance(exc, provider_errors.EmptyDataError):
        return REFRESH_NO_DATA, "provider_no_data", False
    if isinstance(exc, provider_errors.UnsupportedSymbolError):
        return REFRESH_FAILED, "unsupported_symbol", False
    if isinstance(exc, provider_errors.TimeoutError):
        return REFRESH_FAILED, "provider_timeout", True
    if isinstance(exc, provider_errors.ProviderError):
        return REFRESH_FAILED, "provider_error", True
    if isinstance(exc, (ConnectionError, OSError)):
        return REFRESH_FAILED, "network_error", True
    message = str(exc).lower()
    if any(hint in message for hint in _RATE_LIMIT_HINTS):
        return REFRESH_FAILED, "rate_limited", True
    if any(hint in message for hint in _NETWORK_HINTS):
        return REFRESH_FAILED, "network_error", True
    return REFRESH_FAILED, "provider_error", True


def safe_failure_message(reason_code: str) -> str:
    """Return UI-safe refresh feedback without exposing upstream exception text."""
    messages = {
        "credentials_invalid": "Tushare 凭据不可用；请在服务器端检查配置后重试。",
        "call_budget_exhausted": "本次联网调用额度已用尽；请稍后重试。",
        "rate_limited": "数据源请求频率受限；请稍后重试。",
        "provider_no_data": "数据源未返回可用日线数据。",
        "unsupported_symbol": "该证券代码不受支持。",
        "provider_timeout": "数据源请求超时；可稍后重试。",
        "network_error": "连接数据源失败；请检查网络后重试。",
        "provider_error": "数据源暂时不可用；可稍后重试。",
    }
    return messages.get(reason_code, "联网更新失败；请稍后重试。")


def fetch_latest_bars(
    provider: FinancialDataProvider,
    symbol: Any,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Fetch RAW daily bars in ``[start_date, end_date]`` (``YYYYMMDD``)."""
    bars = provider.get_daily_bars(symbol, start_date=start_date, end_date=end_date, adjustment="RAW")
    return [_bar_to_dict(bar) for bar in bars]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _outcome(
    *,
    refresh_status: str,
    reason_code: Optional[str],
    message: str,
    symbol: str,
    retryable: bool = False,
    bars_only: bool = True,
    provider: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    research_as_of: Optional[str] = None,
    generated_at: Optional[str] = None,
    version: Optional[int] = None,
    latest_trade_date: Optional[str] = None,
    new_bar_count: int = 0,
    freshness: Optional[dict[str, Any]] = None,
    current_snapshot: Optional[dict[str, Any]] = None,
    call_budget: Optional[int] = None,
    calls_used: Optional[int] = None,
    error_type: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "refresh_status": refresh_status,
        "reason_code": reason_code,
        "message": message,
        "retryable": retryable,
        "symbol": symbol,
        "bars_only": bars_only,
        "provider": provider,
        "snapshot_id": snapshot_id,
        "research_as_of": research_as_of,
        "generated_at": generated_at,
        "version": version,
        "latest_trade_date": latest_trade_date,
        "new_bar_count": new_bar_count,
        "freshness": freshness,
        "current_snapshot": current_snapshot,
        "call_budget": call_budget,
        "calls_used": calls_used,
        "error_type": error_type,
    }


def _current_snapshot_view(payload: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """What the UI shows on failure: the snapshot still in use, and its cutoff."""
    if payload is None:
        return None
    bars = [bar for bar in (payload.get("data") or {}).get("bars", []) if bar.get("adjustment") == "RAW"]
    return {
        "snapshot_id": payload.get("snapshot_id"),
        "research_as_of": payload.get("research_as_of"),
        "provider": payload.get("provider"),
        "latest_trade_date": _latest_trade_date(bars),
    }


def refresh_snapshot(
    symbol_text: str,
    *,
    snapshot_dir: Path | str,
    bars_only: bool = True,
    research_as_of: Optional[str] = None,
    now: Optional[datetime] = None,
    provider: Optional[FinancialDataProvider] = None,
    token_loader: Optional[Callable[[], Optional[str]]] = None,
    base_snapshot_id: Optional[str] = None,
    materialize_cutoff: bool = False,
    write: bool = True,
) -> dict[str, Any]:
    """Pull the latest A-share close and materialise a new versioned snapshot.

    ``provider`` / ``token_loader`` / ``now`` / ``write`` are injectable so the
    whole flow can be exercised offline without credentials, network, or a real
    clock.  No exception escapes except :class:`UnsupportedSymbolError` and
    :class:`ValueError` for a malformed request; provider faults become explicit
    failure states.
    """
    symbol = str(normalize_cn_symbol(symbol_text))
    reference_now = _coerce_now(now)
    generated_at = reference_now.isoformat(timespec="seconds")
    cutoff = coerce_research_as_of(research_as_of, reference_now)
    cutoff_date = cutoff[:10]
    directory = Path(snapshot_dir)

    if base_snapshot_id:
        base = next(
            (
                payload for payload in iter_snapshots(directory)
                if payload.get("snapshot_id") == base_snapshot_id
                and payload.get("symbol") == symbol
                and is_provider_backed(payload)
            ),
            None,
        )
    else:
        base = find_base_snapshot(symbol, directory)
    if base is None:
        had_any = any(payload.get("symbol") == symbol for payload in iter_snapshots(directory))
        reason = "base_snapshot_not_provider_backed" if had_any else "base_snapshot_missing"
        message = (
            "该标的只有演示/fixture 快照，不能作为联网刷新的基线（会把合成财务数据当成真实事实）。"
            if had_any else
            "本地没有该标的的 provider 快照；请先运行受控采集（POST /api/snapshots/acquire）建立基线。"
        )
        return _outcome(
            refresh_status=REFRESH_BLOCKED, reason_code=reason, message=message,
            symbol=symbol, bars_only=bars_only, retryable=False,
        )

    base_view = _current_snapshot_view(base)
    base_latest = base_view["latest_trade_date"] if base_view else None
    base_bars = [
        bar for bar in (base.get("data") or {}).get("bars", [])
        if bar.get("trade_date") and str(bar["trade_date"]) <= cutoff_date
    ]
    base_latest_at_cutoff = _latest_trade_date(base_bars)

    # Window: never past the requested cutoff, never past "today".
    end_date = min(cutoff_date, reference_now.date().isoformat())
    start_date = (date.fromisoformat(end_date) - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat()
    window = {"start_date": start_date.replace("-", ""), "end_date": end_date.replace("-", "")}

    call_budget = CALL_BUDGET_BARS_ONLY if bars_only else CALL_BUDGET_FULL
    if provider is None:
        loader = token_loader
        if loader is None:
            from .daily_review.collector import load_tushare_token

            loader = load_tushare_token
        token = (loader() or "").strip()
        if not token:
            return _outcome(
                refresh_status=REFRESH_FAILED, reason_code="credentials_missing",
                message="未配置 TUSHARE_TOKEN；联网更新未启动。请仅在服务器 .env 中配置 Token。",
                symbol=symbol, bars_only=bars_only, retryable=False,
                current_snapshot=base_view, call_budget=call_budget,
            )
        provider = TushareProvider(token, call_budget=TushareCallBudget(max_calls=call_budget))

    latest_bars: list[dict[str, Any]] = []
    statements_dicts: Optional[list[dict[str, Any]]] = None
    profile_dict: Optional[dict[str, Any]] = None
    try:
        latest_bars = fetch_latest_bars(provider, normalize_cn_symbol(symbol), **window)
        if not bars_only:
            statements = provider.get_financial_statements(normalize_cn_symbol(symbol), research_as_of=cutoff)
            statements_dicts = [asdict(statement) for statement in statements]
            fetched_profile = provider.get_profile(normalize_cn_symbol(symbol))
            if fetched_profile is not None:
                profile_dict = _profile_to_dict(fetched_profile)
    except Exception as exc:  # classified, never swallowed silently
        status, reason, retryable = classify_provider_error(exc)
        # Preserve diagnostic detail only in the server log.  Provider messages
        # can contain request URLs or implementation details and must not cross
        # the API/UI boundary.
        LOGGER.warning("Snapshot refresh failed: symbol=%s reason=%s", symbol, reason, exc_info=True)
        return _outcome(
            refresh_status=status, reason_code=reason,
            message=safe_failure_message(reason),
            symbol=symbol, bars_only=bars_only, retryable=retryable,
            provider="tushare", current_snapshot=base_view,
            call_budget=call_budget,
            calls_used=getattr(getattr(provider, "call_budget", None), "used_calls", None),
            error_type=type(exc).__name__,
        )

    calls_used = getattr(getattr(provider, "call_budget", None), "used_calls", None)
    # Only bars at or before the cutoff may enter the snapshot (no future facts).
    fetched_in_window = [bar for bar in latest_bars if str(bar.get("trade_date", "")) <= cutoff_date]
    if not fetched_in_window:
        return _outcome(
            refresh_status=REFRESH_NO_DATA, reason_code="no_bars_at_or_before_cutoff",
            message="Provider 未返回研究截止时间及之前的任何日线数据（可能停牌或该日期尚未收盘）。",
            symbol=symbol, bars_only=bars_only, retryable=False,
            provider="tushare", current_snapshot=base_view,
            call_budget=call_budget, calls_used=calls_used,
        )

    merged = merge_bars(base_bars, fetched_in_window, cutoff_date)
    merged_latest = _latest_trade_date(merged)
    freshness = describe_freshness(merged_latest, reference_now)

    base_cutoff_date = str(base.get("research_as_of", ""))[:10]
    if (
        base_latest_at_cutoff is not None
        and merged_latest is not None
        and merged_latest <= base_latest_at_cutoff
        and (not materialize_cutoff or base_cutoff_date == cutoff_date)
    ):
        return _outcome(
            refresh_status=REFRESH_NO_NEW_DATA, reason_code="already_latest",
            message=f"已是本地最新：快照已包含 {base_latest_at_cutoff} 的收盘价，Provider 未返回更新的交易日。",
            symbol=symbol, bars_only=bars_only, retryable=False,
            provider="tushare", research_as_of=cutoff, generated_at=generated_at,
            latest_trade_date=merged_latest, freshness=freshness,
            current_snapshot=base_view, call_budget=call_budget, calls_used=calls_used,
        )

    version = next_snapshot_version(symbol, directory)
    snapshot_id, version = _unique_snapshot_id(symbol, cutoff_date.replace("-", ""), version, directory)
    fetch_meta = {
        "fetch_window": window,
        "call_budget": call_budget,
        "calls_used": calls_used,
        # A coordinated peer run may need to record a new immutable version on
        # a non-trading day even when the close itself is unchanged.  Keep that
        # intent in provenance rather than making the new cutoff look like an
        # unexplained ordinary refresh.
        "coordinated_cutoff": bool(materialize_cutoff),
        "financials_source": "base_snapshot" if statements_dicts is None else "provider",
        "latest_trade_date": merged_latest,
    }
    payload = build_refreshed_snapshot(
        base,
        fetched_bars=fetched_in_window,
        research_as_of=cutoff,
        generated_at=generated_at,
        snapshot_id=snapshot_id,
        version=version,
        bars_only=bars_only,
        fetch_meta=fetch_meta,
        profile=profile_dict,
        statements=statements_dicts,
    )

    new_bar_count = len([
        bar for bar in merged
        if base_latest_at_cutoff is None or str(bar["trade_date"]) > base_latest_at_cutoff
    ])

    if write:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{snapshot_id}.json"
        if target.exists():  # pragma: no cover - guarded by _unique_snapshot_id
            raise ValueError(f"Refusing to overwrite an existing snapshot: {target.name}")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return _outcome(
        refresh_status=REFRESH_SUCCEEDED, reason_code=None,
        message=f"已创建新快照 {snapshot_id}（v{version}），收盘价截至 {merged_latest}。",
        symbol=symbol, bars_only=bars_only, retryable=False,
        provider="tushare", snapshot_id=snapshot_id, research_as_of=cutoff,
        generated_at=generated_at, version=version,
        latest_trade_date=merged_latest, new_bar_count=new_bar_count,
        freshness=freshness, current_snapshot=base_view,
        call_budget=call_budget, calls_used=calls_used,
    )
