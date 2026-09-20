"""CARD-05 data models: a *claim* extracted from prose, and a *finding* about it.

The card's central boundary is that **the LLM moves text and nothing else**:

* :class:`DraftClaim` is the *only* thing a model produces. Every field is a
  verbatim quote or a literal lifted out of the sentence — no judgement, no
  correction, no expected value.
* :class:`Finding` is produced *exclusively* by :mod:`src.cn.checker.checker`,
  which is deterministic and imports no model client (asserted by test, §10).

Two vocabularies coexist and must not be conflated:

``DraftClaim.claim_type`` — what the *sentence* is (factual / valuation / causal /
opinion / forward-looking). This is what the extractor reports.
``Claim.claim_type`` — the v3.0 graph vocabulary this card has to feed
(fact / inference / opinion). The card's §5 table fixes the mapping, and §5 is
explicit that the model must not choose it: the mapping is applied by
deterministic code after the claim passes schema validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class CheckType(str, Enum):
    """The card's §7.1 thirteen error classes. Exactly thirteen — no more."""

    NUMERIC_ERROR = "numeric_error"
    UNIT_ERROR = "unit_error"
    PERIOD_ERROR = "period_error"
    SCOPE_ERROR = "scope_error"
    CALCULATION_ERROR = "calculation_error"
    VALUATION_MULTIPLE_ERROR = "valuation_multiple_error"
    MISSING_CITATION = "missing_citation"
    WRONG_CITATION = "wrong_citation"
    UNSUPPORTED_CITATION = "unsupported_citation"
    WRONG_PAGE = "wrong_page"
    STALE_CITATION = "stale_citation"
    REVISION_SUPERSEDED = "revision_superseded"
    UNSUPPORTED_CAUSAL_CLAIM = "unsupported_causal_claim"


#: Stable emission order — the report is byte-stable for a given input.
CHECK_TYPE_ORDER: Tuple[str, ...] = tuple(member.value for member in CheckType)

CHECK_TYPE_LABELS: Dict[str, str] = {
    CheckType.NUMERIC_ERROR.value: "数值不符",
    CheckType.UNIT_ERROR.value: "单位错误",
    CheckType.PERIOD_ERROR.value: "期间错配",
    CheckType.SCOPE_ERROR.value: "口径错配",
    CheckType.CALCULATION_ERROR.value: "复算不一致",
    CheckType.VALUATION_MULTIPLE_ERROR.value: "估值倍数错误",
    CheckType.MISSING_CITATION.value: "缺少引用",
    CheckType.WRONG_CITATION.value: "引用对象错",
    CheckType.UNSUPPORTED_CITATION.value: "引用不存在",
    CheckType.WRONG_PAGE.value: "页码错",
    CheckType.STALE_CITATION.value: "引用过期数据",
    CheckType.REVISION_SUPERSEDED.value: "引用了被取代的旧值",
    CheckType.UNSUPPORTED_CAUSAL_CLAIM.value: "无证据因果断言",
}


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


#: Per-check severity. Deliberately conservative (§9: a false positive costs more
#: credibility than a miss). A number that contradicts the disclosure is an
#: ``error``; a *procedural* defect — nothing cited, a causal sentence whose two
#: sides are not both evidenced, or a value cited from a source that could not
#: have existed yet — is a ``warning``. Those three are flagged, never failed.
SEVERITY_BY_CHECK: Dict[str, Severity] = {
    CheckType.NUMERIC_ERROR.value: Severity.ERROR,
    CheckType.UNIT_ERROR.value: Severity.ERROR,
    CheckType.PERIOD_ERROR.value: Severity.ERROR,
    CheckType.SCOPE_ERROR.value: Severity.ERROR,
    CheckType.CALCULATION_ERROR.value: Severity.ERROR,
    CheckType.VALUATION_MULTIPLE_ERROR.value: Severity.ERROR,
    CheckType.MISSING_CITATION.value: Severity.WARNING,
    CheckType.WRONG_CITATION.value: Severity.ERROR,
    CheckType.UNSUPPORTED_CITATION.value: Severity.ERROR,
    CheckType.WRONG_PAGE.value: Severity.ERROR,
    CheckType.STALE_CITATION.value: Severity.WARNING,
    CheckType.REVISION_SUPERSEDED.value: Severity.ERROR,
    CheckType.UNSUPPORTED_CAUSAL_CLAIM.value: Severity.WARNING,
}


class ClaimType(str, Enum):
    """``DraftClaim.claim_type`` — what the sentence *is* (extractor vocabulary)."""

    FACTUAL = "factual"
    VALUATION = "valuation"
    CAUSAL = "causal"
    OPINION = "opinion"
    #: §8 edge case 5: a forecast is not a fact and is never fact-checked.
    FORWARD_LOOKING = "forward_looking"


