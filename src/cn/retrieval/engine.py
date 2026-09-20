"""Retrieval engine: index + search + cite, with graceful degrade (CARD-06).

This is the public entry point of the retrieval layer. It composes:

* the self-implemented :class:`Bm25Index` (deterministic, offline-default),
* an optional :class:`Reranker` (vector re-rank; degrades to ``mode=bm25``),
* the :class:`CitationLedger` that promotes *cited* chunks to evidence ids.

The engine never registers evidence on its own: ``search`` only locates chunks;
``cite`` is the only thing that creates a retrieval evidence id. That is the
"chunk ≠ evidence" discipline the card corrects from v2.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .bm25 import Bm25Index
from .chunking import build_chunks
from .citation import Citation, CitationLedger, CitationValidation
from .models import CitationRecord, CorpusChunk, RetrievalResult, SearchHit
from .rerank import Bm25OnlyReranker, Reranker
from .tokenize import tokenizer_backend


class RetrievalEngine:
    """In-memory evidence-aware retrieval over a fixed corpus of chunks."""

    def __init__(self, reranker: Optional[Reranker] = None) -> None:
        self.reranker = reranker or Bm25OnlyReranker()
        self._chunks: Dict[str, CorpusChunk] = {}
        self._index = Bm25Index()
        self._ledger = CitationLedger()
        self._tokenizer = tokenizer_backend()

    # -- construction -------------------------------------------------------

    def index_chunks(self, chunks: List[CorpusChunk]) -> "RetrievalEngine":
        """(Re)build the index from the given chunks."""
        self._chunks = {c.chunk_id: c for c in chunks}
        self._index = Bm25Index().build_from_chunks(chunks)
        return self

    @classmethod
    def from_registry(
        cls,
        registry,
        document_ids: Optional[List[str]] = None,
        reranker: Optional[Reranker] = None,
    ) -> "RetrievalEngine":
        """Build an engine over CARD-01 facts stored in a ``DocumentRegistry``.

        If ``document_ids`` is omitted, every registered document is indexed.
        Only facts carrying non-empty ``raw_text`` become chunks (see
        :func:`build_chunks`).
        """
        engine = cls(reranker=reranker)
        ids = document_ids if document_ids is not None else [d.document_id for d in registry.list()]
        facts: list = []
        for doc_id in ids:
            facts.extend(registry.get_facts(doc_id))
        engine.index_chunks(build_chunks(facts))
        return engine

    # -- querying -----------------------------------------------------------

    @property
    def mode(self) -> str:
        """``hybrid`` if a reranker is wired and available, else ``bm25``."""
        try:
            if self.reranker.available():
                return "hybrid"
        except Exception:
            # A broken reranker must never crash the chain; degrade to BM25.
            pass
        return "bm25"

    @property
    def corpus_size(self) -> int:
        return len(self._chunks)

    def search(self, query: str, top_k: int = 5) -> RetrievalResult:
        """Locate the best chunks for ``query``. Never raises on empty corpus."""
        warnings: List[str] = []
        if not self._chunks:
            warnings.append("语料为空：未索引任何 chunk，返回空结果（未报错）。")
            return RetrievalResult(
                query=query, mode=self.mode, hits=[], corpus_size=0, warnings=warnings
            )

        ranked = self._index.search(query, top_k=top_k)
        try:
            if self.reranker.available():
                ranked = self.reranker.rerank(query, ranked)
        except Exception:
            # Vector re-rank failed: fall back to BM25 order, keep the result.
            warnings.append("向量重排失败，已回退 BM25 排序（mode 仍为 bm25）。")

        hits: List[SearchHit] = []
        for rank, (chunk_id, score) in enumerate(ranked, start=1):
            chunk = self._chunks.get(chunk_id)
            if chunk is None:
                continue
            hits.append(SearchHit(chunk=chunk, score=score, mode=self.mode, rank=rank))

        if not hits:
            warnings.append("查询无命中：返回空列表，未强行近似。")

        return RetrievalResult(
            query=query, mode=self.mode, hits=hits,
            corpus_size=len(self._chunks), warnings=warnings,
        )

    # -- citation (the only way evidence is created) ------------------------

    def cite(
        self,
        chunk_id: str,
        citation_type: "Citation | str",
        *,
        span: Optional[tuple] = None,
        cited_text: Optional[str] = None,
        derived_from: Optional[List[str]] = None,
    ) -> str:
        """Promote a searched chunk into a retrieval evidence id."""
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            raise KeyError(f"未知 chunk_id（未索引或已被移除）: {chunk_id}")
        return self._ledger.cite(
            chunk, citation_type, span=span, cited_text=cited_text, derived_from=derived_from
        )

    def get_citation(self, evidence_id: str) -> Optional[CitationRecord]:
        return self._ledger.get(evidence_id)

    def citation_count(self) -> int:
        return len(self._ledger)

    def validate_citations(self) -> CitationValidation:
        """Re-check the citation ledger against the current chunk corpus."""
        return self._ledger.validate(self._chunks)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "corpus_size": self.corpus_size,
            "tokenizer": self._tokenizer,
            "citation_count": self.citation_count(),
        }


def recall_at_k(hits: List[SearchHit], gold_chunk_ids: List[str], k: Optional[int] = None) -> float:
    """Retrieval benchmark metric (CARD-06 §13): fraction of gold chunks in top-k.

    ``gold_chunk_ids`` is the set of chunks that *should* be retrieved for the
    query. Returns 0.0 when the gold set is empty (undefined recall is reported
    as 0 rather than 1 to avoid inflating a benchmark).
    """
    if not gold_chunk_ids:
        return 0.0
    top = hits if k is None else hits[:k]
    retrieved = {hit.chunk.chunk_id for hit in top}
    hits_gold = sum(1 for gid in gold_chunk_ids if gid in retrieved)
    return hits_gold / len(gold_chunk_ids)


def citation_precision_recall(
    predicted_chunk_ids: List[str], gold_chunk_ids: List[str]
) -> dict:
    """Citation precision/recall over a set of cited chunks (CARD-06 §13)."""
    gold = set(gold_chunk_ids)
    pred = set(predicted_chunk_ids)
    if not pred:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "tp": 0, "fp": 0, "fn": 0}
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}
