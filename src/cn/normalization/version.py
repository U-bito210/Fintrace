"""Version labelling: AS_REPORTED / RESTATED / CORRECTED (CARD-02 §6, §7.3 rule 5).

CARD-02 only *labels*; detecting a restatement is CARD-04's job. The wording
patterns are not defined here: they come from
:mod:`src.cn.disclosure_lexicon`, which CARD-04 owns, so a phrase can never be
recognised by one card and missed by the other. Two values of the same metric in
different versions may never be paired in one comparison (§7.3 rule 4).
"""

from __future__ import annotations

from typing import Optional

from ..disclosure_lexicon import DISCLOSURE_CORRECTED, DISCLOSURE_RESTATED
from .models import Version


def detect_version(text: object) -> Version:
    """Label the disclosure version of a paragraph / note / document title."""
    if text is None:
        return Version.AS_REPORTED
    value = str(text)
    if not value.strip():
        return Version.AS_REPORTED
    # CORRECTED wins over RESTATED: a 更正公告 supersedes an earlier restatement
    # for the *same* figure, and labelling the newer supersession as the older
    # category would silently re-enable a superseded value.
    if DISCLOSURE_CORRECTED.search(value):
        return Version.CORRECTED
    if DISCLOSURE_RESTATED.search(value):
        return Version.RESTATED
    return Version.AS_REPORTED


def version_rank(version: Version) -> int:
    """Recency rank used to pick the effective value of one metric+period."""
    return {Version.AS_REPORTED: 0, Version.RESTATED: 1, Version.CORRECTED: 2}[version]


# CARD-01 tags a table column with a disclosure-version token when the header
# says so ("调整后金额", "更正后金额"). The token is a *label* supplied by the
# parser; mapping it here keeps the vocabulary in one place instead of letting
# each parser invent its own spelling.
_COLUMN_VERSION_TOKENS = {
    "as_reported": Version.AS_REPORTED,
    "restated": Version.RESTATED,
    "corrected": Version.CORRECTED,
}


def version_from_column(meta: object) -> Optional[Version]:
    """Version declared by a cell's column header, or ``None`` when unmarked.

    ``None`` means "the column said nothing", which is *not* the same as
    "as reported" — the caller falls back to the document-level label. Returning
    ``AS_REPORTED`` here would silently discard an overriding 调整后 column.
    """
    if not isinstance(meta, dict):
        return None
    token = meta.get("version_label")
    if token is None:
        return None
    return _COLUMN_VERSION_TOKENS.get(str(token).strip().lower())


def pick_effective(versions) -> Optional[Version]:
    """The most recent version among several disclosures of the same figure."""
    candidates = [v for v in versions if isinstance(v, Version)]
    if not candidates:
        return None
    return max(candidates, key=version_rank)
