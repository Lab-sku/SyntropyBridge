"""OpenAI provider adapter."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    display_name = "OpenAI"
    default_api_base = "https://api.openai.com/v1"
    api_key_setting = "openai_api_key"
    api_base_setting = "openai_api_base"
    model_prefix = "openai"
