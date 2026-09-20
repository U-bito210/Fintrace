"""Period-key canonicalisation and ``period_kind`` detection (CARD-02 §7.2).

Two jobs, both fail-closed:

* :func:`canonical_period` — produce a key the existing
  :class:`~src.cn.periods.FinancialPeriodEngine` can consume
  (``YYYYQ1`` / ``YYYYH1`` / ``YYYY9M`` / ``YYYYFY``). Anything else raises
  ``ValueError`` instead of being coerced (``FY2024``, ``2024Q1_YTD``, ``Q1 2024``
  are all rejected).
* :func:`detect_period_kind` — decide whether a disclosed figure is a
  year-to-date cumulative, a single quarter or a point-in-time stock, from the
  *source table* plus the *column header* plus the document's declared report
  type. When those three cannot decide, the answer is ``None`` **plus a
  reason** — never a default. 判错比不转换更糟.

One deliberate refinement of the card's §7.2 table is documented in
:data:`_SUMMARY_RULE_NOTE`: "本报告期" is a *single quarter* only when the
document's reporting period is itself a single quarter (一季报/三季报). In an
annual or interim report the same column label denotes the full cumulative
period (FY or H1), so it is labelled ``YTD_CUMULATIVE``. ``report_type`` is the
argument that carries this distinction, which is why it is part of the
signature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from ..periods import supports_period_engine
from .models import PeriodKind

_PERIOD_RE = re.compile(r"^(?P<year>\d{4})(?P<kind>Q1|H1|9M|FY)$")
_ISO_DATE_RE = re.compile(r"^(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})$")
_CN_DATE_RE = re.compile(r"^(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?$")
_YEAR_RE = re.compile(r"^(?P<year>\d{4})年?$")


def _period_from_year_month(year: str, month: str, raw_period: object) -> str:
    """Map a reporting-period-end date onto the engine's quarter token."""
    kind = _ISO_MONTH_KIND.get(str(int(month)))
    if kind is None:
        raise ValueError(
            f"非标准报告期末日期 '{raw_period}'：只接受 3/6/9/12 月末（Q1/H1/9M/FY）。"
        )
    return f"{year}{kind}"


_ISO_MONTH_KIND = {"3": "Q1", "6": "H1", "9": "9M", "12": "FY"}

_CN_PERIOD_WORDS: Tuple[Tuple[str, str], ...] = (
    ("前三季度", "9M"), ("三季度", "9M"), ("第三季度", "9M"), ("三季度报告", "9M"),
    ("第一季度", "Q1"), ("一季度", "Q1"),
    ("半年度", "H1"), ("中期", "H1"), ("半年", "H1"),
    ("年度", "FY"), ("全年", "FY"),
)

_REPORT_KIND_WORDS: Tuple[Tuple[str, str], ...] = (
    ("annual", "annual"), ("interim", "interim"), ("quarterly", "quarterly"),
    ("年报", "annual"), ("年度报告", "annual"), ("annual_report", "annual"),
    ("半年报", "interim"), ("中期报告", "interim"), ("半年", "interim"), ("中报", "interim"),
    ("季报", "quarterly"), ("季度报告", "quarterly"), ("一季报", "quarterly"), ("三季报", "quarterly"),
)

# Statement bodies: the card's §7.2 first and fourth rows.
_BODY_KIND: Tuple[Tuple[Tuple[str, ...], PeriodKind], ...] = (
    (
        (
            "合并资产负债表", "母公司资产负债表", "资产负债表",
            "合并资产负债表（续）", "资产总计表",
        ),
        PeriodKind.POINT_IN_TIME,
    ),
    (
        (
            "合并利润表", "母公司利润表", "利润表", "合并利润及利润分配表",
            "合并利润表（续）", "利润及利润分配表",
        ),
        PeriodKind.YTD_CUMULATIVE,
    ),
    (
        (
            "合并现金流量表", "母公司现金流量表", "现金流量表",
            "合并现金流量表（续）",
        ),
        PeriodKind.YTD_CUMULATIVE,
    ),
)

_SUMMARY_TABLES = ("主要会计数据和财务指标", "主要会计数据", "主要财务数据", "主要财务指标")

_SUMMARY_RULE_NOTE = (
    "摘要表列语义：『本报告期』在季报中为单季值、在年报/半年报中为累计值；"
    "无法判定时返回 None。"
)

# Kinds required for a value to enter the single-quarter / TTM engine.
_DIFFERENTIABLE = (PeriodKind.YTD_CUMULATIVE,)


