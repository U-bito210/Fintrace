"""The fact base a draft is checked against, and the citation resolver.

Two responsibilities:

**1. Turning CARD-02's ``NormalizedFact[]`` into a queryable disclosure set.**
The naive shape — ``{(metric, period): value}`` — is wrong here, and measurably so.
Measured on ``annual_01_profit_continued.pdf``: the 合并利润表 has two columns
(``本报告期`` / ``年初至本报告期末``) that both canonicalise to
``period_key="2025FY"``, so ``revenue`` legitimately has **two** disclosed values
(1,234,567 and 2,000,000). Collapsing them to one and then judging a draft against
that one would flag the *other* disclosed column as a fabrication — the precise
false positive §9 says costs more than a miss.

So a lookup returns a **set of disclosures**, and matching succeeds if the claim's
figure equals *any* of them. When nothing matches, the finding's ``expected`` is
the *primary* disclosure (the same convention CARD-02's ``pick_primary`` applies:
audited statement body first, then document order) and the alternatives are
carried in ``detail`` — an "expected" that quietly hid a second real column would
be its own kind of wrong number.

**2. Resolving a draft's citation string to a document (and page).** Pure parsing:
a reference is a document id or filename, optionally with a page marker
(``#p3`` / ``@3`` / ``第3页``). Nothing is fuzzy-matched; an unrecognised reference
resolves to ``None`` and is reported as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..analysis.series import newest_version
from ..normalization.models import Version
from ..normalization.units import UNIT_SCALES, canonical_unit, unit_scale

#: Magnitude ratios that mean "the draft declared a different unit", §7.2. Derived
#: from the unit ladder itself rather than hard-coded, so 千元/百万元 are covered
#: too — a 10^3 slip is the same class of mistake as a 10^4 slip.
_UNIT_RATIOS: Tuple[float, ...] = tuple(
    sorted({scale for scale in UNIT_SCALES.values() if scale != 1.0})
    + [1.0 / scale for scale in UNIT_SCALES.values() if scale != 1.0]
)

#: Relative slack when comparing two floats that should be equal. Absolute
#: equality is used for ladder ratios; this is for value equality only.
_REL_EPS = 1e-9

_PAGE_MARKERS = (
    re.compile(r"[#@]\s*[pP]?\s*(\d+)\s*$"),
    re.compile(r"第\s*(\d+)\s*页\s*$"),
    re.compile(r"\bp\s*(\d+)\s*$", re.IGNORECASE),
)


def _close(left: float, right: float, tolerance: float = 0.0) -> bool:
    if left == right:
        return True
    return abs(left - right) <= max(tolerance, _REL_EPS * max(abs(left), abs(right), 1.0))


@dataclass(frozen=True)
class Disclosure:
    """One disclosed value, in the base unit, with its full provenance."""

    metric: str
    period_key: str
    value: float                       # base unit (元)
    fact_id: str
    document_id: str
    scope: Optional[str] = None
    page: Optional[int] = None
    version: str = Version.AS_REPORTED.value
    unit: Optional[str] = None         # the unit the filing declared
    available_at: Optional[str] = None
    source_table: Optional[str] = None
    statement_type: Optional[str] = None
    raw_value: Optional[str] = None
    rank: Tuple[int, int, int, int] = (9, 9, 9, 9)

    @property
    def normalized_fact_id(self) -> str:
        """Duck-type alias so CARD-03's ``evidence_operand`` accepts a disclosure.

        The checker recomputes growth through CARD-03's ``compute_yoy`` rather than
        re-deriving a formula of its own (§7.1); that function reads
        ``normalized_fact_id``, which is CARD-02's name for the same identifier.
        """
        return self.fact_id

    @property
    def is_adjusted(self) -> bool:
        """A version that supersedes an as-reported figure."""
        return self.version != Version.AS_REPORTED.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "period_key": self.period_key,
            "value": self.value,
            "fact_id": self.fact_id,
            "document_id": self.document_id,
            "scope": self.scope,
            "page": self.page,
            "version": self.version,
            "unit": self.unit,
            "available_at": self.available_at,
            "source_table": self.source_table,
            "raw_value": self.raw_value,
        }


@dataclass(frozen=True)
class MatchOutcome:
    """How a claimed figure related to the fact base."""

    kind: str
    disclosure: Optional[Disclosure] = None
    alternatives: Tuple[Disclosure, ...] = ()
    detail: str = ""
    #: For ``superseded``: the version that replaced the matched one. Carried
    #: separately because ``expected`` must show the value the author *should*
    #: have quoted, and the matched disclosure is by definition the wrong one.
    replacement: Optional[Disclosure] = None

    @property
    def matched(self) -> bool:
        return self.kind in ("exact",)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "disclosure": self.disclosure.to_dict() if self.disclosure else None,
            "alternatives": [item.to_dict() for item in self.alternatives],
            "replacement": self.replacement.to_dict() if self.replacement else None,
            "detail": self.detail,
        }


class FactIndex:
    """Queryable disclosure set built from normalized facts."""

    def __init__(self, facts: Optional[Iterable[object]] = None) -> None:
        self._disclosures: List[Disclosure] = []
        for fact in facts or ():
            item = self._to_disclosure(fact)
            if item is not None:
                self._disclosures.append(item)
        self._disclosures.sort(
            key=lambda d: (d.metric, d.scope or "", d.period_key, d.rank, d.fact_id)
        )

    # -- construction -------------------------------------------------------
    @staticmethod
    def _to_disclosure(fact: object) -> Optional[Disclosure]:
        metric = str(getattr(fact, "metric", "") or "")
        fact_id = str(getattr(fact, "normalized_fact_id", "") or "")
        value = getattr(fact, "value", None)
        period_key = str(getattr(fact, "period_key", "") or "")
        if not metric or not fact_id or value is None or not period_key:
            return None
        scope = getattr(fact, "scope", None)
        version = getattr(fact, "version", None)
        bbox = getattr(fact, "bbox", None)
        return Disclosure(
            metric=metric,
            period_key=period_key,
            value=float(value),
            fact_id=fact_id,
            document_id=str(getattr(fact, "document_id", "") or ""),
            scope=str(getattr(scope, "value", scope) or "") or None,
            page=getattr(fact, "page", None),
            version=str(getattr(version, "value", version) or "") or Version.AS_REPORTED.value,
            unit=canonical_unit(getattr(fact, "source_unit", None)) or None,
            available_at=getattr(fact, "available_at", None),
            source_table=getattr(fact, "source_table", None),
            statement_type=getattr(fact, "statement_type", None),
            raw_value=getattr(fact, "raw_value", None),
            rank=_rank_of(fact, bbox),
        )

    # -- queries ------------------------------------------------------------
    @property
    def disclosures(self) -> List[Disclosure]:
        return list(self._disclosures)

    def scoped(self, document_ids: Optional[Iterable[str]]) -> "FactIndex":
        """A view limited to some documents — the ones a claim actually cites.

        Needed because a real reference set holds several filings at once. Measured
        failure without it: with ``annual_04`` and ``annual_05`` both loaded, a
        claim citing only ``annual_04`` had its expected value and its recomputed
        growth taken from ``annual_05`` — a different company's numbers, chosen by
        a tie-break on document order. Every "expected" the checker prints has to
        come from the material the sentence cites.
        """
        ids = {str(item) for item in (document_ids or ()) if item}
        if not ids:
            return self
        view = FactIndex.__new__(FactIndex)
        view._disclosures = [item for item in self._disclosures if item.document_id in ids]
        return view

    def metrics(self) -> List[str]:
        return sorted({item.metric for item in self._disclosures})

    def periods(self, metric: str, *, scope: Optional[str] = None) -> List[str]:
        return sorted({
            item.period_key for item in self._disclosures
            if item.metric == metric and (scope is None or item.scope == scope)
        })

    def scopes(self, metric: str, *, period_key: Optional[str] = None) -> List[str]:
        return sorted({
            str(item.scope or "") for item in self._disclosures
            if item.metric == metric
            and (period_key is None or item.period_key == period_key)
        })

    def candidates(
        self,
        metric: str,
        *,
        period_key: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> List[Disclosure]:
        return [
            item for item in self._disclosures
            if item.metric == metric
            and (period_key is None or item.period_key == period_key)
            and (scope is None or item.scope == scope)
        ]

    # -- version semantics --------------------------------------------------
    def _group_of(self, item: Disclosure) -> List[Disclosure]:
        """The disclosure group that defines supersession for *item*.

        **Granularity matters, and getting it wrong produced a false positive.**
        Measured on ``annual_04_restated_comparative.pdf``: ``revenue`` for
        ``2025FY`` appears three times — 1,100,000 in the 合并利润表 本期列
        (as-reported, and still current), plus 900,000 / 1,000,000 in the page-3
        「前期差错更正影响金额」 table's 调整前/调整后 columns. Grouping by
        ``(metric, period, scope)`` alone makes the page-3 RESTATED column the
        "newest version" of the *period*, which then marks the untouched 合并利润表
        figure as superseded and reports a correct sentence as an error.

        A version column is a **table-level** structure: a replacement is
        established by a 调整前/调整后 pair printed side by side. Two disclosures in
        different tables, or in different documents, are not a replacement — they
        are duplicates or a cross-source conflict, and conflicts belong to CARD-13
        rather than to a rule that would have to guess which one wins.
        """
        return [
            other for other in self._disclosures
            if other.metric == item.metric
            and other.period_key == item.period_key
            and other.scope == item.scope
            and other.document_id == item.document_id
            and other.source_table == item.source_table
        ]

    def newest_in_group(self, item: Disclosure) -> Optional[str]:
        return newest_version([other.version for other in self._group_of(item)])

    def is_superseded(self, item: Disclosure) -> bool:
        """Whether a *newer version of the same table cell* exists."""
        newest = self.newest_in_group(item)
        return bool(newest) and item.version != newest

    def primary(
        self, metric: str, *, period_key: Optional[str] = None, scope: Optional[str] = None
    ) -> Optional[Disclosure]:
        """The disclosure that wins among equals — CARD-02's ``pick_primary`` order.

        Superseded entries are excluded first: a restatement's whole point is that
        the replaced number is no longer the right one to quote, so it must not be
        the value the checker tells the author to use. When *every* candidate is
        superseded the excluded ones come back, because a stale value is still
        better than telling the author nothing.
        """
        pool = self.candidates(metric, period_key=period_key, scope=scope)
        if not pool:
            return None
        live = [item for item in pool if not self.is_superseded(item)]
        return sorted(live or pool, key=lambda item: (item.rank, item.fact_id))[0]

    def documents_for(self, metric: str, *, period_key: Optional[str] = None) -> List[str]:
        return sorted({
            item.document_id for item in self.candidates(metric, period_key=period_key)
            if item.document_id
        })

    # -- matching -----------------------------------------------------------
    def match(
        self,
        metric: str,
        value: float,
        *,
        period_key: Optional[str] = None,
        scope: Optional[str] = None,
        tolerance: float = 0.0,
    ) -> MatchOutcome:
        """Compare one claimed figure (already in base units) to the disclosure set.

        ``period_key`` / ``scope`` are *constraints the draft declared*; ``None``
        means the sentence declared nothing and no constraint is imposed. An
        undeclared period is therefore never a ``period`` mismatch, and an
        undeclared 口径 is never a ``scope`` mismatch — imposing one would build an
        error on an inference the checker made rather than on the draft (§9).

        Precedence, and why each step sits where it does:

        1. **exact** — the figure equals a disclosure that is still current. If the
           figure matches *only* a superseded version, ``newest_version`` says so
           and step 2 reports it instead; quoting a number that a restatement
           replaced is a real error that looks identical to a correct one by value
           alone (§8 edge case 4).
        2. **superseded** — matched an older version of the *same* figure.
        3. **unit** — nothing matched, but the magnitude is off by exactly one rung
           of the 元/千元/万元/百万元/亿元 ladder. Report ``unit_error``, never
           ``numeric_error`` (§7.2): the number is right and the unit word is
           wrong, and calling that "the number is wrong" would send the author
           looking in the wrong place.
        4. **period** — matched a disclosure of the same metric+scope under a
           different period.
        5. **scope** — matched a disclosure of the same metric+period under the
           other 口径 (合并 vs 母公司).
        6. **absent** — none of the above: the figure is not in the disclosure set.
        """
        everything = self.candidates(metric)
        if not everything:
            return MatchOutcome("unverifiable", detail=f"事实库中没有指标 {metric} 的任何披露")

        # ``period_key=None`` means the draft declared **no** period, so none is
        # imposed — not "the period is None and therefore nothing matches". The
        # measured failure of reading it the other way: 「营业成本为70万元」 (no year
        # written) matched the 2025FY disclosure by value and was reported as
        # ``period_error``, i.e. §9's cardinal sin — an error resting on the
        # assumption that an omitted period meant a different one. A value that is
        # disclosed somewhere is not wrong for want of a date.
        same_figure = [
            item for item in everything
            if (period_key is None or item.period_key == period_key)
            and (scope is None or item.scope == scope)
        ]

        # 1 / 2 — exact, then superseded.
        hits = [item for item in same_figure if _close(item.value, value, tolerance)]
        if hits:
            current = [item for item in hits if not self.is_superseded(item)]
            if current:
                return MatchOutcome("exact", disclosure=_primary_of(current), alternatives=tuple(hits))
            replacements = [
                other
                for item in hits
                for other in self._group_of(item)
                if other.version == self.newest_in_group(item)
            ]
            return MatchOutcome(
                "superseded",
                disclosure=_primary_of(hits),
                alternatives=tuple(same_figure),
                replacement=_primary_of(replacements),
                detail=(
                    f"命中 {hits[0].version} 版本，但同一张表的该行另有更新版本披露；"
                    "引用了被取代的旧值。"
                ),
            )

        # 3 — unit ladder.
        for pool, label in ((same_figure, "同期"), (everything, "其他期间")):
            scaled = _ladder_hit(pool, value, tolerance)
            if scaled is not None:
                return MatchOutcome(
                    "unit",
                    disclosure=scaled,
                    detail=(
                        f"数值与{label}披露相差 {_describe_ratio(scaled.value, value)}，"
                        "属单位声明错误而非数值错误。"
                    ),
                )

        # 4 — wrong period, same metric. Only reachable when a period *was*
        # declared; with ``period_key=None`` the figure would have matched at step
        # 1 already, so there is nothing to call a mismatch.
        other_period = [
            item for item in everything
            if period_key is not None and item.period_key != period_key
            and _close(item.value, value, tolerance)
        ]
        if other_period:
            return MatchOutcome(
                "period",
                disclosure=_primary_of(other_period),
                alternatives=tuple(other_period),
                detail=f"该数值出现在期间 {other_period[0].period_key}，与草稿声明的期间不符。",
            )

        # 5 — wrong scope, same metric+period.
        other_scope = [
            item for item in everything
            if item.period_key == period_key and scope is not None and item.scope != scope
            and _close(item.value, value, tolerance)
        ]
        if other_scope:
            return MatchOutcome(
                "scope",
                disclosure=_primary_of(other_scope),
                alternatives=tuple(other_scope),
                detail=f"该数值来自 {other_scope[0].scope} 口径，与草稿声明的口径不符。",
            )

        # 6 — absent. ``expected`` is the primary of the same figure when we have
        # one; otherwise the most recent disclosure of that metric.
        fallback = _primary_of(same_figure) or self.primary(metric)
        if fallback is None:
            return MatchOutcome("unverifiable", detail=f"事实库中没有指标 {metric} 的任何披露")
        return MatchOutcome(
            "absent",
            disclosure=fallback,
            alternatives=tuple(same_figure or everything),
            detail="草稿数值与事实库中该指标的任何披露都不符。",
        )


def _rank_of(fact: object, bbox: object) -> Tuple[int, int, int, int]:
    """Document-order tiebreak: page, row, column (bbox vertical as last resort)."""
    page = getattr(fact, "page", None)
    row = getattr(fact, "row", None)
    column = getattr(fact, "column", None)
    y = bbox[1] if isinstance(bbox, (tuple, list)) and len(bbox) == 4 else 0.0
    return (
        page if isinstance(page, int) else 999,
        row if isinstance(row, int) else 999,
        column if isinstance(column, int) else 999,
        int(float(y) * 1000) if isinstance(y, (int, float)) else 0,
    )


def _primary_of(pool: Sequence[Disclosure]) -> Optional[Disclosure]:
    if not pool:
        return None
    return sorted(pool, key=lambda item: (item.rank, item.fact_id))[0]


def _ladder_hit(
    pool: Sequence[Disclosure], value: float, tolerance: float
) -> Optional[Disclosure]:
    """The disclosure whose value is *value* times a unit-ladder ratio (or inverse)."""
    for item in pool:
        if not item.value:
            continue
        for ratio in _UNIT_RATIOS:
            target = item.value * ratio
            if _close(target, value, tolerance):
                return item
    return None


def _describe_ratio(expected: float, claimed: float) -> str:
    if claimed and expected:
        ratio = expected / claimed
        if ratio >= 1:
            return f"{ratio:g} 倍"
        return f"1/{1 / ratio:g}"
    return "一个数量级"


def format_amount(value: Optional[float], unit: Optional[str] = None, *, digits: int = 2) -> str:
    """Render a base-unit value **in its own declared unit**, with the unit word.

    Both halves matter: §10 rule 2 requires ``original`` and ``expected`` to be
    shown in the same unit and period, and an amount without its unit is exactly
    the ambiguity the whole card is about.
    """
    if value is None:
        return "—"
    token = canonical_unit(unit) if unit else None
    scale = unit_scale(token) if token else 1.0
    scaled = float(value) / scale
    if abs(scaled) >= 1000:
        text = f"{scaled:,.0f}"
    elif abs(scaled) >= 1:
        text = f"{scaled:,.{digits}f}"
    else:
        text = f"{scaled:,.{max(digits, 4)}f}"
    return f"{text}{token or '元'}"


def format_percent(value: Optional[float], *, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%"


# --------------------------------------------------------------------------- #
# citation resolution
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Citation:
    """A draft's reference resolved onto a registered document (and page)."""

    raw: str
    document_id: str
    page: Optional[int] = None
    filename: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "document_id": self.document_id,
            "page": self.page,
            "filename": self.filename,
        }


