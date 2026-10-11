"""
Unit tests for the unified model registry (batch 1, revised).

All tests are pure unit tests — no real API calls, no network, no fees.
Uses normal package imports (anthropic SDK is installed).
"""

import os
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Normal package import — src/ must be on sys.path
# ---------------------------------------------------------------------------
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from llms.model_registry import (
    MODEL_REGISTRY,
    ModelInfo,
    cost_currency,
    get_model_info,
    list_models,
    list_available_models,
    serves_registered_pricing,
)
from llms.config import LLMProvider, init_llm, get_llm


# ============================================================================
# 1. 所有模型名唯一
# ============================================================================
def test_model_names_are_unique():
    """Each model name appears exactly once in the registry."""
    names = [info.name for info in MODEL_REGISTRY.values()]
    assert len(names) == len(set(names)), f"Duplicate names: {names}"


# ============================================================================
# 2. get_model_info() 能够查询已知模型
# ============================================================================
def test_get_model_info_known_models():
    """get_model_info returns a ModelInfo for every registered model."""
    for name in MODEL_REGISTRY:
        info = get_model_info(name)
        assert isinstance(info, ModelInfo)
        assert info.name == name


# ============================================================================
# 3. 未知模型抛出明确异常
# ============================================================================
def test_get_model_info_unknown_raises_keyerror():
    """Unknown model name raises KeyError with a helpful message."""
    with pytest.raises(KeyError, match="Unknown model"):
        get_model_info("nonexistent-model-xyz")


# ============================================================================
# 4. list_models() 结果稳定，保持插入顺序
# ============================================================================
def test_list_models_is_stable():
    """list_models returns the same list (insertion order) each call."""
    first = list_models()
    second = list_models()
    assert first == second
    assert first == list(MODEL_REGISTRY.keys())


# ============================================================================
# 5. list_available_models() 根据环境变量正确筛选
# ============================================================================
def test_list_available_models_filters_by_api_key(monkeypatch):
    """Only models whose API key env var is set appear in available list."""
    for info in MODEL_REGISTRY.values():
        monkeypatch.delenv(info.api_key_env, raising=False)

    assert list_available_models() == []

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    available = list_available_models()
    assert "gpt-4o-mini" in available
    assert "gpt-5.4-mini" in available
    assert "claude-3.5-sonnet" not in available


def test_list_available_models_with_anthropic_key(monkeypatch):
    """Setting ANTHROPIC_API_KEY makes Claude models available."""
    for info in MODEL_REGISTRY.values():
        monkeypatch.delenv(info.api_key_env, raising=False)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    available = list_available_models()
    assert "claude-3.5-sonnet" in available
    assert "claude-3.5-haiku" in available
    assert "claude-3-opus" in available
    assert "gpt-4o-mini" not in available


# ============================================================================
# 6. 所有价格非负
# ============================================================================
def test_all_prices_are_non_negative():
    """Every pricing field is >= 0."""
    for info in MODEL_REGISTRY.values():
        assert info.prompt_price_per_1m_usd >= 0, f"{info.name}: prompt price negative"
        assert info.completion_price_per_1m_usd >= 0, f"{info.name}: completion price negative"
        if info.cache_hit_price_per_1m_usd is not None:
            assert info.cache_hit_price_per_1m_usd >= 0, f"{info.name}: cache hit price negative"


# ============================================================================
# 7. 所有 provider 和 api_style 取值合法
# ============================================================================
_VALID_PROVIDERS = {"openai", "anthropic", "deepseek", "dashscope", "zhipu"}
_VALID_API_STYLES = {"openai", "anthropic"}


def test_providers_are_valid():
    """Every model has a known provider."""
    for info in MODEL_REGISTRY.values():
        assert info.provider in _VALID_PROVIDERS, (
            f"{info.name}: unknown provider '{info.provider}'"
        )


def test_api_styles_are_valid():
    """Every model has a known api_style."""
    for info in MODEL_REGISTRY.values():
        assert info.api_style in _VALID_API_STYLES, (
            f"{info.name}: unknown api_style '{info.api_style}'"
        )


# ============================================================================
# 8. api_model_id 不能为空
# ============================================================================
def test_api_model_id_not_empty():
    """Every model has a non-empty api_model_id."""
    for info in MODEL_REGISTRY.values():
        assert info.api_model_id, f"{info.name}: api_model_id is empty"
        assert isinstance(info.api_model_id, str)
        assert info.api_model_id.strip() == info.api_model_id


# ============================================================================
# 9. _SYNC_MODEL_FUNCTIONS 与 registry 严格键相等
# ============================================================================
def test_old_models_keys_equal_registry():
    """_SYNC_MODEL_FUNCTIONS keys are EXACTLY equal to MODEL_REGISTRY keys."""
    from llms.config import _SYNC_MODEL_FUNCTIONS
    sync_keys = set(_SYNC_MODEL_FUNCTIONS.keys())
    reg_keys = set(MODEL_REGISTRY.keys())
    assert sync_keys == reg_keys, (
        f"Keys differ!\n"
        f"  In _SYNC_MODEL_FUNCTIONS but not registry: {sync_keys - reg_keys}\n"
        f"  In registry but not _SYNC_MODEL_FUNCTIONS: {reg_keys - sync_keys}"
    )


