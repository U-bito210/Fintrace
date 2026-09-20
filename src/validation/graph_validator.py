"""CARD-09 §10 — validator for the claim-evidence graph.

Rules enforced (each is a *real* check, not a formality):

1. **Three binding rules covered** (card §7): a fact without evidence is an
   orphan; an inference without ``derived_from`` is a structural error; an
   opinion without an assumption is a structural error. A numeric fact must
   additionally bind a calculation or a numeric evidence value.
2. **No dangling references**: every referenced evidence / claim / calculation id
   must resolve, otherwise the graph would claim a provenance it does not have.
3. **Calculations re-derive.** A node whose ``validation_status`` is not ``ok``
   is an error; when a CARD-03 registry is supplied, every node is re-derived
   from that registry (the registry stays the single calculator — this module
   never re-implements the arithmetic).
4. **Trace reachability.** Every fact claim must ``trace()`` down to a document
   page or a snapshot timestamp.
5. **Detection report always produced.** The three mandatory detections are part
   of the returned payload, so "no report" can never read as "clean".
6. **Thesis status is derived, not asserted.** A thesis whose stored status
   disagrees with the propagation table is an error.

Following the project convention (CARD-05 / CARD-08), *incomplete input yields a
warning, never a silent pass*: without a calculation registry the reproducibility
check is reported as skipped rather than assumed to have succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    TARGET_CALCULATION,
    TARGET_EVIDENCE,
    VALIDATION_PENDING,
    ClaimGraph,
)
from src.cn.graph.builder import detect_cycles
from src.cn.graph.propagation import thesis_status

#: A numeric fact needs a calculation or an evidence value to back the number.
_NUMERIC_TOLERANCE = 1e-9


@dataclass(frozen=True)
class GraphValidation:
    """Outcome of validating one graph (or a corpus of graphs)."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    detection: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "detection": dict(self.detection),
        }


def validate_graph(
    graph: ClaimGraph,
    *,
    registry: Any = None,
    tolerance: float = _NUMERIC_TOLERANCE,
) -> GraphValidation:
    """Validate one graph; return errors, warnings and the detection report."""
    errors: List[str] = []
    warnings: List[str] = []

    _check_binding_rules(graph, errors, warnings, registry=registry)
    _check_references(graph, errors)
    _check_calculations(graph, errors, warnings, registry=registry, tolerance=tolerance)
    _check_trace_reachability(graph, errors, warnings)
    _check_cycles(graph, errors)
    _check_theses(graph, errors)
    _check_pending(graph, warnings)

    detection = graph.detect().to_dict()
    # Rule 5: the detection report is *always* part of the payload. A run that
    # reports nothing must be distinguishable from a run that never looked.
    for key in ("orphan_claims", "unsupported_calculations", "missing_evidence"):
        if key not in detection:  # pragma: no cover - defensive
            errors.append(f"detection_report_missing:{key}")

    return GraphValidation(
        valid=not errors, errors=errors, warnings=warnings, detection=detection
    )


def validate_graph_corpus(
    graphs: Iterable[ClaimGraph], *, registry: Any = None
) -> GraphValidation:
    """Validate several graphs and aggregate — a free end-to-end invariant."""
    errors: List[str] = []
    warnings: List[str] = []
    detection: Dict[str, Any] = {"graphs": []}
    for graph in graphs:
        result = validate_graph(graph, registry=registry)
        errors.extend(f"{graph.run_id}:{item}" for item in result.errors)
        warnings.extend(f"{graph.run_id}:{item}" for item in result.warnings)
        detection["graphs"].append(result.detection)
    detection["clean_graphs"] = sum(1 for item in detection["graphs"] if item.get("clean"))
    detection["graph_count"] = len(detection["graphs"])
    return GraphValidation(valid=not errors, errors=errors, warnings=warnings, detection=detection)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _check_binding_rules(
    graph: ClaimGraph, errors: List[str], warnings: List[str], *, registry: Any
) -> None:
    for claim in graph.claims.values():
        if claim.claim_type == CLAIM_FACT:
            if not claim.evidence_ids:
                errors.append(f"orphan_claim:{claim.claim_id}")
            elif claim.numeric:
                if not _has_numeric_binding(graph, claim, registry):
                    errors.append(f"numeric_fact_without_binding:{claim.claim_id}")
        elif claim.claim_type == CLAIM_INFERENCE:
            if not claim.derived_from:
                errors.append(f"inference_without_derivation:{claim.claim_id}")
        elif claim.claim_type == CLAIM_OPINION:
            if not claim.assumption_ids:
                errors.append(f"opinion_without_assumption:{claim.claim_id}")
        # A claim that binds nothing at all cannot be reasoned about.
        if claim.claim_type == CLAIM_OPINION and not claim.dependency_metadata:
            warnings.append(f"opinion_without_dependency_metadata:{claim.claim_id}")


