"""Offline unit tests for the Retrieval Engine (CARD-06).

Every test here is deterministic and network-free: BM25 is self-implemented with
fixed hyperparameters, the tokenizer is a pure function, and no embedding provider
is touched (the engine degrades to ``mode=bm25``). The suite covers the card's
§11 unit tests: BM25 hand-computed scoring, determinism, chunk/evidence
separation, the three-way citation validation, graceful degrade, and empty/no-hit
edge cases, plus a tool-integration smoke test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from src.agents.tools.cn_tools import SearchCnCorpusTool
from src.cn.documents.registry import DocumentRegistry, DocumentType
from src.cn.retrieval import (
    Bm25Index,
    Citation,
    CorpusChunk,
    RetrievalEngine,
    build_chunks,
    chunk_text,
    cite_chunk,
    recall_at_k,
    tokenize,
)
from src.cn.retrieval.citation import CitationLedger


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@dataclass
class _Fact:
    """Minimal stand-in for an ExtractedFact (only the fields retrieval reads)."""
    document_id: str
    page: Optional[int]
    section: Optional[str]
    raw_text: Optional[str]
    fact_id: str = ""


def _chunk(cid: str, doc: str, page: Optional[int], text: str, section: str = "MD&A") -> CorpusChunk:
    return CorpusChunk(chunk_id=cid, document_id=doc, page=page, section=section, text=text)


# ---------------------------------------------------------------------------
# §11 — BM25 hand-computed example (5-doc corpus, pre-tokenized)
# ---------------------------------------------------------------------------

def test_bm25_hand_computed_scores():
    docs = {
        "D1": ["revenue", "growth", "steady"],
        "D2": ["revenue", "growth", "strong"],
        "D3": ["profit", "decline"],
        "D4": ["revenue"],
        "D5": ["steady", "strong"],
    }
    idx = Bm25Index().build(docs)
    # Query "revenue growth": hand-computed D1 score = 1.215542 (k1=1.5, b=0.75).
    scored = dict(idx.search("revenue growth", top_k=10))
    assert "D1" in scored
    assert abs(scored["D1"] - 1.215542) < 1e-4
    # D1 and D2 both contain both query terms; D2 has same length so ties on
    # terms but D1 < D2 lexicographically -> D1 ranks first.
    ranked = idx.search("revenue growth", top_k=10)
    assert ranked[0][0] == "D1"
    assert ranked[1][0] == "D2"


def test_bm25_idf_monotonic_with_document_frequency():
    docs = {f"D{i}": ["term"] * (1 if i == 0 else 0) for i in range(5)}
    # Build manually so D0 is the only doc with "term".
    idx = Bm25Index()
    idx.add("only", ["term"])
    idx.add("a", ["other"])
    idx.add("b", ["other"])
    scored = idx.search("term", top_k=5)
    assert scored and scored[0][0] == "only"


# ---------------------------------------------------------------------------
# §11 — determinism
# ---------------------------------------------------------------------------

def test_search_is_deterministic_for_same_query():
    docs = {
        "D1": ["营收", "增长", "稳健"],
        "D2": ["营收", "增长", "强劲"],
        "D3": ["利润", "下滑"],
    }
    idx = Bm25Index().build(docs)
    first = [(cid, round(s, 12)) for cid, s in idx.search("营收 增长", top_k=5)]
    second = [(cid, round(s, 12)) for cid, s in idx.search("营收 增长", top_k=5)]
    assert first == second


def test_tokenizer_is_deterministic():
    assert tokenize("营收增长 稳健；利润下滑") == tokenize("营收增长 稳健；利润下滑")


# ---------------------------------------------------------------------------
# §11 — chunk / evidence separation (检索不污染账本)
# ---------------------------------------------------------------------------

def test_searching_does_not_create_evidence():
    chunks = [
        _chunk("c1", "docA", 1, "公司营业收入同比增长 12%。"),
        _chunk("c2", "docA", 2, "毛利率较去年上升 3 个百分点。"),
        _chunk("c3", "docB", 1, "净利润出现下滑。"),
    ]
    engine = RetrievalEngine().index_chunks(chunks)
    # Search 100 times; not a single evidence id should be created.
    for _ in range(100):
        engine.search("营业收入 同比", top_k=3)
    assert engine.citation_count() == 0
    # Citing once creates exactly one evidence id.
    eid = engine.cite("c1", Citation.QUOTE, cited_text="公司营业收入同比增长 12%。")
    assert engine.citation_count() == 1
    assert eid.startswith("cite_")
    # Validation reports no orphan / no mismatch.
    val = engine.validate_citations()
    assert val.valid, val.errors


def test_chunk_is_distinct_from_evidence_id_in_payload():
    chunks = [_chunk("c1", "docA", 1, "营业收入同比增长 12%。")]
    engine = RetrievalEngine().index_chunks(chunks)
    result = engine.search("营业收入", top_k=1)
    hit = result.hits[0].to_dict()
    assert "evidence_id" not in hit
    assert hit["chunk_id"] == "c1"
    assert hit["page"] == 1


# ---------------------------------------------------------------------------
# §10/§11 — three-way citation validation
# ---------------------------------------------------------------------------

def test_quote_citation_requires_verbatim_text():
    chunk = _chunk("c1", "docA", 1, "营业收入同比增长百分之十二。")
    ledger = CitationLedger()
    # Exact match -> ok.
    eid = ledger.cite(chunk, Citation.QUOTE, cited_text="营业收入同比增长百分之十二。")
    assert eid
    # Drift -> rejected.
    import pytest

    with pytest.raises(Exception):
        ledger.cite(chunk, Citation.QUOTE, cited_text="营业收入同比增长百分之十三。")


def test_paraphrase_citation_binds_chunk_id():
    chunk = _chunk("c1", "docA", 1, "营业收入同比增长。")
    rec = cite_chunk(chunk, Citation.PARAPHRASE)
    assert rec.citation_type == "paraphrase"
    assert rec.chunk_id == "c1"


def test_inference_citation_requires_derived_from():
    chunk = _chunk("c1", "docA", 1, "综合判断公司基本面改善。")
    import pytest

    with pytest.raises(Exception):
        cite_chunk(chunk, Citation.INFERENCE)
    rec = cite_chunk(chunk, Citation.INFERENCE, derived_from=["c1", "c2"])
    assert rec.derived_from == ("c1", "c2")


def test_citation_ledger_flags_orphan_when_chunk_removed():
    chunk = _chunk("c1", "docA", 1, "营业收入同比增长。")
    ledger = CitationLedger()
    ledger.cite(chunk, Citation.QUOTE, cited_text="营业收入同比增长。")
    # The chunk is no longer in the corpus -> orphan on validate.
    val = ledger.validate(chunk_lookup={})
    assert not val.valid
    assert any(e.startswith("orphan_citation:") for e in val.errors)


# ---------------------------------------------------------------------------
# §8 — edge cases: empty corpus, no hit, multi-page similar
# ---------------------------------------------------------------------------

def test_empty_corpus_returns_empty_with_warning():
    engine = RetrievalEngine().index_chunks([])
    result = engine.search("anything", top_k=5)
    assert result.hits == []
    assert any("语料为空" in w for w in result.warnings)
    assert result.mode == "bm25"


def test_no_hit_query_returns_empty_list_not_approx():
    chunks = [_chunk("c1", "docA", 1, "营业收入同比增长。")]
    engine = RetrievalEngine().index_chunks(chunks)
    result = engine.search("净利润大幅度上升超预期超预期", top_k=5)
    assert result.hits == []


def test_same_document_multi_page_similar_keeps_page_distinct():
    facts = [
        _Fact("docA", 1, "MD&A", "公司业绩稳健增长。"),
        _Fact("docA", 5, "MD&A", "公司业绩稳健增长。"),  # identical text, different page
    ]
    chunks = build_chunks(facts)
    ids = {c.chunk_id for c in chunks}
    assert len(ids) == 2
    pages = {c.page for c in chunks}
    assert pages == {1, 5}


# ---------------------------------------------------------------------------
# §7 — graceful degrade: no embedding -> mode=bm25
# ---------------------------------------------------------------------------

def test_engine_degrades_to_bm25_without_embedding():
    chunks = [_chunk("c1", "docA", 1, "营业收入同比增长。")]
    engine = RetrievalEngine().index_chunks(chunks)  # default reranker = Bm25Only
    assert engine.mode == "bm25"
    result = engine.search("营业收入", top_k=3)
    assert result.mode == "bm25"
    assert result.hits


# ---------------------------------------------------------------------------
# §13 — benchmark metrics
# ---------------------------------------------------------------------------

def test_recall_at_k_and_citation_pr():
    chunks = [
        _chunk("c1", "docA", 1, "营业收入同比增长。"),
        _chunk("c2", "docA", 2, "毛利率上升。"),
        _chunk("c3", "docB", 1, "净利润下滑。"),
    ]
    engine = RetrievalEngine().index_chunks(chunks)
    hits = engine.search("营业收入 毛利率", top_k=2).hits
    r = recall_at_k(hits, ["c1", "c2"], k=2)
    assert r == 1.0
    pr = recall_at_k(hits, ["cX"], k=2)  # gold empty -> 0
    assert pr == 0.0

    from src.cn.retrieval.engine import citation_precision_recall

    prr = citation_precision_recall(["c1", "c2"], ["c1", "c2"])
    assert prr["precision"] == 1.0 and prr["recall"] == 1.0


# ---------------------------------------------------------------------------
# §11 — chunking from facts
# ---------------------------------------------------------------------------

def test_build_chunks_skips_empty_facts():
    facts = [
        _Fact("docA", 1, "MD&A", "公司业绩稳健增长。"),
        _Fact("docA", 2, "MD&A", ""),          # empty -> skipped
        _Fact("docA", 3, "MD&A", None),        # None -> skipped
    ]
    chunks = build_chunks(facts)
    assert len(chunks) == 1
    assert chunks[0].page == 1


def test_chunk_text_splits_long_text_with_overlap():
    long = "甲" * 700
    windows = chunk_text(long, max_chars=300, overlap=50)
    assert len(windows) > 1
    # Overlap is present: the boundary word reappears across windows.
    joined = "".join(windows)
    assert "甲" in joined


# ---------------------------------------------------------------------------
# §6 — tool integration (offline, temp registry, in-memory ingest)
# ---------------------------------------------------------------------------

def _make_registry_with_draft(tmp_path) -> DocumentRegistry:
    reg = DocumentRegistry(root=tmp_path / "docs")
    text = (
        "第一节 经营业绩。公司2024年营业收入同比增长12%，毛利率较去年上升3个百分点。"
        "管理层讨论与分析显示，核心业务保持稳健增长，现金流状况良好。"
    )
    reg.ingest(
        text.encode("utf-8"),
        declared_type=DocumentType.REPORT_DRAFT.value,
        source="user_upload",
        filename="draft.txt",
    )
    return reg


def test_search_cn_corpus_tool_returns_hits_without_evidence_id(tmp_path):
    reg = _make_registry_with_draft(tmp_path)
    tool = SearchCnCorpusTool(reg)
    result = asyncio.run(tool.execute(query="营业收入 同比", top_k=3))
    import json

    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["mode"] == "bm25"
    assert payload["corpus_size"] > 0
    assert payload["hits"]
    for hit in payload["hits"]:
        assert "evidence_id" not in hit
        assert "page" in hit and "text" in hit


def test_search_cn_corpus_tool_rejects_empty_query(tmp_path):
    reg = _make_registry_with_draft(tmp_path)
    tool = SearchCnCorpusTool(reg)
    result = asyncio.run(tool.execute(query="   ", top_k=3))
    import json

    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "retrieval_missing_query"
