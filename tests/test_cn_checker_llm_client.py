"""Offline contract tests for the real-model Claim extraction bridge."""

from __future__ import annotations

import asyncio

import pytest

from src.cn.checker.llm_client import ConfiguredClaimExtractionClient


class _Provider:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, messages, temperature=0.0):
        self.calls.append((messages, temperature))
        return ('{"claims": []}', 0.001)


def test_bridge_exposes_only_text_and_records_selected_model():
    provider = _Provider()
    client = ConfiguredClaimExtractionClient("qwen-plus", provider_factory=lambda name: provider)

    result = asyncio.run(client.complete([{"role": "user", "content": "[]"}], temperature=0.0))

    assert client.model_name == "qwen-plus"
    assert result == '{"claims": []}'
    assert provider.calls == [([{"role": "user", "content": "[]"}], 0.0)]


def test_bridge_requires_an_explicit_model_name_before_calling_any_provider():
    with pytest.raises(ValueError, match="模型名"):
        ConfiguredClaimExtractionClient("", provider_factory=lambda name: _Provider())


def test_bridge_rejects_non_text_provider_results():
    class BadProvider:
        def __call__(self, messages, temperature=0.0):
            return ({"claims": []}, 0.0)

    client = ConfiguredClaimExtractionClient("qwen-plus", provider_factory=lambda name: BadProvider())
    with pytest.raises(TypeError, match="文本"):
        asyncio.run(client.complete([]))
