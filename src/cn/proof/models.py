"""CARD-23 §6 — Financial Proof Object（结论护照）与验证结果的数据模型.

一个 FPO 要让一条金融 Claim 能够被**追溯、重新计算、重新验证、判断是否仍然有效**，
所以它的字段不是「引用」而是六块**证明**：来源 / 计算 / 假设 / 依赖 / 校验 / 完整性。
本模块只放**结构与不变量**，判定逻辑在 :mod:`src.cn.proof.passport`。

三条设计不变量（都不是风格问题）：

1. **表比对象先立。** 五态的定义、检查项的取值域、状态合并的严重度次序，都是本模块的
   模块级常量。任何一处写死字符串都会让「合并规则有单测」变成一句空话。
2. **全部 frozen。** 一份被验证过的证明对象不应在验证之后还能被改 —— 否则
   ``verify_claim() == VERIFIED`` 与 ``passport`` 表达的可能不是同一个东西。
3. **哈希可重算。** ``object_hash`` 覆盖「除自身以外的全部字段」，故 :meth:`seal` 幂等；
   §13 才可能断言「proof_hash 可重算」而不是「信任生成时写下的那个值」。

**与卡片 §6 的一处偏离**：卡片把六块证明写成 ``dict``（且给了 ``= {}`` 的可变默认值）。
这里改为 frozen 子模型 + ``to_dict()``。原因有两条：可变默认值在 dataclass 上是共享对象
（一处改动会渗到所有实例）；以及 §13 要求 ``proof_hash`` 可重算，需要**规范化且字段完备**
的序列化，而 dict 的「缺键」与「空值」无法区分 —— 那正好是本项目禁止的静默。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple

from ..graph import GraphOperand
from ..runs.models import digest_of

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
#: Bumped whenever the FPO *shape* changes — a verifier must be able to say
#: which generation of proof object it was looking at.
PROOF_VERSION = "card23.v1"
#: Bumped whenever a *check* changes meaning. §11 requires this be versioned and
#: covered by tests, so "the verifier always passes" cannot go unnoticed.
VERIFIER_VERSION = "card23.v1"

#: What a gid/git lookup returns when the run did not record a commit.
COMMIT_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# §7 — the five states, in severity order (most severe first)
# ---------------------------------------------------------------------------
STATUS_VERIFIED = "VERIFIED"
STATUS_DEGRADED = "DEGRADED"
STATUS_STALE = "STALE"
STATUS_BLOCKED = "BLOCKED"
STATUS_CONFLICTED = "CONFLICTED"

PROOF_STATUSES: Tuple[str, ...] = (
    STATUS_VERIFIED,
    STATUS_DEGRADED,
    STATUS_STALE,
    STATUS_BLOCKED,
    STATUS_CONFLICTED,
)

#: §7 merge table, most severe first. ``BLOCKED`` outranks ``CONFLICTED`` because
#: a blocked proof项 means "cannot be trusted at all", while a conflicted one at
#: least names the two values that disagree. Both are fail-closed.
STATUS_PRECEDENCE: Tuple[str, ...] = (
    STATUS_BLOCKED,
    STATUS_CONFLICTED,
    STATUS_STALE,
    STATUS_DEGRADED,
    STATUS_VERIFIED,
)
STATUS_RANK: Dict[str, int] = {name: index for index, name in enumerate(STATUS_PRECEDENCE)}

# ---------------------------------------------------------------------------
# Per-check outcome
# ---------------------------------------------------------------------------
CHECK_PASS = "pass"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_SKIPPED = "skipped"
CHECK_STATUSES: Tuple[str, ...] = (CHECK_PASS, CHECK_WARN, CHECK_FAIL, CHECK_SKIPPED)

#: Statuses that push the overall verdict somewhere (§7's merge rule).
CONTRIBUTING_CHECK_STATUSES: Tuple[str, ...] = (CHECK_WARN, CHECK_FAIL)

CHECK_SOURCE = "source"
CHECK_PERIOD = "period"
CHECK_SCOPE_UNIT = "scope_unit"
CHECK_CALCULATION = "calculation"
CHECK_DEPENDENCY = "dependencies"
CHECK_ASSUMPTION = "assumptions"
CHECK_VALIDATOR = "validator_state"
CHECK_INTEGRITY = "integrity"
CHECK_CONFLICT = "conflict"

#: How far the source check actually got on the document hash (§7.1). §7.1 asks
#: for a *recomputation*; the other three states mean it did not happen, and the
#: difference between "重算过" and "只是交叉核对过" must survive into the report.
HASH_RECOMPUTED = "recomputed"
HASH_IN_MANIFEST = "in_manifest_only"
HASH_UNREADABLE = "unreadable_bytes"
HASH_UNCHECKED = "not_checked"

HASH_STATE_LABELS: Dict[str, str] = {
    HASH_RECOMPUTED: "已按当前字节重算",
    HASH_IN_MANIFEST: "仅比对本 run 输入清单，未重算字节",
    HASH_UNREADABLE: "文档字节不可读，无法重算",
    HASH_UNCHECKED: "既无字节也无 run 清单，未核对",
}

#: §7 numbers nine checks; §8 names seven ``verify_*`` functions. This tuple is
#: the superset actually run, in the order the report lists them.
PROOF_CHECKS: Tuple[str, ...] = (
    CHECK_SOURCE,
    CHECK_PERIOD,
    CHECK_SCOPE_UNIT,
    CHECK_CALCULATION,
    CHECK_DEPENDENCY,
    CHECK_ASSUMPTION,
    CHECK_VALIDATOR,
    CHECK_INTEGRITY,
    CHECK_CONFLICT,
)

# ---------------------------------------------------------------------------
# Version status vocabulary (§6) — shared with CARD-02/04's restatement story
# ---------------------------------------------------------------------------
VERSION_AS_REPORTED = "AS_REPORTED"
VERSION_RESTATED = "RESTATED"
VERSION_CORRECTED = "CORRECTED"
VERSION_SUPERSEDED = "SUPERSEDED"
VERSION_UNKNOWN = "UNKNOWN"
VERSION_STATUSES: Tuple[str, ...] = (
    VERSION_AS_REPORTED,
    VERSION_RESTATED,
    VERSION_CORRECTED,
    VERSION_SUPERSEDED,
    VERSION_UNKNOWN,
)

#: Why we believe ``version_status``. §7 check 2 turns on this, and an
#: unexplained version label is exactly the kind of silent assumption this
#: project refuses, so the basis travels with the value.
VERSION_BASIS_DECLARED = "caller_declared"
VERSION_BASIS_SUPERSESSION = "card13_supersession_record"
VERSION_BASIS_EVIDENCE_INACTIVE = "evidence_deactivated"
VERSION_BASIS_NO_SIGNAL = "no_supersession_signal"

#: Where ``AccountingContext.scope`` came from. The declared 口径 and the derived
#: one must be held **separately**: if the declaration overwrote the derivation,
#: §7 check 4 would be comparing a value with itself and could never disagree —
#: which is exactly the defect it exists to catch.
SCOPE_BASIS_CALCULATION = "calculation_node"
SCOPE_BASIS_UNDECLARED = "undeclared"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProofError(ValueError):
    """Base class for proof-layer failures."""


class ProofBuildError(ProofError):
    """An FPO could not be built for a claim (§11 ``proof_build_failed``)."""


# ---------------------------------------------------------------------------
# Check outcome
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckOutcome:
    """One check's result, carrying what its non-passing means for the whole.

    ``impact`` exists so the merge rule is *data* rather than an if-ladder: a
    writer of a new check must state "if this warns, the claim is at least
    X", and :func:`src.cn.proof.passport.merge_status` just takes the most
    severe floor. Without it, every new check silently becomes blocking-forever.
    """

    check: str
    status: str
    message: str
    impact: str = STATUS_BLOCKED
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.check not in PROOF_CHECKS:
            raise ProofError(f"unknown check: {self.check!r}")
        if self.status not in CHECK_STATUSES:
            raise ProofError(f"unknown check status: {self.status!r}")
        if self.impact not in PROOF_STATUSES:
            raise ProofError(f"unknown impact: {self.impact!r}")

    @property
    def contributing(self) -> bool:
        return self.status in CONTRIBUTING_CHECK_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "status": self.status,
            "message": self.message,
            "impact": self.impact,
            "details": dict(self.details),
        }


def passed(check: str, message: str, **details: Any) -> CheckOutcome:
    return CheckOutcome(check, CHECK_PASS, message, STATUS_BLOCKED, details)


def warned(check: str, message: str, *, impact: str = STATUS_DEGRADED,
           **details: Any) -> CheckOutcome:
    return CheckOutcome(check, CHECK_WARN, message, impact, details)


def failed(check: str, message: str, *, impact: str = STATUS_BLOCKED,
           **details: Any) -> CheckOutcome:
    return CheckOutcome(check, CHECK_FAIL, message, impact, details)


def skipped(check: str, message: str, **details: Any) -> CheckOutcome:
    """Not checked, with the reason attached. Never a silent pass.

    §12 edge 1/2 write "该检查 PASS（跳过）". Implemented as ``skipped`` rather
    than ``pass`` on purpose: this project distinguishes "查过且没问题" from
    "没查", and a ``pass`` would erase that difference from the report. A
    skipped check contributes nothing to the merge, so the overall verdict is
    the same either way — only the honesty differs.
    """
    return CheckOutcome(check, CHECK_SKIPPED, message, STATUS_BLOCKED, details)


# ---------------------------------------------------------------------------
# Six proof blocks (§6)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceProof:
    """Where the claim's number physically is."""

    document_id: Optional[str] = None
    sha256: Optional[str] = None
    page: Optional[int] = None
    locator: str = ""
    evidence_ids: Tuple[str, ...] = ()
    available_at: Optional[str] = None
    snapshot_id: Optional[str] = None

    @property
    def locatable(self) -> bool:
        return self.page is not None or bool(self.available_at) or bool(self.snapshot_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "sha256": self.sha256,
            "page": self.page,
            "locator": self.locator,
            "evidence_ids": list(self.evidence_ids),
            "available_at": self.available_at,
            "snapshot_id": self.snapshot_id,
            "locatable": self.locatable,
        }


