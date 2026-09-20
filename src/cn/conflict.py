"""CARD-13: 来源冲突解决 (source conflict resolution).

Real financial material conflicts with itself. One 指标 during one 期间 routinely
arrives from several sources — the original filing, a 更正公告, a restated
comparative, a third-party snapshot — and a system without an explicit
precedence rule either picks the wrong number or picks whichever arrived last.
Both are incidents.

This module does three things and refuses to do a fourth:

1. **Classifies before it arbitrates.** Not every disagreement is a conflict.
   ``classify_cross_source`` returns the four-class ACME verdict
   (MATCH / ROUNDING_MATCH / CONFLICT / NOT_COMPARABLE), and
   ``classify_change`` separates the three *change* states of §7b
   (DATA_CONFLICT / VERSION_SUPERSESSION / NEW_INFORMATION). A 更正公告 that
   replaces a figure is a *supersession*, not a data conflict, and the two carry
   different downstream rules.

2. **Arbitrates by a named rule.** :data:`SOURCE_PRECEDENCE` is the card's four
   levels; the level that decided a choice is written into
   :class:`ConflictRecord.reason` as a rule id, so every selection is auditable
   and a Validator can check the record against the rule table.

3. **Records every discarded candidate.** :func:`assert_no_silent_override`
   proves mechanically that no candidate vanished without a record. Silent
   override is the red line this card exists to draw (§9).

What it will **not** do: guess. An unresolvable tie is reported ``UNRESOLVED``
and the metric is blocked downstream rather than being resolved by coin flip
(§9 "宁可阻断不误选"), and a value whose unit or period cannot be established
produces no candidate at all — it produces a warning.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .disclosure_lexicon import VERSION_COLUMN_KEYWORDS
from .normalization.adapter import primary_sort_key
from .normalization.metrics import resolve_metric
from .normalization.models import NormalizedFact, Scope, Version
from .normalization.periods import canonical_period, is_supported_period_key
from .normalization.units import UnitNormalizationError, normalize_unit
from .normalization.version import version_rank

# ---------------------------------------------------------------------------
# §7b — change kinds. Three states, three different propagation rules.
# ---------------------------------------------------------------------------

CHANGE_DATA_CONFLICT = "DATA_CONFLICT"
CHANGE_VERSION_SUPERSESSION = "VERSION_SUPERSESSION"
CHANGE_NEW_INFORMATION = "NEW_INFORMATION"

CHANGE_KINDS: Tuple[str, ...] = (
    CHANGE_DATA_CONFLICT,
    CHANGE_VERSION_SUPERSESSION,
    CHANGE_NEW_INFORMATION,
)

CHANGE_KIND_LABELS: Dict[str, str] = {
    CHANGE_DATA_CONFLICT: "同口径数值冲突",
    CHANGE_VERSION_SUPERSESSION: "版本替代（新版本明确更正旧值）",
    CHANGE_NEW_INFORMATION: "新增信息（无替代关系）",
}

# ---------------------------------------------------------------------------
# ACME 跨源四分类 (CARD-22 §7b). The vocabulary lives here because CARD-13 owns
# the *decision* that consumes it; CARD-22's engine classifies and hands the
# verdict over rather than re-inventing a second spelling of "these agree".
# ---------------------------------------------------------------------------

ACME_MATCH = "MATCH"
ACME_ROUNDING_MATCH = "ROUNDING_MATCH"
ACME_CONFLICT = "CONFLICT"
ACME_NOT_COMPARABLE = "NOT_COMPARABLE"

ACME_VERDICTS: Tuple[str, ...] = (
    ACME_MATCH,
    ACME_ROUNDING_MATCH,
    ACME_CONFLICT,
    ACME_NOT_COMPARABLE,
)

ACME_VERDICT_LABELS: Dict[str, str] = {
    ACME_MATCH: "一致",
    ACME_ROUNDING_MATCH: "舍入内一致",
    ACME_CONFLICT: "冲突，需裁决",
    ACME_NOT_COMPARABLE: "口径未统一，不可比",
}

# ---------------------------------------------------------------------------
# §7 — the four-level source precedence, as data.
#
# 「监管披露」means what the exchange / 巨潮 published: the periodic report itself
# and any 更正公告 to it. A third-party data vendor's snapshot is *always* below
# official disclosure — a vendor number may corroborate a filing, never override
# one. Higher rank wins.
# ---------------------------------------------------------------------------

SOURCE_REGULATORY_CORRECTION = "regulatory_correction"  # 后发布且明确更正前值的监管披露
SOURCE_REGULATORY_FILING = "regulatory_filing"          # 原监管披露
SOURCE_COMPANY_ANNOUNCEMENT = "company_announcement"    # 官方公司公告
SOURCE_THIRD_PARTY = "third_party"                      # 第三方数据源
SOURCE_UNKNOWN = "unknown"                              # 来源类别未判定

#: Rank 3 → 0, exactly the card's four levels. ``SOURCE_UNKNOWN`` is deliberately
#: *absent*: an unclassified source has no rank, so it can never win an
#: arbitration and can never be silently promoted to a known class.
SOURCE_PRECEDENCE: Dict[str, int] = {
    SOURCE_REGULATORY_CORRECTION: 3,
    SOURCE_REGULATORY_FILING: 2,
    SOURCE_COMPANY_ANNOUNCEMENT: 1,
    SOURCE_THIRD_PARTY: 0,
}

SOURCE_CLASS_LABELS: Dict[str, str] = {
    SOURCE_REGULATORY_CORRECTION: "监管披露（更正公告）",
    SOURCE_REGULATORY_FILING: "监管披露（定期报告）",
    SOURCE_COMPANY_ANNOUNCEMENT: "公司公告",
    SOURCE_THIRD_PARTY: "第三方数据源",
    SOURCE_UNKNOWN: "来源类别未知",
}

#: Document type → source class. A 更正公告 arrives as ``announcement`` but is a
#: *regulatory* disclosure, so the caller refines it with ``is_correction=True``
#: rather than this table having to guess from a filename.
DOCUMENT_TYPE_SOURCE_CLASS: Dict[str, str] = {
    "annual": SOURCE_REGULATORY_FILING,
    "semi_annual": SOURCE_REGULATORY_FILING,
    "quarterly": SOURCE_REGULATORY_FILING,
    "announcement": SOURCE_COMPANY_ANNOUNCEMENT,
}

# ---------------------------------------------------------------------------
# Rule ids. Every selected value must name one of these (§10 rule 1), which is
# what makes "why this number?" answerable without reading the code path.
# ---------------------------------------------------------------------------

RULE_REGULATORY_CORRECTION = "rule.regulatory_correction_supersedes_prior"
RULE_VERSION_SUPERSESSION = "rule.newer_version_supersedes"
RULE_SOURCE_PRECEDENCE = "rule.source_class_precedence"
RULE_TIE_UNRESOLVED = "rule.tied_top_rank_unresolved"
RULE_VALUES_AGREE = "rule.values_agree_within_tolerance"
RULE_NEW_PERIOD = "rule.new_period_no_supersession"
RULE_NOT_COMPARABLE = "rule.not_comparable_requires_normalization"
RULE_UNKNOWN_SOURCE = "rule.unknown_source_class_cannot_win"
#: Two candidates from the *same* document are not a cross-source conflict at
#: all: CARD-02 already ranks them (audited statement body over summary table).
#: CARD-13 records that decision instead of re-deciding it, so the two cards can
#: never disagree about which table speaks for a company.
RULE_INTRA_DOCUMENT_PRECEDENCE = "rule.intra_document_table_precedence"

RULE_IDS: Tuple[str, ...] = (
    RULE_REGULATORY_CORRECTION,
    RULE_VERSION_SUPERSESSION,
    RULE_SOURCE_PRECEDENCE,
    RULE_TIE_UNRESOLVED,
    RULE_VALUES_AGREE,
    RULE_NEW_PERIOD,
    RULE_NOT_COMPARABLE,
    RULE_UNKNOWN_SOURCE,
    RULE_INTRA_DOCUMENT_PRECEDENCE,
)

RESOLVER_VERSION = "card13.v1"

#: Record status. ``UNRESOLVED`` is a *result*, not an error: §9 prefers a
#: blocked metric to a wrong one.
STATUS_RESOLVED = "RESOLVED"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_SUPERSEDED = "SUPERSEDED"

#: Tolerance for "the same number printed twice". §8 edge case 3: a difference
#: inside the displayed precision is not a conflict. Mirrors CARD-02's
#: ``_values_differ`` so the two cards can never disagree about equality.
ABS_TOLERANCE = 1e-6
REL_TOLERANCE = 1e-9


class ConflictError(RuntimeError):
    """Raised for a malformed conflict request or a provable silent override."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictCandidate:
    """One disclosure of one metric+period, shaped for arbitration.

    ``source_id`` is the card's *source*: a ``document_id`` or a provider
    ``snapshot_id``. ``fact`` keeps the originating :class:`NormalizedFact` when
    the candidate came from CARD-02, so a caller can read page/row/column back
    off it; it is excluded from serialisation because it is already in the
    registry.
    """

    candidate_id: str
    source_id: str
    source_class: str
    metric: str
    period_key: str
    scope: Optional[str]
    currency: str
    value: float
    version: Version
    available_at: Optional[str] = None
    is_explicit_correction: bool = False
    ref: str = ""
    quote: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    fact: Any = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def rank(self) -> Optional[int]:
        """Source precedence rank, or ``None`` when the class is unknown."""
        return SOURCE_PRECEDENCE.get(self.source_class)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "source_class": self.source_class,
            "source_class_label": SOURCE_CLASS_LABELS.get(
                self.source_class, self.source_class
            ),
            "metric": self.metric,
            "period_key": self.period_key,
            "scope": self.scope,
            "currency": self.currency,
            "value": self.value,
            "version": self.version.value if isinstance(self.version, Version) else str(self.version),
            "available_at": self.available_at,
            "is_explicit_correction": self.is_explicit_correction,
            "ref": self.ref,
            "quote": self.quote,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ConflictRecord:
    """One arbitrated disagreement (§5). Append-only by construction — frozen,
    and never rewritten once produced; a later correction appends a new record
    that supersedes this one rather than editing it."""

    conflict_id: str
    change_kind: str
    source_a: str
    source_b: str
    selected_source: Optional[str]
    reason: str
    resolver: str
    timestamp: str

    # -- context that makes the record readable without the graph in hand --
    metric: str = ""
    period_key: str = ""
    scope: Optional[str] = None
    currency: str = ""
    source_class_a: str = SOURCE_UNKNOWN
    source_class_b: str = SOURCE_UNKNOWN
    #: Candidate-level ids. ``source_a``/``source_b`` name a *document*, and one
    #: document routinely yields several candidates for the same key; §9's "no
    #: silent override" guarantee is only checkable at the granularity of the
    #: thing being overridden, so the candidate ids travel with the record.
    candidate_a: str = ""
    candidate_b: str = ""
    value_a: Optional[float] = None
    value_b: Optional[float] = None
    selected_value: Optional[float] = None
    superseded_source: Optional[str] = None
    status: str = STATUS_RESOLVED
    ref_a: str = ""
    ref_b: str = ""
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == STATUS_RESOLVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "change_kind": self.change_kind,
            "change_kind_label": CHANGE_KIND_LABELS.get(self.change_kind, self.change_kind),
            "source_a": self.source_a,
            "source_b": self.source_b,
            "selected_source": self.selected_source,
            "reason": self.reason,
            "rule_id": self.reason,
            "resolver": self.resolver,
            "timestamp": self.timestamp,
            "metric": self.metric,
            "period_key": self.period_key,
            "scope": self.scope,
            "currency": self.currency,
            "source_class_a": self.source_class_a,
            "source_class_b": self.source_class_b,
            "candidate_a": self.candidate_a,
            "candidate_b": self.candidate_b,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "selected_value": self.selected_value,
            "superseded_source": self.superseded_source,
            "status": self.status,
            "ref_a": self.ref_a,
            "ref_b": self.ref_b,
            "note": self.note,
        }


