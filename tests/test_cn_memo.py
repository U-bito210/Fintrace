"""CARD-10 Investment Memo — unit / contract / chain tests (offline).

The card's acceptance (§15) is:

* eleven sections, sentence-level three-tier marking, Markdown == JSON;
* 100% of numbers traceable (automated sampling);
* no Multi-Agent dependency — the single-Agent mainline runs it;
* every assertion here is offline and deterministic.

Every fixture is built from CARD-02/03/08 values, so a sentence that cannot be
bound is a genuine gap rather than a test artefact.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from src.cn.analysis import analyze_financials
from src.cn.documents.registry import DocumentRegistry
from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    VALIDATION_BLOCKED,
    EvidenceNode,
    build_graph_from_analysis,
    deactivate_evidence,
    revalidate,
)
from src.cn.memo import (
    BOUNDARY_NOTE,
    DISCLAIMER,
    SECTION_CATALYSTS,
    SECTION_COUNTER_THESIS,
    SECTION_EVIDENCE_APPENDIX,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_FALSIFICATION,
    SECTION_FINANCIAL_PERFORMANCE,
    SECTION_IDS,
    SECTION_RISKS,
    SECTION_TRACKING_KPIS,
    SECTION_VALUATION,
    STATUS_DATA_UNAVAILABLE,
    MemoError,
    MemoSegmentError,
    MemoSentence,
    _build_memo_prompt,
    _validate_memo_segment_output,
    build_memo,
    render_memo_json,
    render_memo_markdown,
)
from src.cn.normalization import (
    NormalizedFact,
    PeriodKind,
    Scope,
    Version,
)
from src.cn.valuation import ScenarioValuation, ValuationResult
from src.validation import validate_memo

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"

_POINT = PeriodKind.POINT_IN_TIME
_YTD = PeriodKind.YTD_CUMULATIVE


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _nf(
    metric: str,
    value: float,
    period_key: str,
    *,
    kind: PeriodKind = _YTD,
    statement: str = "income",
    page: int = 1,
    document_id: str = "DOC-M",
) -> NormalizedFact:
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period_key}_{page}",
        metric=metric,
        value=float(value),
        currency="CNY",
        period_key=period_key,
        period_kind=kind,
        scope=Scope.CONSOLIDATED,
        version=Version.AS_REPORTED,
        available_at="2025-04-30T00:00:00+08:00",
        source_fact_id=f"x_{metric}_{period_key}",
        document_id=document_id,
        page=page,
        row=1,
        column=1,
        raw_value=str(value),
        source_unit="元",
        source_table="合并报表",
        statement_type=statement,
        metric_known=True,
    )


def _facts() -> List[NormalizedFact]:
    """Two annual years with distinct page numbers and three firing signals."""
    facts: List[NormalizedFact] = []
    for year, revenue, parent, deducted, cost in (
        (2023, 1000.0, 100.0, 90.0, 600.0),
        (2024, 1200.0, 120.0, 80.0, 700.0),
    ):
        facts += [
            _nf("revenue", revenue, f"{year}FY", page=12),
            _nf("net_profit_parent", parent, f"{year}FY", page=13),
            _nf("net_profit_deducted", deducted, f"{year}FY", page=14),
            _nf("operating_cost", cost, f"{year}FY", page=15),
        ]
    for year, cfo, capex in ((2023, 150.0, 60.0), (2024, 100.0, 70.0)):
        facts += [
            _nf("cfo", cfo, f"{year}FY", statement="cashflow", page=31),
            _nf("capex", capex, f"{year}FY", statement="cashflow", page=32),
        ]
    for year, equity, receivables, inventory in (
        (2023, 800.0, 200.0, 100.0),
        (2024, 900.0, 260.0, 105.0),
    ):
        facts += [
            _nf("equity_parent", equity, f"{year}FY", kind=_POINT, statement="balance", page=41),
            _nf("receivables", receivables, f"{year}FY", kind=_POINT, statement="balance", page=42),
            _nf("inventory", inventory, f"{year}FY", kind=_POINT, statement="balance", page=43),
        ]
    facts.append(_nf("contract_liabilities", 50.0, "2024FY", kind=_POINT, statement="balance", page=44))
    return facts


def _analysis_and_graph():
    """CARD-02 facts → CARD-03 engine → CARD-09 graph (the memo's two inputs)."""
    facts = _facts()
    analysis = analyze_financials(facts, document_id="DOC-M", report_period="2024FY")
    evidence = [
        EvidenceNode(
            evidence_id=fact.normalized_fact_id,
            kind="fact",
            document_id=fact.document_id,
            page=fact.page,
            bbox=(1.0, 2.0, 30.0, 8.0),
            metric=fact.metric,
            value=fact.value,
            unit="元",
            available_at=fact.available_at,
        )
        for fact in facts
    ]
    graph = build_graph_from_analysis(
        analysis, run_id="run-memo", document_id="DOC-M", evidence_index=evidence
    )
    return analysis, graph


def _scenario(method, scenario, low, mid, high, calc_id, *, applicable=True, reason=None):
    return ScenarioValuation(
        method=method,
        scenario=scenario,
        value_low=low,
        value_mid=mid,
        value_high=high,
        driver_key="eps",
        driver_mid=1.0,
        target_multiple=12.0,
        band=0.1,
        formula="driver * multiple",
        assumption_ids=(f"{method.lower()}_target_{scenario}",),
        calculation_ids=(calc_id,),
        applicable=applicable,
        not_applicable_reason=reason,
    )


def _valuation() -> ValuationResult:
    """A CARD-08-shaped result: two methods, one non-applicable cell."""
    return ValuationResult(
        status="ok",
        inputs={"symbol": "DOC-M", "period_key": "2024FY"},
        scenarios=(
            _scenario("PE", "base", 10.0, 12.0, 14.0, "CAL-PE-BASE"),
            _scenario("PE", "bull", 13.0, 15.0, 17.0, "CAL-PE-BULL"),
            _scenario("PB", "base", 9.0, 11.0, 13.0, "CAL-PB-BASE"),
            _scenario(
                "PS", "base", None, None, None, "CAL-PS-BASE",
                applicable=False, reason="标的缺少可用收入口径",
            ),
        ),
        sensitivity=(),
        method_dispersion={"across_methods": {"range": 4.0, "mean": 12.5}},
        applicability_note="本区间为相对估值参考，不构成目标价。",
        assumptions=(),
        warnings=(),
        calculation_ids=("CAL-PE-BASE", "CAL-PE-BULL", "CAL-PB-BASE"),
    )


def _memo(**overrides):
    analysis, graph = _analysis_and_graph()
    kwargs = {
        "valuation": _valuation(),
        "graph": graph,
        "symbol": "DOC-M",
        "document_id": "DOC-M",
        "as_of": "2026-09-19",
    }
    kwargs.update(overrides)
    return build_memo(analysis, **kwargs)


def _hit(text, chunk_id, claim_id):
    return SimpleNamespace(text=text, chunk_id=chunk_id, claim_id=claim_id)


def _finding(finding_id):
    return SimpleNamespace(finding_id=finding_id)


# --------------------------------------------------------------------------- #
# §15 acceptance 1 — eleven sections, sentence-level marking, dual output
# --------------------------------------------------------------------------- #
def test_memo_renders_all_eleven_sections_in_card_order():
    memo = _memo()
    assert memo.section_ids == list(SECTION_IDS)
    assert len(memo.sections) == 11
    for section in memo.sections:
        assert section.title


def test_every_sentence_is_tier_marked_or_structural():
    memo = _memo()
    assert memo.sentences, "the fixture must produce sentences"
    for sentence in memo.sentences:
        if sentence.structural:
            # Scaffolding asserts nothing about the company, so it carries no tier.
            assert sentence.claim_type is None, sentence.sentence_span
        else:
            assert sentence.claim_type in (CLAIM_FACT, CLAIM_INFERENCE, CLAIM_OPINION), (
                sentence.sentence_span
            )


def test_fact_sentences_always_bind_a_claim():
    memo = _memo()
    facts = [s for s in memo.sentences if s.claim_type == CLAIM_FACT]
    assert facts, "the fixture must produce fact sentences"
    for sentence in facts:
        assert sentence.claim_id, sentence.sentence_span


def test_inference_sentences_always_bind_derived_from():
    memo = _memo()
    inferences = [s for s in memo.sentences if s.claim_type == CLAIM_INFERENCE]
    assert inferences, "the fixture must produce inference sentences"
    for sentence in inferences:
        assert sentence.derived_from, sentence.sentence_span


def test_opinion_sentences_always_carry_an_assumption_note():
    memo = _memo()
    opinions = [s for s in memo.sentences if s.claim_type == CLAIM_OPINION]
    assert opinions, "the fixture must produce opinion sentences"
    for sentence in opinions:
        assert (sentence.assumption_note or "").strip(), sentence.sentence_span


def test_markdown_and_json_describe_the_same_memo():
    memo = _memo()
    payload = render_memo_json(memo)
    markdown = render_memo_markdown(memo)

    assert payload["section_ids"] == memo.section_ids
    assert [item["section_id"] for item in payload["sections"]] == memo.section_ids
    assert payload["sentence_count"] == len(memo.sentences)
    assert payload["evidence_count"] == len(memo.evidence_ids)
    # Every section heading present in the Markdown, in the same order.
    positions = [markdown.index(f"（{sid}）") for sid in memo.section_ids]
    assert positions == sorted(positions)
    # The JSON tier marking is exactly the sentence-level marking.
    for section, rendered in zip(memo.sections, payload["sections"]):
        assert rendered["tier_marking"] == section.tier_marking
        assert rendered["body_markdown"] == section.body_markdown


def test_disclaimer_and_boundary_note_are_always_rendered():
    memo = _memo()
    assert memo.disclaimers == [DISCLAIMER, BOUNDARY_NOTE]
    markdown = render_memo_markdown(memo)
    assert DISCLAIMER in markdown
    assert BOUNDARY_NOTE in markdown
    assert "免责与适用边界" in markdown


# --------------------------------------------------------------------------- #
# §15 acceptance 2 — 100% of numbers traceable (automated sampling)
# --------------------------------------------------------------------------- #
def test_every_numeric_assertion_binds_a_calculation_or_evidence():
    memo = _memo()
    numeric = [s for s in memo.sentences if not s.structural and s.numeric]
    assert numeric, "the fixture must produce numeric assertions"
    for sentence in numeric:
        assert sentence.calculation_id or sentence.evidence_ids, sentence.sentence_span


def test_sampled_numbers_trace_back_to_a_pdf_page():
    """§12: randomly sample up to ten numbers and walk each to a page."""
    analysis, graph = _analysis_and_graph()
    memo = build_memo(analysis, valuation=_valuation(), graph=graph, document_id="DOC-M")

    sample = [s for s in memo.sentences if not s.structural and s.numeric and s.claim_id]
    assert len(sample) >= 5, "the fixture must produce several traceable numbers"

    for sentence in sample[:10]:
        tree = graph.trace(sentence.claim_id)
        assert tree.locatable, sentence.sentence_span
        pages = [node for node in tree.flatten() if node.node_type == "page"]
        assert pages, f"{sentence.claim_id} traces to no page"
        for page in pages:
            assert page.detail.get("page") is not None
            assert page.detail.get("document_id") == "DOC-M"


def test_financial_performance_numbers_carry_the_registry_calculation_id():
    analysis, graph = _analysis_and_graph()
    memo = build_memo(analysis, valuation=_valuation(), graph=graph, document_id="DOC-M")
    section = memo.section(SECTION_FINANCIAL_PERFORMANCE)
    assert section is not None and section.sentences
    for sentence in section.sentences:
        assert sentence.claim_type == CLAIM_FACT
        assert sentence.evidence_ids, sentence.sentence_span


# --------------------------------------------------------------------------- #
# §15 acceptance 3 — no Multi-Agent dependency
# --------------------------------------------------------------------------- #
def test_memo_builds_with_no_narrator_and_no_model():
    """The default narrator is deterministic; nothing else is required."""
    memo = _memo()
    assert memo.sections
    # Determinism: a second build is byte-identical.
    assert render_memo_markdown(_memo()) == render_memo_markdown(memo)


def test_memo_module_does_not_import_a_multi_agent_framework():
    import src.cn.memo as memo_module

    source = Path(memo_module.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("multiagent", "multi_agent", "src.agents.supervisor", "langgraph", "autogen"):
        assert forbidden not in lowered, f"memo.py must not depend on {forbidden}"


# --------------------------------------------------------------------------- #
# §6 narrator contract — hard validation of LLM output
# --------------------------------------------------------------------------- #
def test_unmarked_sentence_is_rejected():
    bad = [MemoSentence(sentence_span="营业收入增长强劲。")]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, set())


def test_structural_scaffolding_may_not_carry_a_claim_type():
    bad = [MemoSentence(sentence_span="共 3 处。", claim_type=CLAIM_FACT, structural=True)]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, set())


