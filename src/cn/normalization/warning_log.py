"""Deduplicating warning collector for the normalization pipeline.

CARD-02 §9 requires "零静默": every value that cannot be normalized must appear
in ``warnings``. A naive list would flood the result with one identical line per
cell (a 200-row balance sheet easily produces hundreds), which defeats the
purpose — the warnings are meant to be read by a human and by CARD-05.

``WarningLog`` keeps messages deterministic and ordered:

* identical messages collapse to a single entry (first-seen order preserved);
* repetitive per-cell messages are *tallied* and flushed once as
  ``"<reason> ×N：<example>"``.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple


class WarningLog:
    """Ordered, de-duplicated warning list with counted tallies."""

    def __init__(self) -> None:
        self._messages: List[str] = []
        self._seen: set[str] = set()
        self._tallies: Dict[str, Tuple[int, str, str]] = {}   # key -> (count, reason, example)
        self._order: List[str] = []

    def add(self, message: Optional[str]) -> None:
        """Append one warning; repeated identical text is ignored."""
        if not message:
            return
        text = str(message)
        if text in self._seen:
            return
        self._seen.add(text)
        self._messages.append(text)

    def extend(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.add(message)

    def tally(self, key: str, reason: str, example: str = "") -> None:
        """Count a repetitive condition; flushed as one aggregated line."""
        count, _, first = self._tallies.get(key, (0, reason, example))
        if not first:
            first = example
        self._tallies[key] = (count + 1, reason, first)
        if key not in self._order:
            self._order.append(key)

    def as_list(self) -> List[str]:
        """Materialise messages plus counted tallies (tallies appended last)."""
        out = list(self._messages)
        for key in self._order:
            count, reason, example = self._tallies[key]
            suffix = f"（示例：{example}）" if example else ""
            line = f"{reason} ×{count}{suffix}"
            if line not in self._seen:
                self._seen.add(line)
                out.append(line)
        return out

    def __len__(self) -> int:
        return len(self._messages) + len(self._tallies)

    @property
    def messages(self) -> List[str]:
        """Only the non-aggregated messages (stable order)."""
        return list(self._messages)
