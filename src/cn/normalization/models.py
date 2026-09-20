"""Data models for the CARD-02 Financial Normalization Engine.

Six orthogonal normalisation axes are modelled explicitly as enums — unit,
currency, ratio, scope, version and period-kind — because collapsing any two of
them into a bare float is exactly how A-share research reports go wrong
(万元 mistaken for 元, YTD mistaken for a single quarter, 归母 mistaken for
含少数股东).

``NormalizedFact`` carries the CARD-01 back-link (``source_fact_id`` plus
``document_id``/``page``/``bbox``) so every normalized number remains traceable
to the PDF cell it came from, and the CARD-12 replay stays deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class Unit(str, Enum):
    """Declared disclosure unit. The **base unit of this system is 元**."""

    YUAN = "元"
    THOUSAND_YUAN = "千元"
    WAN_YUAN = "万元"
    MILLION_YUAN = "百万元"
    YI_YUAN = "亿元"


class Currency(str, Enum):
    """Reporting currency. 币种不换算，只标注（CARD-02 §8 edge case 4）。"""

    CNY = "CNY"
    USD = "USD"
    HKD = "HKD"
    OTHER = "OTHER"

    @classmethod
    def coerce(cls, value: object, *, default: "Currency" = None) -> Optional["Currency"]:
        """Map a provider/parser currency token onto a member (never guesses silently)."""
        if value is None:
            return default
        if isinstance(value, cls):
            return value
        text = str(value).strip().upper()
        if not text:
            return default
        aliases = {
            "CNY": cls.CNY, "RMB": cls.CNY, "人民币": cls.CNY, "人民币元": cls.CNY, "元": cls.CNY,
            "USD": cls.USD, "US$": cls.USD, "美元": cls.USD,
            "HKD": cls.HKD, "港币": cls.HKD, "港元": cls.HKD,
        }
        return aliases.get(text, cls.OTHER)


class Ratio(str, Enum):
    """Non-monetary magnitudes; they are never scaled by a currency unit."""

    PERCENT = "PERCENT"
    BP = "BP"
    MULTIPLE = "MULTIPLE"


class Scope(str, Enum):
    """口径：合并报表 vs 母公司报表。Different scopes never share a calculation."""

    CONSOLIDATED = "合并"
    PARENT = "母公司"

    @classmethod
    def coerce(cls, value: object) -> Optional["Scope"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        if not text:
            return None
        # 母公司 must be tested first: "母公司" does not contain "合并", but
        # providers emit variants such as "母公司报表"/"合并报表".
        if "母公司" in text or text.upper() in {"PARENT", "PARENT_COMPANY"}:
            return cls.PARENT
        if "合并" in text or text.upper() in {"CONSOLIDATED", "CONSOLIDATION"}:
            return cls.CONSOLIDATED
        return None


class Version(str, Enum):
    """披露版本。Signals come from CARD-04; this card only labels. Default AS_REPORTED."""

    AS_REPORTED = "AS_REPORTED"
    RESTATED = "RESTATED"
    CORRECTED = "CORRECTED"


class PeriodKind(str, Enum):
    """期间语义 —— the field whose absence caused the v3.0 YTD/单季 blind spot."""

    YTD_CUMULATIVE = "YTD_CUMULATIVE"
    SINGLE_QUARTER = "SINGLE_QUARTER"
    TTM = "TTM"
    POINT_IN_TIME = "POINT_IN_TIME"


class MetricSemantics(str, Enum):
    """flow vs stock. ``stock`` values may never be differenced (engine raises)."""

    FLOW = "flow"
    STOCK = "stock"
    DERIVED = "derived"
    MARKET = "market"


DEFAULT_CURRENCY = "CNY"
BASE_UNIT = "元"


@dataclass(frozen=True)
class NormalizedFact:
    """One disclosure value brought to the canonical unit / period / scope form.

    ``value`` is always expressed in the **base unit (元)** and the original
    ``raw_value`` + ``source_unit`` are preserved beside it, so the conversion is
    auditable and reversible for the demo's "原始披露值 → 规范化值" table.
    """

    normalized_fact_id: str
    metric: str                              # §7.1 canonical key, or metric_unknown_<slug>
    value: float                             # base unit = 元
    currency: str                            # CNY | USD | HKD | OTHER (never converted)
    period_key: str                          # YYYYQ1 / YYYYH1 / YYYY9M / YYYYFY
    period_kind: Optional[PeriodKind]         # None => 语义不可判定（禁止猜测）
    scope: Optional[Scope]
    version: Version
    available_at: Optional[str]
    source_fact_id: str                      # back-link to CARD-01 ExtractedFact
    financial_statement_id: Optional[str] = None  # filled by the projection step

    # -- audit extensions (§12 requires document/page/bbox on the ledger entry) --
    document_id: str = ""
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    row: Optional[int] = None
    column: Optional[int] = None
    raw_value: Optional[str] = None
    source_unit: Optional[str] = None
    source_table: Optional[str] = None
    statement_type: Optional[str] = None
    metric_known: bool = True
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, object]:
        return {
            "normalized_fact_id": self.normalized_fact_id,
            "metric": self.metric,
            "value": self.value,
            "currency": self.currency,
            "period_key": self.period_key,
            "period_kind": self.period_kind.value if self.period_kind else None,
            "scope": self.scope.value if self.scope else None,
            "version": self.version.value,
            "available_at": self.available_at,
            "source_fact_id": self.source_fact_id,
            "financial_statement_id": self.financial_statement_id,
            "document_id": self.document_id,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "row": self.row,
            "column": self.column,
            "raw_value": self.raw_value,
            "source_unit": self.source_unit,
            "source_table": self.source_table,
            "statement_type": self.statement_type,
            "metric_known": self.metric_known,
            "warnings": list(self.warnings),
        }

    @property
    def is_flow(self) -> bool:
        """A value that can legitimately be differenced into single quarters."""
        return self.period_kind in (PeriodKind.YTD_CUMULATIVE, PeriodKind.SINGLE_QUARTER, PeriodKind.TTM)

    @property
    def is_stock(self) -> bool:
        return self.period_kind is PeriodKind.POINT_IN_TIME