@dataclass(frozen=True)
class AccountingContext:
    """The measurement: which period, which 口径, which unit, which version.

    ``version_basis`` says *why* ``version_status`` holds — see
    :data:`VERSION_BASIS_*`. Keeping the reason next to the label is what makes
    §7 check 2 defensible instead of a lookup we cannot explain.

    ``scope`` is the **derived** 口径 (where the numbers actually came from) and
    ``declared_scope`` is what the caller *says* the claim is about. They are two
    values on purpose: collapsing them would make §7 check 4 a comparison of a
    value against itself.
    """

    period: str = ""
    period_basis: str = "UNKNOWN"
    scope: str = ""
    currency: str = ""
    unit: str = ""
    version_basis: str = VERSION_BASIS_NO_SIGNAL
    declared_scope: str = ""
    scope_basis: str = SCOPE_BASIS_UNDECLARED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "period": self.period,
            "period_basis": self.period_basis,
            "scope": self.scope,
            "declared_scope": self.declared_scope,
            "scope_basis": self.scope_basis,
            "currency": self.currency,
            "unit": self.unit,
            "version_basis": self.version_basis,
        }


@dataclass(frozen=True)
class CalculationProof:
    """The arithmetic, kept as data so it can be re-executed (§7 check 6)."""

    calculation_id: str
    metric: str
    formula: str
    operation: str
    inputs: Tuple[GraphOperand, ...]
    output: Optional[float]
    formula_version: str = ""
    code_version: str = COMMIT_UNKNOWN
    engine: Optional[str] = None
    period_key: Optional[str] = None
    unit: Optional[str] = None

    @property
    def input_refs(self) -> Tuple[str, ...]:
        return tuple(item.ref for item in self.inputs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calculation_id": self.calculation_id,
            "metric": self.metric,
            "formula": self.formula,
            "operation": self.operation,
            "formula_version": self.formula_version,
            "inputs": [item.to_dict() for item in self.inputs],
            "output": self.output,
            "code_version": self.code_version,
            "engine": self.engine,
            "period_key": self.period_key,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class AssumptionProof:
    """§6 ``{assumption_ids[], sources[], rationale}`` + the CARD-08 grade."""

    assumption_ids: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()
    rationale: str = ""
    #: CARD-08's ``sourced`` / ``assumption`` grade per id — kept because a
    #: *sourced* assumption and a hand-set one must not look alike downstream.
    grades: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assumption_ids": list(self.assumption_ids),
            "sources": list(self.sources),
            "rationale": self.rationale,
            "grades": dict(self.grades),
        }