class GraphClaimType(str, Enum):
    """``Claim.claim_type`` — the v3.0 graph vocabulary this card must feed."""

    FACT = "fact"
    INFERENCE = "inference"
    OPINION = "opinion"


#: The card's §5 mapping table, as data. Applied only after schema validation.
CLAIM_TYPE_MAP: Dict[str, GraphClaimType] = {
    ClaimType.FACTUAL.value: GraphClaimType.FACT,
    ClaimType.VALUATION.value: GraphClaimType.INFERENCE,
    ClaimType.CAUSAL.value: GraphClaimType.INFERENCE,
    ClaimType.OPINION.value: GraphClaimType.OPINION,
    ClaimType.FORWARD_LOOKING.value: GraphClaimType.OPINION,
}


class MatchKind(str, Enum):
    """How well a claim's figure lined up with a disclosure. Drives confidence."""

    EXACT = "exact"                  # 精确证据匹配
    PERIOD_INFERRED = "period_inferred"   # 期间推断匹配
    UNIT_CONVERTED = "unit_converted"     # 需单位换算后匹配
    SUPERSEDED = "superseded"             # 命中被取代的旧版本
    ABSENT = "absent"                     # 无任何披露可对应
    UNVERIFIABLE = "unverifiable"         # 输入不足，无法核验（绝不判错）
    #: The figure matched, but under a different period / scope / unit. These are
    #: *not* "close matches" — each one names a specific, fixable defect, which is
    #: why they get their own kinds instead of collapsing into ``absent``.
    UNIT = "unit"
    PERIOD = "period"
    SCOPE = "scope"


#: §7.3 — the rule table's constants. **There is no LLM self-reported confidence.**
CONFIDENCE_BY_MATCH: Dict[str, float] = {
    MatchKind.EXACT.value: 1.0,
    MatchKind.PERIOD_INFERRED.value: 0.8,
    MatchKind.UNIT_CONVERTED.value: 0.9,
    MatchKind.SUPERSEDED.value: 1.0,
    MatchKind.ABSENT.value: 1.0,
    MatchKind.UNVERIFIABLE.value: 0.0,
}

#: Citation-resolution confidences. A *stale* citation is only ever a warning
#: because the impossibility rests on a declared cutoff rather than on the filing.
CONFIDENCE_CITATION_RESOLVED = 1.0
CONFIDENCE_CITATION_UNRESOLVED = 1.0
CONFIDENCE_CITATION_UNCHECKED = 0.0
CONFIDENCE_CAUSAL_WARNING = 0.8


@dataclass
class DraftClaim:
    """One structured claim lifted out of one draft sentence.

    ``sentence`` is **verbatim** and is the claim's identity for display: §6 says
    an extraction result that does not carry its source sentence is discarded, so
    a claim can always be shown next to the text it came from.

    Fields beyond the card's §5 are marked; each one exists because a §7/§8 rule
    needs it and could not otherwise be expressed:

    * ``source_references`` — §7.4 requires *two* independently evidenced sides of
      a causal sentence, which one scalar reference cannot carry. The first entry
      is mirrored into ``source_reference`` so the §5 field keeps its meaning.
    * ``causal_antecedent`` / ``causal_consequent`` — the two sides themselves.
    * ``extractor`` / ``reason`` — provenance and "why was this left empty",
      so an empty field is never mistaken for "checked and absent".
    * ``scope`` — the 口径 the sentence itself declares (``合并`` / ``母公司``). §5 has
      no such field, yet §7.1 asks for ``scope_error``; without it the rule can only
      ever fire off a CARD-04 signal and a draft that silently swaps 母公司 for 合并
      — a textbook A-share error — would pass. Filled only from explicit wording.
    """

    claim_id: str
    sentence: str
    page: Optional[int] = None
    metric: Optional[str] = None          # canonical metric key when recognised
    metric_label: Optional[str] = None    # the words actually used in the sentence
    period: Optional[str] = None          # canonical YYYYFY / YYYYQ1 / … when recognised
    value: Optional[float] = None         # as written, in the declared unit
    value_literal: Optional[str] = None   # the digits as they appear (drives rounding tol.)
    unit: Optional[str] = None
    growth: Optional[float] = None        # percent, as written
    growth_literal: Optional[str] = None
    valuation_multiple: Optional[str] = None
    source_reference: Optional[str] = None
    claim_type: str = ClaimType.FACTUAL.value

    # -- extensions (documented deviation, see CARD-05 §16) ------------------
    scope: Optional[str] = None           # 合并 / 母公司, when stated explicitly
    source_references: Tuple[str, ...] = ()
    causal_antecedent: Optional[str] = None
    causal_consequent: Optional[str] = None
    document_id: str = ""                 # the *draft* document the sentence came from
    fact_id: Optional[str] = None         # CARD-01 text fact the sentence came from
    extractor: str = ""                   # "llm" | "rules" | "given"
    reason: Optional[str] = None          # why a field is empty

    @property
    def references(self) -> Tuple[str, ...]:
        """All cited sources, first entry first, never duplicated."""
        ordered: List[str] = []
        for item in self.source_references or ():
            text = str(item).strip()
            if text and text not in ordered:
                ordered.append(text)
        if self.source_reference:
            text = str(self.source_reference).strip()
            if text and text not in ordered:
                ordered.insert(0, text)
        return tuple(ordered)

    @property
    def graph_claim_type(self) -> GraphClaimType:
        """§5 mapping — deterministic, never chosen by the model."""
        return CLAIM_TYPE_MAP.get(self.claim_type, GraphClaimType.OPINION)

    @property
    def is_forward_looking(self) -> bool:
        return self.claim_type == ClaimType.FORWARD_LOOKING.value

    @property
    def has_figure(self) -> bool:
        return self.value is not None or self.growth is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "sentence": self.sentence,
            "page": self.page,
            "document_id": self.document_id,
            "fact_id": self.fact_id,
            "metric": self.metric,
            "metric_label": self.metric_label,
            "period": self.period,
            "scope": self.scope,
            "value": self.value,
            "value_literal": self.value_literal,
            "unit": self.unit,
            "growth": self.growth,
            "growth_literal": self.growth_literal,
            "valuation_multiple": self.valuation_multiple,
            "source_reference": self.source_reference,
            "source_references": list(self.references),
            "claim_type": self.claim_type,
            "graph_claim_type": self.graph_claim_type.value,
            "causal_antecedent": self.causal_antecedent,
            "causal_consequent": self.causal_consequent,
            "extractor": self.extractor,
            "reason": self.reason,
        }