@dataclass(frozen=True)
class ConflictChoice:
    """What one metric+period resolved to.

    §6 says the first return value is "选定值"; a choice is that plus the
    provenance needed to act on it. ``fact`` is the winning
    :class:`NormalizedFact` when there was one — a correction read out of prose
    leaves it ``None`` on purpose, because dressing prose up as a table fact is
    exactly the kind of quiet mislabelling this card forbids.
    """

    metric: str
    period_key: str
    scope: Optional[str]
    currency: str
    value: Optional[float]
    change_kind: str
    status: str
    selected: Optional[ConflictCandidate]
    candidates: Tuple[ConflictCandidate, ...]
    conflict_ids: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_UNRESOLVED

    @property
    def fact(self) -> Any:
        return self.selected.fact if self.selected is not None else None

    @property
    def source_id(self) -> Optional[str]:
        return self.selected.source_id if self.selected is not None else None

    @property
    def key(self) -> str:
        return f"{self.metric}@{self.period_key}/{self.scope or '未知口径'}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "period_key": self.period_key,
            "scope": self.scope,
            "currency": self.currency,
            "value": self.value,
            "change_kind": self.change_kind,
            "status": self.status,
            "selected_source": self.source_id,
            "selected_candidate": self.selected.to_dict() if self.selected else None,
            "candidates": [item.to_dict() for item in self.candidates],
            "conflict_ids": list(self.conflict_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CorrectionPair:
    """A 更正前/更正后 pair read out of a correction announcement's prose.

    ``scope`` is **not** read from the sentence — a 更正公告 states 口径 far less
    often than a statement does. It is whatever the caller declared, and when the
    caller declares nothing the value stays ``None`` so the pair cannot silently
    join a 合并 group it was never shown to belong to.
    """

    metric: str
    period_key: str
    before_value: float
    after_value: float
    before_raw: str
    after_raw: str
    unit: str
    unit_source: str
    currency: str
    quote: str
    page: Optional[int]
    bbox: Optional[Tuple[float, float, float, float]]
    source_fact_id: Optional[str]
    scope: Optional[str] = None
    available_at: Optional[str] = None
    scope_source: str = "undeclared"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "period_key": self.period_key,
            "before_value": self.before_value,
            "after_value": self.after_value,
            "before_raw": self.before_raw,
            "after_raw": self.after_raw,
            "unit": self.unit,
            "unit_source": self.unit_source,
            "currency": self.currency,
            "scope": self.scope,
            "scope_source": self.scope_source,
            "available_at": self.available_at,
            "quote": self.quote,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "source_fact_id": self.source_fact_id,
        }


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------


def default_source_class(document_type: object, *, is_correction: bool = False) -> str:
    """Map a document type onto the four-level precedence.

    A ``更正公告`` is a *regulatory* disclosure — it is filed with the exchange —
    so it outranks a plain company announcement and, when it explicitly replaces
    an earlier figure, outranks the filing it corrects. ``is_correction`` must be
    supplied by the caller (CARD-04's signal is the source of truth); inferring
    it from the filename here would be a guess wearing a heuristic's clothes.
    """
    if is_correction:
        return SOURCE_REGULATORY_CORRECTION
    token = str(document_type or "").strip().lower()
    return DOCUMENT_TYPE_SOURCE_CLASS.get(token, SOURCE_UNKNOWN)


def _coerce_scope(value: object) -> Optional[str]:
    scope = Scope.coerce(value) if not isinstance(value, Scope) else value
    if scope is None:
        return None
    return scope.value if isinstance(scope, Scope) else str(scope)


def values_agree(left: float, right: float, *, tolerance: Optional[float] = None) -> bool:
    """§8 edge case 3 — equal within the displayed precision."""
    if tolerance is not None:
        return abs(left - right) <= abs(tolerance)
    if left == right:
        return True
    return abs(left - right) <= max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(left), abs(right)))


def _digest(*parts: object) -> str:
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def candidate_from_fact(
    fact: NormalizedFact,
    *,
    source_class: str,
    is_explicit_correction: bool = False,
    ref: str = "",
) -> ConflictCandidate:
    """Adapt one CARD-02 fact into an arbitration candidate."""
    scope = _coerce_scope(fact.scope)
    version = fact.version if isinstance(fact.version, Version) else Version.AS_REPORTED
    return ConflictCandidate(
        candidate_id=fact.normalized_fact_id,
        source_id=fact.document_id or fact.source_fact_id or "unknown_source",
        source_class=source_class,
        metric=fact.metric,
        period_key=fact.period_key,
        scope=scope,
        currency=fact.currency,
        value=fact.value,
        version=version,
        available_at=fact.available_at,
        is_explicit_correction=is_explicit_correction,
        ref=ref or _fact_ref(fact),
        page=fact.page,
        bbox=fact.bbox,
        fact=fact,
        warnings=tuple(fact.warnings),
    )


def _fact_ref(fact: NormalizedFact) -> str:
    table = fact.source_table or "未知表"
    page = f"第{fact.page}页" if fact.page is not None else "页未知"
    if fact.row is not None:
        page = f"{page} r{fact.row}"
        if fact.column is not None:
            page = f"{page}c{fact.column}"
    return f"{table} {page}"


