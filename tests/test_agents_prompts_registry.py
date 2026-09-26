"""CARD-14 · the prompt registry's own contract.

The registry is the one place an LLM prompt may live, so its failure modes matter
more than its happy path: a prompt that renders half-substituted, or a released
version that silently changes, breaks the reproducibility the manifest claims.
These tests pin the fail-closed behaviour (§9), the variable rules (§8 edge 2),
`(name, version)` addressing (§8 edge 1) and the lock's "版本只增不改" gate (§10).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.prompts import (
    LOCK_PATH,
    RUN_PROMPT_BUNDLES,
    PromptNotFound,
    PromptRegistryError,
    PromptTemplateError,
    PromptVariableError,
    default_registry,
    load_registry,
    lock_report,
    prompt_bundle_hash,
    record_usage,
    registry_table,
    render,
    resolve,
    usage_log,
    verify_lock,
    write_lock,
)


def _write_template(directory: Path, name: str, version: int, body: str,
                    variables: tuple = (), extra: tuple = ()) -> Path:
    front = [
        f"name: {name}",
        f"version: {version}",
        f"variables: {json.dumps(list(variables))}",
        *extra,
    ]
    path = directory / f"{name}.{version}.md"
    path.write_text("---\n" + "\n".join(front) + "\n---\n\n" + body + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def test_the_shipped_registry_loads_with_unique_keys_and_a_source_file():
    registry = load_registry()
    assert registry, "the shipped registry must not be empty"
    for key, spec in registry.items():
        assert key == spec.key
        assert spec.prompt_version >= 1
        assert len(spec.prompt_hash) == 64
        assert spec.source_file and spec.source_file.endswith(".md")
        assert spec.created_at, f"{key} must record when it was released"


def test_a_missing_template_directory_is_fail_closed(tmp_path):
    with pytest.raises(PromptRegistryError):
        load_registry(tmp_path / "not-there")


def test_an_empty_template_directory_is_fail_closed(tmp_path):
    """An empty registry would silently mean 'no prompt' — the failure this module exists to stop."""
    with pytest.raises(PromptRegistryError):
        load_registry(tmp_path)


def test_a_template_without_frontmatter_is_rejected(tmp_path):
    (tmp_path / "bare.1.md").write_text("just text\n", encoding="utf-8")
    with pytest.raises(PromptTemplateError):
        load_registry(tmp_path)


def test_filename_and_frontmatter_must_agree(tmp_path):
    (tmp_path / "actual.1.md").write_text(
        "---\nname: claimed\nversion: 1\nvariables: []\n---\n\nbody\n", encoding="utf-8"
    )
    with pytest.raises(PromptTemplateError) as excinfo:
        load_registry(tmp_path)
    assert "disagrees with the filename" in str(excinfo.value)


def test_an_undeclared_placeholder_is_rejected_at_load_time(tmp_path):
    """Declared-vs-present mismatch is caught while loading, not on first render."""
    _write_template(tmp_path, "p", 1, "hello {{who}}", variables=())
    with pytest.raises(PromptTemplateError) as excinfo:
        load_registry(tmp_path)
    assert "declared variables" in str(excinfo.value)


def test_a_declared_but_unused_variable_is_rejected(tmp_path):
    _write_template(tmp_path, "p", 1, "hello world", variables=("who",))
    with pytest.raises(PromptTemplateError):
        load_registry(tmp_path)


def test_duplicate_name_and_version_is_not_reachable_through_filenames(tmp_path):
    """The filename *is* the key, so two files cannot claim one ``name@version``."""
    _write_template(tmp_path, "p", 1, "one")
    assert sorted(load_registry(tmp_path)) == ["p@1"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_substitutes_declared_variables():
    text = render("cn_research_agent_run_context", cutoff="2026-08-12T10:45:00+08:00")
    assert "2026-08-12T10:45:00+08:00" in text
    assert "{{" not in text


def test_render_rejects_a_missing_variable():
    with pytest.raises(PromptVariableError) as excinfo:
        render("cn_research_agent_run_context")
    assert "missing=" in str(excinfo.value)


def test_render_rejects_an_extra_variable():
    with pytest.raises(PromptVariableError) as excinfo:
        render("cn_research_agent_run_context", cutoff="x", surplus="y")
    assert "unexpected=" in str(excinfo.value)


def test_an_unknown_prompt_name_is_not_found_rather_than_empty():
    with pytest.raises(PromptNotFound):
        render("cn_does_not_exist")


def test_a_value_containing_a_placeholder_is_rejected_not_rendered_twice(tmp_path):
    """Half-rendering is worse than no prompt: catch the double-substitution case."""
    _write_template(tmp_path, "p", 1, "say {{word}}", variables=("word",))
    spec = load_registry(tmp_path)["p@1"]
    with pytest.raises(PromptTemplateError):
        spec.render(word="{{word}}")


def test_braces_in_the_body_that_are_not_placeholders_survive_verbatim():
    """The memo prompt embeds a JSON schema; single braces must pass straight through."""
    text = render(
        "cn_memo_section_narrator",
        section_name="investment_thesis",
        whitelist="[]",
        analysis="{}",
        valuation="{}",
    )
    assert '{"sentence_span": str, "claim_id": str|null' in text


# --------------------------------------------------------------------------- #
# Hashing / addressing
# --------------------------------------------------------------------------- #
def test_hash_is_content_addressed_and_whitespace_sensitive(tmp_path):
    one_dir, two_dir = tmp_path / "one", tmp_path / "two"
    one_dir.mkdir(), two_dir.mkdir()
    _write_template(one_dir, "p", 1, "line one\nline two")
    _write_template(two_dir, "p", 1, "line one\nline  two")
    one = load_registry(one_dir)["p@1"]
    two = load_registry(two_dir)["p@1"]
    assert one.prompt_hash != two.prompt_hash

    three_dir = tmp_path / "three"
    three_dir.mkdir()
    _write_template(three_dir, "p", 1, "line one\nline two")
    assert load_registry(three_dir)["p@1"].prompt_hash == one.prompt_hash


def test_two_versions_coexist_and_the_newest_is_the_default(tmp_path):
    _write_template(tmp_path, "p", 1, "first")
    _write_template(tmp_path, "p", 2, "second")
    registry = load_registry(tmp_path)
    assert sorted(registry) == ["p@1", "p@2"]
    assert resolve("p", registry=registry).prompt_version == 2
    assert resolve("p@1", registry=registry).template == "first"


def test_trailing_and_leading_newline_flags_round_trip(tmp_path):
    tail, head = tmp_path / "tail", tmp_path / "head"
    tail.mkdir(), head.mkdir()
    _write_template(tail, "p", 1, "body", extra=("trailing_newline: true",))
    assert load_registry(tail)["p@1"].template == "body\n"
    _write_template(head, "p", 1, "body", extra=("leading_newline: true",))
    assert load_registry(head)["p@1"].template == "\nbody"
    _write_template(head, "q", 1, "body")
    assert load_registry(head)["q@1"].template == "body"


def test_the_file_newline_is_packaging_not_content(tmp_path):
    """The blank line after `---` and the file's final newline never reach the prompt."""
    path = tmp_path / "p.1.md"
    path.write_text("---\nname: p\nversion: 1\nvariables: []\n---\n\nbody\n", encoding="utf-8")
    assert load_registry(tmp_path)["p@1"].template == "body"


