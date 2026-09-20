"""CARD-04: 会计口径与重述检测 (accounting scope & restatement detection).

The problem this solves
-----------------------
Real A-share disclosure moves: 原披露 → 更正公告 → 重述. A naive pipeline computes
YoY on a superseded figure and reports a growth rate that never happened. Two
mechanisms already exist for *labelling* a version (CARD-02's ``Version``) and for
keeping versions apart inside one series (CARD-03's ``(metric, scope, version)``
key), but nothing decided **whether the two ends of a comparison are comparable
in the first place**. That decision is the whole point of a research agent: §9's
rule is 宁可阻断也不误算.

What this module is not
-----------------------
* **Not a verdict.** A signal says "the filing discloses something that changes
  what the numbers on either side of a comparison mean". It never says the
  company's earnings are worse, that a restatement was fraudulent, or that
  management is hiding anything — no causal word appears anywhere in this file.
* **Not a complete detector.** §9 requires a coverage statement, and
  :func:`detection_coverage` provides it. Matching is deterministic text rules
  over a *declared seed table* (:mod:`src.cn.disclosure_lexicon`); it has not been
  fitted against a real filing corpus, so a wording outside the table yields no
  signal. Silence here means "no rule matched", never "nothing happened".
* **Not LLM-assisted.** §7.3: the model does not participate in signal
  determination. Every signal is reproducible from the document text alone.

Where the evidence comes from
-----------------------------
Signals bind to CARD-01 facts, not to normalized values, so a correction
*announcement* — which declares no report period and is therefore never
normalised — still produces signals. Each ``evidence_id`` resolves to a page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..disclosure_lexicon import (
    SCOPE_WORDING,
    SIGNAL_ACCOUNTING_POLICY_CHANGE,
    SIGNAL_COMPARABLE_PERIOD_ADJUSTED,
    SIGNAL_CONSOLIDATION_SCOPE_CHANGE,
    SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION,
    SIGNAL_RESTATED,
    SIGNAL_UNKNOWN,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Detection surfaces (CARD-04 §7.1). Recorded per signal so a reader can see
#: *where* the claim came from, and so the §9 coverage statement is concrete.
SURFACE_TITLE = "公告标题"
SURFACE_CHAPTER = "附注章节"
SURFACE_PARAGRAPH = "正文段落"
SURFACE_TABLE_COLUMN = "表格版本列"

#: Anything longer than this is truncated in ``detail``. The full text stays on
#: the bound evidence fact; this field exists to make a signal readable in a UI.
_DETAIL_LIMIT = 120


class ScopeSignalType(str, Enum):
    """The six signal types of CARD-04 §5.

    Values mirror the plain strings in :mod:`src.cn.disclosure_lexicon` so the
    lexicon needs no import of this module.
    """

    RESTATED = SIGNAL_RESTATED
    ACCOUNTING_POLICY_CHANGE = SIGNAL_ACCOUNTING_POLICY_CHANGE
    PRIOR_PERIOD_ERROR_CORRECTION = SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION
    CONSOLIDATION_SCOPE_CHANGE = SIGNAL_CONSOLIDATION_SCOPE_CHANGE
    COMPARABLE_PERIOD_ADJUSTED = SIGNAL_COMPARABLE_PERIOD_ADJUSTED
    UNKNOWN = SIGNAL_UNKNOWN


class SignalSeverity(str, Enum):
    """§8 edge case 3: a disclosed change is still a signal when it is immaterial.

    Materiality is read off the wording, never thresholded off a magnitude. A
    threshold would need a size we do not have (the signal often precedes the
    figure), and inventing one would be a guess.
    """

    MATERIAL = "material"            # the note asserts an impact / quotes amounts
    IMMATERIAL = "immaterial"        # the note itself says 不重大 / 无重大影响
    UNDETERMINED = "undetermined"    # disclosed, impact not stated


class ComparabilityDecision(str, Enum):
    BLOCK_YOY = "block_yoy"
    USE_ADJUSTED = "use_adjusted"
    WARNING = "warning"


#: Presentation labels. Lives beside the enum (not in the UI) so the workbench,
#: the Agent tool and any report all 说同一句话 about the same decision.
SIGNAL_TYPE_LABELS: Dict[str, str] = {
    ScopeSignalType.RESTATED.value: "财务重述",
    ScopeSignalType.ACCOUNTING_POLICY_CHANGE.value: "会计政策变更",
    ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION.value: "前期差错更正",
    ScopeSignalType.CONSOLIDATION_SCOPE_CHANGE.value: "合并范围变更",
    ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED.value: "可比期间数据已调整",
    ScopeSignalType.UNKNOWN.value: "未分类口径事项",
}

COMPARABILITY_LABELS: Dict[str, str] = {
    ComparabilityDecision.BLOCK_YOY.value: "阻断同比",
    ComparabilityDecision.USE_ADJUSTED.value: "改用调整后数据",
    ComparabilityDecision.WARNING.value: "提示（不阻断）",
}


#: §6's decision table, as data. ``None`` in the first slot means "the rule does
#: not depend on whether adjusted data is available".
#:
#: The ordering of the *aggregate* precedence is a separate constant below; this
#: table decides one signal at a time.
DECISION_RULES: Tuple[Tuple[ScopeSignalType, Optional[bool], ComparabilityDecision], ...] = (
    # restatement / error correction: use the restated figure when we actually
    # have it, otherwise refuse the comparison outright.
    (ScopeSignalType.RESTATED, True, ComparabilityDecision.USE_ADJUSTED),
    (ScopeSignalType.RESTATED, False, ComparabilityDecision.BLOCK_YOY),
    (ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION, True, ComparabilityDecision.USE_ADJUSTED),
    (ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION, False, ComparabilityDecision.BLOCK_YOY),
    # the restated comparative is already in the figures — nothing to switch to.
    (ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED, None, ComparabilityDecision.USE_ADJUSTED),
    # a policy change is only comparable when applied retrospectively; without a
    # restated comparative it breaks comparability rather than fixing it.
    (ScopeSignalType.ACCOUNTING_POLICY_CHANGE, True, ComparabilityDecision.USE_ADJUSTED),
    (ScopeSignalType.ACCOUNTING_POLICY_CHANGE, False, ComparabilityDecision.WARNING),
    # combining a different set of entities damages comparability by construction,
    # and no adjustment can repair it.
    (ScopeSignalType.CONSOLIDATION_SCOPE_CHANGE, None, ComparabilityDecision.WARNING),
    # §9 fails closed: an unclassified disclosure that touches the statements is
    # treated as comparability-breaking until a human says otherwise.
    (ScopeSignalType.UNKNOWN, None, ComparabilityDecision.BLOCK_YOY),
)

#: When a document carries several signals, the strictest wins. Blocking beats
#: substituting beats warning: a reader who sees 阻断 must not be told 警告.
DECISION_PRECEDENCE: Tuple[ComparabilityDecision, ...] = (
    ComparabilityDecision.BLOCK_YOY,
    ComparabilityDecision.USE_ADJUSTED,
    ComparabilityDecision.WARNING,
)

#: Period wording -> period key. Only forms that state a full fiscal unit are
#: mapped; an unstated period stays unstated (never inferred from context).
_PERIOD_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(20\d{2})\s*年\s*度"), "FY"),
    (re.compile(r"(20\d{2})\s*年\s*半年度"), "H1"),
    (re.compile(r"(20\d{2})\s*年\s*中期"), "H1"),
    (re.compile(r"(20\d{2})\s*年\s*第一季度"), "Q1"),
    (re.compile(r"(20\d{2})\s*年\s*第二季度"), "Q2"),
    (re.compile(r"(20\d{2})\s*年\s*第三季度"), "Q3"),
    (re.compile(r"(20\d{2})\s*年\s*第四季度"), "Q4"),
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountingScopeSignal:
    """One detected 口径/重述 signal, always bound to located evidence."""

    signal_type: ScopeSignalType
    company: str
    periods_affected: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    detected_at: str
    detector_rule: str
    severity: SignalSeverity = SignalSeverity.UNDETERMINED
    surfaces: Tuple[str, ...] = ()
    detail: str = ""
    document_id: str = ""
    #: Evidence for the *adjusted* figures (非 AS_REPORTED 列声明或更正后金额语句).
    #: Empty means the filing disclosed the event without giving us a usable
    #: restated value — which is precisely when §9 says to block.
    adjusted_evidence_ids: Tuple[str, ...] = ()

    @property
    def adjusted_data_available(self) -> bool:
        return bool(self.adjusted_evidence_ids)

    @property
    def comparability(self) -> ComparabilityDecision:
        return comparability_decision(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_type": self.signal_type.value,
            "company": self.company,
            "periods_affected": list(self.periods_affected),
            "evidence_ids": list(self.evidence_ids),
            "detected_at": self.detected_at,
            "detector_rule": self.detector_rule,
            "severity": self.severity.value,
            "surfaces": list(self.surfaces),
            "detail": self.detail,
            "document_id": self.document_id,
            "adjusted_evidence_ids": list(self.adjusted_evidence_ids),
            "adjusted_data_available": self.adjusted_data_available,
            "comparability": self.comparability.value,
        }


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def comparability_decision(signal: AccountingScopeSignal) -> ComparabilityDecision:
    """§6: ``'block_yoy' | 'use_adjusted' | 'warning'``, from :data:`DECISION_RULES`.

    Pure function of the signal, so a caller can re-derive the decision from a
    serialised signal without re-running detection.
    """
    available = signal.adjusted_data_available
    for signal_type, needs_adjusted, decision in DECISION_RULES:
        if signal_type is not signal.signal_type:
            continue
        if needs_adjusted is None or needs_adjusted is available:
            return decision
    # An unmapped type must never silently pass as comparable.
    return ComparabilityDecision.BLOCK_YOY


def aggregate_comparability(
    signals: Iterable[AccountingScopeSignal],
) -> Optional[ComparabilityDecision]:
    """The strictest decision across a document's signals, or ``None`` if clean."""
    decisions = {signal.comparability for signal in signals}
    for decision in DECISION_PRECEDENCE:
        if decision in decisions:
            return decision
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _attribute(fact: object, name: str, default: Any = None) -> Any:
    return getattr(fact, name, default)


