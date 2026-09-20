"""Citation ledger: promote *cited* chunks into retrieval evidence (CARD-06 §6/§10).

This is the retrieval sub-ledger. It is deliberately separate from the financial
``EvidenceLedger`` (``src.cn.evidence``): a citation points at a *span of document
text*, not at a normalized financial figure. Keeping them apart preserves the
card's central correction to v2 — **chunk ≠ evidence**: a chunk exists only to be
searched, and only a chunk that is actually cited to support a claim is promoted
here into an evidence id.

The three citation types from the competition scoring rubric are enforced here:

* ``quote``      — the cited text MUST be character-exact against the chunk text
  (verbatim). The validator rejects any drift.
* ``paraphrase`` — rephrasing is allowed, but the citation MUST still bind the
  original ``chunk_id`` so the reader can jump back.
* ``inference``  — combining multiple pieces of evidence is allowed, but it MUST
  be marked with a non-empty ``derived_from`` (the chunk ids it rests on).

A hygiene invariant (CARD-06 §10): there are no *orphan* retrieval evidence — the
ledger only ever contains chunks that were explicitly cited. Because we never
auto-register on search, the invariant holds by construction; ``validate`` still
asserts it so a future refactor cannot silently break it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import Citation, CitationRecord, CorpusChunk


class CitationLedgerError(ValueError):
    """A citation is incomplete, unverifiable, or internally inconsistent."""


@dataclass
class CitationValidation:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def verify_quote(chunk: CorpusChunk, cited_text: Optional[str]) -> bool:
    """Character-exact check for ``quote`` citations.

    The cited text may be the whole chunk or a contiguous verbatim substring of
    it (e.g. quoting one sentence out of a paragraph). Either way every character
    in ``cited_text`` must appear in the chunk unchanged.
    """
    if cited_text is None:
        return False
    cited = cited_text.strip()
    if not cited:
        return False
    return cited in chunk.text


def cite_chunk(
    chunk: CorpusChunk,
    citation_type: "Citation | str",
    *,
    span: Optional[Tuple[int, int]] = None,
    cited_text: Optional[str] = None,
    derived_from: Optional[List[str]] = None,
) -> CitationRecord:
    """Create a single citation record for ``chunk`` (does not store it).

    Use :meth:`CitationLedger.cite` to register into a ledger and obtain a stable
    evidence id. This free function is the pure validation front-end the engine
    delegates to.
    """
    ctype = Citation.coerce(citation_type)
    derived = tuple(derived_from or ())

    if ctype is Citation.QUOTE:
        if not verify_quote(chunk, cited_text):
            raise CitationLedgerError(
                "quote 引用必须与 chunk 逐字一致（字符级），"
                f"chunk_id={chunk.chunk_id} 的原文不含给定文本。"
            )
    elif ctype is Citation.PARAPHRASE:
        if not chunk.chunk_id:
            raise CitationLedgerError("paraphrase 引用必须绑定原始 chunk_id。")
    elif ctype is Citation.INFERENCE:
        if not derived:
            raise CitationLedgerError("inference 引用必须标记 derived_from（非空）。")

    evidence_id = f"cite_{uuid.uuid4().hex[:12]}"
    return CitationRecord(
        evidence_id=evidence_id,
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        citation_type=ctype.value,
        span=span,
        cited_text=cited_text,
        derived_from=derived,
    )


class CitationLedger:
    """Stores only explicitly-cited chunks, each as a retrieval evidence id."""

    def __init__(self) -> None:
        self._records: Dict[str, CitationRecord] = {}

    def cite(
        self,
        chunk: CorpusChunk,
        citation_type: "Citation | str",
        *,
        span: Optional[Tuple[int, int]] = None,
        cited_text: Optional[str] = None,
        derived_from: Optional[List[str]] = None,
    ) -> str:
        """Register a citation and return its evidence id."""
        record = cite_chunk(
            chunk, citation_type,
            span=span, cited_text=cited_text, derived_from=derived_from,
        )
        if record.evidence_id in self._records:
            # uuid4 collision is astronomically unlikely, but stay defensive.
            raise CitationLedgerError(f"重复的 citation evidence id: {record.evidence_id}")
        self._records[record.evidence_id] = record
        return record.evidence_id

    def get(self, evidence_id: str) -> Optional[CitationRecord]:
        return self._records.get(evidence_id)

    def records(self) -> List[CitationRecord]:
        return list(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def validate(
        self,
        chunk_lookup: Dict[str, CorpusChunk],
    ) -> CitationValidation:
        """Re-check every citation against the current chunk corpus.

        Asserts the §10 hygiene invariant (no orphan evidence: every evidence id
        maps to a chunk that still exists in the corpus) and re-verifies the
        three-way citation rules.
        """
        errors: List[str] = []
        for record in self._records.values():
            chunk = chunk_lookup.get(record.chunk_id)
            if chunk is None:
                # Orphan: cited chunk is no longer in the indexed corpus.
                errors.append(f"orphan_citation:{record.evidence_id}:{record.chunk_id}")
                continue
            ctype = Citation(record.citation_type)
            if ctype is Citation.QUOTE and not verify_quote(chunk, record.cited_text):
                errors.append(f"quote_mismatch:{record.evidence_id}")
            if ctype is Citation.PARAPHRASE and not record.chunk_id:
                errors.append(f"paraphrase_missing_chunk:{record.evidence_id}")
            if ctype is Citation.INFERENCE and not record.derived_from:
                errors.append(f"inference_missing_derived:{record.evidence_id}")
        return CitationValidation(valid=not errors, errors=errors)
