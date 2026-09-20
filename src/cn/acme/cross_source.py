"""CARD-22 §7 F5 — Cross-source Constraints (年报 vs Snapshot Provider).

§7 F5 gives this family exactly two jobs and one prohibition:

* compare only **after** period / scope / version / currency / unit are unified;
* sort the outcome into ``MATCH | ROUNDING_MATCH | CONFLICT | NOT_COMPARABLE``;
* and **never** decide who is wrong — 裁决 is CARD-13's, and this module hands the
  pair over through CARD-13's own ``compare_pair`` rather than inventing a second
  notion of "these two numbers disagree".

That last point is why this module owns no comparison arithmetic at all: it
builds two :class:`~src.cn.conflict.ConflictCandidate` values and calls
CARD-13. A second implementation would be a second opinion, and the whole card
exists because the system may not hold two opinions about the same number.

**What does not happen here.** The tolerance is *not* widened to §7's 0.1% band.
Cross-source equality means "the same figure printed twice" (CARD-13 §8 edge 3),
so the tight band is the correct one: widening it would turn a real vendor-vs-
filing difference into a friendly ``ROUNDING_MATCH`` and rob CARD-13 of the case
it is supposed to arbitrate. §7's tolerance policy governs residual-based
identities and subtotals; the cross-source family is a *classification*, and its
classification rules already live in CARD-13.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..conflict import (
    ABS_TOLERANCE,
    ACME_CONFLICT,
    ACME_MATCH,
    ACME_NOT_COMPARABLE,
    ACME_ROUNDING_MATCH,
    ACME_VERDICT_LABELS,
    SOURCE_REGULATORY_FILING,
    SOURCE_THIRD_PARTY,
    ConflictCandidate,
    candidate_from_fact,
    compare_pair,
)
from ..normalization.models import NormalizedFact
from .models import (
    ConstraintOperand,
    ConstraintViolation,
    FAMILY_CROSS_SOURCE,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    STATUS_NOT_COMPARABLE,
    STATUS_PASSED,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_VIOLATED,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy

CONSTRAINT_CROSS_SOURCE_AGREEMENT = "cross_source_agreement"

#: §7 F5 verdict → CARD-22 check status. ``ROUNDING_MATCH`` is ``passed``: the
#: four-class vocabulary calls it 舍入内一致, i.e. an *agreement* class, and
#: routing it into ``review_queue`` would make the queue useless on day one.
VERDICT_STATUS: Dict[str, str] = {
    ACME_MATCH: STATUS_PASSED,
    ACME_ROUNDING_MATCH: STATUS_PASSED,
    ACME_CONFLICT: STATUS_VIOLATED,
    ACME_NOT_COMPARABLE: STATUS_NOT_COMPARABLE,
}


SeriesKey = Tuple[str, str, str, str]


def _series_key(fact: NormalizedFact) -> SeriesKey:
    return (
        fact.metric,
        fact.period_key,
        fact.scope.value if fact.scope else "",
        str(fact.currency),
    )


def _index(facts: Iterable[NormalizedFact]) -> Dict[SeriesKey, NormalizedFact]:
    out: Dict[SeriesKey, NormalizedFact] = {}
    for fact in facts:
        out.setdefault(_series_key(fact), fact)
    return out


def _measurement_key(key: SeriesKey) -> Tuple[str, str, str]:
    """``(metric, period, currency)`` — everything but 口径."""
    metric, period_key, _scope, currency = key
    return (metric, period_key, currency)


def _counterpart_under_another_scope(
    key: SeriesKey, other: Dict[SeriesKey, NormalizedFact]
) -> Optional[NormalizedFact]:
    """The same measurement declared under a *different* 口径, if there is one.

    This is what keeps F5 honest about §7's "在 period / scope / version /
    currency / unit 统一后比较". 合并营业收入 and 母公司营业收入 are the same
    *name* measured over two different populations: the filing did print a
    revenue figure, we simply cannot line the two up. Reporting that as
    "only one side had a value" would be a false statement about the document —
    and it would bury a real comparability blocker as if it were a data gap.
    """
    wanted = _measurement_key(key)
    matches = [
        fact
        for fact in other.values()
        if _measurement_key(_series_key(fact)) == wanted
        and (_series_key(fact)[2] != key[2])
    ]
    if not matches:
        return None
    # Deterministic pick when several 口径 exist: 合并 before other scopes.
    return min(matches, key=lambda f: (_series_key(f)[2], f.normalized_fact_id))


def _parse_candidate(
    fact: NormalizedFact,
    *,
    source_class: str,
    source_id: Optional[str],
    ref: str,
) -> ConflictCandidate:
    candidate = candidate_from_fact(fact, source_class=source_class, ref=ref)
    if source_id is None:
        return candidate
    return replace(candidate, source_id=source_id)


def _operand(fact: NormalizedFact, *, role: str, source_class: str) -> ConstraintOperand:
    return ConstraintOperand(
        ref=fact.normalized_fact_id,
        value=fact.value,
        period=fact.period_key,
        scope=fact.scope.value if fact.scope else None,
        version=fact.version.value,
        unit="元",
        currency=str(fact.currency),
        label=f"{fact.metric}（{role}/{source_class}）",
    )


def _source_ref(fact: NormalizedFact, *, snapshot_id: Optional[str], role: str) -> SourceRef:
    if snapshot_id:
        return SourceRef(
            snapshot_id=snapshot_id,
            available_at=fact.available_at or "",
            locator=f"{role} {fact.metric} {fact.period_key}",
        )
    return SourceRef(
        document_id=fact.document_id or None,
        page=fact.page,
        bbox=fact.bbox,
        locator=f"{role} {fact.source_table or '未知表'} r{fact.row}c{fact.column}"
        if fact.row is not None
        else f"{role} {fact.source_table or ''}".strip(),
    )


def _pair_violation(
    left: NormalizedFact,
    right: NormalizedFact,
    *,
    key: SeriesKey,
    snapshot_id: Optional[str],
    document_source_class: str,
    snapshot_source_class: str,
    document_label: str,
    snapshot_label: str,
    detected_at: str,
) -> ConstraintViolation:
    """One paired comparison, decided by CARD-13 and translated to a status."""
    metric, period_key, scope, currency = key
    a = _parse_candidate(
        left, source_class=document_source_class, source_id=None,
        ref=f"{document_label} {left.source_table or ''}".strip(),
    )
    b = _parse_candidate(
        right, source_class=snapshot_source_class, source_id=snapshot_id or right.document_id,
        ref=f"{snapshot_label} {snapshot_id or right.document_id}",
    )
    verdict = compare_pair(a, b)
    verdict_id = str(verdict["verdict"])
    status = VERDICT_STATUS.get(verdict_id, STATUS_NOT_COMPARABLE)
    # The canonical four-class token leads the note: §7's vocabulary *is* the
    # interface, so a reader (or the demo table) can filter on it without
    # parsing Chinese prose.
    note = (
        f"[{verdict_id}] "
        f"{ACME_VERDICT_LABELS.get(verdict_id, verdict_id)}："
        f"{verdict['verdict_reason']}；{verdict['required_action']}"
    )
    if status == STATUS_VIOLATED:
        note += "（ACME 只标记，裁决与取值由 CARD-13 完成，本卡不选边）"
    return ConstraintViolation(
        violation_id=make_violation_id(
            CONSTRAINT_CROSS_SOURCE_AGREEMENT, FAMILY_CROSS_SOURCE,
            (metric, period_key, scope, currency, verdict_id),
        ),
        constraint_family=FAMILY_CROSS_SOURCE,
        constraint_id=CONSTRAINT_CROSS_SOURCE_AGREEMENT,
        inputs=(
            _operand(left, role=document_label, source_class=document_source_class),
            _operand(right, role=snapshot_label, source_class=snapshot_source_class),
        ),
        # 惯例：expected = 更高优先级来源（监管披露），actual = 被比对来源。
        expected_value=left.value,
        actual_value=right.value,
        residual=left.value - right.value,
        tolerance=ABS_TOLERANCE,
        status=status,
        source_refs=(
            _source_ref(left, snapshot_id=None, role=document_label),
            _source_ref(right, snapshot_id=snapshot_id, role=snapshot_label),
        ),
        severity=SEVERITY_ERROR if status == STATUS_VIOLATED else SEVERITY_WARNING,
        detected_at=detected_at,
        note=note,
    )


def _one_sided_violation(
    present: NormalizedFact,
    *,
    key: SeriesKey,
    side: str,
    snapshot_id: Optional[str],
    present_is_document: bool,
    detected_at: str,
) -> ConstraintViolation:
    metric, period_key, scope, currency = key
    return ConstraintViolation(
        violation_id=make_violation_id(
            CONSTRAINT_CROSS_SOURCE_AGREEMENT, FAMILY_CROSS_SOURCE,
            (metric, period_key, scope, currency, side),
        ),
        constraint_family=FAMILY_CROSS_SOURCE,
        constraint_id=CONSTRAINT_CROSS_SOURCE_AGREEMENT,
        inputs=(_operand(present, role="单侧", source_class=""),),
        expected_value=None,
        actual_value=None,
        residual=None,
        tolerance=0.0,
        status=STATUS_SKIPPED_MISSING_FIELD,
        source_refs=(
            _source_ref(
                present,
                snapshot_id=snapshot_id if not present_is_document else None,
                role=side,
            ),
        ),
        severity=SEVERITY_WARNING,
        detected_at=detected_at,
        note=(
            f"{metric} {period_key}（{scope or '口径未声明'}/{currency}）"
            f"仅 {side} 一侧有值，跨源一致性无法核对（如实跳过）"
        ),
    )


def check_cross_source(
    *,
    document_facts: Sequence[NormalizedFact],
    snapshot_facts: Sequence[NormalizedFact],
    policy: TolerancePolicy,
    snapshot_id: Optional[str] = None,
    document_source_class: str = SOURCE_REGULATORY_FILING,
    snapshot_source_class: str = SOURCE_THIRD_PARTY,
    document_label: str = "定期报告",
    snapshot_label: str = "行情快照",
    detected_at: str = "",
) -> List[ConstraintViolation]:
    """§7 F5 over one document's facts against one provider snapshot.

    Pairs are formed on the unified key ``(metric, period, scope, currency)`` —
    §7's "在 period / scope / version / currency / unit 统一后比较". Two passes:

    1. a key present on **both** sides is a real comparison, and CARD-13 decides
       which of the four classes it falls into;
    2. a key present on **one** side is checked for a counterpart carrying the
       same metric/period/currency under a *different* 口径. If there is one, the
       pair is a comparability blocker and CARD-13 says ``NOT_COMPARABLE`` — the
       document did print the figure, so "missing" would be a false statement.
       Only when no counterpart exists at all is the constraint reported as
       skipped, because "the snapshot had no such line" is not evidence that the
       filing is right.

    The tolerance is *not* widened to §7's 0.1% band — see the module docstring.
    """
    document_index = _index(document_facts)
    snapshot_index = _index(snapshot_facts)
    results: List[ConstraintViolation] = []

    for key in sorted(set(document_index) & set(snapshot_index), key=str):
        results.append(
            _pair_violation(
                document_index[key], snapshot_index[key], key=key,
                snapshot_id=snapshot_id,
                document_source_class=document_source_class,
                snapshot_source_class=snapshot_source_class,
                document_label=document_label, snapshot_label=snapshot_label,
                detected_at=detected_at,
            )
        )

    for key in sorted(set(document_index) ^ set(snapshot_index), key=str):
        present_is_document = key in document_index
        present = document_index[key] if present_is_document else snapshot_index[key]
        other = snapshot_index if present_is_document else document_index
        side = document_label if present_is_document else snapshot_label
        counterpart = _counterpart_under_another_scope(key, other)
        if counterpart is not None:
            left, right = (
                (present, counterpart) if present_is_document else (counterpart, present)
            )
            results.append(
                _pair_violation(
                    left, right, key=_series_key(left), snapshot_id=snapshot_id,
                    document_source_class=document_source_class,
                    snapshot_source_class=snapshot_source_class,
                    document_label=document_label, snapshot_label=snapshot_label,
                    detected_at=detected_at,
                )
            )
            continue
        results.append(
            _one_sided_violation(
                present, key=key, side=side, snapshot_id=snapshot_id,
                present_is_document=present_is_document, detected_at=detected_at,
            )
        )
    return results
