"""NVIDIA NIM provider adapter (OpenAI-compatible)."""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class NvidiaProvider(OpenAICompatibleProvider):
    name = "nvidia"
    display_name = "NVIDIA NIM"
    default_api_base = "https://integrate.api.nvidia.com/v1"
    api_key_setting = "nvidia_api_key"
    api_base_setting = "nvidia_api_base"
    model_prefix = "nvidia"
