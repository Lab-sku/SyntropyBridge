"""Moonshot Kimi provider adapter (OpenAI-compatible)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class MoonshotProvider(OpenAICompatibleProvider):
    name = "moonshot"
    display_name = "Moonshot Kimi"
    default_api_base = "https://api.moonshot.cn"
    api_key_setting = "moonshot_api_key"
    api_base_setting = "moonshot_api_base"
    model_prefix = "moonshot"
