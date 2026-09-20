"""CARD-05 §13: Checker Precision / Recall / F1 / False-Positive-Rate.

The gold set is the hand-authored expectation in :mod:`src.cn.checker.draft_corpus`
— never the checker's own output — so the numbers below measure the checker rather
than restate it. Each expectation names a ``(check_type, claim_index)`` pair, which
keeps the match exact: "some numeric error was found somewhere in this draft" is not
the same claim as "this sentence's number is wrong".

**Synthetic set.** Planted-error corpora are the right instrument for precision and
recall on *known* defects, and the wrong instrument for claiming field accuracy on
real research. The report says which set it measured, and ``real_model`` is False
because no model was involved: extraction on this path is the deterministic rule
extractor, so the score describes the *checker*, exactly as CARD-05 §15 A splits it.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..documents.registry import DocumentRegistry
from .draft_corpus import (
    DraftSpec,
    default_fixtures_dir,
    market_documents,
    market_normalized_facts,
)
from .pipeline import check_draft

SUITE = "cn_agent_v2"
COMPONENT = "CARD-05 report claim checker"


@dataclass
class Score:
    """Confusion counts plus the four derived rates."""

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    drafts: int = 0
    claims: int = 0
    details: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 1.0

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 2 * self.precision * self.recall / total if total else 0.0

    @property
    def false_positive_rate(self) -> float:
        denominator = self.false_positives + self.true_negatives
        return self.false_positives / denominator if denominator else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": SUITE,
            "set": "synthetic",
            "component": COMPONENT,
            "synthetic": True,
            "real_model": False,
            "extractor": "rules",
            "disclaimer": (
                "Synthetic planted-error set; the checker is scored against hand-authored "
                "expectations. Not a real-model or field-accuracy measurement."
            ),
            "drafts": self.drafts,
            "claims": self.claims,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "true_negatives": self.true_negatives,
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "f1": round(self.f1, 6),
            "false_positive_rate": round(self.false_positive_rate, 6),
            "details": list(self.details),
        }


def _documents_by_filename(registry: DocumentRegistry) -> Dict[str, str]:
    return {document.filename: document.document_id for document in registry.list()}


def load_reference_filings(
    registry: DocumentRegistry, fixtures_dir: Path, filenames: Sequence[str]
) -> Dict[str, str]:
    """Ingest the CARD-01 filings the drafts cite; return filename -> document_id."""
    for filename in filenames:
        if not filename:
            continue
        path = fixtures_dir / filename
        if not path.exists():
            continue
        declared = "annual" if filename.startswith("annual") else "announcement"
        registry.ingest(path.read_bytes(), declared, filename=filename, source="admin_import")
    return _documents_by_filename(registry)


def evaluate_corpus(
    drafts_dir: Optional[str | Path] = None,
    workdir: Optional[str | Path] = None,
    *,
    reference_dir: Optional[str | Path] = None,
    specs: Optional[Sequence[DraftSpec]] = None,
) -> Score:
    """Run the deterministic checker over the planted corpus and score it."""
    drafts_path = Path(drafts_dir or default_fixtures_dir())
    reference_path = Path(reference_dir or drafts_path.parent / "documents")
    # Never default the registry inside the committed fixtures directory: a stray
    # ``registry.db`` there would be an untracked artefact next to the corpus.
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="cn_checker_bench_"))

    registry = DocumentRegistry(root=work / "registry.db")
    try:
        from .draft_corpus import DRAFT_SPECS

        selected = list(specs or DRAFT_SPECS)
        reference_names = sorted({name for spec in selected for name in spec.references})
        reference_names = [name for name in reference_names if name.endswith(".pdf")]
        by_filename = load_reference_filings(registry, reference_path, reference_names)

        score = Score()
        for spec in selected:
            report = _check_spec(registry, drafts_path, spec, by_filename)
            score.drafts += 1
            score.claims += len(report["claims"])
            score.details.append(_score_draft(spec, report, score))
        return score
    finally:
        registry.close()


def _check_spec(
    registry: DocumentRegistry, drafts_path: Path, spec: DraftSpec, by_filename: Dict[str, str]
) -> Dict[str, Any]:
    data = (drafts_path / spec.filename).read_bytes()
    existing = _documents_by_filename(registry)
    document_id = existing.get(spec.filename)
    if document_id is None:
        result = registry.ingest(data, "report_draft", filename=spec.filename, source="admin_import")
        document_id = result.document.document_id

    reference_ids = [by_filename[name] for name in spec.references if name in by_filename]
    extra_facts = market_normalized_facts() if spec.uses_market_document else ()
    extra_documents = market_documents() if spec.uses_market_document else ()
    return check_draft(
        document_id,
        registry=registry,
        reference_document_ids=reference_ids,
        as_of=spec.as_of,
        extra_facts=extra_facts,
        extra_documents=extra_documents,
    )


def _score_draft(spec: DraftSpec, report: Dict[str, Any], score: Score) -> Dict[str, Any]:
    """Attribute findings to gold expectations and fold the counts into *score*."""
    claims = report.get("claims", [])
    claim_ids = [claim["claim_id"] for claim in claims]
    by_claim: Dict[str, List[str]] = {}
    for finding in report.get("findings", ()):
        by_claim.setdefault(finding["claim_id"], []).append(finding["check_type"])

    expected: List[Tuple[str, str]] = []
    for item in spec.expected:
        if item.claim_index >= len(claim_ids):
            continue
        expected.append((claim_ids[item.claim_index], item.check_type))

    matched_expected: List[Tuple[str, str]] = []
    matched_findings: List[Tuple[str, str]] = []
    for pair in expected:
        if pair[1] in by_claim.get(pair[0], ()):
            matched_expected.append(pair)
    for claim_id, check_types in by_claim.items():
        for check_type in check_types:
            if (claim_id, check_type) in expected:
                matched_findings.append((claim_id, check_type))

    missed = [pair for pair in expected if pair not in matched_expected]
    spurious = [
        (claim_id, check_type)
        for claim_id, check_types in by_claim.items()
        for check_type in check_types
        if (claim_id, check_type) not in expected
    ]

    # A claim with no gold expectation and no finding is a true negative; that is
    # what makes the two clean drafts load-bearing for the FPR.
    clean_claims = [
        claim_id for index, claim_id in enumerate(claim_ids)
        if not any(item.claim_index == index for item in spec.expected)
    ]
    true_negatives = len([cid for cid in clean_claims if not by_claim.get(cid)])

    score.true_positives += len(matched_expected)
    score.false_negatives += len(missed)
    score.false_positives += len(spurious)
    score.true_negatives += true_negatives

    return {
        "document_id": spec.doc_id,
        "filename": spec.filename,
        "claims": len(claim_ids),
        "findings": len(report.get("findings", ())),
        "expected": [{"claim_id": cid, "check_type": ct} for cid, ct in expected],
        "matched": [{"claim_id": cid, "check_type": ct} for cid, ct in matched_expected],
        "missed": [{"claim_id": cid, "check_type": ct} for cid, ct in missed],
        "spurious": [{"claim_id": cid, "check_type": ct} for cid, ct in spurious],
        "true_negatives": true_negatives,
        "summary": report.get("summary", {}),
    }


def format_markdown(score: Score) -> str:
    lines = [
        f"# {COMPONENT} 基准（synthetic）",
        "",
        "合成 planted 集，期望值由人工在 `src/cn/checker/draft_corpus.py` 中声明；"
        "本分数衡量**检查器**，不是真实研报现场准确率，也不是模型能力。",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 草稿数 | {score.drafts} |",
        f"| claim 数 | {score.claims} |",
        f"| Precision | {score.precision:.4f} |",
        f"| Recall | {score.recall:.4f} |",
        f"| F1 | {score.f1:.4f} |",
        f"| False Positive Rate | {score.false_positive_rate:.4f} |",
        f"| TP / FP / FN / TN | {score.true_positives} / {score.false_positives} / "
        f"{score.false_negatives} / {score.true_negatives} |",
        "",
        "| 草稿 | claims | findings | 期望 | 命中 | 漏报 | 误报 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in score.details:
        lines.append(
            f"| {item['filename']} | {item['claims']} | {item['findings']} | "
            f"{len(item['expected'])} | {len(item['matched'])} | "
            f"{len(item['missed'])} | {len(item['spurious'])} |"
        )
    missed = [(item["filename"], row["check_type"]) for item in score.details for row in item["missed"]]
    spurious = [(item["filename"], row["check_type"]) for item in score.details for row in item["spurious"]]
    if missed:
        lines += ["", "## 漏报", ""] + [f"- {name}: {check}" for name, check in missed]
    if spurious:
        lines += ["", "## 误报（需要先解释清楚再改规则）", ""] + [
            f"- {name}: {check}" for name, check in spurious
        ]
    return "\n".join(lines)


def write_report(score: Score, out_dir: str | Path) -> Dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "checker_baseline.json"
    md_path = out / "checker_baseline.md"
    json_path.write_text(
        json.dumps(score.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(format_markdown(score), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


__all__ = [
    "COMPONENT",
    "SUITE",
    "Score",
    "evaluate_corpus",
    "format_markdown",
    "load_reference_filings",
    "write_report",
]