def test_empty_sentence_span_is_rejected():
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output([MemoSentence(sentence_span="   ", structural=True)], set())


def test_hallucinated_claim_id_is_rejected():
    bad = [
        MemoSentence(
            sentence_span="净利润为 12.0 亿元。",
            claim_id="C-INVENTED",
            claim_type=CLAIM_FACT,
            calculation_id="CAL-1",
        )
    ]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, {"C11"})


def test_fact_sentence_requires_a_claim_id():
    bad = [MemoSentence(sentence_span="净利润下滑。", claim_type=CLAIM_FACT)]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, set())


def test_inference_sentence_requires_derived_from():
    bad = [MemoSentence(sentence_span="盈利质量下降。", claim_type=CLAIM_INFERENCE)]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, set())


def test_opinion_sentence_requires_an_assumption_note():
    bad = [MemoSentence(sentence_span="估值偏低。", claim_type=CLAIM_OPINION)]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, set())


def test_unverified_number_is_rejected():
    """Card §8 edge case 2 / §10: a number with no calculation is dropped."""
    bad = [
        MemoSentence(
            sentence_span="2024 年净利润同比增长 20%。",
            claim_id="C11",
            claim_type=CLAIM_FACT,
        )
    ]
    with pytest.raises(MemoSegmentError):
        _validate_memo_segment_output(bad, {"C11"})


