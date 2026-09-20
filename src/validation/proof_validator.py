"""CARD-23 §13 — passports and verification results, checked rather than trusted.

The card names three rules; each becomes a check that can actually fail:

1. **``proof_hash`` 可重算验证（断言）.** A passport whose hash does not
   recompute has been edited after sealing — which is the one thing a "proof"
   object must make impossible. Checked by recomputing, never by trusting the
   stored value.
2. **``overall_status`` 与各子项一致（合并规则有单测）.** The verdict must be
   exactly what §7's table yields from the checks. A verifier that could return
   ``VERIFIED`` over a failed REQUIRED dependency would make the whole five-state
   vocabulary decorative.
3. **``BLOCKED``/``CONFLICTED`` 的 claim 不得进入最终 Memo 的结论段落；
   ``STALE`` 只允许带显式「待刷新」标注.** This is why the passport layer exists
   at all: a conclusion the system cannot stand behind must not be rendered as
   advice. Enforced against CARD-10's sentence-level markings, so the check reads
   the same objects the memo is built from rather than re-parsing prose.

**Where the conclusion sections are named.** The card says 结论段落 without
enumerating them, so the list lives here as :data:`CONCLUSION_SECTIONS` and is
exported: the sections that assert a stance (执行摘要 / 投资逻辑 / 估值 / 催化剂 /
风险 / 反向论证 / 证伪条件 / 跟踪指标). 公司概况 and 财务表现 report facts rather
than conclusions, and the 证据附录 is an appendix — the blocked-fact case in those
sections is CARD-10's own validator's business, and duplicating the rule would
give the project two definitions of the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cn.memo import (
    SECTION_CATALYSTS,
    SECTION_COUNTER_THESIS,
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_FALSIFICATION,
    SECTION_INVESTMENT_THESIS,
    SECTION_RISKS,
    SECTION_TRACKING_KPIS,
    SECTION_VALUATION,
)
from src.cn.proof.models import (
    CHECK_STATUSES,
    PROOF_CHECKS,
    PROOF_STATUSES,
    STATUS_CONFLICTED,
    STATUS_STALE,
    STATUS_VERIFIED,
    CheckOutcome,
    FinancialProofObject,
    VerificationResult,
    merge_status,
)

#: Sections that assert an investment stance — see the module docstring.
CONCLUSION_SECTIONS: Tuple[str, ...] = (
    SECTION_EXECUTIVE_SUMMARY,
    SECTION_INVESTMENT_THESIS,
    SECTION_VALUATION,
    SECTION_CATALYSTS,
    SECTION_RISKS,
    SECTION_COUNTER_THESIS,
    SECTION_FALSIFICATION,
    SECTION_TRACKING_KPIS,
)

#: The marker §13 requires on a STALE claim that does appear in a conclusion.
REFRESH_MARKER = "待刷新"


@dataclass(frozen=True)
class ProofValidation:
    """Outcome of validating one passport (or a corpus of them)."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings)}


def validate_proof(
    passport: FinancialProofObject,
    *,
    verification: Optional[VerificationResult] = None,
    memo: Any = None,
) -> ProofValidation:
    """§13 over one passport, optionally against its verdict and a memo."""
    errors: List[str] = []
    warnings: List[str] = []

    _check_structure(passport, errors)
    _check_proof_hash(passport, errors)
    if verification is None:
        warnings.append(
            "no_verification_supplied: §13 规则 2/3 需要验证结果与 memo，"
            "本次只校验护照本体（未检查项不视为通过）"
        )
        return ProofValidation(valid=not errors, errors=errors, warnings=warnings)

    _check_verification_consistency(passport, verification, errors)
    if memo is None:
        warnings.append(
            "no_memo_supplied: §13 规则 3（结论段禁用 BLOCKED/CONFLICTED）未检查"
        )
    else:
        _check_verdict_in_memo(passport, verification, memo, errors, warnings)

    return ProofValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_verification(result: VerificationResult) -> ProofValidation:
    """§13 rule 2 alone — used when only the verdict is on hand."""
    errors: List[str] = []
    warnings: List[str] = []
    _check_checks(result.checks, errors)
    overall, _reasons = merge_status(result.checks)
    if overall != result.overall_status:
        errors.append(
            f"{result.proof_id}: overall_status {result.overall_status} 与检查项合并结果 "
            f"{overall} 不一致（§7 合并规则）"
        )
    if result.verify_source_version == "":
        errors.append(f"{result.proof_id}: 缺少 verify_source_version（验证器版本未记录）")
    return ProofValidation(valid=not errors, errors=errors, warnings=warnings)


def validate_proof_corpus(
    items: Iterable[Tuple[FinancialProofObject, Optional[VerificationResult]]],
    *,
    memo: Any = None,
) -> ProofValidation:
    errors: List[str] = []
    warnings: List[str] = []
    seen = 0
    for passport, verification in items:
        seen += 1
        result = validate_proof(passport, verification=verification, memo=memo)
        errors.extend(f"{passport.claim_id}: {item}" for item in result.errors)
        warnings.extend(f"{passport.claim_id}: {item}" for item in result.warnings)
    if seen == 0:
        warnings.append("no_passports_supplied")
    return ProofValidation(valid=not errors, errors=errors, warnings=warnings)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _check_structure(passport: FinancialProofObject, errors: List[str]) -> None:
    if not passport.claim_id:
        errors.append("passport 缺少 claim_id")
    if passport.version_status not in (
        "AS_REPORTED", "RESTATED", "CORRECTED", "SUPERSEDED", "UNKNOWN",
    ):
        errors.append(f"{passport.claim_id}: 未知 version_status {passport.version_status!r}")
    if not passport.source_proof.evidence_ids and passport.claim_type != "opinion":
        # An opinion may legitimately rest on assumptions alone; a fact may not.
        warnings.append(
            f"{passport.claim_id}: 未绑定证据（孤儿 claim），source 检查应报 BLOCKED"
        )


