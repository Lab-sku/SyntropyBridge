"""OpenAI-compatible base provider.

A large class of providers (OpenAI, NVIDIA NIM, DeepSeek, Moonshot, Zhipu,
Aliyun DashScope, OpenRouter, and others) speak the OpenAI Chat Completions
protocol. This base class captures the shared request/response handling.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List

import httpx

from backend.providers.base import (
    ChatRequest,
    ChatResponse,
    ModelInfo,
    Provider,
    ProviderError,
    StreamChunk,
)
from backend.services.http_client import get_async_client

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(Provider):
    """Base class for any provider that speaks OpenAI's /v1/chat/completions."""

    chat_endpoint: str = "/v1/chat/completions"
    models_endpoint: str = "/v1/models"
    request_timeout: float = 120.0

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request_body(self, request: ChatRequest) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})

        body: Dict[str, Any] = {
            "model": self.normalize_model_id(request.model),
            "messages": messages,
            "temperature": request.temperature,
            "stream": request.stream,
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.top_p != 1.0:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop"] = request.stop
        body.update(request.extra)
        return body

    def _url(self, endpoint: str) -> str:
        """Join the base URL with an endpoint, deduping any `/v1` segment.

        Some provider base URLs already include `/v1` (NVIDIA NIM
        `https://integrate.api.nvidia.com/v1`) while others do not. The
        endpoint strings we append always start with `/v1/...`. Naively
        concatenating produces `/v1/v1/...` which 404s on the real APIs.
        Stripping the trailing `/v1` from the base fixes that.
        """
        base = self.api_base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{endpoint}"

    def _parse_response(self, data: Dict[str, Any], model: str) -> ChatResponse:
        choices = data.get("choices", [])
        content = ""
        finish_reason = "stop"
        if choices:
            choice = choices[0]
            message = choice.get("message", {}) or {}
            content = message.get("content", "") or ""
            finish_reason = choice.get("finish_reason", "stop")

        usage = data.get("usage", {}) or {}
        return ChatResponse(
            content=content,
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason=finish_reason or "stop",
            raw=data,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key",
                status_code=400,
                provider=self.name,
            )

        url = self._url(self.chat_endpoint)
        body = self._request_body(request)

        try:
            client = get_async_client()
            resp = await client.post(
                url, json=body, headers=self._headers(), timeout=self.request_timeout
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {str(e)}", status_code=502, provider=self.name)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message") or err.get("detail") or resp.text
            except Exception:
                msg = resp.text or "请求失败"
            logger.warning("%s API error %s: %s", self.name, resp.status_code, msg)
            raise ProviderError(str(msg), status_code=resp.status_code, provider=self.name)

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ProviderError(f"响应解析失败: {e}", status_code=502, provider=self.name)

        return self._parse_response(data, request.model)

    async def list_models(self) -> List[ModelInfo]:
        if not self.api_key:
            raise ProviderError("未配置 API Key", status_code=400, provider=self.name)
        url = self._url(self.models_endpoint)
        try:
            client = get_async_client()
            resp = await client.get(url, headers=self._headers(), timeout=30.0)
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {e}", status_code=502, provider=self.name)
        if resp.status_code != 200:
            raise ProviderError(
                f"模型列表获取失败: HTTP {resp.status_code}",
                status_code=resp.status_code,
                provider=self.name,
            )
        data = resp.json()
        models: List[ModelInfo] = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            models.append(
                ModelInfo(
                    id=mid if not self.model_prefix else f"{self.model_prefix}/{mid}",
                    display_name=mid,
                )
            )
        return models

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion from the upstream OpenAI-compatible API.

        Yields normalized `StreamChunk` objects so the proxy can fan them
        out to the frontend via SSE without caring about provider details.
        """
        if not self.is_configured():
            raise ProviderError(
                f"{self.display_name} 未配置 API Key",
                status_code=400,
                provider=self.name,
            )

        url = self._url(self.chat_endpoint)
        body = self._request_body(request)
        body["stream"] = True
        # The OpenAI streaming protocol sends token counts only on the
        # last chunk when `stream_options.include_usage` is set.
        body["stream_options"] = {"include_usage": True}

        try:
            client = get_async_client()
            async with client.stream(
                "POST", url, json=body, headers=self._headers(), timeout=self.request_timeout
            ) as resp:
                if resp.status_code != 200:
                    err_text = await resp.aread()
                    try:
                        err = json.loads(err_text)
                        msg = (
                            err.get("error", {}).get("message")
                            or err.get("detail")
                            or err_text.decode("utf-8", "ignore")
                        )
                    except Exception:
                        msg = err_text.decode("utf-8", "ignore") or "请求失败"
                    logger.warning("%s streaming error %s: %s", self.name, resp.status_code, msg)
                    raise ProviderError(str(msg), status_code=resp.status_code, provider=self.name)

                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        yield StreamChunk(finish_reason="stop", model=request.model)
                        return
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choice = (data.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    finish_reason = choice.get("finish_reason") or ""
                    usage = data.get("usage") or None
                    yield StreamChunk(
                        content=content,
                        finish_reason=finish_reason,
                        model=data.get("model", request.model),
                        usage=usage,
                    )
        except httpx.HTTPError as e:
            raise ProviderError(f"网络错误: {str(e)}", status_code=502, provider=self.name)