def _fact_id(fact: object) -> str:
    return str(_attribute(fact, "fact_id", "") or "")


def _periods_in(text: str) -> Tuple[str, ...]:
    """Fiscal period keys stated *verbatim* in the text; empty when unstated."""
    found: List[str] = []
    for pattern, tag in _PERIOD_PATTERNS:
        for match in pattern.finditer(text):
            key = f"{match.group(1)}{tag}"
            if key not in found:
                found.append(key)
    return tuple(found)


def _truncate(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed if len(collapsed) <= _DETAIL_LIMIT else collapsed[:_DETAIL_LIMIT] + "…"


def _surface_for(fact: object) -> str:
    section = str(_attribute(fact, "section", "") or "")
    if section == "标题":
        return SURFACE_TITLE
    if section in ("正文", "日期", ""):
        return SURFACE_PARAGRAPH
    return SURFACE_CHAPTER


def _is_immaterial_assertion(text: str) -> bool:
    for rule in SCOPE_WORDING:
        if rule.asserts_immaterial.search(text):
            return True
    return False


def _severity_for(text: str) -> SignalSeverity:
    """Materiality as *asserted* by the note, never as measured by us."""
    if _is_immaterial_assertion(text):
        return SignalSeverity.IMMATERIAL
    if re.search(r"\d", text):
        return SignalSeverity.MATERIAL
    return SignalSeverity.UNDETERMINED


def _collect_adjusted_evidence(facts: Sequence[object]) -> Tuple[str, ...]:
    """Facts that carry an explicitly non-``AS_REPORTED`` figure.

    Two shapes count, and only these two:

    * a column header declaring a version ("调整后金额" / "上期金额（已重述）"),
      which CARD-01 tags and CARD-02 then labels — the restated column exists;
    * a sentence stating a post-correction amount ("更正后…为 1,000,000 元"), which
      is how a correction announcement delivers the replacement value (it has no
      statement table at all).

    Both require an *explicit* marker. Treating a plain figure as "adjusted"
    would make every filing comparable and disable the block path entirely.
    """
    ids: List[str] = []
    for fact in facts:
        meta = _attribute(fact, "meta", None) or {}
        label = str(meta.get("version_label") or "").strip().lower()
        identifier = _fact_id(fact)
        if not identifier:
            continue
        if label in ("restated", "corrected"):
            ids.append(identifier)
            continue
        text = str(_attribute(fact, "raw_text", "") or "")
        if re.search(r"(更正后|调整后|重述后|修正后)", text) and re.search(r"\d", text):
            ids.append(identifier)
    return tuple(dict.fromkeys(ids))


def _detect_text_signals(
    facts: Sequence[object],
    documents: Dict[str, object],
    adjusted_by_document: Dict[str, Tuple[str, ...]],
    detected_at: str,
) -> List[AccountingScopeSignal]:
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str]] = []

    for fact in facts:
        if str(_attribute(fact, "kind", "text")) != "text":
            continue
        text = str(_attribute(fact, "raw_text", "") or "").strip()
        if not text:
            continue
        document_id = str(_attribute(fact, "document_id", "") or "")
        for rule in SCOPE_WORDING:
            if not rule.pattern.search(text):
                continue
            if rule.context is not None and not rule.context.search(text):
                continue
            key = (document_id, rule.signal, rule.rule_id)
            if key not in grouped:
                grouped[key] = {
                    "evidence": [],
                    "surfaces": [],
                    "periods": [],
                    "detail": _truncate(text),
                    "severity": _severity_for(text),
                }
                order.append(key)
            bucket = grouped[key]
            identifier = _fact_id(fact)
            if identifier and identifier not in bucket["evidence"]:
                bucket["evidence"].append(identifier)
            surface = _surface_for(fact)
            if surface not in bucket["surfaces"]:
                bucket["surfaces"].append(surface)
            for period in _periods_in(text):
                if period not in bucket["periods"]:
                    bucket["periods"].append(period)
            # An immaterial assertion anywhere in the group is the filing's own
            # statement about that change, so it wins over an earlier amount.
            if bucket["severity"] is not SignalSeverity.IMMATERIAL:
                bucket["severity"] = _severity_for(text)
            break  # first matching rule wins (the lexicon is order-significant)

    signals: List[AccountingScopeSignal] = []
    for key in order:
        document_id, signal_value, rule_id = key
        bucket = grouped[key]
        document = documents.get(document_id)
        signals.append(
            AccountingScopeSignal(
                signal_type=ScopeSignalType(signal_value),
                company=str(_attribute(document, "company", "") or ""),
                periods_affected=tuple(bucket["periods"]),
                evidence_ids=tuple(bucket["evidence"]),
                detected_at=detected_at,
                detector_rule=rule_id,
                severity=bucket["severity"],
                surfaces=tuple(bucket["surfaces"]),
                detail=bucket["detail"],
                document_id=document_id,
                adjusted_evidence_ids=adjusted_by_document.get(document_id, ()),
            )
        )
    return signals