# ---------------------------------------------------------------------------
# Classification — ACME four-class, then the three change states
# ---------------------------------------------------------------------------


def _comparability_blocker(a: ConflictCandidate, b: ConflictCandidate) -> Optional[str]:
    """Why two candidates cannot be compared at all, or ``None``."""
    if a.metric != b.metric:
        return f"指标不同（{a.metric} vs {b.metric}）"
    if (a.scope or "") != (b.scope or ""):
        return f"口径不同（{a.scope or '未知'} vs {b.scope or '未知'}）"
    if str(a.currency) != str(b.currency):
        return f"币种不同（{a.currency} vs {b.currency}）"
    return None


def classify_cross_source(
    a: ConflictCandidate, b: ConflictCandidate, *, tolerance: Optional[float] = None
) -> Tuple[str, str]:
    """ACME four-class verdict for a pair, with the reason.

    Returns ``(verdict, reason)``. A different 口径 or 币种 is
    ``NOT_COMPARABLE`` — §7 is explicit that differing scope/version is *not* a
    conflict, it is a job for CARD-02's normalization pass first. A different
    period is also not comparable at the *value* level; the caller asks
    :func:`classify_change` to decide whether that is NEW_INFORMATION.
    """
    blocker = _comparability_blocker(a, b)
    if blocker is not None:
        return ACME_NOT_COMPARABLE, f"{RULE_NOT_COMPARABLE}: {blocker}"
    if a.period_key != b.period_key:
        return (
            ACME_NOT_COMPARABLE,
            f"{RULE_NOT_COMPARABLE}: 期间不同（{a.period_key} vs {b.period_key}）",
        )
    if a.value == b.value:
        return ACME_MATCH, f"{RULE_VALUES_AGREE}: 两个来源取值相同"
    if values_agree(a.value, b.value, tolerance=tolerance):
        return (
            ACME_ROUNDING_MATCH,
            f"{RULE_VALUES_AGREE}: 差异在舍入容差内（{a.value} vs {b.value}）",
        )
    return (
        ACME_CONFLICT,
        f"同期间同口径同版本取值不一致（{a.value} vs {b.value}）",
    )


def _supersedes(older: ConflictCandidate, newer: ConflictCandidate) -> bool:
    """Whether ``newer`` explicitly replaces ``older`` (§7b VERSION_SUPERSESSION).

    Two independent proofs are accepted, and neither is a heuristic on wording:

    * the newer disclosure is tagged ``RESTATED`` or ``CORRECTED`` and its version
      rank is strictly ahead — the label comes from CARD-04's detection, not from
      this module re-reading text; or
    * the newer disclosure is an explicit 更正公告 (``is_explicit_correction``,
      again a CARD-04 signal) published *after* the older one.
    """
    if newer.version == older.version:
        return False
    if version_rank(newer.version) > version_rank(older.version) and newer.version in (
        Version.RESTATED,
        Version.CORRECTED,
    ):
        return True
    if newer.is_explicit_correction and older.version is Version.AS_REPORTED:
        if newer.available_at and older.available_at:
            return str(newer.available_at) >= str(older.available_at)
        return newer.available_at is not None or older.available_at is None
    return False


def classify_change(
    a: ConflictCandidate, b: ConflictCandidate, *, tolerance: Optional[float] = None
) -> Tuple[str, str]:
    """The §7b three-state verdict for a pair, with the reason.

    Order is load-bearing: a supersession is checked before a data conflict,
    because a 更正公告 that changes a figure satisfies *both* naive tests and
    only the supersession reading carries the correct downstream rule (keep the
    old value as ``superseded``, propagate stale to the downstream).
    """
    if a.period_key != b.period_key:
        return (
            CHANGE_NEW_INFORMATION,
            f"{RULE_NEW_PERIOD}: 新期间 {b.period_key} 出现，与 {a.period_key} 无替代关系",
        )
    older, newer = _order_by_recency(a, b)
    if _supersedes(older, newer):
        return (
            CHANGE_VERSION_SUPERSESSION,
            f"{RULE_VERSION_SUPERSESSION}: {newer.version.value}（{newer.ref}）替代 "
            f"{older.version.value}（{older.ref}）",
        )
    if values_agree(a.value, b.value, tolerance=tolerance):
        return (
            CHANGE_DATA_CONFLICT,
            f"{RULE_VALUES_AGREE}: 取值一致，无需裁决",
        )
    return (
        CHANGE_DATA_CONFLICT,
        "同期间同口径同版本取值不一致，无版本替代关系",
    )


def _order_by_recency(
    a: ConflictCandidate, b: ConflictCandidate
) -> Tuple[ConflictCandidate, ConflictCandidate]:
    """(older, newer), deterministic even when timestamps tie.

    Ties fall back to version rank and then ``candidate_id`` so the same input
    always produces the same ``ConflictRecord`` — a resolver that reorders itself
    between runs cannot be audited.
    """
    key_a = (a.available_at or "", version_rank(a.version), a.candidate_id)
    key_b = (b.available_at or "", version_rank(b.version), b.candidate_id)
    return (a, b) if key_a <= key_b else (b, a)


# ---------------------------------------------------------------------------
# Arbitration
# ---------------------------------------------------------------------------


def arbitrate_conflict(
    a: ConflictCandidate,
    b: ConflictCandidate,
    *,
    tolerance: Optional[float] = None,
) -> Tuple[Optional[ConflictCandidate], str, str]:
    """Decide one DATA_CONFLICT pair. Returns ``(winner, rule_id, note)``.

    ``winner is None`` means unresolved, which §9 prefers over a wrong pick.
    Unknown-class sources never win: promoting one would silently move a
    third-party number into an official slot.
    """
    blocker = _comparability_blocker(a, b)
    if blocker is not None:
        return None, RULE_NOT_COMPARABLE, blocker

    rank_a, rank_b = a.rank, b.rank
    if rank_a is None and rank_b is None:
        return (
            None,
            RULE_UNKNOWN_SOURCE,
            "两个来源的类别均未判定，按 §9 不裁决，交人工复核",
        )
    if rank_a is None or rank_b is None:
        known, unknown = (a, b) if rank_a is not None else (b, a)
        return (
            known,
            RULE_SOURCE_PRECEDENCE,
            f"来源类别未知的一方（{unknown.ref}）不得胜出，采用已知类别来源"
            f"（{known.source_class}，{known.ref}）",
        )
    if rank_a == rank_b:
        # §8 edge case 2: two sources of the same class disagreeing is genuinely
        # unresolvable from precedence alone.
        return (
            None,
            RULE_TIE_UNRESOLVED,
            f"同类来源（{a.source_class} / {b.source_class}）取值不一致，"
            "优先级相同无法裁决，交人工复核",
        )
    winner, loser = (a, b) if rank_a > rank_b else (b, a)
    return (
        winner,
        RULE_SOURCE_PRECEDENCE,
        f"来源优先级：{winner.source_class}({winner.rank}) > "
        f"{loser.source_class}({loser.rank})，采用 {winner.ref}",
    )


def compare_pair(
    a: ConflictCandidate, b: ConflictCandidate, *, tolerance: Optional[float] = None
) -> Dict[str, Any]:
    """ACME's four-class verdict for one pair, with the action it implies.

    This is the explicit surface for the CARD-22 hand-off. ``resolve_conflicts``
    never needs it — its grouping keeps incomparable candidates in separate
    series on purpose (§7) — but a caller that *asks* for two specific candidates
    to be compared gets an unambiguous answer, including the ``NOT_COMPARABLE``
    case whose required action is "CARD-02 must normalise first", not "pick one".
    """
    verdict, verdict_reason = classify_cross_source(a, b, tolerance=tolerance)
    change_kind, change_reason = classify_change(a, b, tolerance=tolerance)

    out: Dict[str, Any] = {
        "verdict": verdict,
        "verdict_label": ACME_VERDICT_LABELS.get(verdict, verdict),
        "verdict_reason": verdict_reason,
        "change_kind": change_kind,
        "change_kind_reason": change_reason,
        "a": a.to_dict(),
        "b": b.to_dict(),
        "required_action": None,
        "winner": None,
        "rule_id": None,
    }

    if verdict in (ACME_MATCH, ACME_ROUNDING_MATCH):
        out["required_action"] = "无需裁决：两个来源一致"
        out["winner"] = a.source_id
        out["rule_id"] = RULE_VALUES_AGREE
        return out

    if verdict == ACME_NOT_COMPARABLE:
        out["required_action"] = (
            "不可比：先由 CARD-02 统一口径 / 币种 / 期间后再提交裁决；"
            "本卡不做跨口径推断"
        )
        out["rule_id"] = RULE_NOT_COMPARABLE
        return out

    winner, rule, note = arbitrate_conflict(a, b, tolerance=tolerance)
    out["rule_id"] = rule
    out["required_action"] = (
        f"已裁决：采用 {winner.ref}" if winner is not None else "未裁决：阻断该指标，交人工复核"
    )
    out["winner"] = winner.source_id if winner is not None else None
    out["winner_candidate"] = winner.to_dict() if winner is not None else None
    out["note"] = note
    return out


