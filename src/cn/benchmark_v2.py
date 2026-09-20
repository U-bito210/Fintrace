"""CARD-11 real-world benchmark (``cn_agent_v2``) — metric engine and report.

The card asks for a *three-tier* evaluation system (synthetic / real / holdout),
24 metrics, an ablation table, and an honesty contract strict enough to survive
a judge asking "how do you know?".

What lives here is deliberately **pure**: every metric is a function of already
captured rows (truth labels + predictions + run metadata). Nothing in this
module calls a model, reads a network, or reads the wall clock, so the same
inputs always produce the same report — which is the only way a benchmark number
can be audited later.

Three contract points worth reading before changing anything:

* **Nothing is pre-filled.** ``THRESHOLDS`` starts with every target at ``None``
  ("TARGET / TO BE MEASURED"). A fabricated 95% is worse than a blank, because
  a blank invites the measurement and a fake number ends the conversation.
* **An unavailable metric is a row, not a deletion.** ``Metric.note`` explains
  why a value is ``None``; the report prints ``N/A (reason)``. §9 is explicit
  that *the honesty of the report is itself under review*.
* **Synthetic and real results are physically separated.** ``isolate_payload``
  refuses to build a report that mixes a real-tier payload into a synthetic
  directory (or vice versa) — a mixed report is how a demo number becomes a
  fake headline.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
SUITE_ID = "cn_agent_v2"
SUITE_VERSION = "2.0"

TIER_SYNTHETIC = "synthetic"
TIER_REAL = "real"
TIER_HOLDOUT = "holdout"
TIERS: Tuple[str, ...] = (TIER_SYNTHETIC, TIER_REAL, TIER_HOLDOUT)

#: ``data/benchmark_real/<tier>/`` filename discipline (card §10): a real-tier
#: artefact must carry this prefix so a mixed directory is visible at a glance.
REAL_FILENAME_PREFIX = "real_"

#: Fractions: numerator / denominator, defaults to 0.0 when the denominator is 0
#: *and* the caller declared the metric measureable. A metric with no data at
#: all is ``None`` + note, never 0.0 — "no data" and "scored zero" differ.
Numeric = Optional[float]

TOLERANCE_DEFAULT = 1e-6

# --------------------------------------------------------------------------- #
# The 24 metrics (card §7)
# --------------------------------------------------------------------------- #
EXTRACTION_METRICS: Tuple[str, ...] = (
    "extraction_precision",
    "extraction_recall",
    "extraction_f1",
    "numeric_exact_match",
    "unit_accuracy",
    "period_accuracy",
    "scope_accuracy",
    "page_attribution_accuracy",
)
CITATION_METRICS: Tuple[str, ...] = ("citation_precision", "citation_recall")
CALCULATION_METRICS: Tuple[str, ...] = ("calculation_accuracy",)
CHECKER_METRICS: Tuple[str, ...] = (
    "checker_precision",
    "checker_recall",
    "checker_f1",
    "checker_false_positive_rate",
)
AGENT_METRICS: Tuple[str, ...] = (
    "tool_selection_accuracy",
    "parameter_accuracy",
    "task_completion_rate",
    "evidence_coverage",
)
SAFETY_METRICS: Tuple[str, ...] = ("unsafe_conclusion_leakage",)
REPLAY_METRICS: Tuple[str, ...] = ("replay_success_rate", "multi_run_consistency")
COST_METRICS: Tuple[str, ...] = ("latency_seconds", "token_cost")
ADVERSARIAL_METRICS: Tuple[str, ...] = (
    "mutation_detection_precision",
    "mutation_detection_recall",
    "mutation_detection_f1",
    "mutation_detection_false_positive_rate",
)

METRIC_GROUPS: Dict[str, Tuple[str, ...]] = {
    "extraction": EXTRACTION_METRICS,
    "citation": CITATION_METRICS,
    "calculation": CALCULATION_METRICS,
    "checker": CHECKER_METRICS,
    "agent": AGENT_METRICS,
    "safety": SAFETY_METRICS,
    "replay": REPLAY_METRICS,
    "cost": COST_METRICS,
}

#: The card's "24 项指标". Adversarial (FinFuzz) metrics are reported separately
#: because CARD-26 owns the mutation corpus.
BASE_METRICS: Tuple[str, ...] = tuple(
    metric for group in METRIC_GROUPS.values() for metric in group
)

#: Every academic-style group except cost is "higher is better".
LOWER_IS_BETTER = frozenset({"latency_seconds", "token_cost", "unsafe_conclusion_leakage"})

#: Card §7 threshold discipline: **all targets are unfrozen until each module
#: has a measured baseline.** A value of ``None`` means TARGET / TO BE MEASURED.
#: Do not fill these in by hand — ``freeze_thresholds`` does it from a baseline.
THRESHOLDS: Dict[str, Numeric] = {metric: None for metric in (*BASE_METRICS, *ADVERSARIAL_METRICS)}

#: Card §7b — every innovation needs a "without vs with" comparison before it can
#: be claimed as a contribution.
ABLATION_COMPARISONS: Tuple[Dict[str, str], ...] = (
    {
        "innovation": "ACME (CARD-22)",
        "baseline": "parser_only",
        "treatment": "parser_plus_accounting_constraints",
        "metric": "financial_extraction_error_detection",
    },
    {
        "innovation": "Claim Passport (CARD-23)",
        "baseline": "citation_only",
        "treatment": "fpo_verifier",
        "metric": "unsupported_claim_detection",
    },
    {
        "innovation": "Thesis Fragility (CARD-24)",
        "baseline": "no_fragility_engine",
        "treatment": "fragility_engine",
        "metric": "critical_dependency_correctness",
    },
    {
        "innovation": "Temporal Revalidation (CARD-25)",
        "baseline": "full_recomputation",
        "treatment": "incremental_subgraph",
        "metric": "nodes_recomputed",
    },
    {
        "innovation": "FinFuzz (CARD-26)",
        "baseline": "regular_tests_only",
        "treatment": "regular_plus_finfuzz",
        "metric": "per_error_type_recall",
    },
)

#: Placeholder used *only* when a comparison has not been run. It is written into
#: the report verbatim so an un-run ablation cannot be mistaken for a result.
NOT_MEASURED = "TARGET / TO BE MEASURED"


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Metric:
    """One measured (or honestly unmeasured) metric value."""

    name: str
    value: Numeric
    unit: str = ""
    note: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit, "note": self.note}

    def render(self) -> str:
        if not self.available:
            return f"N/A（{self.note or '未测量'}）"
        value = self.value if self.value is not None else 0.0
        if self.unit == "%":
            return f"{value * 100:.2f}%"
        if self.unit == "seconds":
            return f"{value:.4f}s"
        if self.unit == "CNY":
            return f"¥{value:.4f}"
        return f"{value:.6f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class TruthLabel:
    """Ground truth for one extracted fact (card §5).

    ``{document_id, metric, period, expected_value, unit, scope, page}`` — every
    row can be checked back against the printed page it came from.
    """

    document_id: str
    metric: str
    period: str
    expected_value: Optional[float] = None
    unit: str = ""
    scope: str = ""
    page: Optional[int] = None
    expected_evidence_ids: Tuple[str, ...] = ()
    expected_text: str = ""

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self.document_id, self.metric, self.period, self.scope)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TruthLabel":
        return cls(
            document_id=str(data.get("document_id", "")),
            metric=str(data.get("metric", "")),
            period=str(data.get("period", data.get("period_key", ""))),
            expected_value=_optional_float(data.get("expected_value")),
            unit=str(data.get("unit", "") or ""),
            scope=str(data.get("scope", "") or ""),
            page=int(data["page"]) if data.get("page") is not None else None,
            expected_evidence_ids=tuple(str(item) for item in (data.get("expected_evidence_ids") or ())),
            expected_text=str(data.get("expected_text", "") or ""),
        )


@dataclass(frozen=True)
class Prediction:
    """One extracted fact produced by the system under test."""

    document_id: str
    metric: str
    period: str
    value: Optional[float] = None
    unit: str = ""
    scope: str = ""
    page: Optional[int] = None
    evidence_ids: Tuple[str, ...] = ()
    cited_evidence_ids: Tuple[str, ...] = ()

    @property
    def key(self) -> Tuple[str, str, str, str]:
        return (self.document_id, self.metric, self.period, self.scope)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Prediction":
        return cls(
            document_id=str(data.get("document_id", "")),
            metric=str(data.get("metric", "")),
            period=str(data.get("period", data.get("period_key", ""))),
            value=_optional_float(data.get("value", data.get("expected_value"))),
            unit=str(data.get("unit", "") or ""),
            scope=str(data.get("scope", "") or ""),
            page=int(data["page"]) if data.get("page") is not None else None,
            evidence_ids=tuple(str(item) for item in (data.get("evidence_ids") or ())),
            cited_evidence_ids=tuple(str(item) for item in (data.get("cited_evidence_ids") or ())),
        )


@dataclass
class TierPayload:
    """Everything one tier contributes to the report."""

    tier: str
    labels: List[TruthLabel] = field(default_factory=list)
    predictions: List[Prediction] = field(default_factory=list)
    calculations: List[Mapping[str, Any]] = field(default_factory=list)
    checker_rows: List[Mapping[str, Any]] = field(default_factory=list)
    agent_rows: List[Mapping[str, Any]] = field(default_factory=list)
    sentences: List[Mapping[str, Any]] = field(default_factory=list)
    replay_rows: List[Mapping[str, Any]] = field(default_factory=list)
    run_summaries: List[Mapping[str, Any]] = field(default_factory=list)
    cost_rows: List[Mapping[str, Any]] = field(default_factory=list)
    adversarial_rows: List[Mapping[str, Any]] = field(default_factory=list)
    case_count: int = 0
    notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier: {self.tier!r} (expected one of {TIERS})")


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Elementary scoring
# --------------------------------------------------------------------------- #
def prf1(true_positive: int, false_positive: int, false_negative: int) -> Tuple[Numeric, Numeric, Numeric]:
    """Precision / recall / F1 from raw counts.

    A zero denominator yields ``0.0`` only when there was something to get wrong;
    when *both* the positive and negative classes are empty the caller should not
    report the metric at all (it has no information).
    """
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def _close(left: Optional[float], right: Optional[float], tolerance: float) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance


# --------------------------------------------------------------------------- #
# Metric calculators — extraction / citation
# --------------------------------------------------------------------------- #
def extraction_metrics(
    labels: Sequence[TruthLabel],
    predictions: Sequence[Prediction],
    *,
    tolerance: float = TOLERANCE_DEFAULT,
) -> Dict[str, Metric]:
    """The eight extraction-group metrics (card §7).

    Matching is on the *label key* ``(document_id, metric, period, scope)``, so a
    prediction with the right number but the wrong period is a false positive
    **and** a false negative — period errors are not silently forgiven.
    """
    if not labels and not predictions:
        blank = {name: Metric(name, None, note="no_labels_or_predictions") for name in EXTRACTION_METRICS}
        return blank

    by_key: Dict[Tuple[str, str, str, str], TruthLabel] = {label.key: label for label in labels}
    matched: List[Tuple[TruthLabel, Prediction]] = []
    seen: set = set()
    for prediction in predictions:
        label = by_key.get(prediction.key)
        if label is None or prediction.key in seen:
            continue
        seen.add(prediction.key)
        matched.append((label, prediction))

    true_positive = len(matched)
    false_positive = len(predictions) - true_positive
    false_negative = len(labels) - true_positive
    precision, recall, f1 = prf1(true_positive, false_positive, false_negative)

    if not matched:
        numeric = unit = period = scope = page = None
    else:
        numeric = sum(1 for label, prediction in matched if _close(label.expected_value, prediction.value, tolerance)) / len(matched)
        unit = sum(1 for label, prediction in matched if label.unit == prediction.unit) / len(matched)
        scope = sum(1 for label, prediction in matched if label.scope == prediction.scope) / len(matched)
        page = sum(1 for label, prediction in matched if label.page is not None and label.page == prediction.page) / len(matched)
        # Period correctness is measured over the metric identity, independent of
        # the join above: "did it attach the right period to this metric at all".
        grouped: Dict[Tuple[str, str], set] = {}
        for label in labels:
            grouped.setdefault((label.document_id, label.metric), set()).add(label.period)
        period_hits = period_total = 0
        for prediction in predictions:
            periods = grouped.get((prediction.document_id, prediction.metric))
            if not periods:
                continue
            period_total += 1
            period_hits += int(prediction.period in periods)
        period = (period_hits / period_total) if period_total else None

    note = "" if matched else "no_matched_labels"
    return {
        "extraction_precision": Metric("extraction_precision", precision, "%" if matched else "", note),
        "extraction_recall": Metric("extraction_recall", recall, "%" if matched else "", note),
        "extraction_f1": Metric("extraction_f1", f1, "%" if matched else "", note),
        "numeric_exact_match": Metric("numeric_exact_match", numeric, "%", "" if numeric is not None else "no_matched_labels"),
        "unit_accuracy": Metric("unit_accuracy", unit, "%", "" if unit is not None else "no_matched_labels"),
        "period_accuracy": Metric("period_accuracy", period, "%", "" if period is not None else "no_labeled_periods"),
        "scope_accuracy": Metric("scope_accuracy", scope, "%", "" if scope is not None else "no_matched_labels"),
        "page_attribution_accuracy": Metric("page_attribution_accuracy", page, "%", "" if page is not None else "no_matched_labels"),
    }


def citation_metrics(
    labels: Sequence[TruthLabel], predictions: Sequence[Prediction]
) -> Dict[str, Metric]:
    """Citation precision / recall over evidence ids (card §7)."""
    expected: set = set()
    for label in labels:
        expected.update(label.expected_evidence_ids)
    cited: set = set()
    for prediction in predictions:
        cited.update(prediction.cited_evidence_ids or prediction.evidence_ids)
    if not expected and not cited:
        return {
            "citation_precision": Metric("citation_precision", None, note="no_citations"),
            "citation_recall": Metric("citation_recall", None, note="no_expected_citations"),
        }
    hit = expected & cited
    precision = len(hit) / len(cited) if cited else 0.0
    recall = len(hit) / len(expected) if expected else 0.0
    return {
        "citation_precision": Metric("citation_precision", precision, "%"),
        "citation_recall": Metric("citation_recall", recall, "%"),
    }


def calculation_metrics(
    rows: Sequence[Mapping[str, Any]], *, tolerance: float = TOLERANCE_DEFAULT
) -> Dict[str, Metric]:
    """Calculation accuracy, including YTD single-quarter and YoY status moves.

    Each row: ``{calculation_id, expected_value, actual_value, kind?}``. A row may
    declare ``expected_unavailable=True`` — the correct answer is then *no value*,
    and producing one is an error (that is the YTD-single-quarter guard).
    """
    if not rows:
        return {"calculation_accuracy": Metric("calculation_accuracy", None, note="no_calculations")}
    correct = 0
    for row in rows:
        expected_unavailable = bool(row.get("expected_unavailable", False))
        actual = row.get("actual_value")
        if expected_unavailable:
            correct += int(actual is None or row.get("actual_unavailable") is True)
            continue
        correct += int(_close(_optional_float(row.get("expected_value")), _optional_float(actual), tolerance))
    return {"calculation_accuracy": Metric("calculation_accuracy", correct / len(rows), "%")}


# --------------------------------------------------------------------------- #
# Metric calculators — checker / agent / safety
# --------------------------------------------------------------------------- #
def checker_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Metric]:
    """Checker precision / recall / F1 and false-positive rate (card §7).

    Each row: ``{predicted: bool, actual: bool}`` — ``actual`` is whether the
    finding is a genuine error according to human annotation. The false-positive
    rate is reported separately because a checker that screams at everything
    looks fine on recall and is useless in practice.
    """
    if not rows:
        return {
            name: Metric(name, None, note="no_checker_annotations") for name in CHECKER_METRICS
        }
    tp = sum(1 for row in rows if bool(row.get("predicted")) and bool(row.get("actual")))
    fp = sum(1 for row in rows if bool(row.get("predicted")) and not bool(row.get("actual")))
    fn = sum(1 for row in rows if not bool(row.get("predicted")) and bool(row.get("actual")))
    tn = sum(1 for row in rows if not bool(row.get("predicted")) and not bool(row.get("actual")))
    precision, recall, f1 = prf1(tp, fp, fn)
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    return {
        "checker_precision": Metric("checker_precision", precision, "%"),
        "checker_recall": Metric("checker_recall", recall, "%"),
        "checker_f1": Metric("checker_f1", f1, "%"),
        "checker_false_positive_rate": Metric("checker_false_positive_rate", fpr, "%"),
    }


def agent_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Metric]:
    """Tool selection, parameter accuracy, task completion, evidence coverage.

    Each row: ``{expected_tools, called_tools, expected_arguments,
    called_arguments, completed, required_evidence, covered_evidence}`` where the
    ``*_arguments`` values are mappings of argument name → expected value.
    """
    if not rows:
        return {name: Metric(name, None, note="no_agent_rows") for name in AGENT_METRICS}

    tool_hits = parameter_hits = parameter_total = completed = 0
    covered = required = 0
    for row in rows:
        expected_tools = {str(item) for item in (row.get("expected_tools") or ())}
        called_tools = {str(item) for item in (row.get("called_tools") or ())}
        tool_hits += int(expected_tools == called_tools)

        expected_arguments = dict(row.get("expected_arguments") or {})
        called_arguments = dict(row.get("called_arguments") or {})
        for key, value in expected_arguments.items():
            parameter_total += 1
            parameter_hits += int(called_arguments.get(key) == value)

        completed += int(bool(row.get("completed", False)))
        required += len(row.get("required_evidence") or ())
        covered += len(set(row.get("covered_evidence") or ()) & set(row.get("required_evidence") or ()))

    size = len(rows)
    return {
        "tool_selection_accuracy": Metric("tool_selection_accuracy", tool_hits / size, "%"),
        "parameter_accuracy": Metric(
            "parameter_accuracy",
            parameter_hits / parameter_total if parameter_total else None,
            "%",
            "" if parameter_total else "no_expected_arguments",
        ),
        "task_completion_rate": Metric("task_completion_rate", completed / size, "%"),
        "evidence_coverage": Metric(
            "evidence_coverage",
            covered / required if required else None,
            "%",
            "" if required else "no_required_evidence",
        ),
    }


def unsafe_conclusion_leakage(sentences: Sequence[Mapping[str, Any]]) -> Dict[str, Metric]:
    """Fraction of *fact*-marked statements whose backing claim is not a fact.

    Card §7's safety group: an inference dressed as a fact is the single most
    damaging failure mode for a research agent, so it gets its own metric rather
    than being folded into extraction accuracy.

    Each row: ``{claim_type, backing_claim_type}`` — the tier the sentence
    advertises and the tier the graph actually holds. ``backing_claim_type`` may
    be ``None`` for a statement bound to no claim at all, which also leaks.
    """
    if not sentences:
        return {"unsafe_conclusion_leakage": Metric("unsafe_conclusion_leakage", None, note="no_statements")}
    facts = [row for row in sentences if str(row.get("claim_type")) == "fact"]
    if not facts:
        return {"unsafe_conclusion_leakage": Metric("unsafe_conclusion_leakage", 0.0, "%", "no_fact_statements")}
    leaks = sum(1 for row in facts if str(row.get("backing_claim_type")) != "fact")
    return {"unsafe_conclusion_leakage": Metric("unsafe_conclusion_leakage", leaks / len(facts), "%")}


# --------------------------------------------------------------------------- #
# Metric calculators — replay / cost / adversarial
# --------------------------------------------------------------------------- #
def replay_metrics(
    replay_rows: Sequence[Mapping[str, Any]],
    run_summaries: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = TOLERANCE_DEFAULT,
) -> Dict[str, Metric]:
    """Replay success rate and multi-run consistency (card §7).

    ``replay_rows``: ``{run_id, replayed: bool, identical: bool}``.
    ``run_summaries``: one metric→value mapping per repeated run (card §8 edge 3
    requires n≥3 before the consistency number means anything).
    """
    if replay_rows:
        replayed = sum(1 for row in replay_rows if bool(row.get("replayed")))
        identical = sum(1 for row in replay_rows if bool(row.get("identical")))
        replay_success = identical / len(replay_rows)
        replay_note = "" if len(replay_rows) >= 3 else f"n={len(replay_rows)}_below_3"
        replay = Metric("replay_success_rate", replay_success, "%", replay_note)
        replay_extra = f"{replayed}/{len(replay_rows)} replayed"
    else:
        replay = Metric("replay_success_rate", None, note="no_replay_rows")
        replay_extra = ""

    if len(run_summaries) < 2:
        consistency = Metric(
            "multi_run_consistency",
            None,
            note="requires_at_least_2_runs" if run_summaries else "no_run_summaries",
        )
    else:
        keys = set(run_summaries[0])
        for summary in run_summaries[1:]:
            keys &= set(summary)
        comparable = sorted(key for key in keys if _optional_float(run_summaries[0].get(key)) is not None)
        if not comparable:
            consistency = Metric("multi_run_consistency", None, note="no_comparable_metrics")
        else:
            stable = sum(
                1
                for key in comparable
                if all(
                    _close(_optional_float(run_summaries[0].get(key)), _optional_float(summary.get(key)), tolerance)
                    for summary in run_summaries[1:]
                )
            )
            note = "" if len(run_summaries) >= 3 else f"n={len(run_summaries)}_below_3"
            consistency = Metric("multi_run_consistency", stable / len(comparable), "%", note)

    if replay_extra:
        replay = Metric(replay.name, replay.value, replay.unit, (replay.note + " " + replay_extra).strip())
    return {"replay_success_rate": replay, "multi_run_consistency": consistency}


def cost_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Metric]:
    """Latency (mean + p95 in the note) and token cost."""
    if not rows:
        return {
            "latency_seconds": Metric("latency_seconds", None, note="no_cost_rows"),
            "token_cost": Metric("token_cost", None, note="no_cost_rows"),
        }
    latencies = sorted(float(row.get("latency_seconds", 0.0) or 0.0) for row in rows)
    mean = sum(latencies) / len(latencies)
    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]
    currencies = {str(row.get("cost_currency", "USD")) for row in rows}
    currency = currencies.pop() if len(currencies) == 1 else "MIXED"
    total = sum(float(row.get("cost", row.get("cost_usd", 0.0)) or 0.0) for row in rows)
    return {
        "latency_seconds": Metric("latency_seconds", mean, "seconds", f"p95={p95:.4f}s"),
        "token_cost": Metric("token_cost", total, currency, f"n={len(rows)}"),
    }


def adversarial_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Metric]:
    """FinFuzz integration (CARD-26): mutation detection P/R/F1 + FPR.

    Each row: ``{error_type, planted: bool, detected: bool}``. ``per-error-type``
    recall is reported in the note so a strong average cannot hide a blind spot.
    """
    if not rows:
        return {
            name: Metric(name, None, note="no_adversarial_rows")
            for name in ADVERSARIAL_METRICS
        }
    tp = sum(1 for row in rows if bool(row.get("planted")) and bool(row.get("detected")))
    fn = sum(1 for row in rows if bool(row.get("planted")) and not bool(row.get("detected")))
    fp = sum(1 for row in rows if not bool(row.get("planted")) and bool(row.get("detected")))
    tn = sum(1 for row in rows if not bool(row.get("planted")) and not bool(row.get("detected")))
    precision, recall, f1 = prf1(tp, fp, fn)
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    per_type: Dict[str, List[int]] = {}
    for row in rows:
        if not bool(row.get("planted")):
            continue
        bucket = per_type.setdefault(str(row.get("error_type", "unknown")), [0, 0])
        bucket[0] += int(bool(row.get("detected")))
        bucket[1] += 1
    detail = " ".join(
        f"{name}={hits}/{total}"
        for name, (hits, total) in sorted(per_type.items())
    )
    return {
        "mutation_detection_precision": Metric("mutation_detection_precision", precision, "%"),
        "mutation_detection_recall": Metric("mutation_detection_recall", recall, "%", detail),
        "mutation_detection_f1": Metric("mutation_detection_f1", f1, "%"),
        "mutation_detection_false_positive_rate": Metric(
            "mutation_detection_false_positive_rate", fpr, "%"
        ),
    }


# --------------------------------------------------------------------------- #
# Tier evaluation
# --------------------------------------------------------------------------- #
def evaluate_tier(payload: TierPayload, *, tolerance: float = TOLERANCE_DEFAULT) -> Dict[str, Any]:
    """Run every applicable calculator over one tier and return its metrics."""
    metrics: Dict[str, Metric] = {}
    metrics.update(extraction_metrics(payload.labels, payload.predictions, tolerance=tolerance))
    metrics.update(citation_metrics(payload.labels, payload.predictions))
    metrics.update(calculation_metrics(payload.calculations, tolerance=tolerance))
    metrics.update(checker_metrics(payload.checker_rows))
    metrics.update(agent_metrics(payload.agent_rows))
    metrics.update(unsafe_conclusion_leakage(payload.sentences))
    metrics.update(replay_metrics(payload.replay_rows, payload.run_summaries, tolerance=tolerance))
    metrics.update(cost_metrics(payload.cost_rows))
    metrics.update(adversarial_metrics(payload.adversarial_rows))
    return tier_report(
        payload.tier,
        metrics,
        case_count=payload.case_count or max(len(payload.labels), len(payload.predictions)),
        notes=payload.notes,
    )


def tier_report(
    tier: str,
    metrics: Mapping[str, Metric],
    *,
    case_count: int = 0,
    notes: Sequence[str] = (),
    sources: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Shape a computed metric mapping into the report's tier object."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    groups: Dict[str, Dict[str, Any]] = {}
    for group, names in METRIC_GROUPS.items():
        groups[group] = {
            name: {**metrics[name].to_dict(), "source": (sources or {}).get(name, "computed")}
            for name in names
            if name in metrics
        }
    groups["adversarial"] = {
        name: {**metrics[name].to_dict(), "source": (sources or {}).get(name, "computed")}
        for name in ADVERSARIAL_METRICS
        if name in metrics
    }
    measured = sum(1 for metric in metrics.values() if metric.available)
    return {
        "tier": tier,
        "case_count": case_count,
        "metrics": {name: metric.to_dict() for name, metric in metrics.items()},
        "groups": groups,
        "measured_metrics": measured,
        "unmeasured_metrics": len(metrics) - measured,
        "notes": [str(item) for item in notes],
    }


# --------------------------------------------------------------------------- #
# Component harmonisation
# --------------------------------------------------------------------------- #
#: Which module already measures which cn_agent_v2 metric. Components publish
#: their own baselines (CARD-01 extraction, CARD-05 checker, ...); this table is
#: the single place that knows how to read them, so no metric is ever computed
#: twice from two different definitions.
COMPONENT_SOURCES: Dict[str, Tuple[str, ...]] = {
    "card01_document_extraction": (
        "extraction_precision",
        "extraction_recall",
        "extraction_f1",
        "page_attribution_accuracy",
    ),
    "card05_report_checker": (
        "checker_precision",
        "checker_recall",
        "checker_f1",
        "checker_false_positive_rate",
    ),
}


def component_metrics(component: str, payload: Mapping[str, Any]) -> Dict[str, Metric]:
    """Read one component's published baseline into cn_agent_v2 metrics.

    Two shapes are accepted, and nothing else is guessed at:

    * a payload already keyed by cn_agent_v2 metric ids (``metrics`` mapping);
      this is the path CARD-03 / CARD-09 / CARD-10 use when they publish;
    * the two legacy component shapes below, whose field names differ enough to
      need an explicit reader.
    """
    if component == "card01_document_extraction":
        return _read_extraction_baseline(payload)
    if component == "card05_report_checker":
        return _read_checker_baseline(payload)
    return _read_native_baseline(payload)


def _read_native_baseline(payload: Mapping[str, Any]) -> Dict[str, Metric]:
    metrics: Dict[str, Metric] = {}
    raw = payload.get("metrics", payload)
    if not isinstance(raw, Mapping):
        return metrics
    for name in (*BASE_METRICS, *ADVERSARIAL_METRICS):
        entry = raw.get(name)
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            metrics[name] = Metric(
                name,
                _optional_float(entry.get("value")),
                str(entry.get("unit", "") or ""),
                str(entry.get("note", "") or ""),
            )
        else:
            metrics[name] = Metric(name, _optional_float(entry))
    return metrics


def _read_extraction_baseline(payload: Mapping[str, Any]) -> Dict[str, Metric]:
    """``output/benchmark/cn_agent_v2/document_extraction_baseline.json``."""
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return {}
    extraction = metrics.get("extraction")
    micro = extraction.get("micro") if isinstance(extraction, Mapping) else None
    # A real-model / real-document payload must never be folded in silently, so
    # the note travels with the number all the way into the report.
    note = "" if payload.get("synthetic", True) else "non_synthetic_component"
    out: Dict[str, Metric] = {}
    if isinstance(micro, Mapping):
        out["extraction_precision"] = Metric("extraction_precision", _optional_float(micro.get("precision")), "%", note)
        out["extraction_recall"] = Metric("extraction_recall", _optional_float(micro.get("recall")), "%", note)
        out["extraction_f1"] = Metric("extraction_f1", _optional_float(micro.get("f1")), "%", note)
    if metrics.get("page_attribution_accuracy") is not None:
        out["page_attribution_accuracy"] = Metric(
            "page_attribution_accuracy",
            _optional_float(metrics.get("page_attribution_accuracy")),
            "%",
            note,
        )
    return out


def _read_checker_baseline(payload: Mapping[str, Any]) -> Dict[str, Metric]:
    """``output/benchmark/cn_agent_v2/checker_baseline.json``."""
    out: Dict[str, Metric] = {}
    mapping = {
        "checker_precision": "precision",
        "checker_recall": "recall",
        "checker_f1": "f1",
        "checker_false_positive_rate": "false_positive_rate",
    }
    for metric, field in mapping.items():
        if payload.get(field) is not None:
            out[metric] = Metric(metric, _optional_float(payload.get(field)), "%")
    return out


def merge_components(components: Mapping[str, Mapping[str, Any]]) -> Tuple[Dict[str, Metric], Dict[str, str]]:
    """Merge component baselines into one metric mapping + a source table."""
    metrics: Dict[str, Metric] = {}
    sources: Dict[str, str] = {}
    for component, payload in components.items():
        for name, metric in component_metrics(component, payload).items():
            if name in metrics and sources.get(name) != component:
                # Two components claiming the same metric is a definition
                # conflict, not a merge: report it instead of letting the last
                # writer win silently.
                raise ValueError(
                    f"{name} is produced by both {sources[name]!r} and {component!r}; "
                    "one metric must have exactly one definition."
                )
            metrics[name] = metric
            sources[name] = component
    return metrics, sources


def unmeasured_metrics(measured: Mapping[str, Metric], *, note: str) -> Dict[str, Metric]:
    """Every metric the given components did not measure, marked N/A with a reason."""
    return {
        name: Metric(name, None, note=note)
        for name in (*BASE_METRICS, *ADVERSARIAL_METRICS)
        if name not in measured
    }


# --------------------------------------------------------------------------- #
# Environment fingerprint (card §10)
# --------------------------------------------------------------------------- #
def environment_fingerprint(extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A reproducible anchor for "which code, which runtime, which day".

    ``git_commit`` is read from the environment only. A missing commit is
    recorded as ``"unknown"`` — never invented (the same rule CARD-09 applies to
    ``code_version``).
    """
    fingerprint: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor_count": os.cpu_count(),
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "git_commit": os.environ.get("FITRACE_GIT_COMMIT") or os.environ.get("GIT_COMMIT") or "unknown",
    }
    if extra:
        fingerprint.update({str(key): value for key, value in extra.items()})
    return fingerprint