def _detect_table_signals(
    facts: Sequence[object],
    documents: Dict[str, object],
    adjusted_by_document: Dict[str, Tuple[str, ...]],
    detected_at: str,
) -> List[AccountingScopeSignal]:
    """A statement *table* that carries two versions of the same column set.

    This is §8 edge case 2 and the only place where the number of versions is
    visible structurally: a 前期差错更正 note prints 调整前 and 调整后 side by side.
    The columns themselves are labelled by CARD-01; the conclusion that the
    comparable period has been restated is drawn here.
    """
    by_table: Dict[Tuple[str, str], Dict[int, Tuple[str, str]]] = {}
    order: List[Tuple[str, str]] = []

    for fact in facts:
        meta = _attribute(fact, "meta", None) or {}
        label = str(meta.get("version_label") or "").strip().lower()
        if label not in ("as_reported", "restated", "corrected"):
            continue
        table = str(_attribute(fact, "table", "") or "")
        if not table:
            continue
        column = _attribute(fact, "column", None)
        if column is None:
            continue
        document_id = str(_attribute(fact, "document_id", "") or "")
        key = (document_id, table)
        if key not in by_table:
            by_table[key] = {}
            order.append(key)
        # The header cell is the one with no value; it is the evidence a reader
        # needs to jump to, so it beats a data cell for the same column.
        is_header_cell = _attribute(fact, "raw_value", None) is None
        existing = by_table[key].get(int(column))
        if existing is None or is_header_cell:
            by_table[key][int(column)] = (label, _fact_id(fact))

    signals: List[AccountingScopeSignal] = []
    for key in order:
        document_id, table = key
        columns = by_table[key]
        labels = {label for label, _ in columns.values()}
        if not ({"restated", "corrected"} & labels):
            continue  # an as-reported-only table is simply a table
        document = documents.get(document_id)
        evidence = tuple(
            identifier for _, identifier in columns.values() if identifier
        )
        signals.append(
            AccountingScopeSignal(
                signal_type=ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED,
                company=str(_attribute(document, "company", "") or ""),
                periods_affected=(),
                evidence_ids=evidence,
                detected_at=detected_at,
                detector_rule="table_version_columns",
                severity=SignalSeverity.MATERIAL,
                surfaces=(SURFACE_TABLE_COLUMN,),
                detail=_truncate(
                    f"表『{table}』版本列：" + "、".join(sorted(labels))
                ),
                document_id=document_id,
                adjusted_evidence_ids=adjusted_by_document.get(document_id, ()),
            )
        )
    return signals