# --------------------------------------------------------------------------- #
# Bundles / usage log
# --------------------------------------------------------------------------- #
def test_a_bundle_covers_every_prompt_and_ignores_order():
    names = list(RUN_PROMPT_BUNDLES["research"])
    assert prompt_bundle_hash(names) == prompt_bundle_hash(list(reversed(names)))
    assert prompt_bundle_hash(names) != prompt_bundle_hash(names[:1])


def test_record_usage_logs_the_prompt_and_falls_back_to_a_declared_model():
    record_usage("cn_daily_review_theme_polish_system")
    rows = usage_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["prompt_name"] == "cn_daily_review_theme_polish_system"
    assert row["prompt_hash"] == resolve("cn_daily_review_theme_polish_system").prompt_hash
    # Declared in the template's frontmatter: the caller does not have to know it.
    assert row["model"] == "deepseek-v4-flash"
    assert row["temperature"] == 0.3


def test_record_usage_never_invents_a_model_that_is_not_declared():
    record_usage("cn_research_agent_base")
    assert usage_log()[0]["model"] is None


def test_record_usage_rejects_an_unregistered_prompt():
    with pytest.raises(PromptNotFound):
        record_usage("cn_not_a_prompt")


def test_the_table_exposes_every_prompt_and_bundle():
    table = registry_table()
    assert table["count"] == len(default_registry())
    assert {row["name"] for row in table["prompts"]} >= set(RUN_PROMPT_BUNDLES["research"])
    assert {row["run_kind"] for row in table["bundles"]} == set(RUN_PROMPT_BUNDLES)
    for bundle in table["bundles"]:
        assert len(bundle["prompt_hash"]) == 64