def _series_key(candidate: ConflictCandidate) -> Tuple[str, Optional[str], str]:
    """Outer grouping key — one *series* of comparable figures.

    Scope and currency are in the key because they define the measurement:
    合并营业收入 and 母公司营业收入 are two different quantities that legitimately
    coexist, and §7 is explicit that a differing 口径 is not a conflict. Version
    is *not* in the key, because two versions of one figure must meet in order
    for VERSION_SUPERSESSION to be detectable at all.

    Period is deliberately *not* in the key either: periods are the inner
    dimension, so that a new period appearing beside older ones can be reported
    as NEW_INFORMATION rather than vanishing into a separate bucket.
    """
    return (candidate.metric, candidate.scope, str(candidate.currency))


def _period_key(candidate: ConflictCandidate) -> str:
    return candidate.period_key


# ---------------------------------------------------------------------------
# Public API — §6
# ---------------------------------------------------------------------------


def resolve_conflicts(
    facts: Sequence[NormalizedFact],
    *,
    source_class_of: Optional[Callable[[NormalizedFact], str]] = None,
    source_class_by_document: Optional[Mapping[str, str]] = None,
    is_correction_of: Optional[Callable[[NormalizedFact], bool]] = None,
    extra_candidates: Sequence[ConflictCandidate] = (),
    company_of: Optional[Callable[[str], Optional[str]]] = None,
    tolerance: Optional[float] = None,
    resolver_version: str = RESOLVER_VERSION,
    occurred_at: Optional[str] = None,
    recorder: Any = None,
) -> Tuple[List[ConflictChoice], List[ConflictRecord]]:
    """Arbitrate every disagreement among ``facts``; return choices + records.

    ``source_class_of`` / ``source_class_by_document`` supply the precedence
    class; when neither can classify an origin the source is ``SOURCE_UNKNOWN``,
    which means it can never win an arbitration — the fail-closed direction.
    ``is_correction_of`` marks a fact as an explicit 更正公告 value (normally fed
    from CARD-04's ``prior_period_error_correction`` signal).

    ``company_of`` scopes grouping by company. When it is absent and the input
    spans several documents, the result carries a
    ``company_unverified_across_documents`` warning instead of pretending the
    grouping was verified.

    Nothing is dropped: every input fact appears in exactly one
    :class:`ConflictChoice`, either as the selection or among its candidates.
    """
    fact_list = list(facts)
    stamp = occurred_at or datetime.now(timezone.utc).isoformat()

    def _class_for(fact: NormalizedFact) -> str:
        if source_class_of is not None:
            return str(source_class_of(fact)) or SOURCE_UNKNOWN
        if source_class_by_document is not None:
            found = source_class_by_document.get(fact.document_id)
            if found:
                return str(found)
        return SOURCE_UNKNOWN

    warnings: List[str] = []

    candidates: List[ConflictCandidate] = [
        candidate_from_fact(
            fact,
            source_class=_class_for(fact),
            is_explicit_correction=bool(is_correction_of(fact)) if is_correction_of else False,
        )
        for fact in fact_list
    ]
    candidates.extend(extra_candidates)

    # Counted *after* the extra candidates are added: a correction announcement
    # supplied by the caller carries its own source id, and omitting it would let
    # a cross-document grouping pass as verified when it never was.
    sources = {candidate.source_id for candidate in candidates if candidate.source_id}
    if company_of is None and len(sources) > 1:
        warnings.append(
            "company_unverified_across_documents：未提供 company_of，"
            f"{len(sources)} 个来源被当作同一公司分组，请核验后再使用裁决结果。"
        )

    # Two levels. Outer = (company, metric, scope, currency): figures that are the
    # same *kind* of number. Inner = period. That split is what lets a group say
    # both "these two 900,000/1,000,000 disclosures are versions of one figure"
    # and "2025Q1 arrived later and supersedes nothing" (§7b).
    series: Dict[Tuple[Any, ...], List[ConflictCandidate]] = {}
    for candidate in candidates:
        company = None
        if company_of is not None:
            company = company_of(candidate.source_id)
        series.setdefault(
            (company,) + _series_key(candidate), []
        ).append(candidate)

    choices: List[ConflictChoice] = []
    records: List[ConflictRecord] = []

    for key in sorted(series, key=lambda item: tuple(str(part) for part in item)):
        members = sorted(series[key], key=lambda item: item.candidate_id)
        by_period: Dict[str, List[ConflictCandidate]] = {}
        for candidate in members:
            by_period.setdefault(_period_key(candidate), []).append(candidate)

        period_choices: List[ConflictChoice] = []
        for period in sorted(by_period):
            choice, period_records = _resolve_group(
                sorted(by_period[period], key=lambda item: item.candidate_id),
                tolerance=tolerance,
                resolver_version=resolver_version,
                timestamp=stamp,
            )
            period_choices.append(choice)
            records.extend(period_records)

        # Several periods present for one series means new information arrived.
        # §7b: history is *not* overwritten — the downstream calculation goes
        # stale and its claims need a refresh, which is a different rule from
        # supersession and must be recorded as such.
        if len(period_choices) > 1:
            ordered = sorted(period_choices, key=lambda item: item.period_key)
            newest = ordered[-1]
            oldest = ordered[0]
            anchor = newest.selected or (newest.candidates[0] if newest.candidates else None)
            prior = oldest.selected or (oldest.candidates[0] if oldest.candidates else None)
            if anchor is not None and prior is not None:
                # Orientation matters: the record is *about the new period*, so
                # ``a`` is the newly arrived disclosure and the record's
                # ``period_key`` names the period that just appeared. Swapping
                # these would file a "new quarter arrived" event under the old
                # quarter and make the audit trail read backwards.
                record = _record(
                    anchor,
                    prior,
                    CHANGE_NEW_INFORMATION,
                    rule=RULE_NEW_PERIOD,
                    note=(
                        f"新增期间 {newest.period_key} 的数据（原 {oldest.period_key}）；"
                        "无替代关系，不覆盖历史期间，下游计算按 stale 处理并置 needs_refresh"
                    ),
                    selected_source=anchor.source_id,
                    status=STATUS_RESOLVED,
                    selected_value=anchor.value,
                    resolver_version=resolver_version,
                    timestamp=stamp,
                )
                records.append(record)
                period_choices = [
                    replace(
                        choice,
                        conflict_ids=choice.conflict_ids + (record.conflict_id,),
                    )
                    for choice in period_choices
                ]

        choices.extend(period_choices)

    if recorder is not None:
        for record in records:
            recorder.emit(
                "validation",
                CONFLICT_EVENT,
                input_={
                    "metric": record.metric,
                    "period_key": record.period_key,
                    "scope": record.scope,
                },
                output=record.to_dict(),
                status="ok" if record.resolved else "warn",
                detail={"rule_id": record.reason},
            )

    # The card's §9 red line, asserted on every call rather than only in tests.
    assert_no_silent_override(choices, records)

    if warnings:
        choices = [
            replace(choice, warnings=choice.warnings + tuple(warnings)) for choice in choices
        ]
    return choices, records


CONFLICT_EVENT = "source_conflict_resolved"


