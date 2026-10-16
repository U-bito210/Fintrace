"""Orchestration: a registered draft document in, a correction report out.

This is the one place that knows how the pieces connect, and it exists so that the
Agent tool, the workbench and the tests all exercise the *same* read model —
mirroring CARD-01/03/04's ``document_service`` convention rather than letting each
caller re-assemble the pipeline.

Flow (CARD-01 → CARD-05, with 02/03/04 in between)::

    draft facts ──sentences──> claim extraction ──> claims
    reference filings ──normalize (CARD-02)──> normalized facts ──> FactIndex
    reference filings ──detect signals (CARD-04)──> scope signals
    claims × FactIndex × signals ──deterministic rules──> findings

Reference selection is explicit. When the caller names no reference documents, the
pipeline uses every *other* registered document that produced facts, and **says so
in the payload** — an auto-selection that stayed invisible would be exactly the
kind of silent assumption this project forbids.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..analysis import detect_scope_signals
from ..documents.models import DocumentType
from ..documents.registry import DocumentRegistry
from ..normalization import normalize_financials
from .checker import build_report, check_report, industry_check_capability
from .extractor import ClaimExtractionClient, ExtractionOutcome, extract_claims
from .facts import FactIndex, ReferenceIndex, format_amount
from .models import DraftClaim
from .rule_extractor import EXTRACTOR_RULES, extract_claims_rules
from .sentences import DraftSentence, sentences_from_facts

#: Document kinds that can serve as a *source* a draft is checked against.
REFERENCE_TYPES = (
    DocumentType.ANNUAL.value,
    DocumentType.INTERIM.value,
    DocumentType.QUARTERLY.value,
    DocumentType.ANNOUNCEMENT.value,
)


class DraftNotFound(KeyError):
    """The draft's document_id is not registered."""


def selected_reference_documents(
    registry: DocumentRegistry,
    *,
    draft_id: str,
    reference_document_ids: Optional[Iterable[str]] = None,
    company: Optional[str] = None,
) -> List[Any]:
    """Resolve the fact base's documents, and make the choice inspectable.

    Two rules, in order:

    1. The caller's explicit list wins, always.
    2. Otherwise every other registered filing that produced facts — narrowed to
       the draft's own company when the draft declares one. Without that filter a
       draft about company A would be checked against company B's figures too, and
       "the number is elsewhere in the pile" is not a defensible verdict. The
       narrowing is reported in ``coverage.reference_documents``, so which filings
       were used is never a mystery.
    """
    if reference_document_ids is not None:
        picked = []
        for document_id in reference_document_ids:
            document = registry.get(str(document_id))
            if document is not None:
                picked.append(document)
        return picked

    candidates = [
        document for document in registry.list()
        if document.document_id != draft_id
        and document.document_type in REFERENCE_TYPES
        and registry.get_facts(document.document_id)
    ]
    if company:
        same_company = [item for item in candidates if item.company == company]
        if same_company:
            return same_company
    return candidates


def _reference_material(
    registry: DocumentRegistry, documents: Sequence[Any]
) -> tuple:
    """``(normalized facts, issues)` for every reference document."""
    normalized: List[object] = []
    issues: List[str] = []
    for document in documents:
        facts = registry.get_facts(document.document_id)
        if not facts:
            continue
        try:
            result = normalize_financials(facts, document)
        except Exception as exc:  # one bad filing must not sink the whole check
            issues.append(
                f"参考文档 {document.filename or document.document_id} 规范化失败："
                f"{type(exc).__name__}: {exc}"
            )
            continue
        normalized.extend(result.facts)
        for warning in result.warnings:
            issues.append(f"{document.filename or document.document_id}：{warning}")
    return normalized, issues


