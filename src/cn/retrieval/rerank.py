"""Optional re-ranking for the Retrieval Engine (CARD-06 §7).

BM25 is the deterministic, offline-default path. The card allows an *optional*
vector re-rank (model-dependent, via a domestic embedding endpoint) as an
enhancement. Crucially, when no embedding credential is configured the engine
must **degrade gracefully to BM25 and label the result ``mode=bm25``** — it must
never pretend a vector re-rank happened.

This module defines the ``Reranker`` protocol and ships ``Bm25OnlyReranker``, the
default no-op reranker that simply preserves BM25 order. A real embedding
reranker plugs in here by implementing ``Reranker`` (it would call a provider
from ``src.llms``); the engine treats any failure as a signal to fall back to
BM25 rather than crash the main chain.
"""

from __future__ import annotations

from typing import List, Protocol, Tuple


class Reranker(Protocol):
    """Any re-ranker takes ranked BM25 hits and returns a (possibly) re-ordered list.

    Returns ``(chunk_id, score)`` pairs, same shape as :meth:`Bm25Index.search`.
    ``available`` lets the engine decide the final ``mode`` label without calling
    the model.
    """

    mode_label: str

    def available(self) -> bool:
        ...

    def rerank(self, query: str, hits: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        ...


class Bm25OnlyReranker:
    """Default reranker: identity. Keeps BM25 order, declares itself unavailable."""

    mode_label = "bm25"

    def available(self) -> bool:
        return False

    def rerank(self, query: str, hits: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        return list(hits)
