"""CARD-04 §10 validator rules: the comparability gate must actually bite.

Three deterministic checks, each of which exists because a specific way of
misusing disclosure data is otherwise invisible:

1. **A percentage in a ``block_yoy`` period.** If CARD-04 refused the comparison,
   the analysis layer must not have produced one anyway. This is the check the
   whole card exists for: without it the gate is a comment, not a guarantee.
2. **An unresolvable signal evidence id.** Every signal must point at a located
   fact; a signal whose evidence cannot be resolved is an unsourced claim.
3. **A ``use_adjusted`` percentage computed against an ``AS_REPORTED`` operand.**
   The decision says "compare the restated figure"; a percentage built on the
   superseded figure is precisely the error the decision was made to avoid, and it
   is indistinguishable from a correct one by value alone.

The module is pure: no network, no LLM, no wall-clock dependence. It reports
problems; it never repairs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Version token that must NOT be the comparison operand under ``use_adjusted``.
VERSION_AS_REPORTED = "AS_REPORTED"


@dataclass(frozen=True)
class ComparabilityValidation:
    """Outcome of the §10 comparability gate."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings)}


def _version_of(identifier: str, versions: Dict[str, str]) -> Optional[str]:
    return versions.get(identifier)


def _collect_versions(normalized_facts: Optional[Iterable[object]]) -> Dict[str, str]:
    versions: Dict[str, str] = {}
    for fact in normalized_facts or ():
        identifier = str(getattr(fact, "normalized_fact_id", "") or "")
        if not identifier:
            continue
        version = getattr(fact, "version", None)
        versions[identifier] = str(getattr(version, "value", version) or VERSION_AS_REPORTED)
    return versions


def validate_comparability(
    result: object,
    *,
    normalized_facts: Optional[Sequence[object]] = None,
    known_fact_ids: Optional[Iterable[str]] = None,
    signals: Optional[Sequence[object]] = None,
) -> ComparabilityValidation:
    """Apply CARD-04 §10 to one :class:`AnalysisResult`.

    ``normalized_facts`` is needed for rule 3 only (resolving an operand's
    version); ``known_fact_ids`` for rule 2 (resolving signal evidence). Each rule
    is skipped when its inputs were not supplied — a skipped rule is reported as a
    warning rather than silently passing, so "valid" never overstates coverage.
    """
    errors: List[str] = []
    warnings: List[str] = []

    comparability = getattr(result, "comparability", None)
    metrics = list(getattr(result, "metrics", ()) or ())
    signal_list = list(signals if signals is not None else getattr(result, "scope_signals", ()) or ())

    percentages = [
        item for item in metrics
        if getattr(item, "delta_pct", None) is not None
    ]

    # Rule 1 — the gate.
    if comparability == "block_yoy":
        for item in percentages:
            errors.append(
                f"block_yoy 期间出现百分比同比：{getattr(item, 'metric', '?')}"
                f"（{getattr(item, 'period_key', '?')}，delta_pct={item.delta_pct}）。"
                "可比性已阻断即不得输出百分比。"
            )

    # Rule 2 — signal evidence must resolve.
    if known_fact_ids is None:
        if signal_list:
            warnings.append("未提供 known_fact_ids，跳过『信号证据可解析』检查。")
    else:
        known = {str(identifier) for identifier in known_fact_ids}
        for signal in signal_list:
            missing = [
                str(identifier)
                for identifier in (getattr(signal, "evidence_ids", ()) or ())
                if str(identifier) not in known
            ]
            if missing:
                errors.append(
                    f"信号 {getattr(signal, 'signal_type', None)} 的证据无法解析到文档页：{missing}"
                )
            if not (getattr(signal, "evidence_ids", ()) or ()):
                errors.append(
                    f"信号 {getattr(signal, 'signal_type', None)} 未绑定任何证据（禁止无源信号）"
                )

    # Rule 3 — use_adjusted must compare against a non-as-reported operand.
    if normalized_facts is None:
        if comparability == "use_adjusted":
            warnings.append("未提供 normalized_facts，跳过『use_adjusted 比较值版本』检查。")
    elif comparability == "use_adjusted":
        versions = _collect_versions(normalized_facts)
        for item in percentages:
            if str(getattr(item, "comparison", "")) not in ("ComparisonKind.YOY", "yoy"):
                continue
            inputs = list(getattr(item, "inputs", ()) or ())
            if len(inputs) < 2:
                continue
            comparison_operand = _version_of(str(inputs[1]), versions)
            if comparison_operand is None:
                warnings.append(
                    f"{getattr(item, 'metric', '?')}：比较值 {inputs[1]} 不在给定事实集中，"
                    "无法核对版本，已跳过该项检查。"
                )
                continue
            if comparison_operand == VERSION_AS_REPORTED:
                errors.append(
                    f"use_adjusted 下 {getattr(item, 'metric', '?')} 的百分比引用了 AS_REPORTED 的比较值"
                    f"（{inputs[1]}）；应取调整后/重述后版本。"
                )

    return ComparabilityValidation(valid=not errors, errors=errors, warnings=warnings)


__all__ = ["ComparabilityValidation", "VERSION_AS_REPORTED", "validate_comparability"]