@dataclass(frozen=True)
class PeriodKindDecision:
    """``period_kind`` plus the reason when the kind could not be decided."""

    kind: Optional[PeriodKind]
    warning: Optional[str] = None

    @property
    def decided(self) -> bool:
        return self.kind is not None


def report_kind(report_type: object) -> Optional[str]:
    """Collapse a declared report type (or period key) to annual/interim/quarterly."""
    if report_type is None:
        return None
    text = str(report_type).strip()
    if not text:
        return None
    lowered = text.lower()
    for word, kind in _REPORT_KIND_WORDS:
        if word in lowered:
            return kind
    match = _PERIOD_RE.fullmatch(text.upper())
    if match:
        return _kind_of_period_token(match.group("kind"))
    return None


def _kind_of_period_token(kind: str) -> Optional[str]:
    if kind == "FY":
        return "annual"
    if kind == "H1":
        return "interim"
    if kind in {"9M", "Q1", "Q2", "Q3", "Q4"}:
        return "quarterly"
    return None


def period_token(period_key: str) -> str:
    """``"2025FY"`` -> ``"FY"``; raises for unsupported keys."""
    match = _PERIOD_RE.fullmatch(str(period_key).strip().upper())
    if not match:
        raise ValueError(f"不支持的期间键 '{period_key}'：只接受 YYYYQ1/YYYYH1/YYYY9M/YYYYFY。")
    return match.group("kind")


def period_year(period_key: str) -> int:
    match = _PERIOD_RE.fullmatch(str(period_key).strip().upper())
    if not match:
        raise ValueError(f"不支持的期间键 '{period_key}'：只接受 YYYYQ1/YYYYH1/YYYY9M/YYYYFY。")
    return int(match.group("year"))


def is_supported_period_key(value: object) -> bool:
    """True when the engine accepts the key (no coercion, no guessing)."""
    return supports_period_engine(str(value).strip().upper() if value is not None else None)


def canonical_period(raw_period: object, report_type: object = None) -> str:
    """Return the engine-compatible period key, or raise ``ValueError``.

    Accepted inputs (everything else raises):

    ==============================  ======================
    ``"2025FY"`` / ``"2025H1"``     already canonical
    ``"2025年"`` + report_type      年度 -> ``2025FY`` …
    ``"2024年度"`` / ``"2024前三季度"``  Chinese period words
    ``"2025-09-30"``                报告期末日期 -> ``20259M``
    ==============================  ======================
    """
    if raw_period is None:
        raise ValueError("期间为空：无法生成规范化期间键。")
    text = str(raw_period).strip()
    if not text:
        raise ValueError("期间为空：无法生成规范化期间键。")
    text = re.sub(r"\s+", "", text)

    upper = text.upper()
    if _PERIOD_RE.fullmatch(upper):
        return upper

    match = _ISO_DATE_RE.match(text)
    if match:
        return _period_from_year_month(match.group("year"), match.group("month"), raw_period)

    match = _CN_DATE_RE.match(text)
    if match:
        return _period_from_year_month(match.group("year"), match.group("month"), raw_period)

    match = _YEAR_RE.match(text)
    if match:
        kind = _kind_from_report_type(report_type)
        if kind is None:
            raise ValueError(
                f"期间 '{raw_period}' 只声明了年份，且报告类型（{report_type!r}）不足以判定季度，"
                "拒绝猜测。"
            )
        return f"{match.group('year')}{kind}"

    for word, kind in _CN_PERIOD_WORDS:
        if word in text:
            year = re.search(r"(\d{4})", text)
            if not year:
                raise ValueError(f"期间 '{raw_period}' 缺少年份，拒绝猜测。")
            return f"{year.group(1)}{kind}"

    raise ValueError(
        f"不支持的期间键 '{raw_period}'：只接受 YYYYQ1/YYYYH1/YYYY9M/YYYYFY、"
        "报告期末日期或『YYYY年+报告类型』。"
    )


def _kind_from_report_type(report_type: object) -> Optional[str]:
    kind = report_kind(report_type)
    if kind == "annual":
        return "FY"
    if kind == "interim":
        return "H1"
    return None


def previous_period_key(period_key: str, *, point_in_time: bool = False) -> Optional[str]:
    """The immediately preceding comparable period.

    * flow columns (今日 vs 上年同期) -> same period kind, previous year;
    * stock columns (期末余额 vs 期初余额) -> the previous fiscal year end.

    Returns ``None`` only when the key itself is unsupported.
    """
    try:
        token = period_token(period_key)
        year = period_year(period_key)
    except ValueError:
        return None
    if point_in_time:
        return f"{year - 1}FY"
    return f"{year - 1}{token}"