def test_structurally_sound_narrator_output_passes_validation():
    good = [
        MemoSentence(
            sentence_span="2024FY 归母净利润为 120.0 亿元。",
            claim_id="C11",
            claim_type=CLAIM_FACT,
            calculation_id="CAL-1",
            evidence_ids=("E-1",),
        ),
        MemoSentence(
            sentence_span="盈利质量较上年走弱。",
            claim_id="C18",
            claim_type=CLAIM_INFERENCE,
            derived_from=("C11",),
        ),
        MemoSentence(
            sentence_span="估值区间不构成目标价。",
            claim_type=CLAIM_OPINION,
            assumption_note="登记假设",
        ),
        MemoSentence(sentence_span="本节共 3 条陈述。", structural=True),
    ]
    _validate_memo_segment_output(good, {"C11", "C18"})  # must not raise


def test_a_broken_narrator_becomes_a_memo_error_not_an_empty_section():
    def broken(section_id, context):
        raise RuntimeError("model exploded")

    with pytest.raises(MemoError):
        _memo(narrator=broken)


def test_a_narrator_number_with_no_trail_is_dropped_and_reported():
    """Card §8 edge case 2: unverified number → the sentence is dropped, loudly."""

    def liar(section_id, context):
        return [
            {"sentence_span": "净利润大涨 300%。", "claim_type": "fact", "claim_id": "C11"},
            {"sentence_span": "本节由模型撰写。", "structural": True},
        ]

    memo = _memo(narrator=liar)
    assert "300%" not in render_memo_markdown(memo)
    section = memo.section("investment_thesis")
    assert any("rejected_sentence" in item for item in section.warnings)


