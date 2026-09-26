"""CARD-05 deterministic checker: 13 rule classes, zero model involvement.

**This module imports no LLM client, and a test asserts it.** The card's §10 makes
that a structural requirement, not a stylistic one: the whole value of CARD-05 is
that "the number is wrong" is a statement a reader can audit, and a rule that a
model could have influenced is not auditable.

Design rules taken from the card and applied literally:

* **§9 — 误报比漏报更伤可信度.** Every rule is written to prefer silence over a
  wrong accusation. Three places where that shows up: a metric with no disclosure
  anywhere is reported as *无法核验* (a warning) rather than as an error (§8.3); a
  figure with no declared unit is never failed, because the failure would rest on
  an assumption we made rather than on the filing; and a match against *any*
  disclosed value counts as correct even when another column of the same figure
  disagrees.
* **§7.2 — unit discipline before value judgement.** A magnitude that is exactly a
  rung of the 元/千元/万元/百万元/亿元 ladder away is a ``unit_error``. Calling it
  ``numeric_error`` would be technically defensible and practically useless.
* **§8.4 — version before value.** A figure matching a superseded disclosure is
  ``revision_superseded`` even though the number itself is "right".
* **§7.3 — confidence is a constant table.** No rule asks a model how sure it is.
* **§7.1 — recomputation goes through CARD-03.** Growth is re-derived by calling
  ``compute_yoy``, so the checker and the analysis engine can never disagree about
  what a percentage should be.

Order of work per claim: citations first (they are about provenance), then the
figure, then growth, then valuation multiples, then causality. Findings are
emitted in a fixed order so the report is byte-stable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..analysis.calculations import CalculationRegistry
from ..analysis.comparisons import compute_yoy
from ..analysis.series import previous_year_key
from ..industries import L1_INDUSTRIES, get_industry_code
from ..normalization.units import looks_like_percent, unit_scale
from .facts import (
    Citation,
    Disclosure,
    FactIndex,
    MatchOutcome,
    ReferenceIndex,
    format_amount,
    format_percent,
)
from .models import (
    CHECK_TYPE_LABELS,
    CHECK_TYPE_ORDER,
    CLAIM_TYPE_MAP,
    CONFIDENCE_BY_MATCH,
    CONFIDENCE_CAUSAL_WARNING,
    CONFIDENCE_CITATION_RESOLVED,
    CONFIDENCE_CITATION_UNRESOLVED,
    CONFIDENCE_CITATION_UNCHECKED,
    SEVERITY_BY_CHECK,
    SUMMARY_KEYS,
    CheckType,
    ClaimType,
    DraftClaim,
    Finding,
    MatchKind,
    Severity,
    empty_summary,
)

#: Metrics that carry a market price; the denominator side of a PE/PB recompute.
PRICE_METRICS = ("raw_price",)
#: Every named multiple the checker will attempt to re-derive. Anything else is
#: an analyst's target multiple and is counted as opinion, never failed (§7.1).
_MULTIPLE_PATTERNS: Tuple[Tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"(?:市盈率|PE|P/E)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE), "pe", "net_profit_parent"),
    (re.compile(r"(?:市净率|PB|P/B)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE), "pb", "equity_parent"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*倍\s*(?:市盈率|PE|P/E)", re.IGNORECASE), "pe", "net_profit_parent"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*倍\s*(?:市净率|PB|P/B)", re.IGNORECASE), "pb", "equity_parent"),
)
#: Words that make a sentence a forecast rather than a statement of fact (§8.5).
_FORWARD_MARKERS = ("预计", "预期", "将达", "有望", "目标价", "预计将", "未来", "指引")
_CITATION_SHARE = 0.5
_UNANCHORED_PERIOD_RE = re.compile(r"上半年|下半年|前三季度|第[一二三四]季度|[一二三四]季度")

# --------------------------------------------------------------------------- #
# §7.1's second half — a multiple borrowed from another industry
# --------------------------------------------------------------------------- #
#: Declared-industry wording → Shenwan Level-1 code, **longest wording first** so
#: that 非银金融 is tried before 银行 inside a phrase like 非银金融业.
#:
#: Only the frozen L1 names are accepted, with an optional 业/行业 suffix. A phrase
#: that is not exactly one of them is not an industry declaration for this rule:
#: the point of §7.1's limit is that a *verifiable* defect may be failed, and a
#: fuzzy industry guess is exactly how this rule would start manufacturing the
#: false positives the limit exists to prevent. Broad words that are really a
#: sub-sector (白酒 / 新能源 / 券商 / 保险) are deliberately absent — declaring one
#: of them is not a mismatch with the company's own L1 industry.
_INDUSTRY_WORDS: Dict[str, str] = {
    name: ind.code
    for ind, name in sorted(
        ((item, item.name) for item in L1_INDUSTRIES.values()),
        key=lambda pair: len(pair[1]),
        reverse=True,
    )
}
_INDUSTRY_NAME_BY_CODE: Dict[str, str] = {
    ind.code: ind.name for ind in L1_INDUSTRIES.values()
}
_INDUSTRY_ALTERNATION = "|".join(re.escape(word) for word in _INDUSTRY_WORDS)

#: 「行业名(+业/行业) + 倍数」. The industry word must sit **next to** the multiple,
#: so an incidental mention of a sector elsewhere in the sentence is never read as
#: a declaration of where the multiple came from.
_INDUSTRY_MULTIPLE_RE = re.compile(
    rf"(?P<word>{_INDUSTRY_ALTERNATION})(?:业|行业)?\s*的?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*倍\s*"
    r"(?P<label>市盈率|市净率|市销率|PE|P/E|PB|P/B|PS|P/S)?",
    re.IGNORECASE,
)

#: An *exchange-qualified* code, the only form accepted as a draft's self-declared
#: target. A bare six-digit number in a research draft is far more often a figure
#: than a ticker, so accepting one would trade a silent miss for a loud false alarm.
_DRAFT_SYMBOL_RE = re.compile(
    r"(?<!\d)(\d{6})\s*[.．]\s*(?:SH|SZ|BJ)(?![A-Za-z])", re.IGNORECASE
)


@dataclass
class CheckContext:
    """Everything a rule may read. Built once, never mutated by a rule."""

    index: FactIndex
    references: ReferenceIndex
    scope_signals: Tuple[Any, ...] = ()
    as_of: Optional[str] = None
    registry: CalculationRegistry = None          # type: ignore[assignment]
    #: The target company the draft names, resolved once from all its sentences.
    #: A document-level property, so it cannot live on a single claim; ``None``
    #: whenever the draft names no target or names more than one.
    draft_symbol: Optional[str] = None

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = CalculationRegistry()

    def blocked_documents(self) -> List[str]:
        """Documents whose comparability CARD-04 refused outright."""
        blocked: List[str] = []
        for signal in self.scope_signals:
            if _decision_of(signal) == "block_yoy":
                document_id = str(_signal_field(signal, "document_id", "") or "")
                if document_id:
                    blocked.append(document_id)
        return sorted(set(blocked))

    def blocks_period(
        self, period_key: Optional[str], document_ids: Optional[Iterable[str]] = None
    ) -> Tuple[bool, Optional[Any]]:
        """Is a percentage forbidden for *period_key*, and by which signal?

        Two filters, both needed to keep the gate from over-reaching:

        * a signal with no ``periods_affected`` covers its whole document, so it
          blocks every period — the conservative reading §9 asks for;
        * when the claim names its sources, only a signal from one of those
          documents applies. Otherwise a 重分类 disclosure in one company's filing
          would silently forbid a percentage about a different company's figures,
          which is a false positive dressed up as caution.
        """
        scope = {str(item) for item in (document_ids or ()) if item}
        unrestricted = not scope
        for signal in self.scope_signals:
            if _decision_of(signal) != "block_yoy":
                continue
            document_id = str(_signal_field(signal, "document_id", "") or "")
            if not unrestricted and document_id and document_id not in scope:
                continue
            affected = [str(item) for item in (_signal_field(signal, "periods_affected", ()) or ())]
            if not affected or (period_key and period_key in affected):
                return True, signal
        return False, None

    def any_adjusted(self) -> bool:
        return any(_decision_of(signal) == "use_adjusted" for signal in self.scope_signals)


def _signal_field(signal: object, name: str, default: Any = None) -> Any:
    """Read a field off a CARD-04 signal in either of its two shapes.

    CARD-04 exposes the same record both as a frozen dataclass
    (``detect_scope_signals``) and as a serialized dict (``build_scope_report``).
    Reading only the attribute form silently disables the whole 口径 gate when the
    caller happens to hold the dict form — a failure with no symptom other than
    findings that should have been blocked. Both shapes are accepted here.
    """
    if isinstance(signal, dict):
        return signal.get(name, default)
    return getattr(signal, name, default)


def _decision_of(signal: object) -> str:
    """Read a CARD-04 signal's comparability decision, whatever form it takes."""
    raw = _signal_field(signal, "comparability")
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw))


