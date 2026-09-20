"""Bridge located document facts into the existing EvidenceLedger.

CARD-01's integration tests require the ingest -> facts -> evidence-ledger ->
reverse-page-lookup chain. Document facts are not A-share market statements, so
we register only *numeric* table cells, keyed by document_id (as the "symbol")
and a stable metric derived from table/row/column. This keeps the evidence ledger
contract intact without inventing financial meaning.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from ..evidence import EvidenceLedger
from .models import ExtractedFact


def _parse_float(raw_value: Optional[str]) -> Optional[float]:
    if not raw_value:
        return None
    cleaned = raw_value.replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fact_metric(fact: ExtractedFact) -> str:
    metric = fact.table or fact.section or "text"
    if fact.row is not None:
        metric = f"{metric}_r{fact.row}"
        if fact.column is not None:
            metric = f"{metric}_c{fact.column}"
    return metric


def evidence_id_for(document, fact: ExtractedFact) -> str:
    """Stable evidence id for one located fact.

    Deterministic by construction: ``hash()`` is randomized per process for
    strings, so relying on it would make evidence ids — and therefore replay
    (CARD-12) and the jump index — change between runs.
    """
    digest = hashlib.sha1(fact.raw_text.encode("utf-8")).hexdigest()[:8]
    return f"docfact_{document.document_id}_{digest}_{_fact_metric(fact)}"


def _evidence_id(document, fact: ExtractedFact) -> str:
    return evidence_id_for(document, fact)


def register_document_facts(
    document,
    facts: List[ExtractedFact],
    snapshot_id: Optional[str] = None,
) -> EvidenceLedger:
    """Add numeric document facts to a fresh EvidenceLedger and return it."""
    ledger = EvidenceLedger(snapshot_id=snapshot_id or document.document_id)
    for fact in facts:
        value = _parse_float(fact.raw_value)
        if value is None:
            continue
        metric = _fact_metric(fact)
        try:
            ledger.add_fact(
                evidence_id=_evidence_id(document, fact),
                symbol=document.document_id,
                metric=metric,
                value=value,
                currency=fact.currency or "CNY",
                unit=fact.unit or "raw",
                fiscal_period=fact.period,
                period_basis=fact.scope or "DOC",
                published_at=fact.available_at,
                available_at=fact.available_at,
                provider="document_intelligence",
                field_path=f"documents.{document.document_id}.page{fact.page}.{metric}",
            )
        except Exception:
            # Duplicate or invalid; skip without breaking the chain.
            continue
    return ledger


def document_fact_jumps(document, facts: List[ExtractedFact]) -> dict:
    """Map every registered ``evidence_id`` back to its source page slot.

    Closes the CARD-01 reverse-jump chain end-to-end: a citation shown in a
    report (``evidence_id``) can be resolved to ``document_id`` → ``page`` →
    ``bbox`` without re-parsing, which is what the demo clicks on.
    """
    jumps: dict = {}
    for fact in facts:
        if _parse_float(fact.raw_value) is None:
            continue
        jumps[_evidence_id(document, fact)] = {
            "document_id": document.document_id,
            "fact_id": fact.fact_id,
            "filename": document.filename,
            "page": fact.page,
            "bbox": list(fact.bbox) if fact.bbox else None,
            "raw_text": fact.raw_text,
            "raw_value": fact.raw_value,
            "unit": fact.unit,
            "period": fact.period,
            "scope": fact.scope,
            "available_at": fact.available_at,
        }
    return jumps
