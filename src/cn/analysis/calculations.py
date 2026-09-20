"""Auditable calculation records for CARD-03.

Every number the engine reports is produced by a :class:`Calculation` that
stores its formula, its operands (each pointing at either a CARD-02
``NormalizedFact`` → evidence ID, another calculation, or a formula constant)
and the operation used. :meth:`CalculationRegistry.recalculate` re-runs the
operation from the recorded operands, so a test (or a judge) can prove that the
value is a deterministic function of sourced inputs rather than a number the
model typed.

Constants are explicit and **never** sourced facts: a ``×100`` for a percentage
or a ``2`` for a two-point average is a formula constant, and is labelled as
such. Note that the project's :class:`~src.cn.evidence.EvidenceLedger` models a
*single* primitive operation over ≥2 existing evidence records — so only the
subset of calculations that fits that shape is publishable into the ledger
(see :mod:`src.cn.analysis.evidence_bridge`); the registry keeps the rest fully
resolvable rather than dropping the provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Unit

#: Operand kinds. ``evidence`` and ``calculation`` are derived from other
#: recorded values; ``constant`` is a pure formula constant.
OPERAND_EVIDENCE = "evidence"
OPERAND_CALCULATION = "calculation"
OPERAND_CONSTANT = "constant"

#: Operations the registry can both apply and re-apply. Each has a fixed arity
#: contract so a malformed calculation is rejected at registration time, not at
#: read time.
SUPPORTED_OPERATIONS = (
    "identity",     # the disclosed value itself, unscaled (level rows)
    "add",          # a + b + ...
    "subtract",     # a - (b + c + ...)
    "multiply",     # a * b * ...
    "divide",       # a / b / ...
    "average",      # (a + b + ...) / n
    "percent_of",   # a / b * 100
    "growth_pct",   # (a - b) / |b| * 100
)

#: Operations expressible as one :class:`~src.cn.evidence.EvidenceRecord`.
LEDGER_EXPRESSIBLE = {
    "add": "add",
    "subtract": "subtract",
    "multiply": "multiply",
    "divide": "divide",
}


class CalculationError(ValueError):
    """A calculation is malformed, unknown, or cannot be re-evaluated."""


@dataclass(frozen=True)
class Operand:
    """One input to a calculation.

    ``operand_id`` is a ``NormalizedFact.normalized_fact_id`` for evidence, the
    owning ``calculation_id`` for a chained calculation, or the literal value
    rendered as text for a constant.
    """

    kind: str
    operand_id: str
    value: float
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "id": self.operand_id, "value": self.value, "label": self.label}


@dataclass(frozen=True)
class Calculation:
    """A recorded, re-evaluable calculation."""

    calculation_id: str
    metric: str
    formula: str
    operation: str
    operands: Tuple[Operand, ...]
    value: float
    unit: str
    period_key: str
    period_basis: str
    scope: Optional[str] = None
    version: Optional[str] = None
    currency: Optional[str] = None
    source: str = "disclosed"          # disclosed | derived
    engine: Optional[str] = None       # reused engine that produced a derivation
    note: Optional[str] = None

    @property
    def evidence_ids(self) -> Tuple[str, ...]:
        """Evidence IDs directly consumed (skips calculations and constants)."""
        return tuple(op.operand_id for op in self.operands if op.kind == OPERAND_EVIDENCE)

    @property
    def all_input_ids(self) -> Tuple[str, ...]:
        return tuple(op.operand_id for op in self.operands if op.kind != OPERAND_CONSTANT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calculation_id": self.calculation_id,
            "metric": self.metric,
            "formula": self.formula,
            "operation": self.operation,
            "operands": [operand.to_dict() for operand in self.operands],
            "value": self.value,
            "unit": self.unit,
            "period_key": self.period_key,
            "period_basis": self.period_basis,
            "scope": self.scope,
            "version": self.version,
            "currency": self.currency,
            "source": self.source,
            "engine": self.engine,
            "note": self.note,
        }


class CalculationRegistry:
    """Deterministic store of :class:`Calculation` records.

    The registry is the CARD-03 half of the "deterministic calculations are not
    model output" claim: ``recalculate`` must reproduce ``value`` for every
    record, and the tests assert exactly that for every published metric.
    """

    def __init__(self) -> None:
        self._records: Dict[str, Calculation] = {}
        self._counter: Dict[str, int] = {}

    # -- construction -----------------------------------------------------

    def register(
        self,
        *,
        metric: str,
        formula: str,
        operation: str,
        operands: Sequence[Operand],
        value: Optional[float],
        unit: str = Unit.YUAN.value,
        period_key: str,
        period_basis: str,
        scope: Optional[str] = None,
        version: Optional[str] = None,
        currency: Optional[str] = None,
        source: str = "disclosed",
        engine: Optional[str] = None,
        note: Optional[str] = None,
        key_hint: Optional[str] = None,
    ) -> Calculation:
        """Record one calculation and mint its stable ID.

        IDs are deterministic (same inputs → same ID) so the CARD-12 replay and
        the CARD-09 claim graph can reference them across runs.
        """
        if operation not in SUPPORTED_OPERATIONS:
            raise CalculationError(f"Unsupported operation '{operation}'.")
        if value is None:
            raise CalculationError("A calculation record must carry a numeric value.")
        if not operands:
            raise CalculationError("A calculation needs at least one operand.")
        self._check_arity(operation, operands)

        stem = "_".join(
            part for part in (
                "calc", metric, period_key, period_basis.lower(),
                _token(scope), _token(version), key_hint,
            ) if part
        )
        seen = self._counter.get(stem, 0)
        self._counter[stem] = seen + 1
        calculation_id = stem if seen == 0 else f"{stem}_{seen}"

        record = Calculation(
            calculation_id=calculation_id,
            metric=metric,
            formula=formula,
            operation=operation,
            operands=tuple(operands),
            value=float(value),
            unit=unit,
            period_key=period_key,
            period_basis=period_basis,
            scope=scope,
            version=version,
            currency=currency,
            source=source,
            engine=engine,
            note=note,
        )
        self._records[calculation_id] = record
        return record

    # -- lookup -----------------------------------------------------------

    def get(self, calculation_id: str) -> Calculation:
        record = self._records.get(calculation_id)
        if record is None:
            raise KeyError(calculation_id)
        return record

    def has(self, calculation_id: str) -> bool:
        return calculation_id in self._records

    def records(self) -> List[Calculation]:
        return list(self._records.values())

    def resolve(self, calculation_id: str) -> Dict[str, Any]:
        """Full audit payload for one calculation (formula + inputs + value)."""
        return self.get(calculation_id).to_dict()

    # -- verification -----------------------------------------------------

    def recalculate(self, calculation_id: str) -> float:
        """Re-run the operation from the recorded operands.

        Raises :class:`CalculationError` rather than returning a guess when an
        operand is missing — a calculation that cannot be reproduced is a bug,
        not an approximation.
        """
        record = self.get(calculation_id)
        return self._apply(record.operation, [operand.value for operand in record.operands])

    @staticmethod
    def _apply(operation: str, values: Sequence[float]) -> float:
        if operation == "identity":
            if len(values) != 1:
                raise CalculationError("identity takes exactly one operand.")
            return float(values[0])
        if operation == "add":
            return float(sum(values))
        if operation == "subtract":
            result = values[0]
            for value in values[1:]:
                result -= value
            return float(result)
        if operation == "multiply":
            result = 1.0
            for value in values:
                result *= value
            return float(result)
        if operation == "divide":
            result = values[0]
            for value in values[1:]:
                if value == 0:
                    raise CalculationError("division_by_zero")
                result /= value
            return float(result)
        if operation == "average":
            return float(sum(values) / len(values))
        if operation == "percent_of":
            if len(values) != 2:
                raise CalculationError("percent_of takes exactly two operands.")
            if values[1] == 0:
                raise CalculationError("division_by_zero")
            return float(values[0] / values[1] * 100)
        if operation == "growth_pct":
            if len(values) != 2:
                raise CalculationError("growth_pct takes exactly two operands.")
            if values[1] == 0:
                raise CalculationError("division_by_zero")
            return float((values[0] - values[1]) / abs(values[1]) * 100)
        raise CalculationError(f"Unsupported operation '{operation}'.")

    def validate(self, *, tolerance: float = 1e-9) -> List[str]:
        """Re-derive every record; return the IDs that failed to reproduce."""
        errors: List[str] = []
        for calculation_id, record in self._records.items():
            try:
                expected = self._apply(record.operation, [op.value for op in record.operands])
            except CalculationError as exc:
                errors.append(f"{calculation_id}:{exc}")
                continue
            if abs(expected - record.value) > tolerance * max(1.0, abs(expected)):
                errors.append(f"{calculation_id}:mismatch({expected!r}!={record.value!r})")
        return errors

    # -- internal ---------------------------------------------------------

    @staticmethod
    def _check_arity(operation: str, operands: Sequence[Operand]) -> None:
        if operation == "identity" and len(operands) != 1:
            raise CalculationError("'identity' requires exactly one operand.")
        if operation in ("percent_of", "growth_pct") and len(operands) != 2:
            raise CalculationError(f"'{operation}' requires exactly two operands.")
        if operation in ("add", "subtract", "divide", "multiply") and len(operands) < 2:
            raise CalculationError(f"'{operation}' requires at least two operands.")


def evidence_operand(fact: Any) -> Operand:
    """Build an operand from a CARD-02 ``NormalizedFact`` (or any duck-type)."""
    return Operand(
        kind=OPERAND_EVIDENCE,
        operand_id=str(getattr(fact, "normalized_fact_id", "")),
        value=float(getattr(fact, "value")),
        label=str(getattr(fact, "raw_value", "") or ""),
    )


def calculation_operand(record: Calculation, label: str = "") -> Operand:
    return Operand(kind=OPERAND_CALCULATION, operand_id=record.calculation_id, value=record.value, label=label)


def constant_operand(value: float, label: str = "") -> Operand:
    return Operand(kind=OPERAND_CONSTANT, operand_id=repr(float(value)), value=float(value), label=label)


def _token(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(value))
    return cleaned.strip("_").lower() or None


def is_ledger_publishable(record: Calculation, *, known_ids: Optional[Iterable[str]] = None) -> bool:
    """Whether this calculation maps onto one ``EvidenceLedger.add_calculation``.

    The ledger supports a single primitive operation over ≥2 *existing* records.
    Constants break the mapping (they are not evidence), so such calculations
    stay registry-only — recorded digits that can never be presented as a
    provider fact.
    """
    known = set(known_ids or ())
    if record.operation not in LEDGER_EXPRESSIBLE:
        return False
    if any(operand.kind == OPERAND_CONSTANT for operand in record.operands):
        return False
    if len(record.operands) < 2:
        return False
    if not known:
        return True
    return all(operand.operand_id in known for operand in record.operands)
