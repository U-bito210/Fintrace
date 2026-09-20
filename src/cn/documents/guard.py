"""Controlled Environment Guard for the Document Intelligence Layer.

Uploaded PDF/DOC/TXT are *untrusted data*. The rules below enforce the
competition's "real agent, real run" safety requirements and are merged into
CARD-01 (not a separate card):

1. Document Content Isolation — document text is DATA, never an INSTRUCTION.
2. Prompt Injection Guard — heuristic scan flags suspicious spans at ingest.
3. File Access Allowlist / Path Traversal Protection — only workspace dirs.
4. External Network Policy — offline by default; documents trigger no network.
5. Tool Permission Minimization — documents cannot coerce prompt/env leaks.

All checks are fail-closed: a rejected path raises, a suspected injection is
recorded (not silently dropped), and an offline network attempt is blocked.
"""

from __future__ import annotations

import os
import re
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import FACT_KIND_TEXT


# ---------------------------------------------------------------------------
# File access allowlist / path traversal protection
# ---------------------------------------------------------------------------

# Default workspace roots a document may be read from / written under.
# data/, output/ and runs/ are all git-ignored (see .gitignore) so secrets and
# real financial files never enter version control.
DEFAULT_ALLOW_ROOTS = ("data", "output", "runs")


class PathTraversalError(PermissionError):
    """A file path escaped the workspace allowlist or used traversal."""


def _project_root() -> Path:
    # src/cn/documents/guard.py -> project root (3 levels up: documents/cn/src).
    return Path(__file__).resolve().parents[3]


def resolve_allow_roots(roots: Optional[Iterable[str | Path]] = None) -> List[Path]:
    """Resolve allowlist roots to absolute paths under the project root."""
    base = _project_root()
    if roots is None:
        roots = DEFAULT_ALLOW_ROOTS
    resolved: List[Path] = []
    for raw in roots:
        path = Path(raw)
        if not path.is_absolute():
            path = (base / path).resolve()
        else:
            path = path.resolve()
        resolved.append(path)
    return resolved