def detect_scope_signals(
    facts: Sequence[object],
    documents: object,
    *,
    detected_at: Optional[str] = None,
) -> List[AccountingScopeSignal]:
    """CARD-04 §6: locate every 口径/重述 signal in the given facts.

    ``documents`` is a :class:`DocumentRecord`, a sequence of them, or a mapping
    of ``document_id -> record``; the record supplies the company name only.
    """
    stamp = detected_at or datetime.now(timezone.utc).isoformat()
    fact_list = list(facts)
    index = _document_index(documents)
    adjusted = {
        document_id: _collect_adjusted_evidence(
            [f for f in fact_list if str(_attribute(f, "document_id", "") or "") == document_id]
        )
        for document_id in index
    }
    # Facts whose document is not in the index still belong to some document_id;
    # collect for those too rather than reporting "no adjusted data" for them.
    for fact in fact_list:
        document_id = str(_attribute(fact, "document_id", "") or "")
        if document_id and document_id not in adjusted:
            adjusted[document_id] = _collect_adjusted_evidence(
                [f for f in fact_list if str(_attribute(f, "document_id", "") or "") == document_id]
            )

    signals = _detect_text_signals(fact_list, index, adjusted, stamp)
    signals.extend(_detect_table_signals(fact_list, index, adjusted, stamp))
    return _dedupe(signals)