# --------------------------------------------------------------------------- #
# Lock: 版本只增不改
# --------------------------------------------------------------------------- #
def test_the_shipped_lock_matches_the_shipped_registry():
    """The gate itself: a released prompt may only be added to, never edited."""
    verify_lock()
    report = lock_report()
    assert not any(report[key] for key in ("lock", "missing", "changed", "added"))


def test_lock_report_flags_an_edit_without_a_version_bump(tmp_path):
    _write_template(tmp_path, "p", 1, "original")
    lock = tmp_path / "lock.json"
    write_lock(lock, registry=load_registry(tmp_path))
    _write_template(tmp_path, "p", 1, "edited in place")
    report = lock_report(registry=load_registry(tmp_path), path=lock)
    assert report["changed"] == ["p@1"]
    with pytest.raises(PromptRegistryError, match="changed"):
        verify_lock(registry=load_registry(tmp_path), path=lock)


def test_lock_report_flags_a_deleted_version(tmp_path):
    _write_template(tmp_path, "p", 1, "one")
    _write_template(tmp_path, "p", 2, "two")
    lock = tmp_path / "lock.json"
    write_lock(lock, registry=load_registry(tmp_path))
    (tmp_path / "p.1.md").unlink()
    report = lock_report(registry=load_registry(tmp_path), path=lock)
    assert report["missing"] == ["p@1"]
    assert report["added"] == []


def test_lock_report_flags_a_new_version_that_was_never_frozen(tmp_path):
    _write_template(tmp_path, "p", 1, "one")
    lock = tmp_path / "lock.json"
    write_lock(lock, registry=load_registry(tmp_path))
    _write_template(tmp_path, "p", 2, "two")
    report = lock_report(registry=load_registry(tmp_path), path=lock)
    assert report["added"] == ["p@2"]


def test_a_missing_lock_file_is_reported_rather_than_treated_as_clean(tmp_path):
    _write_template(tmp_path, "p", 1, "one")
    report = lock_report(registry=load_registry(tmp_path), path=tmp_path / "absent.json")
    assert report["lock"] and report["added"] == ["p@1"]


def test_write_lock_is_round_trip_stable(tmp_path):
    _write_template(tmp_path, "p", 1, "one")
    lock = tmp_path / "lock.json"
    write_lock(lock, registry=load_registry(tmp_path))
    first = lock.read_text(encoding="utf-8")
    write_lock(lock, registry=load_registry(tmp_path))
    assert lock.read_text(encoding="utf-8") == first
    assert LOCK_PATH.is_file()


def test_the_lock_note_explains_the_remedy():
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert "add a new version file" in payload["note"]
