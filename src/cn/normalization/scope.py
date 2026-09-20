"""口径 (scope) detection: 合并报表 vs 母公司报表 (CARD-02 §6, §7.3 rule 4).

The two scopes are never mixed inside one calculation: a 母公司 number compared
against a 合并 number is a wrong answer that looks plausible, so an undetectable
— or genuinely ambiguous — scope stays ``None`` and the caller decides.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import Scope

_PARENT_RE = re.compile(r"母公司|PARENT", re.IGNORECASE)
_CONSOLIDATED_RE = re.compile(r"合并|CONSOLIDATED", re.IGNORECASE)


def detect_scope(text: object) -> Optional[Scope]:
    """Classify a table header / section title; ``None`` when it says neither.

    A title matching *both* spellings (``"合并及母公司资产负债表"``, used as a
    page-level title covering two tables) is ambiguous and returns ``None``
    rather than picking one — the caller records a warning for it.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    parent = bool(_PARENT_RE.search(value))
    consolidated = bool(_CONSOLIDATED_RE.search(value))
    if parent and not consolidated:
        return Scope.PARENT
    if consolidated and not parent:
        return Scope.CONSOLIDATED
    return None


def scope_token(scope: Optional[Scope]) -> str:
    """Stable scope label used inside ids and statement bases."""
    if scope is None:
        return "未标注口径"
    return scope.value