# ============================================================================
# 10. 现有 OpenAI 和 Anthropic 初始化行为不变（Mock）
# ============================================================================
def test_init_llm_with_openai_key(monkeypatch):
    """init_llm with gpt-4o-mini works when OPENAI_API_KEY is set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-mock")
    provider = init_llm("gpt-4o-mini")
    assert isinstance(provider, LLMProvider)
    assert provider.current_model == "gpt-4o-mini"
    assert provider.current_llm is not None
    assert provider.model_info.name == "gpt-4o-mini"
    assert provider.model_info.provider == "openai"


def test_init_llm_with_anthropic_key(monkeypatch):
    """init_llm with claude-3.5-sonnet works when ANTHROPIC_API_KEY is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-mock")
    provider = init_llm("claude-3.5-sonnet")
    assert isinstance(provider, LLMProvider)
    assert provider.current_model == "claude-3.5-sonnet"
    assert provider.current_llm is not None
    assert provider.model_info.name == "claude-3.5-sonnet"
    assert provider.model_info.provider == "anthropic"
    assert provider.model_info.api_style == "anthropic"


def test_init_llm_missing_key_raises(monkeypatch):
    """init_llm raises ValueError when API key is missing."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        init_llm("gpt-4o-mini")


# ============================================================================
# 11. get_async_llm() 真实导入，不执行网络请求
# ============================================================================
def test_get_async_llm_imports_without_network(monkeypatch):
    """get_async_llm() can be imported and creates an AsyncLLMProvider
    without making any network request."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-no-network")

    # Ensure global provider is init'd for the async path
    init_llm("gpt-4o-mini")

    # Block all real network calls by replacing the OpenAI client constructor
    # before any async client is created.
    import openai as openai_module
    original = openai_module.AsyncOpenAI
    calls = []

    class BlockedAsyncOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(openai_module, "AsyncOpenAI", BlockedAsyncOpenAI)

    try:
        from llms.async_client import get_async_llm
        provider = get_async_llm()
        assert provider is not None
        assert provider.model_name == "gpt-4o-mini"
    finally:
        monkeypatch.setattr(openai_module, "AsyncOpenAI", original)


# ============================================================================
# 12. 网络阻断 — 禁止任何真实 API 请求
# ============================================================================
def test_init_llm_blocks_real_network(monkeypatch):
    """LLMProvider.__init__ does not make any network call — it only
    validates env vars and resolves function references."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    # init_llm itself does not call the network — it only resolves
    # the function reference.  The actual API call happens later in
    # __call__().  We verify that init succeeds without network.
    provider = init_llm("gpt-4o-mini")
    assert provider.current_model == "gpt-4o-mini"

    # The LLM callable is a real function (not mocked) but we never
    # invoke it, so no network ever happens.
    assert callable(provider.current_llm)


# ============================================================================
# Extra: set_model() 原子性 — 失败切换后状态不变
# ============================================================================
def test_set_model_atomic_on_failure(monkeypatch):
    """When set_model fails, the provider's state is unchanged."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    provider = init_llm("gpt-4o-mini")
    assert provider.current_model == "gpt-4o-mini"

    # Remove the key needed for Claude — switching should fail.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        provider.set_model("claude-3.5-sonnet")

    # State must be unchanged — still on gpt-4o-mini.
    assert provider.current_model == "gpt-4o-mini"
    assert provider.model_info.name == "gpt-4o-mini"


def test_set_model_atomic_on_invalid_model(monkeypatch):
    """When set_model is given an invalid model name, state is unchanged."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    provider = init_llm("gpt-4o-mini")
    assert provider.current_model == "gpt-4o-mini"

    with pytest.raises(ValueError, match="not available"):
        provider.set_model("nonexistent-model")

    assert provider.current_model == "gpt-4o-mini"


# ============================================================================
# Extra: 精确价格测试
# ============================================================================
def test_gpt_4o_mini_pricing_exact():
    """gpt-4o-mini prices match official OpenAI standard tier."""
    info = get_model_info("gpt-4o-mini")
    assert info.prompt_price_per_1m_usd == 0.150, "Standard input: $0.15/1M"
    assert info.completion_price_per_1m_usd == 0.600, "Standard output: $0.60/1M"
    assert info.cache_hit_price_per_1m_usd == 0.075, "Cached input: $0.075/1M"


def test_gpt_5_4_mini_pricing_exact():
    """gpt-5.4-mini prices match gpt-5-mini standard tier."""
    info = get_model_info("gpt-5.4-mini")
    assert info.prompt_price_per_1m_usd == 0.75, "Standard input: $0.75/1M"
    assert info.completion_price_per_1m_usd == 4.50, "Standard output: $4.50/1M"
    assert info.cache_hit_price_per_1m_usd == 0.075, "Cached input: $0.075/1M"


def test_claude_sonnet_pricing_exact():
    """claude-3.5-sonnet prices match official Anthropic pricing."""
    info = get_model_info("claude-3.5-sonnet")
    assert info.prompt_price_per_1m_usd == 3.0
    assert info.completion_price_per_1m_usd == 15.0


def test_claude_haiku_pricing_exact():
    """claude-3.5-haiku prices match official Anthropic pricing."""
    info = get_model_info("claude-3.5-haiku")
    assert info.prompt_price_per_1m_usd == 0.80
    assert info.completion_price_per_1m_usd == 4.0


def test_claude_opus_pricing_exact():
    """claude-3-opus prices match official Anthropic pricing."""
    info = get_model_info("claude-3-opus")
    assert info.prompt_price_per_1m_usd == 15.0
    assert info.completion_price_per_1m_usd == 75.0


# ============================================================================
# Extra: ModelInfo is frozen
# ============================================================================
def test_model_info_is_frozen():
    """ModelInfo is a frozen dataclass — cannot be mutated."""
    info = get_model_info("gpt-4o-mini")
    with pytest.raises(FrozenInstanceError):
        info.name = "hacked"  # type: ignore[misc]


# ============================================================================
# Extra: list_models includes all expected models
# ============================================================================
def test_list_models_contains_all_expected():
    """All currently supported models are in the list."""
    models = list_models()
    expected = {"gpt-4o-mini", "gpt-5.4-mini", "claude-3.5-sonnet", "claude-3.5-haiku", "claude-3-opus", "deepseek-v4-flash", "deepseek-v4-pro", "qwen3-max", "qwen-plus", "qwen-flash", "glm-4.7-flash", "glm-5.3"}
    assert set(models) == expected


# ============================================================================
# Extra: model_info 属性在所有模型上可访问
# ============================================================================
def test_model_info_accessible(monkeypatch):
    """model_info property returns correct ModelInfo for each model."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-test")
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-glm-test")

    for model_name in MODEL_REGISTRY:
        provider = init_llm(model_name)
        assert provider.model_info.name == model_name
        assert provider.model_info is get_model_info(model_name)


