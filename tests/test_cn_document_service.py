"""Offline tests for the document read service and its API surface.

These cover CARD-01's headline capability end to end: 报告数字 → document_id →
原始 PDF 页（含 bbox）。Everything runs against the version-controlled synthetic
filings, with a throwaway registry — no local ``data/`` artifact, no provider,
no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
from src.cn import document_service as service
from src.cn.documents.registry import DocumentRegistry


FIXTURES = Path(__file__).parent / "fixtures" / "documents"

ANNUAL_CONTINUED = "annual_01_profit_continued.pdf"
ANNUAL_UNIT_WAN = "annual_02_balance_unit_wan.pdf"
ANNUAL_SCAN_MIXED = "annual_03_cashflow_with_scan.pdf"
DEGRADED_SCAN = "scan_sample.pdf"


@pytest.fixture()
def registry(tmp_path):
    """A throwaway registry that may read the bundled fixtures."""
    instance = DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])
    previous = service.get_registry()
    service._REGISTRY = instance
    try:
        yield instance
    finally:
        service._REGISTRY = previous
        instance.close()


def _ingest(registry: DocumentRegistry, name: str, declared: str = "annual"):
    return registry.ingest(FIXTURES / name, declared_type=declared, source="data_dir")


def _document_id(registry: DocumentRegistry, name: str) -> str:
    return _ingest(registry, name).document.document_id


def _facts(registry: DocumentRegistry, name: str) -> list[dict]:
    return service.document_facts(_document_id(registry, name))["facts"]


# ---------------------------------------------------------------------------
# Listing and detail
# ---------------------------------------------------------------------------

def test_list_documents_reports_fact_counts_and_never_exposes_server_paths(registry):
    _ingest(registry, ANNUAL_CONTINUED)
    _ingest(registry, ANNUAL_UNIT_WAN)

    listing = service.list_documents()

    assert listing["count"] == 2
    blob = json.dumps(listing, ensure_ascii=False)
    assert "stored_path" not in blob
    assert str(FIXTURES) not in blob and FIXTURES.as_posix() not in blob
    for item in listing["items"]:
        assert item["document_id"].startswith("doc_")
        assert item["fact_count"] > 0
        assert item["parser"]


def test_document_detail_exposes_parse_events_and_integrity(registry):
    parsed = _ingest(registry, ANNUAL_SCAN_MIXED)

    detail = service.document_detail(parsed.document.document_id)

    assert detail["status"] == "partial"          # one page failed to parse
    assert detail["parse_events"], "逐页解析摘要必须可见"
    assert {event["status"] for event in detail["parse_events"]} & {"ok", "partial", "failed"}
    assert detail["integrity"] == {"file_exists": True, "tampered": False, "facts_invalidated": 0}
    assert detail["preview"]["page_image"] is True
    assert str(FIXTURES) not in json.dumps(detail, ensure_ascii=False)


def test_degraded_scan_is_reported_as_not_a_fact_source(registry):
    parsed = _ingest(registry, DEGRADED_SCAN, declared="other")

    detail = service.document_detail(parsed.document.document_id)

    assert detail["status"] == "degraded"
    assert detail["is_fact_source"] is False
    assert detail["fact_count"] == 0
    assert any("不可作为事实来源" in note for note in detail["notes"])


def test_unknown_document_raises_key_error(registry):
    with pytest.raises(KeyError):
        service.document_detail("doc_does_not_exist")


# ---------------------------------------------------------------------------
# Fact table: labels are reconstructed, never guessed
# ---------------------------------------------------------------------------

def test_fact_rows_rebuild_row_labels_and_column_roles(registry):
    rows = _facts(registry, ANNUAL_UNIT_WAN)

    assert rows, "数值事实不能为空"
    # A value cell's own text is the number — the row label must come from the
    # table context, not from the cell we are displaying.
    assert {(row["row_label"], row["column_label"]) for row in rows} == {
        ("货币资金", "期末余额"), ("货币资金", "期初余额"),
        ("应收账款", "期末余额"), ("应收账款", "期初余额"),
    }
    assert {row["column_role"] for row in rows} == {"current", "prior"}
    assert all(row["row_label_source"] == "table_context" for row in rows)
    assert all(row["unit"] == "万元" for row in rows)


def test_fact_rows_map_canonical_metrics_and_leave_unknown_labels_untouched(registry):
    rows = _facts(registry, ANNUAL_UNIT_WAN)

    receivables = [row for row in rows if row["row_label"] == "应收账款"]
    assert all(row["metric"] == "receivables" and row["metric_known"] for row in receivables)

    cash = [row for row in rows if row["row_label"] == "货币资金"]
    assert all(row["metric"] is None and row["metric_known"] is False for row in cash)
    assert cash[0]["metric_note"], "未映射的标签必须给出原因，不能静默"


def test_numeric_only_filters_out_label_and_header_cells(registry):
    document_id = _document_id(registry, ANNUAL_UNIT_WAN)

    numeric = service.document_facts(document_id)
    everything = service.document_facts(document_id, numeric_only=False, include_header_cells=True)

    assert numeric["fact_count"] == 4
    assert everything["fact_count"] > numeric["fact_count"]
    assert any(row["is_header_cell"] for row in everything["facts"])
    assert all(row["raw_value"] is not None for row in numeric["facts"])


# ---------------------------------------------------------------------------
# Reverse jump
# ---------------------------------------------------------------------------

def test_jump_target_returns_page_and_bbox_without_any_server_path(registry):
    document_id = _document_id(registry, ANNUAL_UNIT_WAN)
    rows = service.document_facts(document_id)["facts"]
    row = next(item for item in rows if item["row_label"] == "应收账款" and item["column_role"] == "current")

    target = service.jump_target(document_id, row["fact_id"])

    assert target["page"] == 1
    assert target["bbox"] and target["bbox"] == row["bbox"]
    assert target["raw_value"] == "8300"
    assert target["metric"] == "receivables"
    assert target["preview"]["page_image"] is True
    assert "stored_path" not in json.dumps(target, ensure_ascii=False)


def test_jump_follows_a_continued_table_across_pages(registry):
    document_id = _document_id(registry, ANNUAL_CONTINUED)
    rows = service.document_facts(document_id)["facts"]

    assert {row["page"] for row in rows} == {1, 2}, "续表必须保留各自页码"
    net_profit = next(row for row in rows if row["row_label"] == "净利润" and row["column_role"] == "current")
    target = service.jump_target(document_id, net_profit["fact_id"])

    assert target["page"] == 2
    assert target["metric"] == "net_profit"
    assert target["row_label"] == "净利润"


def test_jump_target_rejects_an_unknown_fact(registry):
    document_id = _document_id(registry, ANNUAL_CONTINUED)
    with pytest.raises(KeyError):
        service.jump_target(document_id, "fact_not_registered")


# ---------------------------------------------------------------------------
# Integrity: a tampered file must not stay jumpable
# ---------------------------------------------------------------------------

def test_tampered_file_invalidates_facts_and_stops_the_jump(registry, tmp_path):
    stored = tmp_path / "documents" / ANNUAL_UNIT_WAN
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes((FIXTURES / ANNUAL_UNIT_WAN).read_bytes())
    parsed = registry.ingest(stored, declared_type="annual", source="data_dir")
    document_id = parsed.document.document_id

    jumpable = service.document_facts(document_id)["facts"]
    assert jumpable and all(row["jumpable"] for row in jumpable)
    target_id = jumpable[0]["fact_id"]
    assert service.jump_target(document_id, target_id)["page"] == 1

    stored.write_bytes(stored.read_bytes() + b"\n% tampered\n")

    detail = service.document_detail(document_id)
    assert detail["integrity"]["tampered"] is True
    assert detail["integrity"]["facts_invalidated"] > 0
    assert detail["jumpable_fact_count"] == 0

    with pytest.raises(ValueError, match="失效"):
        service.jump_target(document_id, target_id)


def test_a_document_outside_the_allowlist_is_never_previewed(registry, monkeypatch):
    document_id = _document_id(registry, ANNUAL_UNIT_WAN)

    class _Restricted:
        """Same documents, but the workspace no longer permits their location."""

        allow_roots = [Path("C:/definitely/not/the/workspace")]

        def __init__(self, inner):
            self._inner = inner

        def get(self, identifier):
            return self._inner.get(identifier)

        def get_facts(self, identifier):
            return self._inner.get_facts(identifier)

        def get_events(self, identifier):
            return self._inner.get_events(identifier)

        def verify_integrity(self, identifier):
            return self._inner.verify_integrity(identifier)

        def list(self, *args, **kwargs):
            return self._inner.list(*args, **kwargs)

    monkeypatch.setattr(service, "get_registry", lambda: _Restricted(registry))

    detail = service.document_detail(document_id)
    assert detail["preview"]["embedded_pdf"] is False
    assert "白名单" in detail["preview"]["reason"]

    with pytest.raises(ValueError, match="白名单"):
        service.document_source(document_id)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

def test_page_image_renders_a_png_with_the_bbox_outlined(registry):
    rows = _facts(registry, ANNUAL_UNIT_WAN)
    row = next(item for item in rows if item["metric"] == "receivables")
    document_id = _document_id(registry, ANNUAL_UNIT_WAN)

    plain = service.page_image(document_id, 1)
    highlighted = service.page_image(document_id, 1, highlights=[row["bbox"]])

    assert plain[:8] == b"\x89PNG\r\n\x1a\n"
    assert highlighted[:8] == b"\x89PNG\r\n\x1a\n"
    assert highlighted != plain, "高亮必须真的改变像素"


def test_page_image_rejects_an_out_of_range_page(registry):
    document_id = _document_id(registry, ANNUAL_UNIT_WAN)
    with pytest.raises(ValueError, match="超出范围"):
        service.page_image(document_id, 99)


def test_document_source_refuses_a_non_pdf(registry, tmp_path):
    stored = tmp_path / "documents" / "notes.txt"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text("营业收入 100\n", encoding="utf-8")
    document_id = registry.ingest(stored, declared_type="other", source="data_dir").document.document_id

    with pytest.raises(ValueError, match="不是 PDF"):
        service.document_source(document_id)


# ---------------------------------------------------------------------------
# CARD-02 + CARD-03 read model: located facts → analysable metrics
# ---------------------------------------------------------------------------

def test_document_analysis_attaches_a_resolvable_formula_to_every_metric(registry):
    """§14 "每行可展开公式与输入证据" must be *data*, not prose.

    Every metric that carries a calculation_id must come back with the formula,
    the operands (each with its evidence ID) and the registry's own recomputed
    value — so the UI can show the arithmetic without owning any of it.
    """
    payload = service.document_analysis(_document_id(registry, ANNUAL_CONTINUED))

    assert payload["validation_errors"] == []
    assert payload["metrics"], "annual_01 is expected to yield level margins"
    for row in payload["metrics"]:
        assert row["calculation_id"], row["metric"]
        calculation = row["calculation"]
        assert calculation is not None, row["metric"]
        assert calculation["formula"]
        assert calculation["operands"], row["metric"]
        for operand in calculation["operands"]:
            assert operand["id"]
            assert operand["kind"] in {"evidence", "calculation", "constant"}
        assert calculation["recalculated"] is not None


def test_document_analysis_never_exposes_a_server_path(registry):
    payload = service.document_analysis(_document_id(registry, ANNUAL_CONTINUED))
    assert "stored_path" not in payload
    assert "C:\\" not in json.dumps(payload, ensure_ascii=False)


def test_document_analysis_level_rows_carry_no_comparison_semantics(registry):
    """A margin is a level: nothing to compare, so nothing may claim ``normal``."""
    payload = service.document_analysis(_document_id(registry, ANNUAL_CONTINUED))
    levels = [row for row in payload["metrics"] if row["comparison"] == "level"]
    assert levels
    for row in levels:
        assert row["comparison_semantic"] is None, row["metric"]
        assert row["delta_pct"] is None
        assert row["delta_abs"] is None


def test_document_analysis_separates_missing_data_from_not_applicable(registry):
    """An annual report has no quarterly series — that is *not applicable*.

    ``data_complete`` must key off ``blocking`` only, otherwise every annual
    filing would be flagged incomplete for a metric it structurally cannot have.
    """
    payload = service.document_analysis(_document_id(registry, ANNUAL_CONTINUED))
    missing = {row["metric"]: row for row in payload["missing"]}

    assert missing["revenue_qoq_single_quarter"]["blocking"] is False
    assert "不适用" in missing["revenue_qoq_single_quarter"]["reason"]

    # A disclosed-in-principle input that this document omits *is* blocking.
    assert missing["capex"]["blocking"] is True
    assert payload["data_complete"] is False
    assert any(row["blocking"] for row in payload["missing"])


def test_document_analysis_does_not_fabricate_a_prior_year(registry):
    """annual_01 has one period: the YoY row is absent, with the gap explained."""
    payload = service.document_analysis(_document_id(registry, ANNUAL_CONTINUED))

    assert all(row["metric"] != "revenue_yoy" for row in payload["metrics"])
    gap = next(row for row in payload["missing"] if row["metric"] == "revenue_yoy")
    assert "缺少上一可比期间" in gap["reason"]
    assert gap["blocking"] is True


def test_document_analysis_unknown_document_is_a_typed_error(registry):
    with pytest.raises(service.DocumentNotFound):
        service.document_analysis("doc_does_not_exist")


# ---------------------------------------------------------------------------
# HTTP API: same envelope, no server paths
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(registry):
    _ingest(registry, ANNUAL_CONTINUED)
    _ingest(registry, ANNUAL_UNIT_WAN)
    return TestClient(api.app)


def _assert_ok(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == api.API_SCHEMA_VERSION
    assert body["status"] == "ok"
    assert body["error"] is None
    assert set(body["meta"]) == set(api._meta())
    return body


def test_api_document_routes_share_the_versioned_envelope(client):
    listing = _assert_ok(client.get("/api/documents"))
    assert listing["data"]["count"] == 2
    assert listing["meta"]["provider"] == "document_registry"

    document_id = next(
        item["document_id"] for item in listing["data"]["items"] if item["filename"] == ANNUAL_CONTINUED
    )
    detail = _assert_ok(client.get(f"/api/documents/{document_id}"))
    facts = _assert_ok(client.get(f"/api/documents/{document_id}/facts"))

    assert detail["data"]["page_count"] == 2
    assert facts["data"]["fact_count"] > 0
    for body in (listing, detail, facts):
        blob = json.dumps(body, ensure_ascii=False)
        assert "stored_path" not in blob
        assert str(FIXTURES) not in blob


def test_api_document_jump_and_page_image_are_locatable(client):
    listing = client.get("/api/documents").json()["data"]["items"]
    document_id = next(item["document_id"] for item in listing if item["filename"] == ANNUAL_UNIT_WAN)
    facts = _assert_ok(client.get(f"/api/documents/{document_id}/facts"))["data"]["facts"]
    target = next(row for row in facts if row["metric"] == "receivables")

    jump = _assert_ok(client.get(f"/api/documents/{document_id}/facts/{target['fact_id']}/jump"))
    assert jump["data"]["page"] == 1
    assert jump["data"]["bbox"] == target["bbox"]
    assert jump["meta"]["unit"] == "万元"
    assert "stored_path" not in json.dumps(jump, ensure_ascii=False)

    image = client.get(f"/api/documents/{document_id}/pages/1.png", params=[("fact_id", target["fact_id"])])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_api_document_analysis_uses_shared_deterministic_read_model(client):
    listing = client.get("/api/documents").json()["data"]["items"]
    document_id = next(item["document_id"] for item in listing if item["filename"] == ANNUAL_CONTINUED)

    response = _assert_ok(client.get(f"/api/documents/{document_id}/analysis"))
    payload = response["data"]
    assert payload["document_id"] == document_id
    assert payload["metrics"]
    assert payload["validation_errors"] == []
    assert response["meta"]["provider"] == "document_registry"
    assert response["meta"]["data_coverage"]["metric_count"] == payload["metric_count"]
    assert "stored_path" not in json.dumps(payload, ensure_ascii=False)


def test_api_document_analysis_returns_typed_not_found(client):
    response = client.get("/api/documents/doc_missing/analysis")
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_api_document_source_streams_bytes_instead_of_paths(client):
    listing = client.get("/api/documents").json()["data"]["items"]
    document_id = next(item["document_id"] for item in listing if item["filename"] == ANNUAL_UNIT_WAN)

    response = client.get(f"/api/documents/{document_id}/source")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert response.text.count("Users") == 0


def test_api_document_errors_are_typed(client):
    missing = client.get("/api/documents/doc_missing")
    unknown_fact = client.get("/api/documents/doc_missing/facts/fact_missing/jump")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert missing.json()["data"] is None
    assert unknown_fact.status_code == 404
    assert unknown_fact.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_api_document_page_out_of_range_is_a_typed_error(client):
    listing = client.get("/api/documents").json()["data"]["items"]
    document_id = next(item["document_id"] for item in listing if item["filename"] == ANNUAL_UNIT_WAN)

    response = client.get(f"/api/documents/{document_id}/pages/42.png")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_LOCATABLE"
