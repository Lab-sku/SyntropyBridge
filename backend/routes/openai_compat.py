"""OpenAI-compatible gateway routes.

Exposes the platform as a drop-in OpenAI endpoint so any OpenAI client
(Claude Code, Cursor, Cline, Kilo, Trae, OpenAI SDK, etc.) can use one
of our `sk-...` keys to reach any configured upstream provider.

Endpoints (mounted on the global FastAPI app at registration time):

  GET  /v1/models              - list models, OpenAI standard envelope
  GET  /v1/models/{model_id}   - retrieve a single model
  POST /v1/chat/completions    - core chat completion (supports stream)
  POST /v1/completions         - legacy text completion shim
  GET  /v1/usage               - this user's monthly usage

Usage
-----

    from backend.routes.openai_compat import router
    app.include_router(router)

The router is mounted directly on the FastAPI app with no prefix
because the client SDKs hard-code `/v1/...` URLs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.database import (
    add_usage_log,
    get_client_ip,
    get_db_context,
    get_model_pricing,
    get_pricing_for_model_list,
    get_user_plan,
    get_wallet,
    update_usage_log_cost,
    update_wallet,
)
from backend.services import key_pool
from backend.services.auth_service import (
    check_key_restrictions,
    get_monthly_summary,
    resolve_api_key,
    update_last_used,
)
from backend.services.billing_service import quote_cost, reconcile_stream_reserve
from backend.services.model_aggregator import get_cached_provider_models
from backend.services.model_pool_service import ModelPoolService
from backend.services.proxy_service import ProxyService
from backend.utils import idempotency
from backend.utils.provider import get_provider_for_model

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    stop: Optional[Any] = None
    presence_penalty: Optional[float] = 0
    frequency_penalty: Optional[float] = 0
    user: Optional[str] = None
    n: Optional[int] = 1


class LegacyCompletionRequest(BaseModel):
    model: str
    prompt: Any
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    stop: Optional[Any] = None
    user: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_error(
    message: str,
    type_: str = "invalid_request_error",
    code: Optional[str] = None,
    status: int = 400,
) -> JSONResponse:
    payload: Dict[str, Any] = {"message": message, "type": type_}
    if code:
        payload["code"] = code
    return JSONResponse(status_code=status, content={"error": payload})


def _completion_id() -> str:
    return f"chatcmpl-{int(time.time() * 1000)}"


def _now_ts() -> int:
    return int(time.time())


def _user_still_active(user_id: int) -> bool:
    """Return ``True`` if the user is still active.

    Streaming generators call this every N chunks so that an admin
    freeze mid-stream tears the connection down promptly instead of
    letting the upstream cost accumulate until the stream finishes
    naturally. Uses the connection pool so the per-chunk overhead is
    bounded.
    """
    from backend.database import get_db_context

    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_active FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return bool(row and row["is_active"])
    except Exception:
        # If the lookup itself fails, don't kill an otherwise healthy
        # stream — the freeze path also invalidates sessions and
        # releases reservations, so the next request will be blocked.
        return True


def _resolve_auth(
    authorization: Optional[str], x_api_key: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Extract the bearer token from either header and resolve it.

    Recognises two key shapes:

    * ``sk-ump_…`` — a user model-pool key.  Resolved to a synthetic
      ``info`` dict with ``is_model_pool_request=True`` and the
      resolved ``user_id``.  The actual upstream credentials are
      looked up per-request inside :func:`chat_completions` so the
      pool selection (priority / cooldown / max_tokens) is fresh on
      every call.
    * ``sk-…`` (everything else) — a managed platform API key, resolved
      via :func:`resolve_api_key` to the standard ``info`` dict.
    """
    raw = (authorization or x_api_key or "").strip()
    if not raw:
        return None
    # Strip the optional ``Bearer `` prefix the SDKs send.
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw.startswith("sk-ump_"):
        user_id = ModelPoolService.resolve_key(raw)
        if not user_id:
            return None
        return {
            "id": None,
            "user_id": user_id,
            "is_active": True,
            "is_model_pool_request": True,
            "allowed_models": [],
            "denied_models": [],
            "allowed_ips": [],
            "monthly_token_limit": None,
            "monthly_credit_limit": None,
            "source": "user_model_pool_key",
        }
    return resolve_api_key(raw)


