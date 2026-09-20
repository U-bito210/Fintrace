"""Offline regression tests for the CARD-01 extraction benchmark corpus.

These guard two things:

1. **Reproducibility** — the committed corpus under ``tests/fixtures/documents``
   must be byte-reproducible from ``scripts/gen_document_fixtures.py``.
2. **No regression in the measured baseline** — the P/R/F1 figures reported by
   ``output/benchmark/cn_agent_v2/document_extraction_baseline.*`` must stay at
   the level actually measured on the synthetic corpus.

The second point is deliberately *not* a frozen target from CARD-11: the numbers
asserted below are the honestly measured synthetic baseline (the parser is exact
on fixtures generated from the same spec), and they bound regressions only. They
say nothing about real filings — that measurement belongs to CARD-11's Real set.
"""

from __future__ import annotations

import json

import pytest

from src.cn.documents.extraction_benchmark import (
    default_fixtures_dir,
    evaluate_corpus,
    extracted_figures,
)
from src.cn.documents.guard import OfflineGuard
from src.cn.documents.synthetic_corpus import (
    CORPUS_SCHEMA_VERSION,
    corpus,
    load_gold,
    load_manifest,
    verify_corpus,
)

REQUIRED_GOLD_KEYS = {"document_id", "metric", "period", "expected_value", "unit", "scope", "page"}


@pytest.fixture(scope="module")
def fixtures_dir():
    path = default_fixtures_dir()
    if not (path / "manifest.json").exists():
        pytest.skip("合成语料未生成，请先运行 scripts/gen_document_fixtures.py")
    return path


@pytest.fixture(scope="module")
def scores(fixtures_dir, tmp_path_factory):
    registry_root = tmp_path_factory.mktemp("docbench") / "documents"
    with OfflineGuard():
        return evaluate_corpus(fixtures_dir, registry_root)


# ---------------------------------------------------------------------------
# Corpus integrity
# ---------------------------------------------------------------------------

def test_corpus_is_byte_reproducible(fixtures_dir):
    verdict = verify_corpus(fixtures_dir)
    assert verdict["ok"] is True, verdict
    assert verdict["checked"] == len(corpus())


def test_manifest_schema_and_disclaimer(fixtures_dir):
    manifest = load_manifest(fixtures_dir)
    assert manifest["schema_version"] == CORPUS_SCHEMA_VERSION
    assert manifest["synthetic"] is True
    # The corpus must never be presentable as real disclosures (AGENTS.md rule).
    assert "never be presented as real disclosures" in manifest["disclaimer"]
    for entry in manifest["documents"]:
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0


def test_corpus_covers_every_required_type(fixtures_dir):
    docs = corpus()
    by_type: dict = {}
    for doc in docs:
        by_type.setdefault(doc.document_type, []).append(doc)
    # CARD-01 §13: 年报/公告/研报草稿各 ≥3 份 + 扫描页样例.
    assert len(by_type["annual"]) >= 3
    assert len(by_type["announcement"]) >= 3
    assert len(by_type["report_draft"]) >= 3
    assert any(d.expect_scan_page is not None for d in docs), "缺少扫描页样例"
    assert any(d.expect_injection for d in docs), "缺少注入样本"
    assert any(d.gold for d in docs), "缺少带数值真值的样例"


def test_gold_records_follow_card11_format(fixtures_dir):
    total = 0
    for doc in corpus():
        payload = load_gold(fixtures_dir, doc.doc_id)
        assert payload["document_id"] == doc.doc_id
        for rec in payload["records"]:
            assert REQUIRED_GOLD_KEYS <= set(rec), f"{doc.doc_id} 真值字段缺失: {rec}"
            assert isinstance(rec["expected_value"], (int, float))
            assert isinstance(rec["page"], int) and rec["page"] >= 1
            total += 1
    assert total >= 20, "合成集数值真值过少，基准不具区分度"


# ---------------------------------------------------------------------------
# Baseline regression guards (measured on the synthetic corpus)
# ---------------------------------------------------------------------------

def test_baseline_extraction_is_exact_on_synthetic(fixtures_dir, scores):
    micro = scores.micro()
    # Measured baseline: the parser is exact on fixtures generated from the same
    # spec, so any drift means a parser regression.
    assert micro["recall"] == 1.0, micro
    assert micro["precision"] == 1.0, micro
    assert micro["false_positive"] == 0, micro
    assert micro["true_positive"] >= 20


def test_baseline_structural_metrics(fixtures_dir, scores):
    payload = scores.to_dict()
    assert payload["ingest_success_rate"] == 1.0, payload
    assert payload["location_coverage"] == 1.0, payload
    assert payload.get("bbox_coverage") == 1.0, payload
    assert payload.get("label_recall") == 1.0, payload
    assert payload.get("page_attribution_accuracy") == 1.0, payload
    assert payload.get("document_status_accuracy") == 1.0, payload
    assert payload.get("scan_page_handling_accuracy") == 1.0, payload
    assert payload.get("injection_detection_accuracy") == 1.0, payload
    assert payload.get("degraded_labelling_accuracy") == 1.0, payload


def test_every_document_ingests_and_locates(fixtures_dir, scores):
    assert len(scores.documents) == len(corpus())
    for doc in scores.documents:
        assert not doc.errors, f"{doc.document_id}: {doc.errors}"
        assert doc.status in ("ok", "partial", "degraded")


def test_scan_sample_matches_its_declared_expectation(fixtures_dir, scores):
    by_id = {d.document_id: d for d in scores.documents}
    assert by_id["SYN-SCAN-01"].status == "degraded"
    assert by_id["SYN-SCAN-01"].fact_count == 0
    assert by_id["SYN-ANNUAL-03"].status == "partial"


def test_report_written_json_is_machine_readable(fixtures_dir, scores, tmp_path):
    from src.cn.documents.extraction_benchmark import write_report

    paths = write_report(scores, tmp_path)
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["suite"] == "cn_agent_v2"
    assert payload["set"] == "synthetic"
    assert payload["synthetic"] is True
    assert "Not a real-model benchmark" in payload["disclaimer"]
    assert payload["metrics"]["extraction"]["micro"]["f1"] == 1.0


def test_extracted_figures_requires_label_and_value():
    """A figure needs both a row label and a numeric cell in that same row."""
    from src.cn.documents.models import ExtractedFact, FACT_KIND_TABLE_CELL, FACT_KIND_TEXT

    def fact(row, col, text, value=None, kind=FACT_KIND_TABLE_CELL):
        return ExtractedFact(
            document_id="d", raw_text=text, raw_value=value, kind=kind,
            page=1, table="t", row=row, column=col,
        )

    facts = [
        fact(0, 0, "营业收入"),                 # label
        fact(0, 1, "123", "123"),               # + numeric -> one figure
        fact(1, 1, "999", "999"),               # numeric with NO label in its row -> dropped
        fact(2, 0, "净利润"),                    # label with NO numeric cell -> dropped
        fact(3, 0, "说明", "456", FACT_KIND_TEXT),  # non-table fact -> never a figure
    ]
    assert [(f.label, f.value, f.page) for f in extracted_figures(facts)] == [("营业收入", 123.0, 1)]
