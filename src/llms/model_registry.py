"""
Unified model registry — single source of truth for all LLM model metadata.

Design rules
------------
* **No imports from openai.py, claude.py, or config.py.**  This module is a pure
  data layer and must never cause a circular dependency.
* **Frozen dataclass.**  ``ModelInfo`` is immutable — all pricing, provider, and
  protocol metadata is set at registration time and never mutated at runtime.
* **provider ≠ api_style.**  ``provider`` identifies the vendor (openai, anthropic,
  deepseek, dashscope, …); ``api_style`` tells the Agent which *message format* to
  use ("openai" or "anthropic").  DeepSeek uses ``provider="deepseek"`` with
  ``api_style="openai"`` because it speaks the OpenAI wire protocol; Alibaba's
  百炼/DashScope works the same way.
* **api_model_id** is the concrete model string sent to the API.  For OpenAI this
  is usually the same as ``name``; for Anthropic it is the dated snapshot
  (e.g. ``"claude-3-5-sonnet-20241022"``).
* **A price carries its own unit of account.**  ``price_currency`` states the
  currency the three price fields are quoted in; cost calculation multiplies
  token counts by those rates and produces an amount **in that currency**.
  ``price_currency=None`` means *unpriced*: the model is callable, but no
  verifiable price exists in a unit we can state, so its cost is **unknown —
  never zero**.  Converting a CNY list price at a hand-picked exchange rate, or
  storing a "reference" number, is forbidden: ``None`` is the honest value.
* **list_models / list_available_models preserve insertion order** (the order
  models appear in ``MODEL_REGISTRY``).  Callers that need sorted output should
  sort explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelInfo:
    """Immutable metadata for a single registered LLM model.

    Every field that influences runtime behaviour (API key, pricing, tool
    support) is captured here so that both the sync ``LLMProvider`` and the
    async ``AsyncLLMProvider`` read from the same source.
    """

    # -- Identity ---------------------------------------------------------------
    name: str
    """Logical name users select, e.g. ``"gpt-4o-mini"``."""

    provider: str
    """Vendor: ``"openai"`` | ``"anthropic"`` | ``"deepseek"`` | …"""

    api_style: str
    """Wire protocol: ``"openai"`` or ``"anthropic"``.

    Determines message format, tool-calling shape, and Agent transcript
    construction.  DeepSeek uses ``api_style="openai"``."""

    api_model_id: str
    """Concrete model string sent to the API, e.g. ``"claude-3-5-sonnet-20241022"``."""

    # -- Auth / endpoint --------------------------------------------------------
    api_key_env: str
    """Environment variable that holds the API key, e.g. ``"OPENAI_API_KEY"``."""

    base_url_env: Optional[str] = None
    """Env var for a custom base URL (OpenAI-compatible providers).  ``None``
    means use the SDK default."""

    default_base_url: Optional[str] = None
    """Fallback base URL when ``base_url_env`` is not set.  ``None`` for
    official OpenAI / Anthropic."""

    # -- Capabilities -----------------------------------------------------------
    supports_tools: bool = True
    """Native tool / function calling support."""

    supports_json: bool = False
    """``response_format={"type": "json_object"}`` support."""

    supports_thinking: bool = False
    """Whether the model can emit a reasoning / thinking block."""

    # -- Pricing (per 1M tokens, quoted in ``price_currency``) ------------------
    prompt_price_per_1m_usd: float = 0.0
    """Input / prompt price per 1M tokens, in ``price_currency``."""

    completion_price_per_1m_usd: float = 0.0
    """Output / completion price per 1M tokens, in ``price_currency``."""

    cache_hit_price_per_1m_usd: Optional[float] = None
    """Discounted input price when a cache hit occurs (DeepSeek).  ``None``
    means the provider does not expose cache-hit pricing."""

    price_currency: Optional[str] = "USD"
    """Unit of account for the three price fields above.

    ``"USD"`` when the vendor publishes a USD list price that can be cited.
    ``None`` means the model is registered but **unpriced** — see the module
    docstring.  The numeric fields keep their historical ``_usd`` suffix, and
    that suffix is load-bearing: a model whose price is not known in USD must
    be registered here with ``price_currency=None``, never with a guessed or
    converted number.
    """

    price_source: Optional[str] = None
    """Where the price came from, e.g. ``"platform.openai.com/docs/pricing"``.

    ``None`` when ``price_currency`` is ``None`` (there is no price to source).
    Recorded as data rather than a code comment so the ledger can state its own
    provenance years later.
    """

    @property
    def priced(self) -> bool:
        """``True`` when a price exists in a unit of account we can state.

        ``False`` means cost is *unknown*, not free: consumers must report it as
        unknown rather than adding ``0.0`` to a running total.
        """
        return self.price_currency is not None


# ============================================================================
# Registry (single source of truth)
# Order is deliberate — list_models() preserves insertion order.
# ============================================================================

MODEL_REGISTRY: Dict[str, ModelInfo] = {
    # -- OpenAI ----------------------------------------------------------------
    # gpt-4o-mini: Standard-tier pricing, source: platform.openai.com/docs/pricing
    "gpt-4o-mini": ModelInfo(
        name="gpt-4o-mini",
        provider="openai",
        api_style="openai",
        api_model_id="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        supports_tools=True,
        supports_json=True,
        supports_thinking=False,
        prompt_price_per_1m_usd=0.150,
        completion_price_per_1m_usd=0.600,
        cache_hit_price_per_1m_usd=0.075,
        price_source="platform.openai.com/docs/pricing",
    ),
    "gpt-5.4-mini": ModelInfo(
        name="gpt-5.4-mini",
        provider="openai",
        api_style="openai",
        api_model_id="gpt-5.4-mini",
        api_key_env="OPENAI_API_KEY",
        supports_tools=True,
        supports_json=True,
        supports_thinking=False,
        prompt_price_per_1m_usd=0.75,
        completion_price_per_1m_usd=4.50,
        cache_hit_price_per_1m_usd=0.075,
        price_source="platform.openai.com/docs/pricing",
    ),
    # -- Anthropic -------------------------------------------------------------
    "claude-3.5-sonnet": ModelInfo(
        name="claude-3.5-sonnet",
        provider="anthropic",
        api_style="anthropic",
        api_model_id="claude-3-5-sonnet-20241022",
        api_key_env="ANTHROPIC_API_KEY",
        supports_tools=True,
        supports_json=False,
        supports_thinking=False,
        prompt_price_per_1m_usd=3.0,
        completion_price_per_1m_usd=15.0,
        price_source="anthropic.com/pricing",
    ),
    "claude-3.5-haiku": ModelInfo(
        name="claude-3.5-haiku",
        provider="anthropic",
        api_style="anthropic",
        api_model_id="claude-3-5-haiku-20241022",
        api_key_env="ANTHROPIC_API_KEY",
        supports_tools=True,
        supports_json=False,
        supports_thinking=False,
        prompt_price_per_1m_usd=0.80,
        completion_price_per_1m_usd=4.0,
        price_source="anthropic.com/pricing",
    ),
    "claude-3-opus": ModelInfo(
        name="claude-3-opus",
        provider="anthropic",
        api_style="anthropic",
        api_model_id="claude-3-opus-20240229",
        api_key_env="ANTHROPIC_API_KEY",
        supports_tools=True,
        supports_json=False,
        supports_thinking=False,
        prompt_price_per_1m_usd=15.0,
        completion_price_per_1m_usd=75.0,
        price_source="anthropic.com/pricing",
    ),
    # -- DeepSeek (OpenAI-compatible) -------------------------------------------
    "deepseek-v4-flash": ModelInfo(
        name="deepseek-v4-flash",
        provider="deepseek",
        api_style="openai",
        api_model_id="deepseek-v4-flash",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
        supports_tools=True,
        supports_json=True,
        supports_thinking=True,
        prompt_price_per_1m_usd=0.14,
        completion_price_per_1m_usd=0.28,
        cache_hit_price_per_1m_usd=0.0028,
        price_source="api-docs.deepseek.com/quick_start/pricing",
    ),
    "deepseek-v4-pro": ModelInfo(
        name="deepseek-v4-pro",
        provider="deepseek",
        api_style="openai",
        api_model_id="deepseek-v4-pro",
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
        supports_tools=True,
        supports_json=True,
        supports_thinking=True,
        prompt_price_per_1m_usd=0.435,
        completion_price_per_1m_usd=0.87,
        cache_hit_price_per_1m_usd=0.003625,
        price_source="api-docs.deepseek.com/quick_start/pricing",
    ),
    # -- Alibaba Cloud Model Studio (百炼 / DashScope, OpenAI-compatible) --------
    # Deliberately registered as UNPRICED.  百炼 bills in CNY, and this ledger's
    # unit is USD, so a price here would need either an exchange rate we would
    # have to invent or a CNY number we would have to trust.  As of 2026-09-17
    # the official Alibaba pages disagree with each other on the CNY list price
    # of the same model ID (see e.g. help.aliyun.com/zh/model-studio/models,
    # aliyun.com/price/advantage), and the tiering by input length makes any
    # single number wrong for most requests anyway.  So: callable, unpriced,
    # cost reported as unknown.  Registering a real price later is a data edit
    # here — nothing else has to change.
    "qwen3-max": ModelInfo(
        name="qwen3-max",
        provider="dashscope",
        api_style="openai",
        api_model_id="qwen3-max",
        api_key_env="DASHSCOPE_API_KEY",
        base_url_env="DASHSCOPE_BASE_URL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        supports_tools=True,
        supports_json=True,
        supports_thinking=True,  # capability only — we never send a thinking switch
        price_currency=None,
    ),
    "qwen-plus": ModelInfo(
        name="qwen-plus",
        provider="dashscope",
        api_style="openai",
        api_model_id="qwen-plus",
        api_key_env="DASHSCOPE_API_KEY",
        base_url_env="DASHSCOPE_BASE_URL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        supports_tools=True,
        supports_json=True,
        supports_thinking=True,
        price_currency=None,
    ),
    "qwen-flash": ModelInfo(
        name="qwen-flash",
        provider="dashscope",
        api_style="openai",
        api_model_id="qwen-flash",
        api_key_env="DASHSCOPE_API_KEY",
        base_url_env="DASHSCOPE_BASE_URL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        supports_tools=True,
        supports_json=True,
        supports_thinking=True,
        price_currency=None,
    ),
}


# ============================================================================
# Query helpers
# ============================================================================


def get_model_info(model_name: str) -> ModelInfo:
    """Return the ``ModelInfo`` for *model_name*.

    Raises:
        KeyError:  *model_name* is not registered.
    """
    if model_name not in MODEL_REGISTRY:
        raise KeyError(
            f"Unknown model '{model_name}'. "
            f"Registered models: {list(MODEL_REGISTRY.keys())}"
        )
    return MODEL_REGISTRY[model_name]


def list_models() -> List[str]:
    """Return all registered model names in insertion order."""
    return list(MODEL_REGISTRY.keys())


def list_available_models() -> List[str]:
    """Return models whose required API key is set in the environment.

    Preserves insertion order of ``MODEL_REGISTRY``.
    """
    available: List[str] = []
    for name, info in MODEL_REGISTRY.items():
        if os.getenv(info.api_key_env):
            available.append(name)
    return available


# ============================================================================
# Cost unit of account
# ============================================================================


def effective_base_url(info: ModelInfo) -> Optional[str]:
    """The endpoint actually in force: env override first, else the default."""
    if info.base_url_env:
        override = os.getenv(info.base_url_env)
        if override:
            return override
    return info.default_base_url


def _endpoint_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return urlsplit(url).netloc.lower() or None


def serves_registered_pricing(model_name: str) -> bool:
    """Whether the endpoint in force is the one the registered price belongs to.

    Overriding ``base_url_env`` (百炼, a gateway, a proxy) voids the price: the
    same model *id* can be resold by another vendor on other terms and in
    another currency — DeepSeek served through Alibaba Model Studio bills in
    CNY, so the registered USD rate no longer describes what you are charged.
    A budget ceiling built on that number fails open, which is worse than
    having no ceiling at all.
    """
    info = get_model_info(model_name)
    return _endpoint_host(effective_base_url(info)) == _endpoint_host(info.default_base_url)


def cost_currency(model_name: str) -> Optional[str]:
    """Unit of account for *model_name*'s cost, or ``None`` when it is unknown.

    ``None`` means the cost must be reported as unknown.  It is **not** zero and
    it is **not** USD-by-default: an unknown unit of account is exactly the
    condition under which a fabricated total becomes indistinguishable from a
    real one.
    """
    info = get_model_info(model_name)
    if not info.priced:
        return None
    if not serves_registered_pricing(model_name):
        return None
    return info.price_currency
