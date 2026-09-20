"""CARD-26 FinFuzz — the ten mutation operators (§7).

Every operator is a **deterministic function** of ``(claim, context)``. No model,
no randomness, no wall clock: rerunning a suite must reproduce it byte for byte
(§9, §18). Randomness, where a suite needs any, belongs to the sampler in
:mod:`src.cn.finfuzz.suite` and is seeded there.

Each operator returns zero or more :class:`OperatorOutcome`. Returning nothing is
the honest answer when the claim cannot carry that tamper — §11's "generation
failed → skip and record", not a silent hole. Three real preconditions:

* ``version`` needs the fact base to hold a *superseded* value for the claim's
  ``(metric, period, scope)``; with a single disclosed version there is no old
  value to quote and the tamper cannot exist.
* ``valuation`` needs a claim that carries a multiple, and the *basis-swap*
  variant needs a recomputable denominator — see :func:`mutate_valuation_basis`.
* ``ytd_vs_single`` needs a claim read off a cumulative column.

**Text edits are best-effort and verified.** An outcome carries the literal
substitutions that turn the source sentence into the tampered one, but a
substitution is only offered when its ``old`` text really occurs in the sentence.
An edit that cannot be applied is dropped and named in the outcome's note, so the
text channel can never quietly become a no-op and then be scored as a *checker*
miss for a tamper that never happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from src.cn.checker.facts import FactIndex
from src.cn.checker.models import ClaimType, DraftClaim
from src.cn.normalization.units import UNIT_SCALES

from .models import MutationOperators

#: A filename that is deliberately in no registry. Used by the ``citation``
#: operator for the "cited document does not exist" case. It is a *mutation*
#: payload, so it is supposed to be unfindable; nothing here is presented as a
#: real filing.
ABSENT_DOCUMENT = "不存在的研报.pdf"

#: Cumulative phrasings the ``ytd_vs_single`` operator rewrites into a
#: single-period statement. Ordered longest-first so 「年初至报告期末累计」 is not
#: half-eaten by 「年初至报告期末」.
YTD_PHRASES: Tuple[str, ...] = (
    "年初至报告期末累计",
    "年初至报告期末",
    "年初至本报告期末",
    "前三季度累计",
    "前三季度",
)

#: The single-period phrasing the YTD operator substitutes in. Deliberately a
#: Q4-worded label: the point of the operator is to claim a *single quarter*, and
#: 「第四季度」 is exactly the phrasing CARD-05's period table carries no pattern
#: for — itself a finding the suite is meant to surface rather than paper over.
SINGLE_PERIOD_PHRASE = "第四季度"

#: The causal connective the ``causal`` operator inserts. It has to satisfy
#: CARD-05's ``_CAUSAL_RE``, which anchors the antecedent on 因/由于/受/受益于 —
#: the plain 「A导致B」 rewrite the card describes as prose is *not* recognised by the
#: extractor, and using it would have produced an extractor-stage miss on every
#: claim while looking like a checker defect.
CAUSAL_CONNECTIVE = "受行业景气度变化推动，"

#: Scope tampering targets, keyed by the scope the claim declares.
_OPPOSITE_SCOPE: Dict[str, str] = {"合并": "母公司", "母公司": "合并"}

#: Direction words CARD-05's growth pattern treats as negative.
_NEGATIVE_WORDS: Tuple[str, ...] = ("下降", "减少", "下滑", "降低")
_POSITIVE_WORDS: Tuple[str, ...] = ("增长", "上升", "增加", "提高")


class OperatorError(RuntimeError):
    """Raised when an operator is asked to do something it cannot do safely."""


@dataclass(frozen=True)
class OperatorContext:
    """Everything an operator may read. Built once; never mutated by an operator."""

    index: Optional[FactIndex] = None
    #: Filenames known to be registered, so a "wrong document" citation points at
    #: a *real* filing that does not carry the number — which is the defect.
    registered_filenames: Tuple[str, ...] = ()
    #: CARD-02's canonical unit words, as they appear in prose. Defaults to every
    #: scale the normalizer knows.
    unit_words: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorOutcome:
    """One tampered claim, plus how the tamper reads in the sentence."""

    mutated_claim: DraftClaim
    description: str
    text_edits: Tuple[Tuple[str, str], ...] = ()
    equivalent: bool = False
    note: str = ""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _literal_of(claim: DraftClaim) -> Optional[str]:
    """The digits as they appear, falling back to a plain rendering of the value."""
    if claim.value_literal:
        return claim.value_literal
    if claim.value is None:
        return None
    return _plain_number(claim.value)


def _plain_number(value: float) -> str:
    """Render a number the way prose would: ``1100.0`` → ``1100``."""
    if float(value) == int(value):
        return str(int(value))
    return f"{value:g}"


def _verified_edits(
    sentence: str, edits: Tuple[Tuple[str, str], ...]
) -> Tuple[Tuple[Tuple[str, str], ...], List[str]]:
    """Keep only the edits whose ``old`` really occurs in *sentence*.

    Returns the surviving edits and one warning per dropped edit. Dropping is the
    right failure mode: an inapplicable edit would leave the text channel
    unchanged, and the suite would then score the miss against the checker.
    """
    kept: List[Tuple[str, str]] = []
    dropped: List[str] = []
    for old, new in edits:
        if not old or old not in sentence:
            dropped.append(f"text_edit_not_applicable:{old!r}")
            continue
        kept.append((old, new))
    return tuple(kept), dropped


def _period_year(period: Optional[str]) -> Optional[str]:
    match = re.match(r"^(\d{4})", str(period or ""))
    return match.group(1) if match else None


def _cause(*groups: str) -> str:
    return "；".join(group for group in groups if group)


def _close(left: Optional[float], right: Optional[float], tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= max(tolerance, abs(float(right)) * 1e-9)


def _to_base(value: float, unit: Optional[str]) -> Optional[float]:
    """Convert an amount to CARD-02's base unit (元), or ``None`` if unitless.

    The fact index stores base-unit amounts; a claim is written in whatever unit
    its sentence declared. Comparing the two without converting silently matches
    nothing — which is how :func:`mutate_version` first declined on every claim.
    """
    scale = UNIT_SCALES.get(str(unit or ""))
    if not scale:
        return None
    return float(value) * float(scale)


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #
def mutate_numeric(
    claim: DraftClaim, ctx: OperatorContext, *, factor: int = 10
) -> Tuple[OperatorOutcome, ...]:
    """§7 ``numeric`` — move the decimal point / change the magnitude.

    ``1.26 → 12.6``; the card's headline case is ``×10``. The value literal is
    rewritten with the value so the text channel really carries the tamper.

    A second, *equivalent* outcome covers the full-width digit form
    (``110`` → ``１１０``). §12 edge case 1 asks for rewrites that are not actually
    errors, and this is one the pipeline must stay silent on: full-width digits are
    a formatting variant of the same number, so a finding here is a false positive
    — and one this operator pair lets the suite measure directly.
    """
    literal = _literal_of(claim)
    if claim.value is None or literal is None:
        return ()
    outcomes: List[OperatorOutcome] = []

    scaled = claim.value * factor
    scaled_literal = _plain_number(scaled)
    edits, dropped = _verified_edits(claim.sentence, ((literal, scaled_literal),))
    outcomes.append(OperatorOutcome(
        mutated_claim=replace(claim, value=scaled, value_literal=scaled_literal),
        description=f"数值 ×{factor}：{literal} → {scaled_literal}（量级篡改）",
        text_edits=edits,
        note=_cause(*dropped),
    ))

    fullwidth = "".join(
        chr(ord(char) + 0xFEE0) if char.isdigit() else char for char in literal
    )
    fw_edits, fw_dropped = _verified_edits(claim.sentence, ((literal, fullwidth),))
    if fw_edits and fullwidth != literal:
        outcomes.append(OperatorOutcome(
            mutated_claim=replace(claim),   # identical fields: the value is unchanged
            description=f"全角数字等值改写：{literal} → {fullwidth}（伪变异，不应报错）",
            text_edits=fw_edits,
            equivalent=True,
            note=_cause("equivalent_variant", *fw_dropped),
        ))
    return tuple(outcomes)


def mutate_unit(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``unit`` — keep the number, swap the unit word, change the magnitude.

    The card is emphatic about the boundary: ``3.1 亿元 = 31,000 万元``, so
    *"3.1 亿元 written as 31,000 万元" is not an error*. The planted tamper must
    therefore be a genuine magnitude error (``110 万元`` → ``110 亿元``), while the
    equal-value rewrite becomes this operator's *equivalent* variant — the
    corpus's own false-positive pressure sample.

    Both variants are emitted because the pair is the whole point: the same two
    numbers, one neutral and one wrong, and the detector has to tell them apart.
    """
    unit = str(claim.unit or "")
    old_scale = UNIT_SCALES.get(unit)
    if claim.value is None or not unit or not old_scale:
        return ()
    words = tuple(ctx.unit_words) or tuple(UNIT_SCALES)

    # The tamper has to *exaggerate*: 110 万元 read as 110 亿元. Picking the first
    # unit that merely differs would take the smallest one, which both understates
    # and — because the equal-value rewrite below always goes smaller — collides
    # with it, leaving two variants whose only difference is the value field.
    bigger = sorted(
        (w for w in words if w != unit and UNIT_SCALES.get(w, 0.0) > old_scale),
        key=lambda w: UNIT_SCALES[w],
    )
    smaller = sorted(
        (w for w in words if w != unit and 0.0 < UNIT_SCALES.get(w, 0.0) < old_scale),
        key=lambda w: -UNIT_SCALES[w],
    )
    other = bigger[0] if bigger else (smaller[0] if smaller else None)
    if other is None:
        return ()

    literal = _literal_of(claim) or _plain_number(claim.value)
    outcomes: List[OperatorOutcome] = []

    new_scale = UNIT_SCALES.get(other)
    if new_scale and new_scale != old_scale:
        edits, dropped = _verified_edits(claim.sentence, ((unit, other),))
        outcomes.append(OperatorOutcome(
            mutated_claim=replace(claim, unit=other),
            description=f"单位篡改：数值 {literal} 不变，单位 {unit} → {other}（量级错）",
            text_edits=edits,
            note=_cause(*dropped),
        ))

    equivalent = _equivalent_unit_literal(claim, ctx, exclude=other)
    if equivalent is not None:
        new_literal, new_unit = equivalent
        eq_edits, eq_dropped = _verified_edits(
            claim.sentence, ((f"{literal}{unit}", f"{new_literal}{new_unit}"),)
        )
        if eq_edits:
            # The *amount* must move with the unit, or the rewrite is not
            # equivalent at all: 110 万元 → 110 元 is a 10^4 error, and the suite
            # would score a correct detection as a false positive. Only the
            # rendering changes; the amount is held fixed by construction in
            # :func:`_equivalent_unit_literal`.
            new_scale = UNIT_SCALES.get(new_unit) or 0.0
            rescaled = claim.value * (old_scale / new_scale) if new_scale else claim.value
            outcomes.append(OperatorOutcome(
                mutated_claim=replace(
                    claim, value=rescaled, value_literal=new_literal, unit=new_unit
                ),
                description=(
                    f"等值换单位：{literal}{unit} = {new_literal}{new_unit}"
                    "（伪变异，数值相等不是错误）"
                ),
                text_edits=eq_edits,
                equivalent=True,
                note=_cause("equivalent_variant", *eq_dropped),
            ))
    return tuple(outcomes)