# --------------------------------------------------------------------------- #
# entry points (§6)
# --------------------------------------------------------------------------- #

def check_report(
    claims: Sequence[DraftClaim],
    normalized_facts: Sequence[object],
    scope_signals: Optional[Sequence[object]] = None,
    *,
    documents: Optional[Iterable[object]] = None,
    as_of: Optional[str] = None,
    index: Optional[FactIndex] = None,
    references: Optional[ReferenceIndex] = None,
) -> List[Finding]:
    """Apply all thirteen rule classes to *claims* against the fact base.

    ``documents`` is optional and only widens citation resolution: without it a
    citation can still be resolved by ``document_id``, but not by filename.
    ``as_of`` is the cutoff the draft was written under; leaving it out disables
    the ``stale_citation`` rule rather than guessing a cutoff from the wall clock
    (the check must not depend on when it runs).
    """
    fact_index = index or FactIndex(normalized_facts)
    if references is None:
        references = ReferenceIndex.build(normalized_facts, documents)
        _note_availability(references, normalized_facts)
    context = CheckContext(
        index=fact_index,
        references=references,
        scope_signals=tuple(scope_signals or ()),
        as_of=as_of,
        draft_symbol=draft_target_symbol(claims),
    )
    findings: List[Finding] = []
    for claim in claims or ():
        findings.extend(_check_claim(claim, context))
    return _order(findings)


def summarize(
    findings: Sequence[Finding],
    *,
    claims: Optional[Sequence[DraftClaim]] = None,
    unextracted: int = 0,
    no_claim: int = 0,
) -> Dict[str, Any]:
    """Count findings, and — when *claims* is given — say what was *not* checked.

    The asymmetry is deliberate. A summary that only counted findings would read
    as "everything else is fine" even when ten sentences never made it through
    extraction, which is the failure mode §9 forbids. Passing the claims lets the
    summary carry the skipped and unverifiable counts beside the error counts.
    """
    summary = empty_summary()
    for finding in findings:
        summary["total"] += 1
        summary["by_type"][finding.check_type] = summary["by_type"].get(finding.check_type, 0) + 1
        summary["by_severity"][finding.severity] = summary["by_severity"].get(finding.severity, 0) + 1
        if finding.severity == Severity.ERROR.value:
            summary["errors"] += 1
        else:
            summary["warnings"] += 1
    summary["clean"] = summary["errors"] == 0
    summary["sentences_unextracted"] = int(unextracted or 0)
    summary["sentences_no_claim"] = int(no_claim or 0)

    if claims is not None:
        checked_ids = {finding.claim_id for finding in findings}
        for claim in claims:
            if claim.is_forward_looking:
                summary["claims_skipped_forward_looking"] += 1
                continue
            if claim.claim_type == ClaimType.OPINION.value:
                summary["claims_skipped_opinion"] += 1
                continue
            if _is_multiple_only(claim) and claim.claim_id not in checked_ids:
                # §7.1: a multiple that cannot be recomputed is an analyst's
                # opinion. Skipped — but counted, so "no finding" is never read as
                # "verified".
                summary["claims_skipped_opinion"] += 1
                continue
            summary["claims_checked"] += 1
            if _is_unverifiable(claim) and claim.claim_id not in checked_ids:
                summary["unverifiable_claims"] += 1
    return summary