@dataclass(frozen=True)
class UpstreamProof:
    """One upstream node as it stood when the passport was built."""

    target_id: str
    target_type: str
    role: str
    state: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_type": self.target_type,
            "role": self.role,
            "state": self.state,
        }


@dataclass(frozen=True)
class DependencyProof:
    upstream: Tuple[UpstreamProof, ...] = ()

    @property
    def blocked(self) -> Tuple[str, ...]:
        return tuple(item.target_id for item in self.upstream if item.state != "ok")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upstream": [item.to_dict() for item in self.upstream],
            "blocked": list(self.blocked),
        }


@dataclass(frozen=True)
class ValidationProof:
    """Which validator rules were consulted and what they said."""

    validator_rules: Tuple[str, ...] = ()
    result: str = ""
    blocked_reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "validator_rules": list(self.validator_rules),
            "result": self.result,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class IntegrityProof:
    """§6 ``{object_hash, audit_chain_ref, run_id, git_commit}``.

    ``object_hash`` is a hash of the *other* fields (including the other three
    in this block), which is why :meth:`FinancialProofObject.payload` strips it:
    a hash that covered itself could never be recomputed.
    """

    object_hash: str = ""
    audit_chain_ref: str = ""
    run_id: str = ""
    git_commit: str = COMMIT_UNKNOWN
    proof_version: str = PROOF_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_hash": self.object_hash,
            "audit_chain_ref": self.audit_chain_ref,
            "run_id": self.run_id,
            "git_commit": self.git_commit,
            "proof_version": self.proof_version,
        }