def _build_evidence_index(
    registry: DocumentRegistry,
    documents: Sequence[Any],
    index: FactIndex,
    injected_documents: Sequence[Any] = (),
) -> Dict[str, Dict[str, Any]]:
    """``fact_id -> its document and page``, across **both** id spaces.

    Findings cite evidence from two layers, and CARD-05 §10 requires every one of
    them to resolve to a document *and a page*:

    * CARD-02 normalized facts (``nf_…``) — what the figure rules compare against;
    * CARD-01 extracted facts (``fact_…``) — what CARD-04's scope signals carry as
      ``evidence_ids``, and what the ``block_yoy`` finding republishes.

    Measured failure without the second layer: ``draft_05``'s ``scope_error`` cited
    ``fact_bad948ce81b8`` and the §10 validator could not resolve it, because the
    index only held normalized ids. Publishing only one layer would have made a
    legitimate finding look unsourced — or, worse, taught the validator to accept an
    id space it cannot actually resolve.

    ``layer`` is recorded per entry so a consumer never has to guess which id space
    it is holding.
    """
    entries: Dict[str, Dict[str, Any]] = {}
    for document in list(documents) + list(injected_documents):
        document_id = str(getattr(document, "document_id", "") or "")
        if not document_id:
            continue
        for fact in registry.get_facts(document_id):
            fact_id = str(getattr(fact, "fact_id", "") or "")
            if not fact_id:
                continue
            entries[fact_id] = {
                "document_id": document_id,
                "page": getattr(fact, "page", None),
                "kind": str(getattr(fact, "kind", "") or ""),
                "table": getattr(fact, "table", None),
                "row": getattr(fact, "row", None),
                "column": getattr(fact, "column", None),
                "layer": "extracted",
            }
    for item in index.disclosures:
        entries[item.fact_id] = {
            "document_id": item.document_id,
            "page": item.page,
            "kind": "normalized",
            "table": item.source_table,
            "row": None,
            "column": None,
            "metric": item.metric,
            "period_key": item.period_key,
            "scope": item.scope,
            "version": item.version,
            "unit": item.unit,
            "available_at": item.available_at,
            "layer": "normalized",
        }
    return entries


def reference_material(
    registry: DocumentRegistry, documents: Sequence[Any]
) -> tuple:
    """``(normalized facts, issues)`` — the fact base a check compares against.

    Public because :func:`check_draft` returns a *report*, not the material it used,
    and a caller that needs to re-run the rule engine over a different claim set
    against the same disclosures (CARD-26's mutation suite) has no other way to get
    it. Exposing it is additive: :func:`check_draft` keeps using the same helper, so
    there is still exactly one normalization path.
    """
    return _reference_material(registry, documents)


def reference_signals(registry: DocumentRegistry, documents: Sequence[Any]) -> tuple:
    """CARD-04 scope signals for every reference document (typed, not serialized)."""
    return _reference_signals(registry, documents)


def _reference_signals(registry: DocumentRegistry, documents: Sequence[Any]) -> tuple:
    """CARD-04 signals for every reference document.

    Uses ``detect_scope_signals`` (the typed API) rather than the serialized report:
    the checker reads ``comparability`` / ``periods_affected`` off each signal, and
    handing it dataclasses keeps that contract explicit. The checker still tolerates
    the dict form, because a caller may legitimately pass a report payload through.
    """
    signals: List[Any] = []
    for document in documents:
        facts = registry.get_facts(document.document_id)
        if not facts:
            continue
        try:
            signals.extend(detect_scope_signals(facts, [document]))
        except Exception:
            continue
    return signals


