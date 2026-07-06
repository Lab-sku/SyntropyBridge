"""OpenRouter provider adapter (OpenAI-compatible, aggregate models)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    display_name = "OpenRouter (模型聚合)"
    default_api_base = "https://openrouter.ai/api/v1"
    api_key_setting = "openrouter_api_key"
    api_base_setting = "openrouter_api_base"
    model_prefix = "openrouter"
