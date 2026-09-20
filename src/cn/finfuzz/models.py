"""CARD-26 FinFuzz — data models.

FinFuzz is a **test framework, not a business module**: it mutates *correct*
claims into semantic errors and measures whether the deterministic detectors
catch them. Two consequences shape every type here:

* **Nothing may invent a result.** A suite that was not run reports
  ``TARGET / TO BE MEASURED`` (the sentinel CARD-11 already uses), never a
  plausible-looking zero. A per-error-type row with no sample stays in the report
  as ``N/A`` — deleting it would read as "covered and clean" (§11).
* **A miss must name a stage.** "Not detected" is ambiguous between "the
  extractor threw the tamper away" and "the checker looked and did not care".
  Those are different defects with different fixes, so the result carries
  :data:`FAILURE_EXTRACTOR` / :data:`FAILURE_CHECKER` rather than a bare ``False``
  (§7, §13 rule 2).

The operator vocabulary is fixed at ten members by §6. It is *not* the same
vocabulary as :class:`src.cn.checker.models.CheckType`: an operator names the
*tamper*, a check type names the *finding*. Many-to-many by design —
``citation`` can surface as any of five check types, and ``sign``/``numeric``
can both land on a recompute failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple


class FinFuzzError(RuntimeError):
    """Raised when a suite cannot be built or scored at all."""


class MutationOperators(str, Enum):
    """§6 — the ten financial-semantic mutation classes.

    Named exactly as the card's §6 block names them, including the plural class
    name, so the card's API sketch and the code stay greppable as one thing.
    """

    NUMERIC = "numeric"                 # 小数点移动/量级：1.26 → 12.6
    UNIT = "unit"                       # 万元↔亿元, CNY↔USD, %↔bp
    SIGN = "sign"                       # -3.2% ↔ +3.2%
    PERIOD = "period"                   # FY↔H1, Q2↔Q3, 2024↔2025
    YTD_VS_SINGLE = "ytd_vs_single"     # 9M cumulative 冒充 Q3 single
    SCOPE = "scope"                     # consolidated ↔ parent
    VERSION = "version"                 # corrected ↔ as-reported old value
    VALUATION = "valuation"             # PE_TTM ↔ PE_NTM, PE ↔ PB
    CITATION = "citation"               # 正确数字 + 错误页码 / 错误 Evidence ID
    CAUSAL = "causal"                   # 无 attribution 时把并列改为因果


#: Stable emission order — iterating the enum is how callers enumerate operators.
OPERATOR_ORDER: Tuple[str, ...] = tuple(member.value for member in MutationOperators)

OPERATOR_LABELS: Dict[str, str] = {
    MutationOperators.NUMERIC.value: "数值/量级篡改",
    MutationOperators.UNIT.value: "单位篡改",
    MutationOperators.SIGN.value: "正负号篡改",
    MutationOperators.PERIOD.value: "期间篡改",
    MutationOperators.YTD_VS_SINGLE.value: "累计冒充单期",
    MutationOperators.SCOPE.value: "口径篡改",
    MutationOperators.VERSION.value: "引用被取代的旧值",
    MutationOperators.VALUATION.value: "估值基数篡改",
    MutationOperators.CITATION.value: "引用篡改",
    MutationOperators.CAUSAL.value: "无证据因果",
}

#: Which CARD-05 findings legitimately count as "the tamper was caught".
#:
#: A *set* rather than a single value, because several operators are not
#: one-to-one with a check type. ``sign`` is the honest example: flipping the
#: sign of a disclosed growth rate is recomputed by the calculator, so CARD-05
#: reports ``calculation_error``, not ``numeric_error``. Accepting only the
#: literal-looking type would have scored a correct detection as a miss.
EXPECTED_CHECK_TYPES: Dict[str, Tuple[str, ...]] = {
    MutationOperators.NUMERIC.value: ("numeric_error",),
    MutationOperators.UNIT.value: ("unit_error",),
    MutationOperators.SIGN.value: ("calculation_error", "numeric_error"),
    MutationOperators.PERIOD.value: ("period_error",),
    MutationOperators.YTD_VS_SINGLE.value: ("period_error",),
    MutationOperators.SCOPE.value: ("scope_error",),
    MutationOperators.VERSION.value: ("revision_superseded",),
    MutationOperators.VALUATION.value: ("valuation_multiple_error",),
    MutationOperators.CITATION.value: (
        "missing_citation",
        "unsupported_citation",
        "wrong_citation",
        "wrong_page",
        "stale_citation",
    ),
    MutationOperators.CAUSAL.value: ("unsupported_causal_claim",),
}

#: How the tamper was applied.
#:
#: ``claim`` mutates the extracted claim's fields — the extractor is bypassed, so
#: a miss is unambiguously the checker's. ``text`` mutates the *sentence* and
#: re-runs extraction, which is the end-to-end path a real bad draft takes and
#: the only channel that can produce an extractor-stage miss. Both exist because
#: the two questions are different and §7 asks for the split.
CHANNEL_CLAIM = "claim"
CHANNEL_TEXT = "text"
CHANNELS: Tuple[str, ...] = (CHANNEL_CLAIM, CHANNEL_TEXT)

#: §7 / §13 rule 2 — the two ways a planted mutation can go undetected.
FAILURE_EXTRACTOR = "extractor"
FAILURE_CHECKER = "checker"
FAILURE_STAGES: Tuple[str, ...] = (FAILURE_EXTRACTOR, FAILURE_CHECKER)

#: Values of :attr:`MutationResult.detected_by`.
DETECTED_BY_CHECKER = "checker"
DETECTED_BY_VALIDATOR = "validator"
DETECTED_BY_VALUES: Tuple[str, ...] = (DETECTED_BY_CHECKER, DETECTED_BY_VALIDATOR)

#: §11 — an error type with no sample is reported as ``N/A`` and the row is kept.
NOT_APPLICABLE = "N/A"

#: §16 / §18 — an un-run comparison is written verbatim so it cannot be mistaken
#: for a measurement. Same sentinel CARD-11 uses.
NOT_MEASURED = "TARGET / TO BE MEASURED"


@dataclass(frozen=True)
class MutationSpec:
    """One planned tamper: what it was, how it was applied, what should happen.

    The card's §6 fields are all present. The extensions below each exist because
    a rule needed them and could not otherwise be expressed:

    * ``channel`` — without it a miss cannot be attributed (§7).
    * ``expected_check_types`` — a set, not one value (see
      :data:`EXPECTED_CHECK_TYPES`).
    * ``text_edits`` / ``source_sentence`` — the text channel needs the exact
      substitution that was made, so the suite can replay it and so a reader can
      see the tamper rather than infer it from two dicts.
    * ``claim_id`` — the join key back to CARD-05's findings.
    * ``combination`` — §12.2. When two operators are applied to the same claim the
      spec keeps one as ``operator`` (the bucket the report scores it under) and
      records the rest here, so a combination is *labelled* rather than silently
      counted as a single-operator sample.
    """

    mutation_id: str
    operator: str
    source_claim: Dict[str, Any]
    mutated_claim: Dict[str, Any]
    description: str
    expected_detection: bool = True
    channel: str = CHANNEL_CLAIM
    expected_check_types: Tuple[str, ...] = ()
    source_sentence: str = ""
    text_edits: Tuple[Tuple[str, str], ...] = ()
    claim_id: str = ""
    note: str = ""
    combination: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.operator not in OPERATOR_ORDER:
            raise FinFuzzError(f"unknown operator: {self.operator!r}")
        if self.channel not in CHANNELS:
            raise FinFuzzError(f"unknown channel: {self.channel!r}")
        unknown = [name for name in self.combination if name not in OPERATOR_ORDER]
        if unknown:
            raise FinFuzzError(f"{self.mutation_id}: unknown combination operator(s): {unknown}")
        if not self.source_claim or not self.mutated_claim:
            raise FinFuzzError(f"{self.mutation_id}: spec needs both sides of the diff")
        if self.expected_detection and not self.expected_check_types:
            # A planted mutation with nothing to look for cannot be scored, and
            # would silently score as a permanent miss.
            raise FinFuzzError(f"{self.mutation_id}: expected_detection=True needs expected_check_types")

    @property
    def operators(self) -> Tuple[str, ...]:
        """Every operator that shaped this spec, primary first, deduplicated."""
        ordered: List[str] = []
        for name in (self.operator, *self.combination):
            if name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    @property
    def is_combination(self) -> bool:
        return len(self.operators) > 1

    @property
    def equivalent(self) -> bool:
        """True for a §12.1 伪变异 — a rewrite that changes nothing semantically.

        These are the false-positive pressure samples: the correct behaviour is to
        stay silent, so a finding here *is* the defect.
        """
        return not self.expected_detection

    def diff(self) -> Dict[str, Tuple[Any, Any]]:
        """The changed fields, as ``{field: (before, after)}``.

        §13 rule 1 asks every spec to carry a diff description; deriving it from
        the two payloads means the description cannot drift from the mutation.
        """
        keys = sorted(set(self.source_claim) | set(self.mutated_claim))
        return {
            key: (self.source_claim.get(key), self.mutated_claim.get(key))
            for key in keys
            if self.source_claim.get(key) != self.mutated_claim.get(key)
        }

    def described_diff(self) -> List[str]:
        return [f"{key}: {before!r} -> {after!r}" for key, (before, after) in self.diff().items()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "operator": self.operator,
            "channel": self.channel,
            "claim_id": self.claim_id,
            "source_claim": dict(self.source_claim),
            "mutated_claim": dict(self.mutated_claim),
            "description": self.description,
            "expected_detection": self.expected_detection,
            "expected_check_types": list(self.expected_check_types),
            "combination": list(self.combination),
            "source_sentence": self.source_sentence,
            "text_edits": [list(item) for item in self.text_edits],
            "note": self.note,
        }


@dataclass(frozen=True)
class MutationResult:
    """What happened when one spec met the detectors.

    ``detected`` is the metric's input; ``failure_stage`` is the diagnosis. Both
    are kept because a report that only carried ``detected`` could state the
    recall but never the reason for a miss.
    """

    spec: MutationSpec
    detected: bool
    detected_by: Optional[str] = None
    error_type_classified: Optional[str] = None
    detection_time_ms: int = 0
    failure_stage: Optional[str] = None
    observed_check_types: Tuple[str, ...] = ()
    extracted_claim: Optional[Dict[str, Any]] = None
    extraction_carried: Optional[bool] = None
    #: *Every* detector that fired, not only the first. ``detected_by`` has to be
    #: single-valued per the card, but a count of detections per detector is only
    #: honest if a corroborating second opinion is not silently dropped.
    detected_by_all: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.detected and self.detected_by is None:
            raise FinFuzzError(f"{self.spec.mutation_id}: detected result must name a detector")
        if self.detected and self.failure_stage is not None:
            raise FinFuzzError(f"{self.spec.mutation_id}: a detected result has no failure stage")
        if not self.detected and self.failure_stage not in FAILURE_STAGES:
            raise FinFuzzError(
                f"{self.spec.mutation_id}: an undetected result must be attributed "
                f"to one of {FAILURE_STAGES}, got {self.failure_stage!r}"
            )

    @property
    def operator(self) -> str:
        return self.spec.operator

    @property
    def planted(self) -> bool:
        """CARD-11's ``planted`` column: a true mutation, which *should* be found."""
        return self.spec.expected_detection

    @property
    def is_false_positive(self) -> bool:
        """A 伪变异 that was flagged anyway — §13 rule 3's numerator."""
        return self.spec.equivalent and self.detected

    def to_adversarial_row(self) -> Dict[str, Any]:
        """The exact three fields CARD-11's ``adversarial_metrics`` reads.

        Narrowed here, at the boundary, rather than widening CARD-11's contract:
        CARD-11 owns the metric definition and must not learn FinFuzz's internals.
        """
        return {
            "error_type": self.operator,
            "planted": self.planted,
            "detected": bool(self.detected),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mutation_id": self.spec.mutation_id,
            "operator": self.operator,
            "channel": self.spec.channel,
            "expected_detection": self.spec.expected_detection,
            "detected": self.detected,
            "detected_by": self.detected_by,
            "detected_by_all": list(self.detected_by_all),
            "error_type_classified": self.error_type_classified,
            "detection_time_ms": self.detection_time_ms,
            "failure_stage": self.failure_stage,
            "observed_check_types": list(self.observed_check_types),
            "extraction_carried": self.extraction_carried,
            "description": self.spec.description,
            "diff": self.spec.described_diff(),
            "notes": list(self.notes),
        }


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    """``None`` when there is nothing to divide — never a fabricated ``0.0``."""
    if denominator <= 0:
        return None
    return numerator / denominator


