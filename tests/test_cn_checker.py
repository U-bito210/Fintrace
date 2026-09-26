"""Offline tests for CARD-05 研报纠错系统.

Two acceptance levels, and the split matters (§15):

**A. Checker level (deterministic).** Given a correct ``DraftClaim``, the rules are
100% reproducible: one hand-built case per error class, a clean draft that must
produce *nothing*, the unit ladder, the rounding boundary, and the causal warning.
No model is involved anywhere on this path, so none of these tests can be flaky.

**B. End-to-end level.** A real PDF goes through CARD-01 parsing → rule extraction →
deterministic checking → a finding whose evidence resolves back to a source page,
plus the Agent tool and the §10 validator. The *LLM* extractor is exercised only
through a scripted offline client, because "model quality" is CARD-11's metric and
never a gate on this card (§15 B).

Two invariants get their own tests because they break silently and expensively:

* **误报为零.** The two clean drafts in the corpus are load-bearing — a checker
  that flags correct sentences is worse than one that misses errors (§9).
* **checker.py imports no model client.** Asserted against the shipped source text,
  not the import graph, because a deferred ``import`` inside a rule is invisible to
  ``sys.modules`` and would break the card's central guarantee just as completely.

Everything is offline: synthetic fixtures, hand-built facts, a scripted client, no
network, no wall clock.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.cn.checker.benchmark import Score, evaluate_corpus, format_markdown
from src.cn.checker.checker import check_report, is_forward_looking, summarize
from src.cn.checker.draft_corpus import (
    DRAFT_SPECS,
    MARKET_DOCUMENT,
    market_documents,
    market_normalized_facts,
)
from src.cn.checker.extractor import EXTRACTOR_LLM, extract_claims
from src.cn.checker.facts import FactIndex, format_amount
from src.cn.checker.models import (
    CHECK_TYPE_ORDER,
    CLAIM_TYPE_MAP,
    CONFIDENCE_BY_MATCH,
    SEVERITY_BY_CHECK,
    CheckType,
    ClaimType,
    DraftClaim,
    GraphClaimType,
    MatchKind,
    Severity,
)
from src.cn.checker.pipeline import (
    DraftNotFound,
    acheck_draft,
    check_draft,
    coverage_of,
    selected_reference_documents,
)
from src.cn.checker.rule_extractor import EXTRACTOR_RULES, extract_claims_rules
from src.cn.checker.sentences import (
    DraftSentence,
    sentences_from_facts,
    split_sentences,
)
from src.cn.documents.models import (
    FACT_KIND_TABLE_CELL,
    FACT_KIND_TEXT,
    DocumentRecord,
    ExtractedFact,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.normalization import NormalizedFact, PeriodKind, Scope, Version
from src.validation import (
    check_source_is_model_free,
    checker_module_source,
    validate_checker_module,
    validate_checker_report,
)

FIXTURES = Path(__file__).parent / "fixtures"
DRAFTS = FIXTURES / "report_drafts"
FILINGS = FIXTURES / "documents"
AS_OF = "2026-05-01"

#: Reference filings the corpus drafts cite.
REF_ANNUAL_PLAIN = "annual_01_profit_continued.pdf"
REF_ANNUAL_RESTATED = "annual_04_restated_comparative.pdf"
REF_ANNUAL_RECLASS = "annual_05_reclassification.pdf"
REF_CORRECTION = "announcement_04_correction.pdf"


# --------------------------------------------------------------------------- #
# builders — hand-built inputs keep level A purely deterministic
# --------------------------------------------------------------------------- #

def _nf(
    metric: str,
    value: float,
    *,
    period_key: str = "2025FY",
    scope: Optional[str] = None,
    unit: str = "元",
    page: Optional[int] = 1,
    document_id: str = "DOC-A",
    source_table: str = "合并利润表",
    version: Version = Version.AS_REPORTED,
    available_at: Optional[str] = "2026-04-25",
    row: int = 1,
    column: int = 1,
) -> NormalizedFact:
    """One CARD-02 normalized fact, in base units, bound to a document and page."""
    identifier = f"nf_{document_id}_{metric}_{period_key}_{scope or 'X'}_{page}_{row}_{column}"
    return NormalizedFact(
        normalized_fact_id=identifier,
        metric=metric,
        value=float(value),
        currency="CNY",
        period_key=period_key,
        period_kind=(
            PeriodKind.POINT_IN_TIME if metric in ("raw_price", "diluted_shares")
            else PeriodKind.YTD_CUMULATIVE
        ),
        scope=Scope.coerce(scope) if scope else None,
        version=version,
        available_at=available_at,
        source_fact_id=f"fact_{identifier}",
        document_id=document_id,
        page=page,
        row=row,
        column=column,
        raw_value=str(value),
        source_unit=unit,
        source_table=source_table,
    )


def _claim(
    *,
    claim_id: str = "c1",
    sentence: str = "",
    metric: Optional[str] = None,
    period: Optional[str] = "2025FY",
    value: Optional[float] = None,
    value_literal: Optional[str] = None,
    unit: Optional[str] = None,
    growth: Optional[float] = None,
    growth_literal: Optional[str] = None,
    multiple: Optional[str] = None,
    scope: Optional[str] = None,
    references: Sequence[str] = (),
    claim_type: str = ClaimType.FACTUAL.value,
    page: int = 1,
    causal_antecedent: Optional[str] = None,
    causal_consequent: Optional[str] = None,
) -> DraftClaim:
    return DraftClaim(
        claim_id=claim_id,
        sentence=sentence or "句子",
        page=page,
        metric=metric,
        metric_label=metric,
        period=period,
        value=value,
        value_literal=value_literal,
        unit=unit,
        growth=growth,
        growth_literal=growth_literal,
        valuation_multiple=multiple,
        source_reference=references[0] if references else None,
        source_references=tuple(references),
        claim_type=claim_type,
        scope=scope,
        causal_antecedent=causal_antecedent,
        causal_consequent=causal_consequent,
        extractor="given",
    )


#: The two sides, as an extractor would report them for the standard causal sentence.
_CAUSAL_SIDES = dict(
    causal_antecedent="受原材料价格上涨推动",
    causal_consequent="2025年营业收入下降",
)


def _document(document_id: str, filename: str, *, document_type: str = "annual",
              company: str = "示例制造股份有限公司",
              publication_date: Optional[str] = "2026-04-25") -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        sha256="0" * 64,
        filename=filename,
        source="admin_import",
        document_type=document_type,
        company=company,
        report_period="2025FY",
        publication_date=publication_date,
    )


def _baseline_facts() -> List[NormalizedFact]:
    """A small, fully-specified filing: every rule below is anchored on it.

    Chosen so each planted defect has exactly **one** interpretation — e.g. 母公司
    revenue (550,000) sits beside 合并 revenue (1,100,000) so that quoting the
    consolidated figure as the parent-company one is a 口径 error rather than a
    coincidence, and the 更正 table carries a 调整前/调整后 pair so supersession is
    established by a table-level structure and nothing else.
    """
    return [
        # 合并利润表 — the audited statement body.
        _nf("revenue", 1_100_000, row=1, column=1),
        _nf("revenue", 1_000_000, period_key="2024FY", row=2, column=1),
        _nf("operating_cost", 700_000, row=3, column=1),
        _nf("net_profit_parent", 1_100_000, row=4, column=1),
        _nf("equity_parent", 11_000_000, row=5, column=1),
        # 母公司报表 — same period, different 口径.
        _nf("revenue", 550_000, scope="母公司", row=1, column=2),
        # 市价 — needed for the PE recompute.
        _nf("raw_price", 1.10, unit="元", source_table="市价"),
        _nf("diluted_shares", 10_000_000, unit="股", source_table="市价"),
        # 前期差错更正 — a 调整前/调整后 pair inside ONE table establishes supersession.
        _nf(
            "revenue", 900_000, source_table="前期差错更正",
            version=Version.AS_REPORTED, row=1, column=1,
        ),
        _nf(
            "revenue", 950_000, source_table="前期差错更正",
            version=Version.RESTATED, row=1, column=2,
        ),
    ]


def _documents() -> List[DocumentRecord]:
    return [
        _document("DOC-A", "annual_a.pdf"),
        _document("DOC-B", "announcement_b.pdf", document_type="announcement"),
    ]


def _run(
    claims: Sequence[DraftClaim],
    facts: Optional[Sequence[NormalizedFact]] = None,
    *,
    signals: Sequence[object] = (),
    as_of: Optional[str] = None,
    documents: Optional[Sequence[DocumentRecord]] = None,
) -> List[Any]:
    """Apply ``check_report`` with the standard reference set."""
    fact_list = list(facts if facts is not None else _baseline_facts())
    return check_report(
        list(claims),
        fact_list,
        list(signals),
        documents=list(documents if documents is not None else _documents()),
        as_of=as_of,
    )


def _types(findings: Sequence[Any]) -> List[str]:
    return [item.check_type for item in findings]


# --------------------------------------------------------------------------- #
# §11 — one case per error class
# --------------------------------------------------------------------------- #

class TestThirteenCheckTypes:
    """§7.1's thirteen classes, one hand-built case each. Exactly thirteen."""

    def test_exactly_thirteen_check_types_are_declared(self):
        assert len(CHECK_TYPE_ORDER) == 13
        assert set(CHECK_TYPE_ORDER) == {member.value for member in CheckType}
        assert set(SEVERITY_BY_CHECK) == set(CHECK_TYPE_ORDER)

    def test_numeric_error(self):
        findings = _run([_claim(
            metric="revenue", value=150.0, value_literal="150", unit="万元",
            references=["DOC-A#p1"], sentence="公司2025年实现营业收入150万元。",
        )])
        assert _types(findings) == [CheckType.NUMERIC_ERROR.value]
        assert findings[0].severity == Severity.ERROR.value
        # §10 rule 2: both sides carry an explicit unit and the same period.
        assert "万元" in findings[0].original
        assert "元" in findings[0].expected
        assert "2025FY" in findings[0].expected

    def test_unit_error(self):
        findings = _run([_claim(
            metric="operating_cost", value=700_000.0, value_literal="700,000", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.UNIT_ERROR.value]
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.UNIT_CONVERTED.value]

    def test_period_error(self):
        findings = _run([_claim(
            metric="revenue", period="2024FY", value=110.0, value_literal="110",
            unit="万元", references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.PERIOD_ERROR.value]
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.PERIOD_INFERRED.value]

    def test_period_error_shows_current_value_and_both_evidence_ids(self):
        facts = [
            _nf("research_expense", 18_606_756_000, period_key="2024FY",
                scope="合并", unit="千元", page=119),
            _nf("research_expense", 18_356_108_000, period_key="2023FY",
                scope="合并", unit="千元", page=119),
        ]
        claim = _claim(
            metric="research_expense", period="2024FY", value=183.56,
            value_literal="183.56", unit="亿元", references=["DOC-A#p119"],
        )
        findings = check_report([claim], facts)
        assert _types(findings) == [CheckType.PERIOD_ERROR.value]
        assert findings[0].expected == "18,606,756千元（2024FY）"
        assert len(findings[0].evidence_ids) == 2

    def test_scope_error(self):
        """The parent-company label on the consolidated figure — the A-share classic."""
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元", scope="母公司",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.SCOPE_ERROR.value]
        finding = findings[0]
        assert "母公司" in finding.suggestion
        # `expected` is the same figure under the 口径 the draft claimed.
        assert finding.expected.startswith(format_amount(550_000, "元"))

    def test_calculation_error(self):
        findings = _run([_claim(
            metric="revenue", growth=25.0, growth_literal="25.0",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.CALCULATION_ERROR.value]
        # Recomputed through CARD-03, so the expected percentage is CARD-03's.
        assert findings[0].expected == "+10.00%"

    def test_valuation_multiple_error(self):
        findings = _run([_claim(
            multiple="市盈率 50倍", period=None, references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.VALUATION_MULTIPLE_ERROR.value]
        assert "10.0" in findings[0].expected

    def test_missing_citation(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=[],
        )])
        assert _types(findings) == [CheckType.MISSING_CITATION.value]
        assert findings[0].severity == Severity.WARNING.value

    def test_wrong_citation(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-B#p1"],
        )])
        assert _types(findings) == [CheckType.WRONG_CITATION.value]

    def test_unsupported_citation(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["不存在的年报.pdf#p1"],
        )])
        assert _types(findings) == [CheckType.UNSUPPORTED_CITATION.value]

    def test_wrong_page(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-A#p9"],
        )])
        assert _types(findings) == [CheckType.WRONG_PAGE.value]
        assert "第1页" in findings[0].expected

    def test_stale_citation(self):
        findings = _run(
            [_claim(
                metric="revenue", value=110.0, value_literal="110", unit="万元",
                references=["DOC-A#p1"],
            )],
            as_of="2026-01-01",
        )
        assert _types(findings) == [CheckType.STALE_CITATION.value]
        assert findings[0].severity == Severity.WARNING.value

    def test_ingest_time_is_not_a_publication_date(self):
        claim = _claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-A#p1"],
        )
        findings = _run(
            [claim], as_of="2025-04-01",
            documents=[_document("DOC-A", "annual_a.pdf", publication_date=None)],
        )
        assert CheckType.STALE_CITATION.value not in _types(findings)

    def test_revision_superseded(self):
        """§8 edge case 4: version before value — 调整前 is superseded by 调整后."""
        findings = _run([_claim(
            metric="revenue", value=90.0, value_literal="90", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.REVISION_SUPERSEDED.value]
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.SUPERSEDED.value]
        # `expected` is the replacement, never the superseded value.
        assert findings[0].expected.startswith(format_amount(950_000, "元"))

    def test_unsupported_causal_claim(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, metric="revenue", value=None,
            references=["DOC-A#p1"], sentence="受原材料涨价推动，2025年营业收入下降。",
        )])
        assert _types(findings) == [CheckType.UNSUPPORTED_CAUSAL_CLAIM.value]
        assert findings[0].severity == Severity.WARNING.value

    def test_all_thirteen_are_reachable(self):
        """The union of the cases above covers every declared class.

        Guards the cheap failure mode: a new check type added to the enum without a
        rule that can ever emit it would otherwise sit at zero forever.
        """
        covered = set()
        covered |= set(_types(_run([_claim(
            metric="revenue", value=150.0, value_literal="150", unit="万元",
            references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="operating_cost", value=700_000.0, unit="万元",
            references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", period="2024FY", value=110.0, unit="万元",
            references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=110.0, unit="万元", scope="母公司",
            references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", growth=25.0, references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(multiple="市盈率 50倍", period=None)])))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=110.0, unit="万元", references=[]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=110.0, unit="万元", references=["DOC-B#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=110.0, unit="万元", references=["nope.pdf#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=110.0, unit="万元", references=["DOC-A#p9"]),
        ])))
        covered |= set(_types(_run(
            [_claim(metric="revenue", value=110.0, unit="万元", references=["DOC-A#p1"])],
            as_of="2026-01-01",
        )))
        covered |= set(_types(_run([_claim(
            metric="revenue", value=90.0, unit="万元", references=["DOC-A#p1"]),
        ])))
        covered |= set(_types(_run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1"]),
        ])))
        assert covered == set(CHECK_TYPE_ORDER)