def _document_index(documents: object) -> Dict[str, object]:
    if documents is None:
        return {}
    if isinstance(documents, dict):
        return {str(key): value for key, value in documents.items()}
    if isinstance(documents, (list, tuple)):
        return {
            str(_attribute(doc, "document_id", "") or ""): doc
            for doc in documents
            if _attribute(doc, "document_id", None)
        }
    identifier = str(_attribute(documents, "document_id", "") or "")
    return {identifier: documents} if identifier else {}


def _dedupe(signals: Sequence[AccountingScopeSignal]) -> List[AccountingScopeSignal]:
    """One signal per (document, type, rule); later duplicates merge their evidence."""
    merged: Dict[Tuple[str, str, str], AccountingScopeSignal] = {}
    order: List[Tuple[str, str, str]] = []
    for signal in signals:
        key = (signal.document_id, signal.signal_type.value, signal.detector_rule)
        if key not in merged:
            merged[key] = signal
            order.append(key)
            continue
        previous = merged[key]
        merged[key] = AccountingScopeSignal(
            signal_type=previous.signal_type,
            company=previous.company,
            periods_affected=tuple(
                dict.fromkeys(previous.periods_affected + signal.periods_affected)
            ),
            evidence_ids=tuple(dict.fromkeys(previous.evidence_ids + signal.evidence_ids)),
            detected_at=previous.detected_at,
            detector_rule=previous.detector_rule,
            severity=previous.severity,
            surfaces=tuple(dict.fromkeys(previous.surfaces + signal.surfaces)),
            # Merged, not dropped: two tables can trigger the same rule, and a
            # reader who sees one table named while the evidence lists two is
            # being told less than the evidence knows.
            detail=_merge_detail(previous.detail, signal.detail),
            document_id=previous.document_id,
            adjusted_evidence_ids=tuple(
                dict.fromkeys(previous.adjusted_evidence_ids + signal.adjusted_evidence_ids)
            ),
        )
    return [merged[key] for key in order]


