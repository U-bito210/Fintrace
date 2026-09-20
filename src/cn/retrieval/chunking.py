"""Chunking: turn CARD-01 ``ExtractedFact`` rows into ``CorpusChunk`` (CARD-06 §4).

The retrieval layer never re-parses PDFs. Its input is the located text already
produced by the Document Intelligence Layer (CARD-01): each ``ExtractedFact``
carries ``document_id`` / ``page`` / ``section`` / ``raw_text``. We promote every
fact whose ``raw_text`` is non-empty into one or more ``CorpusChunk`` objects,
preserving the page/section provenance exactly.

Design choices:

* **One fact → one or a few chunks.** A short fact (a single number-cell caption,
  a one-line note) becomes a single chunk. A long narrative fact (an MD&A
  paragraph the parser kept whole) is split into overlapping windows so a query
  term near the middle still lands in its own chunk. Every produced chunk keeps
  the fact's page/section, so a citation jump is always page-accurate.
* **chunk_id is deterministic** from ``(document_id, page, sequence)`` — re-running
  on the same registry yields identical ids, which is what the deterministic
  citation ledger depends on.
* **Empty / whitespace facts are skipped** (no point in indexing nothing). This is
  a *parse* outcome, not an error, and never raises.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .models import CorpusChunk

# Respect the competition's "chunk ≠ evidence" discipline: a chunk only exists to
# be searched. These bounds keep each chunk focused and the index small.
MAX_CHARS_PER_CHUNK = 600
OVERLAP_CHARS = 80


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK, overlap: int = OVERLAP_CHARS) -> List[str]:
    """Split a long string into overlapping windows.

    Used when a single fact's ``raw_text`` exceeds ``max_chars``. Windows overlap
    by ``overlap`` characters so a query term near a boundary is never orphaned.
    A short string is returned as a single-element list.
    """
    if text is None:
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    windows: List[str] = []
    step = max(1, max_chars - overlap)
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        windows.append(text[start:end].strip())
        if end >= len(text):
            break
        start += step
    return [w for w in windows if w]


def _fact_text(fact) -> Optional[str]:
    raw = getattr(fact, "raw_text", None)
    if raw is None:
        return None
    return str(raw).strip()


def build_chunks(facts: Iterable[object]) -> List[CorpusChunk]:
    """Promote located facts into retrieval chunks.

    ``facts`` is any iterable of objects exposing ``document_id``, ``page``,
    ``section`` and ``raw_text`` (typically ``ExtractedFact`` from CARD-01). The
    original insertion order of ``facts`` determines the per-(doc, page) sequence
    number used in ``chunk_id``, so the result is reproducible.
    """
    chunks: List[CorpusChunk] = []
    seen: dict = {}
    for fact in facts:
        document_id = getattr(fact, "document_id", None)
        if document_id is None:
            continue
        page = getattr(fact, "page", None)
        section = getattr(fact, "section", None)
        text = _fact_text(fact)
        if not text:
            continue
        key = (document_id, page)
        seq = seen.get(key, 0)
        seen[key] = seq + 1
        windows = chunk_text(text)
        for i, window in enumerate(windows):
            suffix = f"_{i}" if len(windows) > 1 else ""
            chunk_id = f"chk_{document_id}_p{page}_s{seq}{suffix}"
            chunks.append(
                CorpusChunk(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    page=page,
                    section=section,
                    text=window,
                )
            )
    return chunks


def build_chunks_for_document(
    document_id: str, facts: Iterable[object]
) -> List[CorpusChunk]:
    """Convenience: chunks for a single document (others are ignored)."""
    return build_chunks([f for f in facts if getattr(f, "document_id", None) == document_id])