def _is_user_admin(user_id: int) -> bool:
    """Return ``True`` when *user_id* matches an admin's username.

    Admin sessions don't carry a ``user_id``; the admin's ``users`` row
    is looked up by username.  When the OpenAI-compatible gateway is
    called with an admin's API key, the resolved ``user_id`` belongs to
    that auto-created admin user row.  We detect this by joining
    ``users.username`` against ``admin_users.username`` so the gateway
    can skip the wallet pre-flight for admin smoke-tests (mirrors the
    behaviour of :func:`backend.routes.chat.chat_send_stream`).
    """
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                  FROM users u
                  JOIN admin_users a ON a.username = u.username
                 WHERE u.id = ?
                 LIMIT 1
                """,
                (int(user_id),),
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


def _ensure_chat_capable_openai(model: str) -> Optional[JSONResponse]:
    """Return an OpenAI-style 400 error response if *model* is not chat-capable.

    Mirrors :func:`backend.routes.chat._ensure_chat_capable` but emits
    the OpenAI-standard error envelope so OpenAI SDKs surface the
    message correctly.  Detection defaults to ``chat`` for unknown
    ids, so a brand-new chat model is never blocked by mistake.

    Returns ``None`` when the model is chat-capable (caller proceeds);
    returns a ``JSONResponse`` otherwise (caller ``return``s it).
    """
    from backend.services.model_aggregator import detect_model_type

    model_type = detect_model_type(model)
    if model_type == "chat":
        return None
    labels = {
        "embedding": "向量嵌入（embedding）模型",
        "image": "图像生成模型",
        "audio": "语音/音频模型",
    }
    label = labels.get(model_type, model_type)
    return _openai_error(
        f"模型 {model} 是{label}，不能用于对话。",
        type_="invalid_request_error",
        code="model_not_chat_capable",
        status=400,
    )


def _enforce_ip_restrictions(info: Dict[str, Any], request: Request) -> None:
    """Raise 403 if the key has IP restrictions and the client IP
    is not on the allow-list.  Call this after ``_resolve_auth`` on
    every authenticated endpoint (GET *and* POST)."""
    allowed_ips = info.get("allowed_ips") or []
    if allowed_ips:
        client_ip = get_client_ip(request)
        if not _check_ip_allowed(client_ip, allowed_ips):
            raise HTTPException(status_code=403, detail="API Key 不允许该 IP")


def _extract_usage_from_response(payload: Dict[str, Any]) -> Tuple[int, int, int]:
    """Pull token counts from the various response shapes we may receive."""
    if not isinstance(payload, dict):
        return 0, 0, 0
    usage = payload.get("usage") or {}
    if not usage and "data" in payload and isinstance(payload["data"], dict):
        usage = payload["data"].get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return prompt, completion, total


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def authenticate(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    allowed_ips = info.get("allowed_ips") or []
    if allowed_ips:
        client_ip = get_client_ip(request)
        if not _check_ip_allowed(client_ip, allowed_ips):
            raise HTTPException(status_code=403, detail="API Key 不允许该 IP")
    return info


def _check_ip_allowed(client_ip: str, allowed_ips: list) -> bool:
    from ipaddress import ip_address, ip_network
    try:
        ip_obj = ip_address(client_ip)
    except Exception:
        return False
    for raw in allowed_ips:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            if "/" in s:
                if ip_obj in ip_network(s, strict=False):
                    return True
            else:
                if ip_obj == ip_address(s):
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# User model-pool dispatch
# ---------------------------------------------------------------------------


def _build_pool_upstream_url(api_base: str) -> str:
    """Compose the upstream chat-completions URL for a pool entry.

    ``api_base`` is the user-supplied base URL (already decrypted).
    Most OpenAI-compatible providers expect ``{base}/v1/chat/completions``,
    but some legacy bases already include the ``/v1`` prefix.  We append
    ``/v1/chat/completions`` only when the base doesn't already end with
    a chat-completions path.
    """
    base = (api_base or "").rstrip("/")
    if not base:
        return ""
    lower = base.lower()
    if lower.endswith("/chat/completions"):
        return base
    if lower.endswith("/v1"):
        return base + "/chat/completions"
    if lower.endswith("/v1/"):
        return base + "chat/completions"
    return base + "/v1/chat/completions"


async def _forward_pool_request(
    pool: Dict[str, Any],
    body: Dict[str, Any],
) -> Tuple[Any, int]:
    """Forward *body* to the pool's upstream and return (result, status).

    On a non-streaming call, returns ``(parsed_json_dict, status_code)``.
    On a streaming call, returns ``(httpx.Response, status_code)`` so the
    caller can iterate the SSE bytes — the response body is *not* pre-read.

    Raises ``httpx.HTTPError`` on network / timeout failures so the
    caller can mark the pool as failed and try the next one.
    """
    from backend.services.http_client import get_async_client

    api_base = pool.get("api_base") or ""
    api_key = pool.get("api_key") or ""
    model_name = pool.get("model_name") or body.get("model") or ""

    url = _build_pool_upstream_url(api_base)
    if not url:
        raise ValueError("pool entry has empty api_base")

    # Build the upstream payload.  Override the model with the pool's
    # configured ``model_name`` so the user can swap models without
    # changing their client's ``model`` field.
    upstream_body = dict(body)
    upstream_body["model"] = model_name

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    client = get_async_client()
    stream = bool(body.get("stream"))
    if stream:
        # Don't pre-read the body — the caller will iterate it.
        req = client.build_request("POST", url, json=upstream_body, headers=headers)
        resp = await client.send(req, stream=True)
        return resp, resp.status_code
    else:
        resp = await client.post(url, json=upstream_body, headers=headers)
        try:
            parsed = resp.json()
        except Exception:
            parsed = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
        return parsed, resp.status_code


async def _handle_model_pool_request(
    request: Request,
    info: Dict[str, Any],
    body: Dict[str, Any],
) -> JSONResponse | StreamingResponse:
    """Dispatch a ``sk-ump_`` request through the user's own model pool.

    Walks the user's pool entries in priority order, forwarding to each
    upstream in turn.  On 429 / 402 / network error, arms the cooldown
    and tries the next entry.  On success, records token usage and
    returns the upstream's response verbatim (re-shaped to OpenAI form
    for non-streaming, passed through for streaming).

    The platform's quota / billing pipeline is bypassed entirely: the
    user is paying their upstream directly with their own key, so the
    platform neither charges credits nor counts the request against the
    user's token quota.
    """
    user_id = int(info["user_id"])

    # Cap the failover attempts at the total number of active pool
    # entries so a misconfigured pool can't trap the loop.
    attempted: set = set()
    last_error: str = ""

    while True:
        # Exclude already-attempted pools so we don't loop on the same
        # failing upstream.  When every active pool has been tried,
        # ``get_next_model_for_user`` will keep returning the same
        # cooled-down entry — we detect that and bail.
        pool = ModelPoolService.get_next_model_for_user(user_id)
        if not pool:
            return _openai_error(
                "所有模型已用尽，请添加更多模型或等待 cooldown",
                type_="service_unavailable",
                code="all_pools_exhausted",
                status=503,
            )
        pool_id = int(pool["id"])
        if pool_id in attempted:
            return _openai_error(
                f"所有模型均不可用：{last_error or '未知错误'}",
                type_="service_unavailable",
                code="all_pools_failed",
                status=503,
            )
        attempted.add(pool_id)

        try:
            result, status_code = await _forward_pool_request(pool, body)
        except Exception as exc:
            logger.warning(
                "model pool forward failed for user=%s pool=%s: %s",
                user_id,
                pool_id,
                exc,
            )
            ModelPoolService.record_usage(
                pool_id, 0, success=False, error_msg=str(exc)
            )
            last_error = str(exc)
            continue

        # Streaming path — pass the SSE bytes through verbatim.
        if bool(body.get("stream")):
            if status_code in (429, 402):
                ModelPoolService.record_usage(
                    pool_id,
                    0,
                    success=False,
                    error_msg=f"HTTP {status_code}",
                )
                last_error = f"HTTP {status_code}"
                # Drain + close so the connection is released.
                try:
                    await result.aclose()
                except Exception:
                    pass
                continue

            # Success — wrap the upstream SSE stream.  We can't read
            # ``usage`` out of a streaming response without consuming
            # it, so we record tokens inside the generator's terminal
            # handler (best-effort).
            async def _pool_stream():
                total_tokens = 0
                try:
                    async for line in result.aiter_lines():
                        if not line:
                            yield b"\n"
                            continue
                        # Forward every line as-is (already SSE-shaped).
                        yield (line + "\n").encode("utf-8")
                        # Best-effort usage extraction from a ``data:``
                        # chunk that carries a ``usage`` field.
                        if line.startswith("data:") and "usage" in line:
                            try:
                                chunk = json.loads(line[5:].strip())
                                u = chunk.get("usage") or {}
                                total_tokens = int(u.get("total_tokens") or 0)
                            except Exception:
                                pass
                        if line.strip() == "data: [DONE]":
                            break
                finally:
                    try:
                        await result.aclose()
                    except Exception:
                        pass
                    ModelPoolService.record_usage(
                        pool_id, total_tokens, success=True
                    )

            return StreamingResponse(
                _pool_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        # Non-streaming path
        if status_code in (429, 402):
            ModelPoolService.record_usage(
                pool_id,
                0,
                success=False,
                error_msg=f"HTTP {status_code}",
            )
            last_error = f"HTTP {status_code}"
            continue

        if status_code != 200:
            # Other upstream errors (5xx, 4xx) — record failure and try
            # the next pool.  The user sees the last error if all pools
            # fail.
            err_msg = ""
            if isinstance(result, dict):
                err = result.get("error") or result.get("detail") or ""
                if isinstance(err, dict):
                    err_msg = err.get("message") or json.dumps(err, ensure_ascii=False)
                else:
                    err_msg = str(err)
            ModelPoolService.record_usage(
                pool_id,
                0,
                success=False,
                error_msg=f"HTTP {status_code}: {err_msg}"[:500],
            )
            last_error = f"HTTP {status_code}: {err_msg}"
            continue

        # Success — record usage and return the upstream response.
        usage = {}
        if isinstance(result, dict):
            usage = result.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        ModelPoolService.record_usage(pool_id, total_tokens, success=True)

        return JSONResponse(status_code=200, content=result)


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


def _model_to_openai(
    provider: str, model_id: str, created: int, pricing: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": provider,
    }
    # Always include pricing when known so the client SDK can show
    # the user the official (or admin-customised) price of the model
    # without an extra round-trip.
    if pricing:
        payload["pricing"] = {
            "input_per_1k": float(pricing.get("input_price_per_1k") or 0.0),
            "output_per_1k": float(pricing.get("output_price_per_1k") or 0.0),
            "currency": "credits",
            "tier": pricing.get("tier"),
            "is_custom": bool(pricing.get("is_custom")),
        }
    return payload


def _provider_display_name(provider: str) -> str:
    """Return the human-readable name for the owned_by field."""
    if provider.startswith("custom:"):
        try:
            from backend.services import custom_providers

            slug = provider.split(":", 1)[1]
            cfg = custom_providers.get_custom_provider(slug)
            if cfg and cfg.get("display_name"):
                return cfg["display_name"]
        except Exception:
            pass
        return provider
    try:
        from backend.providers.base import ProviderRegistry

        cls = ProviderRegistry.get(provider)
        if cls and getattr(cls, "display_name", ""):
            return cls.display_name
    except Exception:
        pass
    return provider


@router.get("/v1/models")
async def list_models(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        return _openai_error(
            "无效的 API Key", type_="invalid_request_error", code="invalid_api_key", status=401
        )
    _enforce_ip_restrictions(info, request)
    # Bump last_used_at for managed keys so the admin dashboard
    # shows a faithful "last seen" for every key — listing models
    # is part of normal client warm-up behaviour and should count.
    update_last_used(info.get("id"))

    sources = get_cached_provider_models()
    created = _now_ts()
    prefixed_ids: List[str] = []
    item_meta: Dict[str, Dict[str, Any]] = {}
    for entry in sources:
        provider = entry.get("provider") or ""
        owned_by = _provider_display_name(provider)
        for m in entry.get("models", []) or []:
            model_id = m.get("model_id") or m.get("id")
            if not model_id:
                continue
            raw = str(model_id)
            prefix = f"{provider}/"
            if raw.startswith(prefix):
                prefixed = raw
            else:
                prefixed = f"{prefix}{raw}"
            rejection = check_key_restrictions(info, prefixed)
            if rejection:
                continue
            prefixed_ids.append(prefixed)
            item_meta[prefixed] = {"owned_by": owned_by, "raw": raw}
    # Bulk-fetch effective pricing (admin custom > official default).
    pricing_map = get_pricing_for_model_list(prefixed_ids)
    items: List[Dict[str, Any]] = []
    for prefixed in prefixed_ids:
        meta = item_meta[prefixed]
        items.append(
            _model_to_openai(meta["owned_by"], prefixed, created, pricing=pricing_map.get(prefixed))
        )

    return {"object": "list", "data": items}


@router.get("/v1/models/{model_id:path}")
async def retrieve_model(
    request: Request,
    model_id: str,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        return _openai_error(
            "无效的 API Key", type_="invalid_request_error", code="invalid_api_key", status=401
        )
    _enforce_ip_restrictions(info, request)
    update_last_used(info.get("id"))
    rejection = check_key_restrictions(info, model_id)
    if rejection:
        return _openai_error(
            rejection, type_="invalid_request_error", code="model_not_allowed", status=403
        )
    provider = get_provider_for_model(model_id)
    # Surface the same effective pricing as /v1/models so the SDK can
    # cache a single price per model.
    pricing_map = get_pricing_for_model_list([model_id])
    return _model_to_openai(
        _provider_display_name(provider), model_id, _now_ts(), pricing=pricing_map.get(model_id)
    )


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        return _openai_error(
            "无效的 API Key", type_="invalid_request_error", code="invalid_api_key", status=401
        )
    update_last_used(info.get("id"))

    try:
        body = await request.json()
    except Exception:
        return _openai_error("请求体不是合法 JSON", status=400)

    if not isinstance(body, dict):
        return _openai_error("请求体必须是 JSON 对象", status=400)

    model = (body.get("model") or "").strip()
    if not model:
        return _openai_error("缺少 model 字段", code="missing_model", status=400)
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return _openai_error("messages 必须是非空数组", code="invalid_messages", status=400)
    if not all(isinstance(m, dict) and m.get("role") for m in messages):
        return _openai_error("messages 格式错误", code="invalid_messages", status=400)

    # Refuse non-chat models (embedding / image / audio) early so the
    # user gets a precise, localised error instead of a confusing 405
    # from the upstream.
    not_chat_err = _ensure_chat_capable_openai(model)
    if not_chat_err is not None:
        return not_chat_err

    # --- User model-pool dispatch ---------------------------------------
    # ``sk-ump_`` keys route to the user's own upstream credentials.
    # The platform's quota / billing pipeline is bypassed entirely —
    # the user pays their upstream directly.  We do NOT fall through
    # to the platform path on pool failure: a 503 is returned so the
    # client can retry (potentially after the cooldown elapses).
    if info.get("is_model_pool_request"):
        return await _handle_model_pool_request(request, info, body)

    rejection = check_key_restrictions(info, model)
    if rejection:
        return _openai_error(
            rejection, type_="invalid_request_error", code="model_not_allowed", status=403
        )

    user_id = int(info["user_id"])
    provider = get_provider_for_model(model)

    # Comprehensive pre-flight quota check (5h/week/month tokens,
    # monthly budget, RPM/TPM rate limits) — same gate used by proxy.py.
    from backend.services import quota_service as _qs

    # Admins smoke-testing their own platform via API key should not be
    # blocked by the wallet pre-flight — their auto-created ``users``
    # row has near-infinite quota but no wallet balance.  Mirrors the
    # behaviour of :func:`backend.routes.chat.chat_send_stream`.
    is_admin_caller = _is_user_admin(user_id)

    can_use, message = _qs.assert_request_allowed(
        user_id=user_id,
        provider=provider,
        model=model,
        estimated_tokens=int(body.get("max_tokens") or 2048),
    )
    if not can_use:
        # Distinguish "balance empty" from "quota exhausted" so admin
        # smoke-tests aren't blocked by the wallet check inside
        # ``assert_request_allowed``.  The quota dimensions (5h/week/
        # month/budget/rate-limit) still apply.
        if is_admin_caller and "余额" in (message or ""):
            pass  # fall through — admin bypasses balance gate
        else:
            return _openai_error(
                message or "配额不足",
                type_="insufficient_quota",
                code="insufficient_quota",
                status=429,
            )

    # Balance pre-check: refuse early when the wallet is empty.
    # Admins skip this — they have no wallet to charge.
    if not is_admin_caller:
        wallet = get_wallet(user_id)
        if float(wallet.get("balance") or 0) <= 0:
            return _openai_error(
                "余额不足，请先充值", type_="insufficient_quota", code="insufficient_quota", status=402
            )

    # M8: Best-effort quota warning notification. Must run *after* the
    # request has been admitted (so we don't warn on rejected requests)
    # and *before* the reservation for this request is taken (so the
    # current request's own reservation doesn't inflate the percentage).
    # All failures are swallowed inside maybe_warn_on_quota.
    if not is_admin_caller:
        try:
            _qs.maybe_warn_on_quota(user_id)
        except Exception:
            pass

    # Reserve the estimated token count so a sibling concurrent request
    # from the same user sees the pending commitment (via
    # snap['reserved_tokens'] in the next assert_request_allowed call)
    # instead of racing past the quota. Released inside the streaming
    # generator's finally block (when it exhausts or the client
    # disconnects) and at each non-streaming return site.
    _estimated_tokens_for_quota = int(body.get("max_tokens") or 2048)
    request_id = uuid4().hex
    _qs.reserve_quota_reservation(user_id, _estimated_tokens_for_quota, request_id=request_id)

    stream = bool(body.get("stream"))
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "max_tokens": body.get("max_tokens") or 2048,
        "temperature": body.get("temperature", 0.7),
        "top_p": body.get("top_p", 1.0),
        "stop": body.get("stop"),
        "user": body.get("user"),
    }

    user_agent = request.headers.get("user-agent") or ""
    client_ip = get_client_ip(request)
    started = time.time()

    if stream:
        # --- Streaming idempotency -----------------------------------------
        # Clients (e.g. OpenAI SDK) may retry streams with the same
        # Idempotency-Key when their connection drops mid-stream.  We
        # cache a completion marker with token counts so retries get a
        # short synthesised replay instead of a full re-stream.
        idem_key = (
            request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key") or ""
        )
        if idem_key:
            stable_body = {
                "model": model,
                "messages": messages,
                "max_tokens": body.get("max_tokens"),
                "temperature": body.get("temperature"),
            }
            stream_idem = idempotency.check_or_reserve_stream(
                key=idem_key,
                method="POST",
                route="/v1/chat/completions",
                body=stable_body,
            )
            if stream_idem.status == "body_mismatch":
                _qs.release_quota_reservation(user_id, request_id=request_id)
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "message": "Idempotency-Key 已被用于不同的请求体",
                            "type": "invalid_request_error",
                            "code": "IDEMPOTENCY_KEY_REUSED",
                        }
                    },
                )
            if stream_idem.status == "in_progress":
                _qs.release_quota_reservation(user_id, request_id=request_id)
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "message": "A stream with this Idempotency-Key is already in progress",
                            "type": "conflict_error",
                            "code": "IDEMPOTENCY_STREAM_IN_PROGRESS",
                        }
                    },
                )
            if stream_idem.status == "completed":
                # Return a short synthesised completion with cached usage.
                cached = stream_idem.cached_usage or {}
                synth_chunk = {
                    "id": _completion_id(),
                    "object": "chat.completion.chunk",
                    "created": _now_ts(),
                    "model": cached.get("model", model),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                usage_chunk = {
                    "id": _completion_id(),
                    "object": "chat.completion.chunk",
                    "created": _now_ts(),
                    "model": cached.get("model", model),
                    "choices": [],
                    "usage": {
                        "prompt_tokens": int(cached.get("prompt_tokens") or 0),
                        "completion_tokens": int(cached.get("completion_tokens") or 0),
                        "total_tokens": int(cached.get("total_tokens") or 0),
                    },
                }

                async def _replay_iter():
                    yield _sse_data(synth_chunk)
                    yield _sse_data(usage_chunk)
                    yield b"data: [DONE]\n\n"

                _qs.release_quota_reservation(user_id, request_id=request_id)
                return StreamingResponse(
                    _replay_iter(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    },
                )
            # status == "proceed" — key reserved, fall through to stream.
        else:
            idem_key = ""

        # --- streaming pre-reserve ---
        # Cap the reserve at the user's actual balance so a cheap request
        # is not rejected just because max_tokens implies a higher worst case.
        # Admins skip the wallet pre-reserve entirely — they have no
        # wallet to charge (mirrors chat.py's admin bypass).
        prompt_token_estimate = max(100, sum(len(m.get("content") or "") for m in messages) // 4)
        max_output_tokens = int(payload.get("max_tokens") or 2048)
        max_cost = _max_stream_cost(
            user_id, provider, model, max_output_tokens, prompt_token_estimate
        )
        if is_admin_caller:
            cost_reserve = 0.0
            wallet_balance = float((get_wallet(user_id) or {}).get("balance") or 0.0)
        else:
            wallet_balance = float((get_wallet(user_id) or {}).get("balance") or 0.0)
            cost_reserve = min(max_cost, wallet_balance) if max_cost > 0 else 0.0

        if cost_reserve > 0:
            try:
                update_wallet(
                    user_id,
                    -cost_reserve,
                    "reserve",
                    related_type="stream_pre_reserve",
                    related_id=None,
                    note=f"{provider}/{model}",
                )
            except ValueError:
                return _openai_error(
                    "余额不足，请先充值",
                    type_="insufficient_quota",
                    code="insufficient_quota",
                    status=402,
                )
        elif not is_admin_caller and wallet_balance <= 0:
            return _openai_error(
                "余额不足，请先充值",
                type_="insufficient_quota",
                code="insufficient_quota",
                status=402,
            )

        return await _stream_chat_response(
            user_id=user_id,
            info=info,
            token_id=info.get("id"),
            provider=provider,
            model=model,
            payload=payload,
            user_agent=user_agent,
            client_ip=client_ip,
            started=started,
            max_cost_reserve=cost_reserve,
            idem_key=idem_key,
            request=request,
            request_id=request_id,
        )

    # --- Non-streaming pre-reserve ----------------------------------------
    # Reserve worst-case cost before dispatching to prevent concurrent
    # requests from draining the wallet below zero (TOCTOU fix).
    # Admins skip the wallet pre-reserve entirely — they have no wallet
    # to charge (mirrors chat.py's admin bypass).
    prompt_token_estimate = max(100, sum(len(m.get("content") or "") for m in messages) // 4)
    max_output_tokens_ns = int(payload.get("max_tokens") or 2048)
    max_cost_ns = _max_stream_cost(
        user_id, provider, model, max_output_tokens_ns, prompt_token_estimate
    )
    if is_admin_caller:
        cost_reserve_ns = 0.0
        wallet_balance_ns = float((get_wallet(user_id) or {}).get("balance") or 0.0)
    else:
        wallet_balance_ns = float((get_wallet(user_id) or {}).get("balance") or 0.0)
        cost_reserve_ns = min(max_cost_ns, wallet_balance_ns) if max_cost_ns > 0 else 0.0

    if cost_reserve_ns > 0:
        try:
            update_wallet(
                user_id,
                -cost_reserve_ns,
                "reserve",
                related_type="api_pre_reserve",
                related_id=None,
                note=f"{provider}/{model}",
            )
        except ValueError:
            return _openai_error(
                "余额不足，请先充值",
                type_="insufficient_quota",
                code="insufficient_quota",
                status=402,
            )
    elif not is_admin_caller and wallet_balance_ns <= 0:
        return _openai_error(
            "余额不足，请先充值",
            type_="insufficient_quota",
            code="insufficient_quota",
            status=402,
        )

    # --- Idempotency (non-streaming only) -----------------------------------
    idem_key = (
        request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key") or ""
    )
    if idem_key:
        idem_result = idempotency.check_or_reserve(
            key=idem_key,
            method="POST",
            route="/v1/chat/completions",
            body=body,
        )
        if idem_result.hit:
            # Refund pre-reserve since we're returning cached response
            if cost_reserve_ns > 0:
                try:
                    update_wallet(
                        user_id,
                        cost_reserve_ns,
                        "refund",
                        related_type="api_idempotent_refund",
                        related_id=None,
                        note=f"{provider}/{model} idempotent cache hit",
                    )
                except Exception:
                    logger.warning("Failed to refund pre-reserve on idempotent hit for user %s", user_id)
            _qs.release_quota_reservation(user_id, request_id=request_id)
            return JSONResponse(
                status_code=idem_result.status_code,
                content=idem_result.response_body,
            )

    try:
        response = await _non_stream_chat_response(
            user_id=user_id,
            token_id=info.get("id"),
            provider=provider,
            model=model,
            payload=payload,
            user_agent=user_agent,
            client_ip=client_ip,
            started=started,
            max_cost_reserve=cost_reserve_ns,
            skip_billing=is_admin_caller,
        )
    except Exception:
        # Upstream blew up — release the reservation so the client can
        # safely retry with the same Idempotency-Key.
        _qs.release_quota_reservation(user_id, request_id=request_id)
        if idem_key:
            idempotency.release(
                key=idem_key,
                method="POST",
                route="/v1/chat/completions",
            )
        raise

    # Persist the response for future retries.
    if idem_key:
        try:
            # JSONResponse.body is raw bytes — decode to a dict so the
            # idempotency store can json.dumps / json.loads it cleanly.
            try:
                cached_body = json.loads(response.body)
            except Exception:
                cached_body = response.body.decode("utf-8", errors="replace")
            idempotency.finalize(
                key=idem_key,
                method="POST",
                route="/v1/chat/completions",
                status_code=response.status_code,
                response_body=cached_body,
            )
        except Exception:
            # Finalize is best-effort; a failure here must not mask a
            # successful upstream response.
            logger.warning("idempotency finalize failed for chat_completions", exc_info=True)

    _qs.release_quota_reservation(user_id, request_id=request_id)
    return response


# ----- non-stream ----------------------------------------------------------


async def _non_stream_chat_response(
    *,
    user_id: int,
    token_id: Optional[int],
    provider: str,
    model: str,
    payload: Dict[str, Any],
    user_agent: str,
    client_ip: str,
    started: float,
    max_cost_reserve: float = 0.0,
    skip_billing: bool = False,
) -> JSONResponse:
    # `proxy_service.forward_request` does its own add_usage_log + billing,
    # but we additionally want to (a) charge the wallet and (b) override
    # the response shape to the OpenAI standard.
    _fwd_ret = await ProxyService.forward_request(
        user_id,
        payload,
        provider,
        token_id=token_id,
    )
    result, status_code, _usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)
    if status_code != 200 or not isinstance(result, dict):
        # Reconcile: refund the pre-reserve on upstream error
        if max_cost_reserve > 0:
            try:
                update_wallet(
                    user_id,
                    max_cost_reserve,
                    "refund",
                    related_type="api_error_refund",
                    related_id=None,
                    note=f"{provider}/{model} upstream error refund",
                )
            except Exception:
                logger.warning("Failed to refund pre-reserve on error for user %s", user_id)
        return _build_upstream_error_response(result, status_code)

    prompt_tokens, completion_tokens, total_tokens = _extract_usage_from_response(result)
    int((time.time() - started) * 1000)

    # Charge wallet — if we pre-reserved, reconcile actual vs reserved.
    # ``skip_billing`` is set for admin callers (no wallet to charge).
    quote = quote_cost(user_id, provider, model, prompt_tokens, completion_tokens)
    cost = float(quote.get("cost_credits") or 0)
    if not skip_billing:
        if max_cost_reserve > 0:
            delta = max_cost_reserve - cost
            if delta > 0.0001:
                # Refund unused portion of the reservation
                try:
                    update_wallet(
                        user_id,
                        delta,
                        "refund",
                        related_type="api_reconcile",
                        related_id=None,
                        note=f"{provider}/{model} reconcile refund",
                    )
                except Exception:
                    logger.warning("Failed to reconcile refund for user %s", user_id)
            elif delta < -0.0001:
                # Actual cost exceeded reservation — charge the difference
                try:
                    update_wallet(
                        user_id,
                        delta,
                        "consume",
                        related_type="api_reconcile",
                        related_id=None,
                        note=f"{provider}/{model} reconcile charge",
                    )
                except ValueError:
                    return _openai_error(
                        "余额不足，请先充值",
                        type_="insufficient_quota",
                        code="insufficient_quota",
                        status=402,
                    )
        elif cost > 0:
            # No pre-reservation (free model or unpriced) — direct charge
            try:
                update_wallet(
                    user_id,
                    -cost,
                    "consume",
                    related_type="api",
                    related_id=None,
                    note=f"{provider}/{model}",
                )
            except ValueError:
                return _openai_error(
                    "余额不足，请先充值",
                    type_="insufficient_quota",
                    code="insufficient_quota",
                    status=402,
                )

    # Backfill cost_credits on the usage log so the consumption dashboard
    # and CSV exports reflect the actual credit cost.
    if cost > 0 and _usage_log_id:
        try:
            update_usage_log_cost(_usage_log_id, cost)
        except Exception:
            logger.warning("Failed to backfill cost_credits on usage_log %s", _usage_log_id)

    # Build OpenAI-shape envelope.
    choices = result.get("choices") or []
    if not choices:
        # Some upstreams (e.g. older Anthropic-shaped responses) embed
        # the content under a different key. Fall back to .content / .text.
        content = result.get("content") or ""
        if not content and "data" in result:
            data = result.get("data") or {}
            if isinstance(data, dict) and data.get("outputs"):
                content = (data["outputs"][0] or {}).get("text", "") or ""
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": result.get("finish_reason") or "stop",
            }
        ]

    body = {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": _now_ts(),
        "model": model,
        "choices": [_normalise_choice(c, model) for c in choices],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "system_fingerprint": f"fp_{hashlib.sha256(provider.encode()).hexdigest()[:12]}",
    }
    return JSONResponse(status_code=200, content=body)


def _normalise_choice(choice: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Coerce a possibly messy upstream choice dict into OpenAI shape."""
    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        # Anthropic / older providers may stash the text in `text`.
        content = choice.get("text") or ""
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    return {
        "index": int(choice.get("index") or 0),
        "message": {
            "role": message.get("role") or "assistant",
            "content": content,
        },
        "finish_reason": choice.get("finish_reason") or "stop",
    }