def _merge_detail(first: str, second: str) -> str:
    """Join two signal details that the same rule produced, without repeating."""
    parts: List[str] = []
    for text in (first, second):
        cleaned = (text or "").strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return _truncate("；".join(parts))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def detection_coverage() -> Dict[str, Any]:
    """§9's 检测覆盖范围声明 — what was looked for, where, and what is out of scope.

    Published with every report instead of being buried in a docstring: a reader
    who sees no signal must be able to tell that "nothing matched these rules"
    rather than "the filing is clean".
    """
    return {
        "rule_count": len(SCOPE_WORDING),
        "rules": [
            {
                "rule_id": rule.rule_id,
                "signal_type": rule.signal,
                "note": rule.note,
                "requires_context": rule.context is not None,
            }
            for rule in SCOPE_WORDING
        ],
        "extra_rules": ["table_version_columns"],
        "surfaces": [SURFACE_TITLE, SURFACE_CHAPTER, SURFACE_PARAGRAPH, SURFACE_TABLE_COLUMN],
        "limits": [
            "关键词表为种子表，未在真实披露语料上拟合；表外措辞不产生信号。",
            "无信号 = 未命中规则，不等于不存在口径变化。",
            "附注章节归属按页内最近标题判定，跨页续写不继承标题。",
            "更正公告与原报告分属两个文档时的跨文档关联依赖 CARD-13，本模块不做。",
        ],
    }


def build_scope_report(
    facts: Sequence[object],
    documents: object = None,
    *,
    detected_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Signals + per-document comparability + the coverage statement."""
    facts_list = list(facts)
    signals = detect_scope_signals(facts_list, documents, detected_at=detected_at)
    by_document: Dict[str, List[AccountingScopeSignal]] = {}
    for signal in signals:
        by_document.setdefault(signal.document_id, []).append(signal)

    comparability = [
        {
            "document_id": document_id,
            "decision": aggregate_comparability(items).value if aggregate_comparability(items) else None,
            # De-duplicated: two rules can find the same signal type, and a reader
            # counting "how many kinds of change" must not be misled by that.
            "signal_types": list(dict.fromkeys(item.signal_type.value for item in items)),
            "evidence_ids": sorted({eid for item in items for eid in item.evidence_ids}),
        }
        for document_id, items in by_document.items()
    ]
    return {
        "status": "ok",
        "signal_count": len(signals),
        "signals": [signal.to_dict() for signal in signals],
        "comparability": comparability,
        "blocked_documents": [
            row["document_id"] for row in comparability if row["decision"] == ComparabilityDecision.BLOCK_YOY.value
        ],
        "coverage": detection_coverage(),
    }


__all__ = [
    "AccountingScopeSignal",
    "COMPARABILITY_LABELS",
    "ComparabilityDecision",
    "DECISION_PRECEDENCE",
    "DECISION_RULES",
    "SIGNAL_TYPE_LABELS",
    "SURFACE_CHAPTER",
    "SURFACE_PARAGRAPH",
    "SURFACE_TABLE_COLUMN",
    "SURFACE_TITLE",
    "ScopeSignalType",
    "SignalSeverity",
    "aggregate_comparability",
    "build_scope_report",
    "comparability_decision",
    "detect_scope_signals",
    "detection_coverage",
]
