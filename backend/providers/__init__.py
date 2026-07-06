"""Provider plugin system for multi-platform AI API proxy.

This package provides a unified interface to multiple AI providers including
OpenAI, Anthropic, Google Gemini, DeepSeek, Moonshot, Zhipu, Aliyun DashScope,
ByteDance Ark, OpenRouter, NVIDIA NIM, and MiniMax.
"""

# Import all provider modules so they self-register with the registry.
from backend.providers import (  # noqa: F401
    aliyun,
    anthropic,
    deepseek,
    doubao,
    google,
    minimax,
    mimo,
    moonshot,
    nvidia,
    openai,
    openrouter,
    siliconflow,
    zhipu,
)
from backend.providers.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ModelInfo,
    Provider,
    ProviderCapability,
    ProviderError,
    ProviderRegistry,
    StreamChunk,
    detect_provider_from_model,
    get_provider,
    list_providers,
)

__all__ = [
    "Provider",
    "ProviderCapability",
    "ProviderError",
    "ModelInfo",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "StreamChunk",
    "ProviderRegistry",
    "get_provider",
    "list_providers",
    "detect_provider_from_model",
]
