"""CARD-05 §10 validator rules: the finding log has to be auditable.

Three checks, each closing a way the report could look authoritative while being
unverifiable:

1. **Evidence must resolve.** A finding that accuses a draft of a wrong number is
   only useful if a reviewer can open the page it is accusing the draft of
   misquoting. Any ``evidence_ids`` entry that does not resolve to a located fact
   is an unsourced accusation. Value-level findings (``numeric_error`` and its
   siblings) additionally must cite *something*: silence there means the checker
   contradicted the draft without saying what it contradicted it with.
2. **``original`` / ``expected`` must be comparable.** §10 rule 2: both sides are
   rendered in an explicit unit, so a reviewer can see the error without doing
   arithmetic. A bare ``150`` opposite ``1,100,000元`` is exactly the ambiguity the
   card exists to remove.
3. **No finding may come from a model.** ``checker.py`` must import no model
   client — asserted against its *source text*, because an import that happens
   inside a function body is invisible to an import-graph check and would break the
   card's central guarantee just as thoroughly.

Pure: no network, no LLM, no wall-clock dependence. It reports problems; it never
repairs them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..cn.normalization.units import UNIT_SCALES

#: Check types whose ``original``/``expected`` are a **claimed amount versus a
#: disclosed amount**, and so must both be rendered with an explicit unit (§10 rule
#: 2). The card names ``numeric_error``; ``unit_error`` is included because a
#: "单位声明错误" finding whose ``expected`` carries no unit word is that same defect
#: where it matters most. ``wrong_page``/``wrong_citation`` expect page references
#: and ``calculation_error`` expects a percentage, so all three are excluded.
AMOUNT_COMPARABLE: Tuple[str, ...] = (
    "numeric_error",
    "unit_error",
    "period_error",
    "scope_error",
    "revision_superseded",
)

#: Check types that assert something about *a disclosed figure*, and therefore must
#: name the disclosure they are contradicting.
#:
#: Three deliberate absences, each for the same reason — demanding positive evidence
#: would force the checker to cite something unrelated:
#:
#: * ``missing_citation`` — its whole content is "nothing was cited".
#: * ``unsupported_citation`` — the citation points at no document, so there is no
#:   document to resolve it to.
#: * ``wrong_citation`` — measured on ``draft_04``: the cited announcement yields no
#:   normalized facts at all, so the finding's content *is* the absence of a
#:   disclosure. It already names the resolved-but-wrong citation in ``original``;
#:   requiring an ``evidence_id`` here would make the checker invent one.
EVIDENCE_REQUIRED: Tuple[str, ...] = (
    "numeric_error",
    "unit_error",
    "period_error",
    "scope_error",
    "calculation_error",
    "valuation_multiple_error",
    "wrong_page",
    "revision_superseded",
)

#: Module fragments ``checker.py`` may never import (§10 rule 3). Named by
#: fragment so a new client module under an existing package is still caught.
BANNED_MODULE_FRAGMENTS: Tuple[str, ...] = (
    "src.llms",
    "..llms",
    "openai",
    "anthropic",
    "dashscope",
    "zhipuai",
    "google.generativeai",
    "transformers",
)

#: Symbols likewise banned, for the case where a client arrives by dependency
#: injection rather than by import.
BANNED_SYMBOLS: Tuple[str, ...] = (
    "LlmClient",
    "ModelClient",
    "ClaimExtractionClient",
    "extract_claims",
    "OpenAI(",
    "AsyncOpenAI(",
)

#: A banned symbol only counts as a whole identifier. Substring matching made
#: ``extract_claims`` fire on ``extract_claims_rules`` — CARD-05's own
#: deterministic rule extractor, i.e. exactly the import a model-free module is
#: *supposed* to make. Any other consumer of this assertion (CARD-24/25/26 scan
#: their own sources with it) inherited the same false positive.
_BANNED_SYMBOL_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    symbol: re.compile(rf"(?<!\w){re.escape(symbol)}(?!\w)")
    for symbol in BANNED_SYMBOLS
}

#: Unit words a rendered amount may end in. Built from CARD-02's ladder so 千元 /
#: 百万元 are covered without a second table that could drift.
_UNIT_TOKENS: Tuple[str, ...] = tuple(
    sorted({str(token) for token in UNIT_SCALES} | {"元"}, key=len, reverse=True)
)

_AMOUNT_RE = re.compile(
    r"(?P<num>-?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>" + "|".join(re.escape(t) for t in _UNIT_TOKENS) + r")?"
)
_PERIOD_RE = re.compile(r"[（(]\s*(\d{4}(?:FY|Q[1-4]|H1|9M))\s*[）)]")
_PERCENT_RE = re.compile(r"-?\d+(?:\.\d+)?\s*%")

#: Placeholders that mean "we could not verify". Legitimate at warning level; an
#: ``error`` that says this is a contradiction the checker cannot actually back.
_UNVERIFIED_TOKENS = ("无法核验", "不可核验", "—")


@dataclass(frozen=True)
class CheckerValidation:
    """Outcome of the CARD-05 §10 gate."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def validate_checker_report(
    report: Mapping[str, Any],
    *,
    known_fact_ids: Optional[Iterable[str]] = None,
    evidence_lookup: Optional[Mapping[str, Any]] = None,
    checker_source: Optional[str] = None,
) -> CheckerValidation:
    """Apply §10 to one ``build_report`` payload.

    ``known_fact_ids`` / ``evidence_lookup`` decide rule 1: the first answers "does
    this id exist at all", the second (a ``fact_id -> fact`` mapping, or a mapping
    of ids to ``(document_id, page)``) additionally answers "is it located". Each
    rule whose inputs are missing is reported as a **warning**, never as a silent
    pass — ``valid`` must not overstate coverage.

    ``checker_source`` feeds rule 3. When omitted the rule is skipped with a
    warning, because the caller may be validating a payload that was produced
    elsewhere; :func:`checker_module_source` reads the shipped file.
    """
    errors: List[str] = []
    warnings: List[str] = []
    findings = list(report.get("findings", ()) or ())

    _check_evidence(
        findings, known_fact_ids=known_fact_ids, evidence_lookup=evidence_lookup,
        errors=errors, warnings=warnings,
    )
    _check_comparable_values(findings, errors=errors)

    if checker_source is None:
        warnings.append(
            "未提供 checker 源码，跳过『finding 不得由模型产生』的源码断言。"
        )
    else:
        errors.extend(check_source_is_model_free(checker_source))

    return CheckerValidation(valid=not errors, errors=errors, warnings=warnings)


