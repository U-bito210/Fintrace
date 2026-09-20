"""The CARD-05 planted-error draft corpus (committed fixtures, byte-reproducible).

§11 asks for "含 planted errors 的草稿集" and §15 A asks that all thirteen error
classes be detected at 100% on it. That is only a meaningful test if the *expected*
findings are written down independently of the checker — so they are authored here
by hand, next to the draft text that plants them, and the test asserts the checker
reproduces them. Nothing in this module runs the checker.

**Synthetic.** Every sentence, company and figure here was written for this corpus.
No real research report and no copyrighted filing is reproduced. Real drafts belong
in ``data/`` (git-ignored).

Byte-reproducibility follows CARD-01's rule exactly: ``fixtures_builder`` pins
``rl_config.invariant``, and :func:`verify_corpus` re-renders **from the spec** and
compares sha256, rather than re-reading the file it just wrote (which would pass
unconditionally).

The corpus also covers the negative direction, which is where a checker earns its
keep: two drafts contain nothing wrong, including one that quotes *both* columns of
a two-column statement — the case that a naive one-value-per-(metric, period) index
reports as a fabrication.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..documents.fixtures_builder import build_report_draft_lines

SCHEMA_VERSION = 1

DISCLAIMER = (
    "Synthetic research-draft fixtures for checker evaluation only. Not real "
    "research, not sourced financial facts, and must never be presented as either."
)

#: Reference filings the drafts cite, by fixture filename. These live in
#: ``tests/fixtures/documents`` (CARD-01's corpus) so the checker is exercised
#: against the same parsed facts the rest of the system sees.
ANNUAL_04 = "annual_04_restated_comparative.pdf"     # 示例制造股份有限公司, restatement + 双列调整表
ANNUAL_05 = "annual_05_reclassification.pdf"         # 示例材料股份有限公司, block_yoy 重分类
ANNUAL_01 = "annual_01_profit_continued.pdf"         # 示例股份有限公司, 本期/累计 双列
ANNOUNCEMENT_04 = "announcement_04_correction.pdf"   # 更正公告，无报表数值

#: A synthetic market-data filing supplying the inputs a PE/PB recompute needs.
#: The committed filings are income/balance statements and carry no share price, so
#: §7.1's "verifiable multiple" branch has no other way to be exercised. It is a
#: separate, clearly-named document rather than a figure smuggled into a filing.
MARKET_DOCUMENT = {
    "document_id": "SYN-MARKET-01",
    "filename": "synthetic_market_data.json",
    "company": "示例制造股份有限公司",
    "note": "合成市价/股本数据，仅供 checker 复算估值倍数使用；非真实行情。",
}

#: Normalized-fact rows for the market document. Values chosen so the recomputed PE
#: is exactly 10.0x: 1.10 元 × 10,000,000 股 ÷ 1,100,000 元 = 10.0.
MARKET_FACTS: Tuple[Dict[str, Any], ...] = (
    {"metric": "raw_price", "value": 1.10, "source_unit": "元", "period_key": "2025FY",
     "period_kind": "POINT_IN_TIME", "scope": "合并", "page": 1, "row": 1, "column": 1,
     "source_table": "市价"},
    {"metric": "diluted_shares", "value": 10_000_000.0, "source_unit": "股", "period_key": "2025FY",
     "period_kind": "POINT_IN_TIME", "scope": "合并", "page": 1, "row": 2, "column": 1,
     "source_table": "市价"},
    {"metric": "net_profit_parent", "value": 1_100_000.0, "source_unit": "元", "period_key": "2025FY",
     "period_kind": "YTD_CUMULATIVE", "scope": "合并", "page": 1, "row": 3, "column": 1,
     "source_table": "市价"},
    # 母公司 revenue sits BELOW the 合并 revenue so that a draft quoting the
    # consolidated figure as if it were the parent-company one is a scope error and
    # not a coincidence.
    {"metric": "revenue", "value": 600_000.0, "source_unit": "元", "period_key": "2025FY",
     "period_kind": "YTD_CUMULATIVE", "scope": "合并", "page": 1, "row": 4, "column": 1,
     "source_table": "市价"},
    {"metric": "revenue", "value": 550_000.0, "source_unit": "元", "period_key": "2025FY",
     "period_kind": "YTD_CUMULATIVE", "scope": "母公司", "page": 1, "row": 4, "column": 2,
     "source_table": "市价"},
)


@dataclass(frozen=True)
class ExpectedFinding:
    """One hand-authored expectation, matched by (check type, claim index)."""

    check_type: str
    claim_index: int          # 0-based index of the *claim* (not the sentence)
    severity: str = "error"
    expected_contains: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_type": self.check_type,
            "claim_index": self.claim_index,
            "severity": self.severity,
            "expected_contains": self.expected_contains,
            "note": self.note,
        }


@dataclass(frozen=True)
class DraftSpec:
    """One planted draft: its text, its reference set, and what should be found."""

    doc_id: str
    filename: str
    title: str
    lines: Tuple[str, ...]
    references: Tuple[str, ...]
    note: str
    as_of: Optional[str] = None
    expected: Tuple[ExpectedFinding, ...] = ()
    expected_claims: int = 0
    page_break_after: Optional[int] = None
    uses_market_document: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.doc_id,
            "filename": self.filename,
            "title": self.title,
            "note": self.note,
            "as_of": self.as_of,
            "references": list(self.references),
            "uses_market_document": self.uses_market_document,
            "lines": list(self.lines),
            "expected_claims": self.expected_claims,
            "expected": [item.to_dict() for item in self.expected],
        }


# --------------------------------------------------------------------------- #
# the corpus
# --------------------------------------------------------------------------- #

DRAFT_SPECS: Tuple[DraftSpec, ...] = (
    DraftSpec(
        doc_id="SYN-DRAFT-CLEAN-01",
        filename="draft_01_clean_summary.pdf",
        title="示例制造股份有限公司 2025 年度经营情况摘要（草稿）",
        note="好草稿：全部数值与引用均正确，用于零误报基线。",
        references=(ANNUAL_04,),
        as_of="2026-05-01",
        expected_claims=2,
        lines=(
            "公司2025年实现营业收入110万元，同比增长10.0%。（来源：annual_04_restated_comparative.pdf#p1）",
            "营业成本为70万元，同比下降2.78%。（来源：annual_04_restated_comparative.pdf#p1）",
            "上述财务数据均取自公司2025年年度报告。",
            "风险提示：行业竞争加剧、原材料价格波动。",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-CLEAN-02",
        filename="draft_02_clean_two_column.pdf",
        title="示例股份有限公司 2025 年度主要财务数据（草稿）",
        note=(
            "好草稿，但引用了双列表的两列（本报告期 1,234,567 与 年初至本报告期末 2,000,000），"
            "两列都规范化到同一 (metric, period)。把「同期间多列」误判成造假的实现会在这里误报。"
        ),
        references=(ANNUAL_01,),
        as_of="2026-05-01",
        expected_claims=3,
        lines=(
            "公司2025年营业收入为123.46万元。（来源：annual_01_profit_continued.pdf#p1）",
            "其中年初至报告期末累计营业收入为200万元。（来源：annual_01_profit_continued.pdf#p1）",
            "归母净利润为33万元。（来源：annual_01_profit_continued.pdf#p2）",
            "以上数据未经审计，仅供内部讨论。",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-FIGURES",
        filename="draft_03_planted_figures.pdf",
        title="示例制造股份有限公司 2025 年度业绩点评（草稿，含植入错误）",
        note="植入 数值／单位／期间／重述／复算 五类错误。第二页用于验证 finding 的草稿页码。",
        references=(ANNUAL_04,),
        as_of="2026-05-01",
        expected_claims=5,
        page_break_after=3,
        expected=(
            ExpectedFinding("numeric_error", 0, "error", "1,100,000元",
                            "草稿写 150 万元，披露 110 万元。"),
            ExpectedFinding("unit_error", 1, "error", "700,000元",
                            "草稿写 700,000 万元，量级差 10^4；数字对、单位错，不得判 numeric_error。"),
            ExpectedFinding("period_error", 2, "error", "2025FY",
                            "2024 年列的数值被写成 2024 年，但 110 万元是 2025FY 的披露。"),
            ExpectedFinding("revision_superseded", 3, "error", "1,000,000元",
                            "引用了第 3 页「调整前」列的 90 万元，已被同表「调整后」列取代。"),
            ExpectedFinding("calculation_error", 4, "error", "+10.00%",
                            "草稿称同比 +25%，按披露复算为 +10%。"),
        ),
        lines=(
            "公司2025年实现营业收入150万元。（来源：annual_04_restated_comparative.pdf#p1）",
            "公司2025年营业成本为700,000万元。（来源：annual_04_restated_comparative.pdf#p1）",
            "公司2024年营业收入为110万元。（来源：annual_04_restated_comparative.pdf#p1）",
            "公司2025年营业收入为90万元。（来源：annual_04_restated_comparative.pdf#p3）",
            "公司2025年营业收入同比增长25%。（来源：annual_04_restated_comparative.pdf#p1）",
            "本页结论仅基于公开披露，不构成投资建议。",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-CITATIONS",
        filename="draft_04_planted_citations.pdf",
        title="示例制造股份有限公司 2025 年度跟踪报告（草稿，含引用错误）",
        note="植入 缺引用／引用不存在／引用对象错／页码错／无证据因果 五类。",
        references=(ANNUAL_04, ANNOUNCEMENT_04),
        as_of="2026-05-01",
        expected_claims=5,
        expected=(
            ExpectedFinding("missing_citation", 0, "warning", "需给出可核验的披露来源",
                            "factual 数值句没有任何出处。"),
            ExpectedFinding("unsupported_citation", 1, "error", "已注册文档",
                            "引用的文档在本系统内不存在。"),
            ExpectedFinding("wrong_citation", 2, "error", "营业收入",
                            "更正公告不含任何报表数值，引用它作为营收出处是引用错对象。"),
            ExpectedFinding("wrong_page", 3, "error", "第1页、第3页",
                            "营收披露在第 1、3 页，草稿标注第 9 页。"),
            ExpectedFinding("unsupported_causal_claim", 4, "warning", "独立证据",
                            "因果断言只给出一个来源，两侧无法各自取证。"),
        ),
        lines=(
            "公司2025年营业收入为110万元。",
            "公司2025年营业收入为110万元。（来源：不存在的年报.pdf#p1）",
            "公司2025年营业收入为110万元。（来源：announcement_04_correction.pdf#p1）",
            "公司2025年营业收入为110万元。（来源：annual_04_restated_comparative.pdf#p9）",
            "受原材料价格上涨推动，2025年营业收入下降。（来源：annual_04_restated_comparative.pdf#p1）",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-SCOPE",
        filename="draft_05_planted_scope.pdf",
        title="示例材料股份有限公司 2025 年度业绩点评（草稿，含口径问题）",
        note=(
            "CARD-04 判定 block_yoy：出现同比百分比即为 scope_error；"
            "同一份草稿里的水平值仍然允许（误报控制的边界）。"
        ),
        references=(ANNUAL_05,),
        as_of="2026-05-01",
        expected_claims=2,
        expected=(
            ExpectedFinding("scope_error", 0, "error", "不得给出同比",
                            "重分类披露下两端不可比，百分比不得出具。"),
        ),
        lines=(
            "公司2025年营业收入同比增长11.1%。（来源：annual_05_reclassification.pdf#p1）",
            "公司2025年营业收入为100万元。（来源：annual_05_reclassification.pdf#p1）",
            "公司已披露重分类事项，同比口径存在变化。",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-STALE",
        filename="draft_06_planted_stale.pdf",
        title="示例股份有限公司 2025 年度快评（草稿，含引用时点问题）",
        note="as_of=2026-04-01：annual_01（2026-03-28 公开）可用，annual_04（2026-04-25 公开）尚不可得。",
        references=(ANNUAL_01, ANNUAL_04),
        as_of="2026-04-01",
        expected_claims=2,
        expected=(
            ExpectedFinding("stale_citation", 1, "warning", "尚未公开",
                            "草稿时点引用了当时还未公开的年报。"),
        ),
        lines=(
            "公司2025年营业收入为123.46万元。（来源：annual_01_profit_continued.pdf#p1）",
            "公司2025年营业收入为110万元。（来源：annual_04_restated_comparative.pdf#p1）",
        ),
    ),
    DraftSpec(
        doc_id="SYN-DRAFT-VALUATION",
        filename="draft_07_planted_valuation.pdf",
        title="示例制造股份有限公司 估值与口径点评（草稿，含估值与口径错误）",
        note="植入 估值倍数错 与 口径错配（母公司／合并）两类；依赖合成市价文档。",
        references=(ANNUAL_04, MARKET_DOCUMENT["filename"]),
        uses_market_document=True,
        as_of="2026-05-01",
        expected_claims=3,
        expected=(
            ExpectedFinding("valuation_multiple_error", 0, "error", "10.0x",
                            "以披露股价 1.10 元、总股本 10,000,000 股、归母净利润 1,100,000 元"
                            "复算，PE 应为 10.0 倍。"),
            ExpectedFinding("scope_error", 1, "error", "母公司",
                            "草稿称母公司口径 60 万元，该数值实为合并口径；母公司口径为 55 万元。"),
        ),
        lines=(
            "公司当前市盈率为50倍。（来源：SYN-MARKET-01#p1）",
            "公司2025年母公司营业收入为60万元。（来源：SYN-MARKET-01#p1）",
            "公司2025年合并营业收入为60万元。（来源：SYN-MARKET-01#p1）",
            "目标价与目标倍数由分析师独立判断，不属于事实核查范围。",
        ),
    ),
)


def corpus() -> List[DraftSpec]:
    """The committed corpus, in a stable order."""
    return list(DRAFT_SPECS)


def spec_by_filename(filename: str) -> Optional[DraftSpec]:
    for spec in DRAFT_SPECS:
        if spec.filename == filename:
            return spec
    return None


def render(spec: DraftSpec) -> bytes:
    """Render one draft from its spec — the single definition of its bytes."""
    return build_report_draft_lines(
        title=spec.title,
        lines=list(spec.lines),
        page_break_after=spec.page_break_after,
    )


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def market_documents() -> List[Any]:
    """The synthetic market-data filing as a :class:`DocumentRecord` stub."""
    from ..documents.models import DocumentRecord

    return [
        DocumentRecord(
            document_id=MARKET_DOCUMENT["document_id"],
            sha256="0" * 64,
            filename=MARKET_DOCUMENT["filename"],
            source="admin_import",
            document_type="other",
            company=MARKET_DOCUMENT["company"],
            report_period="2025FY",
            notes=[MARKET_DOCUMENT["note"]],
        )
    ]


def market_normalized_facts() -> List[Any]:
    """``MARKET_FACTS`` inflated into CARD-02 :class:`NormalizedFact` objects.

    The card's §7.1 valuation rule can only be *tested* if the inputs a PE recompute
    needs exist, and no committed filing carries a share price. These are declared
    as synthetic in the manifest and reported in the check payload rather than
    quietly mixed into the fact base.
    """
    from ..normalization.models import NormalizedFact, PeriodKind, Scope, Version

    facts: List[Any] = []
    for index, row in enumerate(MARKET_FACTS):
        scope_token = row.get("scope")
        facts.append(NormalizedFact(
            normalized_fact_id=f"nf_{MARKET_DOCUMENT['document_id']}_{index:02d}",
            metric=row["metric"],
            value=float(row["value"]),
            currency="CNY",
            period_key=row["period_key"],
            period_kind=PeriodKind(row["period_kind"]) if row.get("period_kind") else None,
            scope=Scope.coerce(scope_token) if scope_token else None,
            version=Version.AS_REPORTED,
            available_at="2026-04-30",
            source_fact_id=f"fact_{MARKET_DOCUMENT['document_id']}_{index:02d}",
            document_id=MARKET_DOCUMENT["document_id"],
            page=row.get("page"),
            row=row.get("row"),
            column=row.get("column"),
            raw_value=str(row["value"]),
            source_unit=row.get("source_unit"),
            source_table=row.get("source_table"),
        ))
    return facts


def default_fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "report_drafts"


def write_corpus(out_dir: str | Path) -> Dict[str, Any]:
    """Write the draft PDFs, the gold files and the manifest."""
    out = Path(out_dir)
    gold_dir = out / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)

    documents: List[Dict[str, Any]] = []
    for spec in DRAFT_SPECS:
        data = render(spec)
        (out / spec.filename).write_bytes(data)
        (gold_dir / f"{spec.doc_id}.json").write_text(
            json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        documents.append({
            "document_id": spec.doc_id,
            "filename": spec.filename,
            "document_type": "report_draft",
            "sha256": sha256_of(data),
            "bytes": len(data),
            "note": spec.note,
            "references": list(spec.references),
            "as_of": spec.as_of,
            "expected_claims": spec.expected_claims,
            "expected_findings": len(spec.expected),
        })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_on": "2026-09-18",
        "generator": "scripts/gen_report_draft_fixtures.py",
        "synthetic": True,
        "disclaimer": DISCLAIMER,
        "market_document": MARKET_DOCUMENT,
        "market_facts": list(MARKET_FACTS),
        "reference_filings": {
            "annual_01_profit_continued.pdf": ANNUAL_01,
            "annual_04_restated_comparative.pdf": ANNUAL_04,
            "annual_05_reclassification.pdf": ANNUAL_05,
            "announcement_04_correction.pdf": ANNOUNCEMENT_04,
        },
        "drafts": documents,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def verify_corpus(out_dir: str | Path) -> Dict[str, Any]:
    """Re-render every draft **from the spec** and compare to the manifest.

    Re-rendering rather than reading the file the writer just produced is the whole
    point: a comparison against an on-disk file that was written moments earlier
    passes unconditionally and can never notice a non-reproducible renderer.
    """
    out = Path(out_dir)
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "checked": 0, "mismatches": [], "file_mismatches": [],
                "gold_mismatches": [], "missing": ["manifest.json"]}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches: List[str] = []
    file_mismatches: List[str] = []
    gold_mismatches: List[str] = []
    missing: List[str] = []

    for entry in manifest.get("drafts", []):
        filename = entry["filename"]
        spec = spec_by_filename(filename)
        if spec is None:
            mismatches.append(f"{filename}（清单中的草稿已不在 corpus() 声明中）")
            continue
        if sha256_of(render(spec)) != entry["sha256"]:
            mismatches.append(filename)
        path = out / filename
        if not path.exists():
            missing.append(filename)
        elif sha256_of(path.read_bytes()) != entry["sha256"]:
            file_mismatches.append(filename)

        gold_path = out / "gold" / f"{spec.doc_id}.json"
        if not gold_path.exists():
            missing.append(f"gold/{spec.doc_id}.json")
        elif json.loads(gold_path.read_text(encoding="utf-8")) != spec.to_dict():
            gold_mismatches.append(spec.doc_id)

    ok = not (mismatches or file_mismatches or gold_mismatches or missing)
    return {
        "ok": ok,
        "checked": len(manifest.get("drafts", [])),
        "mismatches": mismatches,
        "file_mismatches": file_mismatches,
        "gold_mismatches": gold_mismatches,
        "missing": missing,
    }


__all__ = [
    "ANNUAL_01",
    "ANNUAL_04",
    "ANNUAL_05",
    "ANNOUNCEMENT_04",
    "DISCLAIMER",
    "DRAFT_SPECS",
    "DraftSpec",
    "ExpectedFinding",
    "MARKET_DOCUMENT",
    "MARKET_FACTS",
    "SCHEMA_VERSION",
    "corpus",
    "default_fixtures_dir",
    "render",
    "sha256_of",
    "spec_by_filename",
    "verify_corpus",
    "write_corpus",
]