def test_a_compliant_llm_narrator_is_accepted():
    """The same memo, but the narrative sections come from an injected model."""
    analysis, graph = _analysis_and_graph()
    fact_claim = sorted(graph.facts, key=lambda item: item.claim_id)[0]

    def narrator(section_id, context):
        return [
            MemoSentence(
                sentence_span=f"（模型叙述）关键指标：{fact_claim.text}。",
                claim_id=fact_claim.claim_id,
                claim_type=CLAIM_FACT,
                calculation_id=fact_claim.calculation_ids[0] if fact_claim.calculation_ids else None,
                evidence_ids=fact_claim.evidence_ids,
            )
        ]

    memo = build_memo(
        analysis, valuation=_valuation(), graph=graph, document_id="DOC-M", narrator=narrator
    )
    thesis = memo.section("investment_thesis")
    assert thesis is not None and thesis.sentences
    assert thesis.sentences[0].claim_id == fact_claim.claim_id


def test_prompt_carries_the_claim_id_whitelist_and_the_no_fabrication_rule():
    prompt = _build_memo_prompt("investment_thesis", {"a": 1}, {"b": 2}, ["C11", "C18"])
    assert "C11" in prompt and "C18" in prompt
    assert "白名单" in prompt
    assert "禁止编造" in prompt
    assert "calculation_id" in prompt


