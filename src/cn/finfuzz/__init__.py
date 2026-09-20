"""CARD-26 FinFuzz — financial-semantic adversarial mutation testing.

FinFuzz is a **test framework**, not a business module: it takes correct claims,
mutates them into semantic financial errors, points the deterministic detectors at
the result, and reports per-error-type detection — with every miss attributed to
the stage that produced it.

    from src.cn.finfuzz import load_clean_claims, run_corpus, render_markdown

    cases = load_clean_claims()
    report, _ = run_corpus(cases)
    print(render_markdown(report))

Nothing here runs in the research pipeline. It is invoked from the evaluation
pipeline only (§10), and it imports no model client: mutation generation and both
detectors are deterministic, so a fixed seed reproduces a report exactly (§9).
"""

from __future__ import annotations

from .baseline import (
    BASELINE_FILENAME,
    COMPONENT,
    DISCLAIMER,
    baseline_metrics,
    baseline_payload,
    write_baseline,
)
from .corpus import (
    CORPUS_ROOT,
    DRAFTS_DIR,
    REFERENCE_DIR,
    CleanCase,
    CorpusLoad,
    clean_specs,
    load_clean_claims,
    load_corpus,
    run_corpus,
)
from .models import (
    CHANNEL_CLAIM,
    CHANNEL_TEXT,
    CHANNELS,
    DETECTED_BY_CHECKER,
    DETECTED_BY_VALIDATOR,
    EXPECTED_CHECK_TYPES,
    FAILURE_CHECKER,
    FAILURE_EXTRACTOR,
    FAILURE_STAGES,
    FinFuzzError,
    FinFuzzReport,
    MutationOperators,
    MutationResult,
    MutationSpec,
    NOT_APPLICABLE,
    NOT_MEASURED,
    OPERATOR_LABELS,
    OPERATOR_ORDER,
)
from .operators import OPERATORS, OperatorContext, OperatorOutcome
from .suite import (
    DEFAULT_SEED,
    GenerationOutcome,
    apply_text_edits,
    claim_from_payload,
    combine_specs,
    compute_metrics,
    extract_from_sentence,
    generate_mutation_specs,
    render_markdown,
    run_finfuzz,
    run_suite,
    write_report,
)
from .targets import (
    TARGET_NAMES,
    CheckerTarget,
    Detection,
    DetectionTarget,
    GraphValidatorTarget,
)

__all__ = [
    "BASELINE_FILENAME",
    "CHANNELS",
    "CHANNEL_CLAIM",
    "CHANNEL_TEXT",
    "COMPONENT",
    "CORPUS_ROOT",
    "CheckerTarget",
    "CleanCase",
    "CorpusLoad",
    "DEFAULT_SEED",
    "DETECTED_BY_CHECKER",
    "DETECTED_BY_VALIDATOR",
    "DISCLAIMER",
    "DRAFTS_DIR",
    "Detection",
    "DetectionTarget",
    "EXPECTED_CHECK_TYPES",
    "FAILURE_CHECKER",
    "FAILURE_EXTRACTOR",
    "FAILURE_STAGES",
    "FinFuzzError",
    "FinFuzzReport",
    "GenerationOutcome",
    "GraphValidatorTarget",
    "MutationOperators",
    "MutationResult",
    "MutationSpec",
    "NOT_APPLICABLE",
    "NOT_MEASURED",
    "OPERATORS",
    "OPERATOR_LABELS",
    "OPERATOR_ORDER",
    "OperatorContext",
    "OperatorOutcome",
    "REFERENCE_DIR",
    "TARGET_NAMES",
    "apply_text_edits",
    "baseline_metrics",
    "baseline_payload",
    "claim_from_payload",
    "clean_specs",
    "combine_specs",
    "compute_metrics",
    "extract_from_sentence",
    "generate_mutation_specs",
    "load_clean_claims",
    "load_corpus",
    "render_markdown",
    "run_corpus",
    "run_finfuzz",
    "run_suite",
    "write_baseline",
    "write_report",
]
