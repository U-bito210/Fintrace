"""Declared wording tables for disclosure version and 会计口径 signals.

Why a separate module
---------------------
CARD-02 labels a filing's disclosure version; CARD-04 classifies 口径/重述 signals.
Both needs are driven by the same Chinese wording, and the project's rule is one
table, not two that drift apart. The table therefore cannot live inside either
card: ``src/cn/analysis`` (CARD-03/04) already imports ``src/cn/normalization``
(CARD-02), so a table in the analysis package would make CARD-02's import a
cycle. This module sits below both and imports nothing but the standard library.

Ownership
---------
The wording is **CARD-04's declared vocabulary** (that card owns 口径 semantics);
CARD-02 only *consumes* the version patterns to label facts. Adding a phrase is a
CARD-04 decision, and both cards change behaviour together — which is the point.

What this is not
----------------
A seed table, not a validated corpus. It was written from the standard A-share
wording for correction announcements and restatement notes; it has **not** been
fitted against a real filing corpus (none is available offline — see CARD-04 §9).
Nothing here may be read as a claim that a restatement happened: the tables
recognise *wording*, and the decision that a signal applies stays with CARD-04.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Disclosure version (CARD-02 §7.3 rule 5 labels; CARD-04 confirms)
# ---------------------------------------------------------------------------

#: A 更正公告 supersedes an earlier restatement of the *same* figure, so the
#: correction family is tested first: labelling a newer supersession with the
#: older category would re-enable a superseded value.
DISCLOSURE_CORRECTED = re.compile(r"更正|修正")
DISCLOSURE_RESTATED = re.compile(r"追溯重述|重述|追溯调整|重新表述")

#: Column-header tokens -> version token, read off a statement's header row.
#: A 前期差错更正 note prints the *same* period twice (调整前 / 调整后); without
#: this the two columns were both stamped AS_REPORTED and collided inside one
#: series.
#:
#: ORDER IS LOAD-BEARING: the 前/未调整 family is tested before the bare
#: "重述"/"调整后" alternatives, because "追溯调整前" contains both "调整前" and
#: the longer "追溯调整" phrase — a greedy later rule would label the as-reported
#: column as restated, exactly backwards.
VERSION_COLUMN_KEYWORDS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"更正前|修正前|调整前|重述前|追溯调整前|未调整"), "as_reported"),
    (re.compile(r"更正后|修正后"), "corrected"),
    (re.compile(r"调整后|重述后|追溯调整后|追溯调整|已重述|重述"), "restated"),
)


# ---------------------------------------------------------------------------
# 会计口径 / 重述 wording (CARD-04)
# ---------------------------------------------------------------------------

#: Signal ids mirror :class:`src.cn.analysis.scope_signals.ScopeSignalType` values.
#: They are plain strings here so the lexicon stays importable from CARD-02
#: without dragging the analysis package in.
SIGNAL_RESTATED = "restated"
SIGNAL_ACCOUNTING_POLICY_CHANGE = "accounting_policy_change"
SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION = "prior_period_error_correction"
SIGNAL_CONSOLIDATION_SCOPE_CHANGE = "consolidation_scope_change"
SIGNAL_COMPARABLE_PERIOD_ADJUSTED = "comparable_period_adjusted"
SIGNAL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class WordingRule:
    """One deterministic text rule.

    ``context`` is an optional *additional* pattern that must also be present.
    It exists to keep a broad word from firing on an unrelated sentence: bare
    "重分类" shows up in ordinary accounting prose, whereas "报表项目…重分类"
    is a comparability statement. A rule with an unmet context does not fire at
    all — it is not downgraded to a weaker signal, because a near-miss is not
    evidence of anything.
    """

    rule_id: str
    signal: str
    pattern: re.Pattern[str]
    context: Optional[re.Pattern[str]]
    note: str
    #: Materiality is *asserted* by the note, never inferred from a magnitude we
    #: would have to threshold. ``immaterial`` requires the filing to say so.
    asserts_immaterial: re.Pattern[str] = re.compile(r"不重大|无重大影响|影响不重大|不构成重大")


#: Evaluated in order; the first rule whose pattern (and context) matches wins, so
#: the most specific wording must come first (「前期差错更正」 before 「更正」).
#: Evaluated in order; **the first matching rule wins**, so the list is sorted
#: from most to least specific. Two consequences worth stating:
#:
#: * 「前期差错更正」 precedes the bare 「更正」, otherwise the heading would be
#:   reported under the generic rule and lose the reason a reader needs;
#: * 「可比期间数据已调整」 precedes the bare 「追溯调整」, because naming the
#:   affected comparable period is strictly more informative than saying that
#:   *something* was restated.
#:
#: A rule matching a near-miss (pattern hit, context unmet) does not fire at all
#: and does not fall through to a weaker rule — a near-miss is not evidence.
SCOPE_WORDING: Tuple[WordingRule, ...] = (
    WordingRule(
        rule_id="zh_prior_period_error_correction",
        signal=SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION,
        pattern=re.compile(r"前期(会计)?差错更正|会计差错更正|差错更正"),
        context=None,
        note="年报附注/公告的『前期差错更正』表述",
    ),
    WordingRule(
        rule_id="zh_comparable_period_adjusted",
        signal=SIGNAL_COMPARABLE_PERIOD_ADJUSTED,
        pattern=re.compile(r"可比期间|上年同期|上期|比较数据|前期数据"),
        context=re.compile(r"调整|重述|更正"),
        note="『可比期间数据已调整/重述』表述（点名了受影响的比较期间，比泛化的『重述』更具体，故优先匹配）",
    ),
    WordingRule(
        rule_id="zh_consolidation_scope_change",
        signal=SIGNAL_CONSOLIDATION_SCOPE_CHANGE,
        pattern=re.compile(
            r"合并范围(发生变化|发生变化|变更|变化|调整)"
            r"|合并财务报表范围(发生变化|变更|变化)"
            r"|纳入合并范围|不再纳入合并|新增子公司|处置子公司|新设子公司"
        ),
        context=None,
        note="合并范围变化",
    ),
    WordingRule(
        rule_id="zh_accounting_policy_change",
        signal=SIGNAL_ACCOUNTING_POLICY_CHANGE,
        pattern=re.compile(r"会计政策变更|会计政策发生变化|重要会计政策变更|执行新.{0,8}准则|采用新.{0,8}准则"),
        context=None,
        note="会计政策变更（含新准则首次执行）",
    ),
    WordingRule(
        rule_id="zh_accounting_estimate_change",
        signal=SIGNAL_ACCOUNTING_POLICY_CHANGE,
        pattern=re.compile(r"会计估计变更"),
        context=None,
        note="会计估计变更（与政策变更同属口径变更，信号类型合并，规则可区分）",
    ),
    WordingRule(
        rule_id="zh_traceable_restatement",
        signal=SIGNAL_RESTATED,
        pattern=re.compile(r"追溯重述|追溯调整|重述|重新表述"),
        context=None,
        note="『追溯重述/追溯调整/重述』表述（未点名比较期间时）",
    ),
    WordingRule(
        rule_id="zh_correction_disclosure",
        signal=SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION,
        pattern=re.compile(r"更正公告|更正后|更正前|予以更正|进行更正|更正"),
        context=None,
        note="通用『更正』表述（更正公告标题或正文）",
    ),
    WordingRule(
        rule_id="zh_reclassification_unclassified",
        signal=SIGNAL_UNKNOWN,
        pattern=re.compile(r"重分类|重新分类|重列"),
        context=re.compile(r"报表项目|报表|科目|数据|金额"),
        note="重分类/重列等口径调整，不在已知信号类型内，交人工复核",
    ),
)


__all__ = [
    "DISCLOSURE_CORRECTED",
    "DISCLOSURE_RESTATED",
    "SCOPE_WORDING",
    "SIGNAL_ACCOUNTING_POLICY_CHANGE",
    "SIGNAL_COMPARABLE_PERIOD_ADJUSTED",
    "SIGNAL_CONSOLIDATION_SCOPE_CHANGE",
    "SIGNAL_PRIOR_PERIOD_ERROR_CORRECTION",
    "SIGNAL_RESTATED",
    "SIGNAL_UNKNOWN",
    "VERSION_COLUMN_KEYWORDS",
    "WordingRule",
]