# --------------------------------------------------------------------------- #
# §9 / §11 — false-positive control
# --------------------------------------------------------------------------- #

class TestNoFalsePositives:
    """误报比漏报更伤可信度. These tests are the ones that cost credibility."""

    def test_a_correct_claim_produces_nothing(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            growth=10.0, growth_literal="10.0", references=["DOC-A#p1"],
            sentence="公司2025年实现营业收入110万元，同比增长10.0%。",
        )])
        assert findings == []

    def test_undeclared_period_is_not_a_period_error(self):
        """Measured regression: 「营业成本为70万元」 (no year) was reported as a
        期间错配 against the 2025FY disclosure — an error built on the assumption
        that an omitted period meant a *different* one.
        """
        findings = _run([_claim(
            metric="operating_cost", period=None, value=70.0, value_literal="70",
            unit="万元", references=["DOC-A#p1"],
        )])
        assert findings == []

    def test_a_sentence_without_a_scope_word_is_never_a_scope_error(self):
        facts = _baseline_facts()
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            scope=None, references=["DOC-A#p1"],
        )], facts)
        assert findings == []

    def test_matching_any_disclosed_column_counts_as_correct(self):
        """Two disclosed columns of one figure is not a contradiction."""
        facts = [
            _nf("revenue", 1_234_567, row=1, column=1),
            _nf("revenue", 2_000_000, row=3, column=2),
        ]
        findings = _run([_claim(
            metric="revenue", value=123.46, value_literal="123.46", unit="万元",
            references=["DOC-A#p1"],
        )], facts)
        assert findings == []

    def test_near_miss_in_one_table_does_not_supersede_another_table(self):
        """Supersession is a *table-level* structure, not a period-level one.

        Measured regression: the 合并利润表 本期列 and the 前期差错更正 columns both
        canonicalise to 2025FY, and grouping by (metric, period, scope) alone made
        the untouched statement body look superseded.
        """
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert findings == []

    def test_a_metric_with_no_disclosure_is_unverifiable_not_wrong(self):
        """§8 edge case 3 — 无法核验 is not 核实为错.

        The metric is absent from the fact base *and* the sentence cites nothing, so
        there is no document to point at. The guarantee is the severity: a metric the
        checker simply cannot resolve is never escalated to an error.
        """
        findings = _run([_claim(
            metric="gross_profit", value=10.0, value_literal="10", unit="万元",
            references=[],
        )])
        assert findings, "至少要说明该值无法核验"
        assert all(item.severity == Severity.WARNING.value for item in findings)
        assert {item.check_type for item in findings} == {CheckType.MISSING_CITATION.value}

    def test_a_metric_absent_from_the_cited_document_is_a_citation_defect(self):
        """A different defect, and it must not be reported as a numeric error."""
        findings = _run([_claim(
            metric="gross_profit", value=10.0, value_literal="10", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.WRONG_CITATION.value]

    def test_a_figure_with_no_unit_is_warning_level_not_an_error(self):
        """The comparison would rest on a magnitude we assumed, not on the draft."""
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit=None,
            references=["DOC-A#p1"],
        )])
        assert len(findings) == 1
        assert findings[0].severity == Severity.WARNING.value
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value]

    def test_a_unitless_figure_that_happens_to_read_correctly_in_base_units_passes(self):
        """§9's other edge: when the assumed magnitude *is* right, say nothing."""
        findings = _run([_claim(
            metric="revenue", value=1_100_000.0, value_literal="1,100,000", unit=None,
            references=["DOC-A#p1"],
        )])
        assert findings == []

    def test_clean_drafts_in_the_corpus_produce_no_findings(self):
        """The corpus's two good drafts — the end-to-end zero-false-positive test."""
        score = evaluate_corpus()
        clean = [
            item for item in score.details
            if item["filename"].startswith(("draft_01", "draft_02"))
        ]
        assert len(clean) == 2
        for item in clean:
            assert item["findings"] == 0, item
            assert item["spurious"] == []
        assert score.false_positives == 0
        assert score.false_positive_rate == 0.0