def _equivalent_unit_literal(
    claim: DraftClaim, ctx: OperatorContext, *, exclude: str = ""
) -> Optional[Tuple[str, str]]:
    """Pick a different unit whose scale divides the claim's amount exactly.

    Only exact divisions are offered. A rounded rewrite would change the amount
    and would silently turn a false-positive sample into a real error — which the
    suite would then score backwards, counting a correct detection as a
    false positive.

    ``exclude`` is the unit the planted variant already claimed. Reusing it would
    make "one neutral, one wrong" collapse into "same unit, different number",
    which is a weaker test than the card intends.
    """
    unit = str(claim.unit or "")
    base = UNIT_SCALES.get(unit)
    if not base or claim.value is None:
        return None
    known = set(ctx.unit_words or ()) or set(UNIT_SCALES)
    smaller = [
        (scale, word) for word, scale in UNIT_SCALES.items()
        if scale < base and word != unit and word != exclude and word in known
    ]
    if not smaller:
        return None
    scale, word = max(smaller)
    amount = claim.value * (base / scale)
    if abs(round(amount, 6) - amount) > 1e-9:
        return None
    return _plain_number(round(amount, 6)), word


def mutate_sign(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``sign`` — flip ``-3.2%`` to ``+3.2%``.

    A sign flip on a growth rate is caught by recomputation, so CARD-05 reports
    ``calculation_error`` rather than ``numeric_error``; see
    :data:`~src.cn.finfuzz.models.EXPECTED_CHECK_TYPES`. The operator works on the
    growth figure because a level value in this corpus is never negative.

    **Precondition: the growth must be recomputable**, which needs both a metric
    and a stated period. CARD-05's growth rule returns early without them, so
    emitting the spec anyway would credit the checker with a miss it never had the
    material to avoid — an unevaluable sample dressed up as a blind spot. The
    refusal is recorded as a skipped spec (§11).
    """
    if claim.growth is None or not claim.metric or not claim.period:
        return ()
    literal = claim.growth_literal or _plain_number(abs(claim.growth))
    flipped = -claim.growth
    target = "下降" if flipped < 0 else "增长"
    edits, dropped = _verified_edits(claim.sentence, _sign_edits(claim.sentence, literal, target))
    return (OperatorOutcome(
        mutated_claim=replace(claim, growth=flipped, growth_literal=_plain_number(abs(flipped))),
        description=f"符号篡改：同比 {literal}% → {target}{literal}%",
        text_edits=edits,
        note=_cause(*dropped),
    ),)


def _sign_edits(sentence: str, literal: str, target: str) -> Tuple[Tuple[str, str], ...]:
    """The substitution that flips a 同比 direction word in the sentence."""
    for word in (*_POSITIVE_WORDS, *_NEGATIVE_WORDS):
        pattern = f"{word}{literal}%"
        if pattern in sentence:
            return ((pattern, f"{target}{literal}%"),)
    bare = f"{literal}%"
    if bare in sentence:
        return ((bare, f"{'-' if target in _NEGATIVE_WORDS else '+'}{literal}%"),)
    return ()


def mutate_period(
    claim: DraftClaim, ctx: OperatorContext, *, year_delta: int = -1
) -> Tuple[OperatorOutcome, ...]:
    """§7 ``period`` — move the statement to another year.

    The year comes from the claim's canonical period key, so the tamper is always
    a real period shift rather than a guess. Whether it *survives* extraction is a
    separate question the suite measures: a sentence that also carries a dated
    heading will have its period resolved from the heading, and that is an
    extractor-stage miss, not a checker miss.
    """
    year = _period_year(claim.period)
    if year is None:
        return ()
    target = str(int(year) + year_delta)
    if target == year:
        return ()
    # Prefer the body spelling (「2025年」) over a heading's spaced form ("2025 年度").
    edits, dropped = _verified_edits(claim.sentence, ((f"{year}年", f"{target}年"),))
    if not edits:
        edits, dropped = _verified_edits(claim.sentence, ((year, target),))
    return (OperatorOutcome(
        mutated_claim=replace(claim, period=f"{target}{str(claim.period)[4:]}"),
        description=f"期间篡改：{year} → {target}",
        text_edits=edits,
        note=_cause(*dropped),
    ),)


def mutate_ytd_vs_single(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``ytd_vs_single`` — present a cumulative figure as a single period.

    Only a claim whose period the extractor could *not* pin down qualifies: it is
    read off a cumulative column, so re-labelling it is a real tamper rather than
    a contradiction of a period the sentence already stated.

    The claim channel asserts the quarter directly, which isolates the checker.
    The text channel rewrites the cumulative wording, and CARD-05's period table
    carries no pattern for a bare quarter with no year — so the extractor will
    often drop the tamper. Both outcomes are offered unconditionally and the suite
    reports which stage missed; that split is why §7 asks for a ``period`` *and* a
    ``ytd_vs_single`` operator instead of one.
    """
    if claim.value is None or claim.period is not None:
        return ()
    year = _any_year(claim.sentence)
    if year is None:
        return ()
    edits: Tuple[Tuple[str, str], ...] = ()
    dropped: List[str] = []
    for phrase in YTD_PHRASES:
        if phrase in claim.sentence:
            edits, dropped = _verified_edits(claim.sentence, ((phrase, SINGLE_PERIOD_PHRASE),))
            break
    return (OperatorOutcome(
        mutated_claim=replace(claim, period=f"{year}Q4"),
        description=f"累计冒充单期：取消累计语义，标为 {year}Q4",
        text_edits=edits,
        note=_cause(*dropped),
    ),)


def _any_year(text: str) -> Optional[str]:
    match = re.search(r"(\d{4})", text)
    return match.group(1) if match else None


def mutate_scope(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``scope`` — swap the 口径 the sentence declares.

    Only a claim that *declares* a 口径 can be tampered with. A claim with
    ``scope=None`` names none, and inventing one would put the checker's own
    assumption into the corpus as a planted error.
    """
    scope = str(claim.scope or "")
    other = _OPPOSITE_SCOPE.get(scope)
    if not other:
        return ()
    edits, dropped = _verified_edits(claim.sentence, ((scope, other),))
    return (OperatorOutcome(
        mutated_claim=replace(claim, scope=other),
        description=f"口径篡改：{scope} → {other}",
        text_edits=edits,
        note=_cause(*dropped),
    ),)


def mutate_version(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``version`` — quote the value that has since been superseded.

    The old value is **read out of the fact base, never invented**: the operator
    looks for another disclosed value for the same ``(metric, period, scope)``
    carrying a lower-ranked disclosure version. With only one version on record
    there is no superseded value to quote, and the operator declines (§11) rather
    than manufacturing one.
    """
    index = ctx.index
    if index is None or claim.value is None or not claim.metric or not claim.period:
        return ()
    candidates = index.candidates(claim.metric, period_key=claim.period, scope=claim.scope)
    if len(candidates) < 2:
        return ()
    # ``Disclosure.value`` is in the *base* unit (元); the claim's value is in the
    # unit the sentence declared (万元). Comparing them directly never matches, and
    # the operator would decline on every real claim while looking like a corpus
    # gap — which is exactly how this bug first showed up.
    claimed_base = _to_base(claim.value, claim.unit)
    if claimed_base is None:
        return ()

    # Pick the replacement using the checker's *own* supersession rule rather than
    # a private re-derivation of it. The first attempt here ranked candidates by
    # their disclosure-rank tuple, which selected the **restated** column — the
    # correct current figure — so the "tamper" produced a sentence that was right
    # and was correctly not flagged. Supersession is a *table-level* relationship
    # (an 调整前/调整后 pair printed side by side), so only ``is_superseded`` answers
    # the question this operator is asking.
    superseded = [
        item for item in candidates
        if index.is_superseded(item) and not _close(item.value, claimed_base)
    ]
    if not superseded:
        # No replaced value exists for this figure, so this tamper has no subject.
        # Declining is the honest answer (§11); manufacturing a value would put a
        # fabricated disclosure into the corpus.
        return ()
    old = min(superseded, key=lambda item: (item.rank, item.fact_id))
    if _close(old.value, claimed_base):
        return ()

    literal = _literal_of(claim) or _plain_number(claim.value)
    # The mutation is written the way a draft would write it: the old *amount* in
    # the sentence's own unit, not the base-unit figure the index stores.
    scale = claimed_base / claim.value if claim.value else 1.0
    old_amount = old.value / scale
    old_literal = _plain_number(old_amount)
    edits, dropped = _verified_edits(claim.sentence, ((literal, old_literal),))
    if claim.page is not None and old.page is not None and old.page != claim.page:
        page_edits, page_dropped = _verified_edits(
            claim.sentence, ((f"#p{claim.page}", f"#p{old.page}"),)
        )
        edits = edits + page_edits
        dropped = dropped + page_dropped
    return (OperatorOutcome(
        mutated_claim=replace(claim, value=old_amount, value_literal=old_literal),
        description=(
            f"引用被取代的旧值：{literal} → {old_literal}"
            f"（版本 {old.version}，precedence rank {old.rank[:2]}）"
        ),
        text_edits=edits,
        note=_cause(*dropped),
    ),)


def mutate_valuation_basis(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``valuation`` — tamper with the multiple.

    Two variants, and the difference matters for what the suite may honestly
    claim:

    * a wrong *number* on the same basis — recomputable from disclosed inputs, so
      CARD-05 must catch it;
    * a switched *basis* (PE → PB) — CARD-05 only judges a multiple it can
      recompute, and with no disclosed book value it declines. That is a
      documented boundary of the checker, so this variant carries its missing
      precondition in its note instead of pretending it is a detectable tamper.
    """
    if not claim.valuation_multiple:
        return ()
    kind, claimed = _parse_basis(claim.valuation_multiple)
    if kind is None or claimed is None:
        return ()
    original = str(claim.valuation_multiple).strip()

    wrong = claimed * 6 if kind == "pe" else claimed * 3
    wrong_text = f"{'市盈率' if kind == 'pe' else '市净率'} {_plain_number(wrong)}倍"
    edits, dropped = _verified_edits(claim.sentence, ((original, wrong_text),))
    outcomes = [OperatorOutcome(
        mutated_claim=replace(claim, valuation_multiple=wrong_text),
        description=f"估值倍数篡改：{claimed:g} → {wrong:g}（同一基数，数值错）",
        text_edits=edits,
        note=_cause(*dropped),
    )]

    swapped = "市净率 50倍" if kind == "pe" else "市盈率 50倍"
    swap_edits, swap_dropped = _verified_edits(claim.sentence, ((original, swapped),))
    outcomes.append(OperatorOutcome(
        mutated_claim=replace(claim, valuation_multiple=swapped),
        description="估值基数互换：PE ↔ PB（需可复算分母才判）",
        text_edits=swap_edits,
        note=_cause("basis_swap", *swap_dropped),
    ))
    return tuple(outcomes)


def _parse_basis(text: str) -> Tuple[Optional[str], Optional[float]]:
    match = re.search(
        r"(市盈率|市净率|PE|P/E|PB|P/B)\s*(?:为|是|约|达)?\s*[:：]?\s*(\d+(?:\.\d+)?)",
        str(text), re.IGNORECASE,
    )
    if match:
        return _basis_of(match.group(1)), float(match.group(2))
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*倍\s*(?:的)?\s*(市盈率|市净率|PE|P/E|PB|P/B)", str(text), re.IGNORECASE
    )
    if match:
        return _basis_of(match.group(2)), float(match.group(1))
    return None, None


def _basis_of(label: str) -> str:
    return "pb" if label.upper() in {"市净率", "PB", "P/B"} else "pe"


def mutate_citation(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``citation`` — keep the number right, break the reference.

    Three variants, one per citation defect CARD-05 distinguishes:

    * **wrong page** — the filing is real but the number is not on that page;
    * **absent document** — nothing in the registry answers to the name;
    * **wrong document** — a *registered* filing that does not carry this number.

    ``missing_citation`` is deliberately **not** generated here: it is produced by
    removing the whole reference from a sentence, a different edit of a different
    structure, and folding it in would make one operator stand for two unrelated
    tampering techniques.
    """
    if not claim.references:
        return ()
    reference = claim.references[0]
    outcomes: List[OperatorOutcome] = []

    # The page the checker reads is the one *inside the citation*
    # (``annual_04_...pdf#p1``), not ``DraftClaim.page`` — that field is where the
    # draft sentence sits. Mutating ``page`` therefore changed nothing and every
    # wrong-page spec was scored as a checker miss, which is how this was found.
    cited_page = _cited_page(reference)
    if cited_page is not None:
        wrong_ref = reference.replace(f"#p{cited_page}", f"#p{cited_page + 4}", 1)
        edits, dropped = _verified_edits(claim.sentence, ((reference, wrong_ref),))
        outcomes.append(OperatorOutcome(
            mutated_claim=replace(
                claim, source_reference=wrong_ref, source_references=(wrong_ref,)
            ),
            description=f"引用页码篡改：#p{cited_page} → #p{cited_page + 4}",
            text_edits=edits,
            note=_cause(*dropped),
        ))

    absent = f"{ABSENT_DOCUMENT}#p1"
    absent_edits, absent_dropped = _verified_edits(claim.sentence, ((reference, absent),))
    if absent_edits:
        outcomes.append(OperatorOutcome(
            mutated_claim=replace(claim, source_reference=absent, source_references=(absent,)),
            description=f"引用不存在文档：{reference} → {absent}",
            text_edits=absent_edits,
            note=_cause(*absent_dropped),
        ))

    other = next(
        (
            name for name in ctx.registered_filenames
            if name not in reference and name.endswith(".pdf")
        ),
        None,
    )
    if other:
        wronged = f"{other}#p1"
        wronged_edits, wronged_dropped = _verified_edits(claim.sentence, ((reference, wronged),))
        if wronged_edits:
            outcomes.append(OperatorOutcome(
                mutated_claim=replace(claim, source_reference=wronged, source_references=(wronged,)),
                description=f"引用对象错：{reference} → {wronged}（另一份已注册文档）",
                text_edits=wronged_edits,
                note=_cause(*wronged_dropped),
            ))
    return tuple(outcomes)


def _cited_page(reference: str) -> Optional[int]:
    """The page a citation names, from its ``#pN`` fragment."""
    match = re.search(r"#p(\d+)", str(reference or ""))
    return int(match.group(1)) if match else None


def mutate_causal(claim: DraftClaim, ctx: OperatorContext) -> Tuple[OperatorOutcome, ...]:
    """§7 ``causal`` — turn a plain statement into an unsupported causal claim.

    The card's example: 「原材料上涨」 and 「毛利率下降」 written side by side *describe*;
    rewriting them as 「原材料上涨导致毛利率下降」 *asserts* causation the management
    discussion never made. The tamper is therefore a ``claim_type`` change with no
    bilateral evidence behind it — not a wording tweak — because it is the
    structure that makes the sentence unsupportable.

    CARD-05's extractor only recognises a causal sentence whose antecedent it can
    anchor (因/由于/受/受益于), so the text channel inserts such a connective. Emitting
    the card's bare 「A导致B」 wording instead would have produced an extractor-stage
    miss on *every* causal spec while looking like a checker defect.
    """
    if claim.claim_type in (ClaimType.CAUSAL.value, ClaimType.OPINION.value, ClaimType.FORWARD_LOOKING.value):
        return ()
    subject = claim.metric_label or claim.metric or "该指标"

    anchor: Tuple[Tuple[str, str], ...] = ()
    dropped: List[str] = []
    if claim.growth is not None:
        literal = claim.growth_literal or _plain_number(abs(claim.growth))
        for word in (*_POSITIVE_WORDS, *_NEGATIVE_WORDS):
            pattern = f"同比{word}{literal}%"
            if pattern in claim.sentence:
                anchor, dropped = _verified_edits(
                    claim.sentence, ((pattern, f"{CAUSAL_CONNECTIVE}{pattern}"),)
                )
                break
    if not anchor:
        # Fall back to the metric's own wording, for a numeric sentence with no
        # growth figure ("营业收入为123.46万元" → "受…推动，营业收入为123.46万元").
        #
        # Anchoring on the final 「。」 instead — the first thing tried — put the
        # connective *after* the citation, so the consequence side was a bare
        # punctuation mark and CARD-05's causal pattern (which needs two or more
        # characters after the verb) never matched. That made an *extractor* miss
        # look like a checker one, on every growth-free sentence.
        label = claim.metric_label or claim.metric
        if label:
            anchor, dropped = _verified_edits(
                claim.sentence, ((label, f"{CAUSAL_CONNECTIVE}{label}"),)
            )
    if not anchor:
        for phrase in ("。", "，"):
            if phrase in claim.sentence:
                anchor, dropped = _verified_edits(
                    claim.sentence, ((phrase, f"{CAUSAL_CONNECTIVE}{phrase}"),)
                )
                if anchor:
                    break

    return (OperatorOutcome(
        mutated_claim=replace(
            claim,
            claim_type=ClaimType.CAUSAL.value,
            causal_antecedent="行业景气度变化",
            causal_consequent=f"{subject}变化",
            source_references=tuple(claim.references),
        ),
        description=f"无证据因果：把并列陈述改写为「行业景气度变化导致{subject}变化」",
        text_edits=anchor,
        note=_cause(*dropped),
    ),)


#: The operator table. Keyed by :class:`~src.cn.finfuzz.models.MutationOperators`
#: member value, so a caller can dispatch, enumerate and subset by name.
OPERATORS = {
    MutationOperators.NUMERIC.value: mutate_numeric,
    MutationOperators.UNIT.value: mutate_unit,
    MutationOperators.SIGN.value: mutate_sign,
    MutationOperators.PERIOD.value: mutate_period,
    MutationOperators.YTD_VS_SINGLE.value: mutate_ytd_vs_single,
    MutationOperators.SCOPE.value: mutate_scope,
    MutationOperators.VERSION.value: mutate_version,
    MutationOperators.VALUATION.value: mutate_valuation_basis,
    MutationOperators.CITATION.value: mutate_citation,
    MutationOperators.CAUSAL.value: mutate_causal,
}


__all__ = [
    "ABSENT_DOCUMENT",
    "CAUSAL_CONNECTIVE",
    "OPERATORS",
    "OperatorContext",
    "OperatorError",
    "OperatorOutcome",
    "SINGLE_PERIOD_PHRASE",
    "YTD_PHRASES",
    "mutate_causal",
    "mutate_citation",
    "mutate_numeric",
    "mutate_period",
    "mutate_scope",
    "mutate_sign",
    "mutate_unit",
    "mutate_valuation_basis",
    "mutate_version",
    "mutate_ytd_vs_single",
]