def _build_upstream_error_response(result: Any, status_code: int) -> JSONResponse:
    if isinstance(result, dict):
        message = result.get("error") or result.get("detail") or "上游服务出错"
        if isinstance(message, dict):
            message = message.get("message") or json.dumps(message, ensure_ascii=False)
        code = result.get("code") or status_code
    else:
        message = str(result) if result else "上游服务出错"
        code = status_code
    type_ = "invalid_request_error" if status_code in (400, 403) else "upstream_error"
    return _openai_error(
        str(message),
        type_=type_,
        code=str(code) if code is not None else None,
        status=status_code or 502,
    )


# ----- stream --------------------------------------------------------------


def _max_stream_cost(
    user_id: int, provider: str, model: str, max_output_tokens: int, prompt_tokens: int
) -> float:
    """Worst-case credit cost for a streaming request.

    Uses the model's per-1k-token pricing with the caller's ``max_tokens``
    cap for output and a conservative estimate for input. Returns 0.0 when
    pricing is missing so free-tier / unpriced models keep working.
    """
    pricing = get_model_pricing(provider, model) or {}
    in_price = float(pricing.get("input_price_per_1k") or 0.0)
    out_price = float(pricing.get("output_price_per_1k") or 0.0)
    if out_price <= 0 and in_price <= 0:
        return 0.0
    plan = get_user_plan(user_id)
    discount = float(plan.get("discount_rate") or 1.0)
    cost = (float(prompt_tokens) / 1000.0) * in_price + (
        float(max_output_tokens) / 1000.0
    ) * out_price
    return round(cost * discount, 6)


