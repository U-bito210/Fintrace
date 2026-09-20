"""CARD-09 Claim-Evidence Graph — data models.

The graph is the technical base for *"any conclusion traces to a PDF page"*. It
organises, per run:

    Document -> Evidence -> Calculation -> Claim -> Thesis

and makes the three binding rules of CARD-09 §7 machine-checkable instead of
rhetorical:

* **fact** — must bind Evidence directly (a numeric fact must additionally bind
  a Calculation or a numeric evidence value);
* **inference** — must be traceable to the facts it derives from
  (``derived_from``), so a CARD-03 diagnostic *signal* enters the graph as an
  inference, never as a fact;
* **opinion** — must declare its assumption source.

Two design rules carried over from the project's contracts:

* **No pseudo-confidence.** Verification is a *discrete* status
  (``EXACT_MATCH`` … ``UNVERIFIABLE``); there is deliberately no ``0.83``.
* **Zero silence.** A reference that cannot be resolved is reported by
  :meth:`ClaimGraph.detect_missing_evidence`, never dropped.

The graph **reuses** CARD-03's :class:`~src.cn.analysis.calculations.Calculation`
as the calculation source of truth — :class:`CalculationNode` is a serialisable
*view* of that record, not a second calculator. ``formula_version`` versions the
operand/operation vocabulary; ``code_version`` is never a fabricated git hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------
CLAIM_FACT = "fact"
CLAIM_INFERENCE = "inference"
CLAIM_OPINION = "opinion"
CLAIM_TYPES: Tuple[str, ...] = (CLAIM_FACT, CLAIM_INFERENCE, CLAIM_OPINION)

TEMPORAL_HISTORICAL = "historical"
TEMPORAL_CURRENT = "current"
TEMPORAL_FORWARD_LOOKING = "forward_looking"
TEMPORAL_STATUSES: Tuple[str, ...] = (
    TEMPORAL_HISTORICAL,
    TEMPORAL_CURRENT,
    TEMPORAL_FORWARD_LOOKING,
)

VERIFICATION_EXACT_MATCH = "EXACT_MATCH"
VERIFICATION_ROUNDING_MATCH = "ROUNDING_MATCH"
VERIFICATION_NORMALIZED_MATCH = "NORMALIZED_MATCH"
VERIFICATION_PERIOD_INFERRED = "PERIOD_INFERRED"
VERIFICATION_CONFLICTED = "CONFLICTED"
VERIFICATION_UNVERIFIABLE = "UNVERIFIABLE"
VERIFICATION_STATUSES: Tuple[str, ...] = (
    VERIFICATION_EXACT_MATCH,
    VERIFICATION_ROUNDING_MATCH,
    VERIFICATION_NORMALIZED_MATCH,
    VERIFICATION_PERIOD_INFERRED,
    VERIFICATION_CONFLICTED,
    VERIFICATION_UNVERIFIABLE,
)

VALIDATION_PENDING = "pending"
VALIDATION_SUPPORTED = "supported"
VALIDATION_UNSUPPORTED = "unsupported"
VALIDATION_BLOCKED = "blocked"
VALIDATION_STALE = "stale"
VALIDATION_STATUSES: Tuple[str, ...] = (
    VALIDATION_PENDING,
    VALIDATION_SUPPORTED,
    VALIDATION_UNSUPPORTED,
    VALIDATION_BLOCKED,
    VALIDATION_STALE,
)

ROLE_REQUIRED = "REQUIRED"
ROLE_SUPPORTING = "SUPPORTING"
ROLE_OPTIONAL = "OPTIONAL"
DEPENDENCY_ROLES: Tuple[str, ...] = (ROLE_REQUIRED, ROLE_SUPPORTING, ROLE_OPTIONAL)
#: REQUIRED < SUPPORTING < OPTIONAL, weakest-role-first for deterministic output.
ROLE_RANK: Dict[str, int] = {ROLE_REQUIRED: 0, ROLE_SUPPORTING: 1, ROLE_OPTIONAL: 2}

TARGET_CLAIM = "claim"
TARGET_EVIDENCE = "evidence"
TARGET_CALCULATION = "calculation"
TARGET_ASSUMPTION = "assumption"
TARGET_TYPES: Tuple[str, ...] = (TARGET_CLAIM, TARGET_EVIDENCE, TARGET_CALCULATION, TARGET_ASSUMPTION)

THESIS_SUPPORTED = "supported"
THESIS_WEAKENED = "weakened"
THESIS_BLOCKED = "blocked"
THESIS_STALE = "stale"
THESIS_STATUSES: Tuple[str, ...] = (THESIS_SUPPORTED, THESIS_WEAKENED, THESIS_BLOCKED, THESIS_STALE)

#: Upstream state vocabulary used by the §7b propagation table.
UPSTREAM_OK = "ok"
UPSTREAM_BLOCKED = "blocked"
UPSTREAM_STALE = "stale"
UPSTREAM_CONFLICTED = "conflicted"
UPSTREAM_MISSING = "missing"
UPSTREAM_PENDING = "pending"

#: Versions the CARD-03 *operand/operation schema* the nodes were built against.
FORMULA_VERSION = "card03.v1"
#: Stamped when the caller has no code version. Never a fabricated git hash.
CODE_VERSION_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GraphError(ValueError):
    """The graph is malformed and must not be presented as complete."""


class GraphCycleError(GraphError):
    """A dependency cycle was found (CARD-09 §8 edge case 3)."""


class GraphBindingError(GraphError):
    """A claim breaks one of the three binding rules (§7)."""


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DependencyRef:
    """One entry of ``dependency_metadata``.

    ``role`` carries the REQUIRED / SUPPORTING / OPTIONAL semantics that drive
    state propagation (CARD-09 §7b).
    """

    target_id: str
    target_type: str = TARGET_CLAIM
    role: str = ROLE_REQUIRED

    def __post_init__(self) -> None:
        if self.role not in DEPENDENCY_ROLES:
            raise GraphError(f"unknown dependency role: {self.role!r}")
        if self.target_type not in TARGET_TYPES:
            raise GraphError(f"unknown dependency target_type: {self.target_type!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"target_id": self.target_id, "target_type": self.target_type, "role": self.role}


@dataclass(frozen=True)
class Claim:
    """A claim with its epistemological type kept separate from its time axis.

    ``claim_type`` (fact / inference / opinion) is the *epistemological*
    dimension; ``temporal_status`` (historical / current / forward_looking) is
    the *time* dimension. Mixing them into one enum is the v3.0 defect CARD-09
    exists to prevent.
    """

    claim_id: str
    claim_type: str
    text: str
    temporal_status: str = TEMPORAL_HISTORICAL
    evidence_ids: Tuple[str, ...] = ()
    calculation_ids: Tuple[str, ...] = ()
    assumption_ids: Tuple[str, ...] = ()
    derived_from: Tuple[str, ...] = ()
    dependency_metadata: Tuple[DependencyRef, ...] = ()
    verification_status: str = VERIFICATION_UNVERIFIABLE
    validation_status: str = VALIDATION_PENDING
    rationale: Optional[str] = None
    source: str = ""
    signal_ids: Tuple[str, ...] = ()
    #: Which CARD-03 metric / period this claim speaks for. Kept so downstream
    #: renderers (CARD-10's memo) can resolve "the fact claim for 营收 in 2024FY"
    #: without pattern-matching the claim id.
    metric: Optional[str] = None
    period_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.claim_type not in CLAIM_TYPES:
            raise GraphError(f"unknown claim_type: {self.claim_type!r}")
        if self.temporal_status not in TEMPORAL_STATUSES:
            raise GraphError(f"unknown temporal_status: {self.temporal_status!r}")
        if self.verification_status not in VERIFICATION_STATUSES:
            raise GraphError(f"unknown verification_status: {self.verification_status!r}")
        if self.validation_status not in VALIDATION_STATUSES:
            raise GraphError(f"unknown validation_status: {self.validation_status!r}")

    # -- helpers ----------------------------------------------------------
    @property
    def role_map(self) -> Dict[str, str]:
        return {ref.target_id: ref.role for ref in self.dependency_metadata}

    def role_of(self, target_id: str, default: str = ROLE_REQUIRED) -> str:
        return self.role_map.get(target_id, default)

    @property
    def required_targets(self) -> Tuple[str, ...]:
        return tuple(ref.target_id for ref in self.dependency_metadata if ref.role == ROLE_REQUIRED)

    @property
    def supporting_targets(self) -> Tuple[str, ...]:
        return tuple(ref.target_id for ref in self.dependency_metadata if ref.role == ROLE_SUPPORTING)

    @property
    def numeric(self) -> bool:
        """Whether the claim asserts a number (drives the §7 numeric-fact rule)."""
        return any(ch.isdigit() for ch in self.text)

    def with_status(self, status: str) -> "Claim":
        import dataclasses

        return dataclasses.replace(self, validation_status=status)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "temporal_status": self.temporal_status,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "calculation_ids": list(self.calculation_ids),
            "assumption_ids": list(self.assumption_ids),
            "derived_from": list(self.derived_from),
            "dependency_metadata": [ref.to_dict() for ref in self.dependency_metadata],
            "verification_status": self.verification_status,
            "validation_status": self.validation_status,
            "rationale": self.rationale,
            "source": self.source,
            "signal_ids": list(self.signal_ids),
            "metric": self.metric,
            "period_key": self.period_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Claim":
        return cls(
            claim_id=data["claim_id"],
            claim_type=data["claim_type"],
            text=data.get("text", ""),
            temporal_status=data.get("temporal_status", TEMPORAL_HISTORICAL),
            evidence_ids=tuple(data.get("evidence_ids") or ()),
            calculation_ids=tuple(data.get("calculation_ids") or ()),
            assumption_ids=tuple(data.get("assumption_ids") or ()),
            derived_from=tuple(data.get("derived_from") or ()),
            dependency_metadata=tuple(
                DependencyRef(
                    target_id=item["target_id"],
                    target_type=item.get("target_type", TARGET_CLAIM),
                    role=item.get("role", ROLE_REQUIRED),
                )
                for item in (data.get("dependency_metadata") or ())
            ),
            verification_status=data.get("verification_status", VERIFICATION_UNVERIFIABLE),
            validation_status=data.get("validation_status", VALIDATION_PENDING),
            rationale=data.get("rationale"),
            source=data.get("source", ""),
            signal_ids=tuple(data.get("signal_ids") or ()),
            metric=data.get("metric"),
            period_key=data.get("period_key"),
        )


@dataclass
class Thesis:
    """An investment proposition built from REQUIRED/SUPPORTING claims.

    ``status`` is *derived* by the §7b propagation rules — it is never set by a
    model. :class:`~src.cn.graph.propagation.Propagator` is the only writer.
    """

    thesis_id: str
    statement: str
    claim_ids: Tuple[str, ...] = ()
    dependency_roles: Dict[str, str] = field(default_factory=dict)
    status: str = THESIS_SUPPORTED

    def __post_init__(self) -> None:
        for claim_id in self.claim_ids:
            role = self.dependency_roles.get(claim_id, ROLE_REQUIRED)
            if role not in DEPENDENCY_ROLES:
                raise GraphError(f"unknown dependency role for {claim_id!r}: {role!r}")

    @property
    def role_map(self) -> Dict[str, str]:
        return {cid: self.dependency_roles.get(cid, ROLE_REQUIRED) for cid in self.claim_ids}

    def required_claims(self) -> Tuple[str, ...]:
        return tuple(cid for cid in self.claim_ids if self.role_map[cid] == ROLE_REQUIRED)

    def supporting_claims(self) -> Tuple[str, ...]:
        return tuple(cid for cid in self.claim_ids if self.role_map[cid] == ROLE_SUPPORTING)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "statement": self.statement,
            "claim_ids": list(self.claim_ids),
            "dependency_roles": dict(self.role_map),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Thesis":
        return cls(
            thesis_id=data["thesis_id"],
            statement=data.get("statement", ""),
            claim_ids=tuple(data.get("claim_ids") or ()),
            dependency_roles=dict(data.get("dependency_roles") or {}),
            status=data.get("status", THESIS_SUPPORTED),
        )


@dataclass(frozen=True)
class GraphOperand:
    """One input of a calculation node, as ``{ref, value}`` (CARD-09 §5)."""

    kind: str
    ref: str
    value: float
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "value": self.value, "label": self.label}


@dataclass(frozen=True)
class CalculationNode:
    """A serialisable *view* of a CARD-03 calculation for the graph.

    Built with :meth:`from_record`; the underlying
    :class:`~src.cn.analysis.calculations.CalculationRegistry` remains the
    authority that can re-derive ``value``.
    """

    calculation_id: str
    metric: str
    formula: str
    operation: str
    inputs: Tuple[GraphOperand, ...]
    output: Dict[str, Any]
    validation_status: str = "ok"
    formula_version: str = FORMULA_VERSION
    code_version: str = CODE_VERSION_UNKNOWN
    source: str = "disclosed"
    engine: Optional[str] = None
    scope: Optional[str] = None
    version: Optional[str] = None
    currency: Optional[str] = None
    note: Optional[str] = None

    @property
    def value(self) -> Optional[float]:
        value = self.output.get("value")
        return float(value) if value is not None else None

    @property
    def input_refs(self) -> Tuple[str, ...]:
        return tuple(item.ref for item in self.inputs)

    @property
    def evidence_ids(self) -> Tuple[str, ...]:
        return tuple(item.ref for item in self.inputs if item.kind == "evidence")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calculation_id": self.calculation_id,
            "metric": self.metric,
            "formula": self.formula,
            "formula_version": self.formula_version,
            "operation": self.operation,
            "inputs": [item.to_dict() for item in self.inputs],
            "output": dict(self.output),
            "code_version": self.code_version,
            "validation_status": self.validation_status,
            "source": self.source,
            "engine": self.engine,
            "scope": self.scope,
            "version": self.version,
            "currency": self.currency,
            "note": self.note,
        }

    @classmethod
    def from_record(
        cls,
        record: Any,
        *,
        formula_version: str = FORMULA_VERSION,
        code_version: str = CODE_VERSION_UNKNOWN,
        validation_status: str = "ok",
    ) -> "CalculationNode":
        """Wrap a CARD-03 ``Calculation`` (duck-typed) without recomputing it."""
        inputs = tuple(
            GraphOperand(
                kind=str(getattr(operand, "kind", "")),
                ref=str(getattr(operand, "operand_id", "")),
                value=float(getattr(operand, "value", 0.0)),
                label=str(getattr(operand, "label", "") or ""),
            )
            for operand in getattr(record, "operands", ())
        )
        return cls(
            calculation_id=str(getattr(record, "calculation_id")),
            metric=str(getattr(record, "metric", "")),
            formula=str(getattr(record, "formula", "")),
            operation=str(getattr(record, "operation", "")),
            inputs=inputs,
            output={
                "value": getattr(record, "value", None),
                "unit": getattr(record, "unit", None),
                "period_key": getattr(record, "period_key", None),
                "period_basis": getattr(record, "period_basis", None),
            },
            validation_status=validation_status,
            formula_version=formula_version,
            code_version=code_version,
            source=str(getattr(record, "source", "disclosed")),
            engine=getattr(record, "engine", None),
            scope=getattr(record, "scope", None),
            version=getattr(record, "version", None),
            currency=getattr(record, "currency", None),
            note=getattr(record, "note", None),
        )


@dataclass(frozen=True)
class EvidenceNode:
    """One addressable piece of evidence: a ledger fact or a calculation.

    ``page`` / ``bbox`` are set for document-backed evidence; ``snapshot_id`` +
    ``available_at`` for snapshot-backed evidence. At least one of the two
    locators must be present or the node is reported as unlocatable rather than
    silently accepted (CARD-09 §10 reachability).
    """

    evidence_id: str
    kind: str = "fact"
    document_id: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    snapshot_id: Optional[str] = None
    available_at: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    period_key: Optional[str] = None
    provider: Optional[str] = None
    active: bool = True
    deactivated_reason: Optional[str] = None

    @property
    def locatable(self) -> bool:
        """Reachable from the graph: a document page or a snapshot timestamp."""
        if self.kind == "calculation":
            return True
        return self.page is not None or bool(self.available_at) or bool(self.snapshot_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "document_id": self.document_id,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "snapshot_id": self.snapshot_id,
            "available_at": self.available_at,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "period_key": self.period_key,
            "provider": self.provider,
            "active": self.active,
            "deactivated_reason": self.deactivated_reason,
            "locatable": self.locatable,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceNode":
        bbox = data.get("bbox")
        return cls(
            evidence_id=data["evidence_id"],
            kind=data.get("kind", "fact"),
            document_id=data.get("document_id"),
            page=data.get("page"),
            bbox=tuple(bbox) if bbox else None,
            snapshot_id=data.get("snapshot_id"),
            available_at=data.get("available_at"),
            metric=data.get("metric"),
            value=data.get("value"),
            unit=data.get("unit"),
            period_key=data.get("period_key"),
            provider=data.get("provider"),
            active=bool(data.get("active", True)),
            deactivated_reason=data.get("deactivated_reason"),
        )


@dataclass(frozen=True)
class TraceNode:
    """One level of a ``trace()`` result: conclusion -> … -> PDF page."""

    node_id: str
    node_type: str
    label: str
    detail: Dict[str, Any] = field(default_factory=dict)
    children: Tuple["TraceNode", ...] = ()
    locatable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "detail": dict(self.detail),
            "locatable": self.locatable,
            "children": [child.to_dict() for child in self.children],
        }

    def flatten(self) -> List["TraceNode"]:
        out = [self]
        for child in self.children:
            out.extend(child.flatten())
        return out

    def leaves(self) -> List["TraceNode"]:
        return [node for node in self.flatten() if not node.children]


TraceTree = TraceNode


@dataclass(frozen=True)
class DetectionReport:
    """The three mandatory detections (CARD-09 §10) plus their siblings."""

    run_id: str
    orphan_claims: Tuple[str, ...] = ()
    unsupported_calculations: Tuple[str, ...] = ()
    missing_evidence: Tuple[str, ...] = ()
    missing_claims: Tuple[str, ...] = ()
    missing_calculations: Tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (
            self.orphan_claims
            or self.unsupported_calculations
            or self.missing_evidence
            or self.missing_claims
            or self.missing_calculations
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "orphan_claims": list(self.orphan_claims),
            "unsupported_calculations": list(self.unsupported_calculations),
            "missing_evidence": list(self.missing_evidence),
            "missing_claims": list(self.missing_claims),
            "missing_calculations": list(self.missing_calculations),
            "clean": self.clean,
        }


@dataclass(frozen=True)
class RevalidationReport:
    """Outcome of an incremental re-validation (CARD-09 §6)."""

    run_id: str
    changed_evidence_ids: Tuple[str, ...] = ()
    impacted_claims: Tuple[str, ...] = ()
    status_changes: Tuple[Dict[str, Any], ...] = ()
    blocked_claims: Tuple[str, ...] = ()
    coverage_dropped: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "changed_evidence_ids": list(self.changed_evidence_ids),
            "impacted_claims": list(self.impacted_claims),
            "status_changes": [dict(item) for item in self.status_changes],
            "blocked_claims": list(self.blocked_claims),
            "coverage_dropped": [dict(item) for item in self.coverage_dropped],
        }


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
@dataclass
class ClaimGraph:
    """A serialisable, traversable claim-evidence graph for one run."""

    run_id: str
    claims: Dict[str, Claim] = field(default_factory=dict)
    theses: Dict[str, Thesis] = field(default_factory=dict)
    calculations: Dict[str, CalculationNode] = field(default_factory=dict)
    evidence_index: Dict[str, EvidenceNode] = field(default_factory=dict)
    #: Append-only deactivation log. Mirrors the ledger's no-physical-delete
    #: rule: deactivating evidence adds a record, it never removes one.
    deactivations: List[Dict[str, Any]] = field(default_factory=list)
    #: Append-only stale markers (``{claim_id, reason}``). A claim's staleness is
    #: an **external** trigger — a superseding correction, a newer filing — not
    #: something derivable from the graph's structure. This list is the seam
    #: CARD-25's Temporal Revalidation writes into; propagation reads it.
    stale_claims: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def stale_claim_ids(self) -> set:
        return {str(item.get("claim_id")) for item in self.stale_claims}

    # -- registration -----------------------------------------------------
    def add_claim(self, claim: Claim) -> Claim:
        if claim.claim_id in self.claims:
            raise GraphError(f"duplicate claim_id: {claim.claim_id}")
        self.claims[claim.claim_id] = claim
        return claim

    def add_thesis(self, thesis: Thesis) -> Thesis:
        if thesis.thesis_id in self.theses:
            raise GraphError(f"duplicate thesis_id: {thesis.thesis_id}")
        self.theses[thesis.thesis_id] = thesis
        return thesis

    def add_calculation(self, node: CalculationNode) -> CalculationNode:
        if node.calculation_id in self.calculations:
            raise GraphError(f"duplicate calculation_id: {node.calculation_id}")
        self.calculations[node.calculation_id] = node
        return node

    def add_evidence(self, node: EvidenceNode) -> EvidenceNode:
        if node.evidence_id in self.evidence_index:
            raise GraphError(f"duplicate evidence_id: {node.evidence_id}")
        self.evidence_index[node.evidence_id] = node
        return node

    # -- lookup -----------------------------------------------------------
    def claim(self, claim_id: str) -> Claim:
        record = self.claims.get(claim_id)
        if record is None:
            raise GraphError(f"unknown claim: {claim_id}")
        return record

    def calculation(self, calculation_id: str) -> CalculationNode:
        record = self.calculations.get(calculation_id)
        if record is None:
            raise GraphError(f"unknown calculation: {calculation_id}")
        return record

    def evidence(self, evidence_id: str) -> EvidenceNode:
        record = self.evidence_index.get(evidence_id)
        if record is None:
            raise GraphError(f"unknown evidence: {evidence_id}")
        return record

    def active_evidence(self, evidence_id: str) -> bool:
        node = self.evidence_index.get(evidence_id)
        return bool(node and node.active)

    @property
    def facts(self) -> List[Claim]:
        return [claim for claim in self.claims.values() if claim.claim_type == CLAIM_FACT]

    @property
    def inferences(self) -> List[Claim]:
        return [claim for claim in self.claims.values() if claim.claim_type == CLAIM_INFERENCE]

    @property
    def opinions(self) -> List[Claim]:
        return [claim for claim in self.claims.values() if claim.claim_type == CLAIM_OPINION]

    def fact_claim_for(self, metric: str, period_key: Optional[str] = None) -> Optional[Claim]:
        """The fact claim for a metric (optionally narrowed to one period).

        Returns ``None`` rather than a guess when the metric has no fact claim —
        a renderer must then mark the sentence as unbound instead of inventing an
        id.
        """
        matches = [
            claim
            for claim in self.facts
            if claim.metric == metric and (period_key is None or claim.period_key == period_key)
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: (str(item.period_key), item.claim_id))
        # With a period requested there is exactly one candidate; without one,
        # the latest period is the deterministic choice.
        return matches[-1] if period_key is None else matches[0]

    def claims_for_signal(self, signal_id: str) -> List[Claim]:
        return sorted(
            (claim for claim in self.claims.values() if signal_id in claim.signal_ids),
            key=lambda item: item.claim_id,
        )

    # -- detection (CARD-09 §6 / §10) -------------------------------------
    def detect_orphan_claims(self) -> List[str]:
        """Fact claims that bind no evidence at all."""
        return sorted(claim.claim_id for claim in self.facts if not claim.evidence_ids)

    def detect_unsupported_calculations(self) -> List[str]:
        """Calculations with an input that does not resolve in the graph."""
        unsupported: List[str] = []
        for node in self.calculations.values():
            for ref in node.input_refs:
                if ref in self.calculations or ref in self.evidence_index:
                    continue
                # A constant operand is a formula constant, not a missing input.
                if any(item.kind == "constant" and item.ref == ref for item in node.inputs):
                    continue
                unsupported.append(node.calculation_id)
                break
        return sorted(unsupported)

    def detect_missing_evidence(self) -> List[str]:
        """Every referenced evidence id that cannot be resolved."""
        referenced: set[str] = set()
        for claim in self.claims.values():
            referenced.update(claim.evidence_ids)
            referenced.update(
                ref.target_id
                for ref in claim.dependency_metadata
                if ref.target_type == TARGET_EVIDENCE
            )
        for node in self.calculations.values():
            referenced.update(node.evidence_ids)
        return sorted(ref for ref in referenced if ref not in self.evidence_index)

    def detect_missing_claims(self) -> List[str]:
        """Every referenced claim id that cannot be resolved."""
        referenced: set[str] = set()
        for claim in self.claims.values():
            referenced.update(claim.derived_from)
            referenced.update(
                ref.target_id for ref in claim.dependency_metadata if ref.target_type == TARGET_CLAIM
            )
        for thesis in self.theses.values():
            referenced.update(thesis.claim_ids)
        return sorted(ref for ref in referenced if ref not in self.claims)

    def detect_missing_calculations(self) -> List[str]:
        """Every referenced calculation id that cannot be resolved."""
        referenced: set[str] = set()
        for claim in self.claims.values():
            referenced.update(claim.calculation_ids)
            referenced.update(
                ref.target_id
                for ref in claim.dependency_metadata
                if ref.target_type == TARGET_CALCULATION
            )
        for node in self.calculations.values():
            referenced.update(
                item.ref for item in node.inputs if item.kind == "calculation"
            )
        return sorted(ref for ref in referenced if ref not in self.calculations)

    def detect(self) -> DetectionReport:
        """The full detection report — produced on every run (§10)."""
        return DetectionReport(
            run_id=self.run_id,
            orphan_claims=tuple(self.detect_orphan_claims()),
            unsupported_calculations=tuple(self.detect_unsupported_calculations()),
            missing_evidence=tuple(self.detect_missing_evidence()),
            missing_claims=tuple(self.detect_missing_claims()),
            missing_calculations=tuple(self.detect_missing_calculations()),
        )

    # -- traversal --------------------------------------------------------
    def trace(self, claim_id: str, *, max_depth: int = 8) -> TraceTree:
        """Walk conclusion -> evidence -> calculation -> PDF page.

        Cycle-safe: a dependency already on the current path is expanded once
        and then reported as a ``cycle`` child rather than recursed forever.
        """
        return self._trace_claim(claim_id, depth=0, max_depth=max_depth, path=set())

    def _trace_claim(self, claim_id: str, *, depth: int, max_depth: int, path: set) -> TraceNode:
        claim = self.claims.get(claim_id)
        if claim is None:
            return TraceNode(
                node_id=claim_id,
                node_type="missing",
                label=f"未解析的 claim：{claim_id}",
                detail={"missing": True},
                locatable=False,
            )
        if claim_id in path or depth >= max_depth:
            return TraceNode(
                node_id=claim_id,
                node_type="claim",
                label=claim.text or claim_id,
                detail={"claim_id": claim_id, "cycle": claim_id in path, "truncated": depth >= max_depth},
                children=(),
                locatable=False,
            )
        path = path | {claim_id}
        children: List[TraceNode] = []
        for evidence_id in claim.evidence_ids:
            children.append(self._trace_evidence(evidence_id, depth=depth + 1, max_depth=max_depth, path=path))
        for calculation_id in claim.calculation_ids:
            children.append(
                self._trace_calculation(calculation_id, depth=depth + 1, max_depth=max_depth, path=path)
            )
        for upstream in claim.derived_from:
            children.append(self._trace_claim(upstream, depth=depth + 1, max_depth=max_depth, path=path))
        node = TraceNode(
            node_id=claim_id,
            node_type="claim",
            label=claim.text or claim_id,
            detail={
                "claim_id": claim_id,
                "claim_type": claim.claim_type,
                "temporal_status": claim.temporal_status,
                "validation_status": claim.validation_status,
                "verification_status": claim.verification_status,
                "dependency_metadata": [ref.to_dict() for ref in claim.dependency_metadata],
            },
            children=tuple(children),
            locatable=any(child.locatable for child in children),
        )
        return node

    def _trace_evidence(self, evidence_id: str, *, depth: int, max_depth: int, path: set) -> TraceNode:
        node = self.evidence_index.get(evidence_id)
        if node is None:
            return TraceNode(
                node_id=evidence_id,
                node_type="missing",
                label=f"悬空证据：{evidence_id}",
                detail={"missing": True},
                locatable=False,
            )
        children: List[TraceNode] = []
        if node.page is not None:
            children.append(
                TraceNode(
                    node_id=f"{evidence_id}@page{node.page}",
                    node_type="page",
                    label=f"{node.document_id or '文档'} 第 {node.page} 页",
                    detail={
                        "document_id": node.document_id,
                        "page": node.page,
                        "bbox": list(node.bbox) if node.bbox else None,
                    },
                    locatable=True,
                )
            )
        if node.available_at:
            children.append(
                TraceNode(
                    node_id=f"{evidence_id}@snapshot",
                    node_type="snapshot",
                    label=f"快照 {node.snapshot_id or ''} available_at={node.available_at}",
                    detail={"snapshot_id": node.snapshot_id, "available_at": node.available_at},
                    locatable=True,
                )
            )
        return TraceNode(
            node_id=evidence_id,
            node_type="evidence",
            label=f"{node.metric or node.evidence_id} = {node.value}",
            detail=node.to_dict(),
            children=tuple(children),
            locatable=node.locatable and node.active,
        )

    def _trace_calculation(
        self, calculation_id: str, *, depth: int, max_depth: int, path: set
    ) -> TraceNode:
        node = self.calculations.get(calculation_id)
        if node is None:
            return TraceNode(
                node_id=calculation_id,
                node_type="missing",
                label=f"悬空计算：{calculation_id}",
                detail={"missing": True},
                locatable=False,
            )
        children: List[TraceNode] = []
        if depth < max_depth:
            for item in node.inputs:
                if item.kind == "evidence":
                    children.append(
                        self._trace_evidence(item.ref, depth=depth + 1, max_depth=max_depth, path=path)
                    )
                elif item.kind == "calculation":
                    children.append(
                        self._trace_calculation(item.ref, depth=depth + 1, max_depth=max_depth, path=path)
                    )
        return TraceNode(
            node_id=calculation_id,
            node_type="calculation",
            label=f"{node.formula} = {node.value}",
            detail=node.to_dict(),
            children=tuple(children),
            locatable=any(child.locatable for child in children) if children else True,
        )

    # -- propagation (implemented in propagation.py) ----------------------
    def propagate_state(self, changed_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        from .propagation import propagate_state as _propagate

        return _propagate(self, changed_ids=changed_ids)

    def coverage(self) -> Dict[str, Any]:
        """Evidence-coverage statistics for CARD-11's benchmark metrics."""
        required = 0
        satisfied = 0
        for claim in self.claims.values():
            refs = tuple(claim.dependency_metadata)
            targets = [ref.target_id for ref in refs if ref.role == ROLE_REQUIRED]
            if not targets:
                continue
            required += len(targets)
            for target_id, ref in zip(targets, [r for r in refs if r.role == ROLE_REQUIRED]):
                if self._resolves(target_id, ref.target_type):
                    satisfied += 1
        unsupported = len(self.detect_unsupported_calculations())
        orphans = len(self.detect_orphan_claims())
        return {
            "run_id": self.run_id,
            "claims": len(self.claims),
            "facts": len(self.facts),
            "inferences": len(self.inferences),
            "opinions": len(self.opinions),
            "theses": len(self.theses),
            "required_edges": required,
            "required_satisfied": satisfied,
            "evidence_coverage": (satisfied / required) if required else 1.0,
            "orphan_claims": orphans,
            "unsupported_claims": orphans + unsupported,
        }

    def _resolves(self, target_id: str, target_type: str) -> bool:
        if target_type == TARGET_CLAIM:
            return target_id in self.claims
        if target_type == TARGET_EVIDENCE:
            return target_id in self.evidence_index
        if target_type == TARGET_CALCULATION:
            return target_id in self.calculations
        if target_type == TARGET_ASSUMPTION:
            return bool(target_id)
        return False

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "claims": {cid: claim.to_dict() for cid, claim in sorted(self.claims.items())},
            "theses": {tid: thesis.to_dict() for tid, thesis in sorted(self.theses.items())},
            "calculations": {
                cid: node.to_dict() for cid, node in sorted(self.calculations.items())
            },
            "evidence_index": {
                eid: node.to_dict() for eid, node in sorted(self.evidence_index.items())
            },
            "deactivations": [dict(item) for item in self.deactivations],
            "stale_claims": [dict(item) for item in self.stale_claims],
            "warnings": list(self.warnings),
        }

    # ``export_json`` is the card's name for the same payload.
    def export_json(self) -> Dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClaimGraph":
        graph = cls(run_id=data["run_id"])
        graph.claims = {cid: Claim.from_dict(item) for cid, item in (data.get("claims") or {}).items()}
        graph.theses = {tid: Thesis.from_dict(item) for tid, item in (data.get("theses") or {}).items()}
        for cid, item in (data.get("calculations") or {}).items():
            graph.calculations[cid] = CalculationNode(
                calculation_id=item["calculation_id"],
                metric=item.get("metric", ""),
                formula=item.get("formula", ""),
                operation=item.get("operation", ""),
                inputs=tuple(
                    GraphOperand(
                        kind=op.get("kind", ""),
                        ref=op.get("ref", ""),
                        value=float(op.get("value", 0.0)),
                        label=op.get("label", ""),
                    )
                    for op in (item.get("inputs") or ())
                ),
                output=dict(item.get("output") or {}),
                validation_status=item.get("validation_status", "ok"),
                formula_version=item.get("formula_version", FORMULA_VERSION),
                code_version=item.get("code_version", CODE_VERSION_UNKNOWN),
                source=item.get("source", "disclosed"),
                engine=item.get("engine"),
                scope=item.get("scope"),
                version=item.get("version"),
                currency=item.get("currency"),
                note=item.get("note"),
            )
        graph.evidence_index = {
            eid: EvidenceNode.from_dict(item) for eid, item in (data.get("evidence_index") or {}).items()
        }
        graph.deactivations = [dict(item) for item in (data.get("deactivations") or [])]
        graph.stale_claims = [dict(item) for item in (data.get("stale_claims") or [])]
        graph.warnings = list(data.get("warnings") or [])
        return graph


__all__ = [
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
    # errors
    "GraphError", "GraphCycleError", "GraphBindingError",
    # nodes
    "DependencyRef", "Claim", "Thesis", "GraphOperand", "CalculationNode", "EvidenceNode",
    "TraceNode", "TraceTree", "DetectionReport", "RevalidationReport",
    # graph
    "ClaimGraph",
]
