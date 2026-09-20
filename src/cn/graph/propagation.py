"""CARD-09 §7b — dependency semantics and fail-closed state propagation.

The propagation table (card §7b) is implemented literally:

===============  =======================  ============================  ============
upstream state   REQUIRED                 SUPPORTING                    OPTIONAL
===============  =======================  ============================  ============
BLOCKED/CONFLICT downstream blocked       coverage drop, not blocked    no effect
STALE            downstream stale         downstream flagged stale      no effect
normal           normal                   normal                        no effect
===============  =======================  ============================  ============

Invariants this module keeps:

* **Discrete states only.** There is no ``0.83`` confidence anywhere in the
  propagation output — CARD-09 §7b forbids uncalibrated pseudo-scores.
* **Fail-closed.** A REQUIRED dependency that is *missing* blocks the downstream
  claim, exactly like a blocked one. Silence is never treated as support.
* **Monotone.** Statuses only ever degrade, so the fixpoint terminates; the
  round guard exists purely to bound a cycle that slipped past build-time
  detection.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .models import (
    ROLE_OPTIONAL,
    ROLE_RANK,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    TARGET_ASSUMPTION,
    TARGET_CALCULATION,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    THESIS_BLOCKED,
    THESIS_STALE,
    THESIS_SUPPORTED,
    THESIS_WEAKENED,
    UPSTREAM_BLOCKED,
    UPSTREAM_CONFLICTED,
    UPSTREAM_MISSING,
    UPSTREAM_OK,
    UPSTREAM_PENDING,
    UPSTREAM_STALE,
    VALIDATION_BLOCKED,
    VALIDATION_PENDING,
    VALIDATION_STALE,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    VERIFICATION_CONFLICTED,
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    Claim,
    ClaimGraph,
    Thesis,
)

#: Upstream states that block a REQUIRED downstream edge (fail-closed).
_BLOCKING_UPSTREAM = (UPSTREAM_BLOCKED, UPSTREAM_CONFLICTED, UPSTREAM_MISSING)


# ---------------------------------------------------------------------------
# Upstream state
# ---------------------------------------------------------------------------
def upstream_state(graph: ClaimGraph, target_id: str, target_type: str) -> str:
    """The state one referenced node contributes to its dependents."""
    if target_type == TARGET_EVIDENCE:
        node = graph.evidence_index.get(target_id)
        if node is None:
            return UPSTREAM_MISSING
        if not node.active:
            return UPSTREAM_BLOCKED
        return UPSTREAM_OK

    if target_type == TARGET_CALCULATION:
        node = graph.calculations.get(target_id)
        if node is None:
            return UPSTREAM_MISSING
        return UPSTREAM_OK if node.validation_status == "ok" else UPSTREAM_BLOCKED

    if target_type == TARGET_CLAIM:
        claim = graph.claims.get(target_id)
        if claim is None:
            return UPSTREAM_MISSING
        if claim.verification_status == VERIFICATION_CONFLICTED:
            return UPSTREAM_CONFLICTED
        status = claim.validation_status
        if status in (VALIDATION_BLOCKED, VALIDATION_UNSUPPORTED):
            return UPSTREAM_BLOCKED
        if status == VALIDATION_STALE:
            return UPSTREAM_STALE
        if status == VALIDATION_PENDING:
            return UPSTREAM_PENDING
        return UPSTREAM_OK

    if target_type == TARGET_ASSUMPTION:
        return UPSTREAM_OK if target_id else UPSTREAM_MISSING

    return UPSTREAM_MISSING


# ---------------------------------------------------------------------------
# Structural (binding-rule) status
# ---------------------------------------------------------------------------
def structural_status(graph: ClaimGraph, claim: Claim) -> str:
    """The status a claim has *ignoring* propagation — its §7 binding outcome.

    * fact without evidence -> unsupported (and flagged orphan by detection);
    * inference without ``derived_from`` -> unsupported;
    * opinion without an assumption -> unsupported.
    """
    if claim.claim_type == CLAIM_FACT:
        return VALIDATION_SUPPORTED if claim.evidence_ids else VALIDATION_UNSUPPORTED
    if claim.claim_type == CLAIM_INFERENCE:
        return VALIDATION_SUPPORTED if claim.derived_from else VALIDATION_UNSUPPORTED
    if claim.claim_type == CLAIM_OPINION:
        return VALIDATION_SUPPORTED if claim.assumption_ids else VALIDATION_UNSUPPORTED
    return VALIDATION_UNSUPPORTED


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------
def resolve_targets(claim: Claim) -> List[Tuple[str, str, str]]:
    """All ``(target_type, target_id, role)`` edges of a claim.

    ``evidence_ids`` / ``calculation_ids`` / ``derived_from`` are REQUIRED by
    default — they *are* the §7 binding rules. An explicit ``dependency_metadata``
    entry overrides that default for the target it names (so an inference may
    declare a supporting rather than required fact). When the same target is
    listed more than once, the strictest role wins, so a caller cannot downgrade
    an edge by merely re-listing it.
    """
    explicit_roles: Dict[Tuple[str, str], str] = {}
    for ref in claim.dependency_metadata:
        key = (ref.target_type, ref.target_id)
        current = explicit_roles.get(key)
        if current is None or ROLE_RANK[ref.role] < ROLE_RANK[current]:
            explicit_roles[key] = ref.role

    ordered: List[Tuple[str, str]] = []
    for target_type, ids in (
        (TARGET_EVIDENCE, claim.evidence_ids),
        (TARGET_CALCULATION, claim.calculation_ids),
        (TARGET_CLAIM, claim.derived_from),
    ):
        for target_id in ids:
            key = (target_type, target_id)
            if key not in ordered:
                ordered.append(key)
    for ref in claim.dependency_metadata:
        key = (ref.target_type, ref.target_id)
        if key not in ordered:
            ordered.append(key)

    return [
        (target_type, target_id, explicit_roles.get((target_type, target_id), ROLE_REQUIRED))
        for target_type, target_id in ordered
    ]


# ---------------------------------------------------------------------------
# Claim / thesis status
# ---------------------------------------------------------------------------
def claim_status(graph: ClaimGraph, claim: Claim) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    """Compute a claim's status plus the coverage drops and the blocking trigger."""
    base = structural_status(graph, claim)
    if base == VALIDATION_UNSUPPORTED:
        return VALIDATION_UNSUPPORTED, [], None

    dropped: List[Dict[str, Any]] = []
    # Staleness is an external trigger (a superseding filing), so a claim marked
    # stale stays stale unless a REQUIRED edge blocks it outright.
    stale_seen = claim.claim_id in graph.stale_claim_ids
    pending_seen = False
    for target_type, target_id, role in resolve_targets(claim):
        state = upstream_state(graph, target_id, target_type)
        if role == ROLE_OPTIONAL:
            continue
        if role == ROLE_REQUIRED:
            if state in _BLOCKING_UPSTREAM:
                return VALIDATION_BLOCKED, dropped, f"{target_type}:{target_id}:{state}"
            if state == UPSTREAM_STALE:
                stale_seen = True
            elif state == UPSTREAM_PENDING:
                pending_seen = True
            continue
        # SUPPORTING: degrade coverage, never block on its own.
        if state in _BLOCKING_UPSTREAM or state == UPSTREAM_STALE:
            dropped.append(
                {
                    "claim_id": claim.claim_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "upstream_state": state,
                }
            )

    if stale_seen:
        return VALIDATION_STALE, dropped, None
    if pending_seen:
        return VALIDATION_PENDING, dropped, None
    return VALIDATION_SUPPORTED, dropped, None


