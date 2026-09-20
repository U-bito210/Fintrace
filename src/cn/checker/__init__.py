"""CARD-05 Report Claim Extractor & Deterministic Checker（研报纠错系统）.

Two halves with a hard boundary between them:

``extractor.py`` / ``rule_extractor.py``
    Turn draft prose into :class:`DraftClaim` — verbatim sentences plus the
    literals that were written in them. The first talks to a model; the second
    does the same job with regular expressions and says so. **Neither one knows
    what the right answer is.**

``checker.py``
    Compare those literals against the disclosure fact base and emit
    :class:`Finding`. Deterministic, offline, and it imports no model client —
    asserted by a test, because "the number is wrong" has to be auditable to be
    worth anything.

The card's thirteen error classes, the 单位-before-数值 ordering rule, the
「误报比漏报更伤可信度」 bias, and the "no model self-reported confidence" rule are
all enforced here rather than described here.

Quick start::

    from src.cn.checker import check_draft

    report = check_draft(document_id)        # deterministic, offline
    report["findings"]                       # Finding dicts, each with a page
    report["summary"]["clean"]               # False when any *error* was found
"""

from .checker import build_report, check_report, summarize
from .extractor import (
    ClaimExtractionClient,
    ExtractionOutcome,
    extract_claims,
    resolve_period_label,
)
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
    SEVERITY_BY_CHECK,
    ClaimType,
    CheckType,
    DraftClaim,
    Finding,
    GraphClaimType,
    MatchKind,
    Severity,
)
from .pipeline import DraftNotFound, acheck_draft, check_draft, coverage_of
from .rule_extractor import EXTRACTOR_RULES, extract_claims_rules
from .sentences import DraftSentence, sentences_from_facts, split_sentences

__all__ = [
    # models
    "CheckType", "ClaimType", "GraphClaimType", "Severity", "MatchKind",
    "DraftClaim", "Finding", "CHECK_TYPE_ORDER", "CHECK_TYPE_LABELS",
    "SEVERITY_BY_CHECK", "CONFIDENCE_BY_MATCH", "CLAIM_TYPE_MAP",
    # sentences
    "DraftSentence", "sentences_from_facts", "split_sentences",
    # facts
    "Disclosure", "FactIndex", "MatchOutcome", "Citation", "ReferenceIndex",
    "format_amount", "format_percent",
    # extraction
    "extract_claims", "extract_claims_rules", "ExtractionOutcome",
    "ClaimExtractionClient", "EXTRACTOR_RULES", "resolve_period_label",
    # checker
    "check_report", "summarize", "build_report",
    # pipeline
    "check_draft", "acheck_draft", "coverage_of", "DraftNotFound",
]