def _resolve_group(
    group: Sequence[ConflictCandidate],
    *,
    tolerance: Optional[float],
    resolver_version: str,
    timestamp: str,
) -> Tuple[ConflictChoice, List[ConflictRecord]]:
    """Resolve one (metric, period, scope, currency) bucket.

    Every candidate here shares metric, period, 口径 and currency by construction,
    so there is exactly one quantity under discussion and the only question is
    which source's version of it stands. Three outcomes (§7b):

    * **agreement** — everyone states the same number; nothing to arbitrate.
    * **VERSION_SUPERSESSION** — one or more candidates explicitly replace
      others; the newest link of the chain wins and every earlier link is kept as
      ``superseded`` rather than erased.
    * **DATA_CONFLICT** — no version relation, so precedence decides; a tie at the
      top rank is left ``UNRESOLVED`` because §9 prefers a blocked metric to a
      guessed one.
    """
    assert group, "empty group"
    head = group[0]
    records: List[ConflictRecord] = []

    if len(group) == 1:
        return (
            ConflictChoice(
                metric=head.metric,
                period_key=head.period_key,
                scope=head.scope,
                currency=str(head.currency),
                value=head.value,
                change_kind=_single_change_kind(head),
                status=STATUS_RESOLVED,
                selected=head,
                candidates=tuple(group),
            ),
            records,
        )

    # A group whose candidates all come from *one* document is not a cross-source
    # conflict: CARD-02's projection already ranks a filing's own tables (audited
    # statement body over 主要会计数据 summary). CARD-13 records that decision
    # rather than re-deciding it, so the two cards cannot disagree.
    if len({candidate.source_id for candidate in group}) == 1:
        return _resolve_intra_document(
            group, resolver_version=resolver_version, timestamp=timestamp
        )

    # Pairwise over the whole group rather than against a pivot: the pivot
    # shortcut mis-attributes supersession as soon as there are three versions of
    # one figure (§8 edge case 1), and these groups are small.
    pair_verdicts: List[
        Tuple[ConflictCandidate, ConflictCandidate, str, str, str, ConflictCandidate]
    ] = []
    for index, left in enumerate(group):
        for right in group[index + 1 :]:
            verdict, verdict_reason = classify_cross_source(left, right, tolerance=tolerance)
            change_kind, _change_reason = classify_change(left, right, tolerance=tolerance)
            _older, newer = _order_by_recency(left, right)
            pair_verdicts.append((left, right, verdict, verdict_reason, change_kind, newer))

    # -- VERSION_SUPERSESSION -------------------------------------------------
    superseding = [item for item in pair_verdicts if item[4] == CHANGE_VERSION_SUPERSESSION]
    if superseding:
        candidates_up_for_selection = {item[5].candidate_id: item[5] for item in superseding}
        winner = max(
            candidates_up_for_selection.values(),
            key=lambda candidate: (
                version_rank(candidate.version),
                candidate.available_at or "",
                candidate.candidate_id,
            ),
        )
        losers = [candidate for candidate in group if candidate.candidate_id != winner.candidate_id]
        # §8 edge case 1: a chain of corrections resolves to its latest link, and
        # every intermediate link still gets its own record.
        ordered_losers = sorted(
            losers,
            key=lambda candidate: (
                version_rank(candidate.version),
                candidate.available_at or "",
                candidate.candidate_id,
            ),
        )
        for loser in ordered_losers:
            records.append(
                _record(
                    loser,
                    winner,
                    CHANGE_VERSION_SUPERSESSION,
                    rule=RULE_REGULATORY_CORRECTION
                    if winner.is_explicit_correction
                    else RULE_VERSION_SUPERSESSION,
                    note=(
                        f"{loser.version.value} 被 {winner.version.value} 替代"
                        f"（{winner.ref} 取代 {loser.ref}）；"
                        "旧值保留为 superseded，下游按 stale/blocked 传播"
                    ),
                    selected_source=winner.source_id,
                    status=STATUS_RESOLVED,
                    selected_value=winner.value,
                    superseded_source=loser.source_id,
                    resolver_version=resolver_version,
                    timestamp=timestamp,
                )
            )
        return (
            ConflictChoice(
                metric=winner.metric,
                period_key=winner.period_key,
                scope=winner.scope,
                currency=str(winner.currency),
                value=winner.value,
                change_kind=CHANGE_VERSION_SUPERSESSION,
                status=STATUS_RESOLVED,
                selected=winner,
                candidates=tuple(group),
                conflict_ids=tuple(record.conflict_id for record in records),
            ),
            records,
        )

    # -- plain agreement ------------------------------------------------------
    conflicting = [item for item in pair_verdicts if item[2] == ACME_CONFLICT]
    if not conflicting:
        selected = min(group, key=lambda item: item.candidate_id)
        record = _record(
            group[0],
            group[1],
            CHANGE_DATA_CONFLICT,
            rule=RULE_VALUES_AGREE,
            note="全部候选取值一致（含舍入容差），无冲突",
            selected_source=selected.source_id,
            status=STATUS_RESOLVED,
            selected_value=selected.value,
            resolver_version=resolver_version,
            timestamp=timestamp,
        )
        records.append(record)
        return (
            ConflictChoice(
                metric=selected.metric,
                period_key=selected.period_key,
                scope=selected.scope,
                currency=str(selected.currency),
                value=selected.value,
                change_kind=CHANGE_DATA_CONFLICT,
                status=STATUS_RESOLVED,
                selected=selected,
                candidates=tuple(group),
                conflict_ids=(record.conflict_id,),
            ),
            records,
        )

    # -- DATA_CONFLICT --------------------------------------------------------
    # §7 is a *precedence* rule, so the decision is made once for the group: the
    # highest source class present wins, provided it is strictly highest and every
    # candidate at that rank states the same number. Anything else — a tie at the
    # top, or an unclassified source sitting at the top — is unresolved (§9).
    known = [candidate for candidate in group if candidate.rank is not None]
    unknown = [candidate for candidate in group if candidate.rank is None]
    if not known:
        return _unresolved_group(
            group,
            head,
            records,
            rule=RULE_UNKNOWN_SOURCE,
            note="组内所有来源类别均未判定，按 §9 不裁决，交人工复核",
            resolver_version=resolver_version,
            timestamp=timestamp,
        )

    best_rank = max(candidate.rank for candidate in known)
    top = [candidate for candidate in known if candidate.rank == best_rank]
    top_disagrees = any(
        not values_agree(top[0].value, other.value, tolerance=tolerance) for other in top[1:]
    )
    if top_disagrees:
        return _unresolved_group(
            group,
            head,
            records,
            rule=RULE_TIE_UNRESOLVED,
            note=(
                f"最高优先级来源类别 {SOURCE_CLASS_LABELS.get(top[0].source_class, top[0].source_class)}"
                f"存在 {len(top)} 个来源且取值不一致，优先级相同无法裁决"
            ),
            resolver_version=resolver_version,
            timestamp=timestamp,
        )

    winner = min(top, key=lambda item: item.candidate_id)
    for other in group:
        if other.candidate_id == winner.candidate_id:
            continue
        if other.rank is None:
            rule = RULE_SOURCE_PRECEDENCE
            note = (
                f"来源类别未知的一方（{other.ref}）不得胜出，采用已知类别来源"
                f"（{winner.source_class}，{winner.ref}）"
            )
        else:
            rule = RULE_SOURCE_PRECEDENCE
            note = (
                f"来源优先级：{winner.source_class}({winner.rank}) > "
                f"{other.source_class}({other.rank})，采用 {winner.ref}"
            )
        records.append(
            _record(
                other,
                winner,
                CHANGE_DATA_CONFLICT,
                rule=rule,
                note=note,
                selected_source=winner.source_id,
                status=STATUS_RESOLVED,
                selected_value=winner.value,
                resolver_version=resolver_version,
                timestamp=timestamp,
            )
        )
    return (
        ConflictChoice(
            metric=winner.metric,
            period_key=winner.period_key,
            scope=winner.scope,
            currency=str(winner.currency),
            value=winner.value,
            change_kind=CHANGE_DATA_CONFLICT,
            status=STATUS_RESOLVED,
            selected=winner,
            candidates=tuple(group),
            conflict_ids=tuple(record.conflict_id for record in records),
        ),
        records,
    )


def _unresolved_group(
    group: Sequence[ConflictCandidate],
    head: ConflictCandidate,
    records: List[ConflictRecord],
    *,
    rule: str,
    note: str,
    resolver_version: str,
    timestamp: str,
) -> Tuple[ConflictChoice, List[ConflictRecord]]:
    """§9 fail-closed: record why, select nothing, block the metric.

    Every candidate in the group gets a record, not just the two that tied, so
    ``assert_no_silent_override`` can still prove nothing disappeared and the
    demo's blocking table can show the whole contested set.
    """
    for other in group[1:]:
        records.append(
            _record(
                head,
                other,
                CHANGE_DATA_CONFLICT,
                rule=rule,
                note=note if other is group[1] else f"{note}（同组，一并阻断）",
                selected_source=None,
                status=STATUS_UNRESOLVED,
                resolver_version=resolver_version,
                timestamp=timestamp,
            )
        )
    return (
        ConflictChoice(
            metric=head.metric,
            period_key=head.period_key,
            scope=head.scope,
            currency=str(head.currency),
            value=None,
            change_kind=CHANGE_DATA_CONFLICT,
            status=STATUS_UNRESOLVED,
            selected=None,
            candidates=tuple(group),
            conflict_ids=tuple(record.conflict_id for record in records),
            warnings=(f"unresolved：{note}",),
        ),
        records,
    )


