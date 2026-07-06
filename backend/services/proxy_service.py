import json
import time
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import httpx

from backend.config import Config
from backend.database import add_usage_log, get_setting
from backend.services.channel_service import ChannelService
from backend.services.http_client import get_async_client, post_with_retry
from backend.services import key_pool
from backend.utils.circuit_breaker import CircuitBreaker, CircuitOpenError

_provider_breakers: dict[str, CircuitBreaker] = {}

import re as _re

_ORG_ID_RE = _re.compile(r"org-[A-Za-z0-9]{20,}")
_KEY_ID_RE = _re.compile(r"sk-[A-Za-z0-9]{8,}")
_REQ_ID_RE = _re.compile(r"req-[A-Za-z0-9]{16,}")


def _sanitize_upstream_error(detail: str, status_code: int) -> str:
    if not detail:
        return f"上游服务返回错误 ({status_code})"
    cleaned = _ORG_ID_RE.sub("[redacted]", detail)
    cleaned = _KEY_ID_RE.sub("[redacted]", cleaned)
    cleaned = _REQ_ID_RE.sub("[redacted]", cleaned)
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "..."
    return cleaned


def _breaker_for(provider: str) -> CircuitBreaker:
    if provider not in _provider_breakers:
        _provider_breakers[provider] = CircuitBreaker(
            name=f"provider:{provider}",
            failure_threshold=5,
            cooldown_seconds=60.0,
            expected_exceptions=(
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ),
        )
    return _provider_breakers[provider]


_LEGACY_MINIMAX_FALLBACK = "/v1/text/chatcompletion_v2"


def _resolve_provider_endpoint(provider: str) -> Tuple[str, str]:
    default_api_base = ""
    chat_path: Optional[str] = None
    try:
        from backend.providers.base import ProviderRegistry

        cls = ProviderRegistry.get(provider)
        if cls is not None:
            default_api_base = getattr(cls, "default_api_base", "") or ""
            chat_path = getattr(cls, "chat_endpoint", None)
    except Exception:
        pass

    if not chat_path:
        chat_path = _LEGACY_MINIMAX_FALLBACK
    return default_api_base, chat_path


def _build_chat_url(api_base: str, provider: str) -> str:
    _, chat_path = _resolve_provider_endpoint(provider)
    base = (api_base or "").rstrip("/")
    if chat_path.startswith("/v1/") and base.endswith("/v1"):
        base = base[:-3]
    return f"{base}{chat_path}"


def _extract_upstream_error(body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    try:
        obj = json.loads(body)
    except Exception:
        return body[:300]
    if not isinstance(obj, dict):
        return body[:300]
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("type") or "")
    if isinstance(err, str):
        return err
    detail = obj.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        msgs = [d.get("msg") for d in detail if isinstance(d, dict) and d.get("msg")]
        if msgs:
            return "; ".join(str(m) for m in msgs)
    return body[:300]