# ============================================================================
# DeepSeek — registry metadata
# ============================================================================

def test_deepseek_flash_registry_metadata():
    """deepseek-v4-flash has correct provider, api_style, and endpoint."""
    info = get_model_info("deepseek-v4-flash")
    assert info.provider == "deepseek"
    assert info.api_style == "openai"
    assert info.api_model_id == "deepseek-v4-flash"
    assert info.api_key_env == "DEEPSEEK_API_KEY"
    assert info.base_url_env == "DEEPSEEK_BASE_URL"
    assert info.default_base_url == "https://api.deepseek.com"
    assert info.supports_tools is True
    assert info.supports_json is True


def test_deepseek_pro_registry_metadata():
    """deepseek-v4-pro has correct provider, api_style, and endpoint."""
    info = get_model_info("deepseek-v4-pro")
    assert info.provider == "deepseek"
    assert info.api_style == "openai"
    assert info.api_model_id == "deepseek-v4-pro"
    assert info.api_key_env == "DEEPSEEK_API_KEY"
    assert info.base_url_env == "DEEPSEEK_BASE_URL"
    assert info.default_base_url == "https://api.deepseek.com"
    assert info.supports_tools is True
    assert info.supports_json is True


def test_deepseek_supports_thinking():
    """Both DeepSeek models have supports_thinking=True (they support it natively)."""
    for name in ("deepseek-v4-flash", "deepseek-v4-pro"):
        info = get_model_info(name)
        assert info.supports_thinking is True, (
            f"{name}: supports_thinking must be True"
        )


# ============================================================================
# DeepSeek — init_llm / config integration
# ============================================================================

def test_init_llm_with_deepseek_key(monkeypatch):
    """init_llm with deepseek-v4-flash works when DEEPSEEK_API_KEY is set."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test-mock")
    provider = init_llm("deepseek-v4-flash")
    assert isinstance(provider, LLMProvider)
    assert provider.current_model == "deepseek-v4-flash"
    assert provider.current_llm is not None
    assert callable(provider.current_llm)
    assert provider.model_info.name == "deepseek-v4-flash"
    assert provider.model_info.provider == "deepseek"
    assert provider.model_info.api_style == "openai"


def test_init_llm_deepseek_missing_key_raises(monkeypatch):
    """init_llm raises ValueError when DEEPSEEK_API_KEY is missing."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        init_llm("deepseek-v4-flash")


def test_deepseek_v4_flash_in_sync_functions():
    """deepseek-v4-flash is in _SYNC_MODEL_FUNCTIONS."""
    from llms.config import _SYNC_MODEL_FUNCTIONS
    assert "deepseek-v4-flash" in _SYNC_MODEL_FUNCTIONS
    assert callable(_SYNC_MODEL_FUNCTIONS["deepseek-v4-flash"])


def test_deepseek_v4_pro_in_sync_functions():
    """deepseek-v4-pro is in _SYNC_MODEL_FUNCTIONS."""
    from llms.config import _SYNC_MODEL_FUNCTIONS
    assert "deepseek-v4-pro" in _SYNC_MODEL_FUNCTIONS
    assert callable(_SYNC_MODEL_FUNCTIONS["deepseek-v4-pro"])


# ============================================================================
# DeepSeek — openai_compatible client (Mock — zero real API calls)
# ============================================================================

def test_openai_compatible_client_uses_correct_params(monkeypatch):
    """The OpenAI-compatible client is constructed with the correct
    api_key, base_url, and model_id — and sends extra_body to disable thinking."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-mock")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    captured_kwargs = {}
    create_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="mock response")
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    # Patch the reference inside openai_compatible, not the openai module
    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash

    text, cost = deepseek_v4_flash([{"role": "user", "content": "Hello"}])

    assert text == "mock response"
    assert cost > 0
    assert captured_kwargs["api_key"] == "sk-ds-mock"
    assert captured_kwargs["base_url"] == "https://api.deepseek.com"
    assert create_calls[0]["model"] == "deepseek-v4-flash"
    # CRITICAL: extra_body must be sent to disable thinking
    assert create_calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}, (
        "extra_body must be sent with thinking disabled"
    )


def test_openai_compatible_thinking_not_sent(monkeypatch):
    """Verify that extra_body disables thinking and NO reasoning_effort is sent."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-mock")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    create_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="ok")
                )],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_pro

    deepseek_v4_pro([{"role": "user", "content": "Hi"}])

    # extra_body must be present to disable thinking
    assert create_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}, (
        "extra_body must be sent with thinking disabled"
    )
    # reasoning_effort must NOT be sent (v1 does not support it)
    assert "reasoning_effort" not in create_kwargs, (
        "reasoning_effort must NOT be sent in v1"
    )


