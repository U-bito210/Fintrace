"""Data models for the Retrieval Engine (CARD-06).

These dataclasses are the contract between the chunker, the BM25 index, the
search path and the citation ledger. They follow the CARD-06 specification:

* A ``CorpusChunk`` is a located span of document text — it is **not** an
  evidence record. Only a chunk that is actually cited to support a claim is
  promoted to an evidence id by ``CitationLedger``.
* A ``SearchHit`` is what the search path returns: the located chunk, a score,
  the retrieval ``mode`` (``bm25`` or ``hybrid``) and a stable rank.
* ``Citation`` is the three-way引用类型 from the competition scoring rubric:
  ``quote`` (逐字一致，校验器强制) / ``paraphrase`` (允许重述，须绑定 chunk_id)
  / ``inference`` (允许组合多证据，须标记 derived_from)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class Citation(str, Enum):
    """引用类型（竞赛引用管理三型）。

    * ``quote``      — 报告文本与 chunk 逐字一致，校验器强制字符级相等。
    * ``paraphrase`` — 允许重述，必须携带被引用 chunk 的 chunk_id。
    * ``inference``  — 允许组合多证据，必须标记 derived_from（非空）。
    """

    QUOTE = "quote"
    PARAPHRASE = "paraphrase"
    INFERENCE = "inference"

    @classmethod
    def coerce(cls, value: "Citation | str") -> "Citation":
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(f"未知引用类型: {value!r}（应为 quote/paraphrase/inference）")


@dataclass(frozen=True)
class CorpusChunk:
    """A located span of document text. Strictly separate from an evidence id."""

    chunk_id: str
    document_id: str
    page: Optional[int]
    section: Optional[str]
    text: str

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "page": self.page,
            "section": self.section,
            "text": self.text,
        }


@dataclass
class SearchHit:
    """One retrieval result: the located chunk plus score / mode / rank."""

    chunk: CorpusChunk
    score: float
    mode: str  # "bm25" | "hybrid"
    rank: int

    def to_dict(self) -> dict:
        # 注意：出参**不含 evidence_id** —— 引用时才由 CitationLedger 生成。
        return {
            "chunk_id": self.chunk.chunk_id,
            "document_id": self.chunk.document_id,
            "page": self.chunk.page,
            "section": self.chunk.section,
            "text": self.chunk.text,
            "score": self.score,
            "mode": self.mode,
            "rank": self.rank,
        }


@dataclass
class RetrievalResult:
    """Envelope returned by ``RetrievalEngine.search``."""

    query: str
    mode: str
    hits: List[SearchHit] = field(default_factory=list)
    corpus_size: int = 0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "mode": self.mode,
            "corpus_size": self.corpus_size,
            "hits": [hit.to_dict() for hit in self.hits],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CitationRecord:
    """A promoted evidence record: a cited chunk becomes a retrieval evidence id."""

    evidence_id: str
    chunk_id: str
    document_id: str
    citation_type: str
    span: Optional[Tuple[int, int]]
    cited_text: Optional[str]
    derived_from: Tuple[str, ...]
