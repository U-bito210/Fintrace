"""CARD-22 §7 F1 — Accounting Identity Constraints.

The identities are a **data table** (:data:`IDENTITY_SPECS`), not a chain of
``if`` statements, for one reason: the card's §22 requires a constraint that
cannot run to be *reported as not run*. A hard-coded branch that never fires is
indistinguishable from a check that always passes; a spec row that emits
``skipped_missing_field`` (or, when its inputs are outside CARD-02's canonical
dictionary, a :class:`~src.cn.acme.models.ConstraintAvailability` entry) is not.

Only identities whose operands exist in CARD-02's §7.1 metric dictionary are
listed as runnable. The two the card names but the dictionary cannot supply
(``利润总额``/``所得税费用``, 现金及现金等价物 trio) are declared in
:data:`DEFERRED_IDENTITIES` with the exact missing vocabulary — inventing those
metrics here would be precisely the "编造指标" the project forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization.adapter import pick_primary
from ..normalization.models import NormalizedFact
from .models import (
    ConstraintAvailability,
    ConstraintOperand,
    ConstraintViolation,
    FAMILY_ACCOUNTING_IDENTITY,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    STATUS_NOT_COMPARABLE,
    STATUS_PASSED,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_VIOLATED,
    STATUS_WARNING,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy


@dataclass(frozen=True)
class IdentitySpec:
    """One accounting identity, declaratively.

    ``required`` are the canonical metric keys the identity needs; ``optional``
    are keys whose *presence changes the reading* rather than the arithmetic —
    today: ``equity_parent`` alone does not close the balance sheet when minority
    interests exist, and the identity must say ``not_comparable`` instead of
    reporting a violation it cannot justify.
    """

    constraint_id: str
    expression: str
    required: Tuple[str, ...]
    optional: Tuple[str, ...] = ()
    kind: str = "sum_of_parts"
    note: str = ""


#: ``Assets ≈ Liabilities + Equity`` (§7 F1).
BALANCE_SHEET_EQUATION = IdentitySpec(
    constraint_id="balance_sheet_equation",
    expression="资产总计 ≈ 负债合计 + 归属母公司所有者权益",
    required=("total_assets", "total_liabilities", "equity_parent"),
    optional=("minority_interest",),
    kind="balance",
    note="权益端为归母口径；披露少数股东权益时该恒等式不成立，记 not_comparable",
)

#: ``Gross Profit ≈ Revenue − Operating Cost`` (§7 F1, 允许报表项目差异).
GROSS_PROFIT_IDENTITY = IdentitySpec(
    constraint_id="gross_profit_identity",
    expression="毛利 ≈ 营业收入 − 营业成本",
    required=("gross_profit", "revenue", "operating_cost"),
    kind="difference",
    note="仅核对「披露的毛利行」；CARD-02 自行派生的毛利与本式恒等，核对它没有信息量",
)

IDENTITY_SPECS: Tuple[IdentitySpec, ...] = (
    BALANCE_SHEET_EQUATION,
    GROSS_PROFIT_IDENTITY,
)

#: §22 — identities the card names whose operands CARD-02's §7.1 dictionary does
#: not carry. Reported on every run so the gap is visible in the payload.
DEFERRED_IDENTITIES: Tuple[ConstraintAvailability, ...] = (
    ConstraintAvailability(
        constraint_id="profit_bridge",
        constraint_family=FAMILY_ACCOUNTING_IDENTITY,
        reason=(
            "净利润 ≈ 利润总额 − 所得税费用：CARD-02 §7.1 指标词典没有 "
            "profit_before_tax / income_tax 两个规范键，无法在规范层核对；"
            "本卡不新增指标（那属于 CARD-02 的词典）"
        ),
    ),
    ConstraintAvailability(
        constraint_id="cash_flow_closure",
        constraint_family=FAMILY_ACCOUNTING_IDENTITY,
        reason=(
            "期末现金 ≈ 期初现金 + 现金净增加额：词典没有现金及现金等价物的"
            "期初/期末/净增加额三个规范键（仅有 cfo/capex）"
        ),
    ),
)

#: §7 F1 requires 少数股东权益 to exist for the balance equation *not* to hold.
#: CARD-02 has no canonical key for it, so presence is detected on the unknown
#: placeholder key instead of silently adding a metric to another card's
#: dictionary.
_MINORITY_MARKERS = ("少数股东", "少数股东权益", "少数股东损益")


def _is_derived(fact: NormalizedFact) -> bool:
    return str(fact.source_table or "") == "derived" or fact.source_fact_id.startswith("derived(")


def _discloses_minority_interest(facts: Iterable[NormalizedFact]) -> Optional[NormalizedFact]:
    for fact in facts:
        if fact.metric in _MINORITY_MARKERS or any(
            marker in fact.metric for marker in _MINORITY_MARKERS
        ):
            return fact
    return None


def _group_facts(
    facts: Sequence[NormalizedFact], metrics: Sequence[str]
) -> Dict[Tuple[str, Optional[str], str, str], Dict[str, List[NormalizedFact]]]:
    """``(period, scope, version, currency)`` → metric → candidates.

    Scope is part of the key (§12 edge 1): 合并 and 母公司 balance sheets are
    different statements and must never be checked against each other. Version is
    part of it too — an AS_REPORTED column and a RESTATED column are two
    measurements, and CARD-04 already decided which one the document speaks in.
    """
    wanted = set(metrics)
    out: Dict[Tuple[str, Optional[str], str, str], Dict[str, List[NormalizedFact]]] = {}
    for fact in facts:
        if fact.metric not in wanted:
            continue
        key = (fact.period_key, fact.scope.value if fact.scope else None,
               fact.version.value, str(fact.currency))
        out.setdefault(key, {}).setdefault(fact.metric, []).append(fact)
    return out


def _operand(fact: NormalizedFact) -> ConstraintOperand:
    return ConstraintOperand(
        ref=fact.normalized_fact_id,
        value=fact.value,
        period=fact.period_key,
        scope=fact.scope.value if fact.scope else None,
        version=fact.version.value,
        unit="元",
        currency=str(fact.currency),
        label=fact.metric,
    )


def _source_ref(fact: NormalizedFact) -> SourceRef:
    return SourceRef(
        document_id=fact.document_id or None,
        page=fact.page,
        bbox=fact.bbox,
        locator=f"{fact.source_table or '未知表'} r{fact.row}c{fact.column}"
        if fact.row is not None
        else None,
    )


def _record(
    spec: IdentitySpec,
    key: Tuple[str, Optional[str], str, str],
    operands: Sequence[Tuple[NormalizedFact, float]],
    *,
    expected: Optional[float],
    actual: Optional[float],
    policy: TolerancePolicy,
    status: Optional[str] = None,
    note: Optional[str] = None,
    detected_at: str = "",
) -> ConstraintViolation:
    period, scope, version, currency = key
    if status is None:
        status, tolerance, residual = policy.classify(
            expected, actual, constraint_id=spec.constraint_id
        )
    else:
        tolerance = policy.tolerance_for(
            expected, actual, constraint_id=spec.constraint_id
        )
        residual = policy.residual(expected, actual)
    severity = SEVERITY_ERROR if status == STATUS_VIOLATED else SEVERITY_WARNING
    inputs = tuple(item[0] for item in operands)
    return ConstraintViolation(
        violation_id=make_violation_id(
            spec.constraint_id,
            FAMILY_ACCOUNTING_IDENTITY,
            (period, scope or "", version)
            + tuple(item.normalized_fact_id for item in inputs),
        ),
        constraint_family=FAMILY_ACCOUNTING_IDENTITY,
        constraint_id=spec.constraint_id,
        inputs=tuple(_operand(item) for item in inputs),
        expected_value=expected,
        actual_value=actual,
        residual=residual,
        tolerance=tolerance,
        status=status,
        source_refs=tuple(_source_ref(item) for item in inputs),
        severity=severity,
        detected_at=detected_at,
        note=(
            note
            or (spec.note if status != STATUS_PASSED else f"恒等式成立：{spec.expression}")
        ),
    )


def check_accounting_identities(
    facts: Sequence[NormalizedFact],
    *,
    policy: TolerancePolicy,
    detected_at: str = "",
) -> List[ConstraintViolation]:
    """Run every §7 F1 identity that this filing has the operands for.

    One emitted record per (identity, period, scope, version, currency). A filing
    that does not disclose an operand yields ``skipped_missing_field`` — §11 is
    explicit that a missing 科目 is not a violation and not an error.
    """
    results: List[ConstraintViolation] = []
    minority = _discloses_minority_interest(facts)

    for spec in IDENTITY_SPECS:
        needed = list(spec.required)
        groups = _group_facts(facts, needed)
        seen_keys = set(groups)
        # A group that has *some* but not all operands must still be reported.
        partial = _group_facts(facts, needed)
        for key in sorted(seen_keys, key=str):
            bucket = partial[key]
            missing = [name for name in needed if name not in bucket]
            if missing:
                primary = bucket[sorted(bucket)[0]][0]
                results.append(
                    _record(
                        spec,
                        key,
                        [(primary, primary.value)],
                        expected=None,
                        actual=None,
                        policy=policy,
                        status=STATUS_SKIPPED_MISSING_FIELD,
                        note=f"缺少披露项：{'、'.join(missing)}（不报错、不误报）",
                        detected_at=detected_at,
                    )
                )
                continue
            picked = {name: pick_primary(bucket[name]) for name in needed}
            results.append(
                _evaluate(spec, key, picked, policy=policy, minority=minority,
                          detected_at=detected_at)
            )

    results.extend(_skipped_without_any_operand(facts, detected_at=detected_at))
    return results


def _skipped_without_any_operand(
    facts: Sequence[NormalizedFact], *, detected_at: str
) -> List[ConstraintViolation]:
    """Identities whose operands are entirely absent still leave a trace.

    Without this, a filing that discloses no balance sheet at all would produce
    an *empty* family, and an empty family looks exactly like a clean one.
    """
    present = {fact.metric for fact in facts}
    out: List[ConstraintViolation] = []
    for spec in IDENTITY_SPECS:
        if present & set(spec.required):
            continue
        out.append(
            ConstraintViolation(
                violation_id=make_violation_id(
                    spec.constraint_id, FAMILY_ACCOUNTING_IDENTITY, ("no-operand",)
                ),
                constraint_family=FAMILY_ACCOUNTING_IDENTITY,
                constraint_id=spec.constraint_id,
                inputs=(),
                expected_value=None,
                actual_value=None,
                residual=None,
                tolerance=0.0,
                status=STATUS_SKIPPED_MISSING_FIELD,
                source_refs=(),
                severity=SEVERITY_WARNING,
                detected_at=detected_at,
                note=(
                    f"未披露任何参与项（{'、'.join(spec.required)}）："
                    "该恒等式本次未核对，不等于通过"
                ),
            )
        )
    return out


def _evaluate(
    spec: IdentitySpec,
    key: Tuple[str, Optional[str], str, str],
    picked: Dict[str, NormalizedFact],
    *,
    policy: TolerancePolicy,
    minority: Optional[NormalizedFact],
    detected_at: str,
) -> ConstraintViolation:
    """The arithmetic, per identity kind. No identity computes anything else."""
    if spec.constraint_id == BALANCE_SHEET_EQUATION.constraint_id:
        assets = picked["total_assets"]
        liabilities = picked["total_liabilities"]
        equity = picked["equity_parent"]
        # expected = 由另两行独立推导（负债 + 权益）；actual = 报表印出来的资产总计。
        expected = liabilities.value + equity.value
        actual = assets.value
        operands = [(assets, assets.value), (liabilities, liabilities.value),
                    (equity, equity.value)]
        if minority is not None:
            # §7 F1 note + §12 edge 1: 归母权益 alone does not close the balance
            # sheet once 少数股东权益 is on it. Reporting ``violated`` here would
            # be a false alarm manufactured by our own missing metric.
            return _record(
                spec, key, operands,
                expected=expected, actual=actual, policy=policy,
                status=STATUS_NOT_COMPARABLE,
                note=(
                    "文档披露少数股东权益（{}），归母权益不能单独闭合恒等式；"
                    "该恒等式记 not_comparable，不报违规".format(
                        minority.page if minority.page is not None else "页码未知"
                    )
                ),
                detected_at=detected_at,
            )
        return _record(spec, key, operands, expected=expected, actual=actual,
                       policy=policy, detected_at=detected_at)

    if spec.constraint_id == GROSS_PROFIT_IDENTITY.constraint_id:
        gross = picked["gross_profit"]
        revenue = picked["revenue"]
        cost = picked["operating_cost"]
        if _is_derived(gross):
            return _record(
                spec, key, [(gross, gross.value), (revenue, revenue.value), (cost, cost.value)],
                expected=None, actual=None, policy=policy,
                status=STATUS_SKIPPED_MISSING_FIELD,
                note=(
                    "毛利为 CARD-02 派生值（revenue − operating_cost），"
                    "核对它与本式恒等，无检测能力；等待文档直接披露毛利行"
                ),
                detected_at=detected_at,
            )
        expected = revenue.value - cost.value
        actual = gross.value
        return _record(
            spec, key,
            [(gross, gross.value), (revenue, revenue.value), (cost, cost.value)],
            expected=expected, actual=actual, policy=policy, detected_at=detected_at,
        )

    raise AssertionError(f"未实现的会计恒等式：{spec.constraint_id}")
