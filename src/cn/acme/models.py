"""CARD-22 §6 data models — the vocabulary ACME reports its findings in.

The point of this card is that a parsed number is *not* trusted until an
explicit, deterministic constraint has been checked against it. That makes the
vocabulary load-bearing: a finding must say which constraint family decided the
outcome, what the operands were, and what it did *not* check. Everything here is
frozen and append-only, and nothing in this module may ever rewrite a disclosed
financial value (§9, and §18's "初赛无任何自动静默修值路径").

Two ideas are deliberately spelled out as separate fields rather than being
folded into one ``ok/not ok`` flag:

* ``status`` — the *check outcome* (including ``skipped_missing_field`` and
  ``check_failed``, which are neither pass nor violation);
* ``resolution_status`` — the *workflow state* of a finding (``opened`` …
  ``dismissed``).

A constraint that could not run is reported as such. §22: "任何约束族缺失时该族
返回 ``not_checked``，IntegrityReport 显式列出未检查族，不假装全覆盖".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# §7 — the six constraint families.  The values are the strings the card's §6
# model declares, so an ``IntegrityReport`` payload can be read against the card
# without a translation table.
# ---------------------------------------------------------------------------

FAMILY_ACCOUNTING_IDENTITY = "accounting_identity"
FAMILY_TABLE_STRUCTURAL = "table_structural"
FAMILY_PERIOD = "period"
FAMILY_CROSS_PERIOD = "cross_period"
FAMILY_CROSS_SOURCE = "cross_source"
FAMILY_CROSS_MODAL = "cross_modal"

CONSTRAINT_FAMILIES: Tuple[str, ...] = (
    FAMILY_ACCOUNTING_IDENTITY,
    FAMILY_TABLE_STRUCTURAL,
    FAMILY_PERIOD,
    FAMILY_CROSS_PERIOD,
    FAMILY_CROSS_SOURCE,
    FAMILY_CROSS_MODAL,
)

FAMILY_LABELS: Dict[str, str] = {
    FAMILY_ACCOUNTING_IDENTITY: "会计恒等式",
    FAMILY_TABLE_STRUCTURAL: "表格结构（小计勾稽）",
    FAMILY_PERIOD: "期间关系",
    FAMILY_CROSS_PERIOD: "跨期连续性",
    FAMILY_CROSS_SOURCE: "跨来源一致性",
    FAMILY_CROSS_MODAL: "跨模态一致性",
}

# ---------------------------------------------------------------------------
# §6 — check outcome (``status``).
# ---------------------------------------------------------------------------

STATUS_VIOLATED = "violated"
STATUS_WARNING = "warning"
STATUS_PASSED = "passed"
STATUS_NOT_COMPARABLE = "not_comparable"
#: The constraint's inputs are simply not disclosed by this filing (§11: "跳过该
#: 约束并记录 skipped_missing_field，不报错不误报").
STATUS_SKIPPED_MISSING_FIELD = "skipped_missing_field"
#: The constraint raised mid-check. §11: it must not take the other constraints
#: down with it, and it must not be silently dropped either.
STATUS_CHECK_FAILED = "check_failed"

CHECK_STATUSES: Tuple[str, ...] = (
    STATUS_VIOLATED,
    STATUS_WARNING,
    STATUS_PASSED,
    STATUS_NOT_COMPARABLE,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_CHECK_FAILED,
)

#: Outcomes that require a human to look at them (feeds ``review_queue``).
OPEN_STATUSES: Tuple[str, ...] = (
    STATUS_VIOLATED,
    STATUS_WARNING,
    STATUS_NOT_COMPARABLE,
    STATUS_CHECK_FAILED,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# ---------------------------------------------------------------------------
# §6 — workflow state (``resolution_status``).
#
# ``auto_corrected`` is deliberately reachable only through the 决赛 path; §9
# forbids writing a corrected value without the four-field trail, which
# :func:`src.cn.acme.models.CorrectionTrail.is_complete` enforces.
# ---------------------------------------------------------------------------

RESOLUTION_OPENED = "opened"
RESOLUTION_RE_PARSED = "re_parsed"
RESOLUTION_REVIEWED = "reviewed"
RESOLUTION_AUTO_CORRECTED = "auto_corrected"
RESOLUTION_DISMISSED = "dismissed"

RESOLUTION_STATUSES: Tuple[str, ...] = (
    RESOLUTION_OPENED,
    RESOLUTION_RE_PARSED,
    RESOLUTION_REVIEWED,
    RESOLUTION_AUTO_CORRECTED,
    RESOLUTION_DISMISSED,
)

#: The single version string every artifact of this card stamps, mirroring
#: CARD-13's ``RESOLVER_VERSION`` — a finding that does not say which rule set
#: produced it cannot be replayed.
VALIDATOR_VERSION = "card22.v1"

#: §7 F6 conflict types.
CONFLICT_VALUE_MISMATCH = "value_mismatch"
CONFLICT_LABEL_MISMATCH = "label_mismatch"
CONFLICT_UNIT_MISMATCH = "unit_mismatch"
CONFLICT_PERIOD_MISMATCH = "period_mismatch"

CONFLICT_TYPES: Tuple[str, ...] = (
    CONFLICT_VALUE_MISMATCH,
    CONFLICT_LABEL_MISMATCH,
    CONFLICT_UNIT_MISMATCH,
    CONFLICT_PERIOD_MISMATCH,
)

CONFLICT_TYPE_LABELS: Dict[str, str] = {
    CONFLICT_VALUE_MISMATCH: "数值不一致",
    CONFLICT_LABEL_MISMATCH: "标签/口径不一致",
    CONFLICT_UNIT_MISMATCH: "单位不一致",
    CONFLICT_PERIOD_MISMATCH: "期间不一致",
}

CROSS_MODAL_UNRESOLVED = "unresolved"
CROSS_MODAL_RESOLVED = "resolved_which_source"

#: §12 edge case 6 — with only two parties present, the missing one is recorded
#: as N/A rather than being invented.
MISSING_PARTY = "N/A"


class AcmeError(RuntimeError):
    """Raised for a malformed constraint request, never for a violated constraint.

    A violated constraint is a *result*; an exception is a programming error.
    Keeping the two apart is what lets §11's ``check_failed`` be reported per
    constraint instead of aborting the whole report.
    """


@dataclass(frozen=True)
class ConstraintOperand:
    """One input of a constraint, carrying the provenance the card demands.

    ``ref`` is the evidence/normalized-fact id when the operand came from the
    document; a synthetic operand (a tolerance, a derived subtotal) has no ref
    and says so through an empty string rather than a fabricated id.
    """

    ref: str
    value: Optional[float]
    period: Optional[str] = None
    scope: Optional[str] = None
    version: Optional[str] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "value": self.value,
            "period": self.period,
            "scope": self.scope,
            "version": self.version,
            "unit": self.unit,
            "currency": self.currency,
            "label": self.label,
        }


@dataclass(frozen=True)
class SourceRef:
    """A resolvable pointer to where a checked value came from (§13 rule 1).

    Document evidence carries ``document_id``/``page`` (+ ``bbox``); snapshot
    evidence carries ``snapshot_id``/``available_at``. At least one pair must be
    present — a finding whose source cannot be resolved is not auditable.
    """

    document_id: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    locator: Optional[str] = None
    snapshot_id: Optional[str] = None
    available_at: Optional[str] = None

    @property
    def resolvable(self) -> bool:
        return bool(
            (self.document_id and (self.page is not None or self.locator))
            or (self.snapshot_id and self.available_at)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "locator": self.locator,
            "snapshot_id": self.snapshot_id,
            "available_at": self.available_at,
            "resolvable": self.resolvable,
        }


@dataclass(frozen=True)
class CorrectionTrail:
    """§13 rule 4 — the four fields required before *any* automatic correction.

    Kept in this card because the 决赛 correction path must not be able to write
    a value by accident: :meth:`is_complete` is the single gate, and
    ``decisions`` in the validator refuses an ``auto_corrected`` record whose
    trail is incomplete.
    """

    original_value: Optional[float]
    candidate_values: Tuple[float, ...]
    constraint_id: str
    selected_value: Optional[float]
    selection_reason: str
    manual_review_status: str

    def is_complete(self) -> bool:
        return (
            self.original_value is not None
            and bool(self.candidate_values)
            and bool(self.constraint_id)
            and self.selected_value is not None
            and bool(self.selection_reason)
            and bool(self.manual_review_status)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_value": self.original_value,
            "candidate_values": list(self.candidate_values),
            "constraint_id": self.constraint_id,
            "selected_value": self.selected_value,
            "selection_reason": self.selection_reason,
            "manual_review_status": self.manual_review_status,
            "complete": self.is_complete(),
        }


@dataclass(frozen=True)
class ConstraintViolation:
    """§6 model. One constraint, one outcome, fully located.

    ``violation_id`` is content-derived (family + constraint + the operand refs
    that participated) so that replaying the same document produces byte-equal
    findings — CARD-12's pack compares them.

    **Sign convention (uniform across all six families).**
    ``expected_value`` is the *reference* value — independently derived from the
    other operands, or taken from the higher-precedence source.
    ``actual_value`` is the value being checked — what the document (or the lower
    precedence source) printed. ``residual = expected − actual``.
    §13 rule 2 requires that identity to hold exactly on every record, so a
    reader can recompute the finding from its own payload without knowing which
    family produced it.
    """

    violation_id: str
    constraint_family: str
    constraint_id: str
    inputs: Tuple[ConstraintOperand, ...]
    expected_value: Optional[float]
    actual_value: Optional[float]
    residual: Optional[float]
    tolerance: float
    status: str
    source_refs: Tuple[SourceRef, ...]
    severity: str
    resolution_status: str = RESOLUTION_OPENED
    detected_at: str = ""
    note: Optional[str] = None
    correction: Optional[CorrectionTrail] = None

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "violation_id": self.violation_id,
            "constraint_family": self.constraint_family,
            "constraint_family_label": FAMILY_LABELS.get(
                self.constraint_family, self.constraint_family
            ),
            "constraint_id": self.constraint_id,
            "inputs": [item.to_dict() for item in self.inputs],
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "residual": self.residual,
            "tolerance": self.tolerance,
            "exceeds_tolerance": (
                self.residual is not None and abs(self.residual) > self.tolerance
            ),
            "status": self.status,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "severity": self.severity,
            "resolution_status": self.resolution_status,
            "detected_at": self.detected_at,
            "note": self.note,
            "correction": self.correction.to_dict() if self.correction else None,
            "validator_version": VALIDATOR_VERSION,
        }


@dataclass(frozen=True)
class CrossModalInconsistency:
    """§6 model — 正文 / 表格 / 图表 disagreeing about one quantity.

    §7 F6 is explicit that ACME must **not** decide which party is right: all
    three values, the computed benchmark, and the conflict type are recorded and
    the resolution stays with a human (or a later card). ``values`` keeps the
    party names as keys so a missing图表 reads as ``N/A`` instead of vanishing.
    """

    inconsistency_id: str
    metric: str
    values: Dict[str, Optional[float]]
    computed_value: Optional[float]
    conflict_type: str
    resolution_status: str = CROSS_MODAL_UNRESOLVED
    source_refs: Tuple[SourceRef, ...] = ()
    quote: Optional[str] = None
    note: Optional[str] = None

    @property
    def unresolved(self) -> bool:
        return self.resolution_status != CROSS_MODAL_RESOLVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "inconsistency_id": self.inconsistency_id,
            "metric": self.metric,
            "values": dict(self.values),
            "computed_value": self.computed_value,
            "conflict_type": self.conflict_type,
            "conflict_type_label": CONFLICT_TYPE_LABELS.get(
                self.conflict_type, self.conflict_type
            ),
            "resolution_status": self.resolution_status,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "quote": self.quote,
            "note": self.note,
            "validator_version": VALIDATOR_VERSION,
        }


@dataclass(frozen=True)
class ConstraintAvailability:
    """§22 — a constraint that could not be run, with the reason, as data.

    Reported on every run so a reader can tell "no violation found" from "no
    check performed". A missing reason is impossible by construction, which is
    the whole point: silence is what this card exists to remove.
    """

    constraint_id: str
    constraint_family: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "constraint_family": self.constraint_family,
            "constraint_family_label": FAMILY_LABELS.get(
                self.constraint_family, self.constraint_family
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IntegrityReport:
    """§6 model — the card's headline output."""

    document_id: str
    constraint_families_checked: Tuple[str, ...]
    totals: Dict[str, int]
    violations: Tuple[ConstraintViolation, ...] = ()
    cross_modal: Tuple[CrossModalInconsistency, ...] = ()
    review_queue: Tuple[str, ...] = ()
    families_not_checked: Tuple[str, ...] = ()
    constraints_not_available: Tuple[ConstraintAvailability, ...] = ()
    warnings: Tuple[str, ...] = ()
    detected_at: str = ""
    validator_version: str = VALIDATOR_VERSION

    # -- convenience views (never a second source of truth: derived) ---------

    @property
    def violations_only(self) -> List[ConstraintViolation]:
        return [item for item in self.violations if item.status == STATUS_VIOLATED]

    @property
    def warnings_only(self) -> List[ConstraintViolation]:
        return [item for item in self.violations if item.status == STATUS_WARNING]

    @property
    def has_open_items(self) -> bool:
        return bool(self.review_queue)

    def by_family(self) -> Dict[str, List[ConstraintViolation]]:
        out: Dict[str, List[ConstraintViolation]] = {family: [] for family in CONSTRAINT_FAMILIES}
        for item in self.violations:
            out.setdefault(item.constraint_family, []).append(item)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "constraint_families_checked": list(self.constraint_families_checked),
            "families_not_checked": list(self.families_not_checked),
            "totals": dict(self.totals),
            "violations": [item.to_dict() for item in self.violations],
            "cross_modal": [item.to_dict() for item in self.cross_modal],
            "review_queue": list(self.review_queue),
            "constraints_not_available": [
                item.to_dict() for item in self.constraints_not_available
            ],
            "warnings": list(self.warnings),
            "detected_at": self.detected_at,
            "validator_version": self.validator_version,
        }


def tally(results: "List[ConstraintViolation]") -> Dict[str, int]:
    """§6 ``totals`` — one counter per ``checked`` status, never a hidden zero."""
    counts: Dict[str, int] = {"checked": len(results)}
    for status in CHECK_STATUSES:
        counts[status] = sum(1 for item in results if item.status == status)
    return counts


def make_violation_id(
    constraint_id: str,
    family: str,
    parts: Tuple[str, ...],
) -> str:
    """Deterministic id: same document + same constraint + same operands → same id."""
    import hashlib

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{family}:{constraint_id}:{digest}"
