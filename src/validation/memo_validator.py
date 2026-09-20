"""CARD-10 §10 — validator for the investment memo.

The memo is the system's user-facing deliverable, so its validator checks the
things a reader would otherwise have to take on trust:

1. **Sentence-level marking is complete.** A rendered sentence is either
   *structural* scaffolding (``structural=True``, no claim type) or a tiered
   assertion (``fact`` / ``inference`` / ``opinion``). ``claim_type=None``
   without ``structural`` is an unmarked sentence and an error — that is exactly
   the gap section-level marking left open in v2.
2. **Every ``fact`` sentence binds evidence; every numeric assertion binds a
   calculation or evidence.** A number with no trail is an error, never a
   warning: the memo must not be able to print a figure it cannot justify.
3. **Bound ids resolve.** A ``claim_id`` that is not in the graph, or a
   ``calculation_id`` absent from the graph's calculations, is an error — a
   memo that cites a non-existent node has a provenance hole.
4. **Narrative quote citations are verbatim.** When a CARD-06 retrieval hit is
   supplied, the quoted text must match it character for character.
5. **Falsification is computable.** The falsification section must carry a
   threshold; "持续关注基本面" style filler is rejected.

Following the project convention, *incomplete input yields a warning, never a
silent pass*: without a graph the referential checks are reported as skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
)
from src.cn.memo import (
    SECTION_FALSIFICATION,
    InvestmentMemo,
    MemoSection,
    MemoSentence,
)

_TIER_TYPES = (CLAIM_FACT, CLAIM_INFERENCE, CLAIM_OPINION)

#: Substrings that make a falsification condition computable rather than a wish.
_FALSIFICATION_MARKERS = ("阈值", "上限", "跌破", "高于", "低于", "%")


@dataclass(frozen=True)
class MemoValidation:
    """Outcome of validating one memo."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_memo(
    memo: InvestmentMemo,
    *,
    graph: Any = None,
    hits: Optional[Sequence[Any]] = None,
    calculation_ids: Optional[Iterable[str]] = None,
) -> MemoValidation:
    """Validate one memo.

    ``graph`` enables the claim/evidence reference checks; ``calculation_ids``
    adds calculation ids from *another* registry to the known set — the
    valuation section legitimately cites CARD-08's calculations, which are not
    nodes of the CARD-09 graph.
    """
    errors: List[str] = []
    warnings: List[str] = []

    _check_marking_complete(memo, errors)
    _check_numeric_binding(memo, errors)
    _check_references(memo, errors, warnings, graph=graph, calculation_ids=calculation_ids)
    _check_quote_verbatim(memo, errors, hits=hits)
    _check_falsification_computable(memo, errors)

    return MemoValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_memo_corpus(
    memos: Iterable[InvestmentMemo],
    *,
    graph: Any = None,
    calculation_ids: Optional[Iterable[str]] = None,
) -> MemoValidation:
    """Validate several memos as one gate (used by the corpus/benchmark run)."""
    errors: List[str] = []
    warnings: List[str] = []
    seen = 0
    for memo in memos:
        seen += 1
        result = validate_memo(memo, graph=graph, calculation_ids=calculation_ids)
        prefix = f"{memo.memo_id}: "
        errors.extend(prefix + item for item in result.errors)
        warnings.extend(prefix + item for item in result.warnings)
    if seen == 0:
        warnings.append("no_memos_supplied")
    return MemoValidation(valid=not errors, errors=errors, warnings=warnings)


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def _sentences(memo: InvestmentMemo) -> List[MemoSentence]:
    return [sentence for section in memo.sections for sentence in section.sentences]


def _check_marking_complete(memo: InvestmentMemo, errors: List[str]) -> None:
    for section in memo.sections:
        for index, sentence in enumerate(section.sentences):
            where = f"{section.section_id}[{index}]"
            if sentence.structural:
                if sentence.claim_type is not None:
                    errors.append(f"{where}: structural sentence carries claim_type")
                continue
            if sentence.claim_type is None:
                errors.append(f"{where}: unmarked sentence")
                continue
            if sentence.claim_type not in _TIER_TYPES:
                errors.append(f"{where}: unknown claim_type {sentence.claim_type!r}")
                continue
            if sentence.claim_type == CLAIM_FACT and not sentence.claim_id:
                errors.append(f"{where}: fact sentence without a claim_id")
            if sentence.claim_type == CLAIM_INFERENCE and not sentence.derived_from:
                errors.append(f"{where}: inference sentence without derived_from")
            if sentence.claim_type == CLAIM_OPINION and not (
                sentence.assumption_note or ""
            ).strip():
                errors.append(f"{where}: opinion sentence without an assumption_note")