def fingerprint_digest(fingerprint: Mapping[str, Any]) -> str:
    """Stable short digest over a fingerprint (key order independent)."""
    digest = hashlib.sha256()
    for key in sorted(fingerprint):
        digest.update(f"{key}={fingerprint[key]}".encode("utf-8"))
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Isolation (card §10)
# --------------------------------------------------------------------------- #
class BenchmarkIsolationError(RuntimeError):
    """Raised when synthetic and real-tier results would be mixed."""


def _is_real_directory(part: str) -> bool:
    """The naming discipline, tested per path segment.

    A *segment* must be ``benchmark_real`` or start with ``real_``. Matching the
    prefix anywhere in the full path would be satisfied by any parent directory
    that merely contains the substring — which is how a check like this quietly
    stops checking anything.
    """
    return part == "benchmark_real" or part.startswith(REAL_FILENAME_PREFIX)


def isolate_payload(payload: Mapping[str, Any], *, output_dir: Path | str) -> Dict[str, Any]:
    """Refuse to write a report whose tier does not match its directory.

    The rule is mechanical because the alternative — trusting a filename prefix
    by convention — is exactly how a dry-run number ends up in a headline.
    """
    tier = str(payload.get("tier", ""))
    if tier not in TIERS:
        raise BenchmarkIsolationError(f"payload has an unknown tier: {tier!r}")
    directory = Path(output_dir)
    real_segments = [part for part in directory.parts if _is_real_directory(part)]
    if tier in (TIER_REAL, TIER_HOLDOUT):
        if not real_segments:
            raise BenchmarkIsolationError(
                f"{tier} results must live under a '*{REAL_FILENAME_PREFIX}*' or "
                f"'benchmark_real' directory (got {directory}) — otherwise a measured real "
                "number is indistinguishable from a synthetic one."
            )
    elif real_segments:
        raise BenchmarkIsolationError(
            f"synthetic results must not be written under {real_segments[0]!r} (got {directory})"
        )
    return dict(payload)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
