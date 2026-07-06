"""Aliyun DashScope (Qwen) provider adapter (OpenAI-compatible mode)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class AliyunProvider(OpenAICompatibleProvider):
    name = "aliyun"
    display_name = "通义千问 Qwen"
    default_api_base = "https://dashscope.aliyuncs.com/compatible-mode"
    api_key_setting = "aliyun_api_key"
    api_base_setting = "aliyun_api_base"
    model_prefix = "aliyun"
