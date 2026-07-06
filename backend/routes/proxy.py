import json
import time
import logging
from ipaddress import ip_address, ip_network
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.config import Config
from backend.database import add_usage_log, check_rate_limit, get_client_ip, get_db_context, get_model_pricing, get_user_plan, get_wallet, update_wallet
from backend.models import AuthUserContext, ChatRequest, ProxyRequest
from backend.services import quota_service
from backend.services.billing_service import charge_for_usage, quote_cost, reconcile_stream_reserve
from backend.services.proxy_service import ProxyService
from backend.services.token_service import TokenService
from backend.services.user_service import UserService
from backend.utils.provider import get_provider_for_model

logger = logging.getLogger(__name__)

router = APIRouter()


def _bill_usage_by_id(user_id: int, usage_log_id: int) -> None:
    """Charge a specific usage_log row by its ID."""
    from backend.services.billing_service import charge_for_usage
    try:
        charge_for_usage(user_id, usage_log_id)
    except Exception:
        logger.exception("proxy billing failed for usage_log_id=%s", usage_log_id)


def _max_stream_cost_proxy(user_id, provider, model, max_output_tokens, prompt_tokens):
    pricing = get_model_pricing(provider, model) or {}
    in_price = float(pricing.get("input_price_per_1k") or 0.0)
    out_price = float(pricing.get("output_price_per_1k") or 0.0)
    if out_price <= 0 and in_price <= 0:
        return 0.0
    plan = get_user_plan(user_id)
    discount = float(plan.get("discount_rate") or 1.0)
    cost = (float(prompt_tokens) / 1000.0) * in_price + (float(max_output_tokens) / 1000.0) * out_price
    return round(cost * discount, 6)


def _user_still_active(user_id: int) -> bool:
    """Return ``True`` if the user is still active (not frozen mid-stream).

    Streaming generators call this every N chunks so an admin freeze
    tears the connection down promptly instead of letting upstream
    cost accumulate. Best-effort: on lookup failure returns ``True``
    so we never kill an otherwise healthy stream due to a transient
    DB hiccup — the freeze path also invalidates sessions and
    releases reservations, so the next request is blocked regardless.
    """
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_active FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return bool(row and row["is_active"])
    except Exception:
        return True


def _reserve_nonstream_cost(
    user_id: int,
    provider: str,
    model: str,
    messages: list,
    max_tokens: Optional[int],
) -> tuple[float, int]:
    """Pre-reserve the max estimated cost for a non-streaming proxy call.

    Returns ``(cost_reserve, prompt_token_estimate)``. A zero reserve is
    valid (free / unpriced model or zero balance on a free model) and
    simply means the downstream settlement will either be a no-op or
    fail with insufficient balance.
    """
    prompt_token_estimate = max(
        100,
        sum(len(m.get("content") or "") for m in (messages or [])) // 4,
    )
    max_output_tokens = int(max_tokens or 2048)
    max_cost = _max_stream_cost_proxy(
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
                related_type="nonstream_pre_reserve",
                related_id=None,
                note=f"{provider}/{model}",
            )
        except ValueError:
            raise HTTPException(status_code=402, detail="余额不足，请先充值")
    elif wallet_balance <= 0 and max_cost > 0:
        raise HTTPException(status_code=402, detail="余额不足，请先充值")
    return cost_reserve, prompt_token_estimate