#: Default reason recorded for a metric no component measured. It is deliberately
#: explicit: an offline report must never look like a complete measurement.
UNMEASURED_OFFLINE = "not_measured_by_any_component"


def build_report(
    tier_payloads: Sequence[TierPayload] = (),
    *,
    components: Optional[Mapping[str, Mapping[str, Any]]] = None,
    component_case_count: int = 0,
    ablation_runs: Optional[Mapping[str, Mapping[str, Any]]] = None,
    extra_fingerprint: Optional[Mapping[str, Any]] = None,
    tolerance: float = TOLERANCE_DEFAULT,
    unmeasured_note: str = UNMEASURED_OFFLINE,
) -> Dict[str, Any]:
    """Assemble the JSON report for one or more tiers.

    ``components`` folds the per-card baselines that already exist (CARD-01
    extraction, CARD-05 checker, and anything publishing cn_agent_v2 metric ids)
    into the synthetic tier. Merging is strict: two components claiming the same
    metric is an error, because a metric with two definitions has none.

    ``ablation_runs`` maps an ``ABLATION_COMPARISONS`` entry's ``treatment`` (or
    ``baseline``) to a captured summary. Anything not supplied is reported as
    ``TARGET / TO BE MEASURED`` — the card forbids inventing an ablation result.
    """
    tiers = [evaluate_tier(payload, tolerance=tolerance) for payload in tier_payloads]

    if components:
        if any(item["tier"] == TIER_SYNTHETIC for item in tiers):
            raise ValueError(
                "synthetic tier supplied twice (a synthetic TierPayload and component baselines)"
            )
        measured, sources = merge_components(components)
        merged = {**unmeasured_metrics(measured, note=unmeasured_note), **measured}
        tiers.append(
            tier_report(
                TIER_SYNTHETIC,
                merged,
                case_count=component_case_count,
                notes=[f"component:{name}" for name in sorted(components)],
                sources=sources,
            )
        )

    if not tiers:
        raise ValueError("at least one tier payload or component baseline is required")
    tiers.sort(key=lambda item: TIERS.index(item["tier"]))

    return {
        "suite_id": SUITE_ID,
        "suite_version": SUITE_VERSION,
        "environment": environment_fingerprint(extra_fingerprint),
        "tiers": tiers,
        "base_metric_count": len(BASE_METRICS),
        "metric_groups": {group: list(names) for group, names in METRIC_GROUPS.items()},
        "thresholds": dict(THRESHOLDS),
        "threshold_status": "UNFROZEN" if any(value is None for value in THRESHOLDS.values()) else "FROZEN",
        "ablation": build_ablation_table(ablation_runs or {}),
    }


