"""CARD-26 FinFuzz — spec generation, execution and scoring (§7, §8).

The three public entry points are the card's, verbatim::

    generate_mutation_specs(claims, operators, seed=42)
    run_suite(specs, targets)
    compute_metrics(results)

plus :func:`run_finfuzz`, which chains them.

Two design decisions are worth stating because they are what make the numbers
mean something.

**One outcome becomes up to two specs, one per channel.** The same tamper asked
at claim level ("can the checker see this?") and at text level ("does the tamper
even survive extraction?") are different experiments with different answers, and
§7 requires both answers to be separately visible. They are separate specs rather
than one spec with two flags so a caller can run just one of them.

**A source claim a target already objects to is skipped, and the skip is
recorded.** Attribution is the entire point of the report: if the unmutated claim
already drew a finding, then a finding on the mutated one says nothing about the
tamper. §11's "generation failed → skip and record" is the right home for that,
and the skipped list keeps it from looking like coverage.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.cn.checker.facts import FactIndex
from src.cn.checker.models import CheckType, DraftClaim
from src.cn.checker.rule_extractor import extract_claims_rules
from src.cn.checker.sentences import DraftSentence

from .models import (
    CHANNEL_CLAIM,
    CHANNEL_TEXT,
    EXPECTED_CHECK_TYPES,
    FAILURE_CHECKER,
    FAILURE_EXTRACTOR,
    FinFuzzError,
    FinFuzzReport,
    MutationOperators,
    MutationResult,
    MutationSpec,
    NOT_APPLICABLE,
    NOT_MEASURED,
    OPERATOR_ORDER,
    prf1,
)
from .operators import OPERATORS, OperatorContext
from .targets import DetectionTarget, TARGET_NAMES

DEFAULT_SEED = 42

#: How many specs one operator may contribute per source claim. ``None`` means
#: every applicable variant (the deterministic default). A cap exists because §8's
#: signature takes a ``seed``, and a seed is only meaningful if something is
#: sampled; the cap is applied by a seeded shuffle so the subset is reproducible.
DEFAULT_MAX_PER_OPERATOR: Optional[int] = None


def _mutation_id(operator: str, channel: str, claim_id: str, ordinal: int) -> str:
    """A stable id.

    Derived from the operator, channel, claim and ordinal — never from a counter
    that depends on iteration order or the wall clock, so two runs of the same
    corpus produce the same ids and a diff between reports is readable.
    """
    return f"{operator}:{channel}:{claim_id}:{ordinal}"


# --------------------------------------------------------------------------- #
# generation (§8)
# --------------------------------------------------------------------------- #
@dataclass
class GenerationOutcome:
    """Specs plus everything that was deliberately not turned into one."""

    specs: List[MutationSpec] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)


def generate_mutation_specs(
    claims: Sequence[DraftClaim],
    operators: Optional[Iterable[str]] = None,
    *,
    seed: int = DEFAULT_SEED,
    context: Optional[OperatorContext] = None,
    targets: Optional[Mapping[str, DetectionTarget]] = None,
    max_per_operator: Optional[int] = DEFAULT_MAX_PER_OPERATOR,
) -> GenerationOutcome:
    """§8 — build one spec per applicable (claim, operator, channel).

    ``targets`` is optional and only used as a guard: when supplied, a claim that a
    target already objects to contributes no spec for that target's benefit, and
    the refusal is recorded in ``skipped``. Without it the caller is asserting the
    corpus is clean, which :func:`src.cn.finfuzz.corpus.load_clean_claims` verifies
    before handing it over.
    """
    selected = tuple(operators) if operators else OPERATOR_ORDER
    unknown = [name for name in selected if name not in OPERATORS]
    if unknown:
        raise FinFuzzError(f"unknown operator(s): {sorted(unknown)}")
    op_ctx = context or OperatorContext(index=FactIndex(()))

    blocked: Dict[str, Tuple[str, ...]] = {}
    if targets:
        for name, target in targets.items():
            for claim in claims:
                if target.detect([claim]).detected:
                    blocked.setdefault(claim.claim_id, ())
                    blocked[claim.claim_id] = (*blocked[claim.claim_id], name)

    outcome = GenerationOutcome()
    rng = random.Random(seed)
    seen: Dict[Tuple[str, str, str], str] = {}

    for operator in selected:
        produced: List[MutationSpec] = []
        for claim in claims:
            if blocked.get(claim.claim_id):
                outcome.skipped.append({
                    "claim_id": claim.claim_id,
                    "operator": operator,
                    "reason": "target_already_objects:" + ",".join(blocked[claim.claim_id]),
                })
                continue
            for ordinal, built in enumerate(OPERATORS[operator](claim, op_ctx)):
                produced.extend(_specs_for(operator, claim, built, ordinal))
        if not produced:
            outcome.skipped.append({
                "claim_id": "",
                "operator": operator,
                "reason": "no_applicable_variant",
            })
            continue
        if max_per_operator is not None and len(produced) > max_per_operator:
            kept = sorted(rng.sample(range(len(produced)), max_per_operator))
            outcome.skipped.append({
                "claim_id": "",
                "operator": operator,
                "reason": f"sampled:{len(produced)}->{max_per_operator}",
            })
            produced = [produced[index] for index in kept]
        kept_specs, dropped = _dedupe(produced, seen)
        if dropped:
            outcome.skipped.append({
                "claim_id": "",
                "operator": operator,
                "reason": f"duplicate_mutation:{dropped}",
            })
        outcome.specs.extend(kept_specs)

    outcome.specs.sort(key=lambda spec: spec.mutation_id)
    return outcome


def _payload_key(spec: MutationSpec) -> Tuple[str, str, str]:
    """Identity of a tamper: same claim, same channel, same resulting claim.

    §12.3 asks that mutating one claim twice be de-duplicated. Keying on the
    *result* rather than on the mutation id is the part that matters: two operators
    can be configured to produce the identical tampered claim, and counting that as
    two samples would inflate the per-error-type denominator with a copy.
    """
    payload = json.dumps(spec.mutated_claim, ensure_ascii=False, sort_keys=True, default=str)
    return (spec.channel, spec.claim_id, payload)


def _dedupe(
    produced: Sequence[MutationSpec], seen: Dict[Tuple[str, str, str], str]
) -> Tuple[List[MutationSpec], int]:
    kept: List[MutationSpec] = []
    dropped = 0
    for spec in produced:
        key = _payload_key(spec)
        if key in seen:
            dropped += 1
            continue
        seen[key] = spec.mutation_id
        kept.append(spec)
    return kept, dropped


def combine_specs(first: MutationSpec, second: MutationSpec) -> MutationSpec:
    """§12.2 — fold two tampers on one claim into a labelled combination.

    The result keeps ``first``'s payload as its base, so the diff stays readable,
    and records both operators. It exists so a combination can be *scored and
    labelled* instead of being indistinguishable from the single-operator sample it
    would otherwise look like. Combining across claims or channels is refused — that
    would not be one tamper any more, and the report's per-claim attribution would
    have nothing to attach to.
    """
    if first.claim_id != second.claim_id:
        raise FinFuzzError("combine_specs: both specs must target the same claim")
    if first.channel != second.channel:
        raise FinFuzzError("combine_specs: both specs must use the same channel")
    merged = dict(first.mutated_claim)
    for key, value in second.mutated_claim.items():
        if merged.get(key) in (None, (), []):
            merged[key] = value
    expected = tuple(sorted(set(first.expected_check_types) | set(second.expected_check_types)))
    return MutationSpec(
        mutation_id=f"{first.mutation_id}+{second.operator}",
        operator=first.operator,
        source_claim=dict(first.source_claim),
        mutated_claim=merged,
        description=f"{first.description}；叠加 {second.operator}：{second.description}",
        expected_detection=first.expected_detection and second.expected_detection,
        channel=first.channel,
        expected_check_types=expected,
        source_sentence=first.source_sentence,
        text_edits=tuple(first.text_edits) + tuple(second.text_edits),
        claim_id=first.claim_id,
        note="combination",
        combination=tuple(sorted({*first.operators, *second.operators})),
    )


def _specs_for(operator: str, claim: DraftClaim, built: Any, ordinal: int) -> List[MutationSpec]:
    """Turn one :class:`OperatorOutcome` into its claim- and/or text-channel specs."""
    source = claim.to_dict()
    mutated = built.mutated_claim.to_dict()
    diff = {
        key for key in set(source) | set(mutated) if source.get(key) != mutated.get(key)
    }
    specs: List[MutationSpec] = []

    if diff:
        specs.append(MutationSpec(
            mutation_id=_mutation_id(operator, CHANNEL_CLAIM, claim.claim_id, ordinal),
            operator=operator,
            source_claim=source,
            mutated_claim=mutated,
            description=built.description,
            expected_detection=not built.equivalent,
            channel=CHANNEL_CLAIM,
            expected_check_types=() if built.equivalent else EXPECTED_CHECK_TYPES[operator],
            source_sentence=claim.sentence,
            text_edits=(),
            claim_id=claim.claim_id,
            note=built.note,
        ))
    if built.text_edits:
        specs.append(MutationSpec(
            mutation_id=_mutation_id(operator, CHANNEL_TEXT, claim.claim_id, ordinal),
            operator=operator,
            source_claim=source,
            mutated_claim=mutated,
            description=built.description + "（文本通道）",
            expected_detection=not built.equivalent,
            channel=CHANNEL_TEXT,
            expected_check_types=() if built.equivalent else EXPECTED_CHECK_TYPES[operator],
            source_sentence=claim.sentence,
            text_edits=built.text_edits,
            claim_id=claim.claim_id,
            note=built.note,
        ))
    return specs


def apply_text_edits(sentence: str, edits: Sequence[Tuple[str, str]]) -> Tuple[str, bool]:
    """Apply the tamper to the sentence. Returns ``(text, applied_any)``."""
    text = sentence
    applied = False
    for old, new in edits:
        if old and old in text:
            text = text.replace(old, new, 1)
            applied = True
    return text, applied


def extract_from_sentence(document_id: str, sentence: str, *, page: Optional[int] = None) -> List[DraftClaim]:
    """Run the deterministic extractor over one mutated sentence.

    The sentence is re-wrapped in the CARD-05 ``DraftSentence`` shape rather than
    calling the extractor with raw text, because the extractor's contract is
    sentence-in / claim-out and the wrapper is what carries the page and the id a
    finding is later joined on.
    """
    wrapped = DraftSentence(
        sentence_id=f"{document_id or 'finfuzz'}#s0001",
        document_id=document_id or "finfuzz",
        text=sentence,
        page=page,
        index=0,
    )
    return list(extract_claims_rules([wrapped]).claims)


def _carried(extracted: DraftClaim, spec: MutationSpec) -> bool:
    """Did the tamper survive extraction?

    Checked field by field against the *diff* the spec recorded, so the test is
    exactly "the mutated fields came out of the extractor changed". An equivalent
    spec has an empty diff, and vacuous truth is the correct answer there: nothing
    was supposed to change, and nothing did.
    """
    mutated = spec.mutated_claim
    for key in spec.diff():
        if _jsonable(getattr(extracted, key, None)) != _jsonable(mutated.get(key)):
            return False
    return True


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value


# --------------------------------------------------------------------------- #
# execution (§8)
# --------------------------------------------------------------------------- #
def run_suite(
    specs: Sequence[MutationSpec],
    targets: Mapping[str, DetectionTarget],
    *,
    clock: Any = None,
) -> List[MutationResult]:
    """§8 — point every spec at every target and record what happened.

    ``clock`` defaults to :func:`time.perf_counter`; it exists so a test can pin
    the timing field. Timing never reaches a metric or the report — §18 requires
    the report to be reproducible, and a wall-clock value in it would not be.
    """
    if not targets:
        raise FinFuzzError("run_suite needs at least one target")
    ordered = [name for name in TARGET_NAMES if name in targets]
    ordered += [name for name in sorted(targets) if name not in ordered]
    now = clock or time.perf_counter

    results: List[MutationResult] = []
    for spec in specs:
        results.append(_run_one(spec, targets, ordered, now))
    return results


def _run_one(
    spec: MutationSpec,
    targets: Mapping[str, DetectionTarget],
    ordered: Sequence[str],
    now: Any,
) -> MutationResult:
    notes: List[str] = []
    if spec.note:
        notes.extend(part for part in spec.note.split("；") if part)

    claims: List[DraftClaim]
    extracted: Optional[DraftClaim] = None
    carried: Optional[bool] = None

    if spec.channel == CHANNEL_CLAIM:
        claims = [claim_from_payload({**_claim_kwargs(), **spec.mutated_claim})]
    else:
        text, applied = apply_text_edits(spec.source_sentence, spec.text_edits)
        if not applied:
            # Should be unreachable: generation only offers verified edits. Kept as
            # a hard error rather than a quiet pass, because a text spec that does
            # not edit anything would be scored as a checker miss.
            raise FinFuzzError(f"{spec.mutation_id}: text channel produced no change")
        document_id = str(spec.source_claim.get("document_id") or "")
        page = spec.source_claim.get("page")
        produced = extract_from_sentence(document_id, text, page=page)
        if not produced:
            notes.append("extractor_returned_no_claim")
            return MutationResult(
                spec=spec,
                detected=False,
                failure_stage=FAILURE_EXTRACTOR,
                extraction_carried=False,
                notes=tuple(notes),
            )
        extracted = produced[0]
        carried = _carried(extracted, spec)
        claims = [extracted]
        if not carried:
            notes.append("tamper_not_present_in_extracted_claim")

    start = now()
    observations: List[Tuple[str, Any]] = []
    for name in ordered:
        observations.append((name, targets[name].detect(claims)))
    elapsed_ms = max(0, int(round((now() - start) * 1000)))

    observed = tuple(sorted({item for _, detection in observations for item in detection.check_types}))
    fired = [(name, detection) for name, detection in observations if detection.detected]

    # ``detected`` is simply "some detector objected". That is only meaningful
    # because spec generation refuses to mutate a claim any target already objects
    # to — with a clean baseline, an objection on the mutated twin *is* caused by
    # the tamper. Scoping detection to the expected check types instead would have
    # hidden every structurally-visible tamper behind the checker's vocabulary.
    detected = bool(fired)

    matched = sorted(set(observed) & set(spec.expected_check_types))
    if spec.expected_detection and detected and not matched:
        notes.append("detection_unclassified:" + ",".join(observed))
    classified = (matched[0] if matched else (observed[0] if observed else None))

    if detected:
        detector = fired[0][0]
        return MutationResult(
            spec=spec,
            detected=True,
            detected_by=detector,
            error_type_classified=classified,
            detection_time_ms=elapsed_ms,
            observed_check_types=observed,
            extracted_claim=extracted.to_dict() if extracted is not None else None,
            extraction_carried=carried,
            detected_by_all=tuple(name for name, _ in fired),
            notes=tuple(notes),
        )

    stage = FAILURE_CHECKER
    if spec.channel == CHANNEL_TEXT and carried is False:
        stage = FAILURE_EXTRACTOR
    elif spec.channel == CHANNEL_TEXT and extracted is None:
        stage = FAILURE_EXTRACTOR
    return MutationResult(
        spec=spec,
        detected=False,
        failure_stage=stage,
        error_type_classified=None,
        detection_time_ms=elapsed_ms,
        observed_check_types=observed,
        extracted_claim=extracted.to_dict() if extracted is not None else None,
        extraction_carried=carried,
        notes=tuple(notes),
    )


def claim_from_payload(payload: Mapping[str, Any]) -> DraftClaim:
    """Rebuild a :class:`DraftClaim` from a ``to_dict()`` payload.

    ``DraftClaim.to_dict()`` publishes derived fields (``graph_claim_type``,
    ``references``) that are properties, not constructor arguments, so the payload
    is filtered against the dataclass's own field names rather than splatted. That
    keeps this working when CARD-05 adds a derived field, instead of raising
    ``TypeError`` at the first mutation.
    """
    allowed = {field.name for field in dataclass_fields(DraftClaim)}
    kwargs = {key: value for key, value in payload.items() if key in allowed}
    for key, value in list(kwargs.items()):
        if isinstance(value, list) and key in _TUPLE_FIELDS:
            kwargs[key] = tuple(value)
    return DraftClaim(**kwargs)


#: ``DraftClaim`` fields whose declared type is a tuple; JSON round-trips them to lists.
_TUPLE_FIELDS = frozenset({"source_references"})


def _claim_kwargs() -> Dict[str, Any]:
    """Fields :class:`DraftClaim` needs that a mutated payload may not carry."""
    return {"claim_id": "", "sentence": "", "source_references": ()}


# --------------------------------------------------------------------------- #
# scoring (§7)
# --------------------------------------------------------------------------- #
def compute_metrics(results: Sequence[MutationResult], *, suite_id: str = "finfuzz") -> FinFuzzReport:
    """§7 — overall and per-error-type metrics, plus the stage attribution."""
    planted = [item for item in results if item.planted]
    equivalents = [item for item in results if not item.planted]

    true_positive = sum(1 for item in planted if item.detected)
    false_negative = sum(1 for item in planted if not item.detected)
    false_positive = sum(1 for item in equivalents if item.detected)
    true_negative = sum(1 for item in equivalents if not item.detected)

    precision, recall, f1 = prf1(true_positive, false_positive, false_negative)
    denominator = false_positive + true_negative
    fpr = (false_positive / denominator) if denominator else None

    per_error_type: Dict[str, Dict[str, Any]] = {}
    for operator in OPERATOR_ORDER:
        rows = [item for item in results if item.operator == operator]
        misses = [item for item in rows if item.planted and not item.detected]
        if not rows:
            per_error_type[operator] = {
                "recall": NOT_APPLICABLE,
                "n": 0,
                "detected": 0,
                "false_positives": 0,
                "equivalent_n": 0,
                "note": "no_spec_generated",
            }
            continue
        per_op_planted = [item for item in rows if item.planted]
        per_op_equiv = [item for item in rows if not item.planted]
        hits = sum(1 for item in per_op_planted if item.detected)
        per_error_type[operator] = {
            "recall": (hits / len(per_op_planted)) if per_op_planted else NOT_APPLICABLE,
            "n": len(per_op_planted),
            "detected": hits,
            "false_positives": sum(1 for item in per_op_equiv if item.detected),
            "equivalent_n": len(per_op_equiv),
            "misses": [
                {"mutation_id": item.spec.mutation_id, "stage": item.failure_stage}
                for item in misses
            ],
        }

    stages = {FAILURE_EXTRACTOR: 0, FAILURE_CHECKER: 0}
    for item in results:
        # Only *planted* misses belong here. A 伪变异 that stayed silent is the
        # desired outcome, not a failure, and folding it in would have reported
        # every true negative as a checker miss.
        if item.detected or not item.planted:
            continue
        if item.failure_stage in stages:
            stages[item.failure_stage] += 1

    by_detector: Dict[str, int] = {name: 0 for name in TARGET_NAMES}
    for item in results:
        for name in item.detected_by_all or ((item.detected_by,) if item.detected_by else ()):
            by_detector[name] = by_detector.get(name, 0) + 1

    report = FinFuzzReport(
        suite_id=suite_id,
        overall={
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "false_positive_rate": fpr,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "n": len(results),
            "planted_n": len(planted),
            "equivalent_n": len(equivalents),
        },
        per_error_type=per_error_type,
        baseline_holdout={
            "status": NOT_MEASURED,
            "note": "holdout 子集未采集；报告不含任何 holdout 数字（§12.4）",
        },
        extractor_vs_checker={
            "detected": len(planted) - stages[FAILURE_EXTRACTOR] - stages[FAILURE_CHECKER],
            "extractor_failures": stages[FAILURE_EXTRACTOR],
            "checker_failures": stages[FAILURE_CHECKER],
            "by_detector": by_detector,
            "note": (
                "extractor = 篡改未通过抽取（文本通道），checker = 篡改已通过但无检测器命中"
            ),
        },
        rows=[item.to_adversarial_row() for item in results],
        counts={
            "specs": len(results),
            "operators": len({item.operator for item in results}),
            "equivalent": len(equivalents),
            # §12.2 — a combination is scored under its primary operator but counted
            # here, so "8 operators ran" and "2 of those samples were actually
            # multi-operator tampers" are both visible rather than one hiding the
            # other.
            "combinations": sum(1 for item in results if item.spec.is_combination),
        },
        operators_run=tuple(sorted({item.operator for item in results})),
    )
    return report


def run_finfuzz(
    suite_id: str,
    claims: Sequence[DraftClaim],
    operators: Optional[Iterable[str]] = None,
    targets: Optional[Mapping[str, DetectionTarget]] = None,
    *,
    holdout: Sequence[Any] = (),
    seed: int = DEFAULT_SEED,
    context: Optional[OperatorContext] = None,
    max_per_operator: Optional[int] = DEFAULT_MAX_PER_OPERATOR,
) -> Tuple[FinFuzzReport, GenerationOutcome]:
    """§8 — generate, run and score in one call.

    ``holdout`` is accepted for the card's §12.4 discipline (tuning must not touch
    the holdout subset). Nothing is read from it here, and the report says so
    rather than reporting an empty holdout as a perfect score.
    """
    if not targets:
        raise FinFuzzError("run_finfuzz needs targets")
    generated = generate_mutation_specs(
        claims, operators, seed=seed, context=context, targets=targets, max_per_operator=max_per_operator
    )
    results = run_suite(generated.specs, targets)
    report = compute_metrics(results, suite_id=suite_id)
    report.skipped = list(generated.skipped)
    report.seed = seed
    if holdout:
        report.notes.append(f"holdout_provided_but_not_read:{len(list(holdout))}")
    return report, generated


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_markdown(report: FinFuzzReport) -> str:
    """A readable table for the demo and the benchmark report (§16, §17)."""
    overall = report.overall

    def fmt(value: Any) -> str:
        if value is None:
            return NOT_APPLICABLE
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    lines = [
        f"# FinFuzz Report — {report.suite_id}",
        "",
        "> 合成对抗语料；**不是**真实模型评测结果（`synthetic: true`, `real_model: false`）。",
        "",
        "## Overall",
        "",
        "| metric | value |",
        "|---|---|",
        f"| Precision | {fmt(overall.get('precision'))} |",
        f"| Recall | {fmt(overall.get('recall'))} |",
        f"| F1 | {fmt(overall.get('f1'))} |",
        f"| False Positive Rate | {fmt(overall.get('false_positive_rate'))} |",
        f"| TP / FP / FN / TN | {overall.get('true_positive')} / {overall.get('false_positive')}"
        f" / {overall.get('false_negative')} / {overall.get('true_negative')} |",
        "",
        "## Per-error-type recall",
        "",
        "| operator | recall | detected / n | equivalent (FP) |",
        "|---|---|---|---|",
    ]
    for operator, row in report.per_error_type.items():
        recall = row.get("recall")
        cell = recall if isinstance(recall, str) else f"{recall:.4f}"
        lines.append(
            f"| {operator} | {cell} | {row.get('detected')} / {row.get('n')} |"
            f" {row.get('false_positives')} / {row.get('equivalent_n')} |"
        )
    stage = report.extractor_vs_checker
    lines += [
        "",
        "## Extractor vs checker",
        "",
        f"- detected: {stage.get('detected')}",
        f"- extractor-stage misses: {stage.get('extractor_failures')}",
        f"- checker-stage misses: {stage.get('checker_failures')}",
        f"- by detector: {json.dumps(stage.get('by_detector'), ensure_ascii=False, sort_keys=True)}",
        "",
        "## Holdout",
        "",
        f"- {report.baseline_holdout.get('status')} — {report.baseline_holdout.get('note')}",
        "",
    ]
    if report.skipped:
        lines += ["## Skipped", ""]
        for item in report.skipped:
            lines.append(f"- {item.get('operator')} / {item.get('claim_id') or '-'}: {item.get('reason')}")
        lines.append("")
    return "\n".join(lines)


def write_report(report: FinFuzzReport, out_dir: str | Path) -> Dict[str, str]:
    """Write ``report.json`` + ``report.md``; return the two paths."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "report.json"
    md_path = target / "report.md"
    payload = report.to_dict()
    payload["adversarial_rows"] = report.adversarial_rows()
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


__all__ = [
    "DEFAULT_SEED",
    "GenerationOutcome",
    "apply_text_edits",
    "claim_from_payload",
    "combine_specs",
    "compute_metrics",
    "extract_from_sentence",
    "generate_mutation_specs",
    "render_markdown",
    "run_finfuzz",
    "run_suite",
    "write_report",
]
