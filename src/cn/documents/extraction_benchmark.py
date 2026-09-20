"""Extraction P/R/F1 evaluator for the Document Intelligence Layer.

CARD-01 §13 requires the synthetic-corpus extraction baseline to be measured,
not asserted: target values stay unfrozen until the baseline exists. This module
produces that baseline and feeds CARD-11.

**What is measured.** CARD-01 deliberately emits one ``table_cell`` fact per
cell (so every figure keeps its own bbox and can be jumped back to). A gold
record is therefore a *located figure*: ``(metric, value, page)``. An extracted
figure is a ``(label, value, page)`` triple formed by pairing the label cell of a
table row with each numeric cell in that same row.

* **TP** — a gold figure found in the extraction.
* **FN** — a gold figure the parser missed.
* **FP** — an extracted figure that is not in the gold truth (a fabricated or
  mislocated number — the failure mode CARD-01 exists to prevent).

Matching is deliberately strict on value and page, so a mislocated number counts
as a false positive *and* a false negative, and cannot be hidden by a lenient
match. Announcement / research-draft samples carry no numeric gold truth; they are
scored for ingest-and-locate only (see :func:`evaluate_corpus`), and their figures
are excluded from the P/R/F1 denominator rather than being counted as FPs.

Everything here runs offline and deterministically against ``tests/fixtures/documents``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import ExtractedFact, FACT_KIND_TABLE_CELL, sha256_bytes as sha256_of
from .registry import DocumentRegistry
from .synthetic_corpus import CorpusDocument, corpus, load_gold


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    cleaned = str(raw).replace(",", "").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_numeric_fact(fact: ExtractedFact) -> bool:
    return _to_float(fact.raw_value) is not None


@dataclass
class ExtractedFigure:
    """A (label, value, page) triple recoverable from the located facts."""

    label: str
    value: float
    page: Optional[int]
    table: Optional[str] = None
    row: Optional[int] = None
    column: Optional[int] = None
    column_kind: Optional[str] = None

    def key(self) -> Tuple[str, float, Optional[int]]:
        return (self.label, self.value, self.page)


def extracted_figures(facts: Sequence[ExtractedFact]) -> List[ExtractedFigure]:
    """Pair each table row's label cell with its numeric cells.

    The label is the first non-numeric ``table_cell`` in the row; every numeric
    cell in that row becomes a candidate figure. Rows without a label or without
    a numeric cell produce nothing (they cannot be a located figure).
    """
    rows: Dict[Tuple[str, Optional[str], Optional[int]], List[ExtractedFact]] = {}
    for fact in facts:
        if fact.kind != FACT_KIND_TABLE_CELL:
            continue
        key = (fact.document_id, fact.table, fact.row)
        rows.setdefault(key, []).append(fact)

    figures: List[ExtractedFigure] = []
    for (_doc, table, row), cells in rows.items():
        cells_sorted = sorted(cells, key=lambda c: (c.column if c.column is not None else -1))
        labels = [c for c in cells_sorted if not _is_numeric_fact(c) and c.raw_text.strip()]
        if not labels:
            continue
        label_fact = labels[0]
        for cell in cells_sorted:
            value = _to_float(cell.raw_value)
            if value is None:
                continue
            figures.append(ExtractedFigure(
                label=label_fact.raw_text.strip(),
                value=value,
                page=cell.page,
                table=table,
                row=row,
                column=cell.column,
                column_kind=cell.meta.get("column_label") if isinstance(cell.meta, dict) else None,
            ))
    return figures


@dataclass
class DocumentScore:
    document_id: str
    filename: str
    document_type: str
    status: str
    fact_count: int
    gold_count: int
    true_positive: int
    false_positive: int
    false_negative: int
    missing_metrics: List[str] = field(default_factory=list)
    unexpected: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def precision(self) -> Optional[float]:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else None

    @property
    def recall(self) -> Optional[float]:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else None

    @property
    def f1(self) -> Optional[float]:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def to_dict(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "document_type": self.document_type,
            "status": self.status,
            "fact_count": self.fact_count,
            "gold_records": self.gold_count,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "missing_metrics": self.missing_metrics,
            "unexpected": self.unexpected,
            "errors": self.errors,
        }


@dataclass
class CorpusScore:
    documents: List[DocumentScore] = field(default_factory=list)
    ingest_ok: int = 0
    ingest_total: int = 0
    locate_ok: int = 0
    locate_total: int = 0
    page_attribution_ok: int = 0
    page_attribution_total: int = 0
    label_recall_ok: int = 0
    label_recall_total: int = 0
    scale_ok: int = 0
    scale_total: int = 0
    status_matches: int = 0
    status_total: int = 0
    scan_page_ok: int = 0
    scan_page_total: int = 0
    injection_ok: int = 0
    injection_total: int = 0
    degraded_ok: int = 0
    degraded_total: int = 0

    # -- aggregate metrics -------------------------------------------------

    def _totals(self, docs: Iterable[DocumentScore]) -> Tuple[int, int, int]:
        tp = sum(d.true_positive for d in docs)
        fp = sum(d.false_positive for d in docs)
        fn = sum(d.false_negative for d in docs)
        return tp, fp, fn

    def micro(self) -> Dict[str, Optional[float]]:
        """Micro-averaged over every gold figure in the corpus."""
        scored = [d for d in self.documents if d.gold_count]
        tp, fp, fn = self._totals(scored)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        f1 = None
        if precision is not None and recall is not None and (precision + recall):
            f1 = 2 * precision * recall / (precision + recall)
        return {
            "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": precision, "recall": recall, "f1": f1,
        }

    def macro(self) -> Dict[str, Optional[float]]:
        """Macro-averaged over documents that carry gold truth."""
        scored = [d for d in self.documents if d.gold_count]
        if not scored:
            return {"precision": None, "recall": None, "f1": None, "documents": 0}
        precisions = [d.precision for d in scored if d.precision is not None]
        recalls = [d.recall for d in scored if d.recall is not None]
        f1s = [d.f1 for d in scored if d.f1 is not None]
        mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
        return {
            "precision": mean(precisions),
            "recall": mean(recalls),
            "f1": mean(f1s),
            "documents": len(scored),
        }

    def to_dict(self) -> Dict[str, object]:
        return {
            "documents_scored": len(self.documents),
            "extraction": {"micro": self.micro(), "macro": self.macro()},
            "ingest_success_rate": _ratio(self.ingest_ok, self.ingest_total),
            "location_coverage": _ratio(self.locate_ok, self.locate_total),
            "bbox_coverage": _ratio(self.scale_ok, self.scale_total),
            "label_recall": _ratio(self.label_recall_ok, self.label_recall_total),
            "page_attribution_accuracy": _ratio(self.page_attribution_ok, self.page_attribution_total),
            "document_status_accuracy": _ratio(self.status_matches, self.status_total),
            "scan_page_handling_accuracy": _ratio(self.scan_page_ok, self.scan_page_total),
            "injection_detection_accuracy": _ratio(self.injection_ok, self.injection_total),
            "degraded_labelling_accuracy": _ratio(self.degraded_ok, self.degraded_total),
            "per_document": [d.to_dict() for d in self.documents],
        }


def _ratio(ok: int, total: int) -> Optional[float]:
    return (ok / total) if total else None


def evaluate_document(
    doc: CorpusDocument,
    registry: DocumentRegistry,
    path: Path,
    gold: Sequence[Dict[str, object]],
) -> Tuple[DocumentScore, List[ExtractedFact], List]:
    """Ingest one corpus document and score its located figures against gold.

    Returns ``(score, facts, events)`` so the caller can compute corpus-level
    location / attribution metrics without re-ingesting.
    """
    score = DocumentScore(
        document_id=doc.doc_id, filename=doc.filename,
        document_type=doc.document_type, status="", fact_count=0, gold_count=len(gold),
        true_positive=0, false_positive=0, false_negative=0,
    )
    try:
        result = registry.ingest(path, declared_type=doc.document_type)
    except Exception as exc:
        score.errors.append(f"ingest 失败: {exc!r}")
        return score, [], []

    score.status = result.document.status
    score.fact_count = len(result.facts)

    figures = extracted_figures(result.facts)
    extracted = {fig.key(): fig for fig in figures}

    # Match gold records (metric, value, page) against extracted figures. Value
    # and page are both required, so a mislocated number is simultaneously an FP
    # and an FN and cannot be masked by a lenient match.
    unmatched_gold: List[Dict[str, object]] = []
    matched_keys = set()
    for rec in gold:
        key = (str(rec["metric"]), float(rec["expected_value"]), rec.get("page"))
        if key in extracted and key not in matched_keys:
            matched_keys.add(key)
            score.true_positive += 1
        else:
            unmatched_gold.append(rec)
    score.false_negative = len(unmatched_gold)
    score.missing_metrics = [str(r["metric"]) for r in unmatched_gold]
    score.false_positive = max(0, len(extracted) - len(matched_keys))
    score.unexpected = [
        f"{fig.label}={fig.value}@p{fig.page}"
        for key, fig in extracted.items() if key not in matched_keys
    ]
    return score, result.facts, result.events


def evaluate_corpus(
    fixtures_dir: str | Path,
    registry_root: str | Path,
    *,
    documents: Optional[Sequence[CorpusDocument]] = None,
) -> CorpusScore:
    """Run the whole corpus through the registry and aggregate every metric.

    Fixtures are staged into the registry's own data tree first: the registry
    enforces a workspace allowlist (Controlled Environment Guard), so files are
    only ever ingested from an allowed root.
    """
    fixtures = Path(fixtures_dir)
    registry = DocumentRegistry(root=registry_root)
    try:
        return _evaluate_with_registry(registry, fixtures, documents)
    finally:
        # Windows keeps the SQLite handle locked otherwise, which breaks temp-dir
        # cleanup for the caller.
        registry.close()


def _evaluate_with_registry(
    registry: DocumentRegistry,
    fixtures: Path,
    documents: Optional[Sequence[CorpusDocument]],
) -> CorpusScore:
    staging = registry.root / "corpus"
    staging.mkdir(parents=True, exist_ok=True)

    score = CorpusScore()
    for doc in (documents if documents is not None else corpus()):
        source = fixtures / doc.filename
        if not source.exists():
            score.documents.append(DocumentScore(
                document_id=doc.doc_id, filename=doc.filename, document_type=doc.document_type,
                status="", fact_count=0, gold_count=len(doc.gold),
                true_positive=0, false_positive=0, false_negative=0,
                errors=[f"fixture 缺失: {source}；请先运行 scripts/gen_document_fixtures.py"],
            ))
            continue
        staged = staging / doc.filename
        staged.write_bytes(source.read_bytes())

        gold_payload = load_gold(fixtures, doc.doc_id)
        gold_records_list = gold_payload.get("records", [])
        doc_score, facts, events = evaluate_document(doc, registry, staged, gold_records_list)
        score.documents.append(doc_score)

        # -- ingest / locate / bbox coverage --
        score.ingest_total += 1
        if doc_score.status and doc_score.status != "error":
            score.ingest_ok += 1

        score.locate_total += len(facts)
        score.locate_ok += sum(1 for f in facts if f.page is not None)

        cells = [f for f in facts if f.kind == FACT_KIND_TABLE_CELL]
        score.scale_total += len(cells)
        score.scale_ok += sum(1 for f in cells if f.bbox)

        # -- label recall vs page attribution (only where gold exists) --
        # A label is "recalled" if its metric text appears as a cell label in the
        # document. Among recalled labels, attribution is correct when the matched
        # value lands on the expected page — this separates "not found" from
        # "found on the wrong page", which strict P/R/F1 alone cannot show.
        if gold_records_list:
            labels = {f.raw_text.strip() for f in cells if not _is_numeric_fact(f)}
            figures_by_label: Dict[str, List[ExtractedFigure]] = {}
            for fig in extracted_figures(facts):
                figures_by_label.setdefault(fig.label, []).append(fig)
            for rec in gold_records_list:
                metric = str(rec["metric"])
                score.label_recall_total += 1
                if metric in labels:
                    score.label_recall_ok += 1
                candidates = figures_by_label.get(metric, [])
                if not candidates:
                    continue
                score.page_attribution_total += 1
                if any(fig.page == rec.get("page") for fig in candidates):
                    score.page_attribution_ok += 1

        # -- declared vs actual document status (covers degraded/partial paths) --
        score.status_total += 1
        if doc_score.status == doc.expect_status:
            score.status_matches += 1

        # -- a scan page must fail, carry no facts, and never be silently empty --
        if doc.expect_scan_page is not None:
            score.scan_page_total += 1
            page_events = [e for e in events if e.page == doc.expect_scan_page]
            page_has_facts = any(f.page == doc.expect_scan_page for f in facts)
            if page_events and all(e.status in ("failed", "partial") for e in page_events) and not page_has_facts:
                score.scan_page_ok += 1

        # -- the planted injection must be flagged; clean samples must not be --
        score.injection_total += 1
        if bool(doc_score.status) and (
            registry.list(document_type=doc.document_type)
            and _flagged(registry, staged) == doc.expect_injection
        ):
            score.injection_ok += 1

        # -- a missing disclosure date must be labelled on the facts (card §7) --
        if doc.expect_facts_degraded:
            score.degraded_total += 1
            if facts and all(
                f.status == "partial" and f.meta.get("degraded") for f in facts
            ):
                score.degraded_ok += 1

    return score


def _flagged(registry: DocumentRegistry, path: Path) -> bool:
    sha = sha256_of(path.read_bytes()) if path.exists() else None
    record = registry.find_by_sha256(sha) if sha else None
    return bool(record.suspected_injection) if record else False


def format_markdown(score: CorpusScore, *, title: str = "CARD-01 文档抽取基准（合成集）") -> str:
    """Human-readable report; the JSON form is what CARD-11 consumes."""
    micro = score.micro()
    macro = score.macro()
    lines = [
        f"# {title}",
        "",
        "> 合成样例集（非真实披露）。P/R/F1 为解析器基线数字，",
        "> 目标值待 baseline 后再冻结（CARD-01 §13 / CARD-11 §7 阈值纪律）。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 文档数 | {len(score.documents)} |",
        f"| 提取 Precision (micro) | {_fmt(micro['precision'])} |",
        f"| 提取 Recall (micro) | {_fmt(micro['recall'])} |",
        f"| 提取 F1 (micro) | {_fmt(micro['f1'])} |",
        f"| 提取 Precision (macro) | {_fmt(macro['precision'])} |",
        f"| 提取 Recall (macro) | {_fmt(macro['recall'])} |",
        f"| 提取 F1 (macro) | {_fmt(macro['f1'])} |",
        f"| TP / FP / FN | {micro['true_positive']} / {micro['false_positive']} / {micro['false_negative']} |",
        f"| ingest 成功率 | {_fmt(_ratio(score.ingest_ok, score.ingest_total))} |",
        f"| 定位覆盖率 (page 非空) | {_fmt(_ratio(score.locate_ok, score.locate_total))} |",
        f"| bbox 覆盖率 (表格单元格) | {_fmt(_ratio(score.scale_ok, score.scale_total))} |",
        f"| 指标标签召回率 | {_fmt(_ratio(score.label_recall_ok, score.label_recall_total))} |",
        f"| 页码归属准确率 | {_fmt(_ratio(score.page_attribution_ok, score.page_attribution_total))} |",
        f"| 文档状态判定准确率 | {_fmt(_ratio(score.status_matches, score.status_total))} |",
        f"| 扫描页处理准确率 | {_fmt(_ratio(score.scan_page_ok, score.scan_page_total))} |",
        f"| 注入判定准确率 | {_fmt(_ratio(score.injection_ok, score.injection_total))} |",
        f"| 缺失日期 degraded 标注准确率 | {_fmt(_ratio(score.degraded_ok, score.degraded_total))} |",
        "",
        "## 逐文档",
        "",
        "| document_id | 类型 | 状态 | facts | gold | TP | FP | FN | P | R | F1 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in score.documents:
        lines.append(
            f"| {d.document_id} | {d.document_type} | {d.status} | {d.fact_count} | {d.gold_count} "
            f"| {d.true_positive} | {d.false_positive} | {d.false_negative} "
            f"| {_fmt(d.precision)} | {_fmt(d.recall)} | {_fmt(d.f1)} |"
        )
    misses = [(d.document_id, m) for d in score.documents for m in d.missing_metrics]
    if misses:
        lines += ["", "## 未命中（FN）明细", ""]
        lines += [f"- {doc_id}: {metric}" for doc_id, metric in misses]
    return "\n".join(lines) + "\n"


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value <= 1.0:
        return f"{value:.4f}"
    return str(value)


def write_report(score: CorpusScore, out_dir: str | Path) -> Dict[str, Path]:
    """Write the JSON (CARD-11 input) and markdown (human) baseline reports."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "document_extraction_baseline.json"
    md_path = out / "document_extraction_baseline.md"
    payload = {
        "suite": "cn_agent_v2",
        "set": "synthetic",
        "component": "CARD-01 document extraction",
        "scoring": "field-level located figures (metric, value, page)",
        "synthetic": True,
        "disclaimer": "Synthetic fixtures only; no real filing data. Not a real-model benchmark.",
        "metrics": score.to_dict(),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(format_markdown(score), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def default_fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "documents"
