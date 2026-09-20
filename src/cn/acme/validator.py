"""CARD-22 §8 — the ACME entry point and its orchestration.

``validate_multimodal_evidence`` runs the six §7 families over one filing and
returns an :class:`~src.cn.acme.models.IntegrityReport`. Three properties are
structural rather than best-effort:

1. **A family that cannot run is reported, never omitted.** Each family either
   contributes findings, contributes a ``ConstraintAvailability`` row naming the
   missing input, or contributes a ``check_failed`` finding carrying its own
   exception text (§11: 不得因检查失败静默跳过).
2. **A failing family does not take the others down.** Every family call is
   isolated, so one malformed table cannot suppress the balance-sheet check.
3. **Nothing here writes a financial value.** §9/§18: 初赛 has no correction path
   at all; the only writer is a 决赛 caller that supplies a complete
   :class:`~src.cn.acme.models.CorrectionTrail`.

The report is a pure function of its inputs (no clock beyond the caller-supplied
``detected_at``), so CARD-12 can replay it and CARD-11 can score it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization.models import NormalizedFact
from .cross_modal import ChartAnnotation, check_cross_modal
from .cross_period import check_cross_period_continuity
from .cross_source import check_cross_source
from .identities import DEFERRED_IDENTITIES, check_accounting_identities
from .models import (
    CONSTRAINT_FAMILIES,
    ConstraintAvailability,
    ConstraintViolation,
    FAMILY_ACCOUNTING_IDENTITY,
    FAMILY_CROSS_MODAL,
    FAMILY_CROSS_PERIOD,
    FAMILY_CROSS_SOURCE,
    FAMILY_PERIOD,
    FAMILY_TABLE_STRUCTURAL,
    IntegrityReport,
    SEVERITY_ERROR,
    STATUS_CHECK_FAILED,
    SourceRef,
    tally,
)
from .period_relations import check_period_relations
from .structural import check_table_structural
from .tolerance import DEFAULT_POLICY, TolerancePolicy

FamilyResult = Tuple[List[ConstraintViolation], List[Any], List[str]]


def _run_family(family: str, runner, *args, **kwargs) -> FamilyResult:
    """Isolate one family. A refusal becomes a ``check_failed`` finding.

    The exception message is preserved verbatim: an operator can only act on
    "Duplicate income record for fiscal period 2025H1" if that is what the report
    says. A generic "检查失败" would satisfy the letter of §11 and none of its
    purpose.
    """
    try:
        return list(runner(*args, **kwargs)), [], []
    except Exception as exc:  # noqa: BLE001 - deliberate: isolate, then report
        return (
            [
                ConstraintViolation(
                    violation_id=f"{family}:check_failed",
                    constraint_family=family,
                    constraint_id=f"{family}_unavailable",
                    inputs=(),
                    expected_value=None,
                    actual_value=None,
                    residual=None,
                    tolerance=0.0,
                    status=STATUS_CHECK_FAILED,
                    source_refs=(),
                    severity=SEVERITY_ERROR,
                    detected_at=str(kwargs.get("detected_at", "")),
                    note=f"约束族 {family} 检查失败：{type(exc).__name__}: {exc}",
                )
            ],
            [],
            [f"约束族 {family} 检查失败，其余族不受影响：{type(exc).__name__}: {exc}"],
        )


def validate_multimodal_evidence(
    facts: Sequence[NormalizedFact],
    statements: Sequence[Any] = (),
    snapshots: Optional[Sequence[NormalizedFact]] = None,
    chart_annotations: Optional[Sequence[ChartAnnotation]] = None,
    *,
    raw_facts: Sequence[object] = (),
    text_facts: Sequence[object] = (),
    prior_facts: Optional[Sequence[NormalizedFact]] = None,
    scope_signals: Sequence[object] = (),
    snapshot_id: Optional[str] = None,
    document_id: str = "",
    policy: Optional[TolerancePolicy] = None,
    detected_at: str = "",
    include_identities: bool = True,
) -> IntegrityReport:
    """§8 ``validate_multimodal_evidence``.

    ``facts`` / ``statements`` come from CARD-02; ``raw_facts`` are CARD-01's
    located cells (F2 needs the table geometry, which the projection discards);
    ``snapshots`` are provider facts (F5); ``prior_facts`` are the previous
    filing's facts (F4). Every optional input that is absent produces an explicit
    ``not_checked`` row — never a quietly empty family.
    """
    policy = policy or DEFAULT_POLICY
    results: List[ConstraintViolation] = []
    cross_modal: List[Any] = []
    notes: List[str] = []
    checked: List[str] = []
    not_checked: List[str] = []
    unavailable: List[ConstraintAvailability] = []

    # -- F1 accounting identities ------------------------------------------
    if include_identities:
        produced, _extra, family_notes = _run_family(
            FAMILY_ACCOUNTING_IDENTITY, check_accounting_identities, facts,
            policy=policy, detected_at=detected_at,
        )
        results.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_ACCOUNTING_IDENTITY)
    else:
        not_checked.append(FAMILY_ACCOUNTING_IDENTITY)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="accounting_identities",
                constraint_family=FAMILY_ACCOUNTING_IDENTITY,
                reason="调用方显式关闭了会计恒等式族（include_identities=False）",
            )
        )
    # §22 — the identities the card names but CARD-02's dictionary cannot feed.
    unavailable.extend(DEFERRED_IDENTITIES)

    # -- F2 table structural ------------------------------------------------
    if raw_facts:
        produced, _extra, family_notes = _run_family(
            FAMILY_TABLE_STRUCTURAL, check_table_structural, raw_facts,
            policy=policy, detected_at=detected_at,
        )
        results.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_TABLE_STRUCTURAL)
    else:
        not_checked.append(FAMILY_TABLE_STRUCTURAL)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="subtotal_reconciliation",
                constraint_family=FAMILY_TABLE_STRUCTURAL,
                reason=(
                    "未提供 CARD-01 表格单元格事实（raw_facts）：小计勾稽依赖表格几何，"
                    "投影后的财务报表已丢失行列结构，无法核对"
                ),
            )
        )

    # -- F3 period relations ------------------------------------------------
    if statements:
        produced, _extra, family_notes = _run_family(
            FAMILY_PERIOD, check_period_relations, statements, facts,
            policy=policy, detected_at=detected_at,
        )
        results.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_PERIOD)
    else:
        not_checked.append(FAMILY_PERIOD)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="ytd_single_quarter_relation",
                constraint_family=FAMILY_PERIOD,
                reason="未提供 CARD-02 投影后的财务报表，单季化关系无从核对",
            )
        )

    # -- F4 cross-period ----------------------------------------------------
    if prior_facts:
        produced, _extra, family_notes = _run_family(
            FAMILY_CROSS_PERIOD, check_cross_period_continuity,
            opening_facts=facts, closing_facts=prior_facts, policy=policy,
            scope_signals=scope_signals, detected_at=detected_at,
        )
        results.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_CROSS_PERIOD)
    else:
        not_checked.append(FAMILY_CROSS_PERIOD)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="opening_closing_continuity",
                constraint_family=FAMILY_CROSS_PERIOD,
                reason=(
                    "未提供上一期报告的规范化事实（prior_facts）：期初=上期期末是跨文档"
                    "比较，单份文档内无法成立"
                ),
            )
        )

    # -- F5 cross-source ----------------------------------------------------
    if snapshots:
        produced, _extra, family_notes = _run_family(
            FAMILY_CROSS_SOURCE, check_cross_source,
            document_facts=facts, snapshot_facts=snapshots, policy=policy,
            snapshot_id=snapshot_id, detected_at=detected_at,
        )
        results.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_CROSS_SOURCE)
    else:
        not_checked.append(FAMILY_CROSS_SOURCE)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="cross_source_agreement",
                constraint_family=FAMILY_CROSS_SOURCE,
                reason=(
                    "未提供 Snapshot Provider 事实（snapshots）：跨源一致性需要第二个来源，"
                    "本卡不自行构造来源"
                ),
            )
        )

    # -- F6 cross-modal -----------------------------------------------------
    if text_facts or chart_annotations:
        produced, family_notes = _run_cross_modal(
            facts,
            policy=policy,
            chart_annotations=chart_annotations or (),
            text_facts=text_facts,
            document_id=document_id,
            detected_at=detected_at,
        )
        cross_modal.extend(produced)
        notes.extend(family_notes)
        checked.append(FAMILY_CROSS_MODAL)
    else:
        not_checked.append(FAMILY_CROSS_MODAL)
        unavailable.append(
            ConstraintAvailability(
                constraint_id="cross_modal_ratio_consistency",
                constraint_family=FAMILY_CROSS_MODAL,
                reason="未提供正文文本事实或图表标注，跨模态比对缺少正文/图表一侧",
            )
        )

    review_queue = tuple(
        item.violation_id for item in results if item.open
    ) + tuple(item.inconsistency_id for item in cross_modal)
    totals = tally(results)
    totals["cross_modal"] = len(cross_modal)

    return IntegrityReport(
        document_id=document_id or _infer_document_id(facts),
        constraint_families_checked=tuple(checked),
        totals=totals,
        violations=tuple(results),
        cross_modal=tuple(cross_modal),
        review_queue=review_queue,
        families_not_checked=tuple(not_checked),
        constraints_not_available=tuple(unavailable),
        warnings=tuple(notes),
        detected_at=detected_at,
    )


def _run_cross_modal(
    facts: Sequence[NormalizedFact],
    *,
    policy: TolerancePolicy,
    chart_annotations: Sequence[ChartAnnotation],
    text_facts: Sequence[object],
    document_id: str,
    detected_at: str,
) -> Tuple[List[Any], List[str]]:
    notes: List[str] = []
    try:
        produced = check_cross_modal(
            facts, policy=policy, chart_annotations=chart_annotations,
            text_facts=text_facts, document_id=document_id, notes=notes,
        )
        return list(produced), notes
    except Exception as exc:  # noqa: BLE001 - isolate, then report
        return [], [
            f"约束族 {FAMILY_CROSS_MODAL} 检查失败，其余族不受影响："
            f"{type(exc).__name__}: {exc}"
        ]


def _infer_document_id(facts: Sequence[NormalizedFact]) -> str:
    for fact in facts:
        if fact.document_id:
            return fact.document_id
    return ""


__all__ = [
    "CONSTRAINT_FAMILIES",
    "SourceRef",
    "validate_multimodal_evidence",
]
