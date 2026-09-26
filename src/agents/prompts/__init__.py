"""CARD-14 Prompt Registry — the only home for LLM prompt text.

See :mod:`src.agents.prompts.registry` for the contract. The short version:

* ``templates/<name>.<version>.md`` holds the text; frontmatter declares
  ``name`` / ``version`` / ``variables``.
* :func:`render` substitutes ``{{variable}}`` placeholders and raises on any
  mismatch — a half-rendered prompt is never returned.
* :func:`prompt_bundle_hash` produces the value that goes into
  ``RunManifest.prompt_hash`` for a whole run.
* ``registry.lock.json`` freezes released hashes so a version can only be added,
  never silently edited.
"""

from __future__ import annotations

from .registry import (
    LOCK_PATH,
    RUN_PROMPT_BUNDLES,
    TEMPLATES_DIR,
    PromptNotFound,
    PromptRegistryError,
    PromptSpec,
    PromptTemplateError,
    PromptVariableError,
    bundle_for,
    default_registry,
    known_prompts,
    load_registry,
    lock_payload,
    lock_report,
    prompt_bundle,
    prompt_bundle_hash,
    prompt_hash,
    record_usage,
    registry_table,
    render,
    reset_usage,
    resolve,
    usage_log,
    verify_lock,
    write_lock,
)

__all__ = [
    "LOCK_PATH",
    "RUN_PROMPT_BUNDLES",
    "TEMPLATES_DIR",
    "PromptNotFound",
    "PromptRegistryError",
    "PromptSpec",
    "PromptTemplateError",
    "PromptVariableError",
    "bundle_for",
    "default_registry",
    "known_prompts",
    "load_registry",
    "lock_payload",
    "lock_report",
    "prompt_bundle",
    "prompt_bundle_hash",
    "prompt_hash",
    "record_usage",
    "registry_table",
    "render",
    "reset_usage",
    "resolve",
    "usage_log",
    "verify_lock",
    "write_lock",
]