def _has_numeric_binding(graph: ClaimGraph, claim: Any, registry: Any) -> bool:
    """A numeric fact is bound by a calculation or a numeric evidence value."""
    if claim.calculation_ids:
        # Prefer proving the calculation reproduces; fall back to its presence.
        if registry is not None:
            for calculation_id in claim.calculation_ids:
                try:
                    registry.recalculate(calculation_id)
                except Exception:
                    continue
                return True
            return False
        return True
    for evidence_id in claim.evidence_ids:
        node = graph.evidence_index.get(evidence_id)
        if node is not None and node.value is not None:
            return True
        if evidence_id in graph.calculations:  # a calculation published as evidence
            return True
    return False


def _check_references(graph: ClaimGraph, errors: List[str]) -> None:
    for evidence_id in graph.detect_missing_evidence():
        errors.append(f"missing_evidence:{evidence_id}")
    for claim_id in graph.detect_missing_claims():
        errors.append(f"unknown_claim:{claim_id}")
    for calculation_id in graph.detect_missing_calculations():
        errors.append(f"unknown_calculation:{calculation_id}")


def _check_calculations(
    graph: ClaimGraph,
    errors: List[str],
    warnings: List[str],
    *,
    registry: Any,
    tolerance: float,
) -> None:
    checks_possible = registry is not None and hasattr(registry, "recalculate")
    if not checks_possible and graph.calculations:
        warnings.append("no_registry_calculation_recheck_skipped")
    for node in graph.calculations.values():
        if node.validation_status != "ok":
            errors.append(f"calculation_not_reproducible:{node.calculation_id}")
        if not checks_possible:
            continue
        try:
            expected = registry.recalculate(node.calculation_id)
        except Exception:
            errors.append(f"calculation_unresolvable:{node.calculation_id}")
            continue
        value = node.value
        if value is None:
            errors.append(f"calculation_without_value:{node.calculation_id}")
            continue
        if abs(expected - float(value)) > tolerance * max(1.0, abs(expected)):
            errors.append(f"calculation_value_mismatch:{node.calculation_id}")


def _check_trace_reachability(
    graph: ClaimGraph, errors: List[str], warnings: List[str]
) -> None:
    for claim in graph.facts:
        tree = graph.trace(claim.claim_id)
        if tree.locatable:
            continue
        # Distinguish "the evidence exists but is unlocatable" from "evidence is
        # missing entirely" — they need different fixes.
        if any(ref in graph.evidence_index for ref in claim.evidence_ids):
            errors.append(f"unreachable_evidence:{claim.claim_id}")
        # A missing evidence is already reported by the reference rule.
    for evidence_id, node in graph.evidence_index.items():
        if node.kind == "fact" and not node.locatable:
            warnings.append(f"unlocatable_evidence:{evidence_id}")


def _check_cycles(graph: ClaimGraph, errors: List[str]) -> None:
    for cycle in detect_cycles(graph):
        errors.append("dependency_cycle:" + "->".join(cycle))


def _check_theses(graph: ClaimGraph, errors: List[str]) -> None:
    for thesis in graph.theses.values():
        expected = thesis_status(graph, thesis)
        if thesis.status != expected:
            errors.append(f"thesis_status_stale:{thesis.thesis_id}:{thesis.status}!={expected}")


def _check_pending(graph: ClaimGraph, warnings: List[str]) -> None:
    for claim in graph.claims.values():
        if claim.validation_status == VALIDATION_PENDING:
            warnings.append(f"claim_left_pending:{claim.claim_id}")


__all__ = ["GraphValidation", "validate_graph", "validate_graph_corpus"]