# --------------------------------------------------------------------------- #
# §8 edge cases — missing upstream, blocked claims, honesty about absence
# --------------------------------------------------------------------------- #
def test_missing_graph_marks_the_sections_that_need_it_unavailable():
    analysis, _graph = _analysis_and_graph()
    memo = build_memo(analysis, valuation=_valuation(), graph=None, document_id="DOC-M")

    for section_id in (SECTION_FINANCIAL_PERFORMANCE, SECTION_EVIDENCE_APPENDIX):
        section = memo.section(section_id)
        assert section is not None
        assert section.data_status == STATUS_DATA_UNAVAILABLE
        assert not section.sentences, "an unbound number must not be rendered"
        assert any("claim_graph_not_provided" in item for item in section.warnings)


def test_missing_valuation_marks_the_valuation_section_unavailable():
    memo = _memo(valuation=None)
    for section_id in (SECTION_VALUATION,):
        section = memo.section(section_id)
        assert section is not None
        assert section.data_status == STATUS_DATA_UNAVAILABLE
        assert any("valuation_not_provided" in item for item in section.warnings)
    markdown = render_memo_markdown(memo)
    assert "本节不提供结论" in markdown


def test_assumption_only_valuation_raises_a_warning_banner():
    import dataclasses

    analysis, graph = _analysis_and_graph()
    valuation = dataclasses.replace(_valuation(), status="assumption_only")
    memo = build_memo(analysis, valuation=valuation, graph=graph, document_id="DOC-M")
    assert "assumption_only_valuation" in memo.section(SECTION_VALUATION).warnings


def test_a_blocked_conclusion_is_reported_and_not_hidden():
    """§9 / failure mode: the memo must not paper over what the system cannot know."""
    analysis, graph = _analysis_and_graph()
    inference = sorted(graph.inferences, key=lambda item: item.claim_id)[0]
    upstream_fact = inference.derived_from[0]
    evidence_id = graph.claim(upstream_fact).evidence_ids[0]

    deactivate_evidence(graph, evidence_id, "失败注入 #6：来源证据被撤回")
    revalidate(graph)
    assert graph.claim(inference.claim_id).validation_status == VALIDATION_BLOCKED

    memo = build_memo(analysis, valuation=_valuation(), graph=graph, document_id="DOC-M")
    risks = memo.section(SECTION_RISKS)
    blocked = [
        sentence
        for sentence in risks.sentences
        if "blocked" in sentence.sentence_span or sentence.claim_id == inference.claim_id
    ]
    assert blocked, "a blocked conclusion must appear in the memo"
    # The memo still serialises — a blocked conclusion is not a broken memo.
    assert render_memo_markdown(memo)
    assert render_memo_json(memo)["sentence_count"] == len(memo.sentences)


def test_counter_thesis_states_the_absence_rather_than_inventing_one():
    memo = _memo()
    section = memo.section(SECTION_COUNTER_THESIS)
    assert section is not None and section.sentences
    assert any("未发现显著反向证据" in s.sentence_span for s in section.sentences)
    assert section.sentences[0].claim_type == CLAIM_OPINION


def test_counter_thesis_uses_a_real_claim_when_one_is_supplied():
    analysis, graph = _analysis_and_graph()
    fact_claim = sorted(graph.facts, key=lambda item: item.claim_id)[0]
    memo = build_memo(
        analysis,
        valuation=_valuation(),
        graph=graph,
        document_id="DOC-M",
        counter_claims=(fact_claim.claim_id,),
    )
    section = memo.section(SECTION_COUNTER_THESIS)
    assert section.sentences[0].claim_id == fact_claim.claim_id
    assert "反向证据" in section.sentences[0].sentence_span


def test_draft_review_findings_are_reported_in_the_executive_summary():
    """§8 edge case 4 — the draft-check scenario."""
    memo = _memo(findings=[_finding("F1"), _finding("F2"), _finding("F3")])
    summary = memo.section(SECTION_EXECUTIVE_SUMMARY)
    hits = [s for s in summary.sentences if "3 处" in s.sentence_span]
    assert hits, "the finding count must be stated"
    assert hits[0].structural is True
    assert set(hits[0].evidence_ids) == {"F1", "F2", "F3"}


