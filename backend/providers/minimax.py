"""MiniMax (MiniMax) provider adapter (MiniMax-specific protocol)."""

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
class MiniMaxProvider(Provider):
    name = "minimax"
    display_name = "MiniMax (MiniMax)"
    default_api_base = "https://api.minimaxi.com"
    api_key_setting = "minimax_api_key"
    api_base_setting = "minimax_api_base"
    model_prefix = "minimax"
    request_timeout = 120.0

    _FALLBACK_MODELS: List[ModelInfo] = [
        ModelInfo(
            id="MiniMax-M1", display_name="MiniMax-M1 (推理/Reasoning)", context_length=1000000
        ),
        ModelInfo(
            id="MiniMax-Text-01", display_name="MiniMax-Text-01 (文本/Text)", context_length=128000
        ),
        ModelInfo(id="abab-6.5s-chat", display_name="abab-6.5s-chat", context_length=245760),
        ModelInfo(id="abab-6.5g-chat", display_name="abab-6.5g-chat", context_length=8000),
        ModelInfo(id="abab-6.5t-chat", display_name="abab-6.5t-chat", context_length=8000),
        ModelInfo(id="abab-5.5s-chat", display_name="abab-5.5s-chat", context_length=16384),
        ModelInfo(id="abab-5.5-chat", display_name="abab-5.5-chat", context_length=16384),
    ]

    _DYNAMIC_ENDPOINTS = (
        "/v1/models",
        "/v1/api/models",
        "/v1/model/list",
        "/v1/text/models",
    )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key",
                status_code=400,
                provider=self.name,
            )

        url = f"{self.api_base.rstrip('/')}/v1/text/chatcompletion_v2"
        messages: List[Dict[str, Any]] = []
        if request.system:
            messages.append(
                {"role": "system", "name": "MM AI Assistant", "content": request.system}
            )
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})

        body: Dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 2048,
            "temperature": request.temperature,
        }
        if request.top_p != 1.0:
            body["top_p"] = request.top_p
        body.update(request.extra)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            client = get_async_client()
            resp = await client.post(url, json=body, headers=headers, timeout=self.request_timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {e}", status_code=502, provider=self.name)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("base_resp", {}).get("status_msg") or resp.text
            except Exception:
                msg = resp.text or "请求失败"
            raise ProviderError(str(msg), status_code=resp.status_code, provider=self.name)

        data = resp.json()
        base_resp = data.get("base_resp", {})
        if base_resp and base_resp.get("status_code", 0) != 0:
            msg = base_resp.get("status_msg", "未知错误")
            raise ProviderError(
                f"MiniMax API错误: {msg} (代码: {base_resp.get('status_code')})",
                status_code=400,
                provider=self.name,
            )

        choices = data.get("choices", [])
        content = ""
        if choices:
            content = choices[0].get("message", {}).get("content", "") or ""
        usage = data.get("usage", {})
        return ChatResponse(
            content=content,
            model=data.get("model", request.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason="stop",
            raw=data,
        )

    async def list_models(self) -> List[ModelInfo]:
        if not self.is_configured():
            raise ProviderError("未配置 API Key", status_code=400, provider=self.name)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        for endpoint in self._DYNAMIC_ENDPOINTS:
            url = f"{self.api_base.rstrip('/')}{endpoint}"
            try:
                client = get_async_client()
                resp = await client.get(url, headers=headers, timeout=20.0)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue

            raw_models: List[Any] = []
            if isinstance(data, list):
                raw_models = data
            elif isinstance(data, dict):
                for key in ("data", "models", "items", "model_list"):
                    if isinstance(data.get(key), list):
                        raw_models = data[key]
                        break

            models: List[ModelInfo] = []
            for m in raw_models:
                if not isinstance(m, dict):
                    continue
                mid = m.get("model") or m.get("model_name") or m.get("id") or m.get("name")
                if not mid:
                    continue
                mid = str(mid)
                display = m.get("display_name") or m.get("name") or mid
                models.append(
                    ModelInfo(
                        id=mid,
                        display_name=str(display),
                        context_length=int(m.get("context_length", 0) or 0),
                    )
                )
            if models:
                return models

        return list(self._FALLBACK_MODELS)
