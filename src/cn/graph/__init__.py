"""CARD-09 Claim-Evidence Graph（主张-证据图谱）.

Turns the project's headline claim — *any conclusion has an evidence chain* —
from a slogan into a data structure plus a traversal check:

    Document -> Evidence -> Calculation -> Claim -> Thesis

What the package gives you:

* :func:`build_graph` / :func:`build_graph_from_analysis` — assemble a graph and
  reject a dependency cycle outright;
* :meth:`ClaimGraph.trace` — walk a claim down to the PDF page / snapshot
  timestamp, level by level;
* three mandatory detections (:meth:`ClaimGraph.detect_orphan_claims`,
  :meth:`ClaimGraph.detect_unsupported_calculations`,
  :meth:`ClaimGraph.detect_missing_evidence`) plus their siblings;
* :func:`propagate_state` — the §7b REQUIRED / SUPPORTING / OPTIONAL table,
  fail-closed and free of uncalibrated pseudo-confidence;
* :func:`deactivate_evidence` / :func:`revalidate` — the demo's "delete an
  evidence, watch the conclusion turn blocked" base, implemented as an
  append-only audit log rather than a physical delete.

Design rules this package enforces:

* **Reuse, do not duplicate.** CARD-03's ``CalculationRegistry`` stays the
  calculator; :class:`CalculationNode` is only a serialisable view of it.
* **Discrete states, never scores.** ``0.83`` confidence does not exist here.
* **Zero silence.** An unresolvable reference is reported by the detection
  methods, never dropped.

Quick start::

    from src.cn.graph import build_graph, CLAIM_FACT, Claim

    graph = build_graph("run-1", claims=[Claim("c1", CLAIM_FACT, "营收 1 亿", evidence_ids=("e1",))])
    graph.trace("c1").to_dict()
"""

from .builder import (
    assert_acyclic,
    build_graph,
    build_graph_from_analysis,
    claims_from_analysis,
    detect_cycles,
    evidence_index_from_ledger,
)
from .models import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    CLAIM_OPINION,
    CLAIM_TYPES,
    CODE_VERSION_UNKNOWN,
    DEPENDENCY_ROLES,
    FORMULA_VERSION,
    ROLE_OPTIONAL,
    ROLE_RANK,
    ROLE_REQUIRED,
    ROLE_SUPPORTING,
    TARGET_ASSUMPTION,
    TARGET_CALCULATION,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    TARGET_TYPES,
    TEMPORAL_CURRENT,
    TEMPORAL_FORWARD_LOOKING,
    TEMPORAL_HISTORICAL,
    TEMPORAL_STATUSES,
    THESIS_BLOCKED,
    THESIS_STALE,
    THESIS_STATUSES,
    THESIS_SUPPORTED,
    THESIS_WEAKENED,
    UPSTREAM_BLOCKED,
    UPSTREAM_CONFLICTED,
    UPSTREAM_MISSING,
    UPSTREAM_OK,
    UPSTREAM_PENDING,
    UPSTREAM_STALE,
    VALIDATION_BLOCKED,
    VALIDATION_PENDING,
    VALIDATION_STALE,
    VALIDATION_STATUSES,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    VERIFICATION_CONFLICTED,
    VERIFICATION_EXACT_MATCH,
    VERIFICATION_NORMALIZED_MATCH,
    VERIFICATION_PERIOD_INFERRED,
    VERIFICATION_ROUNDING_MATCH,
    VERIFICATION_STATUSES,
    VERIFICATION_UNVERIFIABLE,
    CalculationNode,
    Claim,
    ClaimGraph,
    DependencyRef,
    DetectionReport,
    EvidenceNode,
    GraphBindingError,
    GraphCycleError,
    GraphError,
    GraphOperand,
    RevalidationReport,
    Thesis,
    TraceNode,
    TraceTree,
)
from .propagation import (
    claim_status,
    coverage_dropped,
    downstream_closure,
    propagate_state,
    resolve_targets,
    structural_status,
    thesis_status,
    upstream_state,
)
from .revalidation import deactivate_evidence, mark_stale, revalidate

__all__ = [
    # construction
    "build_graph", "build_graph_from_analysis", "claims_from_analysis",
    "evidence_index_from_ledger", "detect_cycles", "assert_acyclic",
    # models
    "Claim", "Thesis", "DependencyRef", "GraphOperand", "CalculationNode",
    "EvidenceNode", "TraceNode", "TraceTree", "DetectionReport",
    "RevalidationReport", "ClaimGraph",
    "GraphError", "GraphCycleError", "GraphBindingError",
    # vocabularies
    "CLAIM_FACT", "CLAIM_INFERENCE", "CLAIM_OPINION", "CLAIM_TYPES",
    "TEMPORAL_HISTORICAL", "TEMPORAL_CURRENT", "TEMPORAL_FORWARD_LOOKING", "TEMPORAL_STATUSES",
    "VERIFICATION_EXACT_MATCH", "VERIFICATION_ROUNDING_MATCH", "VERIFICATION_NORMALIZED_MATCH",
    "VERIFICATION_PERIOD_INFERRED", "VERIFICATION_CONFLICTED", "VERIFICATION_UNVERIFIABLE",
    "VERIFICATION_STATUSES",
    "VALIDATION_PENDING", "VALIDATION_SUPPORTED", "VALIDATION_UNSUPPORTED", "VALIDATION_BLOCKED",
    "VALIDATION_STALE", "VALIDATION_STATUSES",
    "ROLE_REQUIRED", "ROLE_SUPPORTING", "ROLE_OPTIONAL", "DEPENDENCY_ROLES", "ROLE_RANK",
    "TARGET_CLAIM", "TARGET_EVIDENCE", "TARGET_CALCULATION", "TARGET_ASSUMPTION", "TARGET_TYPES",
    "THESIS_SUPPORTED", "THESIS_WEAKENED", "THESIS_BLOCKED", "THESIS_STALE", "THESIS_STATUSES",
    "UPSTREAM_OK", "UPSTREAM_BLOCKED", "UPSTREAM_STALE", "UPSTREAM_CONFLICTED",
    "UPSTREAM_MISSING", "UPSTREAM_PENDING",
    "FORMULA_VERSION", "CODE_VERSION_UNKNOWN",
    # propagation (§7b)
    "propagate_state", "claim_status", "thesis_status", "structural_status",
    "upstream_state", "resolve_targets", "downstream_closure", "coverage_dropped",
    # revalidation (§6)
    "deactivate_evidence", "revalidate", "mark_stale",
]