async def _stream_chat_response(
    *,
    user_id: int,
    info: Dict[str, Any],
    token_id: Optional[int],
    provider: str,
    model: str,
    payload: Dict[str, Any],
    user_agent: str,
    client_ip: str,
    started: float,
    max_cost_reserve: float = 0.0,
    idem_key: str = "",
    request: Optional[Request] = None,
    request_id: str = "",
) -> StreamingResponse:
    from backend.services import quota_service as _qs

    completion_id = _completion_id()
    created_ts = _now_ts()

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    errored: Optional[str] = None
    response_status = 200
    has_charged = False
    reconciled = False
    key_id_for_health: Optional[str] = None
    _stream_log_id: Optional[int] = None
    accumulated_text = ""

    async def event_iter() -> AsyncIterator[bytes]:
        nonlocal prompt_tokens, completion_tokens, total_tokens, errored
        nonlocal response_status, has_charged, reconciled, key_id_for_health, _stream_log_id
        nonlocal accumulated_text
        # ``done`` flips this so we don't double-emit a final chunk
        # when the upstream errored mid-stream. Without this guard
        # the wire format becomes ``error → done → [DONE]`` which
        # most OpenAI SDKs interpret as a *successful* stream and
        # silently drop the error — a sharp edge we want to avoid.
        terminated = False
        key_id_for_health = key_pool.get_key(provider)

        first_chunk = True
        # Check whether the user has been frozen mid-stream every N
        # deltas. An admin freeze sets ``is_active=0`` synchronously,
        # but without this check the in-flight stream would keep
        # consuming upstream tokens until it finishes naturally.
        _chunk_count = 0
        async for raw in ProxyService.stream_chat(user_id, payload, provider, token_id=token_id):
            event, data = _parse_internal_sse(raw)
            if event is None:
                continue
            if terminated:
                # An error already closed the stream; keep draining
                # the upstream so the connection tears down cleanly
                # but don't emit anything else to the caller.
                continue
            if event == "delta":
                if first_chunk:
                    first_chunk = False
                    if key_id_for_health:
                        key_pool.mark_success(provider, key_id_for_health)
                content = (data or {}).get("content") or ""
                accumulated_text += content
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": None,
                        }
                    ],
                }
                yield _sse_data(chunk)
                _chunk_count += 1
                if _chunk_count % 50 == 0 and not _user_still_active(user_id):
                    err_chunk = {
                        "id": completion_id,
                        "object": "error",
                        "created": created_ts,
                        "model": model,
                        "error": {
                            "message": "账户已被冻结",
                            "type": "account_frozen",
                        },
                    }
                    yield _sse_data(err_chunk)
                    terminated = True
                    continue
            elif event == "done":
                usage = (data or {}).get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                # Final chunk (finish_reason: stop, empty delta) per OpenAI.
                final = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield _sse_data(final)
                # Wallet was pre-reserved before any chunk was yielded.
                # Actual-vs-reserve reconciliation runs after the generator
                # drains, so we can safely mark "charged" here.
                has_charged = True
                # Cache completion marker for streaming idempotency.
                if idem_key:
                    try:
                        idempotency.finalize_stream(
                            key=idem_key,
                            method="POST",
                            route="/v1/chat/completions",
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            model=model,
                        )
                    except Exception:
                        logger.warning("stream idempotency finalize failed", exc_info=True)
                # Record usage log now that the stream completed successfully.
                try:
                    _stream_log_id = add_usage_log(
                        user_id=user_id,
                        endpoint="/v1/chat/completions (stream)",
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        response_time_ms=int((time.time() - started) * 1000),
                        status_code=response_status,
                        ip_address=client_ip,
                    )
                except Exception as e:
                    logger.warning("Failed to add usage log: %s", e)
                yield b"data: [DONE]\n\n"
            elif event == "error":
                errored = (data or {}).get("error") or "stream error"
                response_status = int((data or {}).get("code") or 500)
                err_chunk = {
                    "id": completion_id,
                    "object": "error",
                    "created": created_ts,
                    "model": model,
                    "error": {
                        "message": str(errored),
                        "type": "upstream_error",
                        "code": "stream_error",
                    },
                }
                yield _sse_data(err_chunk)
                if key_id_for_health:
                    key_pool.mark_failure(provider, key_id_for_health, errored)
                terminated = True
                # Release streaming idempotency reservation so the
                # client can retry with the same key.
                if idem_key:
                    idempotency.release(
                        key=idem_key,
                        method="POST",
                        route="/v1/chat/completions",
                    )
                # Same reasoning as above: don't follow an error with
                # the happy-path terminator.

        # Write the usage log once the stream completes.
        if not has_charged and not errored:
            # If we never got a done event, still record a usage log so
            # the request isn't silently lost.
            try:
                _stream_log_id = add_usage_log(
                    user_id=user_id,
                    endpoint="/v1/chat/completions (stream)",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    response_time_ms=int((time.time() - started) * 1000),
                    status_code=response_status,
                    ip_address=client_ip,
                )
            except Exception as e:
                logger.warning("Failed to add usage log: %s", e)

        if not reconciled and max_cost_reserve > 0:
            reconciled = True
            try:
                reconcile_stream_reserve(
                    user_id=user_id,
                    provider=provider,
                    model=model,
                    cost_reserve=max_cost_reserve,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_log_id=_stream_log_id,
                    messages=payload.get("messages"),
                    accumulated_text=accumulated_text,
                    stream_completed=has_charged,
                )
            except Exception:
                logger.exception(
                    "stream reconcile failed for user %s, reserve=%s",
                    user_id,
                    max_cost_reserve,
                )

        # Release the per-user token reservation now that actual usage
        # has been committed to usage_logs. A sibling concurrent
        # request that lands right after this point will see the real
        # used-tokens total (not the estimate we reserved earlier).
        _qs.release_quota_reservation(user_id, request_id=request_id)

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse_data(obj: Any) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _parse_internal_sse(raw: bytes) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Convert a single `event: <name>\\ndata: <json>` block from the
    internal `proxy_service.stream_chat` output to a (event, dict) pair."""
    if not raw:
        return None, None
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return None, None
    event = None
    data_line: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_line = line[len("data:") :].strip()
    if not event or data_line is None:
        return None, None
    try:
        data = json.loads(data_line)
    except Exception:
        data = {"raw": data_line}
    return event, data


# ---------------------------------------------------------------------------
# /v1/completions  (legacy)
# ---------------------------------------------------------------------------


@router.post("/v1/completions")
async def legacy_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        return _openai_error(
            "无效的 API Key", type_="invalid_request_error", code="invalid_api_key", status=401
        )
    update_last_used(info.get("id"))

    try:
        body = await request.json()
    except Exception:
        return _openai_error("请求体不是合法 JSON", status=400)
    if not isinstance(body, dict):
        return _openai_error("请求体必须是 JSON 对象", status=400)

    model = (body.get("model") or "").strip()
    if not model:
        return _openai_error("缺少 model 字段", status=400)
    prompt = body.get("prompt")
    if prompt is None:
        return _openai_error("缺少 prompt 字段", status=400)

    rejection = check_key_restrictions(info, model)
    if rejection:
        return _openai_error(
            rejection, type_="invalid_request_error", code="model_not_allowed", status=403
        )

    # Wrap the legacy prompt into a chat-style message list.
    prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    chat_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": body.get("max_tokens") or 512,
        "temperature": body.get("temperature", 0.7),
        "top_p": body.get("top_p", 1.0),
        "stop": body.get("stop"),
        "stream": bool(body.get("stream")),
    }
    user_id = int(info["user_id"])

    provider = get_provider_for_model(model)

    # Comprehensive pre-flight quota check (5h/week/month tokens,
    # monthly budget, RPM/TPM rate limits) — same gate used by chat_completions.
    from backend.services import quota_service as _qs

    can_use, message = _qs.assert_request_allowed(
        user_id=user_id,
        provider=provider,
        model=model,
        estimated_tokens=int(chat_body.get("max_tokens") or 512),
    )
    if not can_use:
        return _openai_error(
            message or "配额不足",
            type_="insufficient_quota",
            code="insufficient_quota",
            status=429,
        )

    wallet = get_wallet(user_id)
    if float(wallet.get("balance") or 0) <= 0:
        return _openai_error(
            "余额不足，请先充值", type_="insufficient_quota", code="insufficient_quota", status=402
        )

    # M8: Best-effort quota warning notification (mirrors chat_completions).
    try:
        _qs.maybe_warn_on_quota(user_id)
    except Exception:
        pass

    # Per-user token reservation — mirrors the chat_completions path.
    # Released at the end of _legacy_stream_completions' event_iter
    # (streaming) and at each non-stream return site below.
    request_id = uuid4().hex
    _qs.reserve_quota_reservation(
        user_id, int(chat_body.get("max_tokens") or 512), request_id=request_id
    )

    if chat_body["stream"]:
        # Pre-reserve billing for the legacy stream, mirroring the chat
        # completions path: cap reserve at wallet balance, reconcile after.
        prompt_text_for_estimate = (
            prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
        )
        prompt_token_estimate = max(100, len(prompt_text_for_estimate) // 4)
        max_output_tokens = int(chat_body.get("max_tokens") or 512)
        max_cost = _max_stream_cost(
            user_id, provider, model, max_output_tokens, prompt_token_estimate
        )
        wallet_balance = float((get_wallet(user_id) or {}).get("balance") or 0.0)
        cost_reserve = min(max_cost, wallet_balance) if max_cost > 0 else 0.0

        if cost_reserve > 0:
            try:
                update_wallet(
                    user_id,
                    -cost_reserve,
                    "reserve",
                    related_type="stream_pre_reserve",
                    related_id=None,
                    note=f"{provider}/{model}",
                )
            except ValueError:
                return _openai_error(
                    "余额不足，请先充值",
                    type_="insufficient_quota",
                    code="insufficient_quota",
                    status=402,
                )
        elif wallet_balance <= 0:
            return _openai_error(
                "余额不足，请先充值",
                type_="insufficient_quota",
                code="insufficient_quota",
                status=402,
            )

        # Reuse the streaming chat machinery, then re-shape the chunks
        # to look like a legacy completion.
        return await _legacy_stream_completions(
            user_id=user_id,
            provider=provider,
            model=model,
            chat_payload=chat_body,
            request=request,
            token_id=info.get("id"),
            max_cost_reserve=cost_reserve,
            request_id=request_id,
        )

    _fwd_ret = await ProxyService.forward_request(
        user_id,
        {
            "model": model,
            "messages": chat_body["messages"],
            "stream": False,
            "max_tokens": chat_body["max_tokens"],
            "temperature": chat_body["temperature"],
        },
        provider,
        token_id=info.get("id"),
    )
    result, status_code, _usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)
    if status_code != 200 or not isinstance(result, dict):
        _qs.release_quota_reservation(user_id, request_id=request_id)
        return _build_upstream_error_response(result, status_code)

    prompt_tokens, completion_tokens, total_tokens = _extract_usage_from_response(result)
    quote = quote_cost(user_id, provider, model, prompt_tokens, completion_tokens)
    cost = float(quote.get("cost_credits") or 0)
    if cost > 0:
        try:
            update_wallet(
                user_id,
                -cost,
                "consume",
                related_type="api",
                related_id=None,
                note=f"{provider}/{model}",
            )
        except ValueError:
            _qs.release_quota_reservation(user_id, request_id=request_id)
            return _openai_error(
                "余额不足，请先充值",
                type_="insufficient_quota",
                code="insufficient_quota",
                status=402,
            )
        if _usage_log_id:
            try:
                update_usage_log_cost(_usage_log_id, cost)
            except Exception:
                logger.warning("Failed to backfill cost_credits on usage_log %s", _usage_log_id)

    # Build the legacy text-completions envelope.
    choices = result.get("choices") or []
    text_out = ""
    if choices:
        ch = choices[0]
        message = ch.get("message") or {}
        text_out = message.get("content") or ch.get("text") or ""
    body_out = {
        "id": _completion_id(),
        "object": "text_completion",
        "created": _now_ts(),
        "model": model,
        "choices": [
            {
                "text": text_out,
                "index": 0,
                "logprobs": None,
                "finish_reason": (choices[0].get("finish_reason") if choices else None) or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
    _qs.release_quota_reservation(user_id, request_id=request_id)
    return JSONResponse(status_code=200, content=body_out)


async def _legacy_stream_completions(
    *,
    user_id: int,
    provider: str,
    model: str,
    chat_payload: Dict[str, Any],
    request: Request,
    token_id: Optional[int] = None,
    max_cost_reserve: float = 0.0,
    request_id: str = "",
) -> StreamingResponse:
    from backend.services import quota_service as _qs

    completion_id = _completion_id()
    created_ts = _now_ts()
    client_ip = get_client_ip(request)
    started = time.time()
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    has_charged = False
    reconciled = False
    _stream_log_id: Optional[int] = None
    accumulated_text = ""

    async def event_iter() -> AsyncIterator[bytes]:
        nonlocal prompt_tokens, completion_tokens, total_tokens, has_charged
        nonlocal reconciled, _stream_log_id, accumulated_text
        # Mirror the chat stream fix: once we emit an error chunk
        # we stop yielding anything else so the wire format stays
        # unambiguous. Without this guard the SDK sees
        # ``error → done → [DONE]`` and treats the request as
        # successful.
        terminated = False
        _legacy_kwargs = {"token_id": token_id}
        if request is not None:
            _legacy_kwargs["request"] = request
        # Check whether the user has been frozen mid-stream every N
        # deltas so an admin freeze tears the connection down promptly.
        _chunk_count = 0
        async for raw in ProxyService.stream_chat(
            user_id, chat_payload, provider, **_legacy_kwargs
        ):
            event, data = _parse_internal_sse(raw)
            if event is None:
                continue
            if terminated:
                continue
            if event == "delta":
                content = (data or {}).get("content") or ""
                accumulated_text += content
                chunk = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created_ts,
                    "model": model,
                    "choices": [
                        {"text": content, "index": 0, "logprobs": None, "finish_reason": None}
                    ],
                }
                yield _sse_data(chunk)
                _chunk_count += 1
                if _chunk_count % 50 == 0 and not _user_still_active(user_id):
                    yield _sse_data(
                        {
                            "error": {
                                "message": "账户已被冻结",
                                "type": "account_frozen",
                            },
                        }
                    )
                    terminated = True
                    continue
            elif event == "done":
                usage = (data or {}).get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
                # Billing is handled by pre-reserve + reconcile (below),
                # NOT inline.  Just mark the stream as successfully done.
                has_charged = True
                # Record usage log now that the stream completed successfully.
                try:
                    _stream_log_id = add_usage_log(
                        user_id=user_id,
                        endpoint="/v1/completions (stream)",
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        response_time_ms=int((time.time() - started) * 1000),
                        status_code=200,
                        ip_address=client_ip,
                    )
                except Exception as e:
                    logger.warning("Failed to add usage log: %s", e)
                yield b"data: [DONE]\n\n"
            elif event == "error":
                yield _sse_data(
                    {
                        "error": {
                            "message": (data or {}).get("error", "stream error"),
                            "type": "upstream_error",
                            "code": "stream_error",
                        },
                    }
                )
                terminated = True

        if not has_charged:
            try:
                _stream_log_id = add_usage_log(
                    user_id=user_id,
                    endpoint="/v1/completions (stream)",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    response_time_ms=int((time.time() - started) * 1000),
                    status_code=200,
                    ip_address=client_ip,
                )
            except Exception as e:
                logger.warning("Failed to add usage log: %s", e)

        if not reconciled and max_cost_reserve > 0:
            reconciled = True
            try:
                reconcile_stream_reserve(
                    user_id=user_id,
                    provider=provider,
                    model=model,
                    cost_reserve=max_cost_reserve,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_log_id=_stream_log_id,
                    messages=chat_payload.get("messages"),
                    accumulated_text=accumulated_text,
                    stream_completed=has_charged,
                )
            except Exception:
                logger.exception(
                    "stream reconcile failed for user %s, reserve=%s",
                    user_id,
                    max_cost_reserve,
                )

        # Release the per-user token reservation now that actual usage
        # has been committed to usage_logs.
        _qs.release_quota_reservation(user_id, request_id=request_id)

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# /v1/usage
# ---------------------------------------------------------------------------


@router.get("/v1/usage")
async def get_usage(
    request: Request,
    month: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    info = _resolve_auth(authorization, x_api_key)
    if not info:
        return _openai_error(
            "无效的 API Key", type_="invalid_request_error", code="invalid_api_key", status=401
        )
    _enforce_ip_restrictions(info, request)
    user_id = int(info["user_id"])

    # Historical query: ``?month=YYYY-MM`` returns the aggregate for an
    # arbitrary past month. We validate the shape strictly so a typo
    # doesn't silently fall through to the current-month summary.
    # Current-month (no ``month`` param) keeps using the shared
    # ``get_monthly_summary`` helper so other callers stay consistent.
    if month:
        import re

        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return _openai_error(
                "month 参数格式应为 YYYY-MM",
                type_="invalid_request_error",
                code="invalid_month_format",
                status=400,
            )
        try:
            # Round-trip through the parser so ``2026-13`` is rejected
            # instead of silently returning zero rows.
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return _openai_error(
                "month 参数不是有效的年月",
                type_="invalid_request_error",
                code="invalid_month_value",
                status=400,
            )
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(SUM(prompt_tokens), 0)     AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COALESCE(SUM(total_tokens), 0)      AS total,
                       COALESCE(SUM(cost_credits), 0)      AS cost
                  FROM usage_logs
                 WHERE user_id = ?
                   AND strftime('%Y-%m', request_time) = ?
                """,
                (user_id, month),
            )
            row = cursor.fetchone()

        def _val(key: str, default=0):
            try:
                v = row[key] if row is not None else None
            except (IndexError, KeyError):
                v = None
            return v if v is not None else default

        summary = {
            "prompt_tokens": int(_val("prompt", 0) or 0),
            "completion_tokens": int(_val("completion", 0) or 0),
            "total_tokens": int(_val("total", 0) or 0),
            "total_cost": float(_val("cost", 0.0) or 0.0),
        }
        period = month
    else:
        summary = get_monthly_summary(user_id)
        # ``datetime.utcnow()`` is deprecated in Python 3.12+ and, more
        # importantly, returns *naive* UTC which makes the returned
        # "period" key disagree with the SQLite-side ``strftime('%Y-%m',
        # 'now')`` we used to compute the summary itself. Using a
        # timezone-aware UTC value here keeps the two views consistent
        # regardless of where the server clock sits.
        period = datetime.now(timezone.utc).strftime("%Y-%m")
    return {
        "object": "usage",
        "period": period,
        **summary,
    }
