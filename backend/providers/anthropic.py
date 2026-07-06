"""Anthropic Claude provider adapter (Messages API)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from backend.providers.base import (
    ChatRequest,
    ChatResponse,
    ModelInfo,
    Provider,
    ProviderError,
    ProviderRegistry,
)
from backend.services.http_client import get_async_client

logger = logging.getLogger(__name__)


@ProviderRegistry.register
class AnthropicProvider(Provider):
    name = "anthropic"
    display_name = "Anthropic Claude"
    default_api_base = "https://api.anthropic.com"
    api_key_setting = "anthropic_api_key"
    api_base_setting = "anthropic_api_base"
    model_prefix = "anthropic"
    request_timeout = 120.0

    _FALLBACK_MODELS: List[ModelInfo] = [
        ModelInfo(id="claude-opus-4-5", display_name="Claude Opus 4.5", context_length=200000),
        ModelInfo(id="claude-sonnet-4-5", display_name="Claude Sonnet 4.5", context_length=200000),
        ModelInfo(
            id="claude-3-7-sonnet-20250219", display_name="Claude 3.7 Sonnet", context_length=200000
        ),
        ModelInfo(
            id="claude-3-5-sonnet-20241022", display_name="Claude 3.5 Sonnet", context_length=200000
        ),
        ModelInfo(
            id="claude-3-5-haiku-20241022", display_name="Claude 3.5 Haiku", context_length=200000
        ),
        ModelInfo(id="claude-3-opus-20240229", display_name="Claude 3 Opus", context_length=200000),
        ModelInfo(
            id="claude-3-sonnet-20240229", display_name="Claude 3 Sonnet", context_length=200000
        ),
        ModelInfo(
            id="claude-3-haiku-20240307", display_name="Claude 3 Haiku", context_length=200000
        ),
    ]

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _request_body(self, request: ChatRequest) -> Dict[str, Any]:
        system_text = request.system or ""
        messages: List[Dict[str, Any]] = []
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})
        body: Dict[str, Any] = {
            "model": self.normalize_model_id(request.model),
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
            "temperature": request.temperature,
        }
        if system_text:
            body["system"] = system_text
        if request.stop:
            body["stop_sequences"] = request.stop
        body.update(request.extra)
        return body

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key",
                status_code=400,
                provider=self.name,
            )
        url = f"{self.api_base.rstrip('/')}/v1/messages"
        body = self._request_body(request)
        try:
            client = get_async_client()
            resp = await client.post(
                url, json=body, headers=self._headers(), timeout=self.request_timeout
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {e}", status_code=502, provider=self.name)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message") or err.get("detail") or resp.text
            except Exception:
                msg = resp.text or "请求失败"
            raise ProviderError(str(msg), status_code=resp.status_code, provider=self.name)

        data = resp.json()
        content_parts = data.get("content", [])
        text = ""
        for part in content_parts:
            if part.get("type") == "text":
                text += part.get("text", "")
        usage = data.get("usage", {})
        return ChatResponse(
            content=text,
            model=data.get("model", request.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=(usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
            finish_reason=data.get("stop_reason", "end_turn"),
            raw=data,
        )

    async def list_models(self) -> List[ModelInfo]:
        if not self.is_configured():
            raise ProviderError("未配置 API Key", status_code=400, provider=self.name)
        try:
            client = get_async_client()
            resp = await client.get(
                f"{self.api_base.rstrip('/')}/v1/models",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                timeout=20.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                raw_models = data.get("data", []) if isinstance(data, dict) else []
                models: List[ModelInfo] = []
                for m in raw_models:
                    mid = m.get("id", "")
                    if not mid:
                        continue
                    models.append(
                        ModelInfo(
                            id=mid,
                            display_name=m.get("display_name", mid),
                            context_length=int(m.get("context_length", 0) or 0),
                        )
                    )
                if models:
                    return models
        except Exception:
            pass
        return list(self._FALLBACK_MODELS)