# --------------------------------------------------------------------------- #
# §7.2 — unit discipline
# --------------------------------------------------------------------------- #

class TestUnitDiscipline:
    """A 10^4/10^3/10^8 magnitude slip is a unit_error, never a numeric_error."""

    @pytest.mark.parametrize("declared_unit", ["万元", "千元", "亿元"])
    def test_ladder_slip_is_a_unit_error_not_a_numeric_error(self, declared_unit):
        """The digits are the filing's, the unit word is not.

        ``1,100,000`` written as 万元 reads as 1.1×10^10 against a disclosure of
        1.1×10^6 — exactly one rung of the ladder, which is a unit word that is wrong
        rather than a number that is wrong.
        """
        findings = _run([_claim(
            metric="revenue", value=1_100_000.0, value_literal="1,100,000",
            unit=declared_unit, references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.UNIT_ERROR.value]
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.UNIT_CONVERTED.value]

    def test_a_correct_unit_and_digits_pass(self):
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert findings == []

    def test_a_ten_percent_off_value_is_a_numeric_error(self):
        """A real disagreement must not be excused as a unit slip."""
        findings = _run([_claim(
            metric="revenue", value=121.0, value_literal="121", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.NUMERIC_ERROR.value]

    def test_the_expected_side_is_rendered_in_the_filings_own_unit(self):
        findings = _run([_claim(
            metric="operating_cost", value=700_000.0, value_literal="700,000",
            unit="万元", references=["DOC-A#p1"],
        )])
        # 700,000元 in base unit, shown as the filing declared it.
        assert findings[0].expected.startswith("700,000元")


# --------------------------------------------------------------------------- #
# §8 edge case 2 — rounding tolerance
# --------------------------------------------------------------------------- #

class TestRoundingTolerance:
    """Tolerance is the half-ULP of the *written* digits and nothing more."""

    def test_rounding_to_the_written_precision_passes(self):
        facts = [_nf("revenue", 3_258_000_000)]  # 32.58亿
        # 32.6亿元 has discarded everything below 0.05亿 = 5,000,000元.
        findings = _run([_claim(
            metric="revenue", value=32.6, value_literal="32.6", unit="亿元",
            references=["DOC-A#p1"],
        )], facts)
        assert findings == []

    def test_just_beyond_the_written_precision_fails(self):
        facts = [_nf("revenue", 3_320_000_000)]  # 0.6亿 = 60,000,000 away
        findings = _run([_claim(
            metric="revenue", value=32.6, value_literal="32.6", unit="亿元",
            references=["DOC-A#p1"],
        )], facts)
        assert _types(findings) == [CheckType.NUMERIC_ERROR.value]

    def test_more_written_decimals_means_a_tighter_tolerance(self):
        """The tolerance follows the literal, not a house fudge factor.

        A 2,000,000元 gap is inside 32.6亿's half-ULP (5,000,000) and outside
        32.65亿's (500,000). Trailing zeros are *not* significant digits — ``32.600``
        is the same precision as ``32.6`` — so the test uses a real second decimal.
        """
        facts = [_nf("revenue", 3_262_000_000)]
        loose = _run([_claim(
            metric="revenue", value=32.6, value_literal="32.6", unit="亿元",
            references=["DOC-A#p1"],
        )], facts)
        strict = _run([_claim(
            metric="revenue", value=32.65, value_literal="32.65", unit="亿元",
            references=["DOC-A#p1"],
        )], facts)
        assert loose == []
        assert _types(strict) == [CheckType.NUMERIC_ERROR.value]

    def test_trailing_zeros_do_not_tighten_the_tolerance(self):
        findings = _run([_claim(
            metric="revenue", value=32.6, value_literal="32.600", unit="亿元",
            references=["DOC-A#p1"],
        )], [_nf("revenue", 3_262_000_000)])
        assert findings == []

    def test_percentage_tolerance_follows_written_decimals(self):
        # Recomputed growth is exactly +10.00%; the draft wrote +10.05% and +10%.
        facts = _baseline_facts()
        within = _run([_claim(
            metric="revenue", growth=10.0, growth_literal="10.0",
            references=["DOC-A#p1"],
        )], facts)
        beyond = _run([_claim(
            metric="revenue", growth=10.05, growth_literal="10.05",
            references=["DOC-A#p1"],
        )], facts)
        assert within == []
        assert _types(beyond) == [CheckType.CALCULATION_ERROR.value]


# --------------------------------------------------------------------------- #
# §7.4 — causal claims
# --------------------------------------------------------------------------- #

class TestCausalClaims:
    """A causal sentence needs independent evidence on both sides."""

    def test_one_sided_evidence_is_a_warning(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1"],
            sentence="受原材料价格上涨推动，2025年营业收入下降。", **_CAUSAL_SIDES,
        )])
        assert _types(findings) == [CheckType.UNSUPPORTED_CAUSAL_CLAIM.value]
        assert findings[0].severity == Severity.WARNING.value

    def test_two_independent_sources_clear_the_sentence(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1", "DOC-B#p2"],
            sentence="受原材料价格上涨推动，2025年营业收入下降。", **_CAUSAL_SIDES,
        )])
        assert findings == []

    def test_two_pages_of_one_document_count_as_two_sources(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1", "DOC-A#p3"],
            sentence="受原材料价格上涨推动，2025年营业收入下降。", **_CAUSAL_SIDES,
        )])
        assert findings == []

    def test_the_same_page_twice_is_still_one_source(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1", "DOC-A#p1"],
            sentence="受原材料价格上涨推动，2025年营业收入下降。", **_CAUSAL_SIDES,
        )])
        assert _types(findings) == [CheckType.UNSUPPORTED_CAUSAL_CLAIM.value]

    def test_a_causal_sentence_whose_sides_were_not_separated_is_flagged(self):
        findings = _run([_claim(
            claim_type=ClaimType.CAUSAL.value, references=["DOC-A#p1", "DOC-B#p1"],
            sentence="因为一些原因，收入变化了。",
        )])
        assert _types(findings) == [CheckType.UNSUPPORTED_CAUSAL_CLAIM.value]
        assert "未能分离出因果两侧" in findings[0].detail