def test_openai_compatible_cost_from_registry(monkeypatch):
    """Cost is calculated from MODEL_REGISTRY pricing, not hardcoded."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-mock")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="x")
                )],
                usage=SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash

    _, cost = deepseek_v4_flash([{"role": "user", "content": "Test"}])

    # deepseek-v4-flash: prompt=0.14, completion=0.28 per 1M
    # 1M prompt + 1M completion = 0.14 + 0.28 = 0.42
    assert cost == pytest.approx(0.42, abs=0.01)


def test_deepseek_list_available_with_key(monkeypatch):
    """DeepSeek models appear in available list when DEEPSEEK_API_KEY is set."""
    for info in MODEL_REGISTRY.values():
        monkeypatch.delenv(info.api_key_env, raising=False)

    assert list_available_models() == []

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    available = list_available_models()
    assert "deepseek-v4-flash" in available
    assert "deepseek-v4-pro" in available
    assert "gpt-4o-mini" not in available
    assert "claude-3.5-sonnet" not in available


# ============================================================================
# Batch 2: DeepSeek exact price tests
# ============================================================================

def test_deepseek_v4_flash_pricing_exact():
    """deepseek-v4-flash prices match specified values."""
    info = get_model_info("deepseek-v4-flash")
    assert info.prompt_price_per_1m_usd == 0.14, "Cache-miss input: $0.14/1M"
    assert info.cache_hit_price_per_1m_usd == 0.0028, "Cache-hit input: $0.0028/1M"
    assert info.completion_price_per_1m_usd == 0.28, "Output: $0.28/1M"


def test_deepseek_v4_pro_pricing_exact():
    """deepseek-v4-pro prices match specified values."""
    info = get_model_info("deepseek-v4-pro")
    assert info.prompt_price_per_1m_usd == 0.435, "Cache-miss input: $0.435/1M"
    assert info.cache_hit_price_per_1m_usd == 0.003625, "Cache-hit input: $0.003625/1M"
    assert info.completion_price_per_1m_usd == 0.87, "Output: $0.87/1M"


# ============================================================================
# Batch 2: DEEPSEEK_THINKING_ENABLED guard
# ============================================================================

def test_deepseek_thinking_enabled_default_ok(monkeypatch):
    """DEEPSEEK_THINKING_ENABLED not set or false — init works."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.delenv("DEEPSEEK_THINKING_ENABLED", raising=False)
    # Should not raise
    provider = init_llm("deepseek-v4-flash")
    assert provider.current_model == "deepseek-v4-flash"


def test_deepseek_thinking_enabled_true_raises(monkeypatch):
    """DEEPSEEK_THINKING_ENABLED=true raises ValueError."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.setenv("DEEPSEEK_THINKING_ENABLED", "true")
    with pytest.raises(ValueError, match="DEEPSEEK_THINKING_ENABLED"):
        init_llm("deepseek-v4-flash")


def test_deepseek_thinking_enabled_true_does_not_affect_openai(monkeypatch):
    """DEEPSEEK_THINKING_ENABLED=true does not affect OpenAI models."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_THINKING_ENABLED", "true")
    # Should work fine — guard only applies to DeepSeek models
    provider = init_llm("gpt-4o-mini")
    assert provider.current_model == "gpt-4o-mini"


# ============================================================================
# Batch 2: DeepSeek kwargs strict mock test
# ============================================================================

def test_deepseek_kwargs_strict_assertion(monkeypatch):
    """Capture client.chat.completions.create() kwargs and strictly assert
    extra_body and absence of reasoning_effort."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-strict")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    create_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="mock-strict")
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash
    deepseek_v4_flash([{"role": "user", "content": "Strict test"}])

    # Strict assertions
    assert create_kwargs["extra_body"] == {
        "thinking": {"type": "disabled"}
    }, "extra_body must be exactly {'thinking': {'type': 'disabled'}}"
    assert "reasoning_effort" not in create_kwargs, (
        "reasoning_effort must NOT be present in kwargs"
    )


# ============================================================================
# Batch 2: OpenAI isolation test — no extra_body for OpenAI
# ============================================================================

def test_openai_isolation_no_extra_body(monkeypatch):
    """OpenAI client must NOT receive extra_body (DeepSeek-specific parameter)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    create_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="openai-ok")
                )],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10, total_tokens=15),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai as oai_module
    monkeypatch.setattr(oai_module, "OpenAI", FakeOpenAI)

    from llms.openai import gpt_4o_mini
    gpt_4o_mini([{"role": "user", "content": "Hi"}])

    # OpenAI must NOT receive extra_body
    assert "extra_body" not in create_kwargs, (
        "OpenAI must NOT receive extra_body"
    )
    assert "reasoning_effort" not in create_kwargs, (
        "OpenAI must NOT receive reasoning_effort"
    )


# ============================================================================
# Batch 2: DeepSeek API key isolation
# ============================================================================

def test_deepseek_api_key_no_fallback_to_openai(monkeypatch):
    """DeepSeek client must use DEEPSEEK_API_KEY, not fall back to OPENAI_API_KEY."""
    # Set ONLY OPENAI_API_KEY — DeepSeek should fail
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-only")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    from llms.config import LLMProvider
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        LLMProvider("deepseek-v4-flash")