def _check_evidence(
    findings: Sequence[Mapping[str, Any]],
    *,
    known_fact_ids: Optional[Iterable[str]],
    evidence_lookup: Optional[Mapping[str, Any]],
    errors: List[str],
    warnings: List[str],
) -> None:
    resolvable: Optional[set] = None
    if evidence_lookup is not None:
        resolvable = {str(identifier) for identifier in evidence_lookup}
    elif known_fact_ids is not None:
        resolvable = {str(identifier) for identifier in known_fact_ids}
    else:
        warnings.append(
            "未提供 known_fact_ids / evidence_lookup，跳过『finding 证据可解析』检查："
            "本次无法确认 finding 的证据指向真实披露。"
        )

    for finding in findings:
        claim_id = str(finding.get("claim_id", "?"))
        check_type = str(finding.get("check_type", "?"))
        evidence = [str(item) for item in (finding.get("evidence_ids", ()) or ())]

        if check_type in EVIDENCE_REQUIRED and not evidence:
            errors.append(
                f"{claim_id} 的 {check_type} 未绑定任何证据："
                "数值级 finding 必须指名它所依据的披露。"
            )

        if resolvable is not None:
            missing = [item for item in evidence if item not in resolvable]
            if missing:
                errors.append(
                    f"{claim_id} 的 {check_type} 证据无法解析到已定位事实：{missing}"
                )

        if evidence_lookup is not None:
            unlocated = [
                item for item in evidence
                if item in evidence_lookup and not _is_located(evidence_lookup[item])
            ]
            if unlocated:
                errors.append(
                    f"{claim_id} 的 {check_type} 证据缺少文档页码：{unlocated}"
                )


def _is_located(value: Any) -> bool:
    """Whether an evidence entry carries a document id *and* a page (§10 rule 1)."""
    if isinstance(value, Mapping):
        document_id = value.get("document_id")
        page = value.get("page")
    elif isinstance(value, (tuple, list)) and len(value) >= 2:
        document_id, page = value[0], value[1]
    else:
        document_id = getattr(value, "document_id", None)
        page = getattr(value, "page", None)
    return bool(document_id) and page is not None


