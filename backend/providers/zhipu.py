"""Zhipu GLM provider adapter (OpenAI-compatible)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class ZhipuProvider(OpenAICompatibleProvider):
    name = "zhipu"
    display_name = "智谱 GLM"
    default_api_base = "https://open.bigmodel.cn/api/paas/v4"
    api_key_setting = "zhipu_api_key"
    api_base_setting = "zhipu_api_base"
    model_prefix = "zhipu"