def _settle_nonstream_billing(
    user_id: int,
    provider: str,
    model: str,
    usage_log_id: int,
    cost_reserve: float,
) -> None:
    """Reconcile the non-streaming pre-reserve with the actual cost.

    Writes the actual cost onto the usage log and lets
    :func:`charge_for_usage` perform the wallet debit (with its own
    idempotency guards). Because the usage row already has
    ``cost_credits > 0`` after the write-back, charge_for_usage takes
    the fast-path and only records the idempotency reservation. Any
    over-reserve is refunded atomically.
    """
    try:
        quote = quote_cost(user_id, provider, model, 0, 0)
        # Load the real token counts from the usage_log written by
        # ProxyService.forward_request so the quote reflects actual
        # usage, not the pre-flight estimate.
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT prompt_tokens, completion_tokens FROM usage_logs WHERE id = ?",
                (usage_log_id,),
            )
            row = cursor.fetchone()
        if row:
            quote = quote_cost(
                user_id,
                provider,
                model,
                int(row["prompt_tokens"] or 0),
                int(row["completion_tokens"] or 0),
            )
        actual_cost = float(quote.get("cost_credits") or 0.0)

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE usage_logs SET cost_credits = ? WHERE id = ?",
                (actual_cost, usage_log_id),
            )

        if cost_reserve > 0 and actual_cost < cost_reserve:
            refund = cost_reserve - actual_cost
            try:
                update_wallet(
                    user_id,
                    refund,
                    "refund",
                    related_type="usage",
                    related_id=usage_log_id,
                    note="nonstream over-reserve refund",
                )
            except Exception:
                logger.exception(
                    "nonstream refund failed user=%s log=%s", user_id, usage_log_id
                )

        if cost_reserve > 0 and actual_cost > cost_reserve:
            # Over-reserve in the other direction: actual usage exceeded
            # the pre-reserve. Charge the difference so the wallet debit
            # matches actual consumption. charge_for_usage below takes
            # the idempotency fast-path (cost_credits > 0) and won't
            # double-charge, so the supplementary debit here is safe.
            extra = actual_cost - cost_reserve
            try:
                update_wallet(
                    user_id,
                    -extra,
                    "consume",
                    related_type="usage",
                    related_id=usage_log_id,
                    note=f"nonstream over-reserve charge {provider}/{model}",
                )
            except ValueError:
                logger.warning(
                    "insufficient balance for over-reserve charge user=%s log=%s",
                    user_id,
                    usage_log_id,
                )
            except Exception:
                logger.exception(
                    "nonstream over-reserve charge failed user=%s log=%s",
                    user_id,
                    usage_log_id,
                )

        if actual_cost > 0:
            try:
                charge_for_usage(user_id, usage_log_id)
            except Exception:
                logger.exception(
                    "nonstream charge_for_usage failed user=%s log=%s",
                    user_id,
                    usage_log_id,
                )
        elif cost_reserve > 0:
            # Free / unpriced model — reserve was taken but actual cost
            # is zero, so return the entire reserve.
            try:
                update_wallet(
                    user_id,
                    cost_reserve,
                    "refund",
                    related_type="usage",
                    related_id=usage_log_id,
                    note="nonstream free-model reserve refund",
                )
            except Exception:
                logger.exception(
                    "nonstream free-model refund failed user=%s log=%s",
                    user_id,
                    usage_log_id,
                )
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "nonstream settlement failed user=%s log=%s; falling back to post-billing",
            user_id,
            usage_log_id,
        )
        _bill_usage_by_id(user_id, usage_log_id)


class ProxyAuthContext(BaseModel):
    user: AuthUserContext
    rate_limit_id: str
    token_id: Optional[int] = None
    token_policy: Optional[dict[str, Any]] = None


def _client_ip_allowed(client_ip: str, allowed_ips: list[str]) -> bool:
    if not allowed_ips:
        return True
    try:
        ip_obj = ip_address(client_ip)
    except Exception:
        return False
    for raw in allowed_ips:
        s = str(raw or "").strip()
        if not s:
            continue
        if "/" in s:
            try:
                if ip_obj in ip_network(s, strict=False):
                    return True
            except Exception:
                continue
        else:
            if client_ip == s:
                return True
    return False


def _enforce_token_policy(*, auth: ProxyAuthContext, model: str, client_ip: str) -> None:
    if not auth.token_policy:
        return
    allowed_models = auth.token_policy.get("allowed_models") or []
    if isinstance(allowed_models, list) and allowed_models and model not in allowed_models:
        raise HTTPException(status_code=403, detail="Token 无权限访问该模型")

    allowed_ips = auth.token_policy.get("allowed_ips") or []
    if (
        isinstance(allowed_ips, list)
        and allowed_ips
        and not _client_ip_allowed(client_ip, allowed_ips)
    ):
        raise HTTPException(status_code=403, detail="Token 不允许该 IP")


