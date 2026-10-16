"""Provider-neutral bridge from the project LLM registry to claim extraction.

The checker deliberately depends on the narrow ``ClaimExtractionClient`` protocol,
not on an SDK.  This adapter is the opt-in production bridge: it selects a model
already registered in :mod:`src.llms.config`, then exposes only ``complete`` to the
extractor.  It never receives the reference fact base and therefore cannot turn a
model answer into a financial fact.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Protocol, Tuple


class _SyncProvider(Protocol):
    """The small, testable portion of ``LLMProvider`` used by this adapter."""

    def __call__(self, messages: List[Dict[str, str]], temperature: float = 0.0) -> Tuple[str, float]: ...


ProviderFactory = Callable[[str], _SyncProvider]


def _configured_provider(model_name: str) -> _SyncProvider:
    """Construct only when live extraction was explicitly requested.

    ``LLMProvider`` validates both the registered model name and its required API
    key.  Letting that validation fail before a document is processed is safer than
    returning an empty extraction that might look like a clean report.
    """
    from src.llms.config import LLMProvider

    return LLMProvider(model_name)


class ConfiguredClaimExtractionClient:
    """Use a configured project model as a ``ClaimExtractionClient``.

    The existing provider is synchronous.  ``asyncio.to_thread`` keeps a Streamlit
    or agent event loop responsive while retaining that provider's bounded retry
    and model-registry behavior.  Cost accounting remains owned by the provider;
    this narrow bridge only returns text to the extractor.
    """

    def __init__(
        self,
        model_name: str,
        *,
        provider_factory: ProviderFactory = _configured_provider,
    ) -> None:
        selected = str(model_name or "").strip()
        if not selected:
            raise ValueError("必须指定已注册的模型名。")
        self.model_name = selected
        self._provider = provider_factory(selected)

    async def complete(self, messages: List[Dict[str, str]], temperature: float = 0.0) -> str:
        """Run one extraction call and return only the provider's text portion."""

        def _call() -> Any:
            return self._provider(messages, temperature)

        response = await asyncio.to_thread(_call)
        # ``LLMProvider`` returns ``(text, cost)``.  Keep the adapter defensive so
        # a compatible provider that returns plain text is also usable in tests.
        text = response[0] if isinstance(response, tuple) else response
        if not isinstance(text, str):
            raise TypeError("模型客户端未返回文本内容，无法执行 Claim 提取。")
        return text


__all__ = ["ConfiguredClaimExtractionClient", "ProviderFactory"]