def _single_change_kind(candidate: ConflictCandidate) -> str:
    """A lone candidate has no counterpart, so it is NEW_INFORMATION by default
    — unless it is itself a correction value, which is a supersession of a value
    we do not hold."""
    return CHANGE_VERSION_SUPERSESSION if candidate.is_explicit_correction else CHANGE_NEW_INFORMATION


def _resolve_intra_document(
    group: Sequence[ConflictCandidate],
    *,
    resolver_version: str,
    timestamp: str,
) -> Tuple[ConflictChoice, List[ConflictRecord]]:
    """Record CARD-02's own table precedence for one document's rivals.

    This is the *only* place CARD-13 accepts a resolution it did not reach
    itself, and it is not deferring to a heuristic: ``primary_sort_key`` is
    CARD-02 §7.1's stated order (audited statement body → 主要会计数据 summary →
    unidentified table), already applied by the projection this fact set came
    from. Re-deciding it here would let two cards disagree about which table
    speaks for a company.
    """
    ordered = sorted(
        group,
        key=lambda item: (
            primary_sort_key(item.fact) if item.fact is not None else (2, item.page or 0, 0, 0),
            item.candidate_id,
        ),
    )
    head = ordered[0]
    records: List[ConflictRecord] = []

    if all(values_agree(head.value, other.value) for other in ordered[1:]):
        record = _record(
            head,
            ordered[1],
            CHANGE_DATA_CONFLICT,
            rule=RULE_VALUES_AGREE,
            note="同文档内多处披露取值一致（含舍入容差），无冲突",
            selected_source=head.source_id,
            status=STATUS_RESOLVED,
            selected_value=head.value,
            resolver_version=resolver_version,
            timestamp=timestamp,
        )
        records.append(record)
        return (
            ConflictChoice(
                metric=head.metric,
                period_key=head.period_key,
                scope=head.scope,
                currency=str(head.currency),
                value=head.value,
                change_kind=CHANGE_DATA_CONFLICT,
                status=STATUS_RESOLVED,
                selected=head,
                candidates=tuple(group),
                conflict_ids=(record.conflict_id,),
            ),
            records,
        )

    for other in ordered[1:]:
        records.append(
            _record(
                other,
                head,
                CHANGE_DATA_CONFLICT,
                rule=RULE_INTRA_DOCUMENT_PRECEDENCE,
                note=(
                    f"同文档内 {other.ref} 与 {head.ref} 取值不一致；"
                    f"采用 {head.ref}（CARD-02 表格优先级），"
                    "该差异属同文档内优先级而非跨来源冲突"
                ),
                selected_source=head.source_id,
                status=STATUS_RESOLVED,
                selected_value=head.value,
                resolver_version=resolver_version,
                timestamp=timestamp,
            )
        )
    return (
        ConflictChoice(
            metric=head.metric,
            period_key=head.period_key,
            scope=head.scope,
            currency=str(head.currency),
            value=head.value,
            change_kind=CHANGE_DATA_CONFLICT,
            status=STATUS_RESOLVED,
            selected=head,
            candidates=tuple(group),
            conflict_ids=tuple(record.conflict_id for record in records),
            warnings=("同文档内数值不一致已按 CARD-02 表格优先级处理并留痕",),
        ),
        records,
    )


def _record(
    a: ConflictCandidate,
    b: ConflictCandidate,
    change_kind: str,
    *,
    rule: str,
    note: str,
    selected_source: Optional[str],
    resolver_version: str,
    timestamp: str,
    status: str = STATUS_RESOLVED,
    selected_value: Optional[float] = None,
    superseded_source: Optional[str] = None,
) -> ConflictRecord:
    conflict_id = "CF" + _digest(
        a.candidate_id, b.candidate_id, change_kind, rule, round(a.value, 6), round(b.value, 6)
    )
    return ConflictRecord(
        conflict_id=conflict_id,
        change_kind=change_kind,
        source_a=a.source_id,
        source_b=b.source_id,
        selected_source=selected_source,
        reason=rule,
        resolver=resolver_version,
        timestamp=timestamp,
        metric=a.metric,
        period_key=a.period_key,
        scope=a.scope,
        currency=str(a.currency),
        source_class_a=a.source_class,
        source_class_b=b.source_class,
        candidate_a=a.candidate_id,
        candidate_b=b.candidate_id,
        value_a=a.value,
        value_b=b.value,
        selected_value=selected_value,
        superseded_source=superseded_source,
        status=status,
        ref_a=a.ref,
        ref_b=b.ref,
        note=note,
    )


# ---------------------------------------------------------------------------
# §6 second function + §9/§10 helpers
# ---------------------------------------------------------------------------


def unresolved_report(conflicts: Sequence[ConflictRecord]) -> List[Dict[str, Any]]:
    """One row per unresolved conflict, shaped for the demo's 阻断 table."""
    return [
        {
            "conflict_id": record.conflict_id,
            "metric": record.metric,
            "period_key": record.period_key,
            "scope": record.scope,
            "change_kind": record.change_kind,
            "sources": [record.source_a, record.source_b],
            "source_classes": [record.source_class_a, record.source_class_b],
            "values": [record.value_a, record.value_b],
            "refs": [record.ref_a, record.ref_b],
            "reason": record.reason,
            "note": record.note,
            "required_action": "人工复核或补齐可裁决来源后重新运行",
        }
        for record in conflicts
        if not record.resolved
    ]


def assert_no_silent_override(
    choices: Sequence[ConflictChoice], records: Sequence[ConflictRecord]
) -> None:
    """§9's red line, as a mechanical check.

    Every candidate must be either the *selection* of its group or named in at
    least one record as one of the two compared candidates. A candidate that
    simply vanished has been silently overridden, which is a bug rather than a
    judgement call — so this raises rather than warning.

    Checked at candidate granularity, not source granularity: one document can
    yield several candidates for the same key, and comparing only document ids
    would let a dropped fact hide behind its own sibling.
    """
    selected: Set[str] = set()
    compared: Set[str] = set()
    all_candidates: List[ConflictCandidate] = []
    for choice in choices:
        if choice.selected is not None:
            selected.add(choice.selected.candidate_id)
        all_candidates.extend(choice.candidates)
    for record in records:
        compared.add(record.candidate_a)
        compared.add(record.candidate_b)

    missing = [
        candidate.candidate_id
        for candidate in all_candidates
        if candidate.candidate_id not in selected and candidate.candidate_id not in compared
    ]
    if missing:
        raise ConflictError(
            "检测到静默覆盖：以下候选既未被选中、也未出现在任何 ConflictRecord 中 "
            f"→ {sorted(set(missing))}"
        )


def unresolved_metrics(conflicts: Sequence[ConflictRecord]) -> Set[str]:
    """Metric keys downstream must treat as blocked (§9, §10 rule 2)."""
    return {
        f"{record.metric}@{record.period_key}/{record.scope or '未知口径'}"
        for record in conflicts
        if not record.resolved
    }


def conflict_choices_for_metric(
    choices: Sequence[ConflictChoice],
    metric: str,
    period_key: Optional[str] = None,
    scope: Optional[str] = None,
) -> Optional[ConflictChoice]:
    """The choice for one metric, optionally narrowed by period / 口径.

    A convenience for callers that already know which figure they care about
    (the failure-injection scenarios, the workbench drill-down). Returns ``None``
    when the metric is absent — which is *not* the same as a resolved ``None``
    value, and the callers treat the difference accordingly.
    """
    matches = [choice for choice in choices if choice.metric == metric]
    if period_key is not None:
        matches = [choice for choice in matches if choice.period_key == period_key]
    if scope is not None:
        matches = [choice for choice in matches if (choice.scope or "") == scope]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.period_key)[-1]


def selected_by_metric(choices: Sequence[ConflictChoice]) -> Dict[str, ConflictChoice]:
    """Latest choice per metric key, for the downstream fact-claim gate."""
    return {choice.key: choice for choice in choices}


def summarize(records: Sequence[ConflictRecord]) -> Dict[str, Any]:
    """Counts + rule histogram — the demo's "为什么选这个数" panel."""
    by_kind: Dict[str, int] = {}
    by_rule: Dict[str, int] = {}
    for record in records:
        by_kind[record.change_kind] = by_kind.get(record.change_kind, 0) + 1
        by_rule[record.reason] = by_rule.get(record.reason, 0) + 1
    return {
        "total": len(records),
        "resolved": sum(1 for record in records if record.resolved),
        "unresolved": sum(1 for record in records if not record.resolved),
        "by_change_kind": by_kind,
        "by_rule": by_rule,
        "resolver": RESOLVER_VERSION,
    }


