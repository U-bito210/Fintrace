"""Providers → CARD-02 facts, for the one family that needs a second source.

CARD-22 §7 F5 compares a filing against a Snapshot Provider, so it needs the
provider's :class:`~src.cn.domain.FinancialStatement` records expressed in the
same vocabulary as CARD-02's :class:`~src.cn.normalization.models.NormalizedFact`
(period key, period semantics, scope, version). This module is that translation
and nothing else — no comparison, no tolerance, no arbitration.

Three rules keep it from inventing anything:

* **The projection runs backwards, and only along declared edges.**
  ``FinancialStatement.values`` keys are the *existing* system keys — ``net_profit``
  is 归母, not 含少数股东 (see ``src/cn/providers/tushare.py``). The inverse map is
  built from :data:`~src.cn.normalization.metrics.STANDARD_METRICS`, so a
  statement key that no canonical metric claims is **skipped with a warning**
  rather than guessed at.
* **Scope is declared, not inferred.** A provider statement carries no 口径 field;
  the provider contract is consolidated-only (documented on
  :func:`src.cn.normalization.adapter.assert_homogeneous_group`), so every
  converted fact is stamped ``CONSOLIDATED`` *and* the caller is told so in
  ``warnings``. Without this, every cross-source pair would collapse to
  ``NOT_COMPARABLE`` on "one side declares 合并 and the other declares nothing".
* **Period semantics come from the statement's own ``period_basis``.** An
  unrecognised or empty basis yields ``period_kind=None`` (undeclared) instead of
  a default, matching CARD-02's rule that an unknown period semantic stops the
  comparison rather than starting one.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ..domain import FinancialStatement
from ..normalization.metrics import STANDARD_METRICS
from ..normalization.models import NormalizedFact, PeriodKind, Scope, Version

#: values key → canonical metric key, built from the §7.1 dictionary itself.
_VALUE_KEY_TO_METRIC: Dict[str, str] = {}
for _spec in STANDARD_METRICS:
    if _spec.value_key and _spec.statement != "market":
        _VALUE_KEY_TO_METRIC.setdefault(_spec.value_key, _spec.key)

_ISTOCK_STATEMENTS = ("balance",)
_BASIS_TTM = ("TTM",)
_BASIS_SINGLE_QUARTER = ("SINGLE_QUARTER", "SINGLE QUARTER")


def _period_kind(statement: FinancialStatement) -> Optional[PeriodKind]:
    if statement.statement_type in _ISTOCK_STATEMENTS:
        return PeriodKind.POINT_IN_TIME
    basis = str(statement.period_basis or "").strip().upper()
    if basis in _BASIS_TTM:
        return PeriodKind.TTM
    if basis in _BASIS_SINGLE_QUARTER:
        return PeriodKind.SINGLE_QUARTER
    if basis in ("FY", "YTD", "YTD_CUMULATIVE", "9M", "H1", "Q1"):
        return PeriodKind.YTD_CUMULATIVE
    return None


def facts_from_statements(
    statements: Iterable[FinancialStatement],
    *,
    source_id: str,
    warnings: Optional[List[str]] = None,
) -> List[NormalizedFact]:
    """Convert provider statements into CARD-02 facts.

    ``source_id`` becomes the fact's ``document_id`` so CARD-13's
    :func:`~src.cn.conflict.candidate_from_fact` can key the candidate on the
    snapshot id rather than on a missing document.
    """
    out: List[NormalizedFact] = []
    log = warnings if warnings is not None else []
    seen_keys: set = set()

    for statement in statements:
        period_kind = _period_kind(statement)
        if period_kind is None:
            log.append(
                f"快照 {source_id} 的 {statement.statement_type} {statement.fiscal_period} "
                f"期间语义未声明（period_basis={statement.period_basis!r}），该报表未转换"
            )
            continue
        for value_key, value in sorted(statement.values.items()):
            if value is None:
                continue
            metric = _VALUE_KEY_TO_METRIC.get(value_key)
            if metric is None:
                key = (statement.statement_type, statement.fiscal_period, value_key)
                if key not in seen_keys:
                    seen_keys.add(key)
                    log.append(
                        f"快照字段 {value_key} 没有对应的 §7.1 规范指标，已跳过"
                        "（不猜测它的口径与含义）"
                    )
                continue
            out.append(
                NormalizedFact(
                    normalized_fact_id=(
                        f"nf_snapshot_{source_id}_{metric}_{statement.fiscal_period}"
                    ),
                    metric=metric,
                    value=float(value),
                    currency=statement.currency,
                    period_key=statement.fiscal_period,
                    period_kind=period_kind,
                    # 见模块文档：provider 合同只出合并口径，这里显式声明并留痕。
                    scope=Scope.CONSOLIDATED,
                    version=Version.RESTATED if statement.is_restated else Version.AS_REPORTED,
                    available_at=statement.available_at,
                    source_fact_id=f"snapshot:{source_id}:{value_key}",
                    document_id=source_id,
                    raw_value=str(value),
                    source_unit=statement.unit,
                    source_table=f"snapshot:{statement.statement_type}",
                    statement_type=statement.statement_type,
                    metric_known=True,
                    warnings=("scope_declared_consolidated_from_provider_contract",),
                )
            )
    return out


__all__ = ["facts_from_statements"]
