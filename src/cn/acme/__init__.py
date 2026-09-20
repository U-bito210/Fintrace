"""CARD-22 — ACME: Accounting-Constrained Multimodal Evidence Engine.

A filing is not trusted because it parsed. It is trusted because a deterministic
constraint was checked against it, and because the *absence* of a check is
visible in the output.

Six constraint families (§7): accounting identity, table structural, period,
cross-period, cross-source, cross-modal. All of them are deterministic — this
package imports nothing from ``src.llms`` and never asks a model to decide
whether two numbers agree (§9).

Public surface::

    from src.cn.acme import validate_multimodal_evidence, DEFAULT_POLICY

The tool wrapper is ``ValidateCnAcmeTool`` in ``src.agents.tools.cn_tools``.
"""

from __future__ import annotations

from .cross_modal import ChartAnnotation, BodyClaim, parse_body_claims, ratio_series
from .models import (
    CHECK_STATUSES,
    CONSTRAINT_FAMILIES,
    CONFLICT_TYPES,
    ConstraintAvailability,
    ConstraintOperand,
    ConstraintViolation,
    CorrectionTrail,
    CrossModalInconsistency,
    IntegrityReport,
    SourceRef,
    VALIDATOR_VERSION,
)
from .tolerance import DEFAULT_POLICY, ToleranceExemption, TolerancePolicy
from .validator import validate_multimodal_evidence

__all__ = [
    "CHECK_STATUSES",
    "CONSTRAINT_FAMILIES",
    "CONFLICT_TYPES",
    "ChartAnnotation",
    "BodyClaim",
    "ConstraintAvailability",
    "ConstraintOperand",
    "ConstraintViolation",
    "CorrectionTrail",
    "CrossModalInconsistency",
    "DEFAULT_POLICY",
    "IntegrityReport",
    "SourceRef",
    "ToleranceExemption",
    "TolerancePolicy",
    "VALIDATOR_VERSION",
    "parse_body_claims",
    "ratio_series",
    "validate_multimodal_evidence",
]
