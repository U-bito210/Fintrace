"""``python -m src.agents.prompts [--write-lock] [--check]``."""

from __future__ import annotations

from .registry import main

if __name__ == "__main__":
    raise SystemExit(main())
