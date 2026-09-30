"""Offline contract tests for the versioned workbench HTTP API."""

import json

from fastapi.testclient import TestClient

import api
from src.cn import snapshot_refresh as refresh
from src.cn.daily_review import collector
from tests.test_cn_snapshot_refresh import _base_payload


client = TestClient(api.app)
SNAPSHOT_ID = "600519.SH_20260810_tushare_v1"
REFRESH_SYMBOL = "600519.SH"
REQUIRED_META = {
    "snapshot_id", "research_as_of", "provider", "currency", "unit", "period_basis",
    "evidence_ids", "validation_status", "data_coverage", "freshness",
}


def _assert_ok(response):
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == api.API_SCHEMA_VERSION
    assert body["status"] == "ok"
    assert body["error"] is None
    assert set(body["meta"]) == REQUIRED_META
    return body


def test_research_endpoints_share_versioned_envelope_and_provenance():
    endpoints = ["summary", "market", "financials", "evidence", "valuation", "validation", "trace", "report", "artifacts"]

    for endpoint in endpoints:
        body = _assert_ok(client.get(f"/api/research/{SNAPSHOT_ID}/{endpoint}"))
        assert body["meta"]["snapshot_id"] == SNAPSHOT_ID
        assert body["meta"]["research_as_of"]
        assert body["meta"]["provider"] == "tushare"
        assert body["meta"]["currency"] == "CNY"
        assert body["meta"]["validation_status"] in {"pass", "warning", "blocked"}


def test_financial_period_query_exposes_ttm_basis_and_calculation_evidence():
    body = _assert_ok(client.get(f"/api/research/{SNAPSHOT_ID}/financials?period_basis=TTM"))

    assert body["data"]["period_basis"] == "TTM"
    assert body["data"]["points"]
    assert any(
        point["evidence_ids"]["net_profit"].startswith("calc_")
        for point in body["data"]["points"] if point["evidence_ids"]["net_profit"]
    )


def test_trace_marks_legacy_missing_trace_as_unavailable_instead_of_fabricating_steps():
    body = _assert_ok(client.get(f"/api/research/{SNAPSHOT_ID}/trace"))

    if not body["data"]["available"]:
        assert body["data"]["events"] == []
        assert body["data"]["unavailable_reason"] == "trace_not_recorded_for_legacy_task"


def test_artifact_contract_does_not_expose_server_file_paths():
    body = _assert_ok(client.get(f"/api/research/{SNAPSHOT_ID}/artifacts"))

    for artifact in body["data"]["artifacts"]:
        assert set(artifact) == {"kind", "filename", "media_type", "size_bytes"}
        assert "\\" not in artifact["filename"]


