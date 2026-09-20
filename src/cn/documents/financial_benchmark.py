"""Offline CARD-02/03/04 benchmark on the synthetic document corpus.

This is a deliberately small, deterministic bridge from the document corpus to
the normalization, analysis, and accounting-scope layers.  It measures only
machine-checkable properties; it never treats synthetic numbers as real
financial data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .registry import DocumentRegistry
from .synthetic_corpus import CorpusDocument, corpus, load_gold
from ..analysis import analyze_document
from ..normalization import UNIT_SCALES, normalize_financials


@dataclass
class FinancialDocumentScore:
    document_id: str
    normalized_facts: int = 0
    unit_ok: int = 0
    unit_total: int = 0
    period_kind_ok: int = 0
    period_kind_total: int = 0
    scope_ok: int = 0
    scope_total: int = 0
    calculation_ok: int = 0
    calculation_total: int = 0
    scope_signal_expected: bool = False
    scope_signal_detected: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        ratio = lambda ok, total: ok / total if total else None
        return {
            "document_id": self.document_id,
            "normalized_facts": self.normalized_facts,
            "unit_accuracy": ratio(self.unit_ok, self.unit_total),
            "period_kind_accuracy": ratio(self.period_kind_ok, self.period_kind_total),
            "scope_accuracy": ratio(self.scope_ok, self.scope_total),
            "calculation_accuracy": ratio(self.calculation_ok, self.calculation_total),
            "scope_signal_expected": self.scope_signal_expected,
            "scope_signal_detected": self.scope_signal_detected,
            "errors": list(self.errors),
        }


@dataclass
class FinancialCorpusScore:
    documents: List[FinancialDocumentScore] = field(default_factory=list)

    def _sum(self, field_name: str) -> int:
        return sum(getattr(item, field_name) for item in self.documents)

    def to_dict(self) -> Dict[str, object]:
        ratio = lambda ok, total: ok / total if total else None
        signal_total = sum(item.scope_signal_expected for item in self.documents)
        signal_ok = sum(
            item.scope_signal_expected == item.scope_signal_detected
            for item in self.documents
            if item.scope_signal_expected or item.scope_signal_detected
        )
        return {
            "documents_scored": len(self.documents),
            "unit_accuracy": ratio(self._sum("unit_ok"), self._sum("unit_total")),
            "period_kind_accuracy": ratio(self._sum("period_kind_ok"), self._sum("period_kind_total")),
            "scope_accuracy": ratio(self._sum("scope_ok"), self._sum("scope_total")),
            "calculation_accuracy": ratio(self._sum("calculation_ok"), self._sum("calculation_total")),
            "scope_signal_detection_accuracy": ratio(signal_ok, len([
                item for item in self.documents if item.scope_signal_expected or item.scope_signal_detected
            ])),
            "per_document": [item.to_dict() for item in self.documents],
        }


def _gold_matches(normalized, record: dict) -> list:
    scale = UNIT_SCALES.get(str(record.get("unit") or "元"), 1.0)
    expected = float(record["expected_value"]) * scale
    return [
        fact for fact in normalized
        if fact.page == record.get("page") and math.isclose(fact.value, expected, rel_tol=0.0, abs_tol=1e-6)
    ]


def evaluate_financial_corpus(
    fixtures_dir: str | Path,
    registry_root: str | Path,
    *,
    documents: Optional[List[CorpusDocument]] = None,
) -> FinancialCorpusScore:
    """Measure CARD-02/03/04 on committed synthetic fixtures."""
    fixtures = Path(fixtures_dir)
    registry = DocumentRegistry(root=registry_root)
    result = FinancialCorpusScore()
    try:
        staging = registry.root / "corpus"
        staging.mkdir(parents=True, exist_ok=True)
        for doc in documents or corpus():
            score = FinancialDocumentScore(doc.doc_id)
            source = fixtures / doc.filename
            if not source.exists():
                score.errors.append(f"fixture missing: {source}")
                result.documents.append(score)
                continue
            staged = staging / doc.filename
            staged.write_bytes(source.read_bytes())
            try:
                parsed = registry.ingest(staged, declared_type=doc.document_type)
                normalization = normalize_financials(registry.get_facts(parsed.document.document_id), parsed.document)
                score.normalized_facts = len(normalization.facts)
                for record in load_gold(fixtures, doc.doc_id).get("records", []):
                    matches = _gold_matches(normalization.facts, record)
                    score.unit_total += 1
                    score.period_kind_total += 1
                    score.scope_total += 1
                    if matches:
                        fact = matches[0]
                        score.unit_ok += fact.source_unit == record.get("unit")
                        score.period_kind_ok += fact.period_kind is not None
                        score.scope_ok += fact.scope is None or fact.scope.value == record.get("scope")
                analysis = analyze_document(normalization, documents=parsed.document)
                for metric in analysis.metrics:
                    if metric.calculation_id:
                        score.calculation_total += 1
                        calc = analysis.registry.get(metric.calculation_id) if analysis.registry else None
                        score.calculation_ok += bool(calc and not analysis.registry.validate())
                score.scope_signal_expected = doc.doc_id in {"SYN-ANNUAL-04", "SYN-ANNUAL-05", "SYN-ANN-04"}
                score.scope_signal_detected = bool(analysis.scope_signals)
            except Exception as exc:  # report typed benchmark failure, never hide it
                score.errors.append(repr(exc))
            result.documents.append(score)
        return result
    finally:
        registry.close()


__all__ = ["FinancialDocumentScore", "FinancialCorpusScore", "evaluate_financial_corpus"]
