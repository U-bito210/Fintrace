"""The card's **only** LLM step: moving literals out of a sentence, nothing more.

CARD-05 §6 gives the extractor one job — pull
``metric / period / value / unit / growth / valuation_multiple / source_reference /
claim_type`` out of a sentence — and this module is written so that the model
cannot do more than that even if it tries:

* The model is asked for **literals as written** (``metric_label``,
  ``period_label``, ``value_literal``, ``growth_literal``), not canonical keys.
  Canonicalisation (``revenue`` / ``2025FY`` / ``亿元``) is done afterwards by
  CARD-02's own resolvers, so a model cannot smuggle a value in through a field
  that is supposed to be an identifier.
* A result with no verbatim ``sentence`` (or a sentence that does not match the
  input) is **discarded** (§6). The claim is not "fixed up"; it is dropped and the
  sentence is counted as unextracted.
* Every dropped or defaulted field records a ``reason``, so an empty field is
  never later read as "checked and found absent" (§9).

The model never sees the fact base and never produces a finding: it has no way to
know what the right answer is, which is the point.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Protocol, runtime_checkable

from src.agents.prompts import RUN_PROMPT_BUNDLES, record_usage, render
from ..normalization.metrics import is_known_metric, resolve_metric
from ..normalization.models import Scope
from ..normalization.periods import canonical_period
from ..normalization.units import canonical_unit, parse_numeric
from .models import ClaimType, DraftClaim
from .sentences import DraftSentence

EXTRACTOR_LLM = "llm"

#: A period written as nothing but a year — 「2025年」. See :func:`resolve_period_label`.
_BARE_YEAR_RE = re.compile(r"^\s*(\d{4})\s*年\s*$")

#: CARD-14 §10: registered, not hard-coded. Both messages are registered because
#: the *instruction* is prompt content even when it travels in the user turn.
SYSTEM_PROMPT = render("cn_claim_extractor_system")

RUN_PROMPTS = RUN_PROMPT_BUNDLES["report_checker"]


@runtime_checkable
class ClaimExtractionClient(Protocol):
    """Minimal, provider-neutral surface: one prompt in, text out.

    Kept deliberately narrower than any real client so an offline scripted client
    satisfies it without importing a model SDK.
    """

    async def complete(self, messages: List[Dict[str, str]], temperature: float = 0.0) -> str: ...


# --------------------------------------------------------------------------- #
# period resolution — one policy, shared by both extractors
# --------------------------------------------------------------------------- #

def resolve_period_label(label: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a period *as a draft writes it* to a canonical key.

    Returns ``(period_key, note)``; ``note`` is set when the resolution needed the
    one inference below.

    **The inference, and why it is here rather than in CARD-02.** ``canonical_period``
    deliberately refuses a bare year — 「2025年」 could be FY, H1 or Q4, and CARD-02
    will not guess. That refusal is correct and must not be relaxed. But in a
    *research draft* 「公司2025年实现营业收入…」 reads the fiscal year, and refusing it
    would leave ``period=None``, which makes the checker accept the figure under
    *any* period — a guaranteed miss on every period-mismatch case. So the inference
    is made here, once, at the draft layer, and it is deliberately the narrowest one
    available: **only** a label that is exactly a four-digit year plus 「年」.

    Everything else still goes through CARD-02's resolver, and anything it rejects
    is left unresolved rather than guessed. Both extractors call this, so the rule
    path and the model path cannot drift apart on what a period means.
    """
    if label is None:
        return None, None
    text = str(label).strip()
    if not text:
        return None, None
    try:
        return canonical_period(text, None), None
    except ValueError:
        pass
    match = _BARE_YEAR_RE.match(text)
    if match:
        return f"{match.group(1)}FY", (
            f"期间 {text!r} 只给出年份，按草稿语境取年度（{match.group(1)}FY）。"
        )
    return None, None