@dataclass
class ReferenceIndex:
    """Resolve a draft's citation strings to registered documents.

    Deterministic and total: every input either resolves or returns ``None``.
    No fuzzy matching — "close enough" here would silently bless a citation that
    points at nothing.
    """

    _ids: Dict[str, str] = field(default_factory=dict)       # lowercased alias -> document_id
    _names: Dict[str, Optional[str]] = field(default_factory=dict)
    _available: Dict[str, List[Optional[str]]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        facts: Sequence[object] = (),
        documents: Optional[Iterable[object]] = None,
    ) -> "ReferenceIndex":
        index = cls()
        for fact in facts:
            document_id = str(getattr(fact, "document_id", "") or "")
            if document_id:
                index._remember(document_id, None)
        for document in documents or ():
            document_id = str(getattr(document, "document_id", "") or "")
            if not document_id:
                continue
            filename = getattr(document, "filename", None)
            index._remember(document_id, str(filename) if filename else None)
        return index

    def _remember(self, document_id: str, filename: Optional[str]) -> None:
        self._ids.setdefault(document_id.lower(), document_id)
        if filename:
            self._ids.setdefault(filename.lower(), document_id)
            stem = filename.rsplit(".", 1)[0]
            self._ids.setdefault(stem.lower(), document_id)
            self._names.setdefault(document_id, filename)
        self._names.setdefault(document_id, filename)
        self._available.setdefault(document_id, [])

    def note_available_at(self, document_id: str, value: Optional[str]) -> None:
        self._available.setdefault(document_id, []).append(value)

    def document_ids(self) -> List[str]:
        return sorted(self._names)

    def filename_of(self, document_id: str) -> Optional[str]:
        return self._names.get(document_id)

    def resolve(self, reference: object) -> Optional[Citation]:
        raw = str(reference or "").strip()
        if not raw:
            return None
        token, page = _split_page(raw)
        document_id = self._ids.get(token.lower())
        if document_id is None:
            return None
        return Citation(
            raw=raw, document_id=document_id, page=page, filename=self._names.get(document_id)
        )


def _split_page(reference: str) -> Tuple[str, Optional[int]]:
    text = reference.strip()
    for pattern in _PAGE_MARKERS:
        match = pattern.search(text)
        if match:
            return text[: match.start()].strip(), int(match.group(1))
    return text, None


__all__ = [
    "Citation",
    "Disclosure",
    "FactIndex",
    "MatchOutcome",
    "ReferenceIndex",
    "format_amount",
    "format_percent",
]