def build_report(
    claims: Sequence[DraftClaim],
    findings: Sequence[Finding],
    *,
    document_id: str = "",
    sentences: Optional[Sequence[object]] = None,
    unextracted: int = 0,
    no_claim: int = 0,
    extraction_notes: Optional[Sequence[str]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    engine: str = "src.cn.checker.checker（确定性规则，非模型输出）",
) -> Dict[str, Any]:
    """The full payload the tool and the UI render.

    Each finding is joined to the sentence it came from, so the demo can show
    「错误类型 + 原句 + 正确值 + 建议」 without the caller re-joining anything.
    """
    by_claim: Dict[str, DraftClaim] = {claim.claim_id: claim for claim in claims or ()}
    payload_findings: List[Dict[str, Any]] = []
    for finding in findings:
        item = finding.to_dict()
        claim = by_claim.get(finding.claim_id)
        if claim is not None:
            item["sentence"] = claim.sentence
            item["claim_page"] = claim.page
            item["metric"] = claim.metric
            item["period"] = claim.period
        item["evidence"] = list(finding.evidence_ids)
        payload_findings.append(item)

    return {
        "document_id": document_id,
        "claims": [claim.to_dict() for claim in claims or ()],
        "findings": payload_findings,
        "summary": summarize(
            findings, claims=claims, unextracted=unextracted, no_claim=no_claim
        ),
        "sentences": [getattr(item, "to_dict", lambda: item)() for item in (sentences or ())],
        "extraction_notes": list(extraction_notes or ()),
        "coverage": dict(coverage or {}),
        "engine": engine,
        "labels": {
            "check_types": {name: CHECK_TYPE_LABELS.get(name, name) for name in CHECK_TYPE_ORDER},
            "severities": [Severity.ERROR.value, Severity.WARNING.value],
        },
    }


# --------------------------------------------------------------------------- #
# per-claim rules
# --------------------------------------------------------------------------- #

def _check_claim(claim: DraftClaim, context: CheckContext) -> List[Finding]:
    findings: List[Finding] = []

    if claim.is_forward_looking:
        # §8 edge case 5: a forecast is not a fact. It is counted, never checked.
        return findings

    citations, unresolved, cited_facts = _resolve_citations(claim, context)
    findings.extend(_citation_findings(claim, context, citations, unresolved, cited_facts))

    # Everything about the *figure* is judged against the material the sentence
    # cites — never against the whole reference pile. A number that is right for
    # company B says nothing about a claim about company A.
    cited_documents = {citation.document_id for citation in citations}
    index = context.index.scoped(cited_documents) if cited_documents else context.index
    # A citation that points at a document without the metric is already reported;
    # adding "无法核验" on top would be two findings for one defect.
    citation_defect = any(item.check_type == CheckType.WRONG_CITATION.value for item in findings)

    if claim.value is not None and not citation_defect:
        findings.extend(_value_findings(claim, index))

    if claim.growth is not None:
        findings.extend(_growth_findings(claim, context, index, cited_documents))

    if claim.valuation_multiple and not citation_defect:
        findings.extend(_multiple_findings(claim, index))

    if claim.valuation_multiple:
        # §7.1's other basis. Independent of citations and of the fact base: it
        # compares what the sentence says it borrowed from against the target
        # company's own industry, so a citation defect does not switch it off.
        findings.extend(_industry_multiple_findings(claim, context.draft_symbol))

    if claim.claim_type == ClaimType.CAUSAL.value:
        findings.extend(_causal_findings(claim, citations))

    return findings


# -- citations ---------------------------------------------------------------

def _resolve_citations(
    claim: DraftClaim, context: CheckContext
) -> Tuple[List[Citation], List[str], List[Disclosure]]:
    citations: List[Citation] = []
    unresolved: List[str] = []
    facts: List[Disclosure] = []
    for reference in claim.references:
        citation = context.references.resolve(reference)
        if citation is None:
            unresolved.append(reference)
            continue
        citations.append(citation)
        facts.extend(
            item for item in context.index.disclosures
            if item.document_id == citation.document_id
        )
    return citations, unresolved, facts


def _citation_findings(
    claim: DraftClaim,
    context: CheckContext,
    citations: Sequence[Citation],
    unresolved: Sequence[str],
    cited_facts: Sequence[Disclosure],
) -> List[Finding]:
    findings: List[Finding] = []

    if not claim.references and claim.claim_type == ClaimType.FACTUAL.value and claim.has_figure:
        findings.append(_finding(
            claim,
            CheckType.MISSING_CITATION,
            original=_original_text(claim),
            expected="需给出可核验的披露来源（文档 + 页码）",
            suggestion=(
                "为这一数值补上出处：注明披露文档（年报/公告）与页码，"
                "或删除该数值。无法定位来源的数字不要写进研报。"
            ),
            confidence=CONFIDENCE_CITATION_UNCHECKED,
            detail="factual 数值 claim 未携带任何 source_reference。",
        ))

    if unresolved:
        findings.append(_finding(
            claim,
            CheckType.UNSUPPORTED_CITATION,
            original="、".join(unresolved),
            expected="已注册文档的 document_id 或文件名（可带 #pN 页码）",
            suggestion=(
                f"引用 {'、'.join(unresolved)} 在本系统已注册材料中不存在；"
                "请核对文档名与页码，或先摄入该材料后重新核查。"
            ),
            confidence=CONFIDENCE_CITATION_UNRESOLVED,
            detail=f"以下引用无法解析到已注册文档：{list(unresolved)}",
        ))

    if citations and claim.metric:
        # Does the cited material contain the metric at all? If no cited document
        # discloses it, the citation points at the wrong object (§7.1).
        with_metric = [item for item in cited_facts if item.metric == claim.metric]
        if not with_metric:
            findings.append(_finding(
                claim,
                CheckType.WRONG_CITATION,
                original="、".join(citation.raw for citation in citations),
                expected=f"披露 {claim.metric_label or claim.metric} 的文档与页码",
                suggestion=(
                    f"被引文档中没有 {claim.metric_label or claim.metric} 的披露；"
                    "请改引含该指标的报表页，或修正指标名称。"
                ),
                evidence_ids=_evidence_ids(cited_facts[:3]),
                confidence=CONFIDENCE_CITATION_RESOLVED,
                detail="引用对象存在，但其中没有该指标的披露。",
            ))
        else:
            for citation in citations:
                if citation.page is None:
                    continue
                pages = {item.page for item in with_metric if item.page is not None}
                if pages and citation.page not in pages:
                    findings.append(_finding(
                        claim,
                        CheckType.WRONG_PAGE,
                        original=f"{citation.raw}",
                        expected="、".join(f"第{page}页" for page in sorted(pages)),
                        suggestion=(
                            f"{claim.metric_label or claim.metric} 披露在第 "
                            f"{'、'.join(str(page) for page in sorted(pages))} 页，"
                            f"草稿标注第 {citation.page} 页。"
                        ),
                        evidence_ids=_evidence_ids(
                            [item for item in with_metric if item.page is not None][:3]
                        ),
                        confidence=CONFIDENCE_CITATION_RESOLVED,
                        detail=f"引用的页码 {citation.page} 不是该指标所在页。",
                    ))

    if context.as_of:
        for citation in citations:
            if claim.metric and not any(
                item.document_id == citation.document_id and item.metric == claim.metric
                for item in cited_facts
            ):
                continue
            registered, published_at = context.references.publication_date_of(citation.document_id)
            disclosure_dates = (
                [published_at] if published_at else []
            ) if registered else [
                item.available_at for item in cited_facts
                if item.document_id == citation.document_id and item.available_at
            ]
            if disclosure_dates and min(disclosure_dates) > context.as_of:
                findings.append(_finding(
                    claim,
                    CheckType.STALE_CITATION,
                    original=f"{citation.raw}（最早可得 {min(disclosure_dates)}）",
                    expected=f"截至 {context.as_of} 已公开的披露",
                    suggestion=(
                        f"该披露在草稿时点 {context.as_of} 尚未公开（最早 "
                        f"{min(disclosure_dates)}）；请改用草稿时点之前已公开的数据。"
                    ),
                    evidence_ids=[],
                    severity=Severity.WARNING,
                    confidence=CONFIDENCE_CITATION_RESOLVED,
                    detail="引用对象在草稿 as_of 时点尚不可得。",
                ))
                break

    return findings


# -- figures ------------------------------------------------------------------

def _value_findings(claim: DraftClaim, index: FactIndex) -> List[Finding]:
    metric = claim.metric
    if not metric:
        # The sentence has a number but no recognisable metric. There is nothing
        # to compare it to; §9 forbids inventing one, so this is counted as
        # unverifiable by ``summarize`` and produces no finding.
        return []

    if claim.period is None and _UNANCHORED_PERIOD_RE.search(claim.sentence):
        return [_finding(
            claim,
            CheckType.MISSING_CITATION,
            original=_original_text(claim),
            expected="无法核验（期间未明确）",
            suggestion="该句提及半年或季度但未写明年份；请补全年份及对应披露来源后再核验数值。",
            confidence=CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value],
            severity=Severity.WARNING,
            detail="相对期间未锚定年份，不得拿全年披露值判定数字错误。",
        )]

    declared_unit = claim.unit
    scale = unit_scale(declared_unit) if declared_unit else 1.0
    # The draft's own unit wins: §7.2 says the comparison happens *after*
    # converting by what the draft declared. Reading the digits as if they were
    # 元 when the sentence says 万元 would silently bless a 10^4 error.
    value_base = float(claim.value) * scale
    outcome = index.match(
        metric, value_base, period_key=claim.period, scope=claim.scope,
        tolerance=_tolerance(claim, scale if declared_unit else None),
    )

    if outcome.kind == MatchKind.EXACT.value:
        return []

    if outcome.kind == MatchKind.UNVERIFIABLE.value:
        return [_unverifiable(claim, outcome)]

    if declared_unit is None:
        # No unit word was written, so the only comparison available rests on an
        # assumption we made about the magnitude. §9: never fail a value over an
        # assumption — report it as unverifiable instead, and say why.
        return [_finding(
            claim,
            CheckType.MISSING_CITATION,
            original=_original_text(claim),
            expected="无法核验",
            suggestion=(
                "该数值未声明单位（元/万元/亿元），无法确定量级；补上单位后即可核验。"
            ),
            confidence=CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value],
            severity=Severity.WARNING,
            detail=f"未声明单位，仅能按基准单位比较，结果：{outcome.detail}",
        )]

    return [_finding_from_match(claim, outcome, index)]


