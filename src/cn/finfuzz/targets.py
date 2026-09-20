"""CARD-26 FinFuzz — detection targets.

A *target* is a deterministic detector the suite points at a mutated claim. §8
fixes two: ``checker`` and ``validator``. They are kept behind one small protocol
on purpose, because the report's value comes from the two disagreeing in
*structured* ways rather than from either one alone:

* the **checker** (CARD-05) compares figures and citations against the fact base,
  so it is where numeric / unit / period / citation tampers die;
* the **validator** (CARD-09) reads structural binding — a fact with no evidence,
  an inference with no ``derived_from`` — so it is where a rewritten causal claim
  dies and where a citation pointing at a filing that does not exist dies.

Neither subsumes the other, and a report that only ever consulted one could not
tell "this class of tamper is invisible to us" from "this class is invisible to
*that* detector".

**The graph target reads a node status, not the report's ``valid`` flag.** The
graph is built from mutation claims alone, so it legitimately has no thesis and no
calculations; ``validate_graph().valid`` is therefore false for unrelated reasons
and would mark every claim. The signal used here is the mutated claim's own
``validation_status``, which is exactly the §7 binding verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from src.cn.checker.checker import check_report
from src.cn.checker.facts import FactIndex, ReferenceIndex
from src.cn.checker.models import ClaimType, DraftClaim
from src.cn.graph.builder import build_graph
from src.cn.graph.models import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    VALIDATION_SUPPORTED,
    Claim,
    EvidenceNode,
)
from src.cn.graph.propagation import structural_status

from .models import DETECTED_BY_CHECKER, DETECTED_BY_VALIDATOR


@dataclass(frozen=True)
class Detection:
    """One detector's answer about one claim."""

    detector: str
    detected: bool
    check_types: Tuple[str, ...] = ()
    detail: Dict[str, Any] = field(default_factory=dict)


class DetectionTarget(Protocol):
    """What :func:`src.cn.finfuzz.suite.run_suite` needs from a detector."""

    name: str

    def detect(self, claims: Sequence[DraftClaim]) -> Detection:
        ...


# --------------------------------------------------------------------------- #
# target 1 — CARD-05 report checker
# --------------------------------------------------------------------------- #
@dataclass
class CheckerTarget:
    """CARD-05's deterministic checker, wired to a fixed fact base.

    The fact base is captured once per suite and shared by every mutation. That is
    the whole design of the measurement: the *only* thing that changes between the
    baseline and a mutated run is the claim, so a finding can be attributed to the
    tamper without argument.
    """

    facts: Sequence[Any]
    signals: Sequence[Any] = ()
    documents: Sequence[Any] = ()
    as_of: Optional[str] = None
    name: str = DETECTED_BY_CHECKER

    def detect(self, claims: Sequence[DraftClaim]) -> Detection:
        facts = list(self.facts)
        index = FactIndex(facts)
        references = ReferenceIndex.build(facts, list(self.documents))
        findings = check_report(
            claims,
            facts,
            self.signals,
            documents=list(self.documents),
            as_of=self.as_of,
            index=index,
            references=references,
        )
        types = tuple(sorted({finding.check_type for finding in findings}))
        return Detection(
            detector=self.name,
            detected=bool(types),
            check_types=types,
            detail={
                "findings": len(findings),
                "by_type": _count_by(findings),
            },
        )