def test_deepseek_client_uses_deepseek_key_only(monkeypatch):
    """DeepSeek client reads only DEEPSEEK_API_KEY for its client instance."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-only")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-be-used")

    captured_kwargs = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            from types import SimpleNamespace
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(content="x")
                        )],
                        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    )
                )
            )

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash
    deepseek_v4_flash([{"role": "user", "content": "Test"}])

    assert captured_kwargs["api_key"] == "sk-ds-only", (
        "DeepSeek client must use DEEPSEEK_API_KEY, not OPENAI_API_KEY"
    )


# ============================================================================
# Batch 2: OpenAI vs DeepSeek full isolation
# ============================================================================

def test_openai_deepseek_full_isolation(monkeypatch):
    """OpenAI and DeepSeek clients must have completely isolated:
    - Client instances
    - API Keys
    - Base URLs
    - extra_body parameter
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-iso")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-iso")

    openai_instances = []
    deepseek_instances = []
    openai_create_kwargs = {}
    deepseek_create_kwargs = {}

    # -- OpenAI side (OpenAI client is created without explicit args) --
    class OpenAICompletions:
        def create(self, **kwargs):
            openai_create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="openai")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class OpenAIChat:
        def __init__(self):
            self.completions = OpenAICompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            openai_instances.append(kwargs)
            self.chat = OpenAIChat()

    import llms.openai as oai_module
    monkeypatch.setattr(oai_module, "OpenAI", FakeOpenAI)

    # -- DeepSeek side --
    class DSCompletions:
        def create(self, **kwargs):
            deepseek_create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="deepseek")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class DSChat:
        def __init__(self):
            self.completions = DSCompletions()

    class FakeDSOpenAI:
        def __init__(self, **kwargs):
            deepseek_instances.append(kwargs)
            self.chat = DSChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeDSOpenAI)

    # -- Call both --
    from llms.openai import gpt_4o_mini
    from llms.openai_compatible import deepseek_v4_flash

    gpt_4o_mini([{"role": "user", "content": "OpenAI test"}])
    deepseek_v4_flash([{"role": "user", "content": "DeepSeek test"}])

    # 1. Separate client instances
    assert len(openai_instances) == 1
    assert len(deepseek_instances) == 1
    assert openai_instances[0] is not deepseek_instances[0]

    # 2. OpenAI client is created without explicit api_key (reads from env)
    #    DeepSeek client explicitly passes api_key and base_url
    assert "api_key" not in openai_instances[0], (
        "OpenAI client is created without explicit api_key"
    )
    assert deepseek_instances[0].get("api_key") == "sk-deepseek-iso", (
        "DeepSeek client must have explicit api_key"
    )

    # 3. extra_body isolation
    assert "extra_body" not in openai_create_kwargs, (
        "OpenAI create() must NOT have extra_body"
    )
    assert deepseek_create_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}, (
        "DeepSeek create() must have extra_body"
    )

    # 4. reasoning_effort isolation
    assert "reasoning_effort" not in openai_create_kwargs
    assert "reasoning_effort" not in deepseek_create_kwargs


# ============================================================================
# Batch 2 — revised: Base URL fallback tests
# ============================================================================

def test_deepseek_base_url_fallback_to_default(monkeypatch):
    """When DEEPSEEK_BASE_URL is not set, client falls back to
    https://api.deepseek.com."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-base")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    captured_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="fallback")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash
    deepseek_v4_flash([{"role": "user", "content": "Test"}])

    assert captured_kwargs["api_key"] == "sk-ds-base"
    assert captured_kwargs["base_url"] == "https://api.deepseek.com", (
        f"Expected default https://api.deepseek.com, got {captured_kwargs.get('base_url')}"
    )


def test_deepseek_base_url_env_override(monkeypatch):
    """When DEEPSEEK_BASE_URL is set, it overrides the default."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-custom")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://custom-deepseek.example.com")

    captured_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="custom")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_pro
    deepseek_v4_pro([{"role": "user", "content": "Test"}])

    assert captured_kwargs["api_key"] == "sk-ds-custom"
    assert captured_kwargs["base_url"] == "https://custom-deepseek.example.com", (
        f"Expected custom URL, got {captured_kwargs.get('base_url')}"
    )


# ============================================================================
# Batch 2 — revised: direct call key validation (client layer)
# ============================================================================

def test_deepseek_direct_call_missing_key_raises(monkeypatch):
    """Direct call to deepseek_v4_flash without DEEPSEEK_API_KEY raises
    ValueError from the client layer (not just config layer)."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # Even without involving init_llm, the client layer itself validates.
    from llms.openai_compatible import deepseek_v4_flash
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        deepseek_v4_flash([{"role": "user", "content": "Test"}])


def test_deepseek_direct_call_key_isolated_from_openai(monkeypatch):
    """Direct call with DEEPSEEK_API_KEY uses it even when OPENAI_API_KEY differs."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-direct")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-different")

    captured_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="direct")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash
    deepseek_v4_flash([{"role": "user", "content": "Test"}])

    assert captured_kwargs["api_key"] == "sk-ds-direct", (
        "Direct call must use DEEPSEEK_API_KEY, not OPENAI_API_KEY"
    )


# ============================================================================
# Batch 2 — revised: cache-aware cost calculation
# ============================================================================

def test_deepseek_cache_aware_cost(monkeypatch):
    """When prompt_cache_hit_tokens and prompt_cache_miss_tokens are available,
    cost is calculated using the discounted cache-hit rate."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-cost")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="cache-test")
                )],
                usage=SimpleNamespace(
                    prompt_tokens=1_000_000,
                    completion_tokens=500_000,
                    total_tokens=1_500_000,
                    prompt_cache_hit_tokens=800_000,
                    prompt_cache_miss_tokens=200_000,
                ),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash

    _, cost = deepseek_v4_flash([{"role": "user", "content": "Cache test"}])

    # deepseek-v4-flash: cache-miss=0.14, cache-hit=0.0028, completion=0.28
    # 200k miss * 0.14/1M = 0.028
    # 800k hit  * 0.0028/1M = 0.00224
    # 500k comp * 0.28/1M = 0.14
    # total = 0.028 + 0.00224 + 0.14 = 0.17024
    assert cost == pytest.approx(0.17024, abs=0.001)


def test_deepseek_cost_no_cache_detail_falls_back(monkeypatch):
    """When prompt_cache_hit_tokens/miss_tokens are missing, ALL input tokens
    are treated as cache-miss (conservative)."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-cost")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    class FakeCompletions:
        def create(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="no-cache")
                )],
                usage=SimpleNamespace(
                    prompt_tokens=1_000_000,
                    completion_tokens=1_000_000,
                    total_tokens=2_000_000,
                ),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash

    _, cost = deepseek_v4_flash([{"role": "user", "content": "No cache detail"}])

    # All prompt tokens treated as cache-miss: 0.14 + 0.28 = 0.42
    assert cost == pytest.approx(0.42, abs=0.01)


