"""CARD-09 §6 — evidence deactivation and incremental re-validation.

Two operations, both built on the project's **append-only** rule:

``deactivate_evidence``
    Never deletes. It appends an ``inactive`` record to
    :attr:`~src.cn.graph.models.ClaimGraph.deactivations` (the ledger's
    no-physical-delete rule extended to deletions themselves being audit
    events) and returns the impacted claim ids.

``revalidate``
    Recomputes the whole graph's statuses with the §7b propagation table and
    returns an old → new diff. This is the technical base of the demo's
    "delete an evidence -> the conclusion turns blocked" scene.

A blank ``reason`` is rejected: a deactivation with no stated cause is an audit
hole, not a convenience.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    VALIDATION_BLOCKED,
    ClaimGraph,
    EvidenceNode,
    GraphError,
    RevalidationReport,
)
from .propagation import coverage_dropped, downstream_closure, propagate_state

#: Event type emitted to a CARD-07 recorder when one is supplied.
DEACTIVATION_EVENT = "evidence_deactivated"
REVALIDATION_EVENT = "graph_revalidated"
STALE_EVENT = "claim_marked_stale"


def deactivate_evidence(
    graph: ClaimGraph,
    evidence_id: str,
    reason: str,
    *,
    recorder: Any = None,
    occurred_at: Optional[str] = None,
) -> List[str]:
    """Mark evidence inactive (audit mode), revalidate, return impacted claim ids.

    The card's §6 contract is *"returns the impacted claim ids after
    revalidation"*, so this function propagates the new state before returning —
    a caller that deactivates an evidence and reads a claim status immediately
    must see the blocked outcome, not a stale ``supported``.

    Raises :class:`GraphError` for an unknown evidence id or an empty reason —
    both are caller bugs that must surface, not be swallowed.
    """
    node = graph.evidence_index.get(evidence_id)
    if node is None:
        raise GraphError(f"unknown evidence: {evidence_id}")
    text = (reason or "").strip()
    if not text:
        raise GraphError("deactivate_evidence requires a non-empty reason")
    # Snapshot the statuses *before* the change so a later ``revalidate`` can
    # still report the true old -> new diff.
    status_before = {cid: claim.validation_status for cid, claim in graph.claims.items()}
    if not node.active:
        # Idempotent, but recorded: re-deactivating is itself an audit event.
        graph.deactivations.append(
            {
                "sequence": len(graph.deactivations),
                "evidence_id": evidence_id,
                "reason": text,
                "occurred_at": occurred_at,
                "repeated": True,
                "status_before": status_before,
            }
        )
    else:
        graph.evidence_index[evidence_id] = dataclasses.replace(
            node, active=False, deactivated_reason=text
        )
        graph.deactivations.append(
            {
                "sequence": len(graph.deactivations),
                "evidence_id": evidence_id,
                "reason": text,
                "occurred_at": occurred_at,
                "repeated": False,
                "status_before": status_before,
            }
        )

    if recorder is not None:
        recorder.emit(
            "validation",
            DEACTIVATION_EVENT,
            input_={"evidence_id": evidence_id},
            output={"reason": text, "active": False},
            status="ok",
            evidence_id=evidence_id,
        )

    impacted = sorted(_impacted(graph, [evidence_id]))
    propagate_state(graph, changed_ids=[evidence_id])
    return impacted


def mark_stale(
    graph: ClaimGraph,
    claim_id: str,
    reason: str,
    *,
    recorder: Any = None,
) -> List[Dict[str, Any]]:
    """Append a stale marker for a claim and propagate it downstream.

    Staleness enters the graph from *outside* (a corrected filing supersedes a
    figure), so it is recorded as an append-only marker rather than inferred.
    Returns the resulting status changes.
    """
    if claim_id not in graph.claims:
        raise GraphError(f"unknown claim: {claim_id}")
    text = (reason or "").strip()
    if not text:
        raise GraphError("mark_stale requires a non-empty reason")
    graph.stale_claims.append({"claim_id": claim_id, "reason": text})
    if recorder is not None:
        recorder.emit(
            "validation",
            STALE_EVENT,
            input_={"claim_id": claim_id},
            output={"reason": text},
            status="ok",
        )
    return propagate_state(graph, changed_ids=[claim_id])


def revalidate(
    graph: ClaimGraph,
    changed_evidence_ids: Optional[Iterable[str]] = None,
    *,
    recorder: Any = None,
) -> RevalidationReport:
    """Incremental re-validation: propagate statuses and diff the outcome."""
    changed: Tuple[str, ...] = tuple(
        str(item)
        for item in (
            changed_evidence_ids
            if changed_evidence_ids is not None
            else [record["evidence_id"] for record in graph.deactivations]
        )
    )

    changes = propagate_state(
        graph, changed_ids=changed, baseline=_baseline_for(graph, changed)
    )
    impacted = sorted(_impacted(graph, changed))
    blocked = sorted(
        change["claim_id"] for change in changes if change["new"] == VALIDATION_BLOCKED
    )
    dropped = [
        item for item in coverage_dropped(graph) if item["claim_id"] in set(impacted)
    ]

    report = RevalidationReport(
        run_id=graph.run_id,
        changed_evidence_ids=changed,
        impacted_claims=tuple(impacted),
        status_changes=tuple(changes),
        blocked_claims=tuple(blocked),
        coverage_dropped=tuple(dropped),
    )

    if recorder is not None:
        recorder.emit(
            "validation",
            REVALIDATION_EVENT,
            input_={"changed_evidence_ids": list(changed)},
            output=report.to_dict(),
            status="ok",
        )
    return report


def _impacted(graph: ClaimGraph, seeds: Sequence[str]) -> set:
    return downstream_closure(graph, seeds)


def _baseline_for(graph: ClaimGraph, changed: Sequence[str]) -> Optional[Dict[str, str]]:
    """The earliest recorded pre-change status snapshot for the changed evidence.

    ``deactivate_evidence`` propagates immediately so an inline status read is
    correct, which means by the time ``revalidate`` runs the graph already holds
    the new statuses. The original ones live in the deactivation log, so the diff
    stays truthful instead of reporting "nothing changed".
    """
    changed_set = set(changed)
    baseline: Optional[Dict[str, str]] = None
    for record in graph.deactivations:  # already in append order
        if str(record.get("evidence_id")) not in changed_set:
            continue
        snapshot = record.get("status_before")
        if not isinstance(snapshot, dict):
            continue
        # Earliest wins: it is the state before *any* of the changes.
        baseline = {str(key): str(value) for key, value in snapshot.items()}
        break
    return baseline


__all__ = [
    "DEACTIVATION_EVENT",
    "REVALIDATION_EVENT",
    "STALE_EVENT",
    "deactivate_evidence",
    "mark_stale",
    "revalidate",
]
