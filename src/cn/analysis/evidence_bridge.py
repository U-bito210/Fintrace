"""Bridge CARD-03 calculations into the existing ``EvidenceLedger`` (§10/§12).

The ledger (:mod:`src.cn.evidence`) records a calculation as **one primitive
operation over ≥2 already-registered evidence records** — that shape is what
makes ``EvidenceLedger.validate()`` able to re-derive it. Some of CARD-03's
metrics fit it exactly (``cfo − capex``, ``归母 − 扣非``, ``扣非调整额 / 归母``);
the percentage metrics do **not**, because a ``×100`` is a formula constant
rather than a provider fact.

Rather than weaken the ledger (or invent a fake "100" fact — which ``AGENTS.md``
forbids), this bridge publishes the subset that fits and reports the rest as
:data:`SKIPPED_CONSTANT` / :data:`SKIPPED_NOT_LEDGER_SHAPED`, with the metric
still fully resolvable through the CARD-03
:class:`~src.cn.analysis.calculations.CalculationRegistry`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..evidence import EvidenceError, EvidenceLedger
from .calculations import (
    LEDGER_EXPRESSIBLE,
    OPERAND_CALCULATION,
    Calculation,
    CalculationRegistry,
)
from .models import AnalysisResult

SKIPPED_CONSTANT = "ledger_cannot_express_formula_constant"
SKIPPED_NOT_LEDGER_SHAPED = "operation_not_expressible_in_ledger"
SKIPPED_MISSING_INPUT = "input_not_present_in_ledger"
SKIPPED_DUPLICATE = "duplicate_evidence_id"


def publish_analysis_evidence(
    result: AnalysisResult,
    *,
    ledger: Optional[EvidenceLedger] = None,
    symbol: Optional[str] = None,
    registry: Optional[CalculationRegistry] = None,
) -> Dict[str, object]:
    """Publish the ledger-shaped subset of ``result``'s calculations.

    Iterates **every** recorded calculation (not just the reported metrics), so
    the derived intermediates (``gross_profit = revenue − operating_cost``,
    ``非经常性损益 = 归母 − 扣非``) reach the ledger too, where the ledger can
    re-derive them on demand.

    Never raises on an individual calculation: a publishable record that the
    ledger rejects is reported in ``skipped`` with a typed reason, so the caller
    can still distinguish "no coverage" from "coverage not applicable".
    """
    registry = registry or result.registry
    if registry is None:
        return {
            "published": [],
            "published_count": 0,
            "skipped": [],
            "reason": "analysis_result_has_no_calculation_registry",
        }
    ledger = ledger or EvidenceLedger(snapshot_id=result.document_id)
    symbol_key = symbol or result.document_id

    published: List[str] = []
    skipped: List[Dict[str, str]] = []
    known = {record.evidence_id for record in ledger.records()}

    for record in registry.records():
        reason = _skip_reason(record, known)
        if reason is not None:
            skipped.append({"calculation_id": record.calculation_id, "reason": reason})
            continue

        try:
            ledger.add_calculation(
                evidence_id=record.calculation_id,
                symbol=symbol_key,
                metric=record.metric,
                value=record.value,
                currency=record.currency or "CNY",
                unit=record.unit,
                operation=LEDGER_EXPRESSIBLE[record.operation],
                input_ids=list(record.all_input_ids),
                fiscal_period=record.period_key or None,
                period_basis=record.period_basis or None,
            )
        except EvidenceError:
            skipped.append({"calculation_id": record.calculation_id, "reason": SKIPPED_DUPLICATE})
            continue
        known.add(record.calculation_id)
        published.append(record.calculation_id)

    return {
        "published": published,
        "published_count": len(published),
        "skipped": skipped,
        "ledger_snapshot_id": ledger.snapshot_id,
        "registry_calculation_count": len(registry.records()),
    }


def _skip_reason(record: Calculation, known: Sequence[str]) -> Optional[str]:
    known_set = set(known)
    if record.operation not in LEDGER_EXPRESSIBLE:
        return SKIPPED_NOT_LEDGER_SHAPED
    if any(operand.kind == "constant" for operand in record.operands):
        return SKIPPED_CONSTANT
    if len(record.operands) < 2:
        return SKIPPED_NOT_LEDGER_SHAPED
    for operand in record.operands:
        if operand.operand_id not in known_set:
            return SKIPPED_MISSING_INPUT
    return None


def analysis_jumps(result: AnalysisResult, *, registry: Optional[CalculationRegistry] = None) -> Dict[str, Dict[str, object]]:
    """Map every metric's ``calculation_id`` to its formula + input evidence IDs.

    This is the "每行可展开公式与输入证据" data for the analysis panel (§14) and
    for CARD-09's claim graph.
    """
    registry = registry or result.registry
    if registry is None:
        return {}
    jumps: Dict[str, Dict[str, object]] = {}
    for metric_result in result.metrics:
        if not metric_result.calculation_id:
            continue
        try:
            record = registry.get(metric_result.calculation_id)
        except KeyError:  # pragma: no cover
            continue
        jumps[metric_result.calculation_id] = {
            "metric": record.metric,
            "label": metric_result.label,
            "formula": record.formula,
            "operation": record.operation,
            "value": record.value,
            "unit": record.unit,
            "period_key": record.period_key,
            "period_basis": record.period_basis,
            "scope": record.scope,
            "version": record.version,
            "source": record.source,
            "engine": record.engine,
            "note": record.note,
            "evidence_ids": list(record.evidence_ids),
            "operands": [operand.to_dict() for operand in record.operands],
            "recalculated": _safe_recalculate(registry, record),
        }
    return jumps


def _safe_recalculate(registry: CalculationRegistry, record: Calculation) -> Optional[float]:
    try:
        return registry.recalculate(record.calculation_id)
    except Exception:  # pragma: no cover - registry.validate() reports these
        return None


__all__ = [
    "SKIPPED_CONSTANT",
    "SKIPPED_DUPLICATE",
    "SKIPPED_MISSING_INPUT",
    "SKIPPED_NOT_LEDGER_SHAPED",
    "analysis_jumps",
    "publish_analysis_evidence",
]