def build_ablation_table(runs: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The card §7b table — with un-run comparisons left explicitly unmeasured."""
    table: List[Dict[str, Any]] = []
    for comparison in ABLATION_COMPARISONS:
        baseline = runs.get(comparison["baseline"])
        treatment = runs.get(comparison["treatment"])
        entry = dict(comparison)
        entry["baseline_value"] = _metric_from_summary(baseline, comparison["metric"])
        entry["treatment_value"] = _metric_from_summary(treatment, comparison["metric"])
        entry["status"] = (
            "MEASURED"
            if entry["baseline_value"] is not None and entry["treatment_value"] is not None
            else NOT_MEASURED
        )
        table.append(entry)
    return table


def _metric_from_summary(summary: Optional[Mapping[str, Any]], metric: str) -> Optional[float]:
    if not isinstance(summary, Mapping):
        return None
    direct = summary.get(metric)
    if direct is not None:
        return _optional_float(direct)
    metrics = summary.get("metrics")
    if isinstance(metrics, Mapping):
        entry = metrics.get(metric)
        if isinstance(entry, Mapping):
            return _optional_float(entry.get("value"))
        return _optional_float(entry)
    return None


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """Human-readable report. Includes the environment fingerprint on page one."""
    lines: List[str] = []
    lines.append(f"# Benchmark 报告 · {report.get('suite_id')} v{report.get('suite_version')}")
    lines.append("")
    environment = report.get("environment", {})
    lines.append(
        f"- 环境指纹：`{fingerprint_digest(environment)}` ｜ Python {environment.get('python')} "
        f"｜ 平台 {environment.get('platform')} ｜ git {environment.get('git_commit')}"
    )
    lines.append(
        f"- 指标数：{report.get('base_metric_count')}（基准）+ FinFuzz 对抗组 ｜ "
        f"阈值状态：**{report.get('threshold_status')}**"
    )
    lines.append("")
    for tier in report.get("tiers", []):
        lines.append(f"## 测试集：{tier['tier']}（{tier['case_count']} 例）")
        lines.append("")
        lines.append(f"已测指标 {tier['measured_metrics']} 项 / 未测 {tier['unmeasured_metrics']} 项。")
        lines.append("")
        for group, metrics in tier.get("groups", {}).items():
            lines.append(f"### {group}")
            lines.append("")
            lines.append("| 指标 | 值 | 说明 |")
            lines.append("|---|---|---|")
            for name, payload in metrics.items():
                metric = Metric(str(name), payload.get("value"), str(payload.get("unit", "")), str(payload.get("note", "")))
                lines.append(f"| {name} | {metric.render()} | {payload.get('note') or '—'} |")
            lines.append("")
        for note in tier.get("notes", []):
            lines.append(f"- {note}")
        if tier.get("notes"):
            lines.append("")

    lines.append("## Ablation（创新有效性）")
    lines.append("")
    lines.append("| 创新 | Baseline | 实验组 | 比较指标 | Baseline 值 | 实验组值 | 状态 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in report.get("ablation", []):
        lines.append(
            f"| {row['innovation']} | {row['baseline']} | {row['treatment']} | {row['metric']} | "
            f"{_fmt(row['baseline_value'])} | {_fmt(row['treatment_value'])} | {row['status']} |"
        )
    lines.append("")
    lines.append("## 阈值（冻结流程：baseline → 冻结 → 版本化）")
    lines.append("")
    lines.append("| 指标 | 目标值 |")
    lines.append("|---|---|")
    for name, value in sorted(report.get("thresholds", {}).items()):
        lines.append(f"| {name} | {NOT_MEASURED if value is None else value} |")
    return "\n".join(lines).rstrip() + "\n"


def _fmt(value: Any) -> str:
    return NOT_MEASURED if value is None else f"{float(value):.4f}"


def freeze_thresholds(
    baseline: Mapping[str, Any], *, margin: float = 0.0
) -> Dict[str, Optional[float]]:
    """Derive thresholds from a *measured* baseline (card §15, last bullet).

    For "higher is better" metrics the threshold is ``baseline - margin``; for
    cost-style metrics it is ``baseline + margin``. A metric absent from the
    baseline stays ``None`` — an unfrozen threshold is honest, a guessed one is
    not.
    """
    frozen: Dict[str, Optional[float]] = {}
    metrics = baseline.get("metrics", baseline)
    if not isinstance(metrics, Mapping):
        raise ValueError("baseline must expose a metrics mapping")
    for name in THRESHOLDS:
        entry = metrics.get(name)
        value = _optional_float(entry.get("value") if isinstance(entry, Mapping) else entry)
        if value is None:
            frozen[name] = None
        elif name in LOWER_IS_BETTER:
            frozen[name] = value + margin
        else:
            frozen[name] = max(0.0, value - margin)
    return frozen


def write_report(output_dir: Path | str, report: Mapping[str, Any]) -> Dict[str, Path]:
    """Write ``report.json`` + ``report.md``, after the isolation check."""
    tiers = report.get("tiers") or []
    if len(tiers) == 1:
        isolate_payload({"tier": tiers[0]["tier"]}, output_dir=output_dir)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_report_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


# --------------------------------------------------------------------------- #
# Disk loading (synthetic is offline; real/holdout are integration-gated)
# --------------------------------------------------------------------------- #
def load_tier_payload(path: Path | str, *, tier: str) -> TierPayload:
    """Load one tier's captured rows from a JSON file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return tier_payload_from_dict(payload, tier=tier)


def tier_payload_from_dict(payload: Mapping[str, Any], *, tier: str) -> TierPayload:
    return TierPayload(
        tier=tier,
        labels=[TruthLabel.from_dict(item) for item in (payload.get("labels") or ())],
        predictions=[Prediction.from_dict(item) for item in (payload.get("predictions") or ())],
        calculations=list(payload.get("calculations") or ()),
        checker_rows=list(payload.get("checker_rows") or ()),
        agent_rows=list(payload.get("agent_rows") or ()),
        sentences=list(payload.get("sentences") or ()),
        replay_rows=list(payload.get("replay_rows") or ()),
        run_summaries=list(payload.get("run_summaries") or ()),
        cost_rows=list(payload.get("cost_rows") or ()),
        adversarial_rows=list(payload.get("adversarial_rows") or ()),
        case_count=int(payload.get("case_count", 0) or 0),
        notes=[str(item) for item in (payload.get("notes") or ())],
    )