def _check_comparable_values(
    findings: Sequence[Mapping[str, Any]], *, errors: List[str]
) -> None:
    """§10 rule 2 — the two sides of an amount finding are rendered alike.

    Scoped by the **shape of ``original``**, not only by check type. One check type
    can carry two different things: ``scope_error`` is either "this amount belongs
    to the other 口径" (``original`` = ``60万元``) or "a percentage was emitted where
    CARD-04 forbade the comparison" (``original`` = ``+11.10%``, measured on
    ``draft_05``). Only the first is an amount comparison; the second has no
    expected amount to state, and demanding one would flag a correct finding.
    """
    for finding in findings:
        check_type = str(finding.get("check_type"))
        if check_type not in AMOUNT_COMPARABLE:
            continue
        claim_id = str(finding.get("claim_id", "?"))
        original = str(finding.get("original", ""))
        expected = str(finding.get("expected", ""))

        if _PERCENT_RE.search(original):
            # A percentage is not an amount; no unit is involved and there is no
            # "correct amount" to compare against.
            continue

        if any(token in expected for token in _UNVERIFIED_TOKENS):
            errors.append(
                f"{claim_id} 的 {check_type} 未给出期望值（expected={expected!r}）："
                "error 级 finding 必须能说出正确值，否则应降级为无法核验。"
            )
            continue

        original_amount = _split_amount(original)
        expected_amount = _split_amount(expected)

        if expected_amount is None:
            errors.append(
                f"{claim_id} 的 {check_type} expected={expected!r} 不含可解析的数值，"
                "无法与 original 同口径比较。"
            )
            continue
        if original_amount is None:
            # For these rules ``original`` names a claimed amount; anything else
            # means the finding is not shaped like the rule it claims to be.
            errors.append(
                f"{claim_id} 的 {check_type} original={original!r} 不含可解析的数值。"
            )
            continue

        _, original_unit, _ = original_amount
        _, expected_unit, _ = expected_amount
        if original_unit and not expected_unit:
            errors.append(
                f"{claim_id} 的 {check_type} 两侧单位口径不一致："
                f"original 声明了 {original_unit}，expected={expected!r} 未声明单位。"
            )


def _split_amount(text: str) -> Optional[Tuple[float, Optional[str], Optional[str]]]:
    """``"1,100,000元（2025FY）"`` -> ``(1100000.0, "元", "2025FY")``.

    Returns ``None`` when there is no number at all. A missing unit word yields
    ``None`` for the unit rather than a default: §10 rule 2 is precisely about the
    unit being *stated*, so guessing one here would defeat the check.
    """
    match = _AMOUNT_RE.search(text or "")
    if not match:
        return None
    raw = match.group("num").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    period = _PERIOD_RE.search(text or "")
    return value, match.group("unit"), period.group(1) if period else None


def check_source_is_model_free(source: str, *, label: str = "checker.py") -> List[str]:
    """§10 rule 3 as a *source* assertion. Returns one message per violation.

    Scanning the text rather than the import graph is deliberate: a deferred
    ``import`` inside a rule function is invisible to ``sys.modules`` inspection at
    import time, and an ``importlib`` call would be invisible to both. The source
    is the only artefact that cannot lie about what the module is able to do.

    ``label`` names the file in the message; CARD-24/25/26 reuse this assertion on
    their own sources, where "checker.py" would point at the wrong artefact.
    """
    problems: List[str] = []
    for line_number, line in enumerate(str(source or "").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code = line.split("#", 1)[0]
        if "import" not in code:
            continue
        for fragment in BANNED_MODULE_FRAGMENTS:
            if fragment in code:
                problems.append(
                    f"{label} 第 {line_number} 行引用了模型客户端（{fragment}）：{stripped}"
                )
        for symbol, pattern in _BANNED_SYMBOL_PATTERNS.items():
            if pattern.search(code):
                problems.append(
                    f"{label} 第 {line_number} 行出现模型相关符号（{symbol}）：{stripped}"
                )
    return problems


def checker_module_source(path: Optional[str | Path] = None) -> str:
    """The shipped ``checker.py`` source, for the §10 rule 3 assertion."""
    target = Path(path) if path else Path(__file__).resolve().parents[1] / "cn" / "checker" / "checker.py"
    return target.read_text(encoding="utf-8")


def validate_checker_module(path: Optional[str | Path] = None) -> CheckerValidation:
    """Convenience: run the source assertion on the shipped checker alone."""
    problems = check_source_is_model_free(checker_module_source(path))
    return CheckerValidation(valid=not problems, errors=problems)


__all__ = [
    "AMOUNT_COMPARABLE",
    "BANNED_MODULE_FRAGMENTS",
    "BANNED_SYMBOLS",
    "CheckerValidation",
    "EVIDENCE_REQUIRED",
    "check_source_is_model_free",
    "checker_module_source",
    "validate_checker_module",
    "validate_checker_report",
]