# ---------------------------------------------------------------------------
# Correction pairs read out of prose
#
# A 更正公告 usually states the corrected figure in a sentence rather than in a
# table, so CARD-02 has nothing to normalise and the corrected value would be
# invisible. The tokens are taken from the shared lexicon so a 更正前 phrasing can
# never be recognised by one card and missed by the other.
# ---------------------------------------------------------------------------

#: 「发布日期：2026-06-12」— the announcement's own statement of when it was made.
_DATE = re.compile(r"(?:发布日期|披露日期|公告日期|日期)\s*[：:]\s*(20\d{2}-\d{2}-\d{2})")

_AS_REPORTED_ANCHORS = tuple(
    pattern.pattern
    for pattern, token in VERSION_COLUMN_KEYWORDS
    if token == "as_reported"
)
_CORRECTED_ANCHORS = tuple(
    pattern.pattern for pattern, token in VERSION_COLUMN_KEYWORDS if token == "corrected"
)

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_UNITS = r"(?:亿元|百万元|万元|千元|元)"

#: Clause-level, deliberately. A single regex that jumps anchor → number →
#: anchor → number matches the *shortest* string, and the shortest string in
#: 「更正前2024年度营业收入为900,000元」starts at the number 2024 — the metric
#: label would come back empty and the pair would be silently lost. Splitting the
#: sentence at the anchors and then finding the last/first number inside each
#: clause is both correct and readable.
_CLAUSE_FORWARD = re.compile(
    r"(?P<before_anchor>" + "|".join(_AS_REPORTED_ANCHORS) + r")"
    r"(?P<before_clause>[^。；;]{1,80}?)"
    r"(?P<after_anchor>" + "|".join(_CORRECTED_ANCHORS) + r")"
    r"(?P<after_clause>[^。；;]{0,60})"
)

#: 「…更正后为 1,000,000 元（更正前 900,000 元）」— same pair, anchors reversed.
_CLAUSE_REVERSED = re.compile(
    r"(?P<before_anchor>" + "|".join(_CORRECTED_ANCHORS) + r")"
    r"(?P<before_clause>[^。；;]{1,80}?)"
    r"(?P<after_anchor>" + "|".join(_AS_REPORTED_ANCHORS) + r")"
    r"(?P<after_clause>[^。；;]{0,60})"
)

#: 「2024年度」/「2024年半年度」/「2024年第一季度」 as written in prose.
#:
#: ``度`` is spelled out as its own alternative because ``\s*年\s*`` already
#: consumes the 年 of 年度; without it the alternation would be asked to match
#: "年度" against the remaining "度" and quietly fall through to "no period".
_METRIC_PERIOD = re.compile(
    r"^(?P<year>20\d{2})\s*年\s*"
    r"(?P<kind>半年度|中期|第一季度|第二季度|第三季度|第四季度|度|年度|年报)?"
)

#: 「本公司对 2024 年度财务报表进行追溯重述」— a document-level period hint.
_CORRECTION_TARGET = re.compile(
    r"对\s*(?P<year>20\d{2})\s*年\s*"
    r"(?P<kind>半年度|中期|第一季度|第二季度|第三季度|第四季度|度|年度)"
    r"\s*(?:财务)?报表[^。；;]{0,20}?(?:追溯)?(?:重述|更正|调整)"
)

#: Connector words that sit between a metric label and its value.
_VALUE_CONNECTORS = re.compile(r"(?:为|是|系|：|:|=|\s)+$")


def _canonical_from_parts(year: str, kind: Optional[str]) -> Optional[str]:
    r"""Build a period key from prose parts, or ``None`` — never a guess.

    ``度`` is the residue of 年度 once ``\s*年\s*`` has taken the 年, so it is an
    alias for the annual token rather than an unrecognised one.
    """
    if kind in (None, "", "年度", "年报", "度"):
        token = f"{year}年度"
    else:
        token = f"{year}年{kind}"
    try:
        period = canonical_period(token)
    except Exception:
        return None
    return period if is_supported_period_key(period) else None


def _split_amount(clause: str, *, take_last: bool) -> Optional[Tuple[str, str, Optional[str], int]]:
    """Find the value inside one clause.

    Returns ``(label, raw_number, unit_or_None, end_index)``. ``take_last`` picks
    which number is the value: ``True`` inside a 更正前 clause, because the period
    ("2024") is itself a number and the *amount* is the rightmost one;
    ``False`` inside a 更正后 clause, where the amount comes first.
    """
    numbers = list(re.finditer(_NUMBER, clause))
    if not numbers:
        return None
    chosen = numbers[-1] if take_last else numbers[0]
    label = clause[: chosen.start()]
    label = _METRIC_PERIOD.sub("", label.strip())
    label = _VALUE_CONNECTORS.sub("", label.strip())
    tail = clause[chosen.end() :]
    unit_match = re.match(r"\s*" + _UNITS, tail)
    unit = unit_match.group(0).strip() if unit_match else None
    return label, chosen.group(0), unit, chosen.end()


def _text_stream(facts: Sequence[Any]) -> Tuple[str, List[Tuple[int, int, Any]]]:
    """Concatenate a document's text facts (per page, in order) into one string.

    A correction sentence is routinely split across two adjacent paragraph facts
    ("…更正前营业收入为900,000元，更正后" + "为1,000,000元。"), so scanning fact
    by fact would miss every real pair. Joining with no separator keeps a number
    split across the seam intact. Returns the text plus an index of
    ``(start, end, fact)`` spans so a match can be mapped back to the page/bbox
    of the fact that actually contains it.
    """
    spans: List[Tuple[int, int, Any]] = []
    chunks: List[str] = []
    cursor = 0
    for fact in facts:
        text = getattr(fact, "raw_text", None)
        if not text or getattr(fact, "kind", "") != "text":
            continue
        chunks.append(text)
        spans.append((cursor, cursor + len(text), fact))
        cursor += len(text)
    return "".join(chunks), spans


def _locate(spans: Sequence[Tuple[int, int, Any]], index: int) -> Any:
    for start, end, fact in spans:
        if start <= index < end:
            return fact
    return spans[-1][2] if spans else None


def _resolve_period_candidates(text: str) -> List[Tuple[int, str]]:
    found: List[Tuple[int, str]] = []
    for match in _CORRECTION_TARGET.finditer(text):
        period = _canonical_from_parts(match.group("year"), match.group("kind"))
        if period is not None:
            found.append((match.start(), period))
    return found


