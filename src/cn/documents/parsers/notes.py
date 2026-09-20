"""Body / note text extraction for statement-style filings (CARD-01 → CARD-04).

Why this exists
---------------
``TableParser`` reconstructs the three statements and discards every text line
that is not inside a table. Measured on the committed synthetic corpus, an
annual report therefore yielded **zero** ``text`` facts: CARD-01 §5 declares
``section`` as "章节标题", but no chapter attribution and no note text were ever
emitted. CARD-04's mandate — detect 会计政策变更 / 前期差错更正 from the *notes*
and from correction announcements — had no input surface on a filing at all.

What this emits
---------------
Every text line **outside** a table bbox becomes one ``text`` fact carrying:

* ``section`` — the nearest preceding chapter heading **on the same page**, or
  ``None`` when the page carries no heading. The reset is deliberate: see
  :func:`extract_body_text_facts`.
* ``page`` / ``bbox`` — the verbatim location, so a note fact is jumpable to the
  original page exactly like a cell;
* ``raw_value = None`` — deliberately, and this is load-bearing. CARD-02's
  projector drops any fact whose ``raw_value`` is not numeric (``pipeline.py``),
  and the CARD-01 extraction benchmark scores ``table_cell`` facts only. Leaving
  it ``None`` keeps both untouched. A note line that happens to contain a figure
  is still not a table figure: promoting it would let it be normalised with no
  column context, i.e. inventing a period for it.

Scope boundary
--------------
Heading matching is a *declared seed table*, not a claim of completeness. It
lists the A-share note chapters that CARD-04 needs; anything else simply leaves
``section`` unchanged, and a filing whose notes use a wording outside the table
keeps its text facts with an inherited section. CARD-04 §9's coverage statement
is what communicates that limit to a reader — this module must not pretend the
list is exhaustive.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from ..models import FACT_KIND_TEXT, RawFact
from .base import line_bbox
from .table import STATEMENT_HEADERS

# Canonical note chapter headings (seed table — see module docstring).
CHAPTER_HEADINGS: Tuple[str, ...] = (
    "合并财务报表项目注释",
    "母公司财务报表主要项目注释",
    "重要会计政策及会计估计变更",
    "重要会计政策和会计估计",
    "重要会计政策及会计估计",
    "重要会计政策变更",
    "重要会计估计变更",
    "前期会计差错更正",
    "前期差错更正",
    "会计政策变更",
    "会计估计变更",
    "财务报表附注",
    "财务报表的编制基础",
    "公司基本情况",
    "合并财务报表范围",
    "合并范围的变更",
    "在其他主体中的权益",
    "关联方及关联交易",
    "承诺及或有事项",
    "资产负债表日后事项",
)

# Longest-first so "重要会计政策及会计估计" wins over the "会计政策变更" suffix
# family and a heading is never truncated to a generic prefix.
_HEADINGS_BY_LENGTH: Tuple[str, ...] = tuple(sorted(CHAPTER_HEADINGS, key=len, reverse=True))

# Leading enumeration: 第三节 / 五、 / （三） / 3. / (2). Captured, because
# "was there an enumeration?" is itself the evidence that a line is a heading —
# an optional group alone would match the empty string everywhere.
_ENUM_PREFIX_RE = re.compile(
    r"^\s*(?P<enum>"
    r"第[一二三四五六七八九十百零〇0-9]+[节章部分]"
    r"|[（(][一二三四五六七八九十0-9]+[)）]"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|[0-9]+[、.．]"
    r")?\s*"
)

# A heading is a short label. Allowing a small tail covers "会计政策变更（续）"
# while refusing a full sentence that merely mentions the chapter name — the
# distinction matters because a paragraph mentioning 会计政策变更 is *evidence*
# for CARD-04, whereas a heading is the *anchor* it hangs under.
_HEADING_TAIL_TOLERANCE = 8

# An unlisted enumerated heading ("八、其他重要事项") must still close the
# previous chapter. Treating it as body text left the paragraphs beneath it
# attributed to the *previous* chapter — a silent mis-attribution, which is worse
# than an honest ``None``. The line is accepted as a heading only when it looks
# like a bare label: short, no sentence punctuation.
_MAX_LABEL_LENGTH = 20
_SENTENCE_PUNCTUATION = "。；，"


def heading_of(line: str) -> Optional[str]:
    """Return the chapter heading a line constitutes, else ``None``.

    Two ways to qualify:

    * a canonical heading (or statement title) from the seed tables — matched
      after stripping leading enumeration, so "（三）会计政策变更" qualifies while
      "本公司报告期内无会计政策变更事项" does not;
    * an *unlisted* enumerated bare label ("八、其他重要事项"). Recognising it as a
      chapter boundary is a structural claim only — the label is returned
      verbatim and never mapped onto a signal type, which stays CARD-04's job.
    """
    text = (line or "").strip()
    if not text:
        return None
    match = _ENUM_PREFIX_RE.match(text)
    stripped = text[match.end():].strip() if match else text
    if not stripped:
        return None
    for heading in _HEADINGS_BY_LENGTH:
        if stripped.startswith(heading) and len(stripped) <= len(heading) + _HEADING_TAIL_TOLERANCE:
            return heading
    # Statement titles ("合并利润表") anchor their own body text the same way the
    # table cells are anchored by ``TableParser``.
    for title in sorted(STATEMENT_HEADERS, key=len, reverse=True):
        if stripped.startswith(title) and len(stripped) <= len(title) + _HEADING_TAIL_TOLERANCE:
            return title
    if match and match.group("enum") and _is_bare_label(stripped):
        return stripped
    return None


def _is_bare_label(text: str) -> bool:
    if not text or len(text) > _MAX_LABEL_LENGTH:
        return False
    return not any(char in text for char in _SENTENCE_PUNCTUATION)


def _center_inside(bbox: Sequence[float], box: Sequence[float]) -> bool:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    bx0, by0, bx1, by1 = box
    return bx0 <= cx <= bx1 and by0 <= cy <= by1


def extract_body_text_facts(pages: Sequence[object]) -> List[RawFact]:
    """Emit one ``text`` fact per line lying outside every table bbox.

    ``pages`` is a materialized pdfplumber page list. Pages with no text layer
    contribute nothing: ``TableParser`` already owns the scan-page contract
    (OCR attempt, else fail-closed), and duplicating it here would let a scan
    page be reported twice.

    Chapter attribution resets at every page boundary. An inherited chapter is a
    *guess*, and this project refuses guesses elsewhere (period semantics, units,
    scope) — carrying 其他重要事项 onto a page that is actually the 前期差错更正
    detail produces a confidently wrong label, whereas ``None`` is merely
    uninformative. A note chapter that genuinely spans pages therefore only keeps
    its label on the page carrying the heading; the paragraphs still match by
    their own text, and §9's coverage statement says so out loud.
    """
    facts: List[RawFact] = []
    current_chapter: Optional[str] = None

    for page_index, page in enumerate(pages, start=1):
        current_chapter = None
        words = page.extract_words()
        if not words:
            continue
        table_boxes = [
            tuple(table.bbox)
            for table in page.find_tables()
            if getattr(table, "bbox", None)
        ]
        for line in line_bbox(words):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            bbox = line.get("bbox")
            if bbox and any(_center_inside(bbox, box) for box in table_boxes):
                continue  # already located as a table_cell fact
            heading = heading_of(text)
            if heading is not None:
                current_chapter = heading
            facts.append(
                RawFact(
                    page=page_index,
                    bbox=bbox,
                    raw_text=text,
                    section=current_chapter,
                    kind=FACT_KIND_TEXT,
                )
            )
    return facts


__all__ = ["CHAPTER_HEADINGS", "extract_body_text_facts", "heading_of"]