def test_unknown_snapshot_and_invalid_query_use_safe_typed_errors():
    missing = client.get("/api/research/not-a-snapshot/summary")
    invalid = client.get(f"/api/research/{SNAPSHOT_ID}/financials?period_basis=FUTURE")

    assert missing.status_code == 404
    assert missing.json()["error"] == {
        "code": "SNAPSHOT_NOT_FOUND", "message": "Research snapshot was not found.", "retryable": False,
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.json()["data"] is None


def test_task_detail_and_progress_endpoints_use_persisted_service_contract(monkeypatch):
    monkeypatch.setattr(api.service, "get_task", lambda task_id: {"id": task_id, "status": "running"})
    monkeypatch.setattr(api.service, "get_task_progress", lambda task_id: {
        "task_id": task_id, "status": "running", "progress_percent": 40,
    })

    detail = _assert_ok(client.get("/api/tasks/task-1"))
    progress = _assert_ok(client.get("/api/tasks/task-1/progress"))
    assert detail["data"]["status"] == "running"
    assert progress["data"]["progress_percent"] == 40


def test_task_rerun_endpoint_uses_accepted_envelope(monkeypatch):
    monkeypatch.setattr(api.service, "rerun_task", lambda task_id: {
        "id": "retry-1", "retry_of": task_id, "status": "queued", "symbol": "600519.SH",
        "snapshot_id": SNAPSHOT_ID,
    })

    response = client.post("/api/tasks/task-1/rerun")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ok"
    assert body["data"]["retry_of"] == "task-1"
    assert body["meta"]["snapshot_id"] == SNAPSHOT_ID


def test_task_api_does_not_expose_server_artifact_paths(monkeypatch):
    monkeypatch.setattr(api.service, "get_task", lambda task_id: {
        "id": task_id, "status": "succeeded", "snapshot_id": SNAPSHOT_ID,
        "report_path": "C:\\private\\report.md", "metadata_path": "C:\\private\\metadata.json",
    })

    body = _assert_ok(client.get("/api/tasks/task-1"))

    assert "report_path" not in body["data"]
    assert "metadata_path" not in body["data"]


def test_task_cancel_endpoint_uses_accepted_envelope(monkeypatch):
    monkeypatch.setattr(api.service, "cancel_task", lambda task_id: {
        "id": task_id, "status": "cancelled", "message": "cancelled",
    })

    response = client.post("/api/tasks/task-1/cancel")

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "cancelled"


def test_task_diagnostics_endpoint_returns_safe_summary(monkeypatch):
    monkeypatch.setattr(api.service, "task_diagnostics", lambda task_id: {
        "task_id": task_id, "status": "failed", "error_type": "RuntimeError",
        "error_summary": "safe diagnostic", "trace": [],
    })

    body = _assert_ok(client.get("/api/tasks/task-1/diagnostics"))

    assert body["data"]["error_summary"] == "safe diagnostic"
    assert "report_path" not in body["data"]


def test_evaluation_endpoints_return_measured_artifacts(monkeypatch):
    monkeypatch.setattr(api.service, "list_evaluations", lambda: [{"id": "benchmark/run/results.json"}])
    monkeypatch.setattr(api.service, "evaluation_detail", lambda evaluation_id: {
        "id": evaluation_id, "kind": "benchmark", "summary": {"cases": 2}, "results": [],
    })

    listing = _assert_ok(client.get("/api/evaluations"))
    detail = _assert_ok(client.get("/api/evaluations/benchmark/run/results.json"))

    assert listing["data"]["items"][0]["id"] == "benchmark/run/results.json"
    assert detail["data"]["summary"]["cases"] == 2


def test_openapi_publishes_stable_envelope_meta_and_error_schemas():
    document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]

    assert {"ApiEnvelope", "ApiMeta", "ApiError"}.issubset(schemas)
    assert set(schemas["ApiEnvelope"]["properties"]) == {"schema_version", "status", "data", "meta", "error"}
    assert REQUIRED_META == set(schemas["ApiMeta"]["properties"])


def test_health_is_live_without_provider_or_credentials():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "schema_version": api.API_SCHEMA_VERSION}


# ---------------------------------------------------------------------------
# Online snapshot refresh
# ---------------------------------------------------------------------------
def _canned_outcome(**overrides) -> dict:
    outcome = {
        "refresh_status": "succeeded", "reason_code": None, "message": "ok",
        "retryable": False, "symbol": REFRESH_SYMBOL, "bars_only": True,
        "provider": "tushare", "snapshot_id": "600519.SH_20260928_tushare_v2",
        "research_as_of": "2026-09-28T16:30:00+08:00",
        "generated_at": "2026-09-28T16:30:00+08:00", "version": 2,
        "latest_trade_date": "2026-09-28", "new_bar_count": 1, "freshness": None,
        "current_snapshot": {
            "snapshot_id": REFRESH_SYMBOL + "_20260920_tushare_v1",
            "research_as_of": "2026-09-20T23:00:00+08:00",
            "provider": "tushare", "latest_trade_date": "2026-09-19",
        },
        "call_budget": 1, "calls_used": 1, "error_type": None,
    }
    outcome.update(overrides)
    return outcome