def _finding_from_match(
    claim: DraftClaim, outcome: MatchOutcome, index: Optional[FactIndex] = None
) -> Finding:
    disclosure = outcome.disclosure
    if outcome.kind == MatchKind.SUPERSEDED.value and outcome.replacement is not None:
        # ``expected`` is the figure the author should have quoted, not the one
        # they did — showing the superseded value as "correct" would be its own
        # wrong number.
        target = outcome.replacement
    else:
        target = disclosure
    expected = format_amount(target.value, target.unit) if target else "无法核验"
    period_note = f"（{target.period_key}）" if target else ""
    original = _original_text(claim)
    evidence_ids = [item.fact_id for item in (disclosure, outcome.replacement) if item]
    alternatives = _alternatives_note(outcome, target)

    if outcome.kind == MatchKind.SUPERSEDED.value:
        return _finding(
            claim,
            CheckType.REVISION_SUPERSEDED,
            original=original,
            expected=f"{expected}{period_note}",
            suggestion=(
                f"该数值来自被取代的 {disclosure.version} 版本；同一期间已有"
                f"更新版本披露 {expected}。请改用最新披露值。"
            ),
            evidence_ids=evidence_ids,
            confidence=CONFIDENCE_BY_MATCH[MatchKind.SUPERSEDED.value],
            detail=outcome.detail + alternatives,
        )

    if outcome.kind == MatchKind.UNIT.value:
        return _finding(
            claim,
            CheckType.UNIT_ERROR,
            original=original,
            expected=f"{expected}{period_note}",
            suggestion=(
                f"数值本身与披露一致，但单位声明有误：披露为 {expected}。"
                "请修正单位（元/万元/亿元），不要改动数字。"
            ),
            evidence_ids=evidence_ids,
            confidence=CONFIDENCE_BY_MATCH[MatchKind.UNIT_CONVERTED.value],
            detail=outcome.detail + alternatives,
        )

    if outcome.kind == MatchKind.PERIOD.value:
        counterpart = (
            index.primary(claim.metric, period_key=claim.period, scope=disclosure.scope)
            if index is not None and disclosure is not None and disclosure.scope is not None
            and claim.metric and claim.period else None
        )
        if counterpart is not None:
            expected = format_amount(counterpart.value, counterpart.unit)
            period_note = f"（{counterpart.period_key}）"
            evidence_ids.append(counterpart.fact_id)
        return _finding(
            claim,
            CheckType.PERIOD_ERROR,
            original=original,
            expected=f"{expected}{period_note}",
            suggestion=(
                f"该数值对应期间 {disclosure.period_key}，与草稿声明的 "
                f"{claim.period or '（未声明）'} 不符；请改正期间或改引对应期间的数据。"
            ),
            evidence_ids=evidence_ids,
            confidence=CONFIDENCE_BY_MATCH[MatchKind.PERIOD_INFERRED.value],
            detail=outcome.detail + alternatives,
        )

    if outcome.kind == MatchKind.SCOPE.value:
        # The draft declared a 口径, and the figure it used belongs to the *other*
        # one. Two things make this finding useful rather than merely correct:
        # ``expected`` is the same figure under the 口径 the draft claimed (looked up
        # again, unless the filing never disclosed it), and the mismatched value is
        # named too — the author may equally have meant the number they wrote and
        # only mislabelled it, and both repairs are one edit away.
        declared = claim.scope or "（未声明）"
        found_scope = (disclosure.scope or "（未标注口径）") if disclosure else "（未标注口径）"
        found_amount = format_amount(disclosure.value, disclosure.unit) if disclosure else original
        counterpart = None
        if index is not None and claim.scope and claim.metric:
            counterpart = index.primary(
                claim.metric, period_key=claim.period, scope=claim.scope
            )
        if counterpart is not None:
            expected = f"{format_amount(counterpart.value, counterpart.unit)}" \
                       f"（{counterpart.period_key}）"
            evidence_ids = [disclosure.fact_id, counterpart.fact_id]
            tail = (
                f"「{declared}」口径下该指标披露为 "
                f"{format_amount(counterpart.value, counterpart.unit)}。"
                "请在「改数值」与「改口径」之间任选其一，并注明口径。"
            )
        else:
            tail = (
                f"事实库中未找到「{declared}」口径的同期披露，无法给出替代值；"
                "请核实口径表述，或补充该口径的来源。"
            )
        return _finding(
            claim,
            CheckType.SCOPE_ERROR,
            original=original,
            expected=expected,
            suggestion=(
                f"草稿声明「{declared}」口径，但 {found_amount}"
                f" 对应的是「{found_scope}」口径的披露。{tail}"
            ),
            evidence_ids=evidence_ids,
            confidence=CONFIDENCE_BY_MATCH[MatchKind.PERIOD_INFERRED.value],
            detail=outcome.detail + alternatives,
        )

    return _finding(
        claim,
        CheckType.NUMERIC_ERROR,
        original=original,
        expected=f"{expected}{period_note}",
        suggestion=(
            f"披露值为 {expected}{period_note}；请按披露更正该数值，"
            "或补充说明其计算口径。"
        ),
        evidence_ids=evidence_ids,
        confidence=CONFIDENCE_BY_MATCH[MatchKind.ABSENT.value],
        detail=outcome.detail + alternatives,
    )


