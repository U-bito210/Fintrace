"""CARD-13 Source Conflict Resolver — offline tests.

The card is about *not* picking a number by accident, so the tests are weighted
towards the negative cases: a tie that must stay unresolved, a 口径 difference
that must not be called a conflict, a correction that must be recorded rather
than silently applied, and the mechanical assertion that no candidate can vanish
without a record.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.cn.conflict import (
    ABS_TOLERANCE,
    ACME_CONFLICT,
    ACME_MATCH,
    ACME_NOT_COMPARABLE,
    ACME_ROUNDING_MATCH,
    CHANGE_DATA_CONFLICT,
    CHANGE_KINDS,
    CHANGE_NEW_INFORMATION,
    CHANGE_VERSION_SUPERSESSION,
    RULE_IDS,
    RULE_INTRA_DOCUMENT_PRECEDENCE,
    RULE_NEW_PERIOD,
    RULE_NOT_COMPARABLE,
    RULE_REGULATORY_CORRECTION,
    RULE_SOURCE_PRECEDENCE,
    RULE_TIE_UNRESOLVED,
    RULE_UNKNOWN_SOURCE,
    RULE_VALUES_AGREE,
    RULE_VERSION_SUPERSESSION,
    SOURCE_COMPANY_ANNOUNCEMENT,
    SOURCE_PRECEDENCE,
    SOURCE_REGULATORY_CORRECTION,
    SOURCE_REGULATORY_FILING,
    SOURCE_THIRD_PARTY,
    SOURCE_UNKNOWN,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    ConflictCandidate,
    ConflictError,
    ConflictRecord,
    arbitrate_conflict,
    assert_no_silent_override,
    candidate_from_fact,
    classify_change,
    classify_cross_source,
    compare_pair,
    conflict_choices_for_metric,
    correction_candidates,
    correction_pairs,
    default_source_class,
    resolve_conflicts,
    selected_by_metric,
    summarize,
    unresolved_metrics,
    unresolved_report,
    values_agree,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.normalization import normalize_financials
from src.cn.normalization.models import Version
from src.validation import blocked_metric_keys, validate_conflicts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "documents"
STAMP = "2026-09-19T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _candidate(
    candidate_id: str,
    *,
    value: float = 100.0,
    source_id: str = "",
    source_class: str = SOURCE_REGULATORY_FILING,
    metric: str = "revenue",
    period_key: str = "2024FY",
    scope: str | None = "合并",
    currency: str = "CNY",
    version: Version = Version.AS_REPORTED,
    available_at: str | None = None,
    is_explicit_correction: bool = False,
    ref: str = "",
) -> ConflictCandidate:
    return ConflictCandidate(
        candidate_id=candidate_id,
        source_id=source_id or f"src-{candidate_id}",
        source_class=source_class,
        metric=metric,
        period_key=period_key,
        scope=scope,
        currency=currency,
        value=value,
        version=version,
        available_at=available_at,
        is_explicit_correction=is_explicit_correction,
        ref=ref or candidate_id,
    )


def _resolve(*candidates: ConflictCandidate, tolerance: float | None = None):
    return resolve_conflicts(
        [],
        extra_candidates=list(candidates),
        company_of=lambda _source: "示例公司",
        occurred_at=STAMP,
        tolerance=tolerance,
    )


@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def _ingest(registry: DocumentRegistry, filename: str, declared: str):
    parsed = registry.ingest(FIXTURES / filename, declared_type=declared, source="admin_import")
    return parsed.document, normalize_financials(parsed.facts, parsed.document)


# --------------------------------------------------------------------------- #
# §7 four-level precedence — positive and negative
# --------------------------------------------------------------------------- #
def test_correction_outranks_the_filing_it_corrects():
    filing = _candidate("a", value=900_000.0, source_class=SOURCE_REGULATORY_FILING)
    correction = _candidate(
        "b",
        value=1_000_000.0,
        source_class=SOURCE_REGULATORY_CORRECTION,
        version=Version.CORRECTED,
        available_at="2026-06-12",
        is_explicit_correction=True,
    )
    choices, records = _resolve(filing, correction)
    choice = conflict_choices_for_metric(choices, "revenue", "2024FY")
    assert choice is not None
    assert choice.value == 1_000_000.0
    assert choice.change_kind == CHANGE_VERSION_SUPERSESSION
    assert any(record.reason == RULE_REGULATORY_CORRECTION for record in records)


def test_regulatory_filing_outranks_company_announcement():
    filing = _candidate("a", value=900_000.0, source_class=SOURCE_REGULATORY_FILING)
    announcement = _candidate("b", value=800_000.0, source_class=SOURCE_COMPANY_ANNOUNCEMENT)
    choices, records = _resolve(announcement, filing)
    choice = conflict_choices_for_metric(choices, "revenue")
    assert choice.value == 900_000.0
    assert choice.change_kind == CHANGE_DATA_CONFLICT
    assert all(record.reason == RULE_SOURCE_PRECEDENCE for record in records)


def test_regulatory_filing_outranks_third_party():
    filing = _candidate("a", value=35.2e8, source_class=SOURCE_REGULATORY_FILING)
    vendor = _candidate("b", value=36.0e8, source_class=SOURCE_THIRD_PARTY)
    choices, _records = _resolve(vendor, filing)
    assert conflict_choices_for_metric(choices, "revenue").value == 35.2e8


def test_company_announcement_outranks_third_party():
    announcement = _candidate("a", value=800_000.0, source_class=SOURCE_COMPANY_ANNOUNCEMENT)
    vendor = _candidate("b", value=700_000.0, source_class=SOURCE_THIRD_PARTY)
    choices, _records = _resolve(vendor, announcement)
    assert conflict_choices_for_metric(choices, "revenue").value == 800_000.0


def test_precedence_table_is_the_cards_four_levels():
    assert SOURCE_PRECEDENCE == {
        SOURCE_REGULATORY_CORRECTION: 3,
        SOURCE_REGULATORY_FILING: 2,
        SOURCE_COMPANY_ANNOUNCEMENT: 1,
        SOURCE_THIRD_PARTY: 0,
    }
    # An unknown source has no rank at all, so it can never win an arbitration.
    assert SOURCE_UNKNOWN not in SOURCE_PRECEDENCE


def test_unknown_source_class_cannot_win():
    known = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    unknown = _candidate("b", value=999.0, source_class=SOURCE_UNKNOWN)
    winner, rule, note = arbitrate_conflict(known, unknown)
    assert winner is known
    assert rule == RULE_SOURCE_PRECEDENCE
    assert "未知" in note


def test_two_unknown_sources_stay_unresolved():
    left = _candidate("a", value=100.0, source_class=SOURCE_UNKNOWN)
    right = _candidate("b", value=200.0, source_class=SOURCE_UNKNOWN)
    winner, rule, _note = arbitrate_conflict(left, right)
    assert winner is None
    assert rule == RULE_UNKNOWN_SOURCE


# --------------------------------------------------------------------------- #
# §8 edge case 2 — same class, different values, no version relation → blocked
# --------------------------------------------------------------------------- #
def test_same_class_conflict_is_unresolved_and_blocks_the_metric():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    choices, records = _resolve(left, right)
    choice = conflict_choices_for_metric(choices, "revenue")
    assert choice.status == STATUS_UNRESOLVED
    assert choice.value is None
    assert any(record.reason == RULE_TIE_UNRESOLVED for record in records)
    rows = unresolved_report(records)
    assert rows and rows[0]["required_action"]
    assert unresolved_metrics(records) == {"revenue@2024FY/合并"}


def test_unresolved_never_carries_a_selected_source():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)
    assert all(record.selected_source is None for record in records)
    assert all(record.status == STATUS_UNRESOLVED for record in records)


def test_a_lower_ranked_third_source_is_still_recorded():
    """Three sources, two of them agreeing — the odd one out must not vanish."""
    filing = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    vendor_agree = _candidate("b", value=100.0, source_class=SOURCE_THIRD_PARTY)
    vendor_odd = _candidate("c", value=300.0, source_class=SOURCE_THIRD_PARTY)
    choices, records = _resolve(vendor_odd, vendor_agree, filing)
    choice = conflict_choices_for_metric(choices, "revenue")
    assert choice.value == 100.0
    assert {record.candidate_a for record in records} | {
        record.candidate_b for record in records
    } >= {"b", "c"}
    assert_no_silent_override(choices, records)


# --------------------------------------------------------------------------- #
# §7b three states — one positive case each
# --------------------------------------------------------------------------- #
def test_data_conflict_same_period_same_version_different_values():
    left = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    verdict, _reason = classify_cross_source(left, right)
    change_kind, _change_reason = classify_change(left, right)
    assert verdict == ACME_CONFLICT
    assert change_kind == CHANGE_DATA_CONFLICT


def test_version_supersession_is_not_a_data_conflict():
    older = _candidate("a", value=900_000.0, version=Version.AS_REPORTED)
    newer = _candidate(
        "b",
        value=1_000_000.0,
        version=Version.CORRECTED,
        is_explicit_correction=True,
        available_at="2026-06-12",
    )
    verdict, _reason = classify_cross_source(older, newer)
    change_kind, _change_reason = classify_change(older, newer)
    # It *is* a value disagreement, but the supersession reading must win because
    # only it carries the correct downstream rule.
    assert verdict == ACME_CONFLICT
    assert change_kind == CHANGE_VERSION_SUPERSESSION


def test_restated_version_alone_is_enough_for_supersession():
    older = _candidate("a", value=1_100_000.0, version=Version.AS_REPORTED)
    newer = _candidate("b", value=1_000_000.0, version=Version.RESTATED)
    change_kind, _reason = classify_change(older, newer)
    assert change_kind == CHANGE_VERSION_SUPERSESSION


def test_new_information_when_a_new_period_appears():
    prior = _candidate("a", value=100.0, period_key="2024FY")
    fresh = _candidate("b", value=140.0, period_key="2025Q1", source_id="other")
    change_kind, reason = classify_change(prior, fresh)
    assert change_kind == CHANGE_NEW_INFORMATION
    assert RULE_NEW_PERIOD in reason


def test_new_information_does_not_overwrite_history():
    prior = _candidate("a", value=100.0, period_key="2024FY")
    fresh = _candidate("b", value=140.0, period_key="2025Q1", source_id="other")
    choices, records = _resolve(prior, fresh)
    by_period = {choice.period_key: choice for choice in choices}
    assert by_period["2024FY"].value == 100.0
    assert by_period["2025Q1"].value == 140.0
    record = next(item for item in records if item.change_kind == CHANGE_NEW_INFORMATION)
    assert record.period_key == "2025Q1"
    assert record.superseded_source is None
    assert "不覆盖历史期间" in record.note


def test_all_three_change_kinds_are_distinct_constants():
    assert set(CHANGE_KINDS) == {
        CHANGE_DATA_CONFLICT,
        CHANGE_VERSION_SUPERSESSION,
        CHANGE_NEW_INFORMATION,
    }


# --------------------------------------------------------------------------- #
# §8 edge case 1 — chained corrections resolve to the latest link
# --------------------------------------------------------------------------- #
def test_chained_corrections_resolve_to_the_latest_link():
    original = _candidate("a", value=900_000.0, version=Version.AS_REPORTED)
    first = _candidate(
        "b",
        value=950_000.0,
        source_id="src-b",
        version=Version.CORRECTED,
        available_at="2026-05-01",
        is_explicit_correction=True,
    )
    second = _candidate(
        "c",
        value=1_000_000.0,
        source_id="src-c",
        version=Version.CORRECTED,
        available_at="2026-06-12",
        is_explicit_correction=True,
    )
    choices, records = _resolve(original, first, second)
    choice = conflict_choices_for_metric(choices, "revenue")
    assert choice.value == 1_000_000.0
    superseded = {record.superseded_source for record in records}
    assert superseded == {"src-a", "src-b"}, "every earlier link keeps its own record"


def test_each_link_of_a_chain_produces_one_record():
    original = _candidate("a", value=900_000.0, version=Version.AS_REPORTED)
    first = _candidate(
        "b", value=950_000.0, source_id="src-b", version=Version.CORRECTED,
        available_at="2026-05-01", is_explicit_correction=True,
    )
    second = _candidate(
        "c", value=1_000_000.0, source_id="src-c", version=Version.CORRECTED,
        available_at="2026-06-12", is_explicit_correction=True,
    )
    _choices, records = _resolve(original, first, second)
    assert len(records) == 2
    assert {record.change_kind for record in records} == {CHANGE_VERSION_SUPERSESSION}


# --------------------------------------------------------------------------- #
# §8 edge case 3 — rounding tolerance
# --------------------------------------------------------------------------- #
def test_values_agree_within_tolerance_is_not_a_conflict():
    assert values_agree(1_000_000.0, 1_000_000.0 + ABS_TOLERANCE / 2)
    assert not values_agree(1_000_000.0, 1_000_001.0)


def test_rounding_match_verdict_does_not_produce_a_conflict():
    left = _candidate("a", value=1_000_000.0)
    right = _candidate("b", value=1_000_000.0 + ABS_TOLERANCE / 2, source_id="src-b")
    verdict, _reason = classify_cross_source(left, right)
    assert verdict == ACME_ROUNDING_MATCH
    choices, records = _resolve(left, right)
    assert conflict_choices_for_metric(choices, "revenue").status == STATUS_RESOLVED
    assert all(record.reason == RULE_VALUES_AGREE for record in records)


def test_a_wide_display_tolerance_can_be_declared_by_the_caller():
    left = _candidate("a", value=1_000_000.0)
    right = _candidate("b", value=1_000_400.0, source_id="src-b")
    assert classify_cross_source(left, right)[0] == ACME_CONFLICT
    assert classify_cross_source(left, right, tolerance=1_000.0)[0] == ACME_ROUNDING_MATCH


# --------------------------------------------------------------------------- #
# §7 — a scope difference is not a conflict
# --------------------------------------------------------------------------- #
def test_different_scope_is_not_a_conflict():
    consolidated = _candidate("a", value=1_000_000.0, scope="合并")
    parent = _candidate("b", value=600_000.0, scope="母公司", source_id="src-b")
    verdict, reason = classify_cross_source(consolidated, parent)
    assert verdict == ACME_NOT_COMPARABLE
    assert RULE_NOT_COMPARABLE in reason
    # And they never meet: separate series, each resolved on its own.
    choices, records = _resolve(consolidated, parent)
    assert len(choices) == 2
    assert records == []
    assert {choice.value for choice in choices} == {1_000_000.0, 600_000.0}


def test_different_currency_is_not_a_conflict():
    cny = _candidate("a", value=1_000_000.0, currency="CNY")
    usd = _candidate("b", value=140_000.0, currency="USD", source_id="src-b")
    verdict, _reason = classify_cross_source(cny, usd)
    assert verdict == ACME_NOT_COMPARABLE
    choices, _records = _resolve(cny, usd)
    assert len(choices) == 2


def test_different_metric_is_never_compared():
    revenue = _candidate("a", value=100.0, metric="revenue")
    cost = _candidate("b", value=100.0, metric="operating_cost")
    verdict, _reason = classify_cross_source(revenue, cost)
    assert verdict == ACME_NOT_COMPARABLE


# --------------------------------------------------------------------------- #
# ACME four-class integration (§7b)
# --------------------------------------------------------------------------- #
def test_acme_match_requires_no_arbitration():
    left = _candidate("a", value=35.2e8)
    right = _candidate("b", value=35.2e8, source_id="vendor")
    report = compare_pair(left, right)
    assert report["verdict"] == ACME_MATCH
    assert report["required_action"] == "无需裁决：两个来源一致"


def test_acme_conflict_hands_the_decision_to_this_card():
    filing = _candidate("a", value=35.2e8, source_class=SOURCE_REGULATORY_FILING)
    vendor = _candidate("b", value=36.0e8, source_class=SOURCE_THIRD_PARTY)
    report = compare_pair(filing, vendor)
    assert report["verdict"] == ACME_CONFLICT
    assert report["winner"] == filing.source_id
    assert report["rule_id"] == RULE_SOURCE_PRECEDENCE
    assert "已裁决" in report["required_action"]


def test_acme_not_comparable_returns_a_normalization_requirement():
    left = _candidate("a", value=100.0, scope="合并")
    right = _candidate("b", value=100.0, scope="母公司")
    report = compare_pair(left, right)
    assert report["verdict"] == ACME_NOT_COMPARABLE
    assert report["rule_id"] == RULE_NOT_COMPARABLE
    assert "CARD-02" in report["required_action"]
    assert report["winner"] is None


def test_acme_verdicts_are_a_closed_vocabulary():
    from src.cn.conflict import ACME_VERDICTS

    assert ACME_VERDICTS == (ACME_MATCH, ACME_ROUNDING_MATCH, ACME_CONFLICT, ACME_NOT_COMPARABLE)


# --------------------------------------------------------------------------- #
# §9 no silent override — the mechanical assertion
# --------------------------------------------------------------------------- #
def test_assert_no_silent_override_rejects_a_dropped_candidate():
    left = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    choices, records = _resolve(left, right)
    # candidate_a is the discarded vendor; candidate_b is the winner, which the
    # check exempts because it is the selection. Dropping the *loser* must raise.
    dropped = dataclasses.replace(records[0], candidate_a="ghost")
    with pytest.raises(ConflictError, match="静默覆盖"):
        assert_no_silent_override(choices, [dropped])


def test_resolve_conflicts_always_runs_the_no_silent_override_check():
    """The check is inside the engine, so no caller can forget it."""
    left = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    choices, records = _resolve(left, right)
    # Would raise if the engine had produced an unrecorded candidate.
    assert_no_silent_override(choices, records)


def test_every_candidate_lands_in_exactly_one_choice():
    candidates = [
        _candidate("a", value=100.0),
        _candidate("b", value=200.0, source_id="src-b"),
        _candidate("c", value=100.0, metric="net_profit"),
    ]
    choices, _records = _resolve(*candidates)
    seen = [candidate.candidate_id for choice in choices for candidate in choice.candidates]
    assert sorted(seen) == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# intra-document duplicates defer to CARD-02
# --------------------------------------------------------------------------- #
def test_intra_document_duplicate_defers_to_card02_table_priority(registry):
    document, normalization = _ingest(registry, "annual_01_profit_continued.pdf", "annual")
    choices, records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={document.document_id: SOURCE_REGULATORY_FILING},
        company_of=lambda _source: "示例公司",
        occurred_at=STAMP,
    )
    choice = conflict_choices_for_metric(choices, "revenue", "2025FY")
    # 1,234,567 is the audited statement body; 2,000,000 is the summary table.
    assert choice.value == 1_234_567.0
    assert any(record.reason == RULE_INTRA_DOCUMENT_PRECEDENCE for record in records)


def test_intra_document_disagreement_does_not_block(registry):
    document, normalization = _ingest(registry, "annual_01_profit_continued.pdf", "annual")
    choices, records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={document.document_id: SOURCE_REGULATORY_FILING},
        company_of=lambda _source: "示例公司",
        occurred_at=STAMP,
    )
    assert all(choice.status == STATUS_RESOLVED for choice in choices)
    assert unresolved_report(records) == []


def test_intra_document_records_are_attributed_to_card02_not_this_card(registry):
    document, normalization = _ingest(registry, "annual_01_profit_continued.pdf", "annual")
    _choices, records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={document.document_id: SOURCE_REGULATORY_FILING},
        company_of=lambda _source: "示例公司",
        occurred_at=STAMP,
    )
    intra = [record for record in records if record.reason == RULE_INTRA_DOCUMENT_PRECEDENCE]
    assert intra, "the same-document disagreement must be recorded"
    assert all(record.change_kind == CHANGE_DATA_CONFLICT for record in intra)
    assert all("CARD-02" in record.note for record in intra)


# --------------------------------------------------------------------------- #
# source classification
# --------------------------------------------------------------------------- #
def test_default_source_class_maps_document_types():
    assert default_source_class("annual") == SOURCE_REGULATORY_FILING
    assert default_source_class("quarterly") == SOURCE_REGULATORY_FILING
    assert default_source_class("announcement") == SOURCE_COMPANY_ANNOUNCEMENT
    assert default_source_class("") == SOURCE_UNKNOWN
    assert default_source_class("annual", is_correction=True) == SOURCE_REGULATORY_CORRECTION


def test_unclassified_document_never_wins_by_default(registry):
    document, normalization = _ingest(registry, "annual_05_reclassification.pdf", "annual")
    choices, _records = resolve_conflicts(
        list(normalization.facts),
        company_of=lambda _source: "示例公司",
        occurred_at=STAMP,
    )
    # No source_class_by_document was supplied, so every candidate is UNKNOWN.
    assert all(
        choice.selected is None or choice.selected.source_class == SOURCE_UNKNOWN
        for choice in choices
    )
    assert all(choice.status == STATUS_RESOLVED for choice in choices if len(choice.candidates) == 1)


# --------------------------------------------------------------------------- #
# §12 CARD-01 → 02 → 13 + 更正公告正文 全链 (failure injection #5 material)
# --------------------------------------------------------------------------- #
def test_full_chain_correction_pair_supersedes_the_filing(registry):
    filing, normalization = _ingest(registry, "annual_05_reclassification.pdf", "annual")
    announcement, _ = _ingest(registry, "announcement_04_correction.pdf", "announcement")

    pairs, warnings = correction_pairs(
        registry.get_facts(announcement.document_id),
        scope="合并",
        published_at=announcement.publication_date,
    )
    assert warnings == []
    pair = next(item for item in pairs if item.metric == "revenue" and item.period_key == "2024FY")
    assert pair.before_value == 900_000.0
    assert pair.after_value == 1_000_000.0
    assert pair.unit == "元"
    assert pair.unit_source == "explicit"
    assert pair.available_at == "2026-06-12"
    assert pair.page == 1
    assert "更正前" in pair.quote and "更正后" in pair.quote

    choices, records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={
            filing.document_id: default_source_class("annual"),
            announcement.document_id: default_source_class("announcement", is_correction=True),
        },
        extra_candidates=correction_candidates(pairs, source_id=announcement.document_id),
        company_of=lambda _source: "示例材料股份有限公司",
        occurred_at=STAMP,
    )
    choice = conflict_choices_for_metric(choices, "revenue", "2024FY")
    assert choice.value == 1_000_000.0
    assert choice.change_kind == CHANGE_VERSION_SUPERSESSION

    record = next(
        item
        for item in records
        if item.change_kind == CHANGE_VERSION_SUPERSESSION and item.period_key == "2024FY"
    )
    assert record.reason == RULE_REGULATORY_CORRECTION
    assert record.selected_source == announcement.document_id
    assert record.superseded_source == filing.document_id
    assert record.value_a == 900_000.0
    assert record.value_b == 1_000_000.0
    assert validate_conflicts(records).valid


def test_correction_pair_carries_page_and_quote_for_the_jump_ui(registry):
    announcement, _ = _ingest(registry, "announcement_04_correction.pdf", "announcement")
    pairs, _warnings = correction_pairs(registry.get_facts(announcement.document_id), scope="合并")
    assert pairs
    pair = pairs[0]
    assert pair.page is not None
    assert pair.quote
    candidates = correction_candidates(pairs, source_id=announcement.document_id)
    assert candidates[0].ref.startswith("更正公告第")
    assert any("value_from_prose" in note for note in candidates[0].warnings)


def test_correction_without_a_declared_scope_does_not_join_a_scope_group(registry):
    filing, normalization = _ingest(registry, "annual_05_reclassification.pdf", "annual")
    announcement, _ = _ingest(registry, "announcement_04_correction.pdf", "announcement")
    pairs, _warnings = correction_pairs(registry.get_facts(announcement.document_id))
    choices, _records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={
            filing.document_id: SOURCE_REGULATORY_FILING,
            announcement.document_id: SOURCE_REGULATORY_CORRECTION,
        },
        extra_candidates=correction_candidates(pairs, source_id=announcement.document_id),
        company_of=lambda _source: "示例材料股份有限公司",
        occurred_at=STAMP,
    )
    scopes = {(choice.metric, choice.scope) for choice in choices if choice.metric == "revenue"}
    assert ("revenue", None) in scopes, "the undeclared-scope correction keeps its own group"
    choice = next(c for c in choices if c.metric == "revenue" and c.scope is None)
    assert any("scope_undeclared" in note for note in choice.candidates[0].warnings)


# --------------------------------------------------------------------------- #
# correction-pair extraction — fail-closed behaviour
# --------------------------------------------------------------------------- #
class _TextFact:
    """Minimal stand-in for an ExtractedFact holding one paragraph."""

    def __init__(self, text: str, *, page: int = 1, kind: str = "text", role: str | None = None):
        self.raw_text = text
        self.page = page
        self.bbox = (0.0, 0.0, 1.0, 1.0)
        self.kind = kind
        self.fact_id = f"f{abs(hash(text)) % 10000}"
        self.meta = {"role": role} if role else {}


def test_correction_pair_joins_a_sentence_split_across_two_facts():
    facts = [
        _TextFact("更正前2024年度营业收入为900,000元，更正后"),
        _TextFact("为1,000,000元。"),
    ]
    pairs, warnings = correction_pairs(facts, published_at="2026-06-12")
    assert len(pairs) == 1
    assert pairs[0].before_value == 900_000.0
    assert pairs[0].after_value == 1_000_000.0
    assert warnings == []


def test_correction_without_a_unit_is_rejected():
    facts = [_TextFact("更正前2024年度营业收入为900,000，更正后为1,000,000。")]
    pairs, warnings = correction_pairs(facts)
    assert pairs == []
    assert any("correction_unit_missing" in item for item in warnings)


def test_correction_with_an_inherited_unit_says_so():
    facts = [_TextFact("更正前2024年度营业收入为900,000元，更正后为1,000,000")]
    pairs, warnings = correction_pairs(facts)
    assert len(pairs) == 1
    assert pairs[0].unit_source == "sentence"
    assert any("correction_unit_inherited" in item for item in warnings)


def test_correction_without_a_period_is_rejected():
    facts = [_TextFact("更正前营业收入为900,000元，更正后为1,000,000元。")]
    pairs, warnings = correction_pairs(facts)
    assert pairs == []
    assert any("correction_period_undetermined" in item for item in warnings)


def test_correction_period_can_come_from_a_document_level_target():
    facts = [
        _TextFact("本公司对2024年度财务报表进行追溯重述。"),
        _TextFact("更正前营业收入为900,000元，更正后为1,000,000元。"),
    ]
    pairs, _warnings = correction_pairs(facts)
    assert len(pairs) == 1
    assert pairs[0].period_key == "2024FY"


def test_correction_with_an_unknown_metric_is_rejected():
    facts = [_TextFact("更正前某不存在的项目为900,000元，更正后为1,000,000元。")]
    pairs, warnings = correction_pairs(facts)
    assert pairs == []
    assert any("correction_metric_unknown" in item for item in warnings)


def test_correction_pair_reads_a_reversed_anchor_order():
    facts = [_TextFact("更正后为1,000,000元，更正前2024年度营业收入为900,000元。")]
    pairs, _warnings = correction_pairs(facts)
    assert len(pairs) == 1
    assert pairs[0].before_value == 900_000.0
    assert pairs[0].after_value == 1_000_000.0


def test_correction_publication_date_is_read_from_a_fact_not_the_clock():
    facts = [
        _TextFact("发布日期：2026-06-12", role="publication_date"),
        _TextFact("更正前2024年度营业收入为900,000元，更正后为1,000,000元。"),
    ]
    pairs, _warnings = correction_pairs(facts)
    assert pairs[0].available_at == "2026-06-12"


def test_correction_without_a_document_yields_a_warning_not_a_crash():
    pairs, warnings = correction_pairs([])
    assert pairs == []
    assert any("correction_no_text_facts" in item for item in warnings)


# --------------------------------------------------------------------------- #
# facts → candidates
# --------------------------------------------------------------------------- #
def test_candidate_from_fact_carries_page_and_reference(registry):
    _document, normalization = _ingest(registry, "annual_05_reclassification.pdf", "annual")
    fact = next(item for item in normalization.facts if item.metric == "revenue")
    candidate = candidate_from_fact(fact, source_class=SOURCE_REGULATORY_FILING)
    assert candidate.fact is fact
    assert candidate.scope == "合并"
    assert candidate.page is not None
    assert candidate.ref
    assert candidate.rank == 2


# --------------------------------------------------------------------------- #
# §10 validator
# --------------------------------------------------------------------------- #
def test_validator_accepts_a_resolved_run():
    left = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)
    result = validate_conflicts(records)
    assert result.valid, result.errors


def test_validator_rejects_a_free_text_reason():
    record = ConflictRecord(
        conflict_id="CF1",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="d1",
        source_b="d2",
        selected_source="d2",
        reason="看起来新一点",
        resolver="card13.v1",
        timestamp=STAMP,
        candidate_a="c1",
        candidate_b="c2",
        value_a=1.0,
        value_b=2.0,
        selected_value=2.0,
    )
    result = validate_conflicts([record])
    assert not result.valid
    assert any("not a known rule id" in item for item in result.errors)


def test_validator_rejects_a_resolved_record_without_a_selection():
    record = ConflictRecord(
        conflict_id="CF1",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="d1",
        source_b="d2",
        selected_source=None,
        reason=RULE_SOURCE_PRECEDENCE,
        resolver="card13.v1",
        timestamp=STAMP,
        candidate_a="c1",
        candidate_b="c2",
    )
    result = validate_conflicts([record])
    assert not result.valid
    assert any("RESOLVED without a selected_source" in item for item in result.errors)


def test_validator_rejects_an_unresolved_record_that_names_a_winner():
    record = ConflictRecord(
        conflict_id="CF1",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="d1",
        source_b="d2",
        selected_source="d2",
        reason=RULE_TIE_UNRESOLVED,
        resolver="card13.v1",
        timestamp=STAMP,
        status=STATUS_UNRESOLVED,
        candidate_a="c1",
        candidate_b="c2",
    )
    result = validate_conflicts([record])
    assert not result.valid
    assert any("UNRESOLVED but a selected_source is set" in item for item in result.errors)


def test_validator_rejects_a_selection_from_outside_the_pair():
    record = ConflictRecord(
        conflict_id="CF1",
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="d1",
        source_b="d2",
        selected_source="d3",
        reason=RULE_SOURCE_PRECEDENCE,
        resolver="card13.v1",
        timestamp=STAMP,
        candidate_a="c1",
        candidate_b="c2",
        value_a=1.0,
        value_b=2.0,
        selected_value=3.0,
    )
    result = validate_conflicts([record])
    assert not result.valid
    assert any("neither of the two compared sources" in item for item in result.errors)


def test_validator_rejects_a_supersession_without_a_superseded_source():
    record = ConflictRecord(
        conflict_id="CF1",
        change_kind=CHANGE_VERSION_SUPERSESSION,
        source_a="d1",
        source_b="d2",
        selected_source="d2",
        reason=RULE_VERSION_SUPERSESSION,
        resolver="card13.v1",
        timestamp=STAMP,
        candidate_a="c1",
        candidate_b="c2",
        value_a=1.0,
        value_b=2.0,
        selected_value=2.0,
    )
    result = validate_conflicts([record])
    assert not result.valid
    assert any("VERSION_SUPERSESSION without superseded_source" in item for item in result.errors)


def test_validator_rejects_a_duplicated_conflict_id():
    base = dict(
        change_kind=CHANGE_DATA_CONFLICT,
        source_a="d1",
        source_b="d2",
        selected_source="d2",
        reason=RULE_SOURCE_PRECEDENCE,
        resolver="card13.v1",
        timestamp=STAMP,
        candidate_a="c1",
        candidate_b="c2",
        value_a=1.0,
        value_b=2.0,
        selected_value=2.0,
    )
    result = validate_conflicts(
        [ConflictRecord(conflict_id="CF1", **base), ConflictRecord(conflict_id="CF1", **base)]
    )
    assert not result.valid
    assert any("duplicate_conflict_ids" in item for item in result.errors)


def test_validator_reports_the_blocking_check_as_skipped_without_a_graph():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)
    result = validate_conflicts(records)
    assert "no_graph_blocking_check_skipped" in result.warnings


def test_validator_blocks_an_unresolved_metric_from_appearing_in_a_claim():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)

    class _Claim:
        claim_id = "C11"
        metric = "revenue"
        period_key = "2024FY"

    class _Graph:
        claims = {"C11": _Claim()}

    result = validate_conflicts(records, graph=_Graph())
    assert not result.valid
    assert any("unresolved_metric_in_claim" in item for item in result.errors)


def test_validator_passes_when_the_blocked_metric_has_no_claim():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)

    class _Graph:
        claims = {}

    assert validate_conflicts(records, graph=_Graph()).valid


def test_validator_warns_when_no_records_were_produced():
    result = validate_conflicts([])
    assert result.valid
    assert "no_conflict_records" in result.warnings


def test_conflict_record_is_frozen():
    params = getattr(ConflictRecord, "__dataclass_params__", None)
    assert params is not None and params.frozen, "§10 requires append-only records"
    left = _candidate("a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        left.value = 1.0  # type: ignore[misc]


def test_blocked_metric_keys_are_reported_for_the_downstream_gate():
    left = _candidate("a", value=100.0, source_class=SOURCE_THIRD_PARTY)
    right = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(left, right)
    assert blocked_metric_keys(records) == ["revenue@2024FY/合并"]


# --------------------------------------------------------------------------- #
# run-level surface
# --------------------------------------------------------------------------- #
def test_summarize_counts_rules_and_kinds():
    filing = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    vendor = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(filing, vendor)
    summary = summarize(records)
    assert summary["total"] == 1
    assert summary["resolved"] == 1
    assert summary["unresolved"] == 0
    assert summary["by_rule"] == {RULE_SOURCE_PRECEDENCE: 1}
    assert summary["resolver"]


def test_rule_ids_are_a_closed_set_named_in_the_payload():
    assert RULE_TIE_UNRESOLVED in RULE_IDS
    filing = _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING)
    vendor = _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY)
    _choices, records = _resolve(filing, vendor)
    assert all(record.reason in RULE_IDS for record in records)


def test_company_unverified_warning_appears_when_company_of_is_absent(registry):
    filing, normalization = _ingest(registry, "annual_05_reclassification.pdf", "annual")
    announcement, _ = _ingest(registry, "announcement_04_correction.pdf", "announcement")
    # The correction announcement is a second source, so the grouping spans
    # documents. Without a company_of resolver that link is unverified, and the
    # result must say so rather than presenting the grouping as checked.
    pairs, _warnings = correction_pairs(registry.get_facts(announcement.document_id), scope="合并")
    choices, _records = resolve_conflicts(
        list(normalization.facts),
        source_class_by_document={
            filing.document_id: SOURCE_REGULATORY_FILING,
            announcement.document_id: SOURCE_REGULATORY_CORRECTION,
        },
        extra_candidates=correction_candidates(pairs, source_id=announcement.document_id),
        occurred_at=STAMP,
    )
    assert choices
    assert all(
        any("company_unverified_across_documents" in item for item in choice.warnings)
        for choice in choices
    )


def test_selected_by_metric_indexes_every_choice():
    candidates = [
        _candidate("a", value=100.0),
        _candidate("b", value=50.0, metric="net_profit"),
    ]
    choices, _records = _resolve(*candidates)
    index = selected_by_metric(choices)
    assert set(index) == {"revenue@2024FY/合并", "net_profit@2024FY/合并"}


def test_conflict_choices_for_metric_returns_none_when_absent():
    choices, _records = _resolve(_candidate("a"))
    assert conflict_choices_for_metric(choices, "operating_cost") is None


def test_records_are_deterministic_across_runs():
    def run():
        return _resolve(
            _candidate("a", value=100.0, source_class=SOURCE_REGULATORY_FILING),
            _candidate("b", value=200.0, source_class=SOURCE_THIRD_PARTY),
        )[1]

    first, second = run(), run()
    assert [record.to_dict() for record in first] == [record.to_dict() for record in second]
    assert [record.conflict_id for record in first] == [record.conflict_id for record in second]


# --------------------------------------------------------------------------- #
# §6 tool contract
# --------------------------------------------------------------------------- #
def test_conflict_tool_requires_at_least_one_document(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    payload = json.loads(asyncio.run(ResolveCnSourceConflictsTool(registry).execute()))
    assert payload["status"] == "error"
    assert payload["error_code"] == "conflict_missing_argument"


def test_conflict_tool_rejects_an_unknown_document(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    payload = json.loads(
        asyncio.run(ResolveCnSourceConflictsTool(registry).execute(document_ids=["nope"]))
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "conflict_unknown_document"


def test_conflict_tool_returns_the_corrected_value_and_the_rule(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    filing = registry.ingest(
        FIXTURES / "annual_05_reclassification.pdf", declared_type="annual", source="admin_import"
    )
    announcement = registry.ingest(
        FIXTURES / "announcement_04_correction.pdf",
        declared_type="announcement",
        source="admin_import",
    )
    payload = json.loads(
        asyncio.run(
            ResolveCnSourceConflictsTool(registry).execute(
                document_ids=[filing.document.document_id],
                correction_document_id=announcement.document.document_id,
                scope="合并",
                company="示例材料股份有限公司",
            )
        )
    )
    assert payload["status"] == "ok"
    assert payload["validation"]["valid"] is True
    assert payload["change_kinds"] == [
        CHANGE_DATA_CONFLICT,
        CHANGE_VERSION_SUPERSESSION,
        CHANGE_NEW_INFORMATION,
    ]
    assert payload["conflict_summary"]["by_rule"][RULE_REGULATORY_CORRECTION] == 1
    assert payload["correction_pairs"][0]["after_value"] == 1_000_000.0

    choice = next(
        item
        for item in payload["choices"]
        if item["metric"] == "revenue" and item["period_key"] == "2024FY"
    )
    assert choice["value"] == 1_000_000.0
    assert choice["change_kind"] == CHANGE_VERSION_SUPERSESSION
    # The winning value came from prose, so it must be labelled as such rather
    # than dressed up as a table fact.
    assert choice["value_from_prose"] is True
    assert choice["selected_fact_id"] is None
    assert payload["engine"].startswith("src.cn.conflict")


def test_conflict_tool_without_a_declared_scope_keeps_the_pair_apart(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    filing = registry.ingest(
        FIXTURES / "annual_05_reclassification.pdf", declared_type="annual", source="admin_import"
    )
    announcement = registry.ingest(
        FIXTURES / "announcement_04_correction.pdf",
        declared_type="announcement",
        source="admin_import",
    )
    payload = json.loads(
        asyncio.run(
            ResolveCnSourceConflictsTool(registry).execute(
                document_ids=[filing.document.document_id],
                correction_document_id=announcement.document.document_id,
            )
        )
    )
    assert payload["declared_scope"] is None
    assertions = [
        item
        for item in payload["choices"]
        if item["metric"] == "revenue" and item["scope"] is None
    ]
    assert assertions, "the undeclared-scope correction keeps its own group"
    assert any("scope_undeclared" in note for note in assertions[0]["selected_candidate"]["warnings"])


def test_conflict_tool_is_registered_in_the_snapshot_toolset():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = [tool.name for tool in build_cn_snapshot_tools()]
    assert "resolve_cn_source_conflicts" in names


# --------------------------------------------------------------------------- #
# §6 tool contract
# --------------------------------------------------------------------------- #
def test_conflict_tool_requires_at_least_one_document(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    payload = json.loads(asyncio.run(ResolveCnSourceConflictsTool(registry).execute()))
    assert payload["status"] == "error"
    assert payload["error_code"] == "conflict_missing_argument"


def test_conflict_tool_rejects_an_unknown_document(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    payload = json.loads(
        asyncio.run(ResolveCnSourceConflictsTool(registry).execute(document_ids=["nope"]))
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "conflict_unknown_document"


def test_conflict_tool_returns_the_corrected_value_and_the_rule(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    filing = registry.ingest(
        FIXTURES / "annual_05_reclassification.pdf", declared_type="annual", source="admin_import"
    )
    announcement = registry.ingest(
        FIXTURES / "announcement_04_correction.pdf",
        declared_type="announcement",
        source="admin_import",
    )
    payload = json.loads(
        asyncio.run(
            ResolveCnSourceConflictsTool(registry).execute(
                document_ids=[filing.document.document_id],
                correction_document_id=announcement.document.document_id,
                scope="合并",
                company="示例材料股份有限公司",
            )
        )
    )
    assert payload["status"] == "ok"
    assert payload["validation"]["valid"] is True
    assert payload["change_kinds"] == [
        CHANGE_DATA_CONFLICT,
        CHANGE_VERSION_SUPERSESSION,
        CHANGE_NEW_INFORMATION,
    ]
    assert payload["conflict_summary"]["by_rule"][RULE_REGULATORY_CORRECTION] == 1
    assert payload["correction_pairs"][0]["after_value"] == 1_000_000.0

    choice = next(
        item
        for item in payload["choices"]
        if item["metric"] == "revenue" and item["period_key"] == "2024FY"
    )
    assert choice["value"] == 1_000_000.0
    assert choice["change_kind"] == CHANGE_VERSION_SUPERSESSION
    # The winning value came from prose, so it must be labelled as such rather
    # than dressed up as a table fact.
    assert choice["value_from_prose"] is True
    assert choice["selected_fact_id"] is None
    assert payload["engine"].startswith("src.cn.conflict")


def test_conflict_tool_without_a_declared_scope_keeps_the_pair_apart(registry):
    import asyncio

    from src.agents.tools.cn_tools import ResolveCnSourceConflictsTool

    filing = registry.ingest(
        FIXTURES / "annual_05_reclassification.pdf", declared_type="annual", source="admin_import"
    )
    announcement = registry.ingest(
        FIXTURES / "announcement_04_correction.pdf",
        declared_type="announcement",
        source="admin_import",
    )
    payload = json.loads(
        asyncio.run(
            ResolveCnSourceConflictsTool(registry).execute(
                document_ids=[filing.document.document_id],
                correction_document_id=announcement.document.document_id,
            )
        )
    )
    assert payload["declared_scope"] is None
    assertions = [
        item
        for item in payload["choices"]
        if item["metric"] == "revenue" and item["scope"] is None
    ]
    assert assertions, "the undeclared-scope correction keeps its own group"
    assert any("scope_undeclared" in note for note in assertions[0]["selected_candidate"]["warnings"])


def test_conflict_tool_is_registered_in_the_snapshot_toolset():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = [tool.name for tool in build_cn_snapshot_tools()]
    assert "resolve_cn_source_conflicts" in names