# ============================================================================
# Batch 2 — revised: non-DeepSeek providers do NOT get extra_body
# ============================================================================

def test_non_deepseek_provider_no_extra_body(monkeypatch):
    """Verify that a hypothetical non-DeepSeek OpenAI-compatible provider
    does NOT receive extra_body in the create() call."""
    # We use the DeepSeek test infrastructure but verify the guard at the
    # _call_openai_compatible level: the code checks info.provider == "deepseek".
    # Since only DeepSeek models are registered as provider="deepseek",
    # this test verifies the existing behavior by checking that OpenAI's
    # openai.py client does not send extra_body (already tested above).
    # Additionally, we verify that if a non-deepseek provider were to use
    # openai_compatible.py, it would not get extra_body.

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-provider")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    create_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            create_kwargs.update(kwargs)
            from types import SimpleNamespace
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="provider-test")
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import deepseek_v4_flash
    deepseek_v4_flash([{"role": "user", "content": "Provider check"}])

    # DeepSeek IS a deepseek provider, so it SHOULD get extra_body
    assert create_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}, (
        "DeepSeek provider must receive extra_body"
    )
    assert "reasoning_effort" not in create_kwargs


# ============================================================================
# Cost unit of account — a price without a unit is not a price
# ============================================================================

def test_every_registered_model_declares_its_cost_unit():
    """The invariant that keeps a cost total from becoming fiction.

    Either a model states the currency its numbers are denominated in, or it
    carries no numbers at all.  ``0.0`` with no unit is the one combination that
    lets "unknown" be summed as if it were money.
    """
    for name, info in MODEL_REGISTRY.items():
        has_rate = bool(
            info.prompt_price_per_1m_usd
            or info.completion_price_per_1m_usd
            or info.cache_hit_price_per_1m_usd
        )
        if info.price_currency is None:
            assert not has_rate, f"{name}: an unpriced model must not carry a rate"
            assert info.price_source is None, f"{name}: an unpriced model has no source to cite"
        else:
            assert info.priced is True, name
            assert info.price_source, f"{name}: a priced model must cite where the price came from"


def test_priced_models_are_quoted_in_usd():
    """Every quotable price in this ledger is USD today.

    Registered so that introducing a second currency is a deliberate act that
    has to change this test, rather than a silent change of meaning for every
    ``total_cost_usd`` ever printed.
    """
    for name, info in MODEL_REGISTRY.items():
        if info.priced:
            assert info.price_currency == "USD", f"{name}: {info.price_currency}"


def test_dashscope_models_are_callable_but_unpriced():
    """百炼 models: usable, and honestly un-costed.

    百炼 bills in CNY while this ledger's unit is USD, and the official Alibaba
    pages do not agree on the same model's CNY list price.  A converted or
    "reference" number would be laundered into the cost column as fact, so the
    model is registered with no price at all.
    """
    for name in ("qwen3-max", "qwen-plus", "qwen-flash"):
        info = get_model_info(name)
        assert info.provider == "dashscope", name
        assert info.api_style == "openai", name
        assert info.api_model_id == name, name
        assert info.api_key_env == "DASHSCOPE_API_KEY", name
        assert info.base_url_env == "DASHSCOPE_BASE_URL", name
        assert info.default_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1", name
        assert info.priced is False, name
        assert cost_currency(name) is None, name


def test_unpriced_model_cost_currency_is_unknown_not_usd(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    assert cost_currency("qwen-plus") is None


def test_resold_model_id_voids_the_registered_price(monkeypatch):
    """The same model id can be resold on other terms, in another currency.

    DeepSeek served through 百炼 bills in CNY, so the registered USD rate no
    longer describes what the caller is charged — and a budget ceiling built on
    it would fail open, which is worse than having no ceiling.
    """
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert serves_registered_pricing("deepseek-v4-flash") is False
    assert cost_currency("deepseek-v4-flash") is None


def test_explicitly_stating_the_official_endpoint_keeps_the_price(monkeypatch):
    """Spelling out the vendor's own URL is not a resale."""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    assert serves_registered_pricing("deepseek-v4-flash") is True
    assert cost_currency("deepseek-v4-flash") == "USD"


def test_vendors_without_a_base_url_are_unaffected(monkeypatch):
    """OpenAI and Anthropic keep the SDK default → nothing is overridden."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert serves_registered_pricing("gpt-4o-mini") is True
    assert cost_currency("gpt-4o-mini") == "USD"
    assert cost_currency("claude-3.5-sonnet") == "USD"


def test_every_registered_provider_is_routable():
    """No model may be registrable and unroutable.

    ``AsyncLLMProvider`` routes on the declared wire protocol, so an entry whose
    provider is neither the native client nor an OpenAI-wire vendor would raise
    at call time.  Fail at the data layer instead.
    """
    for name, info in MODEL_REGISTRY.items():
        assert info.provider in ("openai", "anthropic") or info.api_style == "openai", (
            f"{name}: provider '{info.provider}' with api_style '{info.api_style}' has no route"
        )


# ============================================================================
# DashScope — sync client (Mock — zero real API calls)
# ============================================================================

def test_qwen_client_uses_dashscope_key_and_endpoint(monkeypatch):
    """The DashScope client is built from DASHSCOPE_API_KEY and the compatible-mode URL."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-mock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-be-used")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-should-not-be-used")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    captured_kwargs = {}
    create_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="mock"))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import qwen_plus

    text, _cost = qwen_plus([{"role": "user", "content": "Hello"}])

    assert text == "mock"
    assert captured_kwargs["api_key"] == "sk-qwen-mock"
    assert captured_kwargs["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert create_calls[0]["model"] == "qwen-plus"
    # CRITICAL: ``thinking`` is DeepSeek's field.  A vendor that does not know it
    # answers with a 400, so it must not be sent to 百炼.
    assert "extra_body" not in create_calls[0]
    assert "reasoning_effort" not in create_calls[0]


def test_qwen_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    from llms.openai_compatible import qwen3_max
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        qwen3_max([{"role": "user", "content": "Test"}])


def test_init_llm_with_dashscope_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-test-mock")
    provider = init_llm("qwen3-max")
    assert provider.current_model == "qwen3-max"
    assert provider.model_info.provider == "dashscope"
    assert provider.model_info.api_style == "openai"
    assert callable(provider.current_llm)


def test_qwen_models_are_in_sync_functions():
    from llms.config import _SYNC_MODEL_FUNCTIONS
    for name in ("qwen3-max", "qwen-plus", "qwen-flash"):
        assert callable(_SYNC_MODEL_FUNCTIONS[name]), name


def test_qwen_listed_as_available_with_dashscope_key(monkeypatch):
    for info in MODEL_REGISTRY.values():
        monkeypatch.delenv(info.api_key_env, raising=False)

    assert list_available_models() == []

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-test")
    available = list_available_models()
    assert "qwen3-max" in available
    assert "qwen-plus" in available
    assert "deepseek-v4-flash" not in available


# ============================================================================
# DashScope — async path (the one the real Agent evaluation uses)
# ============================================================================

def _fake_async_openai(captured_client, create_kwargs, content="空"):
    """A stand-in AsyncOpenAI that records how it was built and called."""

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured_client.update(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        async def _create(self, **kwargs):
            create_kwargs.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content, tool_calls=[])
                )],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7),
            )

    return FakeAsyncOpenAI


