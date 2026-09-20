"""Bridge normalized facts into the existing ``EvidenceLedger`` (CARD-02 §12).

Symmetrical to ``src/cn/documents/evidence_bridge.py``: the document layer
registers *raw located* cells, this layer registers the *normalized* values built
from them, so a report number can be walked back through

    plan conclusion -> normalized value -> raw cell -> document page/bbox

Every ledger entry carries ``field_path`` with ``document_id``/page/row/column so
the reverse jump (CARD-01 §14) still resolves after normalization, and the
``normalized_fact_id`` is deterministic, which keeps CARD-12 replay stable.
"""

from __future__ import annotations

from typing import Dict, List

from ..evidence import EvidenceLedger
from .adapter import period_basis_for
from .models import BASE_UNIT


def evidence_id_for(fact) -> str:
    """The normalized fact's own id — deterministic, so it doubles as evidence id."""
    return fact.normalized_fact_id


def field_path_for(document_id: str, fact) -> str:
    location = f"page{fact.page}.r{fact.row}c{fact.column}"
    return f"documents.{document_id}.{location}.{fact.metric}"


def period_basis_for_fact(fact) -> str:
    """Grade the value with its *semantic* kind, not a cumulative default.

    A disclosed single-quarter value must never be ledgered as ``*_CUMULATIVE``:
    the period class drives the validator's comparability check, and mislabelling
    it would let a single quarter be compared against a YTD figure.
    """
    return fact.period_kind.value if fact.period_kind else period_basis_for(
        fact.period_key, fact.period_kind
    )


def register_normalized_evidence(
    document, result, *, ledger: EvidenceLedger | None = None
) -> Dict[str, object]:
    """Add every ``NormalizedFact`` of a result to a fresh ledger.

    Pass ``ledger=`` to reuse a caller-owned ledger (e.g. CARD-09's claim graph);
    otherwise a new one is created for this document. A fact that the ledger
    rejects (duplicate id, non-finite value) is skipped — the ledger contract is
    never weakened by a best-effort write.
    """
    ledger = ledger or EvidenceLedger(snapshot_id=document.document_id)
    evidence_ids: List[str] = []
    skipped: List[str] = []

    for fact in result.facts:
        try:
            ledger.add_fact(
                evidence_id=evidence_id_for(fact),
                symbol=document.document_id,
                metric=fact.metric,
                value=fact.value,
                currency=fact.currency,
                unit=BASE_UNIT,
                fiscal_period=fact.period_key,
                period_basis=period_basis_for_fact(fact),
                published_at=fact.available_at,
                available_at=fact.available_at,
                provider="normalization",
                field_path=field_path_for(document.document_id, fact),
            )
        except Exception:
            skipped.append(fact.normalized_fact_id)
            continue
        evidence_ids.append(evidence_id_for(fact))

    return {
        "evidence_ids": evidence_ids,
        "evidence_count": len(evidence_ids),
        "evidence_snapshot_id": document.document_id,
        "evidence_skipped": skipped,
    }


def normalization_jumps(document, result) -> Dict[str, Dict[str, object]]:
    """Map every evidence id back to the source cell (page/bbox) for the UI jump."""
    jumps: Dict[str, Dict[str, object]] = {}
    for fact in result.facts:
        jumps[evidence_id_for(fact)] = {
            "document_id": document.document_id,
            "filename": getattr(document, "filename", None),
            "page": fact.page,
            "bbox": list(fact.bbox) if fact.bbox else None,
            "metric": fact.metric,
            "raw_value": fact.raw_value,
            "source_unit": fact.source_unit,
            "value": fact.value,
            "period_key": fact.period_key,
            "period_kind": fact.period_kind.value if fact.period_kind else None,
            "scope": fact.scope.value if fact.scope else None,
            "source_table": fact.source_table,
        }
    return jumps