# ---------------------------------------------------------------------------
# §6 — FinancialProofObject
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FinancialProofObject:
    """结论护照：a claim plus the six proofs it rests on."""

    claim_id: str
    claim_type: str
    claim_text: str
    economic_period: str
    scope: str
    version_status: str
    source_proof: SourceProof = field(default_factory=SourceProof)
    accounting_context: AccountingContext = field(default_factory=AccountingContext)
    calculation_proof: Optional[CalculationProof] = None
    assumption_proof: AssumptionProof = field(default_factory=AssumptionProof)
    dependency_proof: DependencyProof = field(default_factory=DependencyProof)
    validation_proof: ValidationProof = field(default_factory=ValidationProof)
    integrity_proof: IntegrityProof = field(default_factory=IntegrityProof)
    claim_metric: Optional[str] = None
    claim_period_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.claim_id:
            raise ProofError("FPO 缺少 claim_id")
        if self.version_status not in VERSION_STATUSES:
            raise ProofError(f"unknown version_status: {self.version_status!r}")

    # -- identity ---------------------------------------------------------
    @property
    def proof_id(self) -> str:
        """Deterministic: one claim has one passport, not one per run."""
        return f"fpo:{self.claim_id}"

    # -- hashing ----------------------------------------------------------
    def payload(self) -> Dict[str, Any]:
        """``to_dict()`` minus ``object_hash`` — the thing the hash covers."""
        data = self.to_dict()
        data["integrity_proof"].pop("object_hash", None)
        return data

    def canonical_payload(self) -> str:
        """The exact JSON text :meth:`proof_hash` digests.

        Deliberately the same serialisation CARD-07's :func:`digest_of` uses, so
        the two can never disagree about what "the same object" means.
        """
        return json.dumps(self.payload(), sort_keys=True, ensure_ascii=False, default=str)

    def proof_hash(self) -> str:
        return digest_of(self.payload())

    def seal(self) -> "FinancialProofObject":
        """Return a copy whose ``object_hash`` matches :meth:`proof_hash`.

        Idempotent: the hash excludes ``object_hash``, so sealing twice yields
        the same digest. ``verify_integrity`` relies on that.
        """
        return replace(
            self,
            integrity_proof=replace(self.integrity_proof, object_hash=self.proof_hash()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "claim_text": self.claim_text,
            "economic_period": self.economic_period,
            "scope": self.scope,
            "version_status": self.version_status,
            "claim_metric": self.claim_metric,
            "claim_period_key": self.claim_period_key,
            "source_proof": self.source_proof.to_dict(),
            "accounting_context": self.accounting_context.to_dict(),
            "calculation_proof": (
                self.calculation_proof.to_dict() if self.calculation_proof else None
            ),
            "assumption_proof": self.assumption_proof.to_dict(),
            "dependency_proof": self.dependency_proof.to_dict(),
            "validation_proof": self.validation_proof.to_dict(),
            "integrity_proof": self.integrity_proof.to_dict(),
        }


# ---------------------------------------------------------------------------
# §6 — VerificationResult
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VerificationResult:
    """The五态 verdict plus the per-check trail that justifies it."""

    proof_id: str
    claim_id: str
    overall_status: str
    status_reasons: Tuple[str, ...]
    checks: Tuple[CheckOutcome, ...]
    verify_source_version: str = VERIFIER_VERSION
    verified_at: str = ""

    def __post_init__(self) -> None:
        if self.overall_status not in PROOF_STATUSES:
            raise ProofError(f"unknown overall_status: {self.overall_status!r}")

    @property
    def passed(self) -> bool:
        return self.overall_status == STATUS_VERIFIED

    @property
    def blocking(self) -> bool:
        """Whether this claim must stay out of the memo's conclusion sections."""
        return self.overall_status in (STATUS_BLOCKED, STATUS_CONFLICTED)

    def check(self, name: str) -> Optional[CheckOutcome]:
        for item in self.checks:
            if item.check == name:
                return item
        return None

    @property
    def failing_checks(self) -> Tuple[CheckOutcome, ...]:
        return tuple(item for item in self.checks if item.status == CHECK_FAIL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proof_id": self.proof_id,
            "claim_id": self.claim_id,
            "overall_status": self.overall_status,
            "status_reasons": list(self.status_reasons),
            "checks": [item.to_dict() for item in self.checks],
            "verify_source_version": self.verify_source_version,
            "verified_at": self.verified_at,
        }


# ---------------------------------------------------------------------------
# §7 — the merge rule, as a pure function of the check outcomes
# ---------------------------------------------------------------------------
def merge_status(checks: Tuple[CheckOutcome, ...]) -> Tuple[str, Tuple[str, ...]]:
    """``(overall_status, reasons)`` from the checks — §7's table, mechanised.

    Only ``warn``/``fail`` contribute; ``pass`` and ``skipped`` never move the
    verdict. The most severe ``impact`` among contributors wins, so a single
    failed REQUIRED dependency blocks the claim no matter how many other checks
    passed.
    """
    worst = STATUS_VERIFIED
    reasons: list[str] = []
    for item in checks:
        if not item.contributing:
            continue
        if STATUS_RANK[item.impact] < STATUS_RANK[worst]:
            worst = item.impact
        reasons.append(f"{item.check}({item.status}) → {item.impact}：{item.message}")
    if worst == STATUS_VERIFIED:
        reasons.append("全部证明项通过（skipped 项已逐条列明原因）")
    return worst, tuple(reasons)


__all__ = [
    "AccountingContext",
    "AssumptionProof",
    "CHECK_ASSUMPTION",
    "CHECK_CALCULATION",
    "CHECK_CONFLICT",
    "CHECK_DEPENDENCY",
    "CHECK_FAIL",
    "CHECK_INTEGRITY",
    "CHECK_PASS",
    "CHECK_PERIOD",
    "CHECK_SCOPE_UNIT",
    "CHECK_SKIPPED",
    "CHECK_SOURCE",
    "CHECK_STATUSES",
    "CHECK_VALIDATOR",
    "CHECK_WARN",
    "COMMIT_UNKNOWN",
    "CONTRIBUTING_CHECK_STATUSES",
    "CalculationProof",
    "CheckOutcome",
    "DependencyProof",
    "FinancialProofObject",
    "HASH_IN_MANIFEST",
    "HASH_RECOMPUTED",
    "HASH_STATE_LABELS",
    "HASH_UNCHECKED",
    "HASH_UNREADABLE",
    "IntegrityProof",
    "PROOF_CHECKS",
    "PROOF_STATUSES",
    "PROOF_VERSION",
    "SCOPE_BASIS_CALCULATION",
    "SCOPE_BASIS_UNDECLARED",
    "ProofBuildError",
    "ProofError",
    "STATUS_BLOCKED",
    "STATUS_CONFLICTED",
    "STATUS_DEGRADED",
    "STATUS_PRECEDENCE",
    "STATUS_RANK",
    "STATUS_STALE",
    "STATUS_VERIFIED",
    "SourceProof",
    "UpstreamProof",
    "VERSION_AS_REPORTED",
    "VERSION_BASIS_DECLARED",
    "VERSION_BASIS_EVIDENCE_INACTIVE",
    "VERSION_BASIS_NO_SIGNAL",
    "VERSION_BASIS_SUPERSESSION",
    "VERSION_CORRECTED",
    "VERSION_RESTATED",
    "VERSION_STATUSES",
    "VERSION_SUPERSEDED",
    "VERSION_UNKNOWN",
    "VERIFIER_VERSION",
    "ValidationProof",
    "VerificationResult",
    "failed",
    "merge_status",
    "passed",
    "skipped",
    "warned",
]
