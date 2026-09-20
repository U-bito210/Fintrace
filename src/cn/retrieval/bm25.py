"""Self-implemented BM25 index (CARD-06).

We do **not** pull in a third-party BM25 library: the competition rewards
reproducibility, and a from-scratch implementation with fixed hyperparameters is
byte-for-byte deterministic. The standard BM25 scoring formula is used:

    score(D, Q) = Σ_{t in Q} IDF(t) · tf(t, D) · (k1 + 1)
                  ───────────────────────────────────────────────
                  tf(t, D) + k1 · (1 − b + b · |D| / avgdl)

    IDF(t) = ln(1 + (N − n_t + 0.5) / (n_t + 0.5))

Hyperparameters (fixed at the top of the module, with the rationale):

* ``K1 = 1.5`` — moderate term-frequency saturation. A term appearing 5 times is
  worth more than once, but not 5× more; keeps long corporate-disclosure chunks
  from being dominated by a single boilerplate term.
* ``B = 0.75`` — standard length normalization; longer chunks are penalized so a
  query term in a short, focused MD&A paragraph ranks above the same term buried
  in a 40-page appendix.

Determinism contract: given the same index (same corpus + same tokenization) and
the same query, :meth:`search` returns hits in identical order with identical
scores. Tokenization lives in :mod:`src.cn.retrieval.tokenize`, which is itself
deterministic.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .models import CorpusChunk
from .tokenize import tokenize

# Fixed hyperparameters (see module docstring for rationale).
K1 = 1.5
B = 0.75

# Floating-point tolerance for hand-computed BM25 unit tests.
BM25_EPS = 1e-6


def _idf(doc_freq: int, total_docs: int) -> float:
    """BM25 IDF with the standard +0.5 smoothing inside the logarithm."""
    return math.log(1.0 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))


class Bm25Index:
    """In-memory BM25 over a fixed corpus of token streams.

    Build once with :meth:`build` (or incrementally with :meth:`add`), then call
    :meth:`search`. The index is immutable after build in the sense that search
    always reflects the corpus present at the last ``build``/``add``; there is no
    re-ranking or model dependence.
    """

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self._doc_ids: List[str] = []
        self._doc_lengths: List[int] = []  # token counts
        self._term_freqs: Dict[str, Dict[str, int]] = {}  # term -> doc_id -> tf
        self._doc_freqs: Dict[str, int] = {}  # term -> number of docs containing it
        self._avgdl: float = 0.0
        self._n_docs: int = 0

    # -- construction -------------------------------------------------------

    def add(self, doc_id: str, tokens: List[str]) -> None:
        """Add (or replace) one document's token stream."""
        if doc_id in self._doc_ids:
            # Re-adding the same id must not double-count; reset its contribution.
            self._remove(doc_id)
        self._doc_ids.append(doc_id)
        self._doc_lengths.append(len(tokens))
        tf: Dict[str, int] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        self._term_freqs[doc_id] = tf
        for tok in tf:
            self._doc_freqs[tok] = self._doc_freqs.get(tok, 0) + 1
        self._recompute_stats()

    def build(self, documents: Dict[str, List[str]]) -> "Bm25Index":
        """Build the index from ``{doc_id: tokens}``."""
        for doc_id, tokens in documents.items():
            self.add(doc_id, tokens)
        return self

    def build_from_chunks(self, chunks: List[CorpusChunk]) -> "Bm25Index":
        """Convenience: tokenize each chunk's text and index it."""
        return self.build({c.chunk_id: tokenize(c.text) for c in chunks})

    def _remove(self, doc_id: str) -> None:
        idx = self._doc_ids.index(doc_id)
        self._doc_ids.pop(idx)
        self._doc_lengths.pop(idx)
        tf = self._term_freqs.pop(doc_id, {})
        for tok in tf:
            self._doc_freqs[tok] = max(0, self._doc_freqs.get(tok, 1) - 1)
            if self._doc_freqs.get(tok, 0) == 0:
                self._doc_freqs.pop(tok, None)

    def _recompute_stats(self) -> None:
        self._n_docs = len(self._doc_ids)
        total = sum(self._doc_lengths)
        self._avgdl = (total / self._n_docs) if self._n_docs else 0.0

    # -- querying -----------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        *,
        min_score: float = 0.0,
    ) -> List[Tuple[str, float]]:
        """Return ``[(doc_id, score), ...]`` sorted by descending score.

        Ties are broken by document id (lexicographic) so the output is fully
        deterministic even when two chunks score identically.
        """
        if self._n_docs == 0:
            return []
        query_terms = tokenize(query)
        if not query_terms:
            return []

        scored: List[Tuple[str, float]] = []
        for doc_id in self._doc_ids:
            score = self._score(query_terms, doc_id)
            if score > min_score:
                scored.append((doc_id, score))

        scored.sort(key=lambda item: (-item[1], item[0]))
        if top_k is not None:
            scored = scored[:top_k]
        return scored

    def _score(self, query_terms: List[str], doc_id: str) -> float:
        tf = self._term_freqs.get(doc_id, {})
        doc_len = self._doc_lengths[self._doc_ids.index(doc_id)]
        # Weight repeated query terms (a query "营收 营收 利润" emphasizes 营收).
        from collections import Counter

        counts = Counter(query_terms)
        total = 0.0
        for term, qtf in counts.items():
            if term not in tf:
                continue
            n_t = self._doc_freqs.get(term, 0)
            idf = _idf(n_t, self._n_docs)
            term_freq = tf[term]
            denom = term_freq + self.k1 * (1.0 - self.b + self.b * doc_len / self._avgdl)
            total += qtf * idf * (term_freq * (self.k1 + 1.0)) / denom
        return total

    # -- introspection ------------------------------------------------------

    @property
    def size(self) -> int:
        return self._n_docs

    def vocabulary(self) -> List[str]:
        return sorted(self._doc_freqs.keys())
