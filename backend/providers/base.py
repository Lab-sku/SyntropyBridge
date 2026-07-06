"""Provider abstraction layer for multi-platform AI APIs.

Defines the base Provider class and registry. Each concrete provider
(OpenAI, Anthropic, etc.) implements this interface so the proxy can
route requests uniformly.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Flag
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


def _setting_or_empty(name: str) -> str:
    """Best-effort read of a setting key; never raises.

    Routing helpers must not blow up the caller when the settings
    table is missing (e.g. in early test contexts) — return an empty
    string so the caller can treat the provider as unconfigured.
    """
    try:
        from backend.database import get_setting

        return get_setting(name) or ""
    except Exception:
        return ""


class ProviderError(Exception):
    """Raised when a provider call fails."""

    def __init__(self, message: str, status_code: int = 500, provider: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider


class ProviderCapability(Flag):
    """Capabilities a provider can support."""

    CHAT = 1
    STREAMING = 2
    FUNCTION_CALLING = 4
    VISION = 8
    EMBEDDINGS = 16
    IMAGE_GENERATION = 32
    AUDIO = 64
    TOOLS = 128


@dataclass
class ModelInfo:
    """Metadata about a model available from a provider."""

    id: str
    display_name: str
    context_length: int = 0
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    capabilities: ProviderCapability = ProviderCapability.CHAT


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ChatRequest:
    model: str
    messages: List[ChatMessage]
    max_tokens: int = 2048
    temperature: float = 0.7
    stream: bool = False
    top_p: float = 1.0
    system: Optional[str] = None
    stop: Optional[List[str]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    content: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str = "stop"
    raw: Optional[Dict[str, Any]] = None


class StreamChunk:
    """A single piece of a streaming chat response.

    The proxy service normalizes these chunks from all providers, so the
    rest of the system only has to deal with a single shape.
    """

    def __init__(
        self,
        content: str = "",
        finish_reason: str = "",
        model: str = "",
        usage: Optional[Dict[str, int]] = None,
    ):
        self.content = content
        self.finish_reason = finish_reason
        self.model = model
        self.usage = usage or {}


class Provider(abc.ABC):
    """Abstract base class for AI provider adapters."""

    name: str = ""
    display_name: str = ""
    default_api_base: str = ""
    api_key_setting: str = ""
    api_base_setting: str = ""
    capabilities: ProviderCapability = ProviderCapability.CHAT
    requires_api_key: bool = True
    model_prefix: str = ""

    def __init__(self, api_key: str = "", api_base: str = ""):
        self.api_key = api_key
        self.api_base = api_base or self.default_api_base

    @abc.abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Send a chat completion request."""

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Default streaming implementation: falls back to non-streaming chat.

        Subclasses should override this when they support true server-sent
        events. The fallback keeps older providers functional.
        """
        response = await self.chat(request)
        yield StreamChunk(
            content=response.content,
            finish_reason=response.finish_reason or "stop",
            model=response.model,
            usage={
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
            },
        )

    @abc.abstractmethod
    async def list_models(self) -> List[ModelInfo]:
        """List available models for this provider."""

    async def test_connection(self) -> tuple[bool, str]:
        """Test that the API key works. Returns (success, message)."""
        try:
            models = await self.list_models()
            return True, f"成功，发现 {len(models)} 个模型"
        except ProviderError as e:
            return False, str(e)
        except Exception as e:
            return False, f"连接失败: {str(e)}"

    def normalize_model_id(self, model: str) -> str:
        """Strip the provider prefix from a model id."""
        if self.model_prefix and model.startswith(f"{self.model_prefix}/"):
            return model[len(self.model_prefix) + 1 :]
        return model

    def is_configured(self) -> bool:
        """Whether the provider has enough configuration to work."""
        if not self.requires_api_key:
            return True
        return bool(self.api_key) and self.api_key not in (
            "your-api-key",
            "your-minimax-key-here",
            "your-nvidia-key-here",
            "",
        )


class ProviderRegistry:
    """Global registry of available provider classes."""

    _providers: Dict[str, type] = {}

    @classmethod
    def register(cls, provider_class: type) -> type:
        if not provider_class.name:
            raise ValueError(f"Provider {provider_class.__name__} must define `name`")
        cls._providers[provider_class.name] = provider_class
        return provider_class

    @classmethod
    def get(cls, name: str) -> Optional[type]:
        return cls._providers.get(name)

    @classmethod
    def all(cls) -> Dict[str, type]:
        return dict(cls._providers)

    @classmethod
    def create(cls, name: str, api_key: str = "", api_base: str = "") -> Optional[Provider]:
        provider_class = cls.get(name)
        if not provider_class:
            return None
        return provider_class(api_key=api_key, api_base=api_base)


def get_provider(name: str, api_key: str = "", api_base: str = "") -> Provider:
    """Factory function to instantiate a provider by name."""
    instance = ProviderRegistry.create(name, api_key=api_key, api_base=api_base)
    if not instance:
        raise ProviderError(f"Unknown provider: {name}", status_code=400, provider=name)
    return instance


def list_providers() -> List[Dict[str, Any]]:
    """List all registered providers with their public metadata."""
    return [
        {
            "name": p.name,
            "display_name": p.display_name,
            "default_api_base": p.default_api_base,
            "api_key_setting": p.api_key_setting,
            "api_base_setting": p.api_base_setting,
            "capabilities": [cap.name for cap in ProviderCapability if p.capabilities & cap],
            "requires_api_key": p.requires_api_key,
        }
        for p in ProviderRegistry.all().values()
    ]


def detect_provider_from_model(model: str) -> str:
    """Identify which provider a model id belongs to.

    The prefix rule prefers the most specific match (longest prefix).
    Falls back to keyword matching for known model name patterns, and
    finally to "minimax" when nothing else matches.

    Provider-specific quirks
    ------------------------
    NVIDIA NIM hosts third-party models (DeepSeek, Llama, Qwen, etc.)
    under the vendor's own namespace prefix, e.g.
    ``deepseek-ai/deepseek-v4-flash`` or ``meta/llama-3.3-70b-instruct``.
    A user adding an NVIDIA key therefore sees model ids that begin
    with ``deepseek-ai/`` or ``meta/`` — and these MUST route to
    NVIDIA, not to the upstream DeepSeek / Meta provider, because the
    billing is happening against the NVIDIA key. Without the special
    case below, the keyword map would (correctly) match "deepseek" and
    send the request to the DeepSeek provider — which has no key on
    this install — and the call would 401. The same applies to ``meta``
    and ``qwen`` prefixed models that NVIDIA also hosts.
    """
    if not model:
        return "minimax"

    if model.startswith("custom:"):
        # Custom providers are stored as custom:<slug>/<model_id>
        return model.split("/", 1)[0]

    # ------------------------------------------------------------------
    # NVIDIA-hosted third-party namespaces. Order matters: prefer
    # the most specific prefix (longest match) when the model id
    # is namespaced.
    #
    # For each namespaced prefix we first ask the matching real-vendor
    # provider (when one exists) whether it has an API key configured.
    # If yes, route to the real vendor so the user pays their own key
    # and gets the upstream's actual model catalog. If not, fall
    # through to NVIDIA (NVIDIA NIM hosts these as third-party models
    # and charges against the user's NVIDIA key).
    # ------------------------------------------------------------------
    nvidia_hosted_prefixes = (
        "deepseek-ai",  # → first-party vendor "deepseek"
        "meta",  # Meta itself has no public chat API → nvidia
        "mistralai",  # no first-party "mistral" provider here → nvidia
        "google",  # → first-party vendor "google"
        "nvidia",  # always nvidia
    )
    # The ProviderRegistry is keyed by short names (``deepseek``),
    # not by the namespaced prefix (``deepseek-ai``). This map
    # bridges the two so the registry lookup below doesn't always
    # return None and we never silently fall back to NVIDIA.
    _nvidia_prefix_to_vendor = {
        "deepseek-ai": "deepseek",
        "google": "google",
    }
    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        for vendor in nvidia_hosted_prefixes:
            if prefix == vendor:
                real_vendor = _nvidia_prefix_to_vendor.get(vendor)
                if real_vendor and real_vendor != "nvidia":
                    real_cls = ProviderRegistry.get(real_vendor)
                    if real_cls is not None:
                        try:
                            real_instance = real_cls(
                                api_key=_setting_or_empty(
                                    real_cls.api_key_setting or f"{real_vendor}_api_key"
                                ),
                                api_base=_setting_or_empty(
                                    real_cls.api_base_setting or f"{real_vendor}_api_base"
                                ),
                            )
                            if real_instance.is_configured():
                                return real_vendor
                        except Exception:
                            # Never let a routing helper blow up the
                            # caller — fall through to NVIDIA.
                            pass
                return "nvidia"

    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        candidates = []
        for provider_cls in ProviderRegistry.all().values():
            if provider_cls.model_prefix and prefix == provider_cls.model_prefix:
                candidates.append((provider_cls.model_prefix, provider_cls.name))

        if candidates:
            candidates.sort(key=lambda x: len(x[0]), reverse=True)
            return candidates[0][1]

    keyword_map = {
        "gpt-": "openai",
        "o1-": "openai",
        "o1": "openai",
        "o3-": "openai",
        "o3": "openai",
        "o4-": "openai",
        "o4": "openai",
        "chatgpt": "openai",
        "claude": "anthropic",
        "gemini": "google",
        "deepseek": "deepseek",
        "moonshot": "moonshot",
        "kimi": "moonshot",
        "glm": "zhipu",
        "qwen": "aliyun",
        "tongyi": "aliyun",
        "doubao": "doubao",
        "ep-": "doubao",
        "nvidia/": "nvidia",
        "llama": "nvidia",
        "mistral": "nvidia",
        "gemma": "nvidia",
        "mixtral": "nvidia",
        "nemotron": "nvidia",
        "phi-": "nvidia",
        "abab": "minimax",
        "MiniMax": "minimax",
        "speech": "minimax",
        "mimo": "mimo",
    }

    model_lower = model.lower()
    for keyword, provider_name in keyword_map.items():
        # Prefix match instead of substring: ``"gpt-" in "something-gpt-x"``
        # would falsely route a namespaced model to OpenAI, and ``"o1" in
        # "co1-foo"`` would mis-route. Prefix matching keeps the
        # intended semantics (``gpt-4``, ``o1-mini``, ``claude-3-opus``)
        # without leaking across vendor boundaries. Namespaced IDs
        # (``meta/llama-3``, ``nvidia/…``) are already resolved by the
        # NVIDIA-hosted / model_prefix blocks above, so the keyword map
        # only sees bare model ids here.
        if model_lower.startswith(keyword.lower()):
            return provider_name

    return "minimax"
