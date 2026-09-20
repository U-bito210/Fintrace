"""CARD-09 — graph construction.

``build_graph`` assembles a :class:`~src.cn.graph.models.ClaimGraph` from the
records the other cards already produce (CARD-01/02 evidence, CARD-03
calculations and diagnostic signals), then:

1. rejects a dependency **cycle** (card §8 edge case 3) — a graph that cannot be
   traversed must not be handed on as if it could;
2. sets each claim's structural status from the §7 binding rules;
3. runs one global §7b propagation so the graph is status-consistent on return.

``claims_from_analysis`` is the bridge that makes CARD-03's output *entrably*
correct: a metric row becomes a **fact**, a diagnostic signal becomes an
**inference** whose ``derived_from`` points at the metric facts it is built
from. A signal is therefore never promoted to a fact (card §7).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    TARGET_ASSUMPTION,
    TARGET_CALCULATION,
    TARGET_CLAIM,
    TARGET_EVIDENCE,
    TEMPORAL_CURRENT,
    TEMPORAL_HISTORICAL,
    VALIDATION_SUPPORTED,
    VERIFICATION_EXACT_MATCH,
    VERIFICATION_NORMALIZED_MATCH,
    VERIFICATION_UNVERIFIABLE,
    CalculationNode,
    Claim,
    ClaimGraph,
    DependencyRef,
    EvidenceNode,
    GraphCycleError,
    GraphError,
    Thesis,
)
from .propagation import propagate_state, structural_status

#: Metrics whose value is a market/point-in-time observation rather than a
#: realised financial. Kept tiny and explicit: guessing the time axis for an
#: unknown metric would violate zero-silence.
_CURRENT_METRICS = frozenset(
    {"close", "market_cap", "total_market_cap", "pe", "pb", "ps", "dividend_yield"}
)

#: Tolerance used when checking a CARD-03 value against its own re-derivation.
_DEFAULT_TOLERANCE = 1e-9


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_graph(
    run_id: str,
    *,
    claims: Iterable[Claim] = (),
    theses: Iterable[Thesis] = (),
    calculations: Any = None,
    evidence_index: Any = None,
    ledger: Any = None,
    jumps: Optional[Mapping[str, Mapping[str, Any]]] = None,
    deactivations: Iterable[Mapping[str, Any]] = (),
    warnings: Iterable[str] = (),
    registry: Any = None,
    propagate: bool = True,
) -> ClaimGraph:
    """Assemble, cycle-check, and status-seed a graph.

    ``calculations`` accepts either an iterable of
    :class:`~src.cn.graph.models.CalculationNode` or a CARD-03
    :class:`~src.cn.analysis.calculations.CalculationRegistry` (duck-typed on
    ``records()``); the registry itself may also be passed as ``registry`` so its
    ``validate()`` result stamps each node's ``validation_status``.

    ``evidence_index`` accepts a mapping or an iterable of
    :class:`~src.cn.graph.models.EvidenceNode`. Pass ``ledger`` to build the index
    from an :class:`~src.cn.evidence.EvidenceLedger` instead.
    """
    graph = ClaimGraph(run_id=run_id)

    for claim in claims:
        graph.add_claim(claim)
    for thesis in theses:
        graph.add_thesis(thesis)

    for node in _coerce_calculation_nodes(calculations, registry):
        graph.add_calculation(node)
    for node in _coerce_evidence_nodes(evidence_index, ledger, jumps):
        graph.add_evidence(node)

    graph.deactivations = [dict(item) for item in deactivations]
    graph.warnings = list(warnings)
    _apply_deactivations(graph)

    assert_acyclic(graph)

    if propagate:
        # Seed structural statuses first so the propagation fixpoint starts from
        # the §7 binding outcome rather than from `pending` for every claim.
        for claim_id, claim in list(graph.claims.items()):
            status = structural_status(graph, claim)
            if status != claim.validation_status:
                graph.claims[claim_id] = claim.with_status(status)
        propagate_state(graph)
    return graph


def build_graph_from_analysis(
    analysis: Any,
    *,
    run_id: str,
    document_id: str = "",
    ledger: Any = None,
    evidence_index: Any = None,
    jumps: Optional[Mapping[str, Mapping[str, Any]]] = None,
    extra_claims: Iterable[Claim] = (),
    theses: Iterable[Thesis] = (),
    propagate: bool = True,
) -> ClaimGraph:
    """Convenience: CARD-03 ``AnalysisResult`` -> graph (facts + inferences)."""
    registry = getattr(analysis, "registry", None)
    claims = list(claims_from_analysis(analysis, document_id=document_id, registry=registry))
    claims.extend(extra_claims)
    return build_graph(
        run_id,
        claims=claims,
        theses=theses,
        calculations=registry,
        evidence_index=evidence_index,
        ledger=ledger,
        jumps=jumps,
        registry=registry,
        propagate=propagate,
    )


# ---------------------------------------------------------------------------
# Claim construction
# ---------------------------------------------------------------------------
def claims_from_analysis(analysis: Any, *, document_id: str = "", registry: Any = None) -> List[Claim]:
    """Turn CARD-03 metrics into fact claims and signals into inference claims.

    Deterministic ids: ``claim_fact_<metric>_<period>`` and
    ``claim_inference_<signal>_<periods>``. A signal that cannot be tied back to
    at least one metric fact is still emitted — it then fails the §7 binding rule
    and is reported as unsupported rather than silently dropped.
    """
    registry = registry if registry is not None else getattr(analysis, "registry", None)
    doc = document_id or str(getattr(analysis, "document_id", "") or "")
    known_calculations = (
        {str(record.calculation_id) for record in registry.records()}
        if registry is not None and hasattr(registry, "records")
        else set()
    )

    claims: List[Claim] = []
    calc_to_claim: Dict[str, str] = {}
    used_ids: set = set()

    for metric in getattr(analysis, "metrics", ()) or ():
        if getattr(metric, "value", None) is None:
            continue  # a metric with no value asserts nothing; emit no fact
        claim_id = _unique(
            f"claim_fact_{metric.metric}_{metric.period_key or 'na'}", used_ids
        )
        # ``MetricResult.inputs`` can carry a CARD-02 normalized fact **or** a
        # CARD-03 intermediate calculation (e.g. 非经常性损益 = 归母 − 扣非). Sort
        # them by what the registry actually knows, otherwise a calculation id
        # would masquerade as a dangling piece of evidence.
        raw_inputs = tuple(str(item) for item in (getattr(metric, "inputs", ()) or ()))
        evidence_ids = tuple(item for item in raw_inputs if item not in known_calculations)
        calculation_ids: List[str] = []
        if getattr(metric, "calculation_id", None):
            calculation_ids.append(str(metric.calculation_id))
        calculation_ids.extend(item for item in raw_inputs if item in known_calculations)
        calculation_ids = list(dict.fromkeys(calculation_ids))
        if metric.calculation_id:
            calc_to_claim.setdefault(str(metric.calculation_id), claim_id)
        # A *derived* metric (free cash flow, a ratio) binds its calculation, and
        # the calculation binds the evidence. CARD-09 §7 nevertheless requires a
        # fact to bind Evidence **directly**, so the calculation's evidence
        # operands are expanded onto the claim. The ids are read off the CARD-03
        # registry — nothing here invents a source.
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_ids + _evidence_closure(registry, calculation_ids, known_calculations)
            )
        )

        dependency: List[DependencyRef] = [
            DependencyRef(target_id=item, target_type=TARGET_EVIDENCE, role="REQUIRED")
            for item in evidence_ids
        ]
        dependency.extend(
            DependencyRef(target_id=item, target_type=TARGET_CALCULATION, role="REQUIRED")
            for item in calculation_ids
        )

        claims.append(
            Claim(
                claim_id=claim_id,
                claim_type=CLAIM_FACT,
                text=_metric_text(metric),
                temporal_status=_temporal_status(metric),
                evidence_ids=evidence_ids,
                calculation_ids=tuple(calculation_ids),
                dependency_metadata=tuple(dependency),
                verification_status=_verification_status(metric, registry),
                rationale=getattr(metric, "note", None),
                source="card03.metrics",
                metric=str(metric.metric),
                period_key=str(metric.period_key) if metric.period_key else None,
            )
        )

    for signal in getattr(analysis, "signals", ()) or ():
        periods = "".join(str(p) for p in (getattr(signal, "periods", ()) or ()))
        claim_id = _unique(f"claim_inference_{signal.signal_id}_{periods or 'na'}", used_ids)
        derived = tuple(
            calc_to_claim[cid]
            for cid in (getattr(signal, "metric_calculation_ids", ()) or ())
            if cid in calc_to_claim
        )
        claims.append(
            Claim(
                claim_id=claim_id,
                claim_type=CLAIM_INFERENCE,
                text=str(getattr(signal, "detail", "") or getattr(signal, "label", "")),
                temporal_status=TEMPORAL_HISTORICAL,
                derived_from=derived,
                dependency_metadata=tuple(
                    DependencyRef(target_id=item, target_type=TARGET_CLAIM, role="REQUIRED")
                    for item in derived
                ),
                verification_status=VERIFICATION_UNVERIFIABLE,
                rationale=str(getattr(signal, "label", "") or ""),
                source="card03.signals" + (f"|doc={doc}" if doc else ""),
                signal_ids=(str(getattr(signal, "signal_id", "")),),
            )
        )
    return claims


def evidence_index_from_ledger(
    ledger: Any, *, jumps: Optional[Mapping[str, Mapping[str, Any]]] = None
) -> Dict[str, EvidenceNode]:
    """Build the evidence index from an ``EvidenceLedger`` (+ optional page jumps).

    ``jumps`` is the ``normalization_jumps`` / document bridge payload that maps
    an evidence id to ``{document_id, page, bbox, ...}``. Without it a snapshot
    fact is still locatable through ``available_at``; a document fact with no
    page stays unlocatable and is reported, never faked.
    """
    jumps = jumps or {}
    index: Dict[str, EvidenceNode] = {}
    for record in ledger.records():
        location = jumps.get(record.evidence_id, {}) or {}
        bbox = location.get("bbox")
        kind = str(getattr(record, "kind", "fact") or "fact")
        index[record.evidence_id] = EvidenceNode(
            evidence_id=record.evidence_id,
            kind=kind,
            document_id=location.get("document_id") or getattr(record, "symbol", None),
            page=location.get("page"),
            bbox=tuple(bbox) if bbox else None,
            snapshot_id=getattr(ledger, "snapshot_id", None),
            available_at=getattr(record, "available_at", None),
            metric=getattr(record, "metric", None),
            value=getattr(record, "value", None),
            unit=getattr(record, "unit", None),
            period_key=getattr(record, "fiscal_period", None),
            provider=getattr(record, "provider", None),
            active=True,
        )
    return index


# ---------------------------------------------------------------------------
# Acyclicity
# ---------------------------------------------------------------------------
def detect_cycles(graph: ClaimGraph) -> List[List[str]]:
    """Return every cycle among claim→claim and calculation→calculation edges."""
    edges: Dict[str, List[str]] = {}
    for claim in graph.claims.values():
        edges[claim.claim_id] = [
            target_id
            for target_type, target_id, _role in _claim_targets(claim)
            if target_type == TARGET_CLAIM and target_id in graph.claims
        ]
    for node in graph.calculations.values():
        edges[node.calculation_id] = [
            item.ref
            for item in node.inputs
            if item.kind == "calculation" and item.ref in graph.calculations
        ]

    cycles: List[List[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {node: WHITE for node in edges}
    stack: List[str] = []

    def visit(node: str) -> None:
        colour[node] = GREY
        stack.append(node)
        for neighbour in edges.get(node, ()):
            if colour.get(neighbour, WHITE) == GREY:
                start = stack.index(neighbour)
                cycle = stack[start:] + [neighbour]
                if cycle not in cycles:
                    cycles.append(cycle)
            elif colour.get(neighbour, WHITE) == WHITE:
                visit(neighbour)
        stack.pop()
        colour[node] = BLACK

    for node in sorted(edges):
        if colour[node] == WHITE:
            visit(node)
    return cycles


def assert_acyclic(graph: ClaimGraph) -> None:
    cycles = detect_cycles(graph)
    if cycles:
        rendered = " -> ".join(cycles[0])
        raise GraphCycleError(f"dependency cycle rejected: {rendered}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _claim_targets(claim: Claim) -> List[Tuple[str, str, str]]:
    from .propagation import resolve_targets

    return resolve_targets(claim)


def _evidence_closure(
    registry: Any, calculation_ids: Sequence[str], known_calculations: set
) -> Tuple[str, ...]:
    """Transitive evidence operands of the given calculations, in stable order.

    Constants are skipped (they are formula constants, not evidence) and an
    unresolvable calculation contributes nothing rather than a guess.
    """
    if not calculation_ids:
        return ()
    out: List[str] = []
    seen: set = set()
    stack: List[str] = list(calculation_ids)
    while stack:
        calculation_id = stack.pop(0)
        if calculation_id in seen or calculation_id not in known_calculations:
            continue
        seen.add(calculation_id)
        try:
            record = registry.get(calculation_id)
        except Exception:
            continue
        for operand in getattr(record, "operands", ()):
            kind = str(getattr(operand, "kind", ""))
            ref = str(getattr(operand, "operand_id", ""))
            if kind == "evidence" and ref not in out:
                out.append(ref)
            elif kind == "calculation":
                stack.append(ref)
    return tuple(out)


def _metric_text(metric: Any) -> str:
    value = getattr(metric, "value", None)
    unit = getattr(metric, "unit", "") or ""
    label = getattr(metric, "label", "") or getattr(metric, "metric", "")
    period = getattr(metric, "period_key", "") or ""
    delta = getattr(metric, "delta_pct", None)
    suffix = ""
    if delta is not None:
        suffix = f"，同比 {delta:+.2f}%"
    return f"{label}（{period}）= {value} {unit}{suffix}".strip()


def _temporal_status(metric: Any) -> str:
    return TEMPORAL_CURRENT if str(getattr(metric, "metric", "")) in _CURRENT_METRICS else TEMPORAL_HISTORICAL


def _verification_status(metric: Any, registry: Any) -> str:
    calculation_id = getattr(metric, "calculation_id", None)
    if calculation_id and registry is not None:
        try:
            expected = registry.recalculate(calculation_id)
        except Exception:
            return VERIFICATION_UNVERIFIABLE
        value = getattr(metric, "value", None)
        if value is not None and abs(expected - float(value)) <= _DEFAULT_TOLERANCE * max(1.0, abs(expected)):
            return VERIFICATION_EXACT_MATCH
        return VERIFICATION_UNVERIFIABLE
    if getattr(metric, "inputs", None):
        return VERIFICATION_NORMALIZED_MATCH
    return VERIFICATION_UNVERIFIABLE


def _coerce_calculation_nodes(source: Any, registry: Any) -> List[CalculationNode]:
    if source is None:
        records = registry.records() if registry is not None and hasattr(registry, "records") else []
    elif hasattr(source, "records"):  # a CalculationRegistry
        records = source.records()
        registry = registry or source
    else:
        return [
            item if isinstance(item, CalculationNode) else CalculationNode.from_record(item)
            for item in source
        ]

    status_by_id: Dict[str, str] = {}
    if registry is not None and hasattr(registry, "validate"):
        for error in registry.validate():
            status_by_id[str(error).split(":", 1)[0]] = "mismatch"

    nodes: List[CalculationNode] = []
    for record in records:
        nodes.append(
            CalculationNode.from_record(
                record,
                validation_status=status_by_id.get(getattr(record, "calculation_id", ""), "ok"),
            )
        )
    return nodes


def _coerce_evidence_nodes(source: Any, ledger: Any, jumps: Any) -> List[EvidenceNode]:
    if ledger is not None:
        return list(evidence_index_from_ledger(ledger, jumps=jumps).values())
    if source is None:
        return []
    if isinstance(source, Mapping):
        return [
            item if isinstance(item, EvidenceNode) else EvidenceNode.from_dict(dict(item))
            for item in source.values()
        ]
    return [
        item if isinstance(item, EvidenceNode) else EvidenceNode.from_dict(dict(item))
        for item in source
    ]


def _apply_deactivations(graph: ClaimGraph) -> None:
    """Reflect the append-only deactivation log onto the evidence nodes."""
    for record in graph.deactivations:
        evidence_id = str(record.get("evidence_id", ""))
        node = graph.evidence_index.get(evidence_id)
        if node is None or not node.active:
            continue
        import dataclasses

        graph.evidence_index[evidence_id] = dataclasses.replace(
            node, active=False, deactivated_reason=str(record.get("reason", ""))
        )


def _unique(stem: str, used: set) -> str:
    if stem not in used:
        used.add(stem)
        return stem
    index = 2
    while f"{stem}_{index}" in used:
        index += 1
    result = f"{stem}_{index}"
    used.add(result)
    return result


__all__ = [
    "assert_acyclic",
    "build_graph",
    "build_graph_from_analysis",
    "claims_from_analysis",
    "detect_cycles",
    "evidence_index_from_ledger",
]
