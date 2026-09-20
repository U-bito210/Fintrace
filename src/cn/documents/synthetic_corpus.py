"""Synthetic A-share document corpus for the CARD-01 extraction benchmark.

CARD-01 §13 requires a reproducible sample set under ``tests/fixtures/documents/``
(≥3 annual reports, ≥3 announcements, ≥3 research drafts, plus a scan-page
sample) with a committed generation script, so the extraction P/R/F1 baseline is
reproducible by anyone and no copyrighted filing enters Git.

The corpus is *declarative*: a spec both renders the PDF and derives the gold
truth, so the two can never drift. Gold records follow the CARD-11 §5 format
``{document_id, metric, period, expected_value, unit, scope, page}``.

No real filing content is used, and no number here is presented as a sourced
financial fact — these are fixtures for measuring the *parser*, labelled as such.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .fixtures_builder import (
    build_announcement_pdf,
    build_multi_page_report,
    build_report_draft_pdf,
    build_scan_page_pdf,
)
from .models import sha256_bytes as sha256_of

CORPUS_SCHEMA_VERSION = 1
CORPUS_DATE = "2026-09-17"

# Annual-report fixtures: the only documents carrying numeric gold truth.
# Column labels are the real A-share dual-column wording so edge case 8 is
# exercised on every sample.
DUAL_HEADER = ["项目", "本报告期", "年初至本报告期末"]
UNIT_YUAN = "单位：元"


@dataclass(frozen=True)
class GoldRecord:
    """One expected located figure (CARD-11 §5 truth format)."""

    metric: str
    expected_value: float
    page: int
    unit: str = "元"
    scope: Optional[str] = "合并"
    period: Optional[str] = None
    column_kind: Optional[str] = None   # dual-column label: current_period | ytd

    def to_dict(self, document_id: str) -> Dict[str, object]:
        return {
            "document_id": document_id,
            "metric": self.metric,
            "period": self.period,
            "expected_value": self.expected_value,
            "unit": self.unit,
            "scope": self.scope,
            "page": self.page,
        }


@dataclass(frozen=True)
class CorpusDocument:
    """A corpus member: stable id, declared type, builder, and gold truth."""

    doc_id: str
    filename: str
    document_type: str
    builder: Callable[[], bytes]
    gold: Tuple[GoldRecord, ...] = ()
    note: str = ""
    expect_status: str = "ok"
    expect_scan_page: Optional[int] = None
    expect_injection: bool = False
    expect_facts_degraded: bool = False

    def build(self) -> bytes:
        return self.builder()


def _annual_1() -> bytes:
    return build_multi_page_report(
        "示例股份有限公司", 2025,
        pages=[
            {
                "title": "合并利润表", "header": DUAL_HEADER,
                "rows": [
                    ["营业收入", "1,234,567", "2,000,000"],
                    ["营业成本", "800,000", "1,500,000"],
                    ["销售费用", "120,000", "110,000"],
                ],
            },
            {
                "continuation": True, "header": DUAL_HEADER,
                "rows": [
                    ["净利润", "345,678", "600,000"],
                    ["利润总额", "400,000", "700,000"],
                    ["归母净利润", "330,000", "580,000"],
                ],
            },
        ],
        pub_date="2026-03-28",
        unit_note=UNIT_YUAN,
    )


def _annual_2() -> bytes:
    """万元 unit, single page, publication date intentionally absent (missing field)."""
    return build_multi_page_report(
        "华南机械制造有限公司", 2024,
        pages=[
            {
                "title": "合并资产负债表", "header": ["项目", "期末余额", "期初余额"],
                "rows": [
                    ["货币资金", "12,500", "11,000"],
                    ["应收账款", "8,300", "7,900"],
                ],
            },
        ],
        unit_note="单位：万元",
    )


def _annual_3() -> bytes:
    """Three pages: cash-flow page + a normal page + a scanned page (no text layer)."""
    return build_multi_page_report(
        "北方能源集团股份有限公司", 2025,
        pages=[
            {
                "title": "合并现金流量表", "header": ["项目", "本期金额", "上期金额"],
                "rows": [
                    ["经营活动现金流入", "5,600,000", "4,900,000"],
                    ["经营活动现金流出", "4,100,000", "3,800,000"],
                ],
            },
            {
                "title": "合并现金流量表（续）", "header": ["项目", "本期金额", "上期金额"],
                "rows": [
                    ["投资活动现金流入", "900,000", "700,000"],
                    ["投资活动现金流出", "1,200,000", "1,050,000"],
                ],
            },
            {"scan": True},
        ],
        pub_date="2026-04-18",
        unit_note=UNIT_YUAN,
    )


def _announcement_1() -> bytes:
    return build_announcement_pdf(
        title="关于召开2026年第一次临时股东大会的通知",
        date="2026-03-15",
        body="各位股东：公司定于2026年3月30日召开临时股东大会，审议关于变更募集资金用途的议案。",
    )


def _announcement_2() -> bytes:
    """Carries an ordinary external link (audit-only, must not flag injection)."""
    return build_announcement_pdf(
        title="2025年年度业绩预告",
        date="2026-01-20",
        body="经财务部门初步测算，预计2025年度实现归属于上市公司股东的净利润同比增长约30%。",
        url="http://www.cninfo.com.cn/new/disclosure",
    )


def _announcement_3() -> bytes:
    """A planted prompt-injection attempt — must be flagged, never obeyed."""
    return build_announcement_pdf(
        title="关于公司高级管理人员变动的公告",
        date="2026-02-08",
        body="公司董事会近日收到副总经理的书面辞职报告。",
        inject=True,
    )


def _announcement_4() -> bytes:
    """更正公告 (CARD-04 detection surface).

    Carries the standard A-share correction wording in prose — 前期会计差错更正,
    追溯重述, 更正前/更正后 values. An announcement declares no report period, so
    these facts are never normalised; CARD-04 detects on the located ``text``
    facts, which is exactly the surface a correction announcement provides.
    """
    return build_announcement_pdf(
        title="关于前期会计差错更正的公告",
        date="2026-06-12",
        body=(
            "本公司于2026年6月12日召开董事会，审议通过《关于前期会计差错更正的议案》。"
            "因收入确认时点判断错误，本公司对2024年度财务报表进行追溯重述。"
            "更正前2024年度营业收入为900,000元，更正后为1,000,000元。"
        ),
    )


def _annual_4() -> bytes:
    """Restated annual report (CARD-04 §8 edge case 2).

    Three pages: a 合并利润表 whose prior-year column is the restated figure, the
    note chapters CARD-04 must read, and a 调整前/调整后 two-version table. The
    two versions of the prior-year figures coexist here on purpose — they land in
    the same metric+period, so only the column version label keeps them apart.

    The policy-change note is deliberately split across two sentences: one that
    declares the change, one that says the comparable period was adjusted. A
    single sentence claiming both would be reported under only the more specific
    rule, and the fixture is meant to exercise both.
    """
    return build_multi_page_report(
        "示例制造股份有限公司", 2025,
        pages=[
            {
                "title": "合并利润表",
                "header": ["项目", "本期金额", "上期金额（已重述）"],
                "rows": [
                    ["营业收入", "1,100,000", "1,000,000"],
                    ["营业成本", "700,000", "720,000"],
                ],
            },
            {
                "unit_note": False,
                "paragraphs": [
                    "五、重要会计政策及会计估计",
                    "本公司自2026年1月1日起执行新租赁准则。",
                    "按准则要求，本公司对可比期间数据进行了追溯调整。",
                    "六、前期差错更正",
                    "本公司对2024年度营业收入确认时点进行了更正，更正后2024年度营业收入为1,000,000元。",
                    "七、合并财务报表范围",
                    "报告期内本公司合并范围发生变化，新增纳入合并范围的子公司一家。",
                ],
            },
            {
                "title": "前期差错更正影响金额（2024年度）",
                "header": ["项目", "调整前金额", "调整后金额"],
                "rows": [
                    ["营业收入", "900,000", "1,000,000"],
                    ["营业成本", "700,000", "720,000"],
                ],
            },
        ],
        pub_date="2026-04-25",
        unit_note=UNIT_YUAN,
    )


def _annual_5() -> bytes:
    """A filing whose only 口径 disclosure is an unclassifiable one.

    CARD-04 §7.1 says a suspicious paragraph outside the keyword table becomes an
    ``unknown`` signal plus a review hint, and §9 says unknown blocks the
    comparison. This document exists so the *block* path can be exercised on a
    filing that is otherwise perfectly ordinary — a plain statement with a
    matching prior-year column, so the only reason no percentage may be produced
    is the disclosure itself.
    """
    return build_multi_page_report(
        "示例材料股份有限公司", 2025,
        pages=[
            {
                "title": "合并利润表",
                "header": ["项目", "本期金额", "上期金额"],
                "rows": [
                    ["营业收入", "1,000,000", "900,000"],
                    ["营业成本", "650,000", "600,000"],
                ],
            },
            {
                "unit_note": False,
                "paragraphs": [
                    "八、其他重要事项",
                    "本公司对部分报表项目进行了重新分类列报，该事项不涉及金额调整。",
                ],
            },
        ],
        pub_date="2026-04-26",
        unit_note=UNIT_YUAN,
    )


def _annual_6() -> bytes:
    """Annual report carrying the two inputs CARD-08 was missing: 期末总股本 and
    两期归母权益.

    Feasibility check before CARD-08 started: ``annual_01``…``annual_05`` carry
    only income-statement lines, so no EPS/BPS driver could be built offline (and
    ``SYN-ANNUAL-01`` keeps that "股本缺失 → 估值阻断" path deliberately).  This
    filing adds a 合并资产负债表 with both balance columns and a 合并利润表 with
    both income columns, so the full CARD-01 → CARD-02 → CARD-08 chain runs on a
    really-ingested PDF: 归母净利润 3,600,000 / 期末总股本 4,000,000 → EPS 0.9,
    归母权益 30,000,000 → BPS 7.5, 期初 26,000,000 → ROE ≈ 12.86%.

    Labels stay short on purpose: a 12-character line does not fit the 120 pt
    first column, reportlab wraps it, and pdfplumber then reads
    ``归属于母公司股东权益合计30,000,000`` as one cell — label and number fuse
    and the metric is lost.  ``归母净资产`` / ``归母净利润`` are the §7.1
    dictionary's own short synonyms and therefore stay on one line.

    The two 期末总股本 columns deliberately differ (4,000,000 / 3,800,000): gold
    truth is matched on ``(metric, value, page)`` and de-duplicated, so two
    identical figures in one table would score as a false negative no matter how
    well the parser did.
    """
    return build_multi_page_report(
        "示例医药股份有限公司", 2025,
        pages=[
            {
                "title": "合并资产负债表",
                "header": ["项目", "期末余额", "期初余额"],
                "rows": [
                    ["期末总股本", "4,000,000", "3,800,000"],
                    ["归母净资产", "30,000,000", "26,000,000"],
                    ["资产总计", "58,000,000", "52,000,000"],
                ],
            },
            {
                "title": "合并利润表",
                "header": ["项目", "本期金额", "上期金额"],
                "rows": [
                    ["营业收入", "20,000,000", "17,000,000"],
                    ["营业成本", "11,000,000", "9,500,000"],
                    ["归母净利润", "3,600,000", "2,800,000"],
                ],
            },
        ],
        pub_date="2026-04-20",
        unit_note=UNIT_YUAN,
    )


def _annual_7() -> bytes:
    """Two planted constraint violations, one per family (CARD-22 §16).

    CARD-22 needs a filing where the system is *demonstrably* right about being
    wrong, and it needs both a finding and a clean column, otherwise the detector
    could pass by flagging everything.

    * **F2 小计勾稽 (期末 column).** 流动资产合计 6,000,000 + 非流动资产合计
      4,000,000 = 10,000,000, but 资产总计 prints 11,000,000. A component row lost
      a digit — the classic OCR / 单元格串行 failure this family exists to catch.
    * **F2 期初 column is consistent** (5,500,000 + 3,900,000 = 9,400,000 =
      资产总计), which is the true-negative half of the same check.
    * **F1 passes on both columns and that is deliberate**: 负债合计 + 归母净资产
      equals the printed 资产总计 (11,000,000 / 9,400,000), so the obvious balance
      equation sees nothing. The subtotal check catches what the headline identity
      cannot — the strongest available demonstration that these are *six* families
      and not one.
    * **F6 跨模态 (page 2).** The prose says 毛利率 "下降 1.2 个百分点" while the
      income table computes 33.0% → 37.06% = −4.06pct. The level (33.0%) matches,
      so the mismatch is specifically in the change figure.

    Labels stay short for the same reason as ``annual_06``: a long label wraps,
    pdfplumber fuses label and number into one cell, and the row is lost.
    """
    return build_multi_page_report(
        "示例环保股份有限公司", 2025,
        pages=[
            {
                "title": "合并资产负债表",
                "header": ["项目", "期末余额", "期初余额"],
                "rows": [
                    ["流动资产合计", "6,000,000", "5,500,000"],
                    ["非流动资产合计", "4,000,000", "3,900,000"],
                    ["资产总计", "11,000,000", "9,400,000"],
                    ["负债合计", "6,000,000", "5,400,000"],
                    ["归母净资产", "5,000,000", "4,000,000"],
                ],
            },
            {
                "title": "合并利润表",
                "header": ["项目", "本期金额", "上期金额"],
                "rows": [
                    ["营业收入", "20,000,000", "17,000,000"],
                    ["营业成本", "13,400,000", "10,700,000"],
                    ["归母净利润", "3,600,000", "2,800,000"],
                ],
                "paragraphs": [
                    "报告期内公司毛利率为 33.0%，较上年同期下降 1.2 个百分点。",
                ],
            },
        ],
        pub_date="2026-04-25",
        unit_note=UNIT_YUAN,
    )


def _draft_1() -> bytes:
    return build_report_draft_pdf([
        "摘要：公司营收稳健增长，毛利率同比改善。",
        "基本面：经营活动现金流为正，资产负债率处于可控区间。",
        "风险提示：行业竞争加剧，原材料价格波动。",
    ])


def _draft_2() -> bytes:
    return build_report_draft_pdf([
        "核心结论：维持对该公司基本面的中性判断。",
        "估值：当前市盈率处于近三年中位数附近。",
        "数据说明：本文所引数据均需回溯原始披露页核对。",
    ])


def _draft_3() -> bytes:
    return build_report_draft_pdf([
        "行业观察：上游资本开支放缓。",
        "公司层面：产能利用率环比持平。",
        "待核实：管理层指引尚无正式公告支撑。",
    ])


def corpus() -> List[CorpusDocument]:
    """The committed synthetic corpus, in a stable order.

    Gold truth enumerates *every* figure in every table column, so a false
    positive means a genuinely fabricated or mislocated number rather than a
    column the annotation happened to skip.
    """
    return [
        CorpusDocument(
            doc_id="SYN-ANNUAL-01", filename="annual_01_profit_continued.pdf",
            document_type="annual", builder=_annual_1, note="双列 + 续表合并",
            gold=(
                GoldRecord("营业收入", 1234567.0, 1, column_kind="current_period"),
                GoldRecord("营业收入", 2000000.0, 1, column_kind="ytd"),
                GoldRecord("营业成本", 800000.0, 1, column_kind="current_period"),
                GoldRecord("营业成本", 1500000.0, 1, column_kind="ytd"),
                GoldRecord("销售费用", 120000.0, 1, column_kind="current_period"),
                GoldRecord("销售费用", 110000.0, 1, column_kind="ytd"),
                GoldRecord("净利润", 345678.0, 2, column_kind="current_period"),
                GoldRecord("净利润", 600000.0, 2, column_kind="ytd"),
                GoldRecord("利润总额", 400000.0, 2, column_kind="current_period"),
                GoldRecord("利润总额", 700000.0, 2, column_kind="ytd"),
                GoldRecord("归母净利润", 330000.0, 2, column_kind="current_period"),
                GoldRecord("归母净利润", 580000.0, 2, column_kind="ytd"),
            ),
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-02", filename="annual_02_balance_unit_wan.pdf",
            document_type="annual", builder=_annual_2, note="万元单位 + 缺失发布日期",
            gold=(
                GoldRecord("货币资金", 12500.0, 1, unit="万元"),
                GoldRecord("货币资金", 11000.0, 1, unit="万元"),
                GoldRecord("应收账款", 8300.0, 1, unit="万元"),
                GoldRecord("应收账款", 7900.0, 1, unit="万元"),
            ),
            # Parsing fully succeeds (status ok) but available_at is inferred, so
            # the facts — not the document — carry the degraded label (card §7).
            expect_facts_degraded=True,
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-03", filename="annual_03_cashflow_with_scan.pdf",
            document_type="annual", builder=_annual_3, note="多页 + 扫描页降级",
            gold=(
                GoldRecord("经营活动现金流入", 5600000.0, 1),
                GoldRecord("经营活动现金流入", 4900000.0, 1),
                GoldRecord("经营活动现金流出", 4100000.0, 1),
                GoldRecord("经营活动现金流出", 3800000.0, 1),
                GoldRecord("投资活动现金流入", 900000.0, 2),
                GoldRecord("投资活动现金流入", 700000.0, 2),
                GoldRecord("投资活动现金流出", 1200000.0, 2),
                GoldRecord("投资活动现金流出", 1050000.0, 2),
            ),
            expect_status="partial", expect_scan_page=3,
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-04", filename="annual_04_restated_comparative.pdf",
            document_type="annual", builder=_annual_4,
            note="重述：上期列已重述 + 附注章节 + 调整前/调整后双列表",
            gold=(
                GoldRecord("营业收入", 1100000.0, 1),
                GoldRecord("营业收入", 1000000.0, 1),
                GoldRecord("营业成本", 700000.0, 1),
                GoldRecord("营业成本", 720000.0, 1),
                GoldRecord("营业收入", 900000.0, 3),
                GoldRecord("营业收入", 1000000.0, 3),
                GoldRecord("营业成本", 700000.0, 3),
                GoldRecord("营业成本", 720000.0, 3),
            ),
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-05", filename="annual_05_reclassification.pdf",
            document_type="annual", builder=_annual_5,
            note="口径不可判定：重分类披露（block_yoy 路径）",
            gold=(
                GoldRecord("营业收入", 1000000.0, 1),
                GoldRecord("营业收入", 900000.0, 1),
                GoldRecord("营业成本", 650000.0, 1),
                GoldRecord("营业成本", 600000.0, 1),
            ),
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-06", filename="annual_06_equity_shares.pdf",
            document_type="annual", builder=_annual_6,
            note="股本 + 两期归母权益（CARD-08 每股估值来源）",
            gold=(
                GoldRecord("期末总股本", 4000000.0, 1),
                GoldRecord("期末总股本", 3800000.0, 1),
                GoldRecord("归母净资产", 30000000.0, 1),
                GoldRecord("归母净资产", 26000000.0, 1),
                GoldRecord("资产总计", 58000000.0, 1),
                GoldRecord("资产总计", 52000000.0, 1),
                GoldRecord("营业收入", 20000000.0, 2),
                GoldRecord("营业收入", 17000000.0, 2),
                GoldRecord("营业成本", 11000000.0, 2),
                GoldRecord("营业成本", 9500000.0, 2),
                GoldRecord("归母净利润", 3600000.0, 2),
                GoldRecord("归母净利润", 2800000.0, 2),
            ),
        ),
        CorpusDocument(
            doc_id="SYN-ANNUAL-07", filename="annual_07_acme_constraint_break.pdf",
            document_type="annual", builder=_annual_7,
            note="植入约束违规：小计勾稽(期末) + 正文/表格毛利率变动不一致",
            gold=(
                GoldRecord("流动资产合计", 6000000.0, 1),
                GoldRecord("流动资产合计", 5500000.0, 1),
                GoldRecord("非流动资产合计", 4000000.0, 1),
                GoldRecord("非流动资产合计", 3900000.0, 1),
                GoldRecord("资产总计", 11000000.0, 1),
                GoldRecord("资产总计", 9400000.0, 1),
                GoldRecord("负债合计", 6000000.0, 1),
                GoldRecord("负债合计", 5400000.0, 1),
                GoldRecord("归母净资产", 5000000.0, 1),
                GoldRecord("归母净资产", 4000000.0, 1),
                GoldRecord("营业收入", 20000000.0, 2),
                GoldRecord("营业收入", 17000000.0, 2),
                GoldRecord("营业成本", 13400000.0, 2),
                GoldRecord("营业成本", 10700000.0, 2),
                GoldRecord("归母净利润", 3600000.0, 2),
                GoldRecord("归母净利润", 2800000.0, 2),
            ),
        ),
        CorpusDocument(
            doc_id="SYN-ANN-01", filename="announcement_01_meeting.pdf",
            document_type="announcement", builder=_announcement_1, note="股东大会通知",
        ),
        CorpusDocument(
            doc_id="SYN-ANN-02", filename="announcement_02_forecast_with_url.pdf",
            document_type="announcement", builder=_announcement_2, note="含外链（仅审计，不算注入）",
        ),
        CorpusDocument(
            doc_id="SYN-ANN-03", filename="announcement_03_injection.pdf",
            document_type="announcement", builder=_announcement_3, note="植入注入样本",
            expect_injection=True,
        ),
        CorpusDocument(
            doc_id="SYN-ANN-04", filename="announcement_04_correction.pdf",
            document_type="announcement", builder=_announcement_4, note="更正公告（口径信号）",
        ),
        CorpusDocument(
            doc_id="SYN-DRAFT-01", filename="report_draft_01_summary.pdf",
            document_type="report_draft", builder=_draft_1, note="研报草稿：摘要/基本面/风险",
        ),
        CorpusDocument(
            doc_id="SYN-DRAFT-02", filename="report_draft_02_valuation.pdf",
            document_type="report_draft", builder=_draft_2, note="研报草稿：估值口径",
        ),
        CorpusDocument(
            doc_id="SYN-DRAFT-03", filename="report_draft_03_industry.pdf",
            document_type="report_draft", builder=_draft_3, note="研报草稿：行业观察",
        ),
        CorpusDocument(
            doc_id="SYN-SCAN-01", filename="scan_sample.pdf",
            document_type="annual", builder=build_scan_page_pdf, note="纯扫描页样例",
            expect_status="degraded", expect_scan_page=1,
        ),
    ]


def gold_records(doc: CorpusDocument) -> List[Dict[str, object]]:
    return [rec.to_dict(doc.doc_id) for rec in doc.gold]


def write_corpus(out_dir: str | Path) -> Dict[str, object]:
    """Render every corpus document plus its gold truth; return the manifest.

    Writes:
      ``<out_dir>/*.pdf``            the synthetic documents
      ``<out_dir>/gold/<doc_id>.json``  CARD-11 §5 truth records
      ``<out_dir>/manifest.json``     sha256 + metadata for every document

    Deterministic: reportlab output is byte-stable for fixed input, so the
    manifest doubles as a reproducibility check (see ``verify_corpus``).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gold_dir = out / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)

    entries: List[Dict[str, object]] = []
    for doc in corpus():
        data = doc.build()
        path = out / doc.filename
        path.write_bytes(data)
        (gold_dir / f"{doc.doc_id}.json").write_text(
            json.dumps(
                {
                    "document_id": doc.doc_id,
                    "filename": doc.filename,
                    "document_type": doc.document_type,
                    "note": doc.note,
                    "expect_status": doc.expect_status,
                    "expect_scan_page": doc.expect_scan_page,
                    "expect_injection": doc.expect_injection,
                    "expect_facts_degraded": doc.expect_facts_degraded,
                    "records": gold_records(doc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        entries.append({
            "document_id": doc.doc_id,
            "filename": doc.filename,
            "document_type": doc.document_type,
            "sha256": sha256_of(data),
            "bytes": len(data),
            "gold_records": len(doc.gold),
            "expect_status": doc.expect_status,
            "expect_scan_page": doc.expect_scan_page,
            "expect_injection": doc.expect_injection,
            "expect_facts_degraded": doc.expect_facts_degraded,
            "note": doc.note,
        })

    manifest = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "generated_on": CORPUS_DATE,
        "generator": "scripts/gen_document_fixtures.py",
        "synthetic": True,
        "disclaimer": (
            "Synthetic fixtures for parser benchmarking only. Not sourced financial "
            "facts and must never be presented as real disclosures."
        ),
        "documents": entries,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def verify_corpus(out_dir: str | Path) -> Dict[str, object]:
    """Re-render the corpus in memory and compare to the committed manifest.

    Returns ``{"ok", "mismatches", "file_mismatches", "missing", "checked"}``.
    Reproducibility is an acceptance requirement (CARD-01 §13), so this is used
    both by the CLI (``--check``) and by the offline test suite.

    Two independent checks, because they fail for different reasons:

    * ``mismatches`` — a **fresh in-memory render** whose sha256 differs from the
      manifest. This is the reproducibility check. An earlier version compared
      the on-disk files instead, which the generator had just written, so it
      passed unconditionally and never noticed that reportlab was stamping a
      wall-clock ``/CreationDate`` into every PDF.
    * ``file_mismatches`` — a file on disk that differs from its own manifest
      entry, i.e. someone edited a fixture by hand.
    """
    out = Path(out_dir)
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        return {
            "ok": False, "mismatches": [], "file_mismatches": [], "missing": ["manifest.json"], "checked": 0,
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches: List[str] = []
    file_mismatches: List[str] = []
    missing: List[str] = []

    for entry in manifest.get("documents", []):
        filename = entry["filename"]
        path = out / filename
        fresh = _render_by_filename(filename)
        if fresh is None:
            mismatches.append(f"{filename}（清单中的文档已不在 corpus() 声明中）")
            continue
        if sha256_of(fresh) != entry["sha256"]:
            mismatches.append(filename)
        if not path.exists():
            missing.append(filename)
            continue
        if sha256_of(path.read_bytes()) != entry["sha256"]:
            file_mismatches.append(filename)

    return {
        "ok": not mismatches and not missing,
        "mismatches": mismatches,
        "file_mismatches": file_mismatches,
        "missing": missing,
        "checked": len(manifest.get("documents", [])),
    }


def _render_by_filename(filename: str) -> Optional[bytes]:
    """Render the corpus member with this filename, or ``None`` if undeclared."""
    for document in corpus():
        if document.filename == filename:
            return document.build()
    return None


def load_manifest(out_dir: str | Path) -> Dict[str, object]:
    return json.loads((Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))


def load_gold(out_dir: str | Path, doc_id: str) -> Dict[str, object]:
    return json.loads((Path(out_dir) / "gold" / f"{doc_id}.json").read_text(encoding="utf-8"))
