"""Versioned, content-addressed prompt registry (CARD-14).

The competition source-code checklist names **Prompt** as a first-class module,
and an unversioned prompt is a reproducibility hole: change one sentence and
every earlier run stops being explainable. This package is the single place a
prompt lives, so that:

* the text has exactly **one** home (``templates/<name>.<version>.md``) and no
  Python file carries an LLM prompt literal (CARD-14 §10);
* every prompt has a stable ``sha256`` over its template body, so the value that
  goes into ``RunManifest.prompt_hash`` (CARD-07) actually identifies the bytes
  the model saw;
* a released ``(name, version)`` can never silently change — the frozen hashes
  live in ``registry.lock.json`` and the test suite fails on drift (§10);
* a load failure is **fail-closed**: :func:`render` raises rather than falling
  back to a hard-coded string, because a fallback is precisely the thing that
  bypasses versioning (§9).

Placeholders use ``{{name}}`` (mustache-style, not :meth:`str.format`) so a
template may contain literal JSON braces — the memo narrator prompt embeds a
JSON schema verbatim, and ``str.format`` would try to interpret it.

Quick use::

    from src.agents.prompts import render, prompt_bundle_hash

    text = render("cn_research_agent_base")
    digest = prompt_bundle_hash(RUN_PROMPT_BUNDLES["research"])
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
    "load_registry",
    "lock_payload",
    "lock_report",
    "main",
    "known_prompts",
    "default_registry",
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

#: Template home. One file per ``(name, version)``: ``<name>.<version>.md``.
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: Frozen hashes of every released ``(name, version)``.
LOCK_PATH = Path(__file__).resolve().parent / "registry.lock.json"

LOCK_VERSION = 1

#: ``{{ variable }}`` — the only substitution form this registry understands.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: ``---\n<frontmatter>\n---\n<body>``. The body is verbatim; a template that
#: needs to start with a newline simply starts with a blank line.
_DOCUMENT_RE = re.compile(
    r"\A---[ \t]*\n(?P<front>.*?)\n---[ \t]*(?:\n(?P<body>.*))?\Z", re.DOTALL
)

#: ``<name>.<version>.md``.
_FILENAME_RE = re.compile(r"\A(?P<name>[A-Za-z0-9_]+)\.(?P<version>[0-9]+)\.md\Z")

#: Which registered prompts a given pipeline actually uses. This is what makes a
#: run's ``prompt_hash`` meaningful: it covers the whole set the model saw, not
#: just one file. Consumers import their own entry; the demo table renders all of
#: them so a reviewer can check the mapping without reading the source.
RUN_PROMPT_BUNDLES: Dict[str, Tuple[str, ...]] = {
    "research": (
        "cn_research_agent_base",
        "cn_research_agent_run_context",
        "cn_research_agent_preflight",
        # Multi-turn follow-up note. In the bundle because a run *may* send it;
        # ``record_usage`` still records only the prompts actually sent.
        "cn_research_agent_followup",
        "cn_research_agent_history_turn",
    ),
    "ablation": (
        "cn_ablation_agent_base",
        "cn_ablation_agent_run_context",
        "cn_ablation_agent_no_tools",
    ),
    "report_checker": ("cn_claim_extractor_system", "cn_claim_extractor_user"),
    "memo": ("cn_memo_section_narrator",),
    "daily_review": (
        "cn_daily_review_theme_polish_system",
        "cn_daily_review_theme_polish_user",
    ),
}


# --------------------------------------------------------------------------- #
# Errors — every one of these is fail-closed
# --------------------------------------------------------------------------- #
class PromptRegistryError(RuntimeError):
    """Base class: the registry could not be trusted, so the run must stop."""


class PromptNotFound(PromptRegistryError):
    """No template is registered under the requested name."""


class PromptVariableError(PromptRegistryError):
    """A render was asked for the wrong variable set (never half-render)."""


class PromptTemplateError(PromptRegistryError):
    """A template file is malformed or inconsistent with its frontmatter."""


# --------------------------------------------------------------------------- #
# Data model (CARD-14 §5)
# --------------------------------------------------------------------------- #
@dataclass
class PromptSpec:
    """One immutable-by-convention prompt version."""

    prompt_name: str
    prompt_version: int
    template: str
    variables: List[str]
    prompt_hash: str
    model: Optional[str] = None
    temperature: Optional[float] = None
    created_at: str = ""
    #: Set by the loader; used by the lock report and the demo table.
    source_file: Optional[str] = None
    description: Optional[str] = None

    @property
    def key(self) -> str:
        """Registry address: ``name@version`` (CARD-14 §8 edge 1)."""
        return f"{self.prompt_name}@{self.prompt_version}"

    @property
    def placeholders(self) -> Tuple[str, ...]:
        """Placeholders actually present in the body, in first-appearance order."""
        seen: List[str] = []
        for name in _PLACEHOLDER_RE.findall(self.template):
            if name not in seen:
                seen.append(name)
        return tuple(seen)

    def render(self, **variables: Any) -> str:
        """Substitute ``variables`` into the template, or raise.

        Missing *and* extra variables are both errors: a caller that passes the
        wrong set is a bug, and a partially substituted prompt is worse than no
        prompt at all (§8 edge 2).
        """
        declared = set(self.variables)
        provided = set(variables)
        missing = sorted(declared - provided)
        unexpected = sorted(provided - declared)
        if missing or unexpected:
            raise PromptVariableError(
                f"{self.key}: render variable mismatch"
                f" (missing={missing}, unexpected={unexpected}, declared={sorted(declared)})"
            )
        rendered = _PLACEHOLDER_RE.sub(lambda match: str(variables[match.group(1)]), self.template)
        leftover = sorted({name for name in _PLACEHOLDER_RE.findall(rendered) if name in declared})
        if leftover:
            raise PromptTemplateError(
                f"{self.key}: placeholders {leftover} survived rendering; "
                "a value that itself contains {{...}} would be rendered twice"
            )
        return rendered

    def as_row(self) -> Dict[str, Any]:
        """Flat, JSON-safe view used by the lock file and the demo table."""
        return {
            "name": self.prompt_name,
            "version": self.prompt_version,
            "variables": list(self.variables),
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "temperature": self.temperature,
            "created_at": self.created_at,
            "source_file": self.source_file,
            "description": self.description,
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    """Newline-normalise so the same file hashes identically on every platform."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_frontmatter(block: str, path: Path) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for offset, raw_line in enumerate(block.split("\n"), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise PromptTemplateError(f"{path.name}: frontmatter line {offset} is not 'key: value'")
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value == "":
            values[key] = None
            continue
        try:
            values[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            values[key] = raw_value
    return values


def _body_of(document: str, path: Path) -> str:
    """Body verbatim, minus the packaging whitespace around it.

    The convention is stated once, here, because it is load-bearing: prompting is
    sensitive to whitespace, and a reader who "tidies up" a file must be able to
    tell content from packaging.

    * the blank line after the closing ``---`` is a separator → stripped;
    * the file's final newline is an end-of-file convention → stripped.

    A prompt that genuinely starts or ends with a newline says so in
    frontmatter (``leading_newline`` / ``trailing_newline``) instead of relying
    on stray blank lines — which keeps every template file well-formed for
    ``git diff --check`` and makes the whitespace an explicit, reviewable fact.
    """
    body = document
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    return body


def _parse_bool(value: Any, key: str, path: Path) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PromptTemplateError(f"{path.name}: '{key}' must be true or false")
    return value


def _load_spec(path: Path) -> PromptSpec:
    match = _FILENAME_RE.match(path.name)
    if match is None:
        raise PromptTemplateError(
            f"{path.name}: template files must be named '<prompt_name>.<version>.md'"
        )
    document = _normalize(path.read_text(encoding="utf-8"))
    parsed = _DOCUMENT_RE.match(document)
    if parsed is None:
        raise PromptTemplateError(f"{path.name}: missing '---' frontmatter block")
    front = _parse_frontmatter(parsed.group("front"), path)
    body = _body_of(parsed.group("body") or "", path)
    if _parse_bool(front.get("leading_newline"), "leading_newline", path):
        body = "\n" + body
    if _parse_bool(front.get("trailing_newline"), "trailing_newline", path):
        body += "\n"

    name = front.get("name")
    version = front.get("version")
    if not isinstance(name, str) or not name:
        raise PromptTemplateError(f"{path.name}: frontmatter needs a string 'name'")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PromptTemplateError(f"{path.name}: frontmatter needs an integer 'version' >= 1")
    if name != match.group("name") or str(version) != match.group("version"):
        raise PromptTemplateError(
            f"{path.name}: frontmatter ({name}@{version}) disagrees with the filename "
            f"({match.group('name')}@{match.group('version')})"
        )
    raw_variables = front.get("variables")
    if raw_variables is None:
        raw_variables = []
    if not isinstance(raw_variables, list) or any(not isinstance(item, str) for item in raw_variables):
        raise PromptTemplateError(f"{path.name}: 'variables' must be a list of strings")
    if not body.strip():
        raise PromptTemplateError(f"{path.name}: template body is empty")

    spec = PromptSpec(
        prompt_name=name,
        prompt_version=version,
        template=body,
        variables=list(raw_variables),
        prompt_hash=sha256(body.encode("utf-8")).hexdigest(),
        model=front.get("model"),
        temperature=front.get("temperature"),
        created_at=str(front.get("created_at") or ""),
        source_file=path.name,
        description=front.get("description"),
    )
    # A declared-vs-used mismatch is a template bug, caught at load rather than
    # at the first render (where it would surface as a confusing KeyError).
    declared = set(spec.variables)
    present = set(spec.placeholders)
    if declared != present:
        raise PromptTemplateError(
            f"{path.name}: declared variables {sorted(declared)} != placeholders {sorted(present)}"
        )
    if len(spec.variables) != len(present):
        raise PromptTemplateError(f"{path.name}: 'variables' contains duplicates")
    return spec


def load_registry(templates_dir: Path | str | None = None) -> Dict[str, PromptSpec]:
    """Load every template in ``templates_dir``, keyed by ``name@version``.

    Raises :class:`PromptRegistryError` when the directory is missing or empty —
    an empty registry would silently mean "no prompt", which is the failure mode
    this module exists to prevent.
    """
    directory = Path(templates_dir) if templates_dir is not None else TEMPLATES_DIR
    if not directory.is_dir():
        raise PromptRegistryError(f"prompt template directory is missing: {directory}")
    specs: Dict[str, PromptSpec] = {}
    for path in sorted(directory.glob("*.md")):
        spec = _load_spec(path)
        if spec.key in specs:
            raise PromptRegistryError(
                f"duplicate prompt key {spec.key}: {specs[spec.key].source_file} vs {path.name}"
            )
        specs[spec.key] = spec
    if not specs:
        raise PromptRegistryError(f"no prompt templates found in {directory}")
    return specs


@lru_cache(maxsize=1)
def default_registry() -> Dict[str, PromptSpec]:
    """The shipped registry, loaded once per process.

    Cached because :func:`render` is on the hot path of every agent step; a
    released template never changes at runtime, so re-reading is pure overhead.
    Tests that need a different directory call :func:`load_registry` directly.
    """
    return load_registry(TEMPLATES_DIR)


def resolve(prompt_name: str, *, registry: Mapping[str, PromptSpec] | None = None) -> PromptSpec:
    """Address a prompt by ``name`` (newest version) or ``name@version``."""
    specs = registry if registry is not None else default_registry()
    if prompt_name in specs:
        return specs[prompt_name]
    matches = [spec for spec in specs.values() if spec.prompt_name == prompt_name]
    if not matches:
        raise PromptNotFound(
            f"no prompt named {prompt_name!r}; registered: {sorted(specs)}"
        )
    return max(matches, key=lambda spec: spec.prompt_version)


def known_prompts(*, registry: Mapping[str, PromptSpec] | None = None) -> List[PromptSpec]:
    specs = registry if registry is not None else default_registry()
    return [specs[key] for key in sorted(specs)]


def render(prompt_name: str, **variables: Any) -> str:
    """Render a registered prompt. Fail-closed: unknown name or bad variables raise."""
    return resolve(prompt_name).render(**variables)


def prompt_hash(prompt_name: str, *, registry: Mapping[str, PromptSpec] | None = None) -> str:
    return resolve(prompt_name, registry=registry).prompt_hash


# --------------------------------------------------------------------------- #
# Bundles — what a whole run used
# --------------------------------------------------------------------------- #
def prompt_bundle(
    prompt_names: Sequence[str], *, registry: Mapping[str, PromptSpec] | None = None
) -> Dict[str, Any]:
    """Resolve a set of prompts into an auditable bundle + one covering hash."""
    specs = sorted(
        (resolve(name, registry=registry) for name in prompt_names),
        key=lambda spec: spec.key,
    )
    if not specs:
        raise PromptRegistryError("empty prompt bundle")
    lines = "\n".join(f"{spec.key}:{spec.prompt_hash}" for spec in specs)
    return {
        "prompts": [
            {"name": spec.prompt_name, "version": spec.prompt_version, "prompt_hash": spec.prompt_hash}
            for spec in specs
        ],
        "prompt_hash": sha256(lines.encode("utf-8")).hexdigest(),
    }


def prompt_bundle_hash(
    prompt_names: Sequence[str], *, registry: Mapping[str, PromptSpec] | None = None
) -> str:
    """Single hash covering every prompt in ``prompt_names`` (order-insensitive).

    Order-insensitive because the bundle is a *set* of prompts, and reordering
    an import list must not look like a prompt change in the manifest.
    """
    return str(prompt_bundle(prompt_names, registry=registry)["prompt_hash"])


def bundle_for(run_kind: str, *, registry: Mapping[str, PromptSpec] | None = None) -> Dict[str, Any]:
    """The named bundle from :data:`RUN_PROMPT_BUNDLES`."""
    if run_kind not in RUN_PROMPT_BUNDLES:
        raise PromptRegistryError(
            f"unknown run kind {run_kind!r}; known: {sorted(RUN_PROMPT_BUNDLES)}"
        )
    return prompt_bundle(RUN_PROMPT_BUNDLES[run_kind], registry=registry)


# --------------------------------------------------------------------------- #
# Usage log — "which prompt, which model, which temperature" (§5 fields)
# --------------------------------------------------------------------------- #
_USAGES: List[Dict[str, Any]] = []


def record_usage(prompt_name: str, model: Optional[str] = None,
                 temperature: Optional[float] = None) -> None:
    """Note that ``prompt_name`` was used by ``model`` at ``temperature``.

    Deliberately an append-only in-process log rather than a rewrite of the
    template: the template is the *prompt*, the model binding is a property of
    the *run*, and the CARD-07 manifest is where the run's copy belongs.

    ``None`` means "this layer does not know", and the spec's declared
    ``model`` / ``temperature`` is used instead when it has one — so a prompt
    whose model is fixed statically (the daily-review polish step) is recorded
    correctly without the caller guessing.
    """
    spec = resolve(prompt_name)
    _USAGES.append(
        {
            "prompt_name": spec.prompt_name,
            "prompt_version": spec.prompt_version,
            "prompt_hash": spec.prompt_hash,
            "model": model if model is not None else spec.model,
            "temperature": temperature if temperature is not None else spec.temperature,
        }
    )


def usage_log() -> List[Dict[str, Any]]:
    return [dict(item) for item in _USAGES]


def reset_usage() -> None:
    """Clear the in-process usage log (test helper)."""
    _USAGES.clear()


# --------------------------------------------------------------------------- #
# Lock file — "版本只增不改" (§10)
# --------------------------------------------------------------------------- #
def lock_payload(*, registry: Mapping[str, PromptSpec] | None = None) -> Dict[str, Any]:
    specs = registry if registry is not None else default_registry()
    return {
        "lock_version": LOCK_VERSION,
        "note": (
            "Frozen hashes of released prompts. A change to an existing name@version "
            "is a reproducibility break: add a new version file instead, then re-run "
            "'python -m src.agents.prompts --write-lock'."
        ),
        "prompts": {
            key: {"variables": list(specs[key].variables), "prompt_hash": specs[key].prompt_hash}
            for key in sorted(specs)
        },
    }


def lock_report(
    *, registry: Mapping[str, PromptSpec] | None = None, path: Path | str = LOCK_PATH
) -> Dict[str, List[str]]:
    """Compare the live registry against the frozen lock.

    Three independent problems, reported separately so the fix is obvious:

    * ``missing``  — a frozen version disappeared (deleted or renamed);
    * ``changed``  — a frozen version's content hash moved (edited in place);
    * ``added``    — a version exists that the lock has never seen (bump it in).
    """
    specs = registry if registry is not None else default_registry()
    lock_file = Path(path)
    if not lock_file.is_file():
        return {"missing": [], "changed": [], "added": sorted(specs), "lock": ["registry.lock.json is absent"]}
    frozen = json.loads(lock_file.read_text(encoding="utf-8")).get("prompts", {})
    missing = sorted(set(frozen) - set(specs))
    added = sorted(set(specs) - set(frozen))
    changed = sorted(
        key
        for key in set(frozen) & set(specs)
        if frozen[key].get("prompt_hash") != specs[key].prompt_hash
        or sorted(frozen[key].get("variables") or []) != sorted(specs[key].variables)
    )
    return {"missing": missing, "changed": changed, "added": added, "lock": []}


def verify_lock(
    *, registry: Mapping[str, PromptSpec] | None = None, path: Path | str = LOCK_PATH
) -> None:
    """Raise unless the live registry matches the frozen lock exactly."""
    report = lock_report(registry=registry, path=path)
    problems = [f"{kind}={report[kind]}" for kind in ("lock", "missing", "changed", "added") if report[kind]]
    if problems:
        raise PromptRegistryError(
            "prompt registry does not match registry.lock.json: " + "; ".join(problems)
            + " — bump the version (new file) or re-run 'python -m src.agents.prompts --write-lock'"
        )


def write_lock(
    path: Path | str = LOCK_PATH, *, registry: Mapping[str, PromptSpec] | None = None
) -> Path:
    target = Path(path)
    target.write_text(
        json.dumps(lock_payload(registry=registry), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


# --------------------------------------------------------------------------- #
# Read-only table for the demo / CLI
# --------------------------------------------------------------------------- #
def registry_table(*, registry: Mapping[str, PromptSpec] | None = None) -> Dict[str, Any]:
    specs = registry if registry is not None else default_registry()
    bundles: List[Dict[str, Any]] = []
    for run_kind in sorted(RUN_PROMPT_BUNDLES):
        bundle = bundle_for(run_kind, registry=specs)
        bundles.append(
            {
                "run_kind": run_kind,
                "keys": [f"{item['name']}@{item['version']}" for item in bundle["prompts"]],
                "prompt_hash": bundle["prompt_hash"],
            }
        )
    return {
        "templates_dir": str(TEMPLATES_DIR),
        "count": len(specs),
        "prompts": [spec.as_row() for spec in known_prompts(registry=specs)],
        "bundles": bundles,
        "lock": lock_report(registry=specs),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Inspect the CARD-14 prompt registry.")
    parser.add_argument("--write-lock", action="store_true", help="freeze current hashes into registry.lock.json")
    parser.add_argument("--check", action="store_true", help="exit non-zero if the lock has drifted")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.write_lock:
        print(f"wrote {write_lock()}")
    table = registry_table()
    for row in table["prompts"]:
        print(f"{row['name']:<46} v{row['version']}  {row['prompt_hash'][:16]}  {row['source_file']}")
    for bundle in table["bundles"]:
        print(f"[bundle] {bundle['run_kind']:<15} {bundle['prompt_hash'][:16]}  {' + '.join(bundle['keys'])}")
    if args.check:
        problems = {key: value for key, value in table["lock"].items() if value}
        if problems:
            print(f"LOCK DRIFT: {problems}")
            return 1
        print("lock in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
