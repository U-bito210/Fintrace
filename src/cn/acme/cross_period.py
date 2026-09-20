"""CARD-22 §7 F4 — Cross-period Constraints (期初 = 上期期末).

Concretely: the *comparative* column a filing carries for the previous period
must agree with what the previous filing actually said for that period. A 2025
annual report prints 2024-12-31 as its opening balance; the 2024 annual report
printed a 2024-12-31 closing balance. When those differ, one of two very different
things happened, and §7 F4 insists the check must tell them apart:

* a **disclosed** accounting-policy change / restatement / consolidation-scope
  change — CARD-04 already detects these and CARD-13 already arbitrates the
  version supersession. ACME *consults* that signal and downgrades its own
  finding to ``not_comparable`` with the signal named in the note; or
* an **undisclosed** inconsistency — the finding stands.

So this module reports, and never arbitrates: it does not decide which filing is
right (§18's red line, and CARD-13's job). It only refuses to let the question go
unasked.

The two filings arrive as two separate fact lists on purpose. Merging them would
make the current filing's comparative and the prior filing's closing the *same*
``(metric, period_key)`` key, CARD-02's precedence would pick one, and the
comparison would vanish into a silent collapse.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization.metrics import is_stock_metric
from ..normalization.models import NormalizedFact
from .models import (
    ConstraintOperand,
    ConstraintViolation,
    FAMILY_CROSS_PERIOD,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    STATUS_NOT_COMPARABLE,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_VIOLATED,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy

CONSTRAINT_OPENING_CLOSING = "opening_closing_continuity"

#: CARD-04 signal types that legitimately break continuity. The strings mirror
#: :mod:`src.cn.analysis.scope_signals`; read by attribute so this module keeps
#: working against any object exposing the same two fields (it must not import
#: CARD-04's detector — the whole point is that it *consumes* the signal).
CONTINUITY_BREAKING_SIGNALS: Tuple[str, ...] = (
    "restated",
    "accounting_policy_change",
    "prior_period_error_correction",
    "consolidation_scope_change",
    "comparable_period_adjusted",
)


def _signal_type(signal: object) -> str:
    value = getattr(signal, "signal_type", None)
    return str(getattr(value, "value", value) or "")


def _signal_periods(signal: object) -> Tuple[str, ...]:
    periods = getattr(signal, "periods_affected", ()) or ()
    return tuple(str(item) for item in periods)


def _explaining_signal(
    signals: Iterable[object], period_key: str
) -> Optional[object]:
    """The first CARD-04 signal that explains a break in *this* period.

    A signal that names no period is treated as explaining the comparison it is
    given — CARD-04 emits some signals at document level, and refusing them would
    turn a disclosed restatement into a violation.
    """
    for signal in signals:
        if _signal_type(signal) not in CONTINUITY_BREAKING_SIGNALS:
            continue
        periods = _signal_periods(signal)
        if not periods or period_key in periods:
            return signal
    return None


def _index(facts: Iterable[NormalizedFact]) -> Dict[Tuple[str, str, str, str], NormalizedFact]:
    out: Dict[Tuple[str, str, str, str], NormalizedFact] = {}
    for fact in facts:
        if not is_stock_metric(fact.metric):
            continue
        key = (
            fact.metric,
            fact.period_key,
            fact.scope.value if fact.scope else "",
            str(fact.currency),
        )
        existing = out.get(key)
        if existing is None or (fact.page or 0, fact.row or 0) < (
            existing.page or 0,
            existing.row or 0,
        ):
            out[key] = fact
    return out


def _operand(fact: NormalizedFact, *, role: str) -> ConstraintOperand:
    return ConstraintOperand(
        ref=fact.normalized_fact_id,
        value=fact.value,
        period=fact.period_key,
        scope=fact.scope.value if fact.scope else None,
        version=fact.version.value,
        unit="元",
        currency=str(fact.currency),
        label=f"{fact.metric}（{role}）",
    )


def _source_ref(fact: NormalizedFact) -> SourceRef:
    return SourceRef(
        document_id=fact.document_id or None,
        page=fact.page,
        bbox=fact.bbox,
        locator=f"{fact.source_table or '未知表'} r{fact.row}c{fact.column}"
        if fact.row is not None
        else (fact.source_table or None),
    )


def check_cross_period_continuity(
    *,
    opening_facts: Sequence[NormalizedFact],
    closing_facts: Sequence[NormalizedFact],
    policy: TolerancePolicy,
    scope_signals: Sequence[object] = (),
    detected_at: str = "",
    opening_label: str = "本期报告的比较列",
    closing_label: str = "上期报告",
) -> List[ConstraintViolation]:
    """§7 F4 for one pair of filings (stock metrics only).

    ``opening_facts`` come from the *current* filing (its comparative column),
    ``closing_facts`` from the *prior* filing. Both are indexed by
    ``(metric, period, scope, currency)``; a key present on both sides is a
    continuity claim, a key present on one side is reported as a skipped
    constraint rather than passed.
    """
    opening = _index(opening_facts)
    closing = _index(closing_facts)
    results: List[ConstraintViolation] = []

    for key in sorted(set(opening) | set(closing), key=str):
        metric, period_key, scope, currency = key
        left = opening.get(key)
        right = closing.get(key)
        if left is None or right is None:
            present = left or right
            assert present is not None
            results.append(
                ConstraintViolation(
                    violation_id=make_violation_id(
                        CONSTRAINT_OPENING_CLOSING, FAMILY_CROSS_PERIOD,
                        (metric, period_key, scope, currency, "one-sided"),
                    ),
                    constraint_family=FAMILY_CROSS_PERIOD,
                    constraint_id=CONSTRAINT_OPENING_CLOSING,
                    inputs=(_operand(present, role="单侧"),),
                    expected_value=None,
                    actual_value=None,
                    residual=None,
                    tolerance=0.0,
                    status=STATUS_SKIPPED_MISSING_FIELD,
                    source_refs=(_source_ref(present),),
                    severity=SEVERITY_WARNING,
                    detected_at=detected_at,
                    note=(
                        f"{metric} {period_key} 只有"
                        f"{'本期报告' if left is not None else '上期报告'}一侧有值，"
                        "跨期连续性无法核对（如实跳过，不视为通过）"
                    ),
                )
            )
            continue

        signal = _explaining_signal(scope_signals, period_key)
        versions_differ = left.version is not right.version

        if signal is not None or versions_differ:
            context = (
                f"CARD-04 信号：{_signal_type(signal)}"
                if signal is not None
                else f"披露版本不同（{left.version.value} vs {right.version.value}）"
            )
            results.append(
                ConstraintViolation(
                    violation_id=make_violation_id(
                        CONSTRAINT_OPENING_CLOSING, FAMILY_CROSS_PERIOD,
                        (metric, period_key, scope, currency, "restatement"),
                    ),
                    constraint_family=FAMILY_CROSS_PERIOD,
                    constraint_id=CONSTRAINT_OPENING_CLOSING,
                    inputs=(
                        _operand(left, role=opening_label),
                        _operand(right, role=closing_label),
                    ),
                    expected_value=right.value,
                    actual_value=left.value,
                    residual=policy.residual(right.value, left.value),
                    tolerance=policy.tolerance_for(
                        right.value, left.value, constraint_id=CONSTRAINT_OPENING_CLOSING
                    ),
                    status=STATUS_NOT_COMPARABLE,
                    source_refs=(_source_ref(left), _source_ref(right)),
                    severity=SEVERITY_WARNING,
                    detected_at=detected_at,
                    note=(
                        f"{metric} {period_key} 两期取值不同，但差异有已披露解释（{context}）；"
                        "restatement_context 已标注，ACME 不裁决取哪一个（裁决属 CARD-13）"
                    ),
                )
            )
            continue

        status, tolerance, residual = policy.classify(
            right.value, left.value, constraint_id=CONSTRAINT_OPENING_CLOSING
        )
        results.append(
            ConstraintViolation(
                violation_id=make_violation_id(
                    CONSTRAINT_OPENING_CLOSING, FAMILY_CROSS_PERIOD,
                    (metric, period_key, scope, currency),
                ),
                constraint_family=FAMILY_CROSS_PERIOD,
                constraint_id=CONSTRAINT_OPENING_CLOSING,
                inputs=(
                    _operand(left, role=opening_label),
                    _operand(right, role=closing_label),
                ),
                expected_value=right.value,
                actual_value=left.value,
                residual=residual,
                tolerance=tolerance,
                status=status,
                source_refs=(_source_ref(left), _source_ref(right)),
                severity=SEVERITY_ERROR if status == STATUS_VIOLATED else SEVERITY_WARNING,
                detected_at=detected_at,
                note=(
                    f"{metric} {period_key}：本期报告的比较列（{left.value}）与上期报告"
                    f"（{right.value}）不一致，且未见任何口径/重述信号解释该差异"
                ),
            )
        )
    return results