def correction_pairs(
    facts: Sequence[Any],
    *,
    currency: str = "CNY",
    scope: Optional[str] = None,
    published_at: Optional[str] = None,
) -> Tuple[List[CorrectionPair], List[str]]:
    """Read 更正前/更正后 value pairs out of a document's prose.

    Returns ``(pairs, warnings)``. Every uncertain step fails closed into a
    warning rather than a candidate: no unit anywhere in the sentence → no
    candidate; no period stated or derivable → no candidate. ``scope`` is the
    口径 the caller declares for the corrected figure (``None`` = undeclared, and
    an undeclared scope stays undeclared so the pair cannot silently join a group
    it was never shown to belong to). ``published_at`` is the announcement's
    publication date — normally CARD-01's ``DocumentRecord.publication_date``;
    when omitted it is read from the document's own 发布日期 line, and when that
    is absent the pair simply has no timestamp.

    The *only* value inheritance performed is the sentence's own unit (a value in
    the same sentence as an explicit ``元`` is not guessing about scale), and it
    is recorded as ``unit_source="sentence"`` in the pair so a reviewer can see it.
    """
    text, spans = _text_stream(facts)
    warnings: List[str] = []
    if not text:
        return [], ["correction_no_text_facts：文档没有可扫描的文本段落"]

    stamp = published_at or _publication_date(text, facts)
    if stamp is None:
        warnings.append(
            "correction_publication_date_missing：未取到更正公告发布日期，"
            "该更正值在按时间排序时无时间戳（版本仍可用于替代判定）"
        )

    period_hints = _resolve_period_candidates(text)
    pairs: List[CorrectionPair] = []
    seen: Set[Tuple[str, str, str]] = set()

    for match, forward in _iter_pair_matches(text):
        before_clause = match.group("before_clause") or ""
        after_clause = match.group("after_clause") or ""
        # Two things move together with the anchor order, and mixing them is the
        # easy bug: the *as-reported* clause always holds the **last** number
        # (the period "2024" is itself a number), while the *corrected* clause
        # holds the **first**; and it is the as-reported clause that carries the
        # metric label. So resolve the roles once, then read each side by role.
        as_reported_clause, corrected_clause = (
            (before_clause, after_clause) if forward else (after_clause, before_clause)
        )
        as_reported_side = _split_amount(as_reported_clause, take_last=True)
        corrected_side = _split_amount(corrected_clause, take_last=False)
        if as_reported_side is None or corrected_side is None:
            warnings.append(
                f"correction_value_missing：『{match.group(0).strip()[:60]}』"
                "一侧缺少可解析数值，跳过该更正对"
            )
            continue

        as_reported_label, before_raw, before_unit, _ = as_reported_side
        corrected_label, after_raw, after_unit, _ = corrected_side
        # Either clause may name the metric; the as-reported one normally does.
        label = as_reported_label or corrected_label

        metric_match = resolve_metric(label)
        if metric_match.spec is None:
            warnings.append(
                f"correction_metric_unknown：『{label[:40]}』未匹配到已知指标，跳过该更正对"
            )
            continue

        unit = before_unit or after_unit
        if not unit:
            warnings.append(
                f"correction_unit_missing：『{match.group(0).strip()[:60]}』未声明单位，"
                "拒绝按元处理（零静默）"
            )
            continue
        unit_source = "explicit" if (before_unit and after_unit) else "sentence"
        if unit_source == "sentence":
            warnings.append(
                f"correction_unit_inherited：更正对只有一侧声明单位，按同句 {unit} 处理并留痕"
            )

        try:
            before_value = normalize_unit(before_raw, unit)
            after_value = normalize_unit(after_raw, unit)
        except UnitNormalizationError as exc:  # pragma: no cover - guarded above
            warnings.append(f"correction_unit_error：{exc}")
            continue

        period_key = _period_for(
            as_reported_clause or corrected_clause, match.start(), period_hints
        )
        if period_key is None:
            warnings.append(
                f"correction_period_undetermined：『{match.group(0).strip()[:60]}』"
                "未声明期间且文档未给出追溯重述期间，跳过（禁止猜测）"
            )
            continue

        fingerprint = (metric_match.spec.key, period_key, f"{before_raw}->{after_raw}")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        anchor_fact = _locate(spans, match.start())
        quote = text[max(0, match.start() - 30) : match.end() + 10]
        pairs.append(
            CorrectionPair(
                metric=metric_match.spec.key,
                period_key=period_key,
                before_value=before_value,
                after_value=after_value,
                before_raw=before_raw,
                after_raw=after_raw,
                unit=unit,
                unit_source=unit_source,
                currency=currency,
                quote=quote,
                page=getattr(anchor_fact, "page", None),
                bbox=getattr(anchor_fact, "bbox", None),
                source_fact_id=getattr(anchor_fact, "fact_id", None),
                scope=scope,
                available_at=stamp,
                scope_source="caller" if scope else "undeclared",
            )
        )
    return pairs, warnings


def _publication_date(text: str, facts: Sequence[Any]) -> Optional[str]:
    """The announcement's publication date, from its own 发布日期 line or a fact.

    CARD-01 already parses this onto the ``DocumentRecord``; this is the fallback
    for callers that only hold the fact list. Returns ``None`` rather than today's
    date — a correction stamped with the run's clock would look newer than
    everything forever after.
    """
    for fact in facts:
        meta = getattr(fact, "meta", None) or {}
        if isinstance(meta, dict) and meta.get("role") == "publication_date":
            found = _DATE.search(str(getattr(fact, "raw_text", "") or ""))
            if found:
                return found.group(1)
    found = _DATE.search(text)
    return found.group(1) if found else None


def _iter_pair_matches(text: str) -> List[Tuple[Any, bool]]:
    """All 更正前/更正后 clause pairs in ``text``, either anchor order.

    Overlaps are dropped by keeping the earliest, longest match: a sentence
    stating the pair twice must not become two corrections for the same figure.
    """
    found: List[Tuple[int, int, Any, bool]] = []
    for pattern, forward in ((_CLAUSE_FORWARD, True), (_CLAUSE_REVERSED, False)):
        for match in pattern.finditer(text):
            found.append((match.start(), match.end(), match, forward))
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    out: List[Tuple[Any, bool]] = []
    consumed = -1
    for start, end, match, forward in found:
        if start < consumed:
            continue
        consumed = end
        out.append((match, forward))
    return out


def _period_for(
    before_clause: str, index: int, hints: Sequence[Tuple[int, str]]
) -> Optional[str]:
    """The period a correction pair belongs to, or ``None``.

    Prefers a period stated inside the pair's own clause; falls back to the
    document's declared 追溯重述 target. Never inferred from position alone.
    """
    direct = _METRIC_PERIOD.search(before_clause.strip())
    if direct is not None:
        period = _canonical_from_parts(direct.group("year"), direct.group("kind"))
        if period is not None:
            return period
    if hints:
        return min(hints, key=lambda item: abs(item[0] - index))[1]
    return None


def correction_candidates(
    pairs: Sequence[CorrectionPair],
    *,
    source_id: str,
    source_class: str = SOURCE_REGULATORY_CORRECTION,
) -> List[ConflictCandidate]:
    """Turn correction pairs into the highest-precedence candidate of a pair.

    ``scope`` travels from the pair (i.e. from the caller's declaration), never
    from this function: a correction with an undeclared 口径 lands in its own
    group rather than being quietly filed alongside 合并 figures.
    """
    out: List[ConflictCandidate] = []
    for pair in pairs:
        notes = [
            "value_from_prose：数值来自更正公告正文，非表格单元格",
            f"unit_source={pair.unit_source}",
        ]
        if pair.scope is None:
            notes.append("scope_undeclared：更正公告未声明口径，不并入已声明口径的分组")
        else:
            notes.append(f"scope_source={pair.scope_source}:{pair.scope}")
        out.append(
            ConflictCandidate(
                candidate_id="TC" + _digest(source_id, pair.metric, pair.period_key, pair.quote),
                source_id=source_id,
                source_class=source_class,
                metric=pair.metric,
                period_key=pair.period_key,
                scope=pair.scope,
                currency=pair.currency,
                value=pair.after_value,
                version=Version.CORRECTED,
                available_at=pair.available_at,
                is_explicit_correction=True,
                ref=f"更正公告第{pair.page}页" if pair.page is not None else "更正公告",
                quote=pair.quote,
                page=pair.page,
                bbox=pair.bbox,
                fact=None,
                warnings=tuple(notes),
            )
        )
    return out


__all__ = [
    "ABS_TOLERANCE",
    "ACME_CONFLICT",
    "ACME_MATCH",
    "ACME_NOT_COMPARABLE",
    "ACME_ROUNDING_MATCH",
    "ACME_VERDICTS",
    "ACME_VERDICT_LABELS",
    "CHANGE_DATA_CONFLICT",
    "CHANGE_KINDS",
    "CHANGE_KIND_LABELS",
    "CHANGE_NEW_INFORMATION",
    "CHANGE_VERSION_SUPERSESSION",
    "CONFLICT_EVENT",
    "ConflictCandidate",
    "ConflictChoice",
    "ConflictError",
    "ConflictRecord",
    "CorrectionPair",
    "DOCUMENT_TYPE_SOURCE_CLASS",
    "REL_TOLERANCE",
    "RESOLVER_VERSION",
    "RULE_IDS",
    "RULE_NOT_COMPARABLE",
    "RULE_NEW_PERIOD",
    "RULE_REGULATORY_CORRECTION",
    "RULE_SOURCE_PRECEDENCE",
    "RULE_TIE_UNRESOLVED",
    "RULE_UNKNOWN_SOURCE",
    "RULE_VALUES_AGREE",
    "RULE_VERSION_SUPERSESSION",
    "SOURCE_CLASS_LABELS",
    "SOURCE_COMPANY_ANNOUNCEMENT",
    "SOURCE_PRECEDENCE",
    "SOURCE_REGULATORY_CORRECTION",
    "SOURCE_REGULATORY_FILING",
    "SOURCE_THIRD_PARTY",
    "SOURCE_UNKNOWN",
    "STATUS_RESOLVED",
    "STATUS_SUPERSEDED",
    "STATUS_UNRESOLVED",
    "arbitrate_conflict",
    "assert_no_silent_override",
    "candidate_from_fact",
    "classify_change",
    "classify_cross_source",
    "compare_pair",
    "conflict_choices_for_metric",
    "correction_candidates",
    "correction_pairs",
    "default_source_class",
    "resolve_conflicts",
    "selected_by_metric",
    "summarize",
    "unresolved_metrics",
    "unresolved_report",
    "values_agree",
]