def coverage_of(
    *,
    extractor: str,
    references: Sequence[Any],
    index: FactIndex,
    as_of: Optional[str],
    signals: Sequence[Any],
    claims: Sequence[DraftClaim] = (),
) -> Dict[str, Any]:
    """§9's "检测覆盖范围声明": which rules were actually live for this run.

    A rule that could not run is listed with the reason. "No finding" from a rule
    that never executed is not evidence of a clean draft, and the payload has to
    say which is which.

    ``valuation_multiple_error`` is the one rule with two independent bases — a
    multiple that contradicts disclosed inputs, and a multiple borrowed from the
    wrong industry — so it reports both branches separately. Collapsing them into
    one ``ran`` would let "the recompute had no price to work with" read as "the
    industry branch found nothing either", which is exactly the confusion §9
    forbids.
    """
    metrics = index.metrics()
    has_price = any(metric in metrics for metric in ("raw_price",))
    has_shares = "diluted_shares" in metrics
    has_profit = "net_profit_parent" in metrics or "equity_parent" in metrics
    multi_period = len({item.period_key for item in index.disclosures}) > 1
    registered_ids = {str(getattr(item, "document_id", "") or "") for item in references}
    has_publication_date = any(getattr(item, "publication_date", None) for item in references)
    has_dated_extra = any(
        item.document_id not in registered_ids and item.available_at
        for item in index.disclosures
    )
    stale_ran = bool(as_of and (has_publication_date or has_dated_extra))

    recompute_ran = bool(has_price and has_shares and has_profit)
    recompute_reason = (
        None if recompute_ran
        else "缺少股价/总股本/归母指标，倍数不可复算（分析师目标倍数按 opinion 计数）"
    )
    industry_ran, industry_reason = industry_check_capability(claims)
    multiple_ran = bool(recompute_ran or industry_ran)
    multiple_reasons = [
        reason for reason in (recompute_reason, industry_reason) if reason
    ]

    rules: Dict[str, Dict[str, Any]] = {
        "numeric_error": {"ran": bool(metrics), "reason": None if metrics else "事实库为空"},
        "unit_error": {"ran": bool(metrics), "reason": None if metrics else "事实库为空"},
        "period_error": {
            "ran": multi_period,
            "reason": None if multi_period else "事实库只有单一期间，无法判定期间错配",
        },
        "scope_error": {
            "ran": True,
            "reason": None if signals else "本次未检测到口径信号（≠ 不存在口径变化）",
        },
        "calculation_error": {
            "ran": multi_period,
            "reason": None if multi_period else "事实库只有单一期间，无法复算同比",
        },
        "valuation_multiple_error": {
            "ran": multiple_ran,
            # ``reason`` explains the *rule*, so it is only set when neither basis
            # could run. Which basis was missing is named per branch below — mixing
            # "the recompute had no price" into the rule's own reason would read as
            # "the industry check did not run either".
            "reason": None if multiple_ran else "；".join(multiple_reasons),
            "branches": {
                "recompute_from_disclosure": {
                    "ran": recompute_ran,
                    "reason": recompute_reason,
                },
                "declared_industry_mismatch": {
                    "ran": industry_ran,
                    "reason": industry_reason,
                },
            },
        },
        "missing_citation": {"ran": True, "reason": None},
        "wrong_citation": {"ran": True, "reason": None},
        "unsupported_citation": {"ran": True, "reason": None},
        "wrong_page": {"ran": True, "reason": None},
        "stale_citation": {
            "ran": stale_ran,
            "reason": (
                None if stale_ran else
                "未提供草稿 as_of 时点，无法判定引用是否过期" if not as_of else
                "参考文档无可核验披露日；导入时间不能代替披露时间"
            ),
        },
        "revision_superseded": {"ran": True, "reason": None},
        "unsupported_causal_claim": {"ran": True, "reason": None},
    }
    return {
        "extractor": extractor,
        "reference_documents": [
            {
                "document_id": getattr(item, "document_id", ""),
                "filename": getattr(item, "filename", ""),
                "document_type": getattr(item, "document_type", ""),
            }
            for item in references
        ],
        "fact_count": len(index.disclosures),
        "metrics_available": metrics,
        "as_of": as_of,
        "rules": rules,
    }


def check_draft(
    document_id: str,
    *,
    registry: Optional[DocumentRegistry] = None,
    reference_document_ids: Optional[Iterable[str]] = None,
    as_of: Optional[str] = None,
    extra_facts: Sequence[object] = (),
    extra_documents: Sequence[object] = (),
) -> Dict[str, Any]:
    """Check one registered draft without a model (deterministic path).

    ``extra_facts`` / ``extra_documents`` supply reference material that is not a
    registered filing. In production the only such material is market data (a share
    price and a share count), which today arrives from a provider snapshot rather
    than through CARD-01 — and CARD-05's own corpus needs it to exercise the
    verifiable-multiple rule at all. Anything injected this way is listed in
    ``coverage.injected_facts``, so it is never mistaken for a parsed filing.
    """
    return _assemble(
        document_id,
        registry=registry,
        reference_document_ids=reference_document_ids,
        as_of=as_of,
        extractor=extract_claims_rules,
        extra_facts=extra_facts,
        extra_documents=extra_documents,
    )


async def acheck_draft(
    document_id: str,
    *,
    registry: Optional[DocumentRegistry] = None,
    reference_document_ids: Optional[Iterable[str]] = None,
    as_of: Optional[str] = None,
    llm_client: Optional[ClaimExtractionClient] = None,
    llm_batch_size: int = 3,
    extra_facts: Sequence[object] = (),
    extra_documents: Sequence[object] = (),
) -> Dict[str, Any]:
    """Check one registered draft, optionally through the LLM extractor.

    The extraction is the only awaitable step; everything after it is the same
    deterministic core the offline path uses, so the two routes cannot drift.
    """
    store = registry or DocumentRegistry()
    document = store.get(document_id)
    if document is None:
        raise DraftNotFound(document_id)
    sentences = sentences_from_facts(store.get_facts(document_id), document_id=document_id)
    extraction = await extract_claims(sentences, llm_client, batch_size=llm_batch_size)

    return _assemble(
        document_id,
        registry=store,
        reference_document_ids=reference_document_ids,
        as_of=as_of,
        extractor=None,
        sentences=sentences,
        precomputed=extraction,
        extra_facts=extra_facts,
        extra_documents=extra_documents,
    )