def _alternatives_note(outcome: MatchOutcome, chosen: Optional[Disclosure]) -> str:
    """Name every *other* disclosed value of the same figure in the detail field.

    A figure disclosed under two column labels (a filing's 本期/累计 pair both
    canonicalise to one period) has no single right answer; saying so in the
    finding is the difference between a reviewer trusting the tool and a reviewer
    finding the tool wrong.
    """
    others = [
        item for item in outcome.alternatives
        if chosen is None or item.fact_id != chosen.fact_id
    ]
    if not others:
        return ""
    rendered = "、".join(
        f"{format_amount(item.value, item.unit)}（{item.source_table or '未知表'}"
        f" 第{item.page}页 {item.version}）"
        for item in others[:4]
    )
    return f" 同期间另有披露：{rendered}。"


# -- growth -------------------------------------------------------------------

def _growth_findings(
    claim: DraftClaim,
    context: CheckContext,
    index: FactIndex,
    cited_documents: Iterable[str],
) -> List[Finding]:
    blocked, signal = context.blocks_period(claim.period, cited_documents)
    if blocked:
        rule = _signal_field(signal, "detector_rule", "") or _signal_field(signal, "signal_type", "")
        evidence = [str(item) for item in (_signal_field(signal, "evidence_ids", ()) or ())]
        return [_finding(
            claim,
            CheckType.SCOPE_ERROR,
            original=_original_text(claim),
            expected="（可比性被阻断，不得给出同比）",
            suggestion=(
                "存在会计口径变化／重述披露，两端数据不可直接比较；"
                "请改为披露水平值并说明口径变化，或改用调整后比较列重算。"
            ),
            evidence_ids=evidence,
            confidence=CONFIDENCE_BY_MATCH[MatchKind.EXACT.value],
            detail=f"CARD-04 决策 block_yoy（规则 {rule}），同比百分比不得出具。",
        )]

    metric = claim.metric
    period_key = claim.period
    if not metric or not period_key:
        return []

    current = index.primary(metric, period_key=period_key)
    previous_key = previous_year_key(period_key)
    previous = index.primary(metric, period_key=previous_key)
    if current is None or previous is None:
        return [_finding(
            claim,
            CheckType.MISSING_CITATION,
            original=_original_text(claim),
            expected="无法核验",
            suggestion=(
                f"事实库中缺少 {metric} 在 {period_key} 或 {previous_key} 的披露，"
                "无法复算同比；请补充来源或删除该百分比。"
            ),
            confidence=CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value],
            severity=Severity.WARNING,
            detail="同比复算所需的两端披露不齐。",
        )]

    # §7.1: recomputation reuses CARD-03's own function, so the checker can never
    # disagree with the analysis engine about what the percentage should be.
    result = compute_yoy(
        context.registry,
        metric=metric,
        label=claim.metric_label or metric,
        current_fact=current,
        previous_fact=previous,
        current_period=period_key,
        previous_period=previous_key,
        period_basis="FY",
        scope=current.scope,
        version=current.version,
    )
    expected_pct = getattr(result, "delta_pct", None)
    if expected_pct is None:
        return [_finding(
            claim,
            CheckType.CALCULATION_ERROR,
            original=_original_text(claim),
            expected="（基期为 0 或不可比，不得输出百分比）",
            suggestion=(
                f"{previous_key} 的 {metric} 为 "
                f"{format_amount(previous.value, previous.unit)}，"
                "以此为基期的百分比无意义，请改用绝对额说明变动。"
            ),
            evidence_ids=[previous.fact_id],
            confidence=CONFIDENCE_BY_MATCH[MatchKind.EXACT.value],
            detail="CARD-03 compute_yoy 判定百分比不可计算。",
        )]

    tolerance = _percent_tolerance(claim)
    if abs(expected_pct - float(claim.growth)) <= tolerance:
        return []

    return [_finding(
        claim,
        CheckType.CALCULATION_ERROR,
        original=format_percent(claim.growth),
        expected=format_percent(expected_pct),
        suggestion=(
            f"按披露复算，{period_key} 相对 {previous_key} 的 {metric} 同比应为 "
            f"{format_percent(expected_pct)}；请核对两端口径与复算过程。"
        ),
        evidence_ids=[current.fact_id, previous.fact_id],
        confidence=CONFIDENCE_BY_MATCH[MatchKind.EXACT.value],
        detail="复算走 CARD-03 compute_yoy，与草稿给出的百分比不一致。",
    )]


