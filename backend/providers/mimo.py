"""Xiaomi MiMo provider adapter (OpenAI-compatible).

MiMo offers two billing modes:

1. **按量付费 (Pay-as-you-go)**
   - Base URL: https://api.xiaomimimo.com/v1
   - Key format: sk-xxxxx

2. **Token Plan (订阅制)**
   - Base URL: https://token-plan-cn.xiaomimimo.com/v1  (region-specific)
   - Key format: tp-xxxxx
   - Users should override the base URL with their own regional endpoint.

Both modes speak the standard OpenAI /v1/chat/completions protocol, so
this adapter reuses :class:`OpenAICompatibleProvider` directly.
"""

from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider


@ProviderRegistry.register
class MiMoProvider(OpenAICompatibleProvider):
    name = "mimo"
    display_name = "MiMo (小米)"
    default_api_base = "https://api.xiaomimimo.com/v1"
    api_key_setting = "mimo_api_key"
    api_base_setting = "mimo_api_base"
    # MiMo hosts its own model family (mimo-v2.5-pro, etc.) but also
    # supports third-party models. No fixed prefix — routing relies on
    # model_provider_map auto-populated during model discovery.
    model_prefix = ""
