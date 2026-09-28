"""Offline tests for online snapshot refresh and freshness display.

Everything here runs without network, credentials, ``.env``, or wall-clock time:
the provider is injected, the clock is passed in, and the snapshot directory is a
``tmp_path``.  That is the point — a refresh either produces an explainable new
snapshot or an explicit failure state, and both must be verifiable offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.cn import snapshot_refresh as refresh
from src.cn import workbench_service as service
from src.cn.domain import CompanyProfile, FinancialStatement, MarketBar
from src.cn.errors import EmptyDataError
from src.cn.symbols import CanonicalSymbol

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 28, 16, 30, tzinfo=CST)  # a Monday, after A-share close
SYMBOL = "600519.SH"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------
def _statement(statement_type: str, fiscal_period: str, published: str, values: dict) -> dict:
    basis = "_END" if statement_type == "balance" else "_CUMULATIVE"
    return {
        "statement_type": statement_type, "fiscal_period": fiscal_period,
        "period_basis": f"{fiscal_period}{basis}", "published_at": published,
        "available_at": published, "currency": "CNY", "unit": "CNY",
        "values": values, "is_restated": False,
    }


def _base_payload(*, latest: str = "2026-09-19", include_shares: bool = True,
                  provider: str = "tushare", symbol: str = SYMBOL) -> dict:
    balance_values: dict = {"equity": 200_000_000_000.0}
    if include_shares:
        balance_values["total_shares"] = 1_256_197_800.0
    return {
        "schema_version": "1.0.0",
        "snapshot_id": f"{symbol}_20260920_tushare_v1",
        "provider": provider,
        "symbol": symbol,
        "research_as_of": "2026-09-20T23:00:00+08:00",
        "fetched_at": "2026-09-20T23:00:00+08:00",
        "data_quality": "provider_normalized_from_cn0_probe",
        "source_metadata": {"normalization_version": "tushare-cn-fields-1.1.0"},
        "data": {
            "profile": {
                "name": "贵州茅台", "currency": "CNY",
                "industry_standard": "tushare_stock_basic", "industry_level": "industry",
                "industry_name": "白酒", "entity_type": "operating_company",
            },
            "bars": [{
                "trade_date": latest, "open": 1600.0, "high": 1620.0, "low": 1590.0,
                "close": 1610.0, "volume": 3_000_000.0, "amount": 4.8e9,
                "adjustment": "RAW", "trading_status": None, "adj_factor": None,
            }],
            "statements": [
                _statement("income", "2024FY", "2025-03-30T00:00:00+08:00",
                           {"revenue": 170_000_000_000.0, "net_profit": 86_000_000_000.0}),
                _statement("balance", "2024FY", "2025-03-30T00:00:00+08:00", balance_values),
            ],
        },
    }


def _write_base(directory: Path, **kwargs) -> dict:
    payload = _base_payload(**kwargs)
    (directory / f"{payload['snapshot_id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _market_bar(trade_date: str, close: float = 1700.0) -> MarketBar:
    return MarketBar(
        symbol=CanonicalSymbol("600519", "SH"), trade_date=trade_date,
        open=close - 5, high=close + 10, low=close - 15, close=close,
        volume=2_500_000.0, amount=4.2e9, adjustment="RAW",
    )


def _financial_statement(statement_type: str, fiscal_period: str, published: str, values: dict) -> FinancialStatement:
    """A real domain object — the provider contract returns models, not dicts."""
    return FinancialStatement(**_statement(statement_type, fiscal_period, published, values))


class _Budget:
    used_calls = 1


class _FakeProvider:
    """A provider whose behaviour is entirely controlled by the test."""

    name = "tushare"

    def __init__(self, *, bars=(), profile=None, statements=(), error: Exception | None = None):
        self._bars = list(bars)
        self._profile = profile
        self._statements = list(statements)
        self._error = error
        self.call_budget = _Budget()

    def get_daily_bars(self, symbol, *, start_date=None, end_date=None, adjustment="RAW"):
        if self._error is not None:
            raise self._error
        return list(self._bars)

    def get_profile(self, symbol):
        if self._error is not None:
            raise self._error
        return self._profile

    def get_financial_statements(self, symbol, *, research_as_of=None):
        if self._error is not None:
            raise self._error
        return list(self._statements)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_freshness_label_is_computed_and_never_hardcoded():
    assert refresh.describe_freshness("2026-09-28", NOW)["label"] == "今日收盘后更新"
    before_close = datetime(2026, 9, 28, 14, 59, tzinfo=CST)
    intraday = refresh.describe_freshness("2026-09-28", before_close)
    assert intraday["status"] == "awaiting_close"
    assert intraday["label"] == "当日尚未收盘，收盘价待确认"
    assert intraday["stale"] is False
    assert refresh.describe_freshness("2026-09-25", NOW)["label"] == "距今 1 个交易日"
    # Saturday: a weekend pull must say so instead of pretending it is current.
    saturday = datetime(2026, 9, 26, 11, 0, tzinfo=CST)
    weekend = refresh.describe_freshness("2026-09-25", saturday)
    assert weekend["status"] == "non_trading_day"
    assert weekend["label"] == "非交易日，使用最近交易日（2026-09-25）"
    assert weekend["trading_days_since"] == 0
    # A long gap is surfaced as stale rather than silently accepted.
    assert refresh.describe_freshness("2026-08-28", NOW)["stale"] is True


def test_merge_bars_is_newest_first_excludes_future_and_prefers_provider_facts():
    base = [
        {"trade_date": "2026-09-18", "close": 1.0, "adjustment": "RAW"},
        {"trade_date": "2026-09-19", "close": 2.0, "adjustment": "RAW"},
    ]
    fetched = [
        {"trade_date": "2026-09-19", "close": 2.5, "adjustment": "RAW"},
        {"trade_date": "2026-09-28", "close": 3.0, "adjustment": "RAW"},
        {"trade_date": "2026-09-30", "close": 9.9, "adjustment": "RAW"},  # after cutoff
    ]

    merged = refresh.merge_bars(base, fetched, cutoff_date="2026-09-28")

    assert [bar["trade_date"] for bar in merged] == ["2026-09-28", "2026-09-19", "2026-09-18"]
    assert merged[1]["close"] == 2.5  # the provider fact wins for the same day


def test_next_snapshot_version_is_monotonic_per_symbol(tmp_path):
    _write_base(tmp_path)
    other = _base_payload(symbol="000858.SZ")
    other["snapshot_id"] = "000858.SZ_20260920_tushare_v7"
    (tmp_path / f"{other['snapshot_id']}.json").write_text(json.dumps(other), encoding="utf-8")
    (tmp_path / "600519.SH_illustrative_demo_v3.json").write_text(
        json.dumps({**_base_payload(), "snapshot_id": "600519.SH_illustrative_demo_v3",
                    "provider": "illustrative_fixture"}), encoding="utf-8"
    )

    assert refresh.next_snapshot_version(SYMBOL, tmp_path) == 4  # v1 and v3 already used
    assert refresh.next_snapshot_version("000858.SZ", tmp_path) == 8


def test_research_as_of_accepts_date_only_and_rejects_garbage():
    assert refresh.coerce_research_as_of("2026-09-26", NOW) == "2026-09-26T23:00:00+08:00"
    assert refresh.coerce_research_as_of(None, NOW) == NOW.isoformat(timespec="seconds")
    with pytest.raises(ValueError):
        refresh.coerce_research_as_of("not-a-date", NOW)


# ---------------------------------------------------------------------------
# Orchestration: success
# ---------------------------------------------------------------------------
def test_refresh_creates_a_new_versioned_snapshot_without_touching_the_base(tmp_path):
    base = _write_base(tmp_path)
    base_path = tmp_path / f"{base['snapshot_id']}.json"
    base_bytes = base_path.read_bytes()

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW,
        provider=_FakeProvider(bars=[_market_bar("2026-09-26"), _market_bar("2026-09-28", 1750.0)]),
    )

    assert outcome["refresh_status"] == "succeeded"
    assert outcome["snapshot_id"] == "600519.SH_20260928_tushare_v2"
    assert outcome["version"] == 2
    assert outcome["latest_trade_date"] == "2026-09-28"
    assert outcome["freshness"]["label"] == "今日收盘后更新"
    assert outcome["new_bar_count"] == 2
    assert outcome["current_snapshot"]["snapshot_id"] == base["snapshot_id"]

    # The historical snapshot is byte-identical: a refresh only adds.
    assert base_path.read_bytes() == base_bytes
    written = json.loads((tmp_path / f"{outcome['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert written["provider"] == "tushare"
    assert written["snapshot_version"] == 2
    assert written["source_metadata"]["refresh"] is True
    assert written["source_metadata"]["bars_only"] is True
    assert written["source_metadata"]["base_snapshot_id"] == base["snapshot_id"]
    assert written["data"]["bars"][0]["trade_date"] == "2026-09-28"
    assert all(bar["adjustment"] == "RAW" for bar in written["data"]["bars"])
    # Financial inputs stay the disclosed ones from the base snapshot.
    assert written["data"]["statements"] == base["data"]["statements"]


def test_coordinated_cutoff_materializes_a_version_without_a_new_trading_day(tmp_path):
    """A peer run records its common cutoff without inventing a newer price."""
    base = _write_base(tmp_path, latest="2026-09-28")
    next_day = datetime(2026, 9, 29, 16, 30, tzinfo=CST)

    outcome = refresh.refresh_snapshot(
        SYMBOL,
        snapshot_dir=tmp_path,
        now=next_day,
        research_as_of="2026-09-29",
        materialize_cutoff=True,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )

    assert outcome["refresh_status"] == "succeeded"
    written = json.loads((tmp_path / f"{outcome['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert written["research_as_of"] == "2026-09-29T23:00:00+08:00"
    assert written["data"]["bars"][0]["trade_date"] == "2026-09-28"
    assert written["source_metadata"]["base_snapshot_id"] == base["snapshot_id"]
    assert written["source_metadata"]["coordinated_cutoff"] is True


def test_bars_only_refresh_reuses_disclosed_financials_and_full_refresh_can_replace_them(tmp_path):
    # Two directories: refreshing twice in one directory would (correctly) report
    # "already latest" on the second call, which is not what this test probes.
    bars_only_dir = tmp_path / "bars_only"
    full_dir = tmp_path / "full"
    bars_only_dir.mkdir()
    full_dir.mkdir()
    base = _write_base(bars_only_dir)
    _write_base(full_dir)
    fetched_statement = _financial_statement(
        "balance", "2026H1", "2026-08-25T00:00:00+08:00", {"total_shares": 9.9e9},
    )

    reused = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=bars_only_dir, now=NOW, bars_only=True,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")], statements=[fetched_statement]),
    )
    assert reused["refresh_status"] == "succeeded"
    reused_payload = json.loads((bars_only_dir / f"{reused['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert reused_payload["data"]["statements"] == base["data"]["statements"]
    assert reused_payload["source_metadata"]["financials_source"] == "base_snapshot"

    full = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=full_dir, now=NOW, bars_only=False,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")], statements=[fetched_statement]),
    )
    assert full["refresh_status"] == "succeeded"
    assert full["version"] == 2
    full_payload = json.loads((full_dir / f"{full['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert any(item["fiscal_period"] == "2026H1" for item in full_payload["data"]["statements"])
    assert full_payload["source_metadata"]["financials_source"] == "provider"


def test_full_refresh_drops_statements_disclosed_after_the_research_cutoff(tmp_path):
    _write_base(tmp_path)
    future_statement = _financial_statement(
        "balance", "2026Q3", "2026-10-25T00:00:00+08:00", {"total_shares": 8.8e9},
    )

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW, bars_only=False,
        research_as_of="2026-09-28",
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")], statements=[future_statement]),
    )
    assert outcome["refresh_status"] == "succeeded"

    payload = json.loads((tmp_path / f"{outcome['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert all(item["fiscal_period"] != "2026Q3" for item in payload["data"]["statements"])


# ---------------------------------------------------------------------------
# Orchestration: explicit failure / no-op states
# ---------------------------------------------------------------------------
def test_full_refresh_preserves_the_financial_institution_entity_boundary(tmp_path):
    """A refreshed profile must not silently re-enable PE/PB/PS for a bank."""
    bank = "601398.SH"
    _write_base(tmp_path, symbol=bank)
    profile = CompanyProfile(
        symbol=CanonicalSymbol("601398", "SH"), name="工商银行",
        industry_standard="tushare_stock_basic", industry_level="industry", industry_name="银行",
    )

    outcome = refresh.refresh_snapshot(
        bank, snapshot_dir=tmp_path, now=NOW, bars_only=False,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")], profile=profile),
    )

    assert outcome["refresh_status"] == "succeeded"
    payload = json.loads((tmp_path / f"{outcome['snapshot_id']}.json").read_text(encoding="utf-8"))
    assert payload["data"]["profile"]["entity_type"] == "financial_institution"
    assert payload["data"]["profile"]["name"] == "工商银行"


def test_already_latest_returns_no_new_data_and_writes_nothing(tmp_path):
    _write_base(tmp_path, latest="2026-09-28")

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )

    assert outcome["refresh_status"] == "no_new_data"
    assert outcome["reason_code"] == "already_latest"
    assert outcome["snapshot_id"] is None
    assert outcome["current_snapshot"]["latest_trade_date"] == "2026-09-28"
    assert len(list(tmp_path.glob("*.json"))) == 1  # no new file


def test_missing_credentials_fail_clearly_and_report_the_snapshot_in_use(tmp_path):
    _write_base(tmp_path)

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW, token_loader=lambda: None,
    )

    assert outcome["refresh_status"] == "failed"
    assert outcome["reason_code"] == "credentials_missing"
    assert outcome["retryable"] is False
    assert outcome["current_snapshot"]["research_as_of"] == "2026-09-20T23:00:00+08:00"
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "error, expected_reason, retryable",
    [
        (ConnectionError("connection reset by peer"), "network_error", True),
        (RuntimeError("抱歉，您每分钟最多访问该接口500次"), "rate_limited", True),
        (EmptyDataError("Tushare 'daily' returned no data."), "provider_no_data", False),
    ],
)
def test_provider_faults_map_to_structured_states(tmp_path, error, expected_reason, retryable):
    _write_base(tmp_path)

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW, provider=_FakeProvider(error=error),
    )

    assert outcome["reason_code"] == expected_reason
    assert outcome["retryable"] is retryable
    assert outcome["error_type"] == type(error).__name__
    assert len(list(tmp_path.glob("*.json"))) == 1  # never fabricates a snapshot


def test_provider_exception_text_stays_in_server_logs_not_the_refresh_outcome(tmp_path):
    _write_base(tmp_path)
    secret_detail = "https://provider.example/?token=must-not-reach-ui"

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW,
        provider=_FakeProvider(error=RuntimeError(secret_detail)),
    )

    assert outcome["reason_code"] == "provider_error"
    assert outcome["message"] == "数据源暂时不可用；可稍后重试。"
    assert secret_detail not in outcome["message"]


def test_refresh_without_a_provider_baseline_is_blocked_not_guessed(tmp_path):
    empty = refresh.refresh_snapshot(SYMBOL, snapshot_dir=tmp_path, now=NOW,
                                     provider=_FakeProvider(bars=[_market_bar("2026-09-28")]))
    assert empty["refresh_status"] == "blocked"
    assert empty["reason_code"] == "base_snapshot_missing"

    demo = _write_base(tmp_path, provider="illustrative_fixture")
    demo["snapshot_id"] = f"{SYMBOL}_illustrative_demo_v1"
    (tmp_path / f"{demo['snapshot_id']}.json").write_text(json.dumps(demo), encoding="utf-8")
    refused = refresh.refresh_snapshot(SYMBOL, snapshot_dir=tmp_path, now=NOW,
                                      provider=_FakeProvider(bars=[_market_bar("2026-09-28")]))
    assert refused["refresh_status"] == "blocked"
    assert refused["reason_code"] == "base_snapshot_not_provider_backed"


def test_bars_after_the_cutoff_are_not_available_yet(tmp_path):
    _write_base(tmp_path, latest="2026-09-10")

    outcome = refresh.refresh_snapshot(
        SYMBOL, snapshot_dir=tmp_path, now=NOW, research_as_of="2026-09-12",
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )

    assert outcome["refresh_status"] == "no_data"
    assert outcome["reason_code"] == "no_bars_at_or_before_cutoff"


# ---------------------------------------------------------------------------
# Service layer: freshness display + Validator blocking
# ---------------------------------------------------------------------------
def test_snapshot_freshness_uses_the_snapshots_own_latest_raw_close(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)
    base = _write_base(tmp_path)

    freshness = service.snapshot_freshness(base["snapshot_id"], now=NOW)

    assert freshness["latest_trade_date"] == "2026-09-19"
    assert freshness["provider"] == "tushare"
    assert freshness["status"] == "recent"
    assert "距今" in freshness["label"]


def test_peer_sync_uses_only_pre_cutoff_provider_bases_and_is_bounded(tmp_path, monkeypatch):
    """A group refresh cannot leak a later peer snapshot into the target cutoff."""
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)
    target = _write_base(tmp_path, symbol="000001.SZ")
    bank_a = _write_base(tmp_path, symbol="601398.SH")
    bank_b = _write_base(tmp_path, symbol="601939.SH")
    _write_base(tmp_path, symbol="600519.SH")  # different industry, never selected
    future_bank = _base_payload(symbol="601398.SH")
    future_bank["snapshot_id"] = "601398.SH_20260921_tushare_v2"
    future_bank["research_as_of"] = "2026-09-21T23:00:00+08:00"
    (tmp_path / f"{future_bank['snapshot_id']}.json").write_text(
        json.dumps(future_bank, ensure_ascii=False), encoding="utf-8"
    )
    service.load_snapshot.cache_clear()

    calls: list[tuple[str, dict]] = []

    def fake_refresh(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return {
            "refresh_status": refresh.REFRESH_SUCCEEDED,
            "snapshot_id": f"{symbol}_20260920_tushare_v2",
        }

    result = service.refresh_peer_group(target["snapshot_id"], now=NOW, refresh_fn=fake_refresh)

    assert result["status"] == "completed"
    assert result["synchronized_count"] == 3
    assert result["failed_count"] == 0
    assert [symbol for symbol, _ in calls] == ["000001.SZ", "601398.SH", "601939.SH"]
    assert calls[1][1]["base_snapshot_id"] == bank_a["snapshot_id"]
    assert calls[2][1]["base_snapshot_id"] == bank_b["snapshot_id"]
    assert all(kwargs["bars_only"] is True for _, kwargs in calls)
    assert all(kwargs["materialize_cutoff"] is True for _, kwargs in calls)
    assert all(kwargs["research_as_of"] == target["research_as_of"] for _, kwargs in calls)
    service.load_snapshot.cache_clear()


def test_local_baseline_selection_does_not_reselect_a_newer_online_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)
    base = _write_base(tmp_path)
    refreshed = refresh.refresh_snapshot(
        SYMBOL,
        snapshot_dir=tmp_path,
        now=NOW,
        provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )
    service.load_snapshot.cache_clear()

    selected = service.find_local_baseline_snapshot(refreshed["snapshot_id"])

    assert selected is not None
    assert selected["id"] == base["snapshot_id"]
    assert selected["provider"] == "tushare"
    service.load_snapshot.cache_clear()


def test_validator_blocks_a_refreshed_snapshot_whose_total_shares_are_missing(tmp_path, monkeypatch):
    """A refresh copies facts; it must never invent a valuation-critical input."""
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)
    base = _write_base(tmp_path, include_shares=False)

    outcome = service.refresh_snapshot(
        SYMBOL, now=NOW, provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )
    service.load_snapshot.cache_clear()

    assert outcome["refresh_status"] == "succeeded"
    refreshed = service.load_snapshot(outcome["snapshot_id"])
    assert all("total_shares" not in statement["values"] for statement in refreshed["data"]["statements"])

    readiness = service.research_readiness(outcome["snapshot_id"])
    assert readiness["checks"]["key_metrics"]["status"] == "blocked"
    valuation = service.research_valuation(outcome["snapshot_id"])
    assert valuation["conclusion_allowed"] is False
    assert valuation["gated_reason"] == "validator_blocked"
    assert all(
        interval["implied_price_median"] is None
        for interval in valuation["valuation_ranges"].values()
    )


def test_service_refresh_persists_and_is_discoverable_via_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)
    _write_base(tmp_path)

    outcome = service.refresh_snapshot(
        SYMBOL, now=NOW, provider=_FakeProvider(bars=[_market_bar("2026-09-28")]),
    )
    service.load_snapshot.cache_clear()

    catalog_ids = {item["id"] for item in service.list_research()}
    assert outcome["snapshot_id"] in catalog_ids
    assert service.find_snapshot_for_symbol(SYMBOL)["id"] == outcome["snapshot_id"]
    refreshed_item = next(item for item in service.list_research() if item["id"] == outcome["snapshot_id"])
    assert refreshed_item["snapshot_origin"] == "online_refresh"
    assert refreshed_item["latest_raw_close_date"] == "2026-09-28"