def column_period_role(header_text: object, *, token: object = None) -> str:
    """Classify a column header as ``current`` / ``ytd`` / ``prior`` / ``change`` / ``unknown``.

    ``ytd`` is kept apart from ``current`` because the two columns of an A-share
    summary table mean different things: ``年初至本报告期末`` is always a
    year-to-date cumulative, while ``本报告期`` is the *reporting period itself*
    (a quarter in a 季报, a full year in an 年报).

    Accepts both the raw header wording and the parser's internal token
    (``ytd`` / ``current_period`` / ``period_end`` / ``yoy_change`` /
    ``prior_year`` / ``prior_year_end``).
    """
    token_map = {
        "ytd": "ytd", "current_period": "current", "period_end": "current",
        "yoy_change": "change",
        "prior_year": "prior", "prior_year_end": "prior",
    }
    if token is not None:
        mapped = token_map.get(str(token).strip().lower())
        if mapped:
            return mapped

    text = re.sub(r"\s+", "", str(header_text or ""))
    if not text:
        return "unknown"
    if re.search(r"同比|环比|增减|变动幅|增长率|增幅|降幅", text):
        return "change"
    # The YTD span wording contains "年初" and must win over the 期初 rule below.
    if re.search(r"年初至|年初到", text):
        return "ytd"
    if re.search(r"期初|年初余额|年初数|上年同期|去年同期|上期|上年末|上年度末", text):
        return "prior"
    if re.search(r"期末|本期|本月|本年|年末|报告期|当期", text):
        return "current"
    return "unknown"


def _table_key(source_table_name: object) -> str:
    return re.sub(r"\s+", "", str(source_table_name or ""))


def period_kind_decision(
    source_table_name: object,
    column_header: object = None,
    report_type: object = None,
    *,
    column_token: object = None,
) -> PeriodKindDecision:
    """Decide ``period_kind`` for one located figure, with a reason when undecided."""
    table = _table_key(source_table_name)
    if not table:
        return PeriodKindDecision(
            None, "来源表未知（非三大表/摘要表来源），不推断期间语义，跳过单季化与 TTM。"
        )

    for names, kind in _BODY_KIND:
        if table in names:
            return PeriodKindDecision(kind)

    # Keyword fallback for provider-specific suffixes such as "合并利润表（续）".
    if "资产负债表" in table:
        return PeriodKindDecision(PeriodKind.POINT_IN_TIME)
    if "现金流量表" in table:
        return PeriodKindDecision(PeriodKind.YTD_CUMULATIVE)
    if "利润表" in table or "利润及利润分配表" in table:
        return PeriodKindDecision(PeriodKind.YTD_CUMULATIVE)

    if any(summary in table for summary in _SUMMARY_TABLES):
        role = column_period_role(column_header, token=column_token)
        if role == "change":
            return PeriodKindDecision(None, "同比/环比列不是水平值，已跳过。")
        if role == "ytd":
            return PeriodKindDecision(PeriodKind.YTD_CUMULATIVE)
        if role == "prior":
            return PeriodKindDecision(
                None,
                f"摘要表『{table}』上年同期列的期间语义未声明，不推断（{_SUMMARY_RULE_NOTE}）",
            )
        if role == "unknown":
            return PeriodKindDecision(
                None,
                f"摘要表『{table}』列语义未声明，无法判定期间语义，跳过单季化与 TTM（{_SUMMARY_RULE_NOTE}）",
            )
        # role == "current": 本报告期 = 本报告期覆盖的期间。季报 → 单季；年报/半年报 → 累计。
        if report_kind(report_type) == "quarterly":
            return PeriodKindDecision(PeriodKind.SINGLE_QUARTER)
        return PeriodKindDecision(PeriodKind.YTD_CUMULATIVE)

    return PeriodKindDecision(
        None,
        f"来源表『{table}』不在三大表/摘要表白名单内（公告、附注或未识别表格），"
        "不推断期间语义。",
    )


def detect_period_kind(
    source_table_name: object,
    column_header: object = None,
    report_type: object = None,
    *,
    column_token: object = None,
) -> Optional[PeriodKind]:
    """CARD-02 §6 signature: return the kind, or ``None`` when undecidable.

    Callers that must also surface *why* it is ``None`` should use
    :func:`period_kind_decision`. A ``None`` here always means "must not be
    differenced" — never "assume YTD".
    """
    return period_kind_decision(
        source_table_name, column_header, report_type, column_token=column_token
    ).kind


def is_differentiable(kind: Optional[PeriodKind]) -> bool:
    """Only cumulative YTD flows may enter single-quarter / TTM derivation."""
    return kind in _DIFFERENTIABLE