async def _stream_request_through_breaker(
    provider: str,
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    headers: dict,
) -> httpx.Response:
    breaker = _breaker_for(provider)
    if breaker.is_open():
        raise CircuitOpenError(breaker.name, breaker.retry_after())
    try:
        req = client.build_request("POST", url, json=payload, headers=headers)
        resp = await client.send(req, stream=True)
    except breaker.expected_exceptions as exc:
        await breaker.record_failure(exc)
        raise
    # A non-2xx response means the upstream rejected the request
    # (auth, rate-limit, malformed payload, …). Treating these as
    # successes masks provider outages from the circuit breaker, so
    # record a failure whenever the status code indicates an error.
    # We can't raise an HTTPStatusError here because the caller needs
    # to read the body off the open stream for error reporting.
    if resp.status_code >= 400:
        await breaker.record_failure(
            httpx.HTTPStatusError(
                f"upstream {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        )
    else:
        await breaker.record_success()
    return resp


def _strip_provider_prefix(model: str, provider: str) -> str:
    if not model or not provider:
        return model
    prefix = provider + "/"
    if model.startswith(prefix):
        return model[len(prefix):]
    return model


class ProxyService:
    @staticmethod
    async def forward_request(
        user_id: int,
        payload: Dict[str, Any],
        provider: str = "minimax",
        *,
        token_id: int | None = None,
    ) -> tuple[Dict[str, Any], int, int | None]:
        start_time = time.time()

        attempted: set[int] = set()
        fallback_count = 0
        last_error = ""

        while True:
            channel = ChannelService.select_channel(provider=provider, exclude_ids=attempted)
            if channel:
                attempted.add(channel.id)
                api_key = channel.api_key
                api_base = channel.base_url
                channel_id = channel.id
            else:
                default_api_base, _chat_path = _resolve_provider_endpoint(provider)
                api_key = get_setting(f"{provider}_api_key") or (
                    Config.MINIMAX_API_KEY if provider == "minimax" else ""
                )
                api_base = (
                    get_setting(f"{provider}_api_base")
                    or default_api_base
                    or (Config.MINIMAX_API_BASE if provider == "minimax" else "")
                )
                channel_id = None

            if not api_key:
                response_time_ms = int((time.time() - start_time) * 1000)
                usage_log_id = add_usage_log(
                    user_id=user_id,
                    endpoint=api_base,
                    model=payload.get("model", "unknown"),
                    prompt_tokens=0,
                    completion_tokens=0,
                    response_time_ms=response_time_ms,
                    status_code=500,
                    token_id=token_id,
                    channel_id=channel_id,
                    provider=provider,
                    error_message="upstream_api_key_missing",
                    metadata={
                        "provider": provider,
                        "channel_id": channel_id,
                        "fallback_count": fallback_count,
                    },
                )
                return {"error": "UPSTREAM_API_KEY_MISSING"}, 500, usage_log_id

            url = _build_chat_url(api_base, provider)

            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            upstream_payload = dict(payload)
            upstream_payload["model"] = _strip_provider_prefix(
                upstream_payload.get("model", ""), provider
            )

            prompt_tokens = 0
            completion_tokens = 0
            status_code = 200
            usage_log_id = None

            try:
                try:
                    response = await _breaker_for(provider).call(
                        post_with_retry,
                        url,
                        json=upstream_payload,
                        headers=headers,
                        retries=2,
                    )
                except CircuitOpenError:
                    last_error = "circuit_open"
                    if channel_id:
                        ChannelService.mark_failed(channel_id=channel_id, error=last_error)
                    if fallback_count >= int(Config.CHANNEL_FALLBACK_MAX or 1):
                        return {"error": "CIRCUIT_OPEN"}, 503, None
                    fallback_count += 1
                    if not channel:
                        return {"error": last_error}, 503, None
                    continue
                status_code = response.status_code
                try:
                    result = response.json()
                except Exception:
                    result = {}

                if response.status_code == 200 and "choices" in result:
                    usage = result.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                elif response.status_code == 200 and "data" in result:
                    prompt_tokens = result.get("data", {}).get("prompt_tokens", 0)
                    completion_tokens = result.get("data", {}).get("completion_tokens", 0)
                elif response.status_code == 200:
                    # Fallback for non-OpenAI-shaped responses:
                    # - Anthropic Messages API: top-level ``usage`` with
                    #   ``input_tokens`` / ``output_tokens``.
                    # - Gemini: top-level ``usageMetadata`` with
                    #   ``promptTokenCount`` / ``candidatesTokenCount``.
                    usage = result.get("usage") or {}
                    if usage:
                        prompt_tokens = (
                            usage.get("prompt_tokens")
                            or usage.get("input_tokens")
                            or 0
                        )
                        completion_tokens = (
                            usage.get("completion_tokens")
                            or usage.get("output_tokens")
                            or 0
                        )
                    usage_meta = result.get("usageMetadata") or {}
                    if usage_meta:
                        prompt_tokens = prompt_tokens or int(
                            usage_meta.get("promptTokenCount") or 0
                        )
                        completion_tokens = completion_tokens or int(
                            usage_meta.get("candidatesTokenCount") or 0
                        )

                response_time_ms = int((time.time() - start_time) * 1000)

                usage_log_id = add_usage_log(
                    user_id=user_id,
                    endpoint=url,
                    model=payload.get("model", "unknown"),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    response_time_ms=response_time_ms,
                    status_code=status_code,
                    token_id=token_id,
                    channel_id=channel_id,
                    provider=provider,
                    error_message=None if status_code == 200 else "upstream_error",
                    metadata={
                        "provider": provider,
                        "channel_id": channel_id,
                        "fallback_count": fallback_count,
                    },
                )

                if status_code == 200:
                    if channel_id:
                        ChannelService.mark_healthy(channel_id=channel_id)
                    key_pool.mark_success(provider, api_key)
                    return result, status_code, usage_log_id

                last_error = "upstream_error"
                if channel_id:
                    ChannelService.mark_failed(channel_id=channel_id, error=last_error)

            except Exception:
                response_time_ms = int((time.time() - start_time) * 1000)
                usage_log_id = add_usage_log(
                    user_id=user_id,
                    endpoint=url,
                    model=payload.get("model", "unknown"),
                    prompt_tokens=0,
                    completion_tokens=0,
                    response_time_ms=response_time_ms,
                    status_code=502,
                    token_id=token_id,
                    channel_id=channel_id,
                    provider=provider,
                    error_message="upstream_request_failed",
                    metadata={
                        "provider": provider,
                        "channel_id": channel_id,
                        "fallback_count": fallback_count,
                    },
                )
                last_error = "upstream_request_failed"
                if channel_id:
                    ChannelService.mark_failed(channel_id=channel_id, error=last_error)

            if fallback_count >= int(Config.CHANNEL_FALLBACK_MAX or 1):
                return {"error": "UPSTREAM_ERROR"}, 502, usage_log_id

            fallback_count += 1

            if not channel:
                return {"error": last_error or "UPSTREAM_ERROR"}, 502, usage_log_id

    @staticmethod
    async def stream_chat(
        user_id: int,
        payload: Dict[str, Any],
        provider: str = "minimax",
        *,
        token_id: Optional[int] = None,
        request: Optional[Any] = None,
    ) -> AsyncIterator[bytes]:
        import logging as _log

        logger = _log.getLogger(__name__)

        exclude_ids: set[int] = set()
        max_fallbacks = 3
        last_error: Optional[str] = None

        for attempt in range(max_fallbacks):
            channel = ChannelService.select_channel(provider=provider, exclude_ids=exclude_ids)
            if channel:
                api_key = channel.api_key
                api_base = channel.base_url
                channel_id: Optional[int] = channel.id
            else:
                default_api_base, _chat_path = _resolve_provider_endpoint(provider)
                api_key = get_setting(f"{provider}_api_key") or (
                    Config.MINIMAX_API_KEY if provider == "minimax" else ""
                )
                api_base = (
                    get_setting(f"{provider}_api_base")
                    or default_api_base
                    or (Config.MINIMAX_API_BASE if provider == "minimax" else "")
                )
                channel_id = None

            if not api_key:
                if channel_id is not None:
                    ChannelService.mark_failed(
                        channel_id=channel_id, error="upstream_api_key_missing"
                    )
                    exclude_ids.add(channel_id)
                last_error = "upstream_api_key_missing"
                if channel_id is None:
                    break
                continue

            url = _build_chat_url(api_base, provider)

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            upstream_payload = dict(payload)
            upstream_payload["stream"] = True
            # OpenAI streaming protocol only returns usage on the final
            # chunk when stream_options.include_usage is set. Without it
            # every streaming request falls back to estimation, which
            # under-counts CJK text 4-8x. Mirror the OpenAICompatibleProvider
            # behaviour (openai_compatible.py L181).
            upstream_payload["stream_options"] = {"include_usage": True}
            upstream_payload["model"] = _strip_provider_prefix(
                upstream_payload.get("model", ""), provider
            )

            client = get_async_client()
            prompt_tokens = 0
            completion_tokens = 0
            accumulated = ""
            first_chunk_yielded = False
            start_time = time.time()

            logger.info(
                "stream_chat: POST %s provider=%s model=%s attempt=%d/%d",
                url,
                provider,
                payload.get("model"),
                attempt + 1,
                max_fallbacks,
            )

            try:
                resp = await _stream_request_through_breaker(
                    provider, client, url, upstream_payload, headers
                )
            except CircuitOpenError:
                if channel_id is not None:
                    ChannelService.mark_failed(channel_id=channel_id, error="circuit_open")
                    exclude_ids.add(channel_id)
                last_error = "circuit_open"
                if channel_id is None:
                    break
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if channel_id is not None:
                    ChannelService.mark_failed(channel_id=channel_id, error=str(exc)[:200])
                    exclude_ids.add(channel_id)
                key_pool.mark_failure(provider, api_key, str(exc)[:200])
                last_error = str(exc)[:200]
                if channel_id is None:
                    break
                continue

            try:
                if resp.status_code != 200:
                    try:
                        body = (await resp.aread()).decode("utf-8", errors="ignore")
                    except Exception:
                        body = ""
                    detail = (
                        _extract_upstream_error(body)
                        or body[:200]
                        or f"upstream {resp.status_code}"
                    )
                    if channel_id is not None:
                        ChannelService.mark_failed(
                            channel_id=channel_id,
                            error=f"upstream_{resp.status_code}",
                        )
                        exclude_ids.add(channel_id)
                    last_error = f"upstream {resp.status_code}: {detail}"
                    if channel_id is None:
                        safe_msg = _sanitize_upstream_error(detail, resp.status_code)
                        yield _sse_event(
                            "error",
                            {
                                "error": safe_msg,
                                "code": resp.status_code,
                            },
                        )
                        return
                    continue

                chunk_count = 0
                current_event_type = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if request is not None:
                        chunk_count += 1
                        if chunk_count % 10 == 0:
                            try:
                                if await request.is_disconnected():
                                    await resp.aclose()
                                    return
                            except Exception:
                                pass
                    # Anthropic / Gemini stream with explicit ``event:``
                    # lines preceding the ``data:`` payload. Track the
                    # current event type so the parser below can branch
                    # on it (e.g. ``message_start`` carries input usage,
                    # ``message_delta`` carries output usage). OpenAI-
                    # compatible streams omit ``event:`` and leave the
                    # variable empty, which is a no-op for the logic
                    # below.
                    if line.startswith("event:"):
                        current_event_type = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            chunk = json.loads(body)
                        except Exception:
                            continue
                        # OpenAI-compatible delta extraction.
                        delta_text = (
                            chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                            or chunk.get("choices", [{}])[0].get("text")
                            or ""
                        )
                        # Anthropic delta: ``{"type":"content_block_delta",
                        # "delta":{"type":"text_delta","text":"..."}}``.
                        if not delta_text:
                            anthropic_delta = chunk.get("delta") or {}
                            if isinstance(anthropic_delta, dict):
                                delta_text = anthropic_delta.get("text") or ""
                        if delta_text:
                            accumulated += delta_text
                            yield _sse_event("delta", {"content": delta_text})
                            first_chunk_yielded = True
                        # OpenAI-style usage (final chunk).
                        usage = chunk.get("usage") or {}
                        if usage:
                            prompt_tokens = int(
                                usage.get("prompt_tokens", prompt_tokens) or prompt_tokens
                            )
                            completion_tokens = int(
                                usage.get("completion_tokens", completion_tokens)
                                or completion_tokens
                            )
                        # Anthropic usage: ``message_start`` carries
                        # ``message.usage.input_tokens`` and
                        # ``message_delta`` carries top-level
                        # ``usage.output_tokens``.
                        if current_event_type == "message_start":
                            msg = chunk.get("message") or {}
                            msg_usage = msg.get("usage") or {}
                            if msg_usage:
                                prompt_tokens = int(
                                    msg_usage.get("input_tokens", prompt_tokens)
                                    or prompt_tokens
                                )
                        elif current_event_type == "message_delta":
                            md_usage = chunk.get("usage") or {}
                            if md_usage:
                                completion_tokens = int(
                                    md_usage.get("output_tokens", completion_tokens)
                                    or completion_tokens
                                )
                        # Gemini streaming: each chunk may carry
                        # ``usageMetadata`` with prompt / candidates /
                        # total token counts.
                        usage_meta = chunk.get("usageMetadata") or {}
                        if usage_meta:
                            prompt_tokens = int(
                                usage_meta.get("promptTokenCount", prompt_tokens)
                                or prompt_tokens
                            )
                            completion_tokens = int(
                                usage_meta.get("candidatesTokenCount", completion_tokens)
                                or completion_tokens
                            )

                if prompt_tokens == 0 and completion_tokens == 0 and accumulated:
                    # Stream completed but no usage chunk was emitted —
                    # either the upstream ignored stream_options.include_usage
                    # (non-OpenAI-compatible API) or the usage chunk was
                    # dropped. Surface it so operators can locate the
                    # under-billing culprit.
                    logger.warning(
                        "stream_chat: zero usage despite non-empty content "
                        "provider=%s model=%s url=%s accumulated_len=%d",
                        provider,
                        payload.get("model"),
                        url,
                        len(accumulated),
                    )
                yield _sse_event(
                    "done",
                    {
                        "content": accumulated,
                        "model": payload.get("model", ""),
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    },
                )
                if channel_id is not None:
                    ChannelService.mark_healthy(channel_id=channel_id)
                key_pool.mark_success(provider, api_key)
                return

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning(
                    "stream_chat: upstream error provider=%s model=%s url=%s err=%s first_chunk=%s",
                    provider,
                    payload.get("model"),
                    url,
                    exc,
                    first_chunk_yielded,
                )
                if first_chunk_yielded:
                    yield _sse_event(
                        "error",
                        {
                            "error": f"mid_stream_failure: {exc}",
                            "code": 502,
                        },
                    )
                    key_pool.mark_failure(provider, api_key, str(exc)[:200])
                    return
                if channel_id is not None:
                    ChannelService.mark_failed(channel_id=channel_id, error=str(exc)[:200])
                    exclude_ids.add(channel_id)
                key_pool.mark_failure(provider, api_key, str(exc)[:200])
                last_error = str(exc)[:200]
                if channel_id is None:
                    break
                continue

            except Exception as exc:
                logger.exception(
                    "stream_chat: unexpected error provider=%s model=%s",
                    provider,
                    payload.get("model"),
                )
                if first_chunk_yielded:
                    yield _sse_event(
                        "error",
                        {"error": f"mid_stream_failure: {exc}", "code": 502},
                    )
                    key_pool.mark_failure(provider, api_key, str(exc)[:200])
                    return
                if channel_id is not None:
                    ChannelService.mark_failed(channel_id=channel_id, error=str(exc)[:200])
                    exclude_ids.add(channel_id)
                key_pool.mark_failure(provider, api_key, str(exc)[:200])
                last_error = str(exc)[:200]
                if channel_id is None:
                    break
                continue
            finally:
                try:
                    await resp.aclose()
                except Exception:
                    pass

        yield _sse_event(
            "error",
            {
                "error": last_error or "all_channels_unavailable",
                "code": 503,
            },
        )


def _sse_event(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