# -- valuation multiples -------------------------------------------------------
#
# §7.1 names two things a ``valuation_multiple_error`` may be based on: a multiple
# that contradicts the disclosed inputs, and a multiple borrowed from the wrong
# industry. They are separate branches with separate preconditions, and both are
# deliberately narrow — an analyst's target multiple ("给 25 倍 PE") is an opinion,
# and failing an opinion would be the single largest source of false positives in
# this card.


def draft_target_symbol(claims: Sequence[DraftClaim]) -> Optional[str]:
    """The one company a draft declares itself to be about, else ``None``.

    Read from the draft's own sentences — never from the reference filings, which
    may well cover several issuers. Two rules keep this from guessing:

    * only an exchange-qualified code counts (``300750.SZ``), because a bare
      six-digit number in a research draft is usually a figure;
    * zero or several distinct codes mean "the draft does not name one target",
      in which case :func:`_industry_multiple_findings` stays silent rather than
      deciding which company a page of prose is about.
    """
    seen: List[str] = []
    for claim in claims or ():
        for match in _DRAFT_SYMBOL_RE.finditer(str(claim.sentence or "")):
            code = match.group(1)
            if code not in seen:
                seen.append(code)
    return seen[0] if len(seen) == 1 else None


def _declared_industries(sentence: Optional[str]) -> List[Tuple[str, str, str]]:
    """``(wording, L1 code, matched text)`` for every industry the sentence ties to
    a multiple. Empty when the sentence declares no source industry at all — which
    is the overwhelmingly common case and must never produce a finding."""
    found: List[Tuple[str, str, str]] = []
    for match in _INDUSTRY_MULTIPLE_RE.finditer(str(sentence or "")):
        wording = match.group("word")
        code = _INDUSTRY_WORDS.get(wording)
        if code is None:  # pragma: no cover - the alternation is built from the keys
            continue
        found.append((wording, code, match.group(0)))
    return found


def industry_check_capability(claims: Sequence[DraftClaim]) -> Tuple[bool, Optional[str]]:
    """Could §7.1's industry branch actually run on this draft? And if not, why not.

    §9 requires the payload to distinguish "checked and clean" from "never ran".
    The branch silently returning nothing is indistinguishable from a clean draft,
    so the coverage block asks this function instead of inferring.
    """
    claims = tuple(claims or ())
    multipled = [claim for claim in claims if claim.valuation_multiple]
    if not multipled:
        return False, "草稿没有任何估值倍数声明，无从判断倍数来源行业"
    if not any(_declared_industries(claim.sentence) for claim in multipled):
        return False, "草稿未声明倍数取自哪个行业（零猜测：不声明就不判）"
    symbol = draft_target_symbol(claims)
    if symbol is None:
        return False, "草稿未唯一给出目标公司代码（需形如 300750.SZ 的带交易所后缀写法）"
    if get_industry_code(symbol) is None:
        return False, f"目标公司 {symbol} 不在冻结的申万一级行业映射内"
    return True, None


