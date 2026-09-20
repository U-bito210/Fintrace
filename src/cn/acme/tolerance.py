"""CARD-22 §7 Tolerance Policy — the reason "Assets = Liabilities + Equity" is a
*constraint* and not a string comparison.

§7 forbids mechanical equality: a 元-denominated balance sheet with rounding at
the 万元 boundary legitimately differs by a few hundred 元, and flagging that as a
violation would make the detector useless. The policy therefore combines

* an **absolute** floor (1000 元 — 小额舍入差异), and
* a **relative** band (0.1% — 大额报表),

taking whichever is *larger*, so a small statement gets the floor and a large one
gets the percentage.

Two further rules are encoded here rather than in each caller:

* 容差内差异 → ``warning``, 容差外 → ``violated`` (§7). A caller that only asks
  ``within()`` cannot accidentally turn a rounding artefact into an error.
* A **statement-specific exemption白名单** exists, and it must be source
  traceable. An exemption without a ``source`` is refused at construction time —
  a silent whitelist is just a disabled check wearing a disguise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .models import (
    AcmeError,
    STATUS_PASSED,
    STATUS_VIOLATED,
    STATUS_WARNING,
)

#: §7 defaults, in the system's base unit (元).
DEFAULT_ABS_TOLERANCE = 1000.0
DEFAULT_REL_TOLERANCE = 0.001

#: Ratios (毛利率, 资产负债率) live on a different scale than 元 amounts, so a
#: 1000-元 floor would swallow every percentage comparison whole. The policy
#: therefore carries a per-unit floor as well, and a percent-space caller asks
#: for ``unit="PERCENT"`` explicitly instead of hoping the caller-side number
#: happens to be big enough.
ABS_TOLERANCE_BY_UNIT: Dict[str, float] = {
    "元": DEFAULT_ABS_TOLERANCE,
    "PERCENT": 0.05,          # 0.05 个百分点 — half of a displayed 0.1pct step
    "BP": 5.0,                # 5 个基点
    "MULTIPLE": 0.005,        # 0.005 倍
    "RATIO": 0.0005,
    # Table-internal subtotal checks (§7 F2) compare cells that share an unknown
    # unit, so the floor is expressed in *raw table units* (half of a displayed
    # last digit) and the band is relative.
    "TABLE_UNIT": 0.5,
}
REL_TOLERANCE_BY_UNIT: Dict[str, float] = {
    "元": DEFAULT_REL_TOLERANCE,
    "PERCENT": 0.0,           # ratios use the absolute band only
    "BP": 0.0,
    "MULTIPLE": 0.0,
    "RATIO": 0.0,
    "TABLE_UNIT": DEFAULT_REL_TOLERANCE,
}


@dataclass(frozen=True)
class ToleranceExemption:
    """A registered, *sourced* exemption for one constraint.

    Example: an audited 非经常性损益 reclassification legitimately breaks the
    cash-flow closure for one period. Registering it here means the report says
    "exempted, source: 2025年审计报告附注三" instead of silently not checking.
    """

    constraint_id: str
    reason: str
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise AcmeError(
                f"容差白名单 {self.constraint_id} 缺少来源（source）："
                "无来源的豁免等于关掉检查，禁止登记"
            )
        if not self.reason.strip():
            raise AcmeError(f"容差白名单 {self.constraint_id} 缺少理由（reason）")

    def to_dict(self) -> Dict[str, str]:
        return {
            "constraint_id": self.constraint_id,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(frozen=True)
class TolerancePolicy:
    """The §7 tolerance function. Frozen, so a check cannot widen it as it runs."""

    abs_tolerance: float = DEFAULT_ABS_TOLERANCE
    rel_tolerance: float = DEFAULT_REL_TOLERANCE
    exemptions: Tuple[ToleranceExemption, ...] = ()
    per_unit: bool = True

    def __post_init__(self) -> None:
        if self.abs_tolerance < 0 or self.rel_tolerance < 0:
            raise AcmeError("容差不得为负")

    # -- exemption lookup ---------------------------------------------------

    def exemption_for(self, constraint_id: str) -> Optional[ToleranceExemption]:
        for item in self.exemptions:
            if item.constraint_id == constraint_id:
                return item
        return None

    # -- the tolerance function --------------------------------------------

    def tolerance_for(
        self,
        expected: Optional[float],
        actual: Optional[float],
        *,
        constraint_id: str = "",
        unit: str = "元",
    ) -> float:
        """max(absolute floor, relative band). Never returns ``inf``.

        An unparseable operand is not a licence to accept anything: both ends
        missing yields the plain absolute floor, and the caller reports
        ``skipped_missing_field`` before it ever gets here.
        """
        if self.per_unit and unit in ABS_TOLERANCE_BY_UNIT:
            floor = ABS_TOLERANCE_BY_UNIT[unit]
            rel = REL_TOLERANCE_BY_UNIT[unit]
        else:
            floor = self.abs_tolerance
            rel = self.rel_tolerance
        exemption = self.exemption_for(constraint_id)
        if exemption is not None:
            # An exemption widens the band by one displayed step rather than
            # disabling the check — the residual is still reported.
            floor = max(floor, floor * 2)
        if expected is None or actual is None:
            return floor
        magnitude = max(abs(expected), abs(actual))
        return max(floor, rel * magnitude)

    def residual(self, expected: Optional[float], actual: Optional[float]) -> Optional[float]:
        """``expected − actual``, or ``None`` when either side is undecidable."""
        if expected is None or actual is None:
            return None
        return expected - actual

    def within(
        self,
        expected: Optional[float],
        actual: Optional[float],
        *,
        constraint_id: str = "",
        unit: str = "元",
    ) -> bool:
        residual = self.residual(expected, actual)
        if residual is None:
            return False
        return abs(residual) <= self.tolerance_for(
            expected, actual, constraint_id=constraint_id, unit=unit
        )

    def classify(
        self,
        expected: Optional[float],
        actual: Optional[float],
        *,
        constraint_id: str = "",
        unit: str = "元",
    ) -> Tuple[str, float, Optional[float]]:
        """``(status, tolerance, residual)`` — the §7 passed/warning/violated split.

        Three outcomes, not two. §7 names the split that matters —「容差内差异 →
        warning（不误报为 violated）；容差外 → violated」— but a *zero* residual is
        neither: calling it a warning would put every satisfied constraint into
        the review queue and make the queue unreadable. Exact agreement is
        ``passed``, a non-zero residual inside the band is ``warning``, and
        anything wider is ``violated``.

        Callers get the status *and* the two numbers that justify it, so the
        violation record can always be recomputed from its own payload
        (§13 rule 2).
        """
        residual = self.residual(expected, actual)
        tolerance = self.tolerance_for(
            expected, actual, constraint_id=constraint_id, unit=unit
        )
        if residual is None:
            return STATUS_WARNING, tolerance, None
        if residual == 0:
            return STATUS_PASSED, tolerance, residual
        status = STATUS_WARNING if abs(residual) <= tolerance else STATUS_VIOLATED
        return status, tolerance, residual

    def to_dict(self) -> Dict[str, object]:
        return {
            "abs_tolerance": self.abs_tolerance,
            "rel_tolerance": self.rel_tolerance,
            "per_unit": self.per_unit,
            "exemptions": [item.to_dict() for item in self.exemptions],
        }


#: The shipped default. Callers may construct their own; the tool passes this one
#: so a demo run and a test run cannot disagree about what "within tolerance"
#: means.
DEFAULT_POLICY = TolerancePolicy()


@dataclass(frozen=True)
class ToleranceOverrides:
    """Explicit, named overrides — used by tests and by 总决赛 callers."""

    abs_tolerance: Optional[float] = None
    rel_tolerance: Optional[float] = None
    exemptions: Tuple[ToleranceExemption, ...] = field(default_factory=tuple)

    def build(self) -> TolerancePolicy:
        return TolerancePolicy(
            abs_tolerance=(
                self.abs_tolerance if self.abs_tolerance is not None else DEFAULT_ABS_TOLERANCE
            ),
            rel_tolerance=(
                self.rel_tolerance if self.rel_tolerance is not None else DEFAULT_REL_TOLERANCE
            ),
            exemptions=self.exemptions,
        )