# --------------------------------------------------------------------------- #
# §8 — the other edge cases
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_forward_looking_sentences_are_skipped_but_counted(self):
        claim = _claim(
            claim_type=ClaimType.FORWARD_LOOKING.value, metric="revenue",
            value=500.0, value_literal="500", unit="万元", period=None,
            references=["DOC-A#p1"], sentence="预计2026年营业收入500万元。",
        )
        findings = _run([claim])
        assert findings == []
        summary = summarize(findings, claims=[claim])
        assert summary["claims_skipped_forward_looking"] == 1
        assert summary["claims_checked"] == 0

    @pytest.mark.parametrize(
        "phrase",
        ["预计2026年营业收入500万元。", "公司有望实现增长。", "目标价25元。", "未来三年指引维持。"],
    )
    def test_forward_looking_detector(self, phrase):
        assert is_forward_looking(phrase) is True

    def test_a_superseded_match_is_reported_before_value_disagreement(self):
        """A stale number that is also "wrong" is still a version problem."""
        findings = _run([_claim(
            metric="revenue", value=90.0, value_literal="90", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert _types(findings) == [CheckType.REVISION_SUPERSEDED.value]

    def test_an_unresolvable_citation_suppresses_the_value_verdict(self):
        """One defect, one finding: a bad citation must not also produce 无法核验."""
        findings = _run([_claim(
            metric="revenue", value=110.0, value_literal="110", unit="万元",
            references=["DOC-B#p1"],
        )])
        assert _types(findings) == [CheckType.WRONG_CITATION.value]

    def test_summary_counts_what_was_not_checked(self):
        """A summary that only counted findings would read as "everything else is
        fine" even when sentences never made it through extraction."""
        summary = summarize([], claims=[], unextracted=3, no_claim=2)
        assert summary["sentences_unextracted"] == 3
        assert summary["sentences_no_claim"] == 2
        assert summary["clean"] is True  # no *errors* — but the counts are visible

    def test_summary_clean_tracks_errors_only(self):
        warnings_only = summarize([
            __import__("src.cn.checker.models", fromlist=["Finding"]).Finding(
                claim_id="c", check_type=CheckType.MISSING_CITATION.value,
                severity=Severity.WARNING.value, original="x", expected="y",
            )
        ])
        assert warnings_only["warnings"] == 1
        assert warnings_only["clean"] is True


# --------------------------------------------------------------------------- #
# §9 — the failure modes must be visible
# --------------------------------------------------------------------------- #

class TestCoverageIsDeclared:
    """A rule that could not run must say so; silence is never cleanliness."""

    def test_coverage_names_the_rules_that_did_not_run(self):
        coverage = coverage_of(
            extractor=EXTRACTOR_RULES,
            references=[],
            index=FactIndex([_nf("revenue", 1_100_000)]),
            as_of=None,
            signals=[],
        )
        assert coverage["rules"]["stale_citation"]["ran"] is False
        assert "未提供草稿 as_of" in coverage["rules"]["stale_citation"]["reason"]
        assert coverage["rules"]["calculation_error"]["ran"] is False
        assert "事实库只有单一期间" in coverage["rules"]["calculation_error"]["reason"]

    def test_ingest_only_date_does_not_enable_stale_rule(self):
        facts = FactIndex([_nf("revenue", 1_100_000, available_at="2026-09-26")])
        undated = coverage_of(
            extractor=EXTRACTOR_RULES,
            references=[_document("DOC-A", "annual.pdf", publication_date=None)],
            index=facts, as_of="2025-04-01", signals=[],
        )
        assert undated["rules"]["stale_citation"]["ran"] is False
        assert "导入时间" in undated["rules"]["stale_citation"]["reason"]

        dated = coverage_of(
            extractor=EXTRACTOR_RULES,
            references=[_document("DOC-A", "annual.pdf", publication_date="2025-03-15")],
            index=facts, as_of="2025-04-01", signals=[],
        )
        assert dated["rules"]["stale_citation"]["ran"] is True

    def test_scope_coverage_says_absence_is_not_evidence(self):
        coverage = coverage_of(
            extractor=EXTRACTOR_RULES, references=[],
            index=FactIndex([_nf("revenue", 1_100_000)]), as_of=AS_OF, signals=[],
        )
        assert "≠ 不存在口径变化" in coverage["rules"]["scope_error"]["reason"]

    def test_injected_market_data_is_declared_synthetic(self):
        report = _check_corpus_draft("draft_07_planted_valuation.pdf")
        injected = report["coverage"]["injected_facts"]
        assert injected["count"] > 0
        assert all(item["synthetic"] is True for item in injected["documents"])
        assert injected["documents"][0]["document_id"] == MARKET_DOCUMENT["document_id"]

    def test_reference_documents_are_named_in_the_payload(self):
        report = _check_corpus_draft("draft_01_clean_summary.pdf")
        names = [item["filename"] for item in report["coverage"]["reference_documents"]]
        assert names == [REF_ANNUAL_RESTATED]


# --------------------------------------------------------------------------- #
# §5 — the claim-type mapping is applied by code, not chosen by a model
# --------------------------------------------------------------------------- #

class TestClaimTypeMapping:
    def test_the_mapping_table_covers_every_extractor_vocabulary(self):
        assert set(CLAIM_TYPE_MAP) == {member.value for member in ClaimType}

    @pytest.mark.parametrize(
        "draft_type,expected",
        [
            (ClaimType.FACTUAL.value, GraphClaimType.FACT),
            (ClaimType.VALUATION.value, GraphClaimType.INFERENCE),
            (ClaimType.CAUSAL.value, GraphClaimType.INFERENCE),
            (ClaimType.OPINION.value, GraphClaimType.OPINION),
            (ClaimType.FORWARD_LOOKING.value, GraphClaimType.OPINION),
        ],
    )
    def test_mapping(self, draft_type, expected):
        claim = _claim(claim_type=draft_type)
        assert claim.graph_claim_type == expected

    def test_an_unknown_claim_type_falls_back_to_opinion(self):
        """Never to ``fact``: an unrecognised sentence must not be promoted into a
        factual assertion."""
        claim = _claim(claim_type="something_else")
        assert claim.graph_claim_type == GraphClaimType.OPINION

    def test_the_mapping_is_reported_on_every_claim_payload(self):
        report = _check_corpus_draft("draft_03_planted_figures.pdf")
        for claim in report["claims"]:
            assert claim["graph_claim_type"] in {m.value for m in GraphClaimType}
            assert claim["graph_claim_type"] == (
                CLAIM_TYPE_MAP[claim["claim_type"]].value
            )


# --------------------------------------------------------------------------- #
# §10 — validator rules
# --------------------------------------------------------------------------- #

class TestValidator:
    """Each §10 rule gets a case that must fail and a case that must pass."""

    def test_the_shipped_checker_is_model_free(self):
        validation = validate_checker_module()
        assert validation.valid is True
        assert validation.errors == []

    def test_the_source_assertion_catches_a_banned_import(self):
        problems = check_source_is_model_free("import openai\n")
        assert len(problems) == 1
        assert "openai" in problems[0]

    def test_the_source_assertion_catches_a_banned_symbol(self):
        problems = check_source_is_model_free("from x import LlmClient\n")
        assert any("LlmClient" in item for item in problems)

    def test_the_source_assertion_catches_a_deferred_import(self):
        """The case an import-graph check would miss."""
        source = "def rule():\n    import openai\n    return openai\n"
        assert check_source_is_model_free(source)

    def test_the_source_assertion_ignores_comments(self):
        assert check_source_is_model_free("# import openai\nx = 1\n") == []

    def test_the_checker_module_really_has_no_model_import(self):
        source = checker_module_source()
        assert "src.llms" not in source
        assert "ClaimExtractionClient" not in source

    def test_an_unresolvable_evidence_id_fails(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "1,100,000元（2025FY）", "evidence_ids": ["nf_missing"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"nf_other": {"document_id": "DOC-A", "page": 1}},
            checker_source="",
        )
        assert result.valid is False
        assert any("无法解析" in item for item in result.errors)

    def test_evidence_without_a_page_fails(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "1,100,000元（2025FY）", "evidence_ids": ["nf_1"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"nf_1": {"document_id": "DOC-A", "page": None}},
            checker_source="",
        )
        assert result.valid is False
        assert any("缺少文档页码" in item for item in result.errors)

    def test_a_resolvable_evidence_id_passes(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "1,100,000元（2025FY）", "evidence_ids": ["nf_1"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"nf_1": {"document_id": "DOC-A", "page": 1}},
            checker_source="",
        )
        assert result.valid is True

    def test_an_error_without_evidence_fails(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "1,100,000元（2025FY）", "evidence_ids": [],
        }]}
        result = validate_checker_report(report, evidence_lookup={}, checker_source="")
        assert result.valid is False
        assert any("未绑定任何证据" in item for item in result.errors)

    def test_a_warning_without_evidence_is_fine(self):
        """``missing_citation``'s whole content is "nothing was cited"."""
        report = {"findings": [{
            "claim_id": "c1", "check_type": "missing_citation", "original": "110万元",
            "expected": "需给出可核验的披露来源（文档 + 页码）", "evidence_ids": [],
        }]}
        result = validate_checker_report(report, evidence_lookup={}, checker_source="")
        assert result.valid is True

    def test_an_unstated_unit_on_the_expected_side_fails(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "1,100,000", "evidence_ids": ["nf_1"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"nf_1": {"document_id": "DOC-A", "page": 1}},
            checker_source="",
        )
        assert result.valid is False
        assert any("单位口径不一致" in item for item in result.errors)

    def test_an_error_that_cannot_state_the_right_value_fails(self):
        report = {"findings": [{
            "claim_id": "c1", "check_type": "numeric_error", "original": "150万元",
            "expected": "无法核验", "evidence_ids": ["nf_1"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"nf_1": {"document_id": "DOC-A", "page": 1}},
            checker_source="",
        )
        assert result.valid is False
        assert any("未给出期望值" in item for item in result.errors)

    def test_a_percentage_finding_is_not_treated_as_an_amount(self):
        """``scope_error`` covers both "wrong 口径 amount" and "a percentage was
        emitted where CARD-04 blocked the comparison" — only the first is an amount."""
        report = {"findings": [{
            "claim_id": "c1", "check_type": "scope_error", "original": "+11.10%",
            "expected": "（可比性被阻断，不得给出同比）", "evidence_ids": ["fact_1"],
        }]}
        result = validate_checker_report(
            report, evidence_lookup={"fact_1": {"document_id": "DOC-A", "page": 1}},
            checker_source="",
        )
        assert result.valid is True

    def test_a_missing_lookup_is_a_warning_never_a_silent_pass(self):
        result = validate_checker_report({"findings": []}, checker_source="")
        assert result.warnings
        assert any("跳过" in item for item in result.warnings)

    def test_every_corpus_report_passes_its_own_validator(self):
        """The payload the tool hands back must satisfy the gate it is checked by."""
        source = checker_module_source()
        for spec in DRAFT_SPECS:
            report = _check_corpus_draft(spec.filename)
            result = validate_checker_report(
                report, evidence_lookup=report["evidence_index"], checker_source=source,
            )
            assert result.valid is True, (spec.filename, result.errors)


# --------------------------------------------------------------------------- #
# §10 — the raised 口径 finding still resolves its evidence
# --------------------------------------------------------------------------- #

class TestEvidenceIndex:
    def test_the_report_publishes_both_id_spaces(self):
        report = _check_corpus_draft("draft_05_planted_scope.pdf")
        layers = {item["layer"] for item in report["evidence_index"].values()}
        assert layers == {"extracted", "normalized"}
        assert report["evidence_summary"]["normalized"] > 0
        assert report["evidence_summary"]["extracted"] > 0

    def test_signal_evidence_ids_resolve_through_the_index(self):
        """Measured regression: CARD-04's ``block_yoy`` finding republishes the
        signal's CARD-01 ``fact_…`` ids, which a normalized-only index cannot
        resolve — a legitimate finding that looked unsourced."""
        report = _check_corpus_draft("draft_05_planted_scope.pdf")
        scope_findings = [
            item for item in report["findings"] if item["check_type"] == "scope_error"
        ]
        assert scope_findings
        for finding in scope_findings:
            assert finding["evidence_ids"]
            for identifier in finding["evidence_ids"]:
                assert identifier in report["evidence_index"]

    def test_every_finding_evidence_id_is_locatable(self):
        for spec in DRAFT_SPECS:
            report = _check_corpus_draft(spec.filename)
            index = report["evidence_index"]
            for finding in report["findings"]:
                for identifier in finding["evidence_ids"]:
                    entry = index[identifier]
                    assert entry["document_id"]


# --------------------------------------------------------------------------- #
# §6 — the extractors, and the boundary between them
# --------------------------------------------------------------------------- #

class TestExtractors:
    def test_the_rule_extractor_is_deterministic_and_declares_itself(self):
        sentences = [DraftSentence(
            sentence_id="DOC#s0001", document_id="DOC",
            text="公司2025年实现营业收入110万元，同比增长10.0%。（来源：DOC-A#p1）",
        )]
        first = extract_claims_rules(sentences)
        second = extract_claims_rules(sentences)
        assert [c.to_dict() for c in first.claims] == [c.to_dict() for c in second.claims]
        assert first.claims[0].extractor == EXTRACTOR_RULES
        assert first.claims[0].unit == "万元"
        assert first.claims[0].growth == 10.0

    def test_result_before_cause_is_extracted_and_warned_without_sources(self):
        sentence = DraftSentence(
            sentence_id="D#s1", document_id="D",
            text="2024 年利润增长主要是因为**海外订单大幅放量**。",
        )
        claims = extract_claims_rules([sentence]).claims
        assert len(claims) == 1
        assert claims[0].claim_type == ClaimType.CAUSAL.value
        assert claims[0].causal_antecedent == "海外订单大幅放量"
        assert claims[0].causal_consequent == "2024 年利润增长"
        assert _types(check_report(claims, [])) == [CheckType.UNSUPPORTED_CAUSAL_CLAIM.value]

    @pytest.mark.parametrize("filename", ["annual_report.pdf", "DOC-A.pdf"])
    def test_named_pdf_page_in_prose_is_resolved_and_checked(self, filename):
        sentence = DraftSentence(
            sentence_id="D#s1", document_id="D",
            text=f"公司2024年营业收入为110万元，见 {filename} 第 45 页。",
        )
        claim = extract_claims_rules([sentence]).claims[0]
        assert claim.source_reference == f"{filename}#p45"
        findings = check_report(
            [claim], [_nf("revenue", 1_100_000, period_key="2024FY", page=9)],
            documents=[_document("DOC-A", filename)],
        )
        assert _types(findings) == [CheckType.WRONG_PAGE.value]

    def test_unanchored_half_year_is_not_failed_against_full_year(self):
        sentence = DraftSentence(
            sentence_id="D#s1", document_id="D",
            text="公司上半年营业收入为166万元。（来源：DOC-A#p1）",
        )
        claim = extract_claims_rules([sentence]).claims[0]
        assert claim.period is None
        findings = check_report([claim], [_nf("revenue", 362_000_000)])
        assert _types(findings) == [CheckType.MISSING_CITATION.value]
        assert "期间未明确" in findings[0].expected

    def test_the_rule_extractor_reads_the_declared_scope(self):
        sentences = [
            DraftSentence(sentence_id="D#s1", document_id="D",
                          text="公司2025年母公司营业收入为60万元。"),
            DraftSentence(sentence_id="D#s2", document_id="D",
                          text="公司2025年合并营业收入为60万元。"),
            DraftSentence(sentence_id="D#s3", document_id="D",
                          text="公司2025年营业收入为60万元。"),
        ]
        outcome = extract_claims_rules(sentences)
        assert [c.scope for c in outcome.claims] == ["母公司", "合并", None]

    @pytest.mark.parametrize(
        "text",
        [
            "归属于母公司股东的净利润为33万元。",
            "公司2025年归属于母公司股东的净利润为33万元。",
        ],
    )
    def test_a_metric_name_containing_the_scope_word_is_not_a_declared_scope(self, text):
        """「归属于母公司股东的净利润」 is a metric name, not a 口径 declaration —
        reading it as one would build scope errors on a word that is part of the
        indicator's own name."""
        sentences = [DraftSentence(sentence_id="D#s1", document_id="D", text=text)]
        outcome = extract_claims_rules(sentences)
        assert outcome.claims and outcome.claims[0].scope is None

    def test_a_bare_merge_word_in_prose_is_not_a_declared_scope(self):
        sentences = [DraftSentence(
            sentence_id="D#s1", document_id="D",
            text="公司已披露重分类事项，同比口径存在变化。",
        )]
        outcome = extract_claims_rules(sentences)
        assert all(claim.scope is None for claim in outcome.claims)

    def test_the_llm_extractor_without_a_client_invents_nothing(self):
        sentences = [DraftSentence(sentence_id="D#s1", document_id="D", text="营业收入110万元。")]
        outcome = asyncio.run(extract_claims(sentences, None))
        assert outcome.claims == []
        assert len(outcome.unextracted) == 1
        assert outcome.notes

    def test_a_scripted_client_flows_through_the_same_deterministic_core(self):
        payload = json.dumps({"claims": [{
            "sentence_id": "D#s1",
            "sentence": "公司2025年实现营业收入150万元。（来源：DOC-A#p1）",
            "metric_label": "营业收入", "period_label": "2025年",
            "value_literal": "150", "unit": "万元",
            "source_reference": "DOC-A#p1",
            "claim_type": "factual",
        }]}, ensure_ascii=False)
        body = "公司2025年实现营业收入150万元。（来源：DOC-A#p1）"
        outcome = asyncio.run(extract_claims(
            [DraftSentence(sentence_id="D#s1", document_id="D", text=body)],
            _ScriptedClient(payload),
        ))
        assert len(outcome.claims) == 1
        claim = outcome.claims[0]
        assert claim.extractor == EXTRACTOR_LLM
        assert claim.metric == "revenue"
        # 「2025年」 resolved by the draft-layer policy, recorded as a reason.
        assert claim.period == "2025FY"
        assert "年度" in (claim.reason or "")
        assert claim.unit == "万元"
        assert claim.references == ("DOC-A#p1",)
        # The same rules produce the same verdict whichever extractor ran.
        findings = _run([claim])
        assert _types(findings) == [CheckType.NUMERIC_ERROR.value]

    def test_the_rule_extractor_resolves_the_bare_year_the_same_way(self):
        """Both extractors implement one period policy; two would drift."""
        outcome = extract_claims_rules([
            DraftSentence(sentence_id="D#s1", document_id="D",
                          text="公司2025年实现营业收入150万元。"),
        ])
        assert outcome.claims[0].period == "2025FY"

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("2025年", "2025FY"), ("2025FY", "2025FY"), ("2024年度", "2024FY"),
            ("2025上半年", "2025H1"), ("2025年第一季度", "2025Q1"),
            ("2025前三季度", "20259M"), ("2025-09-30", "20259M"),
        ],
    )
    def test_the_period_policy(self, label, expected):
        from src.cn.checker.extractor import resolve_period_label

        assert resolve_period_label(label)[0] == expected

    @pytest.mark.parametrize("label", ["", "   ", "乱七八糟", "二零二五年"])
    def test_an_unresolvable_period_is_left_empty_not_guessed(self, label):
        from src.cn.checker.extractor import resolve_period_label

        assert resolve_period_label(label)[0] is None

    def test_an_extraction_without_its_verbatim_sentence_is_discarded(self):
        payload = json.dumps({"claims": [{
            "sentence_id": "D#s1", "sentence": "完全不同的一句话。",
            "metric_label": "营业收入", "value_literal": "150", "unit": "万元",
        }]}, ensure_ascii=False)
        outcome = asyncio.run(extract_claims(
            [DraftSentence(sentence_id="D#s1", document_id="D", text="公司营业收入150万元。")],
            _ScriptedClient(payload),
        ))
        assert outcome.claims == []
        assert len(outcome.unextracted) == 1

    def test_a_right_id_with_a_wrong_quote_is_discarded(self):
        """The id is a hint, not proof.

        Measured defect: a result naming the correct ``sentence_id`` but quoting a
        *different* sentence was accepted, so the claim went out carrying a quote the
        draft never contained — and ``DraftClaim.sentence`` is exactly what the report
        shows a reviewer as 原句.
        """
        payload = json.dumps({"claims": [{
            "sentence_id": "D#s1", "sentence": "公司2025年营业收入为999万元。",
            "metric_label": "营业收入", "period_label": "2025年",
            "value_literal": "999", "unit": "万元",
        }]}, ensure_ascii=False)
        outcome = asyncio.run(extract_claims(
            [DraftSentence(sentence_id="D#s1", document_id="D",
                           text="公司2025年营业收入为150万元。")],
            _ScriptedClient(payload),
        ))
        assert outcome.claims == []
        assert len(outcome.unextracted) == 1

    def test_a_whitespace_only_difference_is_tolerated(self):
        payload = json.dumps({"claims": [{
            "sentence_id": "D#s1", "sentence": "公司 2025 年营业收入为 150 万元。",
            "metric_label": "营业收入", "value_literal": "150", "unit": "万元",
        }]}, ensure_ascii=False)
        outcome = asyncio.run(extract_claims(
            [DraftSentence(sentence_id="D#s1", document_id="D",
                           text="公司2025年营业收入为150万元。")],
            _ScriptedClient(payload),
        ))
        assert len(outcome.claims) == 1

    def test_the_llm_cannot_smuggle_a_scope_in(self):
        payload = json.dumps({"claims": [{
            "sentence_id": "D#s1", "sentence": "公司2025年营业收入为60万元。",
            "metric_label": "营业收入", "period_label": "2025年",
            "value_literal": "60", "unit": "万元", "scope": "神仙口径",
        }]}, ensure_ascii=False)
        outcome = asyncio.run(extract_claims(
            [DraftSentence(sentence_id="D#s1", document_id="D",
                           text="公司2025年营业收入为60万元。")],
            _ScriptedClient(payload),
        ))
        assert outcome.claims[0].scope is None
        assert "不是「合并」或「母公司」" in (outcome.claims[0].reason or "")


class _ScriptedClient:
    """An offline stand-in for a model client: one fixed reply, no network."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: List[List[Dict[str, str]]] = []

    async def complete(self, messages, temperature: float = 0.0) -> str:
        self.calls.append(messages)
        return self.reply


# --------------------------------------------------------------------------- #
# §5 / §6 — sentence splitting
# --------------------------------------------------------------------------- #

class TestSentenceSplitting:
    def test_terminators_split_and_clauses_do_not(self):
        text = "公司2025年实现营业收入110万元，同比增长10.0%；毛利率提升。风险提示：竞争加剧。"
        assert split_sentences(text) == [
            "公司2025年实现营业收入110万元，同比增长10.0%；毛利率提升。",
            "风险提示：竞争加剧。",
        ]

    def test_a_wrapped_sentence_is_rejoined(self):
        assert split_sentences("公司2025年实现营业收入\n110万元，同比增长10.0%。") == [
            "公司2025年实现营业收入110万元，同比增长10.0%。",
        ]

    def test_a_citation_on_its_own_line_is_pulled_back(self):
        """Measured regression: a PDF wrap put 「（来源：…）」 on its own visual line,
        and every citation landed one sentence late — five claims mis-cited and a
        clean draft reported as uncited."""
        sentences = split_sentences(
            "公司2025年实现营业收入110万元。\n（来源：annual_04_restated_comparative.pdf#p1）\n"
        )
        assert sentences == [
            "公司2025年实现营业收入110万元。（来源：annual_04_restated_comparative.pdf#p1）",
        ]

    def test_table_cells_are_never_turned_into_sentences(self):
        facts = [
            ExtractedFact(
                document_id="D", fact_id="f1", raw_text="营业收入", page=1,
                row=1, column=0, raw_value=None, kind=FACT_KIND_TABLE_CELL,
            ),
            ExtractedFact(
                document_id="D", fact_id="f2", raw_text="公司营收110万元。", page=1,
                row=None, column=None, raw_value=None, kind=FACT_KIND_TEXT,
            ),
        ]
        sentences = sentences_from_facts(facts, document_id="D")
        assert [item.text for item in sentences] == ["公司营收110万元。"]


# --------------------------------------------------------------------------- #
# §7.3 — confidence is a constant table
# --------------------------------------------------------------------------- #

class TestConfidence:
    def test_confidence_comes_from_the_declared_table(self):
        assert CONFIDENCE_BY_MATCH[MatchKind.EXACT.value] == 1.0
        assert CONFIDENCE_BY_MATCH[MatchKind.PERIOD_INFERRED.value] == 0.8
        assert CONFIDENCE_BY_MATCH[MatchKind.UNIT_CONVERTED.value] == 0.9
        assert CONFIDENCE_BY_MATCH[MatchKind.UNVERIFIABLE.value] == 0.0

    def test_a_finding_always_carries_a_table_confidence(self):
        findings = _run([_claim(
            metric="revenue", value=150.0, value_literal="150", unit="万元",
            references=["DOC-A#p1"],
        )])
        assert findings[0].confidence == CONFIDENCE_BY_MATCH[MatchKind.ABSENT.value]

    def test_the_report_carries_no_model_self_assessment(self):
        report = _check_corpus_draft("draft_03_planted_figures.pdf")
        for finding in report["findings"]:
            assert 0.0 <= finding["confidence"] <= 1.0
        assert "模型" not in report["engine"] or "非模型输出" in report["engine"]


# --------------------------------------------------------------------------- #
# §12 — integration
# --------------------------------------------------------------------------- #

class TestEndToEnd:
    """CARD-01 parse → CARD-02/04 reference material → extract → check → jump back."""

    def test_a_finding_resolves_back_to_a_source_page(self):
        report = _check_corpus_draft("draft_03_planted_figures.pdf")
        assert report["findings"]
        for finding in report["findings"]:
            for identifier in finding["evidence_ids"]:
                entry = report["evidence_index"][identifier]
                assert entry["document_id"]
                assert entry["page"] >= 1
                assert entry["metric"]

    def test_the_finding_carries_the_draft_page_it_came_from(self):
        report = _check_corpus_draft("draft_03_planted_figures.pdf")
        pages = {item["claim_page"] for item in report["findings"]}
        assert 2 in pages, "第二页的植入错误必须回报草稿页码"

    def test_every_claim_quotes_a_verbatim_sentence(self):
        for spec in DRAFT_SPECS:
            report = _check_corpus_draft(spec.filename)
            for claim in report["claims"]:
                assert claim["sentence"].strip()
                assert claim["sentence"] in "".join(
                    item["text"] for item in report["sentences"]
                ) or claim["sentence"] in "\n".join(
                    item["text"] for item in report["sentences"]
                ) or _is_joined(spec, claim["sentence"])

    def test_the_pipeline_raises_for_an_unregistered_draft(self):
        registry = _registry()
        try:
            with pytest.raises(DraftNotFound):
                check_draft("DOC-nope", registry=registry)
        finally:
            registry.close()

    def test_reference_selection_is_explicit_and_reported(self):
        registry = _registry()
        try:
            facts = registry.get_facts(_ingest_draft(registry, "draft_01_clean_summary.pdf"))
            draft_id = facts[0].document_id if facts else ""
            chosen = selected_reference_documents(registry, draft_id=draft_id)
            names = sorted(item.filename for item in chosen)
            assert REF_ANNUAL_RESTATED in names
            assert "draft_01_clean_summary.pdf" not in names
        finally:
            registry.close()

    def test_the_async_path_does_not_look_clean_when_extraction_yields_nothing(self):
        """§9: a model that returned nothing must not read as a clean draft."""
        registry = _registry()
        try:
            document_id = _ingest_draft(registry, "draft_03_planted_figures.pdf")
            offline = check_draft(document_id, registry=registry, as_of=AS_OF)
            online = asyncio.run(acheck_draft(
                document_id, registry=registry, as_of=AS_OF,
                llm_client=_ScriptedClient("{}"),
            ))
            assert offline["summary"]["errors"] == 5
            # The model produced no claims, so there is nothing to report — but the
            # payload says so instead of implying the draft is fine.
            assert online["summary"]["errors"] == 0
            assert online["summary"]["sentences_unextracted"] > 0
            assert online["summary"]["claims_checked"] == 0
            assert online["unextracted_sentences"]
        finally:
            registry.close()

    def test_the_async_path_agrees_with_the_offline_path_on_the_same_claims(self):
        """The extraction is the only awaitable step; the core is shared."""
        registry = _registry()
        try:
            body = "公司2025年实现营业收入150万元。"
            document_id = _ingest_draft(registry, "draft_03_planted_figures.pdf")
            scripted = _ScriptedClient(json.dumps({"claims": [{
                "sentence_id": "", "sentence": body,
                "metric_label": "营业收入", "period_label": "2025年",
                "value_literal": "150", "unit": "万元", "claim_type": "factual",
            }]}, ensure_ascii=False))
            report = asyncio.run(acheck_draft(
                document_id, registry=registry, as_of=AS_OF, llm_client=scripted,
            ))
            assert report["claims"]
            assert report["claims"][0]["metric"] == "revenue"
            assert report["claims"][0]["period"] == "2025FY"
            assert report["claims"][0]["graph_claim_type"] == "fact"
        finally:
            registry.close()


# --------------------------------------------------------------------------- #
# §13 — benchmark
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def score() -> Score:
    """The corpus scored once per module — it re-parses seven PDFs."""
    return evaluate_corpus()


class TestBenchmark:
    """Scored against hand-authored gold, never against the checker's own output."""

    def test_the_corpus_is_the_committed_set(self, score):
        assert score.drafts == len(DRAFT_SPECS) == 7

    def test_perfect_recall_on_the_planted_set(self, score):
        assert score.recall == 1.0
        assert score.false_negatives == 0

    def test_zero_false_positives(self, score):
        assert score.precision == 1.0
        assert score.false_positives == 0
        assert score.false_positive_rate == 0.0

    def test_f1(self, score):
        assert score.f1 == 1.0

    def test_true_negatives_come_from_the_clean_and_unplanted_claims(self, score):
        assert score.true_negatives >= 8

    def test_every_expected_finding_was_attributed_to_its_own_claim(self, score):
        for item in score.details:
            assert item["missed"] == [], item
            assert item["spurious"] == [], item

    def test_the_report_says_what_it_measured(self, score):
        payload = score.to_dict()
        assert payload["synthetic"] is True
        assert payload["real_model"] is False
        assert payload["extractor"] == "rules"
        assert "不是真实研报现场准确率" in format_markdown(score)
        markdown = format_markdown(score)
        assert "Precision" in markdown and "False Positive Rate" in markdown

    def test_the_score_is_reproducible(self, score):
        again = evaluate_corpus()
        assert again.to_dict()["precision"] == score.to_dict()["precision"]
        assert again.to_dict()["recall"] == score.to_dict()["recall"]
        assert again.to_dict()["true_positives"] == score.to_dict()["true_positives"]


# --------------------------------------------------------------------------- #
# §6 — the Agent tool
# --------------------------------------------------------------------------- #

class TestAgentTool:
    def test_the_tool_is_registered(self):
        from src.agents.tools.cn_tools import build_cn_snapshot_tools

        names = [tool.name for tool in build_cn_snapshot_tools()]
        assert "check_cn_report_draft" in names

    def test_missing_argument_is_refused_with_a_code(self):
        body = _run_tool(document_id="")
        assert body["status"] == "error"
        assert body["error_code"] == "checker_missing_argument"

    def test_unknown_document_is_refused_with_a_code(self):
        body = _run_tool(document_id="DOC-nope")
        assert body["status"] == "error"
        assert body["error_code"] == "document_not_found"

    def test_a_planted_draft_reports_its_findings_and_validates(self):
        body = _run_tool(as_of=AS_OF, draft="draft_03_planted_figures.pdf")
        assert body["status"] == "ok"
        assert body["findings_count"] == 5
        assert body["summary"]["errors"] == 5
        assert body["validation"]["valid"] is True
        assert {item["check_type"] for item in body["findings"]} == {
            "numeric_error", "unit_error", "period_error",
            "revision_superseded", "calculation_error",
        }

    def test_the_editorial_view_groups_errors_before_warnings(self):
        body = _run_tool(as_of=AS_OF, draft="draft_04_planted_citations.pdf")
        keys = [group["key"] for group in body["view"]["groups"]]
        assert keys == ["errors", "warnings"]
        errors = body["view"]["groups"][0]
        warnings = body["view"]["groups"][1]
        # 引用不存在 / 引用对象错 / 页码错 are errors; 缺引用 and 无证据因果 are warnings.
        assert errors["count"] == 3
        assert warnings["count"] == 2
        assert errors["items"][0]["check_label"]
        assert {item["check_type"] for item in warnings["items"]} == {
            "missing_citation", "unsupported_causal_claim",
        }

    def test_a_clean_draft_reports_no_findings(self):
        body = _run_tool(as_of=AS_OF, draft="draft_01_clean_summary.pdf")
        assert body["findings_count"] == 0
        assert body["summary"]["clean"] is True
        assert body["status"] == "ok"

    def test_omitting_as_of_says_the_stale_rule_did_not_run(self):
        body = _run_tool(as_of="", draft="draft_01_clean_summary.pdf")
        assert body["coverage"]["rules"]["stale_citation"]["ran"] is False
        assert "as_of" in body["note"]

    def test_the_tool_payload_has_the_contracted_shape(self):
        body = _run_tool(as_of=AS_OF, draft="draft_05_planted_scope.pdf")
        assert set(body) >= {"status", "findings", "summary"}
        assert set(body["summary"]) >= {"total", "errors", "warnings", "clean", "by_type"}
        for finding in body["findings"]:
            assert set(finding) >= {
                "claim_id", "check_type", "severity", "original", "expected",
                "evidence_ids", "suggestion", "confidence",
            }


# --------------------------------------------------------------------------- #
# §9.3 — status vocabulary
# --------------------------------------------------------------------------- #

class TestStatusVocabulary:
    def test_a_draft_with_no_extractable_sentence_is_blocked_not_clean(self):
        registry = DocumentRegistry(root=Path(tempfile.mkdtemp()) / "r.db")
        try:
            data = _blank_draft_pdf()
            result = registry.ingest(data, "report_draft", filename="blank.pdf", source="admin_import")
            body = json.loads(asyncio.run(
                __import__(
                    "src.agents.tools.cn_tools", fromlist=["CheckCnReportDraftTool"],
                ).CheckCnReportDraftTool(registry).execute(
                    document_id=result.document.document_id,
                )
            ))
            assert body["status"] == "blocked"
            assert body["error_code"] == "checker_no_claims"
            # The point of the word: no finding is not a clean bill of health.
            assert "不等于" in body["error"]
        finally:
            registry.close()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _is_joined(spec, sentence: str) -> bool:
    """Whether *sentence* is a run of consecutive draft lines (heading + body)."""
    for line in spec.lines:
        if sentence.endswith(line) or line in sentence:
            return True
    return sentence in spec.title or spec.title.split()[0] in sentence


def _registry() -> DocumentRegistry:
    """A fresh registry with the corpus's reference filings ingested."""
    registry = DocumentRegistry(root=Path(tempfile.mkdtemp(prefix="cn_checker_test_")) / "r.db")
    for name in (REF_ANNUAL_PLAIN, REF_ANNUAL_RESTATED, REF_ANNUAL_RECLASS, REF_CORRECTION):
        path = FILINGS / name
        if path.exists():
            declared = "annual" if name.startswith("annual") else "announcement"
            registry.ingest(path.read_bytes(), declared, filename=name, source="admin_import")
    return registry


def _ingest_draft(registry: DocumentRegistry, filename: str) -> str:
    result = registry.ingest(
        (DRAFTS / filename).read_bytes(), "report_draft", filename=filename,
        source="admin_import",
    )
    return result.document.document_id


def _check_corpus_draft(filename: str) -> Dict[str, Any]:
    """Run one committed draft through the full pipeline, with its own references."""
    spec = next(item for item in DRAFT_SPECS if item.filename == filename)
    registry = _registry()
    try:
        document_id = _ingest_draft(registry, filename)
        reference_ids = [
            document.document_id for document in registry.list()
            if document.filename in spec.references
        ]
        extra_facts = market_normalized_facts() if spec.uses_market_document else ()
        extra_documents = market_documents() if spec.uses_market_document else ()
        return check_draft(
            document_id, registry=registry, reference_document_ids=reference_ids,
            as_of=spec.as_of, extra_facts=extra_facts, extra_documents=extra_documents,
        )
    finally:
        registry.close()


def _run_tool(*, document_id: str = "", as_of: str = "", draft: str = "") -> Dict[str, Any]:
    """Run ``CheckCnReportDraftTool`` against a fresh registry."""
    from src.agents.tools.cn_tools import CheckCnReportDraftTool

    registry = _registry()
    try:
        if draft:
            document_id = _ingest_draft(registry, draft)
        raw = asyncio.run(
            CheckCnReportDraftTool(registry).execute(document_id=document_id, as_of=as_of)
        )
        return json.loads(raw)
    finally:
        registry.close()


def _blank_draft_pdf() -> bytes:
    """A draft PDF whose only text carries no figures at all."""
    from src.cn.documents.fixtures_builder import build_report_draft_lines

    return build_report_draft_lines(
        title="无数字的草稿",
        lines=["本页仅说明编制依据，未列示任何财务数据。"],
    )