def _industry_multiple_findings(
    claim: DraftClaim, target_symbol: Optional[str]
) -> List[Finding]:
    """§7.1's second half: a multiple taken from a sector the draft is not about.

    Fires only when all three of these hold, each read straight off the draft:

    1. the sentence **declares** which industry the multiple came from, and names
       exactly one;
    2. the draft identifies exactly one target company, and that company is in the
       frozen Shenwan mapping;
    3. the declared industry and the target's industry are different L1 codes.

    Anything less is silence, not a warning: "may have borrowed a multiple" is not
    a defect a reader can act on. The rule carries no ``evidence_ids`` by
    construction — its basis is the frozen industry mapping plus the draft's own
    wording, and neither is a disclosure in the fact base. Inventing an id there
    would make the report look better evidenced than it is.
    """
    if not target_symbol:
        return []
    target_code = get_industry_code(target_symbol)
    if target_code is None:
        return []

    declared = _declared_industries(claim.sentence)
    codes = {code for _wording, code, _text in declared}
    if len(codes) != 1:
        # None declared, or two different sectors named for one multiple: the
        # sentence does not say what it borrowed from, so there is nothing to
        # compare. Guessing which of the two it "meant" is how false positives start.
        return []
    wording, declared_code, matched = declared[0]
    if declared_code == target_code:
        return []

    target_name = _INDUSTRY_NAME_BY_CODE[target_code]
    declared_name = _INDUSTRY_NAME_BY_CODE[declared_code]
    return [_finding(
        claim,
        CheckType.VALUATION_MULTIPLE_ERROR,
        original=matched,
        expected=f"{target_name}可比公司的目标倍数",
        suggestion=(
            f"该句把「{wording}」的倍数（{matched}）当作目标倍数，"
            f"而目标公司 {target_symbol} 属申万一级「{target_name}」（{target_code}），"
            f"「{declared_name}」是 {declared_code}。两个行业的估值倍数不可直接套用，"
            "请改用同行业可比公司的倍数，或改写为不含跨行业比照的表述。"
        ),
        evidence_ids=[],
        confidence=CONFIDENCE_BY_MATCH[MatchKind.EXACT.value],
        detail=(
            f"行业错配：草稿声明的倍数来源是「{wording}」（{declared_code}），"
            f"目标公司 {target_symbol} 的申万一级行业是「{target_name}」（{target_code}）。"
            "判定依据是冻结的行业映射与草稿自身的措辞，不含任何事实库披露，故无证据 ID。"
        ),
    )]


def _multiple_findings(claim: DraftClaim, index: FactIndex) -> List[Finding]:
    """§7.1's recompute branch: a multiple that contradicts the disclosed inputs.

    An analyst's target multiple ("给 25 倍 PE") is an opinion. Failing it would be
    the single largest source of false positives in this card, so it is skipped and
    counted instead. Only a multiple that can be recomputed from disclosed inputs —
    price × shares / net profit — is judged here; the industry branch above covers
    §7.1's other named basis.
    """
    parsed = _parse_multiple(claim.valuation_multiple)
    if parsed is None:
        return []
    kind, claimed = parsed

    price = index.primary(PRICE_METRICS[0])
    if price is None:
        return []
    denominator_metric = "net_profit_parent" if kind == "pe" else "equity_parent"
    denominator = index.primary(denominator_metric, period_key=claim.period)
    if denominator is None or not denominator.value:
        return []
    shares = index.primary("diluted_shares")
    if shares is None or not shares.value:
        return []

    market_cap = price.value * shares.value
    expected = market_cap / denominator.value
    tolerance = max(0.05, abs(claimed) * 0.01)
    if abs(expected - claimed) <= tolerance:
        return []

    label = "市盈率" if kind == "pe" else "市净率"
    return [_finding(
        claim,
        CheckType.VALUATION_MULTIPLE_ERROR,
        original=f"{label} {claimed:g}x",
        expected=f"{label} {expected:.1f}x",
        suggestion=(
            f"以披露的股价 {format_amount(price.value, price.unit)}、"
            f"{'归母净利润' if kind == 'pe' else '归母权益'} "
            f"{format_amount(denominator.value, denominator.unit)} 与期末总股本 "
            f"{shares.value:,.0f} 股复算，{label} 约为 {expected:.1f} 倍。"
            "请核对价格日期与分母口径。"
        ),
        evidence_ids=[price.fact_id, denominator.fact_id, shares.fact_id],
        confidence=CONFIDENCE_BY_MATCH[MatchKind.EXACT.value],
        detail="倍数可由披露输入复算，且与草稿给出的数值不符。",
    )]


