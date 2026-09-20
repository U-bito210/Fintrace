"""CARD-22 §13 — validator for :class:`~src.cn.acme.models.IntegrityReport`.

The card lists four rules for the report; each becomes a check that can actually
fail, because a rule nobody can violate is a rule nobody is held to:

1. **Every finding's ``source_refs`` resolve.** A violation the user cannot jump
   back to is an assertion, not evidence. Applied to ``violated`` / ``warning`` /
   ``not_comparable``; ``check_failed`` and ``skipped_missing_field`` are exempt
   because they say "we could not look", not "here is something we found".
2. **``residual`` is recomputable from ``expected``/``actual``.** This is the
   arithmetic version of "show your work": the report's own numbers must satisfy
   ``residual = expected − actual``, so a reader never has to trust a residual the
   rest of the payload contradicts.
3. **``review_queue`` is non-empty exactly when something is open.** A violated
   constraint that never reaches the queue is a finding nobody will read; a queue
   that names ids not in the report is a queue that cannot be worked.
4. **No automatic correction without its four-field trail.** ``auto_corrected``
   with an incomplete :class:`~src.cn.acme.models.CorrectionTrail` is refused here,
   which is what makes §9's "禁止在没有充分证据时静默修改原始财务数字" enforceable
   rather than aspirational.

Two structural properties are verified against the live classes rather than
described in prose: the models must stay ``frozen`` (a mutable finding can be
revised after the fact), and every family must be *either* checked *or* explicitly
listed as not checked — §22's "不假装全覆盖", mechanised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.cn.acme.models import (
    CHECK_STATUSES,
    CONFLICT_TYPES,
    CONSTRAINT_FAMILIES,
    CROSS_MODAL_RESOLVED,
    CROSS_MODAL_UNRESOLVED,
    OPEN_STATUSES,
    RESOLUTION_AUTO_CORRECTED,
    RESOLUTION_STATUSES,
    STATUS_VIOLATED,
    STATUS_WARNING,
    ConstraintViolation,
    CrossModalInconsistency,
    IntegrityReport,
)

#: Statuses whose findings must point somewhere resolvable (§13 rule 1).
_REF_REQUIRED_STATUSES = (
    STATUS_VIOLATED,
    STATUS_WARNING,
    "not_comparable",
)

_RESIDUAL_EPSILON = 1e-6


@dataclass(frozen=True)
class AcmeValidation:
    """Outcome of validating one report (or a corpus of them)."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_acme_report(report: IntegrityReport) -> AcmeValidation:
    """§13 over one ``IntegrityReport``."""
    errors: List[str] = []
    warnings: List[str] = []

    _check_shape(report, errors, warnings)
    _check_source_refs(report, errors)
    _check_residual_recomputable(report, errors)
    _check_review_queue(report, errors)
    _check_correction_trails(report, errors)
    _check_family_coverage(report, errors, warnings)
    _check_cross_modal(report, errors, warnings)
    _check_immutability(errors)

    return AcmeValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_acme_corpus(
    reports: Iterable[IntegrityReport],
) -> AcmeValidation:
    """Validate several reports as one gate (corpus / demo sweep)."""
    errors: List[str] = []
    warnings: List[str] = []
    seen = 0
    for report in reports:
        seen += 1
        result = validate_acme_report(report)
        errors.extend(f"{report.document_id}: {item}" for item in result.errors)
        warnings.extend(f"{report.document_id}: {item}" for item in result.warnings)
    if seen == 0:
        warnings.append("no_acme_reports_supplied")
    return AcmeValidation(valid=not errors, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _check_shape(
    report: IntegrityReport, errors: List[str], warnings: List[str]
) -> None:
    if not report.violations and not report.cross_modal:
        # An empty report is legitimate only when every family said why it could
        # not run; that is checked by ``_check_family_coverage``.
        warnings.append("empty_report")
    seen: Dict[str, int] = {}
    for item in report.violations:
        seen[item.violation_id] = seen.get(item.violation_id, 0) + 1
        if item.constraint_family not in CONSTRAINT_FAMILIES:
            errors.append(
                f"{item.violation_id}: unknown constraint_family {item.constraint_family!r}"
            )
        if item.status not in CHECK_STATUSES:
            errors.append(f"{item.violation_id}: unknown status {item.status!r}")
        if not item.constraint_id:
            errors.append(f"{item.violation_id}: missing constraint_id")
        if item.severity not in ("error", "warning"):
            errors.append(f"{item.violation_id}: unknown severity {item.severity!r}")
        if item.resolution_status not in RESOLUTION_STATUSES:
            errors.append(
                f"{item.violation_id}: unknown resolution_status {item.resolution_status!r}"
            )
        if item.tolerance < 0:
            errors.append(f"{item.violation_id}: negative tolerance {item.tolerance}")
        if item.status == STATUS_VIOLATED and item.severity != "error":
            # §7: anything outside tolerance is an error, not a style note.
            errors.append(
                f"{item.violation_id}: violated but severity is {item.severity!r}"
            )
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        # Duplicate ids would double-count in the demo's violation table and
        # break CARD-12's pack diff, so this is an error, not cosmetics.
        errors.append("duplicate_violation_ids:" + ",".join(duplicates))
    if not report.validator_version:
        errors.append("missing validator_version")


def _check_source_refs(report: IntegrityReport, errors: List[str]) -> None:
    """§13 rule 1."""
    for item in report.violations:
        if item.status not in _REF_REQUIRED_STATUSES:
            continue
        if not item.source_refs:
            errors.append(f"{item.violation_id}: no source_refs to resolve")
            continue
        if not any(ref.resolvable for ref in item.source_refs):
            errors.append(
                f"{item.violation_id}: none of its {len(item.source_refs)} source_refs "
                "resolve (need document_id+page/locator or snapshot_id+available_at)"
            )


def _check_residual_recomputable(report: IntegrityReport, errors: List[str]) -> None:
    """§13 rule 2 — ``residual = expected − actual``, or all three absent."""
    for item in report.violations:
        if item.expected_value is None or item.actual_value is None:
            if item.residual is not None:
                errors.append(
                    f"{item.violation_id}: residual {item.residual} without both "
                    "expected_value and actual_value"
                )
            continue
        if item.residual is None:
            errors.append(
                f"{item.violation_id}: expected/actual present but residual is missing"
            )
            continue
        recomputed = item.expected_value - item.actual_value
        if abs(recomputed - item.residual) > max(_RESIDUAL_EPSILON, abs(recomputed) * 1e-9):
            errors.append(
                f"{item.violation_id}: residual {item.residual} != "
                f"expected − actual = {recomputed}"
            )


def _check_review_queue(report: IntegrityReport, errors: List[str]) -> None:
    """§13 rule 3."""
    open_ids = {item.violation_id for item in report.violations if item.open}
    open_ids.update(item.inconsistency_id for item in report.cross_modal)
    queue = list(report.review_queue)
    missing = sorted(open_ids - set(queue))
    if missing:
        errors.append(
            "review_queue_missing_open_findings:" + ",".join(missing)
        )
    known = {item.violation_id for item in report.violations} | {
        item.inconsistency_id for item in report.cross_modal
    }
    unknown = sorted(set(queue) - known)
    if unknown:
        errors.append("review_queue_names_unknown_findings:" + ",".join(unknown))
    if len(queue) != len(set(queue)):
        errors.append("review_queue_has_duplicates")


def _check_correction_trails(report: IntegrityReport, errors: List[str]) -> None:
    """§13 rule 4 — the only path that may ever write a value."""
    for item in report.violations:
        if item.resolution_status != RESOLUTION_AUTO_CORRECTED:
            if item.correction is not None and not item.correction.is_complete():
                errors.append(
                    f"{item.violation_id}: incomplete correction trail on a non-corrected record"
                )
            continue
        if item.correction is None or not item.correction.is_complete():
            errors.append(
                f"{item.violation_id}: auto_corrected without a complete "
                "original/candidates/reason/manual-review trail"
            )


def _check_family_coverage(
    report: IntegrityReport, errors: List[str], warnings: List[str]
) -> None:
    """§22 — checked ⊕ not-checked, with a reason for every gap."""
    checked = set(report.constraint_families_checked)
    not_checked = set(report.families_not_checked)
    unknown = sorted((checked | not_checked) - set(CONSTRAINT_FAMILIES))
    if unknown:
        errors.append("unknown_families:" + ",".join(unknown))
    overlap = sorted(checked & not_checked)
    if overlap:
        errors.append("family_both_checked_and_not_checked:" + ",".join(overlap))
    uncovered = sorted(set(CONSTRAINT_FAMILIES) - checked - not_checked)
    if uncovered:
        errors.append(
            "family_neither_checked_nor_declared:" + ",".join(uncovered)
        )
    reasons = {
        item.constraint_family
        for item in report.constraints_not_available
        if item.reason
    }
    for family in sorted(not_checked):
        if family not in reasons:
            errors.append(f"not_checked_family_without_reason:{family}")
    if not not_checked:
        warnings.append("all_six_families_checked")


def _check_cross_modal(
    report: IntegrityReport, errors: List[str], warnings: List[str]
) -> None:
    for item in report.cross_modal:
        if not item.metric:
            errors.append(f"{item.inconsistency_id}: missing metric")
        if item.conflict_type not in CONFLICT_TYPES:
            errors.append(
                f"{item.inconsistency_id}: unknown conflict_type {item.conflict_type!r}"
            )
        if item.resolution_status not in (CROSS_MODAL_UNRESOLVED, CROSS_MODAL_RESOLVED):
            errors.append(
                f"{item.inconsistency_id}: unknown resolution_status {item.resolution_status!r}"
            )
        if not item.source_refs or not any(ref.resolvable for ref in item.source_refs):
            errors.append(
                f"{item.inconsistency_id}: no resolvable source_refs"
            )
        # §12 edge 6 — every party is present, missing ones as N/A rather than
        # absent, so a two-party comparison is visible as such.
        for party in ("body", "table", "chart"):
            if party not in item.values:
                errors.append(f"{item.inconsistency_id}: party {party!r} missing from values")
        if item.computed_value is None:
            warnings.append(
                f"{item.inconsistency_id}: no computed benchmark; finding rests on the "
                "supplied parties only"
            )


def _check_immutability(errors: List[str]) -> None:
    for cls in (ConstraintViolation, CrossModalInconsistency, IntegrityReport):
        params = getattr(cls, "__dataclass_params__", None)
        if params is None or not getattr(params, "frozen", False):
            errors.append(
                f"{cls.__name__} is not frozen: §13 requires append-only findings, and a "
                "mutable finding can be revised after it was reported"
            )


def open_findings(report: IntegrityReport) -> List[Any]:
    """The findings a human must look at, in report order."""
    return [item for item in report.violations if item.status in OPEN_STATUSES]


__all__ = [
    "AcmeValidation",
    "open_findings",
    "validate_acme_corpus",
    "validate_acme_report",
]
