"""Offline peer selection regressions using explicitly illustrative facts."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from src.cn import workbench_service as service
from src.cn.peer_workflow import value_with_peers


CUTOFF = "2026-09-23T23:00:00+08:00"


def snapshot(symbol, *, snapshot_id=None, price=10, cutoff=CUTOFF):
    statements = []
    for period, revenue, profit in (("2025FY", 200, 20), ("2025H1", 80, 8), ("2026H1", 100, 10)):
        statements.append({
            "statement_type": "income", "fiscal_period": period,
            "period_basis": f"{period}_CUMULATIVE", "published_at": "2026-08-01T00:00:00+08:00",
            "available_at": "2026-08-01T00:00:00+08:00", "currency": "CNY", "unit": "CNY",
            "values": {"revenue": revenue, "net_profit": profit},
        })
    statements.append({
        "statement_type": "balance", "fiscal_period": "2026H1", "period_basis": "2026H1_END",
        "published_at": "2026-08-01T00:00:00+08:00", "available_at": "2026-08-01T00:00:00+08:00",
        "currency": "CNY", "unit": "CNY", "values": {"equity": 100, "total_shares": 10},
    })
    return {
        "schema_version": "1.0.0", "snapshot_id": snapshot_id or f"{symbol}_illustrative",
        "symbol": symbol, "provider": "illustrative_fixture", "research_as_of": cutoff,
        "fetched_at": cutoff, "data_quality": "illustrative_test_fixture_not_for_research",
        "data": {"profile": {"name": symbol, "currency": "CNY"},
                 "bars": [{"trade_date": "2026-09-21", "close": price, "adjustment": "RAW"}],
                 "statements": statements},
    }


@pytest.fixture
def evaluate(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)

    def run(target, peers):
        monkeypatch.setattr(service, "load_snapshot", lambda _: target)
        for index, payload in enumerate(peers):
            (tmp_path / f"{index}.json").write_text(json.dumps(payload), encoding="utf-8")
        return service.research_peer_valuation(target["snapshot_id"])

    return run


def test_duplicate_snapshots_use_one_version_for_median_distribution_and_evidence(evaluate):
    target = snapshot("603605.SH")
    old = snapshot("300957.SZ", snapshot_id="peer_old", price=100)
    old["fetched_at"] = "2026-09-24T12:00:00+08:00"
    middle = snapshot("300957.SZ", snapshot_id="peer_middle", price=50)
    middle["fetched_at"] = "2026-09-25T12:00:00+08:00"
    newest = snapshot("300957.SZ", snapshot_id="peer_new", price=20)
    newest["fetched_at"] = "2026-09-26T12:00:00+08:00"
    other = snapshot("300896.SZ", price=40)
    result = evaluate(target, [old, middle, newest, other, deepcopy(target)])

    assert result["peer_count"] == 2
    assert result["multiples"]["PE"]["included_symbols"] == ["300957.SZ", "300896.SZ"]
    assert result["multiples"]["PE"]["median"] == pytest.approx(300 / 22)
    assert result["valuation_ranges"]["PE"]["median"] == pytest.approx(300 / 22)
    row = result["distributions"]["PE"][0]
    assert row["snapshot_id"] == "peer_new"
    assert row["value"] == pytest.approx(200 / 22)
    assert row["financial_periods"] == {"ttm": "2026H1", "shares": "2026H1", "equity": "2026H1"}
    assert {item["snapshot_id"] for item in result["snapshot_exclusions"]} == {"peer_old", "peer_middle"}
    assert all(item["selected_snapshot_id"] == "peer_new" for item in result["snapshot_exclusions"])
    assert "peer_count_below_4_low_confidence" in result["validation"]["warnings"]


@pytest.mark.parametrize("reverse", [False, True])
def test_closest_cutoff_wins_independent_of_file_order(evaluate, reverse):
    exact = snapshot("300957.SZ", snapshot_id="exact", price=20)
    nearby = snapshot("300957.SZ", snapshot_id="nearby", price=90, cutoff="2026-09-22T23:00:00+08:00")
    nearby["fetched_at"] = "2026-09-26T12:00:00+08:00"
    peers = [exact, nearby]
    result = evaluate(snapshot("603605.SH"), list(reversed(peers)) if reverse else peers)
    assert result["peer_count"] == 1
    assert result["distributions"]["PE"][0]["snapshot_id"] == "exact"


@pytest.mark.parametrize("field", ["ttm", "shares", "equity"])
def test_matching_basis_label_cannot_hide_different_financial_periods(evaluate, field):
    target = snapshot("603605.SH")
    peer = snapshot("300957.SZ")
    if field == "ttm":
        peer["data"]["statements"] = [s for s in peer["data"]["statements"]
                                      if not (s["statement_type"] == "income" and s["fiscal_period"] == "2026H1")]
    else:
        balance = peer["data"]["statements"][-1]
        older = deepcopy(balance)
        older.update(fiscal_period="2025FY", period_basis="2025FY_END")
        balance["values"]["total_shares" if field == "shares" else "equity"] = None
        peer["data"]["statements"].append(older)
    result = evaluate(target, [peer])
    assert result["period_basis"] == "TTM_SNAPSHOT_AS_OF"
    assert result["available"] is False
    assert result["peer_count"] == 0
    assert result["decisions"][0]["reason"] == "financial_period_mismatch"
    assert result["multiples"]["PE"]["median"] is None


def test_missing_ttm_period_is_not_a_comparable_peer(evaluate):
    peer = snapshot("300957.SZ")
    peer["data"]["statements"] = [s for s in peer["data"]["statements"] if s["statement_type"] == "balance"]
    result = evaluate(snapshot("603605.SH"), [peer])
    assert result["peer_count"] == 0
    assert result["decisions"][0]["reason"] == "missing_financial_periods"


def test_generic_workflow_cannot_double_weight_a_symbol():
    target = service._peer_candidate(snapshot("603605.SH"), CUTOFF)
    peer = service._peer_candidate(snapshot("300957.SZ"), CUTOFF)
    duplicate = replace(peer, valuation=replace(peer.valuation, raw_price=100))
    result = value_with_peers(target, [peer, duplicate])
    assert result.multiples["PE"]["included_symbols"] == ["300957.SZ"]
    assert result.multiples["PE"]["median"] == pytest.approx(100 / 22)
    assert result.decisions[-1].reason == "duplicate_symbol"