def test_refresh_endpoint_creates_a_snapshot_and_points_meta_at_it(tmp_path, monkeypatch):
    """End-to-end offline: only the provider boundary is faked."""
    base = _base_payload()
    (tmp_path / f"{base['snapshot_id']}.json").write_text(
        json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(api.service, "SNAPSHOT_DIR", tmp_path)
    api.service.load_snapshot.cache_clear()
    monkeypatch.setattr(collector, "load_tushare_token", lambda: "test-token-not-a-secret")
    # The refreshed snapshot id embeds the refresh cutoff, which defaults to
    # "now".  Pin the clock after the 09-28 A-share close so the assertion
    # below stays offline and independent of the wall clock.
    monkeypatch.setattr(
        refresh, "_coerce_now",
        lambda now=None: refresh.datetime(2026, 9, 28, 16, 5, tzinfo=refresh.CST),
    )
    monkeypatch.setattr(refresh, "fetch_latest_bars", lambda provider, symbol, **window: [{
        "trade_date": "2026-09-28", "open": 1700.0, "high": 1710.0, "low": 1690.0,
        "close": 1705.0, "volume": 2_000_000.0, "amount": 3.4e9,
        "adjustment": "RAW", "trading_status": None, "adj_factor": None,
    }])

    response = client.post("/api/research/snapshots/refresh", json={"symbol": REFRESH_SYMBOL})
    api.service.load_snapshot.cache_clear()

    assert response.status_code == 200
    body = _assert_ok(response)
    assert body["data"]["refresh_status"] == "succeeded"
    assert body["data"]["snapshot_id"] == "600519.SH_20260928_tushare_v2"
    assert body["meta"]["snapshot_id"] == body["data"]["snapshot_id"]
    assert body["meta"]["provider"] == "tushare"
    assert body["meta"]["freshness"]["latest_trade_date"] == "2026-09-28"
    # The base snapshot is still on disk and still readable.
    assert (tmp_path / f"{base['snapshot_id']}.json").exists()


def test_refresh_no_new_data_is_an_ok_envelope_not_a_fake_success(monkeypatch):
    monkeypatch.setattr(api.service, "refresh_snapshot", lambda symbol, **kw: _canned_outcome(
        refresh_status="no_new_data", reason_code="already_latest", snapshot_id=None,
        message="已是本地最新。",
    ))

    response = client.post("/api/research/snapshots/refresh", json={"symbol": REFRESH_SYMBOL})

    assert response.status_code == 200
    body = _assert_ok(response)
    assert body["data"]["refresh_status"] == "no_new_data"
    # meta falls back to the snapshot still in use, so the UI can name its cutoff.
    assert body["meta"]["snapshot_id"] == _canned_outcome()["current_snapshot"]["snapshot_id"]
    assert body["meta"]["research_as_of"] == "2026-09-20T23:00:00+08:00"


def test_refresh_failures_are_typed_errors_with_the_snapshot_still_in_use(monkeypatch):
    cases = [
        ("credentials_missing", 503, "CREDENTIALS_MISSING", False),
        ("rate_limited", 429, "RATE_LIMITED", True),
        ("network_error", 502, "NETWORK_ERROR", True),
        ("provider_no_data", 502, "PROVIDER_NO_DATA", False),
        ("base_snapshot_missing", 409, "BASE_SNAPSHOT_MISSING", False),
    ]
    for reason, status_code, code, retryable in cases:
        monkeypatch.setattr(api.service, "refresh_snapshot", lambda symbol, _r=reason, **kw: _canned_outcome(
            refresh_status="failed" if reason != "provider_no_data" else "no_data",
            reason_code=_r, message=f"failed: {_r}", retryable=retryable, snapshot_id=None,
        ))

        response = client.post("/api/research/snapshots/refresh", json={"symbol": REFRESH_SYMBOL})

        assert response.status_code == status_code, reason
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == code
        assert body["error"]["retryable"] is retryable
        # Structured failure: the reason AND the snapshot still in use travel together.
        assert body["data"]["reason_code"] == reason
        assert body["meta"]["snapshot_id"] == _canned_outcome()["current_snapshot"]["snapshot_id"]
        assert body["meta"]["research_as_of"] == "2026-09-20T23:00:00+08:00"


def test_refresh_rejects_a_malformed_request_with_a_typed_error(monkeypatch):
    def _raise(symbol, **kwargs):
        raise ValueError("research_as_of must be an ISO-8601 date or datetime.")

    monkeypatch.setattr(api.service, "refresh_snapshot", _raise)
    response = client.post("/api/research/snapshots/refresh",
                           json={"symbol": REFRESH_SYMBOL, "research_as_of": "garbage"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REFRESH_REQUEST"


def test_refresh_never_leaks_the_configured_token(tmp_path, monkeypatch):
    secret = "sentinel-token-value-must-not-leak"
    base = _base_payload()
    (tmp_path / f"{base['snapshot_id']}.json").write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(api.service, "SNAPSHOT_DIR", tmp_path)
    api.service.load_snapshot.cache_clear()
    monkeypatch.setattr(collector, "load_tushare_token", lambda: secret)

    def _boom(provider, symbol, **window):
        raise RuntimeError(f"upstream rejected request: {secret}")

    monkeypatch.setattr(refresh, "fetch_latest_bars", _boom)

    response = client.post("/api/research/snapshots/refresh", json={"symbol": REFRESH_SYMBOL})

    assert response.status_code == 502
    assert secret not in response.text
    assert "sentinel" not in response.text
    assert response.json()["error"]["message"] == "数据源暂时不可用；可稍后重试。"