@dataclass
class Finding:
    """One problem found in one claim.

    ``original`` and ``expected`` are both rendered **in the same unit and
    period** (§10 rule 2) so a reader can compare them without doing arithmetic.
    ``confidence`` comes from :data:`CONFIDENCE_BY_MATCH` — a constant table, never
    a model's opinion.

    ``claim_page`` is an extension: §14's demo is "click a finding -> jump to the
    original page", and the finding itself knows where the *disclosure* is
    (``evidence_ids``), but not where the *sentence in the draft* is.
    """

    claim_id: str
    check_type: str
    severity: str
    original: str
    expected: str
    evidence_ids: List[str] = field(default_factory=list)
    suggestion: str = ""
    confidence: float = 0.0
    # -- extensions (documented deviation) -----------------------------------
    claim_page: Optional[int] = None
    document_id: str = ""                 # the draft
    detail: str = ""                      # machine-readable reason, for the audit trail

    @property
    def is_error(self) -> bool:
        return self.severity == Severity.ERROR.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "check_type": self.check_type,
            "check_label": CHECK_TYPE_LABELS.get(self.check_type, self.check_type),
            "severity": self.severity,
            "original": self.original,
            "expected": self.expected,
            "evidence_ids": list(self.evidence_ids),
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "claim_page": self.claim_page,
            "document_id": self.document_id,
            "detail": self.detail,
        }


#: Summary keys. Fixed, so the report shape cannot drift between runs.
SUMMARY_KEYS: Tuple[str, ...] = (
    "total",
    "errors",
    "warnings",
    "clean",
    "by_type",
    "by_severity",
    "claims_checked",
    "claims_skipped_forward_looking",
    "claims_skipped_opinion",
    "sentences_unextracted",
    "sentences_no_claim",
    "unverifiable_claims",
)


def empty_summary() -> Dict[str, Any]:
    summary: Dict[str, Any] = {key: 0 for key in SUMMARY_KEYS}
    summary["by_type"] = {name: 0 for name in CHECK_TYPE_ORDER}
    summary["by_severity"] = {Severity.ERROR.value: 0, Severity.WARNING.value: 0}
    summary["clean"] = True
    return summary


__all__ = [
    "CLAIM_TYPE_MAP",
    "CHECK_TYPE_LABELS",
    "CHECK_TYPE_ORDER",
    "CONFIDENCE_BY_MATCH",
    "CONFIDENCE_CAUSAL_WARNING",
    "CONFIDENCE_CITATION_RESOLVED",
    "CONFIDENCE_CITATION_UNCHECKED",
    "CONFIDENCE_CITATION_UNRESOLVED",
    "ClaimType",
    "CheckType",
    "DraftClaim",
    "Finding",
    "GraphClaimType",
    "MatchKind",
    "SUMMARY_KEYS",
    "Severity",
    "SEVERITY_BY_CHECK",
    "empty_summary",
]
