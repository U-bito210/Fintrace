"""CARD-22 §7 F6 — Cross-modal Constraints (正文 / 表格 / 图表).

§7 F6's example is the whole specification:

    正文写「毛利率同比下降 2.1 个百分点」、表格算 33.4% → 31.2% = −2.2pct、
    图表标注 −2.2pct → 检出不一致

and its prohibition is just as important: *"不能直接判断正文一定错误"*. So the
output is a :class:`~src.cn.acme.models.CrossModalInconsistency` that names all
three parties, the computed benchmark, and the conflict type — never a verdict.

Two design choices make this checkable offline and non-speculative:

* **The benchmark is computed by the same deterministic definition everywhere.**
  A ratio (毛利率 / 净利率) is derived from CARD-02's canonical facts by the table
  in :data:`COMPUTABLE_RATIOS`; the body-text side is read with a regex over
  CARD-01 text facts. Both end up in *percent / percentage-point space*, which is
  where the tolerance policy's ``PERCENT`` band (0.05pct) applies.
* **A missing third party is ``N/A``, not a guess** (§12 edge case 6): our corpus
  has no chart layer yet, so a two-party comparison is the shipped path and the
  chart slot reads ``N/A`` rather than being silently dropped or invented.

Claims whose benchmark cannot be computed are *not* reported as inconsistencies —
that would assert a conflict type we have not earned. They are returned in the
``notes`` out-parameter so the report can carry them as warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization.models import NormalizedFact
from ..normalization.units import parse_numeric
from .models import (
    CONFLICT_VALUE_MISMATCH,
    MISSING_PARTY,
    STATUS_VIOLATED,
    CrossModalInconsistency,
    FAMILY_CROSS_MODAL,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy

CONSTRAINT_CROSS_MODAL_RATIO = "cross_modal_ratio_consistency"

#: Ratio definitions, as data: ``metric -> (numerator, denominator, minus)``.
#: ``minus`` is the metric subtracted from the numerator first (毛利率's cost).
COMPUTABLE_RATIOS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "gross_margin": ("revenue", "revenue", "operating_cost"),
    "net_margin": ("net_profit_parent", "revenue", None),
}

#: 正文 labels that name each computable ratio. Kept beside the definition so a
#: new ratio cannot be added without stating how the prose spells it.
RATIO_LABELS: Dict[str, str] = {
    "毛利率": "gross_margin",
    "销售毛利率": "gross_margin",
    "净利率": "net_margin",
    "销售净利率": "net_margin",
}

#: Reverse map, shortest label first — ``毛利率`` reads better than ``销售毛利率``
#: in a finding, and building it by comprehension would have let the *longer*
#: synonym win simply by being declared later.
RATIO_LABELS_REVERSE: Dict[str, str] = {
    metric: min(
        (name for name, key in RATIO_LABELS.items() if key == metric),
        key=len,
    )
    for metric in set(RATIO_LABELS.values())
}

KIND_LEVEL = "level"
KIND_DELTA = "delta"

_UP_WORDS = ("上升", "提高", "增加", "增长", "提升")
_DOWN_WORDS = ("下降", "降低", "减少", "下滑", "回落")
_COMPARATIVE_WORDS = ("同比", "环比", "较上年", "较上期", "相比", "对比")

_SENTENCE_SPLIT = re.compile(r"[。；;！!？?\n]")
_CLAUSE_SPLIT = re.compile(r"[，,、]")
_PCT_RE = re.compile(
    r"(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>个百分点|pct|%|％)"
)
_PERIOD_YEAR_RE = re.compile(r"(?P<year>\d{4})\s*年")

#: Words that make a ``%`` figure a *level* ("为 33.0%") rather than an
#: ambiguous relative change.
_LEVEL_WORDS = ("为", "达", "至", "报", "是")


@dataclass(frozen=True)
class ChartAnnotation:
    """A value read off a chart (§7 F6's third party).

    Our corpus carries no chart layer yet, so no producer exists in-tree; the
    type is here because the *contract* must be able to express the third party
    the moment one appears (OCR of an embedded chart, or an annotation table).
    Filling the chart slot by hand would be fabrication.
    """

    metric: str
    kind: str
    value: float
    unit: str = "个百分点"
    direction: Optional[str] = None
    label: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    quote: Optional[str] = None


@dataclass(frozen=True)
class BodyClaim:
    """One quantitative statement read out of the prose."""

    metric: str
    label: str
    kind: str
    value: float                      # percent space
    direction: Optional[str]
    quote: str
    fact_id: str
    page: Optional[int]
    bbox: Optional[Tuple[float, float, float, float]]

    @property
    def signed(self) -> float:
        if self.kind == KIND_DELTA and self.direction == "down":
            return -abs(self.value)
        if self.kind == KIND_DELTA and self.direction == "up":
            return abs(self.value)
        return self.value


def _direction(text: str) -> Optional[str]:
    for word in _DOWN_WORDS:
        if word in text:
            return "down"
    for word in _UP_WORDS:
        if word in text:
            return "up"
    return None


def parse_body_claims(
    facts: Iterable[object], *, notes: Optional[List[str]] = None
) -> List[BodyClaim]:
    """Every 毛利率/净利率 quantity in the prose, with its verbatim quote.

    Split by **clause**, not by sentence. One sentence routinely carries two
    different quantities —「毛利率为 33.0%，较上年同期下降 1.2 个百分点」— and
    reading a single number off the whole sentence silently swapped the change
    figure for the level (caught by the CARD-22 probe before it shipped). Each
    clause is judged on its own, inheriting the sentence's ratio label so
    「较上年同期下降 1.2 个百分点」 still knows it is about 毛利率.

    A ``%`` figure that is neither a 个百分点 change nor introduced by a level
    word ("毛利率同比下降 5%") is **ambiguous** — relative change and absolute
    change are different quantities — so it is skipped with a note rather than
    guessed (§零静默、零猜测).
    """
    claims: List[BodyClaim] = []
    for fact in facts:
        if getattr(fact, "kind", None) != "text":
            continue
        text = str(getattr(fact, "raw_text", "") or "")
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            if not sentence:
                continue
            label = next((name for name in RATIO_LABELS if name in sentence), None)
            if label is None:
                continue
            comparative = any(word in sentence for word in _COMPARATIVE_WORDS)
            for clause in _CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                if not clause:
                    continue
                match = _PCT_RE.search(clause)
                if match is None:
                    continue
                value = parse_numeric(match.group("num"))
                if value is None:
                    continue
                unit = match.group("unit")
                direction = _direction(clause) or _direction(sentence)
                is_points = unit == "个百分点"
                has_level_word = any(word in clause for word in _LEVEL_WORDS)
                if is_points:
                    kind = KIND_DELTA
                elif has_level_word:
                    kind = KIND_LEVEL
                elif comparative and direction is not None:
                    if notes is not None:
                        notes.append(
                            f"跨模态：「{clause[:30]}」的百分比既未写「个百分点」也无"
                            "「为/达/至」等水平词，相对变动与绝对变动语义不可判定，已跳过"
                        )
                    continue
                else:
                    kind = KIND_LEVEL
                claims.append(
                    BodyClaim(
                        metric=RATIO_LABELS[label],
                        label=label,
                        kind=kind,
                        value=float(value),
                        direction=direction if kind == KIND_DELTA else None,
                        quote=clause,
                        fact_id=str(getattr(fact, "fact_id", "") or ""),
                        page=getattr(fact, "page", None),
                        bbox=getattr(fact, "bbox", None),
                    )
                )
    return claims


def ratio_series(
    facts: Sequence[NormalizedFact],
) -> Dict[str, List[Tuple[str, float, str, Optional[str]]]]:
    """``metric -> [(period_key, ratio_in_percent, scope, version)]`` ascending.

    Only groups that are internally comparable take part: one ``(scope, version,
    currency)`` per metric, matching CARD-02's own rule that a mixed 口径 group
    must never be differenced.
    """
    buckets: Dict[Tuple[str, str, str, str, str], Dict[str, NormalizedFact]] = {}
    for fact in facts:
        for metric in COMPUTABLE_RATIOS:
            if fact.metric not in _operands_of(metric):
                continue
            # Period is part of the bucket key. Leaving it out collapsed every
            # period onto one entry per metric, so a two-period comparison saw a
            # single point and silently reported "no benchmark" (or compared the
            # prose against the wrong year) — caught by the CARD-22 probe.
            key = (
                metric,
                fact.period_key,
                fact.scope.value if fact.scope else "",
                fact.version.value,
                str(fact.currency),
            )
            buckets.setdefault(key, {})[fact.metric] = fact

    out: Dict[str, List[Tuple[str, float, str, Optional[str]]]] = {}
    for (metric, period_key, scope, version, _currency), operands in buckets.items():
        ratio = _ratio(metric, operands)
        if ratio is None:
            continue
        out.setdefault(metric, []).append(
            (period_key, ratio, scope, version or None)
        )
    for metric in out:
        out[metric].sort()
    return out


def _operands_of(metric: str) -> Tuple[str, ...]:
    numerator, denominator, minus = COMPUTABLE_RATIOS[metric]
    names = {numerator, denominator}
    if minus:
        names.add(minus)
    return tuple(sorted(names))


def _ratio(metric: str, operands: Dict[str, NormalizedFact]) -> Optional[float]:
    numerator_key, denominator_key, minus_key = COMPUTABLE_RATIOS[metric]
    numerator = operands.get(numerator_key)
    denominator = operands.get(denominator_key)
    if numerator is None or denominator is None or denominator.value == 0:
        return None
    value = numerator.value
    if minus_key:
        minus = operands.get(minus_key)
        if minus is None:
            return None
        value = value - minus.value
    return value / denominator.value * 100.0


def _computed_benchmark(
    metric: str, kind: str, series: Dict[str, List[Tuple[str, float, str, Optional[str]]]]
) -> Optional[Tuple[float, Tuple[str, str, str]]]:
    """``(value_in_percent_space, (period_a, period_b, note))`` or ``None``."""
    points = series.get(metric) or []
    if kind == KIND_LEVEL:
        if not points:
            return None
        period, value, _scope, _version = points[-1]
        return value, (period, "", "期末水平")
    if len(points) < 2:
        return None
    # Two most recent *different* periods of the same group.
    (period_a, value_a, scope, version) = points[-2]
    (period_b, value_b, _s2, _v2) = points[-1]
    return value_b - value_a, (period_b, period_a, f"{scope or '口径未声明'}")


def _source_ref(claim: BodyClaim, document_id: str) -> SourceRef:
    return SourceRef(
        document_id=document_id or None,
        page=claim.page,
        bbox=claim.bbox,
        locator=f"正文：{claim.quote[:40]}",
    )


def check_cross_modal(
    facts: Sequence[NormalizedFact],
    *,
    policy: TolerancePolicy,
    chart_annotations: Sequence[ChartAnnotation] = (),
    text_facts: Sequence[object] = (),
    document_id: str = "",
    notes: Optional[List[str]] = None,
) -> List[CrossModalInconsistency]:
    """§7 F6 — compare 正文 / 表格 / 图表 for each computable ratio statement."""
    claims = parse_body_claims(text_facts, notes=notes)
    series = ratio_series(facts)
    charts: Dict[str, List[ChartAnnotation]] = {}
    for annotation in chart_annotations:
        charts.setdefault(annotation.metric, []).append(annotation)

    results: List[CrossModalInconsistency] = []
    for claim in claims:
        benchmark = _computed_benchmark(claim.metric, claim.kind, series)
        chart = _pick_chart(charts.get(claim.metric, []), claim)
        if benchmark is None and chart is None:
            if notes is not None:
                notes.append(
                    f"跨模态：正文「{claim.quote[:40]}」缺少可计算基准"
                    f"（{RATIO_LABELS_REVERSE.get(claim.metric, claim.metric)} "
                    "的分子/分母未同时披露），未做比对，如实记录"
                )
            continue
        computed, periods = benchmark if benchmark is not None else (None, ("", "", ""))
        body_value = claim.signed
        chart_value = chart.value if chart is not None else None
        status, tolerance, residual = policy.classify(
            computed, body_value, constraint_id=CONSTRAINT_CROSS_MODAL_RATIO, unit="PERCENT"
        )
        # 「容差内差异不误报」: only a residual *wider* than the declared band is a
        # cross-modal finding. A warning-level difference is, by the policy's own
        # definition, still agreement.
        agree_body = computed is None or status != STATUS_VIOLATED
        agree_chart = chart_value is None or (
            computed is None
            or abs(policy.residual(computed, chart_value) or 0.0)
            <= policy.tolerance_for(
                computed, chart_value, constraint_id=CONSTRAINT_CROSS_MODAL_RATIO,
                unit="PERCENT",
            )
        )
        if agree_body and agree_chart:
            continue
        values = {
            "body": body_value,
            "table": computed,
            "chart": chart_value if chart_value is not None else MISSING_PARTY,
        }
        unit_label = "个百分点" if claim.kind == KIND_DELTA else "%"
        stage = "变动" if claim.kind == KIND_DELTA else "水平"
        ratio_label = RATIO_LABELS_REVERSE.get(claim.metric, claim.metric)
        if computed is None:
            note = f"{ratio_label}{stage}：正文 {body_value:+.2f}{unit_label}，表格无法计算基准值"
        else:
            scope_note = f"（{periods[1]} → {periods[0]}）" if periods[1] else f"（{periods[0]}）"
            note = (
                f"{ratio_label}{stage}：正文 {body_value:+.2f}{unit_label}，"
                f"表格计算 {computed:+.2f}{unit_label}{scope_note}，"
                f"容差 {tolerance:g}；ACME 只记录三方取值，不判定正文或表格谁错"
            )
        results.append(
            CrossModalInconsistency(
                inconsistency_id=make_violation_id(
                    CONSTRAINT_CROSS_MODAL_RATIO, FAMILY_CROSS_MODAL,
                    (claim.metric, claim.kind, claim.fact_id, str(body_value), str(computed),
                     str(chart_value)),
                ),
                metric=claim.metric,
                values=values,
                computed_value=computed,
                conflict_type=CONFLICT_VALUE_MISMATCH,
                source_refs=(
                    _source_ref(claim, document_id),
                    SourceRef(
                        document_id=document_id or None,
                        locator=(
                            f"表格计算 {claim.metric}"
                            + (f"（{periods[1]} → {periods[0]}）" if periods[1] else f"（{periods[0]}）")
                        ),
                    ),
                ),
                quote=claim.quote,
                note=note,
            )
        )
    return results


def _pick_chart(
    annotations: Sequence[ChartAnnotation], claim: BodyClaim
) -> Optional[ChartAnnotation]:
    for annotation in annotations:
        if annotation.metric != claim.metric:
            continue
        if annotation.kind != claim.kind:
            continue
        if claim.kind == KIND_DELTA and annotation.direction not in (None, claim.direction):
            continue
        return annotation
    return None
