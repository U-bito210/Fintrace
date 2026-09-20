"""Unit / numeric-literal normalisation (CARD-02 §7.3, §8 edge cases 1-4).

Everything in this module is pure and deterministic: no locale, no float
formatting, no rounding beyond IEEE-754 multiplication. The base unit of the
system is **元**; every other declared unit is a fixed power of ten.

Fail-closed rules:

* an unparseable literal returns ``None`` (the caller records a warning);
* an unknown or missing unit raises (a number whose scale is unknown must never
  be silently treated as 元 — that is a 10,000× error, not a rounding issue).
"""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

from .models import Unit


class UnitNormalizationError(ValueError):
    """A literal or unit cannot be brought to the base unit deterministically."""


# Declared unit -> multiplier to the base unit (元). Exact powers of ten only.
UNIT_SCALES: dict[str, float] = {
    Unit.YUAN.value: 1.0,
    Unit.THOUSAND_YUAN.value: 1e3,
    Unit.WAN_YUAN.value: 1e4,
    Unit.MILLION_YUAN.value: 1e6,
    Unit.YI_YUAN.value: 1e8,
}

# Aliases accepted from real filings / providers, longest-first so that
# "百万元" is never matched as "万元" nor "万元" as "元".
_UNIT_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("亿元人民币", Unit.YI_YUAN.value),
    ("亿元", Unit.YI_YUAN.value),
    ("千万元", 1e7),
    ("千万", 1e7),
    ("百万元", Unit.MILLION_YUAN.value),
    ("百万", Unit.MILLION_YUAN.value),
    ("万元", Unit.WAN_YUAN.value),
    ("万", Unit.WAN_YUAN.value),
    ("千元", Unit.THOUSAND_YUAN.value),
    ("千", Unit.THOUSAND_YUAN.value),
    ("人民币元", Unit.YUAN.value),
    ("元", Unit.YUAN.value),
    ("亿", Unit.YI_YUAN.value),
    ("CNY", Unit.YUAN.value),
    ("RMB", Unit.YUAN.value),
)

# Tokens that mean "no number disclosed". Never treated as zero.
_PLACEHOLDERS = {
    "", "-", "--", "---", "—", "——", "–", "−", "…", "...", "/", "\\",
    "n/a", "na", "nil", "none", "nan", "null", "不适用", "无", "未披露",
    "未适用", "不披露", "不适用。",
}

_CURRENCY_WORDS = ("人民币", "CNY", "RMB", "USD", "美元", "HKD", "港元", "港币")

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９，．－＋（）　％",
    "0123456789,.-+() %",
)

_DASHES = dict.fromkeys(map(ord, "—–−―‑﹣－"), "-")

_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:,\d{3})*|\d*)(?:\.\d+)?$")


def _clean(text: str) -> str:
    """Full-width -> half-width, unified dashes, strip all whitespace."""
    cleaned = str(text).translate(_FULLWIDTH).translate(_DASHES)
    cleaned = cleaned.replace("\u00a0", " ")
    # Numbers never contain meaningful whitespace: "1 234 567" == "1234567".
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned


def looks_like_percent(text: object) -> bool:
    """True when the literal is a ratio expressed in percent (never scaled)."""
    return "%" in str(text)


def split_embedded_unit(text: str) -> Tuple[str, Optional[str]]:
    """Split a trailing inline unit, e.g. ``"12,500万元"`` -> ``("12500", "万元")``."""
    cleaned = _clean(text)
    match = re.search(r"(亿元|千万元|百万元|万元|千元|亿元|人民币元|万元|CNY|RMB|元|万|亿|千)$", cleaned)
    if not match:
        return cleaned, None
    unit = match.group(1)
    return cleaned[: match.start()], unit


def parse_numeric(value: object) -> Optional[float]:
    """Parse one disclosed literal; ``None`` when it is not a single number.

    Explicitly enumerated accounting conventions (CARD-02 §8.1):

    ==========================  ====================================
    ``"1,234,567.89"``          thousand separators
    ``"(1,234)"``               parentheses = negative (会计惯例)
    ``"（１２３４）"``            full-width parentheses/digits
    ``"—"`` / ``"N/A"`` / ``"不适用"``  ``None`` (never 0)
    ``"１２３４"``                full-width digits
    ==========================  ====================================
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and not math.isfinite(value)) else float(value)
    if value is None:
        return None

    cleaned = _clean(value)
    if cleaned.lower() in _PLACEHOLDERS:
        return None

    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    elif cleaned.endswith("-") and len(cleaned) > 1:
        # Some filings right-align the sign: "1,234-".
        negative = True
        cleaned = cleaned[:-1]
    cleaned = cleaned.lstrip("+")

    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]

    number_text, _ = split_embedded_unit(cleaned)
    number_text = number_text.rstrip("元")
    if not _NUMBER_RE.match(number_text):
        return None
    try:
        number = float(number_text.replace(",", ""))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -number if negative else number


def canonical_unit(unit: object) -> Optional[str]:
    """Map a declared unit token onto a canonical :class:`Unit` value, else ``None``."""
    if unit is None:
        return None
    if isinstance(unit, Unit):
        return unit.value
    if isinstance(unit, (int, float)):
        # Providers occasionally store the multiplier itself.
        for name, scale in UNIT_SCALES.items():
            if float(unit) == scale:
                return name
        return None
    token = _clean(unit)
    token = token.replace("：", "").replace(":", "")
    for word in _CURRENCY_WORDS:
        token = token.replace(word, "")
    if not token:
        # e.g. "美元" alone: the currency word was the whole token, so no unit
        # magnitude was declared. Returning a unit here would be a pure guess.
        return None
    if token in UNIT_SCALES:
        return token
    for alias, canonical in _UNIT_ALIASES:
        if token.endswith(alias):
            return canonical
    return None


def unit_scale(unit: object) -> float:
    """Multiplier to the base unit; raises when the unit is unknown or absent."""
    canonical = canonical_unit(unit)
    if canonical is None:
        if unit is None or str(unit).strip() == "":
            raise UnitNormalizationError("未声明单位：无法确定数值量级，拒绝按元处理。")
        raise UnitNormalizationError(f"不支持的单位 '{unit}'：只接受 元/千元/万元/百万元/亿元。")
    return UNIT_SCALES[canonical]


def normalize_unit(raw_value: str | float, unit: str) -> float:
    """Convert one disclosed value to the base unit (元). Zero tolerance.

    >>> normalize_unit("12,500", "万元")
    125000000.0
    """
    number = parse_numeric(raw_value)
    if number is None:
        raise UnitNormalizationError(f"无法解析为数值：{raw_value!r}")
    return number * unit_scale(unit)


def describe_conversion(raw_value: str | float, unit: str) -> str:
    """Human-readable conversion note for the demo's 原始值 → 规范化值 table."""
    return f"{raw_value} {canonical_unit(unit) or unit} = {normalize_unit(raw_value, unit):.2f} 元"
