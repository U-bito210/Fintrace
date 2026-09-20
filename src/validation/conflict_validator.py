"""CARD-13 §10 — validator for source-conflict records.

The card states three rules; this module turns each into a check that can
actually fail. It is the difference between "we wrote down a precedence rule"
and "we can be held to it":

1. **Every selection names a rule.** ``ConflictRecord.reason`` must be one of
   :data:`src.cn.conflict.RULE_IDS`, and a ``RESOLVED`` record must name the
   source it selected. A resolution whose justification is free text is not
   auditable, so it is an error rather than a warning.
2. **A selection is backed by a candidate in its own group.** ``selected_value``
   must equal one of ``value_a`` / ``value_b``, and one of the two candidate ids
   must belong to ``selected_source``. Otherwise the record claims to have chosen
   something it was never shown.
3. **Unresolved metrics stay out of claims.** When a claim graph is supplied,
   every unresolved ``metric@period`` must be absent from every fact claim — §9's
   "宁可阻断不误选". Supplying no graph is reported as *skipped*, never as
   passed, matching the CARD-05 / CARD-08 / CARD-09 convention.

Two structural properties are also checked because §10 requires them by name:
``ConflictRecord`` must be immutable, and no record may be revised in place. Both
are verified against the live class rather than asserted in prose, so a future
edit that drops ``frozen=True`` fails the suite instead of quietly reopening the
silent-override door.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.cn.conflict import (
    CHANGE_KINDS,
    CHANGE_VERSION_SUPERSESSION,
    RULE_IDS,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    ConflictRecord,
    values_agree,
)


@dataclass(frozen=True)
class ConflictValidation:
    """Outcome of validating one conflict record set (or a corpus)."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_conflicts(
    records: Sequence[ConflictRecord],
    *,
    graph: Any = None,
    tolerance: Optional[float] = None,
) -> ConflictValidation:
    """Validate one resolution run's records.

    ``graph`` enables rule 3 — the blocking check that needs to know what the
    claims actually say. Without it that rule is reported as skipped.
    """
    errors: List[str] = []
    warnings: List[str] = []

    _check_record_shape(records, errors, warnings)
    _check_rules_traceable(records, errors)
    _check_selection_backed(records, errors, tolerance=tolerance)
    _check_immutability(errors)
    _check_unresolved_absent_from_claims(records, errors, warnings, graph=graph)

    return ConflictValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_conflicts_corpus(
    runs: Iterable[Sequence[ConflictRecord]], *, graph: Any = None
) -> ConflictValidation:
    """Validate several runs as one gate (used by the corpus / demo sweep)."""
    errors: List[str] = []
    warnings: List[str] = []
    seen = 0
    for records in runs:
        seen += 1
        result = validate_conflicts(records, graph=graph)
        errors.extend(result.errors)
        warnings.extend(result.warnings)
    if seen == 0:
        warnings.append("no_conflict_runs_supplied")
    return ConflictValidation(valid=not errors, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _check_record_shape(
    records: Sequence[ConflictRecord], errors: List[str], warnings: List[str]
) -> None:
    if not records:
        # An empty record set is legitimate (nothing disagreed) but must be
        # visible: "no conflicts" and "conflicts were never computed" differ.
        warnings.append("no_conflict_records")
        return
    seen_ids: Dict[str, int] = {}
    for record in records:
        seen_ids[record.conflict_id] = seen_ids.get(record.conflict_id, 0) + 1
        if record.change_kind not in CHANGE_KINDS:
            errors.append(f"{record.conflict_id}: unknown change_kind {record.change_kind!r}")
        if record.status not in (STATUS_RESOLVED, STATUS_UNRESOLVED):
            errors.append(f"{record.conflict_id}: unknown status {record.status!r}")
        if not record.resolver:
            errors.append(f"{record.conflict_id}: missing resolver version")
        if not record.timestamp:
            warnings.append(f"{record.conflict_id}: missing timestamp")
        if record.source_a == record.source_b and record.candidate_a == record.candidate_b:
            errors.append(f"{record.conflict_id}: compared a candidate with itself")
    duplicates = sorted(key for key, count in seen_ids.items() if count > 1)
    if duplicates:
        # Two identical records for one dispute would double-count in the demo's
        # conflict table, so it is an error rather than a cosmetic warning.
        errors.append("duplicate_conflict_ids:" + ",".join(duplicates))


def _check_rules_traceable(records: Sequence[ConflictRecord], errors: List[str]) -> None:
    for record in records:
        if record.reason not in RULE_IDS:
            errors.append(
                f"{record.conflict_id}: reason {record.reason!r} is not a known rule id"
            )
        if record.status == STATUS_RESOLVED and not record.selected_source:
            errors.append(
                f"{record.conflict_id}: RESOLVED without a selected_source "
                f"(rule {record.reason})"
            )
        if record.status == STATUS_UNRESOLVED and record.selected_source:
            # §9: an unresolved conflict must not also carry a winner.
            errors.append(
                f"{record.conflict_id}: UNRESOLVED but a selected_source is set"
            )


def _check_selection_backed(
    records: Sequence[ConflictRecord], errors: List[str], *, tolerance: Optional[float]
) -> None:
    for record in records:
        if record.status != STATUS_RESOLVED or record.selected_source is None:
            continue
        owned = [record.source_a, record.source_b]
        if record.selected_source not in owned:
            errors.append(
                f"{record.conflict_id}: selected_source {record.selected_source!r} "
                "is neither of the two compared sources"
            )
        if record.selected_value is not None and record.value_a is not None and record.value_b is not None:
            matches = values_agree(record.selected_value, record.value_a, tolerance=tolerance) or (
                values_agree(record.selected_value, record.value_b, tolerance=tolerance)
            )
            if not matches:
                errors.append(
                    f"{record.conflict_id}: selected_value {record.selected_value} "
                    f"matches neither {record.value_a} nor {record.value_b}"
                )
        if record.change_kind == CHANGE_VERSION_SUPERSESSION and not record.superseded_source:
            # §7b: a supersession keeps the old value *as superseded*. A record
            # that names no superseded source has lost the audit link.
            errors.append(
                f"{record.conflict_id}: VERSION_SUPERSESSION without superseded_source"
            )


def _check_immutability(errors: List[str]) -> None:
    params = getattr(ConflictRecord, "__dataclass_params__", None)
    if params is None or not getattr(params, "frozen", False):
        errors.append(
            "ConflictRecord is not frozen: §10 requires append-only records, and a "
            "mutable record can be revised in place after a decision was made"
        )


def _check_unresolved_absent_from_claims(
    records: Sequence[ConflictRecord],
    errors: List[str],
    warnings: List[str],
    *,
    graph: Any,
) -> None:
    """§10 rule 2 — an unresolved metric must not appear in any fact claim."""
    if graph is None:
        warnings.append("no_graph_blocking_check_skipped")
        return

    blocked = {
        (record.metric, record.period_key)
        for record in records
        if record.status == STATUS_UNRESOLVED
    }
    if not blocked:
        return

    claims = getattr(graph, "claims", None) or {}
    for claim in claims.values():
        metric = getattr(claim, "metric", None)
        period = getattr(claim, "period_key", None)
        if metric is None:
            continue
        # A claim without a declared period cannot be matched to a blocked
        # metric@period pair; that is a warning, not a pass.
        if period is None:
            if any(item[0] == metric for item in blocked):
                warnings.append(
                    f"unresolved_metric_claim_period_unknown:{getattr(claim, 'claim_id', '?')}"
                )
            continue
        if (metric, period) in blocked:
            errors.append(
                f"unresolved_metric_in_claim:{getattr(claim, 'claim_id', '?')}:"
                f"{metric}@{period}"
            )


def blocked_metric_keys(records: Sequence[ConflictRecord]) -> List[str]:
    """The ``metric@period/scope`` keys a downstream gate must refuse."""
    return sorted(
        f"{record.metric}@{record.period_key}/{record.scope or '未知口径'}"
        for record in records
        if record.status == STATUS_UNRESOLVED
    )


__all__ = [
    "ConflictValidation",
    "blocked_metric_keys",
    "validate_conflicts",
    "validate_conflicts_corpus",
]