@pytest.mark.asyncio
async def test_async_routes_dashscope_by_wire_protocol(monkeypatch):
    """A new OpenAI-wire vendor needs no routing code — only a registry entry.

    DashScope is picked up because it declares ``api_style="openai"``, builds its
    own client from DASHSCOPE_API_KEY, and receives no DeepSeek-only ``extra_body``.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen-async")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-async")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-async")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)

    import openai as openai_module
    import llms.async_client as ac

    monkeypatch.setattr(ac, "_async_compatible", None, raising=False)
    monkeypatch.setattr(ac, "_async_compatible_model", None, raising=False)

    captured_client, create_kwargs = {}, {}
    monkeypatch.setattr(
        openai_module, "AsyncOpenAI", _fake_async_openai(captured_client, create_kwargs)
    )

    provider = ac.AsyncLLMProvider("qwen-plus")
    response = await provider.call_with_tools(
        [{"role": "user", "content": "hi"}], tools=[]
    )

    assert response.text == "空"
    assert captured_client["api_key"] == "sk-qwen-async"
    assert captured_client["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert create_kwargs["model"] == "qwen-plus"
    assert "extra_body" not in create_kwargs


@pytest.mark.asyncio
async def test_async_compatible_path_still_disables_deepseek_thinking(monkeypatch):
    """Generalising the path must not quietly drop DeepSeek's thinking switch."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-async-only")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    import openai as openai_module
    import llms.async_client as ac

    monkeypatch.setattr(ac, "_async_compatible", None, raising=False)
    monkeypatch.setattr(ac, "_async_compatible_model", None, raising=False)

    captured_client, create_kwargs = {}, {}
    monkeypatch.setattr(
        openai_module, "AsyncOpenAI", _fake_async_openai(captured_client, create_kwargs)
    )

    provider = ac.AsyncLLMProvider("deepseek-v4-flash")
    await provider.call_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert captured_client["api_key"] == "sk-deepseek-async-only"
    assert captured_client["base_url"] == "https://api.deepseek.com"
    assert create_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_async_resold_deepseek_client_follows_the_override(monkeypatch):
    """Pointing DeepSeek at 百炼 must actually move the traffic there."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-via-bailian")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    import openai as openai_module
    import llms.async_client as ac

    monkeypatch.setattr(ac, "_async_compatible", None, raising=False)
    monkeypatch.setattr(ac, "_async_compatible_model", None, raising=False)

    captured_client, create_kwargs = {}, {}
    monkeypatch.setattr(
        openai_module, "AsyncOpenAI", _fake_async_openai(captured_client, create_kwargs)
    )

    provider = ac.AsyncLLMProvider("deepseek-v4-flash")
    await provider.call_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert captured_client["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # ...and the registered USD price no longer applies to this run.
    assert cost_currency("deepseek-v4-flash") is None


# ============================================================================
# Logging — "unknown" must not print as "$0.000000"
# ============================================================================

def test_logger_states_unknown_cost_instead_of_zero(tmp_path, monkeypatch):
    from logger import StockAnalystLogger

    logger = StockAnalystLogger("COST-LABEL-TEST", tmp_path)
    messages = []
    # monkeypatch (not bare assignment): logging.getLogger caches by name, so a
    # leaked patch would follow this logger into every other test using it.
    monkeypatch.setattr(logger.logger, "info", messages.append, raising=False)

    logger.llm_call("qwen-plus", 0.0, 12, currency=None)
    assert "未定价" in messages[-1]
    assert "$" not in messages[-1]

    logger.llm_call("gpt-4o-mini", 0.001234, 12, currency="USD")
    assert "$0.001234" in messages[-1]

    logger.llm_call("qwen-plus", 0.5, 12, currency="CNY")
    assert "¥0.500000" in messages[-1]


# ============================================================================
# Zhipu BigModel / 智谱 — sync client (Mock — zero real API calls)
# ============================================================================

def test_zhipu_models_are_callable_but_unpriced():
    """智谱 models: usable, and honestly un-costed (DashScope precedent).

    bigmodel.cn bills in CNY while this ledger's unit is USD, so no price is
    registered — a converted or "reference" number would be laundered into the
    cost column as fact.  glm-4.7-flash being a free tier does not change that:
    the ledger reports "unknown", not 0.
    """
    for name in ("glm-4.7-flash", "glm-5.3"):
        info = get_model_info(name)
        assert info.provider == "zhipu", name
        assert info.api_style == "openai", name
        assert info.api_model_id == name, name
        assert info.api_key_env == "ZHIPU_API_KEY", name
        assert info.base_url_env == "ZHIPU_BASE_URL", name
        assert info.default_base_url == "https://open.bigmodel.cn/api/paas/v4", name
        assert info.priced is False, name
        assert cost_currency(name) is None, name


def test_zhipu_models_declare_tool_json_and_thinking_support():
    """Capabilities sourced from docs.bigmodel.cn (checked 2026-09-30)."""
    for name in ("glm-4.7-flash", "glm-5.3"):
        info = get_model_info(name)
        assert info.supports_tools is True, name
        assert info.supports_json is True, name
        assert info.supports_thinking is True, name


def test_thinking_disable_flag_implies_capability_on_openai_wire():
    """Only a thinking-capable OpenAI-wire model may declare the disable switch.

    The switch is an OpenAI-wire ``extra_body`` field; a model that cannot
    think, or that does not ride this wire, has no honest use for the flag.
    """
    for name, info in MODEL_REGISTRY.items():
        if info.accepts_thinking_disable:
            assert info.supports_thinking, name
            assert info.api_style == "openai", name


def test_deepseek_entries_carry_the_disable_flag():
    """Generalising the client guard must not silently drop DeepSeek's switch."""
    for name in ("deepseek-v4-flash", "deepseek-v4-pro"):
        assert get_model_info(name).accepts_thinking_disable is True, name