@dataclass
class ExtractionOutcome:
    """Claims plus the explicit account of what did **not** make it through.

    Iterable and sized like the ``list[DraftClaim]`` §6 specifies, so a caller can
    write ``for claim in outcome`` — but it also carries the sentences that did not
    produce a claim, because a silent list would hide them (§9).

    Two lists, and the difference matters when somebody reads the numbers:

    ``unextracted``
        the extractor *failed* on the sentence — no client, a provider error, or a
        result with no verbatim sentence. This is the count §9 means by "X 句未能提取".
    ``no_claim``
        the sentence was processed and simply carries nothing checkable (pure
        narrative, a heading). Merging it into ``unextracted`` would make every
        healthy draft look like a partial failure.
    """

    claims: List[DraftClaim] = field(default_factory=list)
    unextracted: List[DraftSentence] = field(default_factory=list)
    no_claim: List[DraftSentence] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    extractor: str = EXTRACTOR_LLM

    def __iter__(self):
        return iter(self.claims)

    def __len__(self) -> int:
        return len(self.claims)

    def __getitem__(self, item):
        return self.claims[item]

    def claim_for(self, sentence_id: str) -> Optional[DraftClaim]:
        for claim in self.claims:
            if claim.fact_id and claim.fact_id.endswith(sentence_id.split("#")[-1]):
                return claim
        for claim in self.claims:
            if claim.claim_id.startswith(sentence_id):
                return claim
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "extractor": self.extractor,
            "claims": [claim.to_dict() for claim in self.claims],
            "unextracted": [item.to_dict() for item in self.unextracted],
            "no_claim": [item.to_dict() for item in self.no_claim],
            "notes": list(self.notes),
        }


async def extract_claims(
    sentences: Sequence[DraftSentence],
    llm_client: Optional[ClaimExtractionClient] = None,
    *,
    batch_size: int = 3,
    temperature: float = 0.0,
) -> ExtractionOutcome:
    """§6 ``extract_claims``: sentences in, :class:`ExtractionOutcome` out.

    ``batch_size`` is intentionally small because the response contains a verbatim
    copy of every input sentence plus structured fields.  Keeping a bounded batch
    makes a transient provider stall visible only for the affected sentences,
    rather than turning an entire page of a draft into an all-or-nothing request.

    When *llm_client* is ``None`` nothing is invented: every sentence is reported
    as unextracted with an explicit note, and the caller sees ``status=blocked``
    instead of an empty report that would look like "no errors found".
    """
    sentences = list(sentences or ())
    if not sentences:
        return ExtractionOutcome(extractor=EXTRACTOR_LLM)
    if llm_client is None:
        return ExtractionOutcome(
            unextracted=list(sentences),
            notes=("未配置模型客户端，本次未做任何提取；所有句子均记为未提取。",),
            extractor=EXTRACTOR_LLM,
        )

    outcome = ExtractionOutcome(extractor=EXTRACTOR_LLM)
    by_id = {item.sentence_id: item for item in sentences}
    # CARD-14: audit the prompt versions used. ``model`` stays None when the client
    # does not expose one — an unknown model is recorded as unknown, never guessed.
    for prompt_name in RUN_PROMPTS:
        record_usage(prompt_name, getattr(llm_client, "model_name", None), temperature)
    for start in range(0, len(sentences), max(1, batch_size)):
        batch = sentences[start:start + max(1, batch_size)]
        user = _render_batch(batch)
        try:
            raw = await llm_client.complete(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                temperature,
            )
        except Exception as exc:  # a provider failure must not crash the report
            outcome.notes.append(f"第 {start + 1}-{start + len(batch)} 句提取调用失败：{type(exc).__name__}: {exc}")
            outcome.unextracted.extend(batch)
            continue
        outcome.claims.extend(
            _parse_batch(raw, batch, by_id, outcome.unextracted, outcome.notes, outcome.no_claim)
        )
    return outcome