def thesis_status(graph: ClaimGraph, thesis: Thesis) -> str:
    """Derive a thesis status from its REQUIRED / SUPPORTING claims (§7b)."""
    roles = thesis.role_map
    stale = False
    weakened = False
    for claim_id in thesis.claim_ids:
        role = roles[claim_id]
        state = upstream_state(graph, claim_id, TARGET_CLAIM)
        if role == ROLE_REQUIRED:
            if state in _BLOCKING_UPSTREAM:
                return THESIS_BLOCKED
            if state == UPSTREAM_STALE:
                stale = True
        elif role == ROLE_SUPPORTING:
            if state in _BLOCKING_UPSTREAM or state == UPSTREAM_STALE:
                weakened = True
        # OPTIONAL never affects thesis status
    if stale:
        return THESIS_STALE
    if weakened:
        return THESIS_WEAKENED
    return THESIS_SUPPORTED


# ---------------------------------------------------------------------------
# Fixpoint propagation
# ---------------------------------------------------------------------------
def propagate_state(
    graph: ClaimGraph,
    changed_ids: Optional[Iterable[str]] = None,
    *,
    baseline: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Recompute every claim/thesis status; return the change list.

    Deterministic: the result depends only on the graph's contents, never on
    iteration order of a dict. When ``changed_ids`` is given the returned list is
    filtered to the downstream closure of those ids (plus the ids themselves),
    which is the incremental view an evidence deactivation needs.

    ``baseline`` overrides the "before" side of the diff. Re-validation uses it
    to keep reporting the original statuses after a deactivation has already
    propagated once (otherwise the second propagation would see no change).
    """
    before = dict(baseline) if baseline is not None else {
        cid: claim.validation_status for cid, claim in graph.claims.items()
    }
    after: Dict[str, str] = {}
    dropped_by_claim: Dict[str, List[Dict[str, Any]]] = {}
    triggers: Dict[str, Optional[str]] = {}

    # Monotone degradation terminates in at most len(claims) rounds; the +2
    # guards a cycle that slipped through build-time detection.
    for _ in range(len(graph.claims) + 2):
        after = {}
        for claim_id, claim in graph.claims.items():
            status, dropped, trigger = claim_status(graph, claim)
            after[claim_id] = status
            dropped_by_claim[claim_id] = dropped
            triggers[claim_id] = trigger
        if all(after[cid] == graph.claims[cid].validation_status for cid in after):
            break
        for claim_id, status in after.items():
            if status != graph.claims[claim_id].validation_status:
                graph.claims[claim_id] = graph.claims[claim_id].with_status(status)
    else:  # pragma: no cover - defensive; a cycle should be rejected at build time
        raise RuntimeError("graph state propagation did not converge")

    for claim_id, status in after.items():
        if status != graph.claims[claim_id].validation_status:
            graph.claims[claim_id] = graph.claims[claim_id].with_status(status)

    scope: Optional[Set[str]] = None
    if changed_ids is not None:
        seeds = {str(item) for item in changed_ids}
        scope = seeds | downstream_closure(graph, seeds)

    changes: List[Dict[str, Any]] = []
    for claim_id in sorted(graph.claims):
        old = before.get(claim_id, VALIDATION_PENDING)
        new = graph.claims[claim_id].validation_status
        if old == new:
            continue
        if scope is not None and claim_id not in scope:
            continue
        changes.append(
            {
                "claim_id": claim_id,
                "old": old,
                "new": new,
                "reason": _reason_for(new, triggers.get(claim_id)),
                "trigger": triggers.get(claim_id),
                "claim_type": graph.claims[claim_id].claim_type,
            }
        )

    # Thesis statuses are derived, never stored by a caller.
    for thesis in graph.theses.values():
        thesis.status = thesis_status(graph, thesis)

    # Coverage drops are reported even when the status itself did not change:
    # losing a SUPPORTING edge is exactly the silent degradation §7b forbids.
    graph.warnings = [w for w in graph.warnings if not w.startswith("coverage_dropped:")]
    for claim_id in sorted(graph.claims):
        if scope is not None and claim_id not in scope:
            continue
        for item in dropped_by_claim.get(claim_id, ()):
            graph.warnings.append(
                f"coverage_dropped:{claim_id}:{item['target_type']}:{item['target_id']}"
            )
    return changes


def _reason_for(status: str, trigger: Optional[str]) -> str:
    if status == VALIDATION_BLOCKED:
        return f"REQUIRED 上游不可用（{trigger}）→ 阻断" if trigger else "REQUIRED 上游不可用 → 阻断"
    if status == VALIDATION_STALE:
        return "REQUIRED 上游已过期（stale）→ 下游标记 stale"
    if status == VALIDATION_UNSUPPORTED:
        return "未满足 §7 绑定规则（缺证据/推导/假设）"
    if status == VALIDATION_PENDING:
        return "上游仍在 pending"
    return "状态更新"


def downstream_closure(graph: ClaimGraph, seed_ids: Iterable[str]) -> Set[str]:
    """Every claim transitively depending on any id in ``seed_ids``.

    Traverses claim → claim edges (``derived_from`` and ``dependency_metadata``)
    and claim → evidence / calculation edges, so changing an evidence id reaches
    its facts and every inference built on them.
    """
    seeds = {str(item) for item in seed_ids}
    dependents: Dict[str, Set[str]] = {}
    for claim in graph.claims.values():
        for target_type, target_id, _role in resolve_targets(claim):
            dependents.setdefault(target_id, set()).add(claim.claim_id)

    seen: Set[str] = set()
    frontier = list(seeds)
    while frontier:
        current = frontier.pop()
        for dependent in dependents.get(current, ()):  # claim ids reached via an edge
            if dependent in seen:
                continue
            seen.add(dependent)
            frontier.append(dependent)
    # Evidence ids that are themselves claims in the seed set count as impacted.
    return {item for item in seen if item in graph.claims} | (seeds & set(graph.claims))


def coverage_dropped(graph: ClaimGraph) -> List[Dict[str, Any]]:
    """Parse ``coverage_dropped:`` warnings back into structured records."""
    out: List[Dict[str, Any]] = []
    for warning in graph.warnings:
        if not warning.startswith("coverage_dropped:"):
            continue
        _, claim_id, target_type, target_id = (warning.split(":", 3) + ["", "", "", ""])[:4]
        out.append(
            {"claim_id": claim_id, "target_type": target_type, "target_id": target_id}
        )
    return out


__all__ = [
    "claim_status",
    "coverage_dropped",
    "downstream_closure",
    "propagate_state",
    "resolve_targets",
    "structural_status",
    "thesis_status",
    "upstream_state",
]