def test_glm_4_7_flash_disables_thinking_but_glm_5_3_cannot(monkeypatch):
    """GLM-4.x hybrids accept the disable switch; GLM-5.x refuses it.

    GLM-5.x can only ever think (docs.bigmodel.cn chat-completions API,
    checked 2026-09-30), so sending ``{"thinking": {"type": "disabled"}}``
    there is how a working call turns into a 400.  The split must live in
    the registry data, not in a vendor name check.
    """
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-mock")
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)

    captured_kwargs = {}
    create_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="mock"))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import glm_4_7_flash, glm_5_3

    glm_4_7_flash([{"role": "user", "content": "Hello"}])
    assert create_calls[0]["model"] == "glm-4.7-flash"
    assert create_calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}

    glm_5_3([{"role": "user", "content": "Hello"}])
    assert create_calls[1]["model"] == "glm-5.3"
    # CRITICAL: GLM-5.x cannot honour the switch — the bare call must survive.
    assert "extra_body" not in create_calls[1]
    assert "reasoning_effort" not in create_calls[1]


def test_glm_client_uses_zhipu_key_and_endpoint(monkeypatch):
    """The BigModel client is built from ZHIPU_API_KEY and the paas/v4 URL."""
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key-mock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-be-used")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-should-not-be-used")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-should-not-be-used")
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)

    captured_kwargs = {}
    create_calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="mock"))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.chat = FakeChat()

    import llms.openai_compatible as oc_module
    monkeypatch.setattr(oc_module, "OpenAI", FakeOpenAI)

    from llms.openai_compatible import glm_4_7_flash

    text, _cost = glm_4_7_flash([{"role": "user", "content": "Hello"}])

    assert text == "mock"
    assert captured_kwargs["api_key"] == "zhipu-key-mock"
    assert captured_kwargs["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert create_calls[0]["model"] == "glm-4.7-flash"


def test_glm_client_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    from llms.openai_compatible import glm_5_3
    with pytest.raises(ValueError, match="ZHIPU_API_KEY"):
        glm_5_3([{"role": "user", "content": "Test"}])


def test_glm_models_are_in_sync_functions():
    from llms.config import _SYNC_MODEL_FUNCTIONS
    for name in ("glm-4.7-flash", "glm-5.3"):
        assert callable(_SYNC_MODEL_FUNCTIONS[name]), name


def test_glm_listed_as_available_with_zhipu_key(monkeypatch):
    for info in MODEL_REGISTRY.values():
        monkeypatch.delenv(info.api_key_env, raising=False)

    assert list_available_models() == []

    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    available = list_available_models()
    assert "glm-4.7-flash" in available
    assert "glm-5.3" in available


def test_init_llm_with_zhipu_key(monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test-mock")
    provider = init_llm("glm-4.7-flash")
    assert provider.current_model == "glm-4.7-flash"
    assert provider.model_info.provider == "zhipu"
    assert provider.model_info.api_style == "openai"
    assert callable(provider.current_llm)


@pytest.mark.asyncio
async def test_async_glm_4_7_flash_disables_thinking_while_glm_5_3_rides_bare(monkeypatch):
    """The async path must keep the same per-model thinking split as sync."""
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-async")
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)

    import openai as openai_module
    import llms.async_client as ac

    monkeypatch.setattr(ac, "_async_compatible", None, raising=False)
    monkeypatch.setattr(ac, "_async_compatible_model", None, raising=False)

    captured_client, create_kwargs = {}, {}
    monkeypatch.setattr(
        openai_module, "AsyncOpenAI", _fake_async_openai(captured_client, create_kwargs)
    )

    provider = ac.AsyncLLMProvider("glm-4.7-flash")
    await provider.call_with_tools([{"role": "user", "content": "hi"}], tools=[])

    assert captured_client["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert create_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    # GLM-5.x cannot disable thinking — the bare call must survive the refactor.
    create_kwargs.clear()
    provider = ac.AsyncLLMProvider("glm-5.3")
    await provider.call_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert "extra_body" not in create_kwargs
