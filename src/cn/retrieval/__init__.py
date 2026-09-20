"""Retrieval Engine (CARD-06): evidence-aware, deterministic-first search.

Public surface:

* :class:`CorpusChunk`, :class:`SearchHit`, :class:`Citation`, :class:`RetrievalResult`
* :func:`tokenize` — Chinese-aware deterministic tokenizer
* :class:`Bm25Index` — self-implemented BM25 (no third-party BM25 library)
* :func:`build_chunks` — turn CARD-01 ``ExtractedFact`` rows into ``CorpusChunk``
* :class:`CitationLedger` — promote cited chunks to evidence ids (three-way citation)
* :class:`RetrievalEngine` — index + search + cite, with graceful ``mode=bm25`` degrade
"""

from __future__ import annotations

from .bm25 import Bm25Index
from .chunking import build_chunks, chunk_text
from .citation import CitationLedger, cite_chunk
from .engine import RetrievalEngine, citation_precision_recall, recall_at_k
from .models import Citation, CorpusChunk, RetrievalResult, SearchHit
from .rerank import Bm25OnlyReranker, Reranker

# Re-export the tokenizer. NOTE: ``from .tokenize import tokenize`` would bind the
# *submodule* (Python prefers a submodule of the same name), so we bind the
# function explicitly via the module object.
from . import tokenize as _tokenize_module

tokenize = _tokenize_module.tokenize

__all__ = [
    "Bm25Index",
    "build_chunks",
    "chunk_text",
    "CitationLedger",
    "cite_chunk",
    "RetrievalEngine",
    "Citation",
    "CorpusChunk",
    "RetrievalResult",
    "SearchHit",
    "Bm25OnlyReranker",
    "Reranker",
    "tokenize",
    "recall_at_k",
    "citation_precision_recall",
]