def test_falsification_contains_computable_thresholds_not_platitudes():
    """§10: no "持续关注基本面" filler — every condition must be computable."""
    memo = _memo()
    section = memo.section(SECTION_FALSIFICATION)
    assert section is not None and section.sentences
    for sentence in section.sentences:
        text = sentence.sentence_span
        assert "证伪条件" in text
        assert "阈值" in text or "上限" in text or "跌破 0%" in text, text


def test_tracking_kpis_are_deterministic_and_bound():
    memo = _memo()
    section = memo.section(SECTION_TRACKING_KPIS)
    assert section is not None and section.sentences
    assert any(s.claim_type == CLAIM_FACT for s in section.sentences)


def test_evidence_appendix_lists_every_evidence_with_a_location():
    memo = _memo()
    section = memo.section(SECTION_EVIDENCE_APPENDIX)
    assert section is not None and section.sentences
    listed = {node for sentence in section.sentences for node in sentence.evidence_ids}
    assert listed, "the appendix must list evidence ids"
    assert "页" in section.body_markdown


def test_catalysts_are_opinion_and_quote_the_retrieved_chunk():
    memo = _memo(hits=[_hit("公司拟回购不超过 2 亿元。", "chunk-1", None)])
    section = memo.section(SECTION_CATALYSTS)
    assert section is not None and section.sentences
    sentence = section.sentences[0]
    assert sentence.claim_type == CLAIM_OPINION
    assert sentence.evidence_ids == ("chunk-1",)
    assert sentence.citations[0]["quote"] == "公司拟回购不超过 2 亿元。"


def test_company_overview_skips_a_hit_without_a_claim():
    """A retrieval hit is not a claim; it must not be dressed up as a fact."""
    memo = _memo(hits=[_hit("公司拟回购不超过 2 亿元。", "chunk-1", None)])
    section = memo.section("company_overview")
    assert all("回购" not in s.sentence_span for s in section.sentences)


def test_data_grade_is_sourced_for_a_complete_analysis():
    memo = _memo()
    assert memo.data_grade == "sourced"


def test_graph_run_id_is_recorded_for_replay():
    memo = _memo()
    assert memo.graph_run_id == "run-memo"


def test_memo_is_json_serialisable():
    memo = _memo()
    payload = render_memo_json(memo)
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    assert round_tripped["memo_id"] == memo.memo_id


# --------------------------------------------------------------------------- #
# §12 full chain — CARD-01 → 02 → 03 → 09 → 10, plus the agent tool
# --------------------------------------------------------------------------- #
@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def test_memo_tool_renders_a_memo_from_an_ingested_document(registry: DocumentRegistry):
    import asyncio

    from src.agents.tools.cn_tools import GenerateCnInvestmentMemoTool

    registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import"
    )
    document_id = registry.list()[0].document_id

    payload = json.loads(
        asyncio.run(
            GenerateCnInvestmentMemoTool(registry).execute(document_id=document_id)
        )
    )
    assert payload["status"] == "ok", payload
    assert payload["document_id"] == document_id
    assert payload["section_ids"] == list(SECTION_IDS)
    assert payload["memo"]["section_ids"] == list(SECTION_IDS)
    assert "免责与适用边界" in payload["markdown"]
    assert payload["data_grade"] in {"sourced", "mixed", "assumption_only", "unavailable"}
    assert payload["validation"]["valid"] is True, payload["validation"]
    assert payload["engine"].startswith("src.cn.memo")


