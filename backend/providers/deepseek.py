"""DeepSeek provider adapter (OpenAI-compatible)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"
    display_name = "DeepSeek"
    default_api_base = "https://api.deepseek.com"
    api_key_setting = "deepseek_api_key"
    api_base_setting = "deepseek_api_base"
    model_prefix = "deepseek"
