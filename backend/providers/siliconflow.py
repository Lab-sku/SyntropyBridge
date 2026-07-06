"""SiliconFlow (硅基流动) provider adapter (OpenAI-compatible).

API docs: https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions
Base URL: https://api.siliconflow.cn/v1
Key format: sk-xxxxx
"""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class SiliconFlowProvider(OpenAICompatibleProvider):
    name = "siliconflow"
    display_name = "SiliconFlow (硅基流动)"
    default_api_base = "https://api.siliconflow.cn/v1"
    api_key_setting = "siliconflow_api_key"
    api_base_setting = "siliconflow_api_base"
    # No model_prefix — SiliconFlow hosts models from many vendors
    # (Qwen, DeepSeek, GLM, etc.) under their original namespaces.
    # Routing is handled via model_provider_map auto-populated during
    # model discovery rather than a fixed prefix.
    model_prefix = ""