def _check_numeric_binding(memo: InvestmentMemo, errors: List[str]) -> None:
    for section in memo.sections:
        for index, sentence in enumerate(section.sentences):
            if sentence.structural or not sentence.numeric:
                continue
            if not (sentence.calculation_id or sentence.evidence_ids):
                errors.append(
                    f"{section.section_id}[{index}]: numeric sentence without a "
                    f"calculation_id or evidence — {sentence.sentence_span}"
                )


def _check_references(
    memo: InvestmentMemo,
    errors: List[str],
    warnings: List[str],
    *,
    graph: Any,
    calculation_ids: Optional[Iterable[str]],
) -> None:
    known_calculations = {str(item) for item in (calculation_ids or ())}
    if graph is not None:
        known_calculations |= {
            str(node.calculation_id)
            for node in (getattr(graph, "calculations", {}) or {}).values()
        }
    if graph is None:
        warnings.append("no_graph_reference_check_skipped")
    if not known_calculations:
        warnings.append("no_calculation_registry_reference_check_skipped")

    claim_ids = set(getattr(graph, "claims", {}) or {}) if graph is not None else None
    evidence_ids = set(getattr(graph, "evidence_index", {}) or {}) if graph is not None else None

    for section in memo.sections:
        for index, sentence in enumerate(section.sentences):
            where = f"{section.section_id}[{index}]"
            if sentence.claim_id and claim_ids is not None and sentence.claim_id not in claim_ids:
                errors.append(f"{where}: claim_id {sentence.claim_id!r} does not resolve")
            if (
                sentence.calculation_id
                and known_calculations
                and sentence.calculation_id not in known_calculations
            ):
                errors.append(
                    f"{where}: calculation_id {sentence.calculation_id!r} does not resolve"
                )
            if evidence_ids:
                for evidence_id in sentence.evidence_ids:
                    if evidence_id not in evidence_ids:
                        warnings.append(
                            f"{where}: evidence {evidence_id!r} is not in the graph index"
                        )


def _check_quote_verbatim(
    memo: InvestmentMemo, errors: List[str], *, hits: Optional[Sequence[Any]]
) -> None:
    if not hits:
        return
    by_chunk = {
        str(getattr(hit, "chunk_id", "")): str(getattr(hit, "text", "") or "")
        for hit in hits
    }
    for section in memo.sections:
        for index, sentence in enumerate(section.sentences):
            for citation in sentence.citations:
                if str(citation.get("mode")) != "quote":
                    continue
                chunk_id = str(citation.get("chunk_id", ""))
                quote = str(citation.get("quote", ""))
                source = by_chunk.get(chunk_id)
                if source is None:
                    errors.append(
                        f"{section.section_id}[{index}]: quote cites unknown chunk {chunk_id!r}"
                    )
                elif quote and quote not in source:
                    errors.append(
                        f"{section.section_id}[{index}]: quote is not verbatim for chunk {chunk_id!r}"
                    )


def _check_falsification_computable(memo: InvestmentMemo, errors: List[str]) -> None:
    sections = [item for item in memo.sections if item.section_id == SECTION_FALSIFICATION]
    if not sections:
        errors.append("falsification section is missing")
        return
    section: MemoSection = sections[0]
    if section.data_status != "ok":
        # No computable threshold could be produced, and the section says so.
        return
    if not section.sentences:
        errors.append("falsification section is empty")
        return
    for index, sentence in enumerate(section.sentences):
        if not any(marker in sentence.sentence_span for marker in _FALSIFICATION_MARKERS):
            errors.append(
                f"falsification[{index}]: no computable threshold — {sentence.sentence_span}"
            )
