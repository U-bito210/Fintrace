"""Deterministic Chinese-aware tokenizer for the Retrieval Engine (CARD-06).

The BM25 index must produce identical token streams on every machine and every
run, so the index is fully reproducible offline. We therefore:

* prefer ``jieba`` (MIT) when it is importable — it is the canonical choice for
  Chinese word segmentation and is listed as a dependency of this card;
* otherwise fall back to a dependency-free, deterministic tokenizer that splits
  on whitespace/punctuation and emits CJK character bigrams. The fallback keeps
  the offline test-suite green even in an environment without ``jieba``.

Both paths are pure functions of the input string: no wall-clock, no randomness,
no network.
"""

from __future__ import annotations

import re

# Punctuation + whitespace used as hard boundaries between tokens.
_SEPARATORS = re.compile(r"\s+|[，。、；：？！“”‘’（）()\[\]【】《》<>.,;:?!\"'/\\|@#$%^&*_\-+=\n\r\t—…·]+")

# A "CJK run" is one or more consecutive Han / Kana / Hangul characters.
_CJK_RUN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def _has_jieba() -> bool:
    try:
        import jieba  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


_JIEBA_AVAILABLE = _has_jieba()


def _jieba_tokenize(text: str) -> list[str]:
    import jieba

    return [tok for tok in jieba.cut(text) if tok and not _SEPARATORS.fullmatch(tok)]


def _fallback_tokenize(text: str) -> list[str]:
    """Deterministic tokenizer with no third-party dependency.

    Latin/number runs become single lowercased tokens; whitespace/punctuation is
    dropped; each maximal CJK run is emitted as a sequence of overlapping
    character bigrams (a single CJK char yields itself). This is reproducible
    and gives BM25 a sensible term overlap signal for Chinese queries.
    """
    tokens: list[str] = []
    for segment in _SEPARATORS.split(text):
        if not segment:
            continue
        # Mixed segments (e.g. "营收123亿元") — split CJK runs from the rest.
        pos = 0
        for match in _CJK_RUN.finditer(segment):
            if match.start() > pos:
                latin = segment[pos : match.start()]
                if latin:
                    tokens.append(latin.lower())
            run = match.group(0)
            tokens.extend(_cjk_bigrams(run))
            pos = match.end()
        if pos < len(segment):
            tail = segment[pos:]
            if tail:
                tokens.append(tail.lower())
    return tokens


def _cjk_bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Segment ``text`` into lowercase tokens. Deterministic across runs."""
    if text is None:
        return []
    if _JIEBA_AVAILABLE:
        return _jieba_tokenize(text)
    return _fallback_tokenize(text)


def tokenizer_backend() -> str:
    """Which backend is active — useful for diagnostics and the degrade note."""
    return "jieba" if _JIEBA_AVAILABLE else "fallback"