def _render_batch(batch: Sequence[DraftSentence]) -> str:
    payload = [
        {"sentence_id": item.sentence_id, "page": item.page, "text": item.text}
        for item in batch
    ]
    # CARD-14: the instruction line lives in the registry like every other prompt;
    # the JSON payload is data and stays here.
    return render("cn_claim_extractor_user", payload=json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_batch(
    raw: Any,
    batch: Sequence[DraftSentence],
    by_id: Dict[str, DraftSentence],
    unextracted: List[DraftSentence],
    notes: List[str],
    no_claim: Optional[List[DraftSentence]] = None,
) -> List[DraftClaim]:
    data = _loads(raw)
    if data is None:
        notes.append(f"第 {batch[0].index + 1} 句起的一批返回内容不是合法 JSON，该批全部记为未提取。")
        unextracted.extend(batch)
        return []

    items = data.get("claims") if isinstance(data, dict) else data
    if not isinstance(items, list):
        notes.append("返回结构中没有 claims 数组，该批全部记为未提取。")
        unextracted.extend(batch)
        return []

    produced: List[DraftClaim] = []
    covered: set = set()
    bare: List[DraftSentence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sentence = _match_sentence(item, batch, by_id)
        if sentence is None:
            # §6: no verbatim sentence -> the extraction result is discarded.
            notes.append(
                f"丢弃一条提取结果：{item.get('sentence_id') or item.get('sentence')!r} "
                "缺少可逐字对应的原句。"
            )
            continue
        if sentence.sentence_id in covered:
            continue
        claim = _to_claim(item, sentence)
        if not _carries_anything(claim):
            # The model read the sentence and found nothing to move. That is a
            # successful extraction of an empty result, not a failure — counting it
            # as "unextracted" would make every healthy draft look broken.
            covered.add(sentence.sentence_id)
            bare.append(sentence)
            continue
        covered.add(sentence.sentence_id)
        produced.append(claim)

    for sentence in batch:
        if sentence.sentence_id not in covered:
            unextracted.append(sentence)
    if no_claim is not None:
        no_claim.extend(bare)
    return produced


def _carries_anything(claim: DraftClaim) -> bool:
    """Whether an extracted claim has anything a rule could act on."""
    return bool(
        claim.value is not None
        or claim.growth is not None
        or claim.valuation_multiple
        or claim.claim_type == ClaimType.CAUSAL.value
    )


def _match_sentence(
    item: Dict[str, Any], batch: Sequence[DraftSentence], by_id: Dict[str, DraftSentence]
) -> Optional[DraftSentence]:
    """The sentence a result claims to be about, or ``None`` if it cannot be trusted.

    **A ``sentence_id`` alone is not proof of anything.** Measured failure: a result
    that named the right id but quoted a *different* sentence was accepted, so the
    claim went out carrying a quote the draft never contained — and ``DraftClaim.
    sentence`` is what the report shows a reviewer as "原句". A paraphrased or edited
    quote is a fabricated citation, which is the precise failure §6's "必须逐字保留"
    exists to prevent. So the quoted text must match the real sentence; the id is
    only a hint for finding it.

    Comparison is whitespace-normalised and a prefix match is tolerated, because a
    model may reproduce the sentence with different spacing or stop at a clause
    boundary — neither changes the words. Anything beyond that is discarded.
    """
    sid = item.get("sentence_id")
    quoted = _normalise_text(item.get("sentence"))
    candidate: Optional[DraftSentence] = None

    if sid and str(sid) in by_id:
        found = by_id[str(sid)]
        if found in batch:
            candidate = found

    if candidate is None:
        if not quoted:
            return None
        for sentence in batch:
            if _normalise_text(sentence.text) == quoted:
                return sentence
        for sentence in batch:
            if quoted in _normalise_text(sentence.text):
                return sentence
        return None

    if not quoted:
        # §6: "不携带原句的提取结果丢弃" — the id does not stand in for the text.
        return None
    actual = _normalise_text(candidate.text)
    if quoted == actual or quoted in actual:
        return candidate
    return None


def _to_claim(item: Dict[str, Any], sentence: DraftSentence) -> DraftClaim:
    reasons: List[str] = []
    metric_label = _clean(item.get("metric_label"))
    metric = _canonical_metric(metric_label, reasons)

    period_label = _clean(item.get("period_label"))
    period, period_note = resolve_period_label(period_label)
    if period_label and period is None:
        reasons.append(f"期间 {period_label!r} 不是受支持的期间写法，已留空。")
    elif period_note:
        # Recorded, not silent: the reader can see that 「2025年」 was read as FY.
        reasons.append(period_note)

    value_literal = _clean(item.get("value_literal"))
    value = parse_numeric(value_literal.replace("，", ",")) if value_literal else None
    if value_literal and value is None:
        reasons.append(f"数值 {value_literal!r} 无法解析，已留空。")

    unit = canonical_unit(_clean(item.get("unit"))) if _clean(item.get("unit")) else None

    # The 口径 is canonicalised here, not trusted: the model hands back the word it
    # saw and CARD-02's ``Scope.coerce`` decides what it means, so "母公司"/"母公司报表"
    # cannot arrive as two different scopes. An unrecognised word becomes ``None``
    # (undeclared) rather than a scope the checker would then match against.
    scope_token = _clean(item.get("scope"))
    scope_enum = Scope.coerce(scope_token) if scope_token else None
    if scope_token and scope_enum is None:
        reasons.append(f"口径 {scope_token!r} 不是「合并」或「母公司」，已留空。")
    scope = scope_enum.value if scope_enum else None

    growth_literal = _clean(item.get("growth_literal"))
    growth = parse_numeric(growth_literal) if growth_literal else None
    if growth is not None and growth_literal and growth_literal.strip().startswith("-"):
        growth = -abs(growth)

    claim_type = _clean(item.get("claim_type")) or ClaimType.FACTUAL.value
    if claim_type not in {member.value for member in ClaimType}:
        reasons.append(f"claim_type {claim_type!r} 不在枚举内，按 factual 处理。")
        claim_type = ClaimType.FACTUAL.value

    source = _clean(item.get("source_reference"))
    references = tuple(_split_references(source))

    return DraftClaim(
        claim_id=f"{sentence.sentence_id}-c1",
        sentence=sentence.text,
        page=sentence.page,
        metric=metric,
        metric_label=metric_label,
        period=period,
        value=value,
        value_literal=value_literal,
        unit=unit,
        growth=growth,
        growth_literal=growth_literal,
        valuation_multiple=_clean(item.get("valuation_multiple")),
        source_reference=source,
        claim_type=claim_type,
        scope=scope,
        source_references=references,
        causal_antecedent=_clean(item.get("causal_antecedent")),
        causal_consequent=_clean(item.get("causal_consequent")),
        document_id=sentence.document_id,
        fact_id=sentence.fact_id,
        extractor=EXTRACTOR_LLM,
        reason="；".join(reasons) or None,
    )


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #

def _canonical_metric(label: Optional[str], reasons: List[str]) -> Optional[str]:
    if not label:
        return None
    if is_known_metric(label):
        return label
    match = resolve_metric(label)
    if match.known:
        return match.key
    reasons.append(f"指标 {label!r} 不在 CARD-02 标准指标字典内，已留空。")
    return None


def _loads(raw: Any) -> Optional[Any]:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "null", "none", "n/a", "na", "-"):
        return None
    return text


def _normalise_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _split_references(value: Optional[str]) -> List[str]:
    """Split a mixed citation string into individual references.

    Only unambiguous separators are used. A comma is *not* one — it appears inside
    Chinese sentences far too often, and splitting on it would invent references.
    """
    if not value:
        return []
    parts = re.split(r"[;；\n]+", value)
    return [part.strip() for part in parts if part.strip()]


__all__ = [
    "ClaimExtractionClient",
    "EXTRACTOR_LLM",
    "ExtractionOutcome",
    "SYSTEM_PROMPT",
    "extract_claims",
    "resolve_period_label",
]
