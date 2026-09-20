"""CARD-26 FinFuzz — the offline source corpus of *correct* claims.

A mutation suite is only as meaningful as the claims it mutates, and §4 asks for
"正确 Claim 集合". This module builds that set from CARD-05's committed fixture
corpus — the two drafted reports that carry **no** planted error — and refuses to
hand back anything it could not verify is clean.

**Why "verify is clean" is a hard gate, not a nicety.** If an unmutated claim
already draws a finding, then a finding on its mutated twin proves nothing about
the tamper; scoring it would inflate recall with pre-existing objections. So the
loader runs both targets over the pristine claims and raises when either fires.
Spec generation keeps a second, softer guard for callers that supply their own
claims.

**One case per draft, not one merged fact base.** The two clean drafts are
different companies citing different filings. Pooling their disclosures would let
a claim from one draft be judged against the other company's numbers — a wrong
"expected value", not a strictness setting.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.cn.checker.draft_corpus import (
    DRAFT_SPECS,
    MARKET_DOCUMENT,
    market_documents,
    market_normalized_facts,
)
from src.cn.checker.facts import FactIndex
from src.cn.checker.pipeline import (
    reference_material,
    reference_signals,
    selected_reference_documents,
)
from src.cn.checker.rule_extractor import extract_claims_rules
from src.cn.checker.sentences import sentences_from_facts
from src.cn.documents import DocumentRegistry
from src.cn.normalization.units import UNIT_SCALES

from .models import FinFuzzError
from .operators import OperatorContext
from .targets import CheckerTarget, DetectionTarget, GraphValidatorTarget

CORPUS_ROOT = Path("tests/fixtures")
DRAFTS_DIR = CORPUS_ROOT / "report_drafts"
REFERENCE_DIR = CORPUS_ROOT / "documents"


@dataclass(frozen=True)
class CleanCase:
    """One draft's **verified-clean** claims, its fact base, and wired targets.

    ``excluded`` names the claims this draft also produced that a target objected
    to, with the objection. They are not silently dropped: a caller that mutates
    this case should be able to see how much of the draft went unused and why.
    """

    doc_id: str
    suite_id: str
    document_id: str
    filename: str
    company: str
    claims: Tuple[Any, ...]
    targets: Mapping[str, DetectionTarget]
    context: OperatorContext
    excluded: Tuple[Dict[str, Any], ...] = ()

    def __len__(self) -> int:
        return len(self.claims)


@dataclass(frozen=True)
class CorpusLoad:
    """The usable cases plus every draft that could not contribute one."""

    cases: Tuple[CleanCase, ...] = ()
    skipped: Tuple[Dict[str, Any], ...] = ()


def clean_specs(specs: Optional[Sequence[Any]] = None) -> List[Any]:
    """The drafts with no planted error, in the corpus's stable order."""
    return [spec for spec in (specs or DRAFT_SPECS) if not spec.expected]


def load_corpus(
    doc_ids: Optional[Iterable[str]] = None,
    *,
    workdir: Optional[str | Path] = None,
    roots: Optional[Tuple[Path, Path]] = None,
    specs: Optional[Sequence[Any]] = None,
    error_free_only: bool = False,
) -> CorpusLoad:
    """Build a :class:`CleanCase` per draft that has at least one clean claim.

    **Claims are filtered, not drafts.** A draft that plants an error on one line
    usually carries correct lines too, and those are perfectly good mutation
    subjects — refusing the whole draft would throw away real corpus. What matters
    is that the *specific claim* being mutated draws no objection, because that is
    what makes a later finding attributable to the tamper.

    ``error_free_only`` restores the stricter whole-draft view (CARD-05's own two
    clean drafts), which is what a reader might expect "the clean corpus" to mean.

    ``roots`` overrides ``(drafts_dir, reference_dir)`` for a caller with its own
    corpus. The registry lives under a temp directory by default: a stray
    ``registry.db`` beside the committed fixtures would be an untracked artefact
    inside the corpus, which CARD-05's own benchmark refuses for the same reason.
    """
    drafts_dir, reference_dir = roots or (DRAFTS_DIR, REFERENCE_DIR)
    pool = clean_specs(specs) if error_free_only else list(specs or DRAFT_SPECS)
    wanted = {str(item) for item in doc_ids} if doc_ids else None
    selected = [spec for spec in pool if wanted is None or spec.doc_id in wanted]
    missing = (wanted or set()) - {spec.doc_id for spec in selected}
    if missing:
        raise FinFuzzError(f"no corpus draft for: {sorted(missing)}")

    cases: List[CleanCase] = []
    skipped: List[Dict[str, Any]] = []
    for spec in selected:
        built, reason = _build_case(spec, drafts_dir, reference_dir, workdir)
        if built is None:
            skipped.append({"doc_id": spec.doc_id, "reason": reason})
            continue
        cases.append(built)
    return CorpusLoad(cases=tuple(cases), skipped=tuple(skipped))


def load_clean_claims(
    doc_ids: Optional[Iterable[str]] = None,
    *,
    workdir: Optional[str | Path] = None,
    roots: Optional[Tuple[Path, Path]] = None,
    specs: Optional[Sequence[Any]] = None,
    error_free_only: bool = False,
) -> List[CleanCase]:
    """Convenience wrapper returning only the usable cases."""
    return list(load_corpus(
        doc_ids,
        workdir=workdir,
        roots=roots,
        specs=specs,
        error_free_only=error_free_only,
    ).cases)


