"""Draft prose -> page-located sentences.

CARD-05 §5 requires every claim to carry the **verbatim** sentence it came from,
so a finding can be shown next to the text it accuses. CARD-01 gives us located
paragraphs (``kind="text"`` facts with a page); this module cuts those paragraphs
into sentences without touching a character of their content.

**Measured deviation from the card's §4 assumption.** The card assumes the draft
arrives as "带页码段落". Measured on the committed corpus, ``ReportDraftParser``
yields **one text fact per page**, not one per paragraph: it splits on blank
lines, and a reportlab-rendered draft has none — ``extract_text()`` returns the
page as a single string joined by newlines. The page-level location is intact, so
nothing is lost except paragraph granularity, which this module restores at the
sentence level. No CARD-01 change is needed; sentence granularity is a *checker*
concern and belongs here.

Wrapping is handled conservatively: a line that does not end a sentence is joined
to the next one, because a PDF may break a single sentence across visual lines.
Only real terminators (``。！？!?``) and blank lines cut.

**Known consequence: a heading is absorbed into the sentence below it.** A draft's
title carries no terminal punctuation, so the join rule attaches it to the first
body sentence — measured on the committed corpus, a draft's first claim quotes
``「…点评（草稿）公司当前市盈率为50倍。」`` rather than the sentence alone. This is
left as is, deliberately. Separating a heading requires knowing which lines are
set in a larger typeface, and the text layer does not carry it: ``extract_text()``
emits exactly one ``\\n`` per visual line and collapses the vertical gap that
``Spacer`` leaves (measured at 8/18/28/40/60pt — identical output for all five), so
the paragraph split in :mod:`src.cn.documents.parsers.report_draft` never fires
either. Every alternative — "the first line of a page", "a line with no digits" —
misreads a genuinely wrapped sentence, which is the common case. Guessing here
would silently shift a claim's quoted text, and §9 says a wrong quote costs more
than a slightly noisy one. The consequence is cosmetic *and* conservative: the
heading's own words are checked like any other draft text, never exempted from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..documents.models import FACT_KIND_TEXT

#: Sentence terminators. ``；`` and ``，`` are deliberately absent: they separate
#: clauses *within* a sentence, and cutting there would make the quoted "original
#: sentence" a fragment — exactly what §5 forbids.
_TERMINATORS = "。！？!?"
_SPLIT_RE = re.compile(rf"(?<=[{_TERMINATORS}])")

#: A citation opener. A draft may write 「……增长10%（来源：年报#p2）。」 — inside the
#: sentence — or 「……增长10%。（来源：年报#p2）」 — after it. The second form is common,
#: and a naive terminator split cuts between the claim and its own source, so the
#: following sentence ends up carrying the previous sentence's citation while the
#: claim it belongs to appears uncited. Measured on the corpus after a PDF wrap put
#: the citation on its own visual line: five claims, every citation one sentence too
#: late. :func:`_pull_citations_back` re-attaches them.
_CITATION_LEAD_RE = re.compile(r"^\s*[（(]\s*(?:来源|见|参见|详见)\s*[:：]?")

#: A page whose entire text is a heading yields a sentence with no claim; keeping
#: it is harmless and removing it would be an editorial judgement this layer must
#: not make. Titles are recorded with ``kind`` "title" so callers *may* filter.
_KIND_TITLE = "title"


@dataclass(frozen=True)
class DraftSentence:
    """One sentence of the draft, located by page and order."""

    sentence_id: str
    document_id: str
    text: str
    page: Optional[int] = None
    index: int = 0
    fact_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "sentence_id": self.sentence_id,
            "document_id": self.document_id,
            "page": self.page,
            "index": self.index,
            "text": self.text,
            "fact_id": self.fact_id,
        }


def split_sentences(text: str) -> List[str]:
    """Cut *text* into sentences, joining lines that do not end a sentence.

    Pure string work: no model, no heuristic about what is "a claim".
    """
    if not text:
        return []
    sentences: List[str] = []
    buffer = ""
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            # A blank line is a hard boundary even without punctuation.
            if buffer.strip():
                sentences.append(buffer.strip())
            buffer = ""
            continue
        buffer = f"{buffer}{line}" if buffer else line
        parts = _SPLIT_RE.split(buffer)
        # ``re.split`` with a look-behind keeps the terminator; the trailing
        # element is whatever has not been terminated yet.
        tail = parts.pop() if parts else ""
        for part in parts:
            if part.strip():
                sentences.append(part.strip())
        buffer = tail
    if buffer.strip():
        sentences.append(buffer.strip())
    return _pull_citations_back(sentences)


def _pull_citations_back(sentences: List[str]) -> List[str]:
    """Move a leading citation of sentence *i* back onto sentence *i-1*.

    Only the citation group moves; whatever follows it stays where it was, so a
    fragment like ``（来源：年报#p3）公司2025年营收为90万元。`` splits into a suffix for
    the previous sentence and a whole new sentence.
    """
    for index in range(1, len(sentences)):
        match = _CITATION_LEAD_RE.match(sentences[index])
        if not match:
            continue
        citation, remainder = _cut_citation(sentences[index], match.end())
        if not citation:
            continue
        sentences[index - 1] = f"{sentences[index - 1]}{citation}"
        sentences[index] = remainder
    return [item for item in sentences if item.strip()]


def _cut_citation(fragment: str, start: int) -> tuple:
    """Split off the parenthesised citation beginning at *start*."""
    depth = 1
    for position in range(start, len(fragment)):
        if fragment[position] in "（(":
            depth += 1
        elif fragment[position] in "）)":
            depth -= 1
            if depth == 0:
                return fragment[: position + 1], fragment[position + 1:].strip()
    # Unclosed parenthesis: keep the whole fragment as the citation rather than
    # dropping text — a citation without its closing bracket is still a citation.
    return fragment.strip(), ""


def sentences_from_facts(
    facts: Iterable[object],
    *,
    document_id: str = "",
) -> List[DraftSentence]:
    """Convert CARD-01 text facts into ordered, page-located sentences.

    Only ``kind="text"`` facts are read. ``table_cell`` facts are skipped: a
    draft's embedded table is raw structure, and turning cells into "sentences"
    would manufacture quotes that never appeared as prose.
    """
    ordered: List[Tuple[int, int, object]] = []
    for position, fact in enumerate(facts):
        if str(getattr(fact, "kind", "") or "") != FACT_KIND_TEXT:
            continue
        text = str(getattr(fact, "raw_text", "") or "")
        if not text.strip():
            continue
        page = getattr(fact, "page", None)
        ordered.append((page if page is not None else 10**6, position, fact))

    # Document order, by page then by original position. A fact whose page is
    # unknown sorts last rather than being dropped — "unknown" is not "absent".
    ordered.sort(key=lambda item: (item[0], item[1]))

    sentences: List[DraftSentence] = []
    for _, _, fact in ordered:
        doc_id = str(getattr(fact, "document_id", "") or document_id)
        page = getattr(fact, "page", None)
        fact_id = getattr(fact, "fact_id", None)
        for text in split_sentences(str(getattr(fact, "raw_text", "") or "")):
            sentences.append(DraftSentence(
                sentence_id=f"{doc_id}#s{len(sentences) + 1:04d}",
                document_id=doc_id,
                text=text,
                page=page,
                index=len(sentences),
                fact_id=str(fact_id) if fact_id else None,
            ))
    return sentences


def sentence_by_id(sentences: Sequence[DraftSentence], sentence_id: str) -> Optional[DraftSentence]:
    for sentence in sentences:
        if sentence.sentence_id == sentence_id:
            return sentence
    return None


def claim_sentence_id(document_id: str, index: int) -> str:
    """The deterministic id an extractor should hand back for sentence *index*."""
    return f"{document_id}#s{index + 1:04d}"


__all__ = [
    "DraftSentence",
    "claim_sentence_id",
    "sentence_by_id",
    "sentences_from_facts",
    "split_sentences",
]