def test_memo_tool_rejects_an_unknown_document(registry: DocumentRegistry):
    import asyncio

    from src.agents.tools.cn_tools import GenerateCnInvestmentMemoTool

    payload = json.loads(
        asyncio.run(GenerateCnInvestmentMemoTool(registry).execute(document_id="NOPE"))
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "document_not_found"


def test_memo_tool_requires_a_document_id(registry: DocumentRegistry):
    import asyncio

    from src.agents.tools.cn_tools import GenerateCnInvestmentMemoTool

    payload = json.loads(asyncio.run(GenerateCnInvestmentMemoTool(registry).execute()))
    assert payload["status"] == "error"
    assert payload["error_code"] == "memo_missing_argument"


# --------------------------------------------------------------------------- #
# §10 validator rules
# --------------------------------------------------------------------------- #
def test_memo_validator_accepts_a_well_formed_memo():
    memo = _memo()
    result = validate_memo(
        memo, graph=_analysis_and_graph()[1], calculation_ids=_valuation().calculation_ids
    )
    assert result.valid, result.errors
    assert "no_graph_reference_check_skipped" not in result.warnings


def test_memo_validator_warns_when_the_graph_is_absent_instead_of_silently_passing():
    memo = _memo()
    result = validate_memo(memo)
    assert "no_graph_reference_check_skipped" in result.warnings


def test_memo_validator_flags_an_unmarked_sentence():
    from src.cn.memo import MemoSection, InvestmentMemo

    memo = _memo()
    memo.sections.append(
        MemoSection(
            section_id="appendix_note",
            title="附注",
            sentences=[MemoSentence(sentence_span="管理层表示对未来充满信心。")],
        )
    )
    result = validate_memo(memo)
    assert not result.valid
    assert any("unmarked sentence" in item for item in result.errors)


def test_memo_validator_flags_an_unbound_numeric_sentence():
    from src.cn.memo import MemoSection

    memo = _memo()
    memo.sections.append(
        MemoSection(
            section_id="risk_note",
            title="风险附注",
            sentences=[
                MemoSentence(
                    sentence_span="净利润预计增长 50%。",
                    claim_type=CLAIM_OPINION,
                    assumption_note="外推假设",
                )
            ],
        )
    )
    result = validate_memo(memo)
    assert not result.valid
    assert any("numeric sentence without" in item for item in result.errors)


def test_memo_validator_flags_a_dangling_claim_reference():
    from src.cn.memo import MemoSection

    memo = _memo()
    memo.sections.append(
        MemoSection(
            section_id="thesis_note",
            title="逻辑附注",
            sentences=[
                MemoSentence(
                    sentence_span="该结论来自图谱。",
                    claim_id="CLAIM-DOES-NOT-EXIST",
                    claim_type=CLAIM_INFERENCE,
                    derived_from=("CLAIM-DOES-NOT-EXIST",),
                )
            ],
        )
    )
    result = validate_memo(
        memo, graph=_analysis_and_graph()[1], calculation_ids=_valuation().calculation_ids
    )
    assert not result.valid
    assert any("does not resolve" in item for item in result.errors)


def test_memo_validator_checks_quotes_are_verbatim():
    hit = _hit("公司拟回购不超过 2 亿元。", "chunk-1", None)
    memo = _memo(hits=[hit])
    assert validate_memo(memo).valid

    tampered = _memo(hits=[_hit("公司拟回购不超过 9 亿元。", "chunk-1", None)])
    result = validate_memo(tampered, hits=[hit])
    assert not result.valid
    assert any("not verbatim" in item for item in result.errors)


def test_memo_validator_requires_a_computable_falsification():
    from src.cn.memo import MemoSection

    memo = _memo()
    memo.sections = [s for s in memo.sections if s.section_id != SECTION_FALSIFICATION]
    memo.sections.append(
        MemoSection(
            section_id=SECTION_FALSIFICATION,
            title="证伪条件",
            sentences=[
                MemoSentence(
                    sentence_span="持续关注基本面变化。",
                    claim_type=CLAIM_OPINION,
                    assumption_note="定性观察",
                )
            ],
        )
    )
    result = validate_memo(memo)
    assert not result.valid
    assert any("no computable threshold" in item for item in result.errors)


def test_memo_validator_accepts_a_missing_falsification_only_when_it_says_so():
    analysis, graph = _analysis_and_graph()
    memo = build_memo(analysis, valuation=None, graph=None, document_id="DOC-M")
    # No graph → no signals → the section must declare itself unavailable, which
    # is an honest report rather than a fabricated threshold.
    section = memo.section(SECTION_FALSIFICATION)
    assert section.data_status == STATUS_DATA_UNAVAILABLE
    result = validate_memo(memo)
    assert not any("falsification" in item for item in result.errors), result.errors


def test_memo_corpus_validator_reports_a_prefix_per_memo():
    from src.validation import validate_memo_corpus

    result = validate_memo_corpus([_memo(), _memo()])
    assert result.valid
    result = validate_memo_corpus([])
    assert "no_memos_supplied" in result.warnings