def _parse_multiple(text: Optional[str]) -> Optional[Tuple[str, float]]:
    if not text:
        return None
    for pattern, kind, _denominator in _MULTIPLE_PATTERNS:
        match = pattern.search(str(text))
        if match:
            try:
                return kind, float(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


# -- causality ----------------------------------------------------------------

def _causal_findings(claim: DraftClaim, citations: Sequence[Citation]) -> List[Finding]:
    """§7.4: a causal sentence needs *independent* evidence on both sides.

    The card's §5 schema carries one ``source_reference`` per claim, so
    independence is established structurally from the citations: two sides are
    evidenced only when the sentence cites two *different* sources — different
    documents, or different pages of one document. A single citation can support
    at most one side, and re-citing the same page twice is still one source.

    A sentence the extractor classified as causal but whose two sides it could not
    separate is also flagged: the claim cannot be evidenced if nobody wrote down
    what the cause and the effect are.
    """
    sides = {(item.document_id, item.page) for item in citations}
    split_ok = bool(claim.causal_antecedent and claim.causal_consequent)

    if len(sides) >= 2 and split_ok:
        return []

    reasons: List[str] = []
    if len(sides) < 2:
        reasons.append(f"仅解析到 {len(sides)} 个独立来源，因果两侧至少各需一个")
    if not split_ok:
        reasons.append("未能分离出因果两侧")

    return [_finding(
        claim,
        CheckType.UNSUPPORTED_CAUSAL_CLAIM,
        original=claim.sentence,
        expected="因果两侧各需一条独立证据（不同文档或不同页）",
        suggestion=(
            f"该句为因果断言（{'; '.join(reasons)}）。请分别给出原因侧与结果侧的披露来源，"
            "或改写为不含因果关系的描述。"
        ),
        evidence_ids=[],
        severity=Severity.WARNING,
        confidence=CONFIDENCE_CAUSAL_WARNING,
        detail="；".join(reasons) + "。",
    )]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _finding(
    claim: DraftClaim,
    check_type: CheckType,
    *,
    original: str,
    expected: str,
    suggestion: str,
    evidence_ids: Optional[Sequence[str]] = None,
    confidence: float,
    detail: str = "",
    severity: Optional[Severity] = None,
) -> Finding:
    return Finding(
        claim_id=claim.claim_id,
        check_type=check_type.value,
        severity=(severity or SEVERITY_BY_CHECK[check_type.value]).value,
        original=original,
        expected=expected,
        evidence_ids=[str(item) for item in (evidence_ids or ())],
        suggestion=suggestion,
        confidence=float(confidence),
        claim_page=claim.page,
        document_id=claim.document_id,
        detail=detail,
    )


def _unverifiable(claim: DraftClaim, outcome: MatchOutcome) -> Finding:
    """§8.3 — the fact base has nothing to compare against; that is not an error."""
    return _finding(
        claim,
        CheckType.MISSING_CITATION,
        original=_original_text(claim),
        expected="无法核验",
        suggestion=(
            "系统事实库中没有该指标／期间的披露，无法判断对错；"
            "请补充可核验来源，或确认指标名称是否与披露一致。"
        ),
        confidence=CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value],
        severity=Severity.WARNING,
        detail=outcome.detail or "事实库中无对应披露，无法核验（不等于错误）。",
    )


def _original_text(claim: DraftClaim) -> str:
    """The claim's figure as written, with its unit — for §10 rule 2 symmetry."""
    pieces: List[str] = []
    if claim.value_literal:
        pieces.append(f"{claim.value_literal}{claim.unit or ''}")
    elif claim.value is not None:
        pieces.append(format_amount(claim.value, claim.unit))
    if claim.growth is not None:
        pieces.append(format_percent(claim.growth))
    if not pieces:
        pieces.append(claim.sentence)
    return "，".join(pieces)


def _tolerance(claim: DraftClaim, scale: Optional[float]) -> float:
    """§8 edge case 2: tolerance is the rounding granularity of the *displayed* digits.

    A draft that writes ``32.6亿元`` has already discarded everything below 0.05
    billion; failing it against ``32.58亿`` would be punishing the author for
    rounding. Nothing beyond that is allowed — the tolerance is derived from the
    literal, not loosened by a fudge factor.
    """
    literal = claim.value_literal
    if literal is None and claim.value is not None:
        literal = repr(float(claim.value))
    decimals = 0
    if literal and "." in str(literal):
        decimals = len(str(literal).split(".", 1)[1].rstrip("0")) or 0
    return 0.5 * (10 ** -decimals) * (scale if scale else 1.0)


def _percent_tolerance(claim: DraftClaim) -> float:
    """Half-ULP of the written percentage (``18.7%`` -> ±0.05)."""
    literal = claim.growth_literal
    if literal is None and claim.growth is not None:
        literal = repr(float(claim.growth))
    decimals = 1
    if literal and "." in str(literal):
        decimals = len(str(literal).split(".", 1)[1].rstrip("0")) or 0
    return 0.5 * (10 ** -decimals)


def _evidence_ids(items: Sequence[Disclosure]) -> List[str]:
    return [item.fact_id for item in items if item.fact_id]


def _is_unverifiable(claim: DraftClaim) -> bool:
    return not claim.metric and claim.value is not None


def _is_multiple_only(claim: DraftClaim) -> bool:
    """A claim whose entire content is a valuation multiple.

    Whether such a claim is *checked* or *counted as opinion* depends on whether
    the inputs existed — and only ``_multiple_findings`` knows that, which is why
    the caller passes the finding ids before calling this.
    """
    return bool(claim.valuation_multiple) and claim.value is None and claim.growth is None


def _order(findings: Sequence[Finding]) -> List[Finding]:
    rank = {name: position for position, name in enumerate(CHECK_TYPE_ORDER)}
    return sorted(findings, key=lambda item: (item.claim_id, rank.get(item.check_type, 99)))


def _note_availability(references: ReferenceIndex, facts: Sequence[object]) -> None:
    for fact in facts:
        document_id = str(getattr(fact, "document_id", "") or "")
        if not document_id:
            continue
        available = getattr(fact, "available_at", None)
        if available:
            references.note_available_at(document_id, str(available))


def is_forward_looking(text: str) -> bool:
    """Deterministic forward-looking detector, shared by both extractors (§8.5)."""
    return any(marker in str(text or "") for marker in _FORWARD_MARKERS)


def looks_like_growth(text: str) -> bool:
    """Whether a fragment reads as a percentage — reuses CARD-02's own predicate."""
    return looks_like_percent(text)


__all__ = [
    "CheckContext",
    "build_report",
    "check_report",
    "draft_target_symbol",
    "format_amount",
    "format_percent",
    "industry_check_capability",
    "is_forward_looking",
    "looks_like_growth",
    "summarize",
    "CLAIM_TYPE_MAP",
    "SUMMARY_KEYS",
]
