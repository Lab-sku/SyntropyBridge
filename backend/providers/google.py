"""Google Gemini provider adapter.

Uses Google's Generative Language API. Gemini now also supports an
OpenAI-compatible endpoint at /v1beta/openai/, but the native API gives
more control. This implementation supports the OpenAI-compatible
endpoint for simplicity, with a fallback to the native API surface.
"""

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
class GoogleProvider(Provider):
    name = "google"
    display_name = "Google Gemini"
    default_api_base = "https://generativelanguage.googleapis.com"
    api_key_setting = "google_api_key"
    api_base_setting = "google_api_base"
    model_prefix = "google"

    _FALLBACK_MODELS: List[ModelInfo] = [
        ModelInfo(id="gemini-2.5-pro", display_name="Gemini 2.5 Pro", context_length=2000000),
        ModelInfo(id="gemini-2.5-flash", display_name="Gemini 2.5 Flash", context_length=1000000),
        ModelInfo(id="gemini-2.0-flash", display_name="Gemini 2.0 Flash", context_length=1000000),
        ModelInfo(
            id="gemini-2.0-flash-lite", display_name="Gemini 2.0 Flash Lite", context_length=1000000
        ),
        ModelInfo(id="gemini-1.5-pro", display_name="Gemini 1.5 Pro", context_length=2000000),
        ModelInfo(id="gemini-1.5-flash", display_name="Gemini 1.5 Flash", context_length=1000000),
        ModelInfo(
            id="gemini-1.5-flash-8b", display_name="Gemini 1.5 Flash 8B", context_length=1000000
        ),
    ]

    def _model_path(self, model: str) -> str:
        model_id = self.normalize_model_id(model)
        return f"/v1beta/models/{model_id}:generateContent"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key",
                status_code=400,
                provider=self.name,
            )

        url = f"{self.api_base.rstrip('/')}{self._model_path(request.model)}"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        contents: List[Dict[str, Any]] = []
        if request.system:
            contents.append({"role": "user", "parts": [{"text": request.system}]})
            contents.append({"role": "model", "parts": [{"text": "OK"}]})
        for msg in request.messages:
            role = "model" if msg.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg.content}]})

        body: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens or 2048,
            },
        }
        if request.top_p != 1.0:
            body["generationConfig"]["topP"] = request.top_p
        if request.stop:
            body["generationConfig"]["stopSequences"] = request.stop
        body.update(request.extra)

        try:
            client = get_async_client()
            resp = await client.post(url, json=body, headers=headers, timeout=120.0)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {e}", status_code=502, provider=self.name)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message") or resp.text
            except Exception:
                msg = resp.text or "请求失败"
            raise ProviderError(str(msg), status_code=resp.status_code, provider=self.name)

        data = resp.json()
        candidates = data.get("candidates", [])
        text = ""
        finish_reason = "stop"
        if candidates:
            cand = candidates[0]
            content = cand.get("content", {})
            for part in content.get("parts", []):
                text += part.get("text", "")
            finish_reason = cand.get("finishReason", "stop") or "stop"

        usage = data.get("usageMetadata", {})
        return ChatResponse(
            content=text,
            model=request.model,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            total_tokens=usage.get("totalTokenCount", 0),
            finish_reason=finish_reason,
            raw=data,
        )

    async def list_models(self) -> List[ModelInfo]:
        if not self.is_configured():
            raise ProviderError("未配置 API Key", status_code=400, provider=self.name)
        url = f"{self.api_base.rstrip('/')}/v1beta/models"
        headers = {"x-goog-api-key": self.api_key}
        try:
            client = get_async_client()
            resp = await client.get(url, headers=headers, timeout=30.0)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {e}", status_code=502, provider=self.name)
        if resp.status_code != 200:
            return list(self._FALLBACK_MODELS)
        data = resp.json()
        models: List[ModelInfo] = []
        for m in data.get("models", []):
            mid = m.get("name", "").split("/")[-1]
            if not mid:
                continue
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            models.append(
                ModelInfo(
                    id=mid,
                    display_name=mid,
                    context_length=int(m.get("inputTokenLimit", 0) or 0),
                )
            )
        return models or self._FALLBACK_MODELS
