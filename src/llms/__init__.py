"""
LLMs package - Multi-provider LLM integration for stock analysis.

This package provides unified interfaces for different LLM providers:
- OpenAI (GPT-4o, GPT-4o-mini, GPT-4, GPT-3.5-turbo)
- Anthropic Claude (Claude-3.5-Sonnet, Claude-3.5-Haiku, Claude-3-Opus)

All functions follow the same interface:
- Input: List of messages, temperature
- Output: Tuple of (response_text, cost)
- Cost unit: whatever ``ModelInfo.price_currency`` says.  It is ``"USD"`` for
  every currently priced model, and ``None`` for an unpriced model (百炼/qwen),
  where the cost is unknown rather than zero.  Ask
  ``model_registry.cost_currency(name)`` before presenting a cost as money.
- Error handling: Retry logic with exponential backoff
- Logging: Integrated with the stock analyst logger

Usage:
    # Direct model usage
    from llms import gpt_4o_mini, claude_3_5_sonnet
    
    # Configurable usage
    from llms import get_llm, set_default_llm
    set_default_llm("claude-3.5-sonnet")
    llm = get_llm()  # Returns claude_3_5_sonnet
"""

# OpenAI models
from .openai import gpt_4o_mini, calculate_cost as calculate_openai_cost

# Anthropic Claude models  
from .claude import (
    claude_3_5_sonnet,
    claude_3_5_haiku, 
    claude_3_opus,
    calculate_cost as calculate_claude_cost
)

# OpenAI-compatible providers (DeepSeek, Alibaba Cloud Model Studio / 百炼, Zhipu BigModel, etc.)
from .openai_compatible import (
    deepseek_v4_flash,
    deepseek_v4_pro,
    qwen3_max,
    qwen_plus,
    qwen_flash,
    glm_4_7_flash,
    glm_5_3,
)

# Configuration and utilities
from .config import (
    init_llm,
    get_llm,
    list_models
)

# Default model aliases for easy switching
default_model = gpt_4o_mini  # Fast and cost-effective default
premium_model = claude_3_5_sonnet  # High-quality for complex tasks
budget_model = claude_3_5_haiku  # Fastest and cheapest
flagship_model = claude_3_opus  # Most capable for complex reasoning

__all__ = [
    # OpenAI
    'gpt_4o_mini',
    'calculate_openai_cost',
    
    # Claude
    'claude_3_5_sonnet',
    'claude_3_5_haiku', 
    'claude_3_opus',
    'calculate_claude_cost',
    
    # DeepSeek (OpenAI-compatible)
    'deepseek_v4_flash',
    'deepseek_v4_pro',

    # Alibaba Cloud Model Studio / 百炼 (OpenAI-compatible, unpriced)
    'qwen3_max',
    'qwen_plus',
    'qwen_flash',

    # Zhipu BigModel / 智谱 (OpenAI-compatible, unpriced)
    'glm_4_7_flash',
    'glm_5_3',

    # Configuration
    'init_llm',
    'get_llm',
    'list_models',
]