def is_path_allowed(path: str | Path, allow_roots: Optional[Iterable[Path]] = None) -> bool:
    """True iff ``path`` resolves inside one of the allowlist roots."""
    roots = resolve_allow_roots(allow_roots)
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def require_allowed_path(path: str | Path, allow_roots: Optional[Iterable[Path]] = None) -> Path:
    """Resolve a document path and enforce the allowlist.

    Rejects:
      * ``../`` traversal that escapes the workspace,
      * absolute paths outside the allowlist,
      * symlink escapes (resolves through the link, then re-checks).
    Returns the resolved absolute path for safe use by the loader.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (_project_root() / candidate)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise PathTraversalError(f"无法解析路径: {path!r} ({exc})") from exc
    roots = resolve_allow_roots(allow_roots)
    inside = any(_safe_relative_to(resolved, root) for root in roots)
    if not inside:
        raise PathTraversalError(
            f"路径越权被拒绝: {str(resolved)!r} 不在允许目录 {[str(r) for r in roots]}"
        )
    return resolved


def _safe_relative_to(target: Path, root: Path) -> bool:
    """Like Path.relative_to but also defeats symlink escapes."""
    try:
        target.relative_to(root)
    except ValueError:
        return False
    # A symlinked ancestor could still point outside root; re-check the real path.
    try:
        if target.is_symlink():
            return _safe_relative_to(Path(os.path.realpath(target)), root)
    except (OSError, RuntimeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Prompt injection guard (heuristic)
# ---------------------------------------------------------------------------

# Patterns that *may* indicate the document is trying to act as instructions.
# These are detection heuristics only — a hit never changes system behaviour,
# it is recorded for audit and downstream tools (CARD-05/07) to consume.
#
# ``severity`` separates genuine injection attempts ("injection", which sets the
# ``suspected_injection`` flag) from "audit" signals that are normal in real
# filings — a company website / 巨潮资讯网 link appears in most annual reports, so
# URL presence alone must NOT flag a document as injected.
_INJECTION_PATTERNS: List[Tuple[str, str, re.Pattern[str]]] = [
    ("zh_ignore_previous", "injection", re.compile(r"忽略(之前|以上|前述|上面的|先前).{0,6}(规则|指令|要求|提示|prompt)", re.IGNORECASE)),
    ("zh_disregard", "injection", re.compile(r"(无视| disregar|不要理睬).{0,6}(规则|指令|系统)", re.IGNORECASE)),
    ("zh_reveal_prompt", "injection", re.compile(r"(泄露|输出|打印|展示).{0,6}(系统\s*prompt|system\s*prompt|你的提示词|你的指令)", re.IGNORECASE)),
    ("zh_new_instruction", "injection", re.compile(r"你(现在|必须|应该|要).{0,8}(执行|扮演|假装|以.{0,4}身份)", re.IGNORECASE)),
    ("en_ignore_previous", "injection", re.compile(r"ignore (all |any |the )?(previous|above|prior|preceding) (instructions|prompt|rules)", re.IGNORECASE)),
    ("en_system_prompt", "injection", re.compile(r"(reveal|print|output|disclose).{0,6}(system prompt|your (instructions|prompt))", re.IGNORECASE)),
    ("en_jailbreak", "injection", re.compile(r"(you are now|act as|pretend to be|disregard (all|any)).{0,4}(instruction|rule|guideline)", re.IGNORECASE)),
    ("url_present", "audit", re.compile(r"https?://[^\s]+", re.IGNORECASE)),  # recorded, never injection by itself
]

SEVERITY_INJECTION = "injection"
SEVERITY_AUDIT = "audit"


def scan_injection(text: str) -> List[dict]:
    """Scan a text span for injection / audit indicators.

    Returns a list of ``{"rule", "severity", "snippet", "start", "end"}`` hits.
    Never raises on text; an empty list means no heuristic matched.
    """
    hits: List[dict] = []
    if not text:
        return hits
    for rule, severity, pattern in _INJECTION_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            hits.append({
                "rule": rule,
                "severity": severity,
                "snippet": snippet[:200],
                "start": match.start(),
                "end": match.end(),
            })
    return hits


def _segments(hits: List[dict]) -> List[str]:
    segments: List[str] = []
    for hit in hits:
        seg = hit["snippet"]
        if seg not in segments:
            segments.append(seg)
    return segments


def scan_injection_flag(text: str) -> Tuple[bool, List[str]]:
    """Convenience: ``(suspected, segments)`` counting *injection*-severity hits only.

    Audit-only hits (e.g. a plain URL) are intentionally excluded so ordinary
    filings are not mislabelled; use :func:`scan_audit_flags` to collect them.
    """
    hits = [h for h in scan_injection(text) if h["severity"] == SEVERITY_INJECTION]
    return (len(hits) > 0, _segments(hits))


def scan_audit_flags(text: str) -> List[str]:
    """Audit-only signals (external links etc.) recorded without blocking."""
    hits = [h for h in scan_injection(text) if h["severity"] == SEVERITY_AUDIT]
    return _segments(hits)



# ---------------------------------------------------------------------------
# Document content isolation
# ---------------------------------------------------------------------------

def classify_content_role(text: str) -> str:
    """A document fragment is always DATA, never an INSTRUCTION.

    Downstream tools (Claim Extractor CARD-05, Retrieval CARD-06) call this and
    must inject the text only through the data boundary. Even if the fragment
    matches an injection pattern, the role stays ``data`` — the system behaviour
    is unchanged.
    """
    return "data"


def sanitize_for_prompt(text: str) -> str:
    """Neutralize instruction-like phrasing before the text is shown to a model.

    We do NOT delete the content (it remains auditable); we just fence it so a
    careless model cannot treat it as a directive. The original ``raw_text`` in
    ``ExtractedFact`` is preserved verbatim and never overwritten.
    """
    if not text:
        return text
    fenced = text.replace("```", "'''")
    return f"[DOCUMENT_DATA][render_as_text_only]{fenced}[/DOCUMENT_DATA]"


# ---------------------------------------------------------------------------
# External network policy (offline by default)
# ---------------------------------------------------------------------------

class NetworkPolicyViolation(RuntimeError):
    """Raised when a network call is attempted while offline mode is enforced."""


def offline_mode_enabled() -> bool:
    """Offline mode is the default; opt out with FINTRACE_CN_ALLOW_NETWORK=1."""
    import os as _os
    return _os.environ.get("FINTRACE_CN_ALLOW_NETWORK", "0").strip() not in ("1", "true", "True")


_original_socket = socket.socket


class _GuardedSocket(socket.socket):
    """A socket that refuses to connect while offline mode is active."""

    def connect(self, address):  # type: ignore[override]
        if offline_mode_enabled():
            raise NetworkPolicyViolation(
                f"离线模式禁止网络访问，已阻断对 {address} 的连接（文档摄入不得触发外网）。"
            )
        return super().connect(address)

    def create_connection(self, *args, **kwargs):  # type: ignore[override]
        if offline_mode_enabled():
            raise NetworkPolicyViolation("离线模式禁止网络访问（create_connection 被阻断）。")
        return super().create_connection(*args, **kwargs)


@contextmanager
def OfflineGuard():
    """Context manager that blocks socket connections for its duration.

    Used by documents, ingest and the offline test to guarantee no document
    triggers a network call. Restores the original socket on exit.
    """
    saved = socket.socket
    socket.socket = _GuardedSocket  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = saved  # type: ignore[assignment]


@contextmanager
def offline_guard():
    """Alias of :func:`OfflineGuard` for readability at call sites."""
    with OfflineGuard():
        yield
