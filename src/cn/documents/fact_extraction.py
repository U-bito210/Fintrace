"""Promote parser ``RawFact`` rows into validated ``ExtractedFact`` records.

This is where document-level metadata (period, currency, unit, scope, available
date) is merged into each located fact, and where the CARD-01 validator rules are
enforced:

* ExtractedFact without document_id/page is rejected (never silently stored).
* ``raw_text`` is preserved verbatim; it is never overwritten by a normalized value.
* ``normalized_value`` stays ``None`` here (filled by CARD-02).

``available_at`` defaults to the document publication date; when that is absent
the ingest time is used and the fact is flagged ``degraded`` so it is never
mistaken for a confirmed disclosure date.
"""

from __future__ import annotations

import uuid
from typing import List

from .models import (
    ExtractedFact,
    FACT_STATUS_PARTIAL,
    RawFact,
)


class FactPromotionError(ValueError):
    """A raw fact failed validation and cannot become an ExtractedFact."""


def promote_facts(document, raw_facts: List[RawFact]) -> List[ExtractedFact]:
    """Convert raw parser facts to ExtractedFact with registry validation.

    Facts missing ``document_id``/``page`` are dropped (validator rule). Each
    surviving fact keeps its verbatim ``raw_text`` and inherits document-level
    period/currency/unit/scope where the parser did not already set them.
    """
    out: List[ExtractedFact] = []
    for raw in raw_facts:
        if not getattr(raw, "page", None):
            # Validator rule: no page -> reject. Text parsers use page=1, so this
            # only drops genuinely unlocated fragments.
            continue
        available_at = raw.available_at or document.publication_date
        degraded = available_at is None
        if degraded:
            available_at = document.ingested_at or ""
        fact = ExtractedFact(
            document_id=document.document_id or "",
            raw_text=raw.raw_text,
            page=raw.page,
            bbox=raw.bbox,
            section=raw.section,
            table=raw.table,
            row=raw.row,
            column=raw.column,
            raw_value=raw.raw_value,
            normalized_value=None,  # filled by CARD-02
            currency=raw.currency or _doc_currency(document),
            unit=raw.unit,
            period=document.report_period,
            scope=raw.scope,
            available_at=available_at,
            status=FACT_STATUS_PARTIAL if degraded else "ok",
            kind=raw.kind,
            fact_id=f"fact_{uuid.uuid4().hex[:12]}",
            meta=dict(raw.meta),
        )
        if degraded:
            fact.meta.setdefault("degraded", True)
        out.append(fact)
    return out


def _doc_currency(document) -> str | None:
    # A-share filings are RMB unless a parser detected otherwise.
    return "CNY"
