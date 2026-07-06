"""ByteDance Ark (Doubao) provider adapter (OpenAI-compatible)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class DoubaoProvider(OpenAICompatibleProvider):
    name = "doubao"
    display_name = "字节豆包 Doubao"
    default_api_base = "https://ark.cn-beijing.volces.com/api/v3"
    api_key_setting = "doubao_api_key"
    api_base_setting = "doubao_api_base"
    model_prefix = "doubao"