def prf1(true_positive: int, false_positive: int, false_negative: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Precision / recall / F1, or ``None`` where the denominator is empty.

    Deliberately *not* importing CARD-11's :func:`prf1`: that one collapses an
    empty denominator to ``0.0``, which is right when a metric has a row to fill
    and wrong here, where ``0.0`` would be indistinguishable from "the suite
    never ran this error type". §11 asks for ``N/A`` in exactly this case.
    """
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    elif precision is not None and recall is not None:
        f1 = 0.0
    return precision, recall, f1


@dataclass
class FinFuzzReport:
    """§6 — the payload the benchmark consumes and the demo renders."""

    suite_id: str
    overall: Dict[str, Any] = field(default_factory=dict)
    per_error_type: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    baseline_holdout: Dict[str, Any] = field(default_factory=dict)
    extractor_vs_checker: Dict[str, Any] = field(default_factory=dict)
    rows: List[Mapping[str, Any]] = field(default_factory=list)
    skipped: List[Mapping[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    operators_run: Tuple[str, ...] = ()
    seed: Optional[int] = None
    synthetic: bool = True
    real_model: bool = False
    notes: List[str] = field(default_factory=list)

    def adversarial_rows(self) -> List[Mapping[str, Any]]:
        """The ``{error_type, planted, detected}`` rows CARD-11 reads."""
        return [dict(row) for row in self.rows]

    @property
    def measured(self) -> bool:
        """False when no mutation produced a scoreable row at all."""
        return bool(self.rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "overall": dict(self.overall),
            "per_error_type": {key: dict(value) for key, value in self.per_error_type.items()},
            "baseline_holdout": dict(self.baseline_holdout),
            "extractor_vs_checker": dict(self.extractor_vs_checker),
            "skipped": [dict(item) for item in self.skipped],
            "counts": dict(self.counts),
            "operators_run": list(self.operators_run),
            "seed": self.seed,
            "synthetic": self.synthetic,
            "real_model": self.real_model,
            "notes": list(self.notes),
        }


__all__ = [
    "CHANNELS",
    "CHANNEL_CLAIM",
    "CHANNEL_TEXT",
    "DETECTED_BY_CHECKER",
    "DETECTED_BY_VALIDATOR",
    "DETECTED_BY_VALUES",
    "EXPECTED_CHECK_TYPES",
    "FAILURE_CHECKER",
    "FAILURE_EXTRACTOR",
    "FAILURE_STAGES",
    "FinFuzzError",
    "FinFuzzReport",
    "MutationOperators",
    "MutationResult",
    "MutationSpec",
    "NOT_APPLICABLE",
    "NOT_MEASURED",
    "OPERATOR_LABELS",
    "OPERATOR_ORDER",
    "prf1",
]