def _check_proof_hash(passport: FinancialProofObject, errors: List[str]) -> None:
    """§13 rule 1 — recompute, never trust."""
    recomputed = passport.proof_hash()
    stored = passport.integrity_proof.object_hash
    if not stored:
        errors.append(f"{passport.claim_id}: object_hash 为空，护照未封存")
        return
    if stored != recomputed:
        errors.append(
            f"{passport.claim_id}: object_hash 不可重算（记录 {stored[:16]}… vs "
            f"重算 {recomputed[:16]}…），护照已被改动"
        )
    if not passport.integrity_proof.run_id:
        errors.append(f"{passport.claim_id}: 未关联 run_id，审计链断裂")


def _check_checks(checks: Sequence[CheckOutcome], errors: List[str]) -> None:
    if not checks:
        errors.append("verification 没有任何检查项（§7 要求逐项留痕）")
        return
    names = [item.check for item in checks]
    unknown = sorted(set(names) - set(PROOF_CHECKS))
    if unknown:
        errors.append("unknown_checks:" + ",".join(unknown))
    missing = sorted(set(PROOF_CHECKS) - set(names))
    if missing:
        errors.append("missing_checks:" + ",".join(missing))
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append("duplicate_checks:" + ",".join(duplicates))
    for item in checks:
        if item.status not in CHECK_STATUSES:
            errors.append(f"{item.check}: 未知检查状态 {item.status!r}")
        if item.impact not in PROOF_STATUSES:
            errors.append(f"{item.check}: 未知 impact {item.impact!r}")
        if not item.message:
            errors.append(f"{item.check}: 检查项没有 message（无法解释结果）")


def _check_verification_consistency(
    passport: FinancialProofObject, result: VerificationResult, errors: List[str]
) -> None:
    if result.claim_id != passport.claim_id:
        errors.append(
            f"{passport.claim_id}: 验证结果描述的是 {result.claim_id}，与护照不匹配"
        )
    if result.proof_id != passport.proof_id:
        errors.append(
            f"{passport.claim_id}: verification.proof_id {result.proof_id} != "
            f"passport.proof_id {passport.proof_id}"
        )
    _check_checks(result.checks, errors)
    overall, _reasons = merge_status(result.checks)
    if overall != result.overall_status:
        errors.append(
            f"{passport.claim_id}: overall_status {result.overall_status} 与检查项合并结果 "
            f"{overall} 不一致（§7 合并规则）"
        )
    if not result.status_reasons:
        errors.append(f"{passport.claim_id}: 验证结果没有 status_reasons（无法解释判定）")
    if result.overall_status == STATUS_CONFLICTED and not any(
        item.status == "fail" and item.impact == STATUS_CONFLICTED for item in result.checks
    ):
        errors.append(
            f"{passport.claim_id}: CONFLICTED 但没有指向冲突的 fail 检查项（判定无依据）"
        )
    if result.overall_status == STATUS_VERIFIED and any(
        item.status == "fail" for item in result.checks
    ):
        errors.append(
            f"{passport.claim_id}: 存在 fail 检查项却给出 VERIFIED —— 验证器不得降标通过"
        )


def _sentence_hits(memo: Any, claim_id: str, sections: Sequence[str]) -> List[Tuple[str, str]]:
    """``(section_id, sentence)`` for every mention of the claim in those sections."""
    hits: List[Tuple[str, str]] = []
    for section in getattr(memo, "sections", ()) or ():
        if section.section_id not in sections:
            continue
        for sentence in getattr(section, "sentences", ()) or ():
            if sentence.claim_id == claim_id:
                hits.append((section.section_id, sentence.sentence_span))
    return hits


def _check_verdict_in_memo(
    passport: FinancialProofObject,
    result: VerificationResult,
    memo: Any,
    errors: List[str],
    warnings: List[str],
) -> None:
    """§13 rule 3 — blocked conclusions must not read as advice."""
    hits = _sentence_hits(memo, passport.claim_id, CONCLUSION_SECTIONS)
    if not hits:
        return
    where = "、".join(sorted({section for section, _ in hits}))

    if result.blocking:
        errors.append(
            f"{passport.claim_id}: {result.overall_status} 的 claim 出现在 memo 结论段落"
            f"（{where}），§13 禁止其进入结论"
        )
        return

    if result.overall_status == STATUS_STALE:
        unmarked = [
            sentence for _section, sentence in hits if REFRESH_MARKER not in sentence
        ]
        if unmarked:
            errors.append(
                f"{passport.claim_id}: STALE 的 claim 出现在结论段（{where}）但未标注"
                f"「{REFRESH_MARKER}」"
            )
        else:
            warnings.append(
                f"{passport.claim_id}: STALE 出现在结论段，已带「{REFRESH_MARKER}」标注"
            )


__all__ = [
    "CONCLUSION_SECTIONS",
    "REFRESH_MARKER",
    "ProofValidation",
    "validate_proof",
    "validate_proof_corpus",
    "validate_verification",
]