def _assemble(
    document_id: str,
    *,
    registry: Optional[DocumentRegistry],
    reference_document_ids: Optional[Iterable[str]],
    as_of: Optional[str],
    extractor,
    sentences: Optional[Sequence[DraftSentence]] = None,
    precomputed: Optional[ExtractionOutcome] = None,
    extra_facts: Sequence[object] = (),
    extra_documents: Sequence[object] = (),
) -> Dict[str, Any]:
    store = registry or DocumentRegistry()
    document = store.get(document_id)
    if document is None:
        raise DraftNotFound(document_id)

    if sentences is None:
        sentences = sentences_from_facts(store.get_facts(document_id), document_id=document_id)
    extraction = precomputed if precomputed is not None else extractor(sentences)

    references = selected_reference_documents(
        store,
        draft_id=document_id,
        reference_document_ids=reference_document_ids,
        company=document.company,
    )
    # Injected material is *added* to the documents the citation resolver knows
    # about, and kept in its own list so the coverage payload can name it.
    resolver_documents = list(references) + [item for item in extra_documents]
    normalized, issues = _reference_material(store, references)
    normalized.extend(extra_facts)
    index = FactIndex(normalized)
    reference_index = ReferenceIndex.build(normalized, resolver_documents)
    signals = _reference_signals(store, references)

    findings = check_report(
        extraction.claims, normalized, signals,
        documents=resolver_documents, as_of=as_of, index=index, references=reference_index,
    )
    report = build_report(
        extraction.claims,
        findings,
        document_id=document_id,
        sentences=sentences,
        unextracted=len(extraction.unextracted),
        no_claim=len(extraction.no_claim),
        extraction_notes=list(extraction.notes),
        coverage=coverage_of(
            extractor=extraction.extractor, references=references,
            index=index, as_of=as_of, signals=signals,
            claims=extraction.claims,
        ),
    )
    report["coverage"]["injected_facts"] = {
        "count": len(extra_facts),
        "documents": [
            {
                "document_id": getattr(item, "document_id", ""),
                "filename": getattr(item, "filename", ""),
                "synthetic": True,
            }
            for item in extra_documents
        ],
    }
    report["draft"] = {
        "document_id": document_id,
        "filename": document.filename,
        "company": document.company,
        "document_type": document.document_type,
    }
    # §10 rule 1 and §14 need the same thing: ``evidence_ids`` must resolve to a
    # document *and a page*, because "click a finding -> open the page it accuses
    # you of misquoting" is the demo. Without this the report's evidence ids are
    # opaque strings and neither the validator nor the UI can resolve them, so the
    # report carries its own index and is therefore self-validating.
    evidence_index = _build_evidence_index(store, references, index, extra_documents)
    report["evidence_index"] = evidence_index
    report["evidence_summary"] = {
        "count": len(evidence_index),
        "located": sum(1 for item in evidence_index.values() if item.get("page") is not None),
        "normalized": len(index.disclosures),
        "extracted": sum(
            1 for item in evidence_index.values() if item.get("layer") == "extracted"
        ),
    }
    report["reference_issues"] = issues
    report["unextracted_sentences"] = [item.to_dict() for item in extraction.unextracted]
    report["no_claim_sentences"] = [item.to_dict() for item in extraction.no_claim]
    report["scope_signals"] = [
        signal.to_dict() if hasattr(signal, "to_dict") else signal for signal in signals
    ]
    return report


def sample_expected(report: Dict[str, Any], claim_id: str) -> Optional[str]:
    """Convenience for the UI: the expected value attached to a claim, if any."""
    for finding in report.get("findings", ()):
        if finding.get("claim_id") == claim_id:
            return finding.get("expected")
    return None


__all__ = [
    "DraftNotFound",
    "REFERENCE_TYPES",
    "acheck_draft",
    "check_draft",
    "coverage_of",
    "format_amount",
    "reference_material",
    "reference_signals",
    "selected_reference_documents",
    "sample_expected",
    "EXTRACTOR_RULES",
    "FactIndex",
    "DraftClaim",
]