def _count_by(findings: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for finding in findings:
        key = str(getattr(finding, "check_type", "") or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# target 2 — CARD-09 claim-evidence graph validator
# --------------------------------------------------------------------------- #
@dataclass
class GraphValidatorTarget:
    """CARD-09's structural binding verdict for the mutated claim.

    ``resolvable_references`` is what a claim's citation is allowed to become an
    evidence id. A citation naming a filing that is *not* registered contributes
    nothing, so the claim arrives at the graph unbounded — which is how the
    citation tamper becomes visible to a structural detector at all.

    ``derived_from`` is deliberately left empty for every claim: a draft claim
    carries no claim-to-claim edges, and inventing them would fabricate structure
    that the mutation never touched. The consequence is honest and useful — a
    mutated causal claim *is* an inference with no derivation, so the validator
    catches it, while a clean factual claim with a resolvable citation is
    ``supported`` and stays quiet.
    """

    resolvable_references: Tuple[str, ...] = ()
    name: str = DETECTED_BY_VALIDATOR

    def detect(self, claims: Sequence[DraftClaim]) -> Detection:
        resolvable = {str(item) for item in self.resolvable_references}
        evidence: Dict[str, EvidenceNode] = {}
        graph_claims = []
        for claim in claims:
            bound = tuple(
                ref for ref in claim.references
                if not resolvable or _reference_filename(ref) in resolvable
            )
            evidence.update({
                ref: EvidenceNode(evidence_id=ref, kind="fact", page=claim.page)
                for ref in bound
            })
            graph_claims.append(_graph_claim(claim, bound))

        graph = build_graph("finfuzz", claims=graph_claims, evidence_index=evidence)
        statuses = {
            claim.claim_id: graph.claims[claim.claim_id].validation_status
            for claim in graph_claims
            if claim.claim_id in graph.claims
        }
        unsupported = tuple(
            sorted(cid for cid, status in statuses.items() if status != VALIDATION_SUPPORTED)
        )
        return Detection(
            detector=self.name,
            detected=bool(unsupported),
            check_types=tuple(sorted({statuses.get(cid, "") for cid in unsupported})),
            detail={
                "validation_status": dict(sorted(statuses.items())),
                "unsupported_claims": list(unsupported),
            },
        )


def _reference_filename(reference: str) -> str:
    """The filing a citation names, without its ``#pN`` page fragment.

    A claim cites ``annual_04_...pdf#p1``; the registry knows the filename only. The
    page stays part of the evidence *id* (two pages are two pieces of evidence) but
    must not take part in "is this filing real".
    """
    return str(reference).split("#", 1)[0].strip()


def _graph_claim(claim: DraftClaim, bound_references: Tuple[str, ...]) -> Claim:
    """Map a draft claim onto CARD-09's vocabulary.

    The claim type is the deterministic §5 table CARD-05 already owns
    (``DraftClaim.graph_claim_type``), not a second opinion invented here.

    ``derived_from`` is left **empty** even for an inference, and that is the
    correct mapping rather than an omission: CARD-09's ``derived_from`` names
    *other claims* a claim was derived from, while a citation names a filing —
    which is evidence, not a derivation. Treating a citation as ``derived_from``
    would make every inference look supported and the structural detector would
    never fire; leaving it empty is what lets a rewritten causal claim — an
    inference with no derivation — be rejected on structural grounds, exactly as
    CARD-09's ``structural_status`` is documented to do.
    """
    graph_type = claim.graph_claim_type.value
    evidence_ids = bound_references if graph_type == CLAIM_FACT else ()
    return Claim(
        claim_id=claim.claim_id or "claim",
        claim_type=graph_type,
        text=claim.sentence,
        evidence_ids=tuple(evidence_ids),
        derived_from=(),
        metric=claim.metric,
        period_key=claim.period,
        source="finfuzz",
    )


#: §8's ``targets = {checker, validator}``.
TARGET_NAMES: Tuple[str, ...] = (DETECTED_BY_CHECKER, DETECTED_BY_VALIDATOR)


def baseline_detection(
    targets: Mapping[str, DetectionTarget], claims: Sequence[DraftClaim]
) -> Dict[str, bool]:
    """Whether each target already fires on the *unmutated* claims.

    Used as a guard at spec-generation time: a source claim that a target already
    objects to cannot support a mutation aimed at that target, because the finding
    could not be attributed to the tamper. Recording it lets the suite skip the
    spec by name instead of quietly scoring a pre-existing objection as a hit.
    """
    return {name: target.detect(claims).detected for name, target in targets.items()}


__all__ = [
    "CheckerTarget",
    "Detection",
    "DetectionTarget",
    "GraphValidatorTarget",
    "TARGET_NAMES",
    "baseline_detection",
    "_reference_filename",
    "structural_status",
    "CLAIM_FACT",
    "CLAIM_INFERENCE",
    "CLAIM_OPINION",
    "ClaimType",
]