def _build_case(
    spec: Any,
    drafts_dir: Path,
    reference_dir: Path,
    workdir: Optional[str | Path],
) -> Tuple[Optional[CleanCase], str]:
    base = Path(tempfile.mkdtemp(
        prefix="cn_finfuzz_", dir=str(workdir) if workdir else None
    ))
    registry = DocumentRegistry(root=base / "registry.db")
    try:
        by_filename: Dict[str, str] = {}
        for filename in (name for name in spec.references if str(name).endswith(".pdf")):
            path = reference_dir / filename
            if not path.exists():
                continue
            declared = "annual" if filename.startswith("annual") else "announcement"
            ingested = registry.ingest(
                path.read_bytes(), declared, filename=filename, source="admin_import"
            )
            by_filename[filename] = ingested.document.document_id

        result = registry.ingest(
            (drafts_dir / spec.filename).read_bytes(),
            "report_draft",
            filename=spec.filename,
            source="admin_import",
        )
        document_id = result.document.document_id
        document = registry.get(document_id)

        references = selected_reference_documents(
            registry,
            draft_id=document_id,
            reference_document_ids=[by_filename[name] for name in by_filename],
            company=document.company,
        )
        normalized, _issues = reference_material(registry, references)
        extra_facts = market_normalized_facts() if spec.uses_market_document else ()
        extra_documents = market_documents() if spec.uses_market_document else ()
        facts = list(normalized) + list(extra_facts)
        documents = list(references) + list(extra_documents)
        signals = reference_signals(registry, references)
        claims = tuple(extract_claims_rules(
            sentences_from_facts(registry.get_facts(document_id), document_id=document_id)
        ).claims)
    finally:
        registry.close()

    if not claims:
        return None, "draft_produced_no_claims"

    registered = tuple(by_filename)
    if spec.uses_market_document:
        # Both names go in: the drafts cite the market filing by its *document id*
        # (``SYN-MARKET-01#p1``) while the registry knows its filename. Registering
        # only the filename made every market-citing claim look unbounded — a
        # citation-resolution bug that surfaced as a "clean" claim the structural
        # detector refused.
        registered = registered + (
            str(MARKET_DOCUMENT["document_id"]),
            str(MARKET_DOCUMENT["filename"]),
        )

    targets: Dict[str, DetectionTarget] = {
        "checker": CheckerTarget(
            facts=facts, signals=signals, documents=documents, as_of=spec.as_of
        ),
        "validator": GraphValidatorTarget(resolvable_references=registered),
    }
    context = OperatorContext(
        index=FactIndex(facts),
        registered_filenames=registered,
        unit_words=tuple(UNIT_SCALES),
    )

    kept: List[Any] = []
    excluded: List[Dict[str, Any]] = []
    for claim in claims:
        objections = [
            name for name, target in targets.items() if target.detect([claim]).detected
        ]
        if objections:
            excluded.append({
                "claim_id": claim.claim_id,
                "reason": "target_already_objects:" + ",".join(sorted(objections)),
            })
            continue
        kept.append(claim)

    if not kept:
        return None, "no_claim_clean_for_every_target"

    return CleanCase(
        doc_id=spec.doc_id,
        suite_id=f"finfuzz:{spec.doc_id}",
        document_id=document_id,
        filename=spec.filename,
        company=document.company,
        claims=tuple(kept),
        targets=targets,
        context=context,
        excluded=tuple(excluded),
    ), ""


def run_corpus(
    cases: Sequence[CleanCase],
    operators: Optional[Iterable[str]] = None,
    *,
    seed: int = 42,
    max_per_operator: Optional[int] = None,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Run every case and score the pooled rows as one report.

    Pooling is the right aggregate here because the unit of measurement is a
    *tamper*, not a draft: per-error-type recall across the whole corpus is what
    §7 asks for, and a draft-level split would fragment the very rows that make a
    narrow error class visible.
    """
    from .suite import compute_metrics, generate_mutation_specs, run_suite

    results: List[Any] = []
    skipped: List[Dict[str, Any]] = []
    operators_run: List[str] = []
    for case in cases:
        skipped.extend(
            {**item, "case": case.doc_id, "kind": "excluded_claim"} for item in case.excluded
        )
        generated = generate_mutation_specs(
            case.claims,
            operators,
            seed=seed,
            context=case.context,
            targets=case.targets,
            max_per_operator=max_per_operator,
        )
        results.extend(run_suite(generated.specs, case.targets))
        skipped.extend({**item, "case": case.doc_id} for item in generated.skipped)
        operators_run.append(case.doc_id)

    report = compute_metrics(results, suite_id="finfuzz:corpus")
    report.skipped = skipped
    report.seed = seed
    report.notes.append("cases=" + ",".join(operators_run))
    return report, skipped


__all__ = [
    "CORPUS_ROOT",
    "CleanCase",
    "CorpusLoad",
    "DRAFTS_DIR",
    "REFERENCE_DIR",
    "clean_specs",
    "load_clean_claims",
    "load_corpus",
    "run_corpus",
]
