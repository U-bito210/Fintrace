"""A deterministic extractor — the offline path through the same boundary.

**Why this exists (documented deviation, see CARD-05 §16).** §6 frames
``extract_claims`` as the one place a model is allowed. That stays true for the
*production* path (:mod:`src.cn.checker.extractor`). But §15 B also asks for an
end-to-end run on a natural-language draft, and §11/§13 want the checker scored
without spending tokens, both of which a model-only extractor makes impossible
offline. Rather than weaken "全部测试离线通过", this module extracts the same
literals with regular expressions.

It is a **transport** implementation, not a second checker: it produces
:class:`DraftClaim` and stops. It never reads the fact base, never decides right
from wrong, and it announces itself — every claim it makes carries
``extractor="rules"``, so nothing downstream can mistake it for a model's output.
The card's structural rule (§10: ``checker.py`` imports no model client) is
unaffected: the two extractors are siblings, and only one of them talks to a model.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from ..normalization.metrics import STANDARD_METRICS, is_known_metric, resolve_metric
from ..normalization.models import Scope
from ..normalization.units import UNIT_SCALES, canonical_unit, parse_numeric
from .checker import is_forward_looking
from .extractor import EXTRACTOR_LLM, ExtractionOutcome, resolve_period_label
from .models import ClaimType, DraftClaim
from .sentences import DraftSentence

EXTRACTOR_RULES = "rules"

#: Longest-first so 「归属于母公司股东的净利润」 is never read as 「净利润」.
_METRIC_SYNONYMS: Tuple[str, ...] = tuple(sorted(
    {synonym for spec in STANDARD_METRICS for synonym in spec.synonyms},
    key=len,
    reverse=True,
))

_UNIT_WORDS = "|".join(
    sorted((re.escape(word) for word in UNIT_SCALES), key=len, reverse=True)
)

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_VALUE_RE = re.compile(rf"(?P<num>{_NUMBER})\s*(?P<unit>{_UNIT_WORDS})")
_GROWTH_RE = re.compile(
    r"同比(?P<direction>增长|上升|增加|提高|下降|减少|下滑|降低)?[^%\d]{0,8}?"
    rf"(?P<num>{_NUMBER})\s*%"
)
_PERIOD_RES = (
    (re.compile(r"(\d{4})\s*年?\s*上半年"), "H1"),
    (re.compile(r"(\d{4})\s*年?\s*前三季度"), "9M"),
    (re.compile(r"(\d{4})\s*年?\s*第一季度"), "Q1"),
    (re.compile(r"(\d{4})\s*年?\s*(?:一季报|Q1)"), "Q1"),
    (re.compile(r"(\d{4})\s*年度?"), "FY"),
)
_REFERENCE_RES = (
    re.compile(r"[（(]\s*(?:来源|见|参见|详见)\s*[:：]?\s*(?P<ref>[^）)]+)[）)]"),
    re.compile(r"(?:来源|见|参见|详见)\s*[:：]?\s*(?P<ref>(?:SYN|DOC|doc)[\w\-]*(?:\.pdf)?(?:#p\d+|第\d+页)?)"),
)
_PAGE_IN_REF_RE = re.compile(r"第\s*(\d+)\s*页")
_MULTIPLE_RE = re.compile(
    r"(?:(?P<label1>市盈率|市净率|PE|P/E|PB|P/B)\s*(?:为|是|约|达)?\s*[:：]?\s*(?P<num1>\d+(?:\.\d+)?))"
    r"|(?:(?P<num2>\d+(?:\.\d+)?)\s*倍\s*(?:的)?\s*(?P<label2>市盈率|市净率|PE|P/E|PB|P/B))",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"[，,]?\s*(?P<antecedent>(?:因|由于|受|受益于)[^，,。；;]{2,40}?)[，,]?\s*"
    r"(?P<consequent>[^。；;]{0,6}?(?:导致|使得|致使|推动|带动|拖累|拉动)[^。；;]{2,60})"
)

#: Verbs that flip the sign of a growth figure.
_NEGATIVE_DIRECTIONS = {"下降", "减少", "下滑", "降低"}

#: A 口径 word that is **qualified as a 口径** — 「母公司报表」「合并口径」. Used on its
#: own this would already be too loose, and it is never used on its own: the second
#: tier below requires the scope word to sit directly in front of the metric.
_SCOPE_QUALIFIED_RE = re.compile(r"(母公司|合并)\s*(?:报表|口径)")


def extract_claims_rules(sentences: Sequence[DraftSentence]) -> ExtractionOutcome:
    """Deterministic twin of :func:`src.cn.checker.extractor.extract_claims`.

    A sentence that carries no figure is recorded in ``no_claim``, not in
    ``unextracted``: the pattern extractor did read it and there was genuinely
    nothing to move. Only a sentence that *looks* like it carries a figure but
    yields no usable claim would be an extraction failure here, and even then the
    claim is emitted with an explicit ``reason`` so the gap is visible.
    """
    sentences = list(sentences or ())
    outcome = ExtractionOutcome(extractor=EXTRACTOR_RULES)
    for sentence in sentences:
        claim = _claim_from_sentence(sentence)
        if claim is None:
            outcome.no_claim.append(sentence)
        else:
            outcome.claims.append(claim)
    if outcome.no_claim:
        outcome.notes.append(
            f"{len(outcome.no_claim)} 句不含可核查的显式数值，未生成 claim"
            "（纯叙述句，不代表核查失败）。"
        )
    return outcome


def _claim_from_sentence(sentence: DraftSentence) -> Optional[DraftClaim]:
    text = sentence.text
    metric_label = _find_metric(text)
    value_match = _VALUE_RE.search(text)
    growth_match = _GROWTH_RE.search(text)
    period = _find_period(text)
    multiple = _find_multiple(text)
    causal = _CAUSAL_RE.search(text)
    forward = is_forward_looking(text)

    has_figure = bool(value_match or growth_match or multiple)
    if not has_figure and not causal:
        return None

    reasons: List[str] = []
    metric = None
    if metric_label:
        if is_known_metric(metric_label):
            metric = metric_label
        else:
            match = resolve_metric(metric_label)
            if match.known:
                metric = match.key
            else:
                reasons.append(f"指标 {metric_label!r} 不在标准指标字典内，已留空。")
    elif has_figure:
        reasons.append("句中出现数值但未识别到指标名，无法认定指标。")

    if value_match and not period:
        reasons.append("句中出现数值但未声明期间。")

    value_literal = None
    unit = None
    value = None
    if value_match:
        value_literal = value_match.group("num")
        value = parse_numeric(value_literal)
        unit = canonical_unit(value_match.group("unit"))

    growth_literal = None
    growth = None
    if growth_match:
        growth_literal = growth_match.group("num")
        growth = parse_numeric(growth_literal)
        if growth is not None and growth_match.group("direction") in _NEGATIVE_DIRECTIONS:
            growth = -abs(growth)

    claim_type = ClaimType.FACTUAL.value
    if forward:
        claim_type = ClaimType.FORWARD_LOOKING.value
    elif causal:
        claim_type = ClaimType.CAUSAL.value
    elif multiple:
        claim_type = ClaimType.VALUATION.value

    reference = _find_reference(text)
    return DraftClaim(
        claim_id=f"{sentence.sentence_id}-c1",
        sentence=text,
        page=sentence.page,
        metric=metric,
        metric_label=metric_label,
        period=period,
        value=value,
        value_literal=value_literal,
        unit=unit,
        growth=growth,
        growth_literal=growth_literal,
        valuation_multiple=multiple,
        source_reference=reference,
        claim_type=claim_type,
        scope=_find_scope(text, metric_label),
        source_references=(reference,) if reference else (),
        causal_antecedent=causal.group("antecedent").strip() if causal else None,
        causal_consequent=causal.group("consequent").strip() if causal else None,
        document_id=sentence.document_id,
        fact_id=sentence.fact_id,
        extractor=EXTRACTOR_RULES,
        reason="；".join(reasons) or None,
    )


def _find_metric(text: str) -> Optional[str]:
    for synonym in _METRIC_SYNONYMS:
        if len(synonym) >= 2 and synonym in text:
            return synonym
    return None


def _find_period(text: str) -> Optional[str]:
    """The period the sentence names, resolved by the *shared* draft-layer policy.

    The table decides whether 「前三季度」 means 9M or FY; the canonical key comes from
    :func:`src.cn.checker.extractor.resolve_period_label`, so a bare 「2025年」 resolves
    the same way here as it does on the model path. Two resolvers would eventually
    disagree about what a period means, and the disagreement would show up as a
    ``period_error`` against a draft that is correct.
    """
    for pattern, kind in _PERIOD_RES:
        match = pattern.search(text)
        if not match:
            continue
        period, _note = resolve_period_label(f"{match.group(1)}{kind}")
        if period:
            return period
    return None


def _find_multiple(text: str) -> Optional[str]:
    match = _MULTIPLE_RE.search(text)
    if not match:
        return None
    label = match.group("label1") or match.group("label2") or ""
    number = match.group("num1") or match.group("num2") or ""
    return f"{label} {number}倍".strip()


def _find_scope(text: str, metric_label: Optional[str]) -> Optional[str]:
    """The 口径 the sentence itself declares, or ``None`` when it declares none.

    §5 has no ``scope`` field, yet §7.1 asks for ``scope_error``. Without this the
    rule can only ever fire off a CARD-04 signal, and a draft that silently writes
    母公司 while quoting the 合并 figure — the classic A-share error — would pass.

    **Two tiers, both deliberately narrow, because a wrong 口径 is a false error.**
    A bare 「合并」 appears in ordinary prose (「合并计算」「合并范围」), and treating
    any occurrence as a declaration would let the checker then report a mismatch
    against a 口径 the author never claimed. So either:

    1. the word is *qualified* as a 口径 — 「母公司报表」「合并口径」; or
    2. it sits immediately in front of the metric — 「母公司营业收入」.

    Reading 「归属于母公司股东的净利润」 correctly yields ``None``: tier 2 anchors the
    scope word directly on the metric label, so a 母公司 that is part of the metric's
    own name cannot be mistaken for a declared 口径.
    """
    match = _SCOPE_QUALIFIED_RE.search(text)
    if match:
        scope = Scope.coerce(match.group(1))
        if scope is not None:
            return scope.value
    if metric_label:
        anchored = re.search(
            rf"(母公司|合并)\s*(?:的\s*)?{re.escape(metric_label)}", text
        )
        if anchored:
            scope = Scope.coerce(anchored.group(1))
            if scope is not None:
                return scope.value
    return None


def _find_reference(text: str) -> Optional[str]:
    for pattern in _REFERENCE_RES:
        match = pattern.search(text)
        if not match:
            continue
        ref = match.group("ref").strip().strip("，,。；;")
        page = _PAGE_IN_REF_RE.search(ref)
        if page:
            ref = f"{_PAGE_IN_REF_RE.sub('', ref).strip()}#p{page.group(1)}"
        return ref or None
    return None


__all__ = ["EXTRACTOR_RULES", "extract_claims_rules", "EXTRACTOR_LLM"]
