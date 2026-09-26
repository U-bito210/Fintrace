"""Repository hygiene guards.

A ``.gitignore`` pattern is invisible by construction: once a path is ignored it
never shows up in ``git status``, ``git diff`` or a review.  A pattern that
accidentally matches *source* can therefore keep a whole package out of the
repository until somebody clones it and hits ``ModuleNotFoundError``.

That is exactly what happened to ``src/cn/runs/`` — the CARD-07 Run Manifest
package.  The bare pattern ``runs/`` was written for the runtime store
(``_DEFAULT_STORE_DIR == _REPO_ROOT / "runs"``) but, lacking a leading ``/``,
also matched the source package of the same name.  Four ``.py`` files were never
tracked.

The rule below is deliberately implemented in plain Python — no ``git``
subprocess, no wall clock, no network — so it stays a normal offline unit test:

    a *bare* directory pattern (exactly ``name/``: no other slash, no wildcard)
    must not match a directory that contains Python source.

``__pycache__/`` passes (it only ever holds ``.pyc``).  ``runs/`` did not.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"

#: The maintained surface: anything importable here must be in the repository.
GUARDED_ROOTS = ("src", "tests")

#: ``name/`` — anchored, no wildcard, no internal slash.  ``/runs/`` and
#: ``output/`` are anchored/qualified and therefore out of scope; this is about
#: the unanchored form that git matches at *every* depth.
_BARE_DIR_RE = re.compile(r"\A(?P<name>[A-Za-z0-9_.-]+)/\Z")


def _gitignore_lines() -> List[str]:
    return GITIGNORE.read_text(encoding="utf-8").splitlines()


def _bare_directory_patterns() -> List[str]:
    patterns: List[str] = []
    for raw_line in _gitignore_lines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        match = _BARE_DIR_RE.match(line)
        if match is not None:
            patterns.append(match.group("name"))
    return patterns


def test_no_bare_gitignore_pattern_swallows_python_source() -> None:
    """The guard that would have caught ``runs/`` eating ``src/cn/runs/``."""
    patterns = _bare_directory_patterns()
    # Sanity: this file must actually be reading the real .gitignore.
    assert "__pycache__" in patterns, (
        f"expected '__pycache__/' among bare directory patterns, got {patterns!r}"
    )

    offenders: Dict[str, List[str]] = {}
    for root_name in GUARDED_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for directory in root.rglob("*"):
            if not directory.is_dir() or directory.name not in patterns:
                continue
            sources = sorted(
                child.name for child in directory.iterdir() if child.suffix == ".py"
            )
            if sources:
                offenders[str(directory.relative_to(REPO_ROOT)).replace("\\", "/")] = sources

    assert not offenders, (
        "these directories are ignored by an unanchored '.gitignore' pattern but "
        "contain Python source, so they are silently missing from the repository — "
        "anchor the pattern with a leading '/' (e.g. '/runs/') or rename the "
        f"directory: {offenders!r}"
    )


def test_the_anchored_run_store_pattern_still_matches_the_real_store() -> None:
    """Anchoring ``runs/`` must not stop ignoring the runtime manifest store."""
    from src.cn.runs.recorder import _DEFAULT_STORE_DIR

    store = _DEFAULT_STORE_DIR.resolve().relative_to(REPO_ROOT)
    assert store.as_posix() == "runs", (
        "the Run Manifest store moved; revisit the '.gitignore' rule that keeps "
        f"runtime artifacts out of Git (got {store.as_posix()!r})"
    )

    lines = {line.strip() for line in _gitignore_lines()}
    assert "/runs/" in lines, (
        "the anchored '/runs/' rule is gone, so runtime manifests under runs/ "
        "would become committable"
    )