async def get_current_auth(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
):
    rate_limit_id: Optional[str] = None
    user: Optional[AuthUserContext] = None
    token_id: Optional[int] = None
    token_policy: Optional[dict[str, Any]] = None

    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
        resolved = TokenService.get_user_by_token(raw)
        if resolved:
            user, token_id, token_policy = resolved
            rate_limit_id = f"token:{token_id}"

    if not user and x_api_key:
        if not Config.ALLOW_LEGACY_X_API_KEY:
            raise HTTPException(status_code=403, detail="X-API-Key 鉴权已关闭")
        resolved_user = UserService.get_user_by_api_key(x_api_key)
        if resolved_user:
            user = resolved_user
            rate_limit_id = x_api_key

    if not user:
        # The caller *sent* a credential but the resolver rejected
        # it. Surface that as 401 (not 403) so OpenAI SDKs and CLI
        # tools (Claude Code, Cursor, …) treat it as a credential
        # problem and re-prompt — which is exactly what the user
        # wants when a key is wrong.
        if authorization or x_api_key:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        raise HTTPException(status_code=401, detail="未提供凭证")

    if not rate_limit_id:
        # Fallback: rate-limit on the user id when the legacy X-API-Key
        # path authenticated but the key string isn't suitable as an
        # id (shouldn't normally happen, but be defensive).
        rate_limit_id = f"user:{int(user.id)}"

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")

    minute_limit = Config.RATE_LIMIT_PER_MINUTE
    hour_limit = Config.RATE_LIMIT_PER_HOUR
    if token_id and token_policy:
        raw_min = token_policy.get("rate_limit_per_minute")
        raw_hour = token_policy.get("rate_limit_per_hour")
        if raw_min is not None:
            try:
                v = int(raw_min)
                if v > 0:
                    minute_limit = v
            except Exception:
                pass
        if raw_hour is not None:
            try:
                v = int(raw_hour)
                if v > 0:
                    hour_limit = v
            except Exception:
                pass

    allowed_min, _remaining_min = check_rate_limit(rate_limit_id, "api_key:60", minute_limit, 60)
    if not allowed_min:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    allowed_hour, _remaining_hour = check_rate_limit(
        rate_limit_id, "api_key:3600", hour_limit, 3600
    )
    if not allowed_hour:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    return ProxyAuthContext(
        user=user, rate_limit_id=rate_limit_id, token_id=token_id, token_policy=token_policy
    )


@router.post("/v1/text/chatcompletion_v2")
async def chat_completion(
    payload: ProxyRequest, request: Request, auth: ProxyAuthContext = Depends(get_current_auth)
):
    user = auth.user

    provider = get_provider_for_model(payload.model)

    # Comprehensive single-connection quota gate: checks 5h / week / month
    # token quotas, monthly budget, wallet balance, and plan RPM / TPM.
    can_use, message = quota_service.assert_request_allowed(
        user_id=user.id,
        provider=provider,
        model=payload.model,
        estimated_tokens=payload.max_tokens or 2048,
    )
    if not can_use:
        raise HTTPException(status_code=429, detail=message)

    # M8: Best-effort quota warning notification. Non-blocking.
    try:
        quota_service.maybe_warn_on_quota(user.id)
    except Exception:
        pass

    # Per-user token reservation — closes the concurrent-request
    # double-spend window. Released at each exit site below (the
    # streaming generator's reconcile block and the non-streaming
    # success / upstream-error paths).
    request_id = uuid4().hex
    quota_service.reserve_quota_reservation(
        user.id, int(payload.max_tokens or 2048), request_id=request_id
    )

    # TODO (Part 3): reserve estimated cost atomically before dispatching
    # the upstream request, then reconcile actual usage on response.
    # NOTE: For streaming requests, the cost reserve is handled separately
    # below (lines ~350-362) to avoid double-debiting.
    if payload.stream:
        prompt_token_estimate = max(100, sum(len(m.get("content") or "") for m in payload.messages) // 4)
        cost_reserve = 0.0  # Streaming path handles its own reserve
    else:
        cost_reserve, prompt_token_estimate = _reserve_nonstream_cost(
            user.id, provider, payload.model, payload.messages, payload.max_tokens
        )

    _enforce_token_policy(auth=auth, model=payload.model, client_ip=get_client_ip(request))

    upstream_payload = {
        "model": payload.model,
        "messages": payload.messages,
        "stream": payload.stream,
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }

    # Streaming fan-out. We forward the upstream SSE bytes verbatim so
    # the chunk format the caller sees matches what the upstream
    # actually emitted. ``forward_request`` is the non-streaming path
    # and would buffer the entire response, which kills the perceived
    # latency of long completions. Use ``stream_chat`` instead.
    if payload.stream:
        prompt_token_estimate = max(100, sum(len(m.get("content") or "") for m in payload.messages) // 4)
        max_output_tokens = int(payload.max_tokens or 2048)
        max_cost = _max_stream_cost_proxy(user.id, provider, payload.model, max_output_tokens, prompt_token_estimate)
        wallet_balance = float((get_wallet(user.id) or {}).get("balance") or 0.0)
        cost_reserve = min(max_cost, wallet_balance) if max_cost > 0 else 0.0
        if cost_reserve > 0:
            try:
                update_wallet(user.id, -cost_reserve, "reserve", related_type="stream_pre_reserve", related_id=None, note=f"{provider}/{payload.model}")
            except ValueError:
                raise HTTPException(status_code=402, detail="余额不足，请先充值")
        elif wallet_balance <= 0:
            raise HTTPException(status_code=402, detail="余额不足，请先充值")
        client_ip = get_client_ip(request)
        started = time.time()
        async def event_iter():
            prompt_tokens = 0
            completion_tokens = 0
            reconciled = False
            accumulated_text = ""
            stream_completed = False
            errored = False
            _stream_log_id = None
            # Check whether the user has been frozen mid-stream every N
            # delta events so an admin freeze tears the connection down
            # promptly instead of letting upstream cost accumulate.
            _delta_count = 0
            _frozen = False
            try:
                async for raw in ProxyService.stream_chat(user.id, upstream_payload, provider, token_id=auth.token_id, **({"request": request} if request is not None else {})):
                    if _frozen:
                        # Keep draining upstream so the connection tears
                        # down cleanly, but don't forward anything else.
                        continue
                    yield raw
                    try:
                        text = raw.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    ev = None
                    dt = None
                    for line in text.splitlines():
                        if line.startswith("event:"):
                            ev = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            dt = line[len("data:"):].strip()
                    if ev == "delta" and dt:
                        try:
                            _d = json.loads(dt)
                            _c = _d.get("content") if isinstance(_d, dict) else None
                            if isinstance(_c, str):
                                accumulated_text += _c
                        except Exception:
                            pass
                        _delta_count += 1
                        # Defence-in-depth: ProxyService.stream_chat already
                        # checks request.is_disconnected() every 10 chunks,
                        # but verify here too so a client disconnect tears
                        # the loop down promptly even if the inner check
                        # missed it (e.g. chunk boundary alignment).
                        if _delta_count % 25 == 0 and request is not None:
                            try:
                                if await request.is_disconnected():
                                    break
                            except Exception:
                                pass
                        if _delta_count % 50 == 0 and not _user_still_active(user.id):
                            # Emit a synthetic error event in the same
                            # internal SSE format the upstream uses, then
                            # stop forwarding. The ``finally`` block
                            # still reconciles billing and releases the
                            # reservation.
                            yield (
                                b"event: error\ndata: "
                                + json.dumps(
                                    {"error": "账户已被冻结", "type": "account_frozen"}
                                ).encode()
                                + b"\n\n"
                            )
                            _frozen = True
                            errored = True
                            continue
                    elif ev == "done" and dt:
                        stream_completed = True
                        try:
                            dd = json.loads(dt)
                            u = (dd or {}).get("usage") or {}
                            prompt_tokens = int(u.get("prompt_tokens") or 0)
                            completion_tokens = int(u.get("completion_tokens") or 0)
                        except Exception:
                            pass
                    elif ev == "error":
                        errored = True
            except Exception as exc:
                logger.exception("Proxy stream chat failed: %s", exc)
                errored = True
            finally:
                _log_status = 502 if errored else 200
                try:
                    _q = quote_cost(user.id, provider, payload.model, prompt_tokens, completion_tokens)
                    _c = float(_q.get("cost_credits") or 0)
                    _stream_log_id = add_usage_log(user_id=user.id, endpoint="/v1/text/chatcompletion_v2 (stream)", model=payload.model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, response_time_ms=int((time.time() - started) * 1000), status_code=_log_status, ip_address=client_ip, provider=provider, cost_credits=_c)
                except Exception:
                    logger.warning("Failed to add usage log for proxy stream", exc_info=True)
                if not reconciled and cost_reserve > 0:
                    reconciled = True
                    try:
                        reconcile_stream_reserve(
                            user_id=user.id,
                            provider=provider,
                            model=payload.model,
                            cost_reserve=cost_reserve,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            usage_log_id=_stream_log_id,
                            messages=payload.messages,
                            accumulated_text=accumulated_text,
                            stream_completed=stream_completed,
                        )
                    except Exception:
                        logger.exception("stream reconcile failed for user %s", user.id)
                # Release the per-user token reservation once actual
                # usage has been committed to usage_logs.
                quota_service.release_quota_reservation(user.id, request_id=request_id)
        return StreamingResponse(event_iter(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    _fwd_ret = await ProxyService.forward_request(
        user.id, upstream_payload, provider, token_id=auth.token_id
    )
    result, status_code, usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)

    def _refund_reserve(reason: str):
        if cost_reserve > 0:
            try:
                update_wallet(
                    user.id,
                    cost_reserve,
                    "refund",
                    related_type="usage",
                    related_id=usage_log_id,
                    note=f"nonstream {reason} refund",
                )
            except Exception:
                logger.exception(
                    "nonstream %s refund failed user=%s log=%s",
                    reason,
                    user.id,
                    usage_log_id,
                )

    if status_code != 200:
        _refund_reserve("upstream-error")
        quota_service.release_quota_reservation(user.id, request_id=request_id)
        raise HTTPException(status_code=status_code, detail="上游服务错误")

    base_resp = result.get("base_resp", {})
    if base_resp and base_resp.get("status_code", 0) != 0:
        _refund_reserve("upstream-error")
        quota_service.release_quota_reservation(user.id, request_id=request_id)
        raise HTTPException(status_code=400, detail="上游服务错误")

    if usage_log_id is not None:
        _settle_nonstream_billing(
            user.id, provider, payload.model, usage_log_id, cost_reserve
        )

    quota_service.release_quota_reservation(user.id, request_id=request_id)
    return result


@router.post("/v1/chat")
async def chat(
    payload: ChatRequest, request: Request, auth: ProxyAuthContext = Depends(get_current_auth)
):
    user = auth.user

    provider = get_provider_for_model(payload.model)

    # Comprehensive single-connection quota gate: checks 5h / week / month
    # token quotas, monthly budget, wallet balance, and plan RPM / TPM.
    can_use, message = quota_service.assert_request_allowed(
        user_id=user.id,
        provider=provider,
        model=payload.model,
        estimated_tokens=payload.max_tokens or 2048,
    )
    if not can_use:
        raise HTTPException(status_code=429, detail=message)

    # M8: Best-effort quota warning notification. Non-blocking.
    try:
        quota_service.maybe_warn_on_quota(user.id)
    except Exception:
        pass

    # Per-user token reservation — closes the concurrent-request
    # double-spend window. Released at each exit site below (the
    # streaming generator's reconcile block and the non-streaming
    # success / upstream-error paths).
    request_id = uuid4().hex
    quota_service.reserve_quota_reservation(
        user.id, int(payload.max_tokens or 2048), request_id=request_id
    )

    # TODO (Part 3): reserve estimated cost atomically before dispatching
    # the upstream request, then reconcile actual usage on response.

    _enforce_token_policy(auth=auth, model=payload.model, client_ip=get_client_ip(request))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversations (user_id, session_id, role, content)
            VALUES (?, ?, 'user', ?)
        """,
            (user.id, payload.session_id, payload.message),
        )

    messages = [{"role": "user", "content": payload.message}]

    cost_reserve, _prompt_est = _reserve_nonstream_cost(
        user.id, provider, payload.model, messages, payload.max_tokens
    )

    upstream_payload = {
        "model": payload.model,
        "messages": messages,
        "stream": False,
        "max_tokens": payload.max_tokens,
        "temperature": payload.temperature,
    }

    _fwd_ret = await ProxyService.forward_request(
        user.id, upstream_payload, provider, token_id=auth.token_id
    )
    result, status_code, usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)

    def _refund_reserve(reason: str):
        if cost_reserve > 0:
            try:
                update_wallet(
                    user.id,
                    cost_reserve,
                    "refund",
                    related_type="usage",
                    related_id=usage_log_id,
                    note=f"chat {reason} refund",
                )
            except Exception:
                logger.exception(
                    "chat %s refund failed user=%s log=%s",
                    reason,
                    user.id,
                    usage_log_id,
                )

    reply = ""
    tokens_used = 0
    error_msg = None

    if status_code == 200:
        base_resp = result.get("base_resp", {})
        if base_resp and base_resp.get("status_code", 0) != 0:
            error_msg = "上游服务错误"

        if not error_msg:
            if "choices" in result:
                reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            elif "data" in result and "outputs" in result["data"]:
                reply = result["data"]["outputs"][0].get("text", "")
            usage = result.get("usage", {}) or result.get("data", {}).get("usage", {})
            tokens_used = usage.get("total_tokens", 0)

            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO conversations (user_id, session_id, role, content)
                    VALUES (?, ?, 'assistant', ?)
                """,
                    (user.id, payload.session_id, reply),
                )

    if status_code != 200:
        error_msg = "上游服务错误"

    if error_msg:
        _refund_reserve("upstream-error")
        quota_service.release_quota_reservation(user.id, request_id=request_id)
        raise HTTPException(status_code=502, detail=error_msg)

    if usage_log_id is not None:
        _settle_nonstream_billing(
            user.id, provider, payload.model, usage_log_id, cost_reserve
        )

    quota_service.release_quota_reservation(user.id, request_id=request_id)
    return {"reply": reply, "session_id": payload.session_id, "tokens_used": tokens_used}


@router.get("/v1/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    request: Request,
    limit: int = 200,
    before_id: Optional[int] = None,
    auth: ProxyAuthContext = Depends(get_current_auth),
):
    """Return the messages of a single conversation.

    We default to the 200 most recent messages and accept a
    ``limit`` query param (capped at 1000) and an optional
    ``before_id`` cursor for back-pagination. Without these guards
    a long-lived session could easily grow into the millions of
    rows and stall the SQLite thread on the next request.
    """
    user = auth.user
    # Clamp the limit to a sane range so a misbehaving client can't
    # request ``limit=999999999`` and effectively pull the entire
    # history.
    safe_limit = max(1, min(int(limit or 200), 1000))
    sql = """
        SELECT id, role, content, created_at FROM conversations
         WHERE user_id = ? AND session_id = ?
    """
    params: list[Any] = [user.id, session_id]
    if before_id is not None:
        try:
            params.append(int(before_id))
            sql += " AND id < ?"
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="before_id 非法")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(safe_limit)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    # The cursor returns newest-first; flip to chronological order
    # so the client can render the conversation top-down.
    ordered = list(reversed(rows))
    return {
        "session_id": session_id,
        "count": len(ordered),
        "has_more": len(rows) == safe_limit,
        "messages": [
            {"role": row["role"], "content": row["content"], "time": row["created_at"]}
            for row in ordered
        ],
    }


@router.get("/v1/conversations")
async def list_conversations(auth: ProxyAuthContext = Depends(get_current_auth)):
    user = auth.user
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, MAX(created_at) as last_time,
            (SELECT content FROM conversations c2 WHERE c2.session_id = conversations.session_id AND c2.role = 'user' ORDER BY created_at DESC LIMIT 1) as last_message
            FROM conversations
            WHERE user_id = ?
            GROUP BY session_id
            ORDER BY last_time DESC
            LIMIT 50
        """,
            (user.id,),
        )
        rows = cursor.fetchall()
        return [
            {
                "session_id": row["session_id"],
                "last_time": row["last_time"],
                "last_message": row["last_message"],
            }
            for row in rows
        ]
