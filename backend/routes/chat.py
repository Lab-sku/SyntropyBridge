"""Chat surface — sessions, conversations, streaming and non-streaming send.

These endpoints are the bridge between the SPA's chat page and the
proxy layer. They are intentionally mounted **outside** ``admin`` /
``user`` namespaces so a session of *either* role can drive them:
admins use the chat to smoke-test their own configuration, regular
users use it as the day-to-day playground.

The previous version lived in ``admin.py`` and hard-required an
admin session, which meant the moment a regular user opened ``/chat``
the conversation list, send button and history all 401'd. This file
adds a shared ``require_chat_session`` dependency that accepts either
cookie and resolves the underlying ``user_id`` so the same DB rows
are used for both audiences.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.database import (
    add_usage_log,
    get_db_context,
    get_model_pricing,
    get_setting,
    get_user_plan,
    get_wallet,
    update_usage_log_cost,
    update_wallet,
)
from backend.services.billing_service import charge_for_usage, quote_cost, reconcile_stream_reserve
from backend.services.proxy_service import ProxyService
from backend.services.user_service import UserService
from backend.session import (
    ADMIN_SESSION_COOKIE,
    CSRF_COOKIE,
    USER_SESSION_COOKIE,
    get_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _format_utc(ts: Optional[str]) -> Optional[str]:
    """Normalize a SQLite ``CURRENT_TIMESTAMP`` value (``"YYYY-MM-DD HH:MM:SS"``)
    to ISO 8601 with an explicit ``+00:00`` offset.

    SQLite's ``CURRENT_TIMESTAMP`` is always UTC but is returned as a
    *naive* string. When the frontend does ``new Date("2026-06-14
    04:34:56")`` without a timezone marker, JavaScript treats it as
    *local* time — which causes an 8-hour offset for users in CST.
    Appending ``+00:00`` makes the value unambiguous so the browser
    correctly parses the absolute instant.
    """
    if not ts:
        return ts
    s = str(ts).strip()
    if not s:
        return s
    # Already ISO 8601 (e.g. ``2026-06-14T04:34:56Z``) — leave alone.
    if "T" in s and ("Z" in s or "+" in s[10:] or "-" in s[10:]):
        return s
    # SQLite default: "YYYY-MM-DD HH:MM:SS" — convert to ISO 8601.
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        # Tolerate microsecond / fractional suffixes by stripping them.
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return s
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Session dependency: accept either admin *or* user cookie
# ---------------------------------------------------------------------------


def _resolve_session(request: Request) -> dict:
    """Accept either an admin or user session cookie.

    Returns the decoded session row. Raises 401 if neither cookie is
    present or both are expired. This is the single chokepoint every
    chat endpoint goes through, so a future change (e.g. switching to
    bearer-only auth) only needs to be applied here.
    """
    sid = request.cookies.get(USER_SESSION_COOKIE) or request.cookies.get(ADMIN_SESSION_COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="未授权")
    sess = get_session(sid, user_agent=request.headers.get("User-Agent"))
    if not sess:
        raise HTTPException(status_code=401, detail="会话已过期")
    if sess.get("role") not in {"user", "admin"}:
        raise HTTPException(status_code=401, detail="未授权")
    sess["_sid"] = sid
    return sess


def _resolve_user_id(session: dict) -> int:
    """Map a session to a ``users.id``.

    Admin sessions don't carry a ``user_id``; resolve the admin's own
    user record (lazily created if missing) so quota / log writes
    have a valid target. Regular users already have one.

    The admin's chat-only user row is keyed on a deterministic,
    namespaced username (``_admin_{admin_id}_chat``) rather than the
    admin's display username. This avoids two failure modes:

    1. Collisions with a real user who happens to share the admin's
       username (the admin would silently hijack that user's quota /
       wallet / conversation history).
    2. Orphan rows piling up if the admin username changes — the
       namespaced key is stable for the lifetime of the admin row.
    """
    if session.get("role") == "user" and session.get("user_id"):
        return int(session["user_id"])

    # Admin path: find or create a dedicated chat-only user row keyed
    # on the admin_id. The admin_id is stable for the lifetime of the
    # admin_users row, so the same admin reuses the same user row
    # across sessions instead of creating a new one each time.
    admin_id = session.get("admin_id")
    if not admin_id:
        # No admin_id in the session — fall back to the username so
        # we still have a stable key. This should not normally happen
        # for admin sessions (login sets admin_id), but defensive
        # code is cheaper than a 500.
        username = (session.get("username") or "").strip()
        if not username:
            raise HTTPException(status_code=401, detail="会话缺少用户标识")
        chat_username = f"_admin_chat_{username}"[:50]
    else:
        chat_username = f"_admin_{int(admin_id)}_chat"

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ?", (chat_username,))
        row = cursor.fetchone()
        if row:
            return int(row["id"])

    # The admin never had a corresponding chat-only user row yet —
    # create one with a random unusable password (we never log in as
    # this user through the regular auth flow; it's only used for
    # quota + log bookkeeping of the admin's chat activity). Quotas
    # are bumped to a very large number so admin testing isn't
    # accidentally capped. A wallet row is also seeded with the same
    # near-infinite balance so that any other code path that inspects
    # the wallet (e.g. the admin dashboard's wallet card) doesn't
    # show a misleading 0.
    import secrets

    from backend.security import Security

    api_key = Security.generate_api_key()
    random_password = secrets.token_urlsafe(32)
    # 9_999_999 credits ≈ 100k CNY. Stays well inside the BIGINT range
    # (column is REAL elsewhere but kept as float-safe integer here).
    _ADMIN_INITIAL_BALANCE = 9_999_999.0
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (username, email, password_hash, api_key,
                               quota_5h, quota_week, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (chat_username, None, Security.hash_password(random_password), api_key, 999_999, 9_999_999),
        )
        new_id = int(cursor.lastrowid or 0)
        # Seed a wallet so the admin user record behaves consistently
        # with the rest of the system. ``ON CONFLICT`` keeps the call
        # idempotent if the row was created in a previous run that
        # crashed between the user and wallet inserts.
        cursor.execute(
            """
            INSERT INTO wallets (user_id, balance, total_recharged, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (new_id, _ADMIN_INITIAL_BALANCE, _ADMIN_INITIAL_BALANCE),
        )
    if not new_id:
        raise HTTPException(status_code=500, detail="无法解析管理员用户")
    return new_id


def _require_csrf(request: Request, session: dict = Depends(_resolve_session)) -> dict:
    """CSRF guard for state-changing methods.

    Re-uses the existing pattern from admin/user routers — header
    token must match the cookie token, and both must match the value
    stored in the session row. We always go through ``Depends`` so
    ``_resolve_session`` runs first and yields a usable session dict.
    """
    import hmac as _hmac

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        csrf_header = request.headers.get("X-CSRF-Token", "")
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        session_csrf = session.get("csrf") or ""
        if (
            not csrf_header
            or not _hmac.compare_digest(csrf_header, csrf_cookie)
            or not _hmac.compare_digest(csrf_header, session_csrf)
        ):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return session


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def _get_provider_for_model(model: str) -> str:
    """Resolve a model id to its provider.

    Honours the admin-configured ``model_provider_map`` first, then
    falls back to the prefix/keyword heuristic in
    :func:`backend.providers.detect_provider_from_model`. Keeping a
    local copy here means the chat surface never has to import from
    the proxy module (which is loaded only on demand for non-SPA
    flows) and means a misconfigured map can't accidentally send a
    request to a vendor the admin never approved.
    """
    try:
        raw = get_setting("model_provider_map") or ""
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Use the *full* provider registry, not the legacy
                # ``{"minimax", "nvidia"}`` whitelist. The chat
                # surface and proxy both delegate to
                # :func:`backend.providers.detect_provider_from_model`
                # which already knows about deepseek / aliyun /
                # moonshot / zhipu / doubao / openrouter / openai /
                # custom:* — restricting the admin override to
                # only two providers would silently drop any
                # override the admin sets for the other vendors
                # and make the override look like it took effect
                # while it actually never fires.
                try:
                    from backend.providers.base import ProviderRegistry

                    valid = set(ProviderRegistry.all().keys())
                except Exception:
                    valid = {"minimax", "nvidia"}
                if model in parsed and parsed[model] in valid:
                    return str(parsed[model])
                for k, v in parsed.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        continue
                    if k.endswith("*") and v in valid:
                        prefix = k[:-1]
                        if prefix and model.startswith(prefix):
                            return v
    except Exception:
        # The map is best-effort; a bad JSON blob should not 500 the
        # chat endpoint. Fall through to the heuristic below.
        pass

    try:
        from backend.providers import detect_provider_from_model

        return detect_provider_from_model(model)
    except Exception:
        return "minimax"


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.get("/chat/conversations")
async def list_chat_conversations(
    request: Request,
    _session: dict = Depends(_resolve_session),
):
    """Return the recent chat sessions for the resolved user.

    Frontend calls this from ``chatStore.loadConversations`` to render
    the sidebar. The list shape matches what the old admin-only
    endpoint returned, so the SPA didn't need any change to consume
    it.
    """
    user_id = _resolve_user_id(_session)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, MAX(created_at) as last_time,
                (SELECT content FROM conversations c2
                 WHERE c2.session_id = conversations.session_id
                   AND c2.role = 'user'
                 ORDER BY created_at ASC LIMIT 1) as first_message,
                (SELECT title FROM conversations c4
                 WHERE c4.session_id = conversations.session_id
                   AND c4.title != ''
                 LIMIT 1) as title,
                (SELECT model FROM conversations c3
                 WHERE c3.session_id = conversations.session_id
                   AND c3.role = 'user'
                   AND c3.model != ''
                 ORDER BY created_at DESC LIMIT 1) as model
              FROM conversations
             WHERE user_id = ?
             GROUP BY session_id
             ORDER BY last_time DESC
             LIMIT 50
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        return [
            {
                "session_id": row["session_id"],
                "last_time": _format_utc(row["last_time"]),
                "last_message": row["first_message"],
                "title": row["title"] or "",
                "model": row["model"] or "",
            }
            for row in rows
        ]


class GenerateTitleRequest(BaseModel):
    session_id: str
    model: str = ""


@router.post("/chat/generate-title")
async def generate_chat_title(
    body: GenerateTitleRequest,
    request: Request,
    _session: dict = Depends(_require_csrf),
):
    """Generate an AI-powered title for a conversation session.

    Called once after the first assistant reply. Uses the user's
    currently selected model to produce a concise 4-5 character title.
    The title is persisted on every conversation row of the session
    so the sidebar can read it without a JOIN.
    """
    user_id = _resolve_user_id(_session)
    session_id = body.session_id
    model = body.model

    if not session_id:
        raise HTTPException(400, "session_id required")

    with get_db_context() as conn:
        cursor = conn.cursor()

        # Check if title already exists — skip if so
        cursor.execute(
            "SELECT title FROM conversations WHERE session_id = ? AND user_id = ? AND title != '' LIMIT 1",
            (session_id, user_id),
        )
        existing = cursor.fetchone()
        if existing and existing["title"]:
            return {"title": existing["title"]}

        # Get first user message
        cursor.execute(
            "SELECT content FROM conversations WHERE session_id = ? AND user_id = ? AND role = 'user' ORDER BY created_at ASC LIMIT 1",
            (session_id, user_id),
        )
        first_msg = cursor.fetchone()
        if not first_msg:
            raise HTTPException(404, "No user messages found")

        user_content = first_msg["content"][:200]

    # Call the model to generate title
    title_prompt = "使用四到五个字直接返回这句话的简要主题，不要解释、不要标点、不要语气词、不要多余文本，如果没有主题，请直接返回\"闲聊\""
    title_payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": user_content},
            {"role": "user", "content": title_prompt},
        ],
        "stream": False,
        "max_tokens": 20,
    }

    try:
        provider = _provider_for_model(model)
        _fwd_ret = await ProxyService.forward_request(
            user_id=user_id,
            payload=title_payload,
            provider=provider,
        )
        result, status_code, _usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)

        if status_code != 200 or "error" in result:
            logger.warning("Title generation failed: %s", result)
            return {"title": ""}

        # Extract title from response
        choices = result.get("choices", [])
        if choices:
            raw_title = choices[0].get("message", {}).get("content", "").strip()
            # Clean up: remove quotes, punctuation, limit length
            title = raw_title.replace("\"", "").replace("'", "").replace("。", "").replace("，", "")
            title = title[:20] if len(title) > 20 else title
        else:
            title = ""

        # Persist title on all rows of this session
        if title:
            with get_db_context() as conn:
                conn.execute(
                    "UPDATE conversations SET title = ? WHERE session_id = ? AND user_id = ?",
                    (title, session_id, user_id),
                )

        return {"title": title}

    except Exception as e:
        logger.error("Title generation error: %s", e)
        return {"title": ""}


@router.get("/chat/conversations/{session_id}")
async def get_chat_conversation(
    session_id: str,
    request: Request,
    _session: dict = Depends(_resolve_session),
):
    """Return every turn in a single chat session, oldest first."""
    user_id = _resolve_user_id(_session)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, content, model, created_at
              FROM conversations
             WHERE user_id = ? AND session_id = ?
             ORDER BY created_at ASC
            """,
            (user_id, session_id),
        )
        rows = cursor.fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "model": row["model"] or "",
                "created_at": _format_utc(row["created_at"]),
            }
            for row in rows
        ]


@router.delete("/chat/conversations/{session_id}")
async def delete_chat_conversation(
    session_id: str,
    request: Request,
    session: dict = Depends(_require_csrf),
):
    """Delete every turn in a session for the resolved user."""
    user_id = _resolve_user_id(session)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM conversations WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
    return {"message": "对话已删除"}


# ---------------------------------------------------------------------------
# Send (non-streaming)
# ---------------------------------------------------------------------------


class ChatSendRequest(BaseModel):
    message: str
    model: str
    session_id: str = "default"
    max_tokens: Optional[int] = 1024
    temperature: Optional[float] = 0.7


@router.post("/chat/send")
async def chat_send(
    chat_data: ChatSendRequest,
    request: Request,
    session: dict = Depends(_require_csrf),
):
    """Single-shot chat send. Persists both turns and returns the reply.

    Kept for the API-only clients that don't speak SSE. The SPA's
    chat page should prefer ``/chat/send/stream`` for the perceived
    latency win.
    """
    user_id = _resolve_user_id(session)

    _ensure_chat_capable(chat_data.model)

    provider = _get_provider_for_model(chat_data.model)
    _check_provider_ready(provider)

    from backend.services import quota_service as _qs

    can_use, message = _qs.assert_request_allowed(
        user_id=user_id,
        provider=provider,
        model=chat_data.model,
        estimated_tokens=int(chat_data.max_tokens or 1024),
    )
    if not can_use:
        raise HTTPException(status_code=429, detail=message or "配额不足")

    # M9: User-level model access control (deny list). Applies the same
    # check that API-key callers go through in auth_service.check_key_restrictions,
    # so a deny row in user_model_access blocks the web chat UI too.
    try:
        from backend.services.auth_service import check_user_model_access

        deny_reason = check_user_model_access(user_id, chat_data.model)
        if deny_reason:
            raise HTTPException(status_code=403, detail=deny_reason)
    except HTTPException:
        raise
    except Exception:
        pass

    # M8: Best-effort quota warning notification. Non-blocking.
    try:
        _qs.maybe_warn_on_quota(user_id)
    except Exception:
        pass

    # Per-user token reservation — released at the function's single
    # return site below.
    request_id = uuid4().hex
    _qs.reserve_quota_reservation(
        user_id, int(chat_data.max_tokens or 1024), request_id=request_id
    )

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversations (user_id, session_id, role, content, model)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (user_id, chat_data.session_id, chat_data.message, chat_data.model),
        )

    # Build context-enriched messages array with conversation history.
    history, truncated_history = _load_conversation_history(user_id, chat_data.session_id)
    messages = history if history else []
    if not messages or messages[-1].get("content") != chat_data.message or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": chat_data.message})

    payload = {
        "model": chat_data.model,
        "messages": messages,
        "stream": False,
        "max_tokens": chat_data.max_tokens,
        "temperature": chat_data.temperature,
    }

    _fwd_ret = await ProxyService.forward_request(user_id, payload, provider)
    result, status_code, _usage_log_id = (_fwd_ret[0], _fwd_ret[1], _fwd_ret[2] if len(_fwd_ret) > 2 else None)

    reply = ""
    error_msg: Optional[str] = None
    if status_code == 200:
        if "choices" in result:
            reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        elif "data" in result and "outputs" in result["data"]:
            reply = result["data"]["outputs"][0].get("text", "")
    else:
        error_msg = _extract_chat_error(result, status_code)

    if not error_msg and reply:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO conversations (user_id, session_id, role, content, model)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (user_id, chat_data.session_id, reply, chat_data.model),
            )

    if error_msg:
        return {
            "reply": "",
            "session_id": chat_data.session_id,
            "tokens_used": 0,
            "error": error_msg,
            "truncated_history": truncated_history,
        }

    tokens_used = 0
    if status_code == 200 and isinstance(result, dict):
        usage = result.get("usage") or result.get("data", {}).get("usage", {}) or {}
        tokens_used = int(usage.get("total_tokens") or 0)

    if _usage_log_id is not None:
        try:
            charge_for_usage(user_id, _usage_log_id)
        except Exception:
            logger.exception("chat send billing failed for usage_log_id=%s", _usage_log_id)

    _qs.release_quota_reservation(user_id, request_id=request_id)
    return {
        "reply": reply,
        "session_id": chat_data.session_id,
        "tokens_used": tokens_used,
        "truncated_history": truncated_history,
    }


# ---------------------------------------------------------------------------
# Send (streaming)
# ---------------------------------------------------------------------------


def _max_stream_cost_chat(user_id, provider, model, max_output_tokens, prompt_tokens):
    pricing = get_model_pricing(provider, model) or {}
    in_price = float(pricing.get("input_price_per_1k") or 0.0)
    out_price = float(pricing.get("output_price_per_1k") or 0.0)
    if out_price <= 0 and in_price <= 0:
        return 0.0
    plan = get_user_plan(user_id)
    discount = float(plan.get("discount_rate") or 1.0)
    cost = (float(prompt_tokens) / 1000.0) * in_price + (float(max_output_tokens) / 1000.0) * out_price
    return round(cost * discount, 6)


@router.post("/chat/send/stream")
async def chat_send_stream(
    chat_data: ChatSendRequest,
    request: Request,
    session: dict = Depends(_require_csrf),
):
    """Server-Sent Events variant of ``/chat/send``.

    Yields ``event: delta`` for every incremental token chunk, then a
    final ``event: done`` with the accumulated usage, and stores the
    full assistant reply in the conversations table on success.

    The frontend (``frontend/src/lib/api.js → streamChat``) parses
    these events, so the wire format is part of the API contract —
    changing the event names here is a coordinated change.
    """
    user_id = _resolve_user_id(session)

    _ensure_chat_capable(chat_data.model)

    provider = _get_provider_for_model(chat_data.model)
    _check_provider_ready(provider)

    from backend.services import quota_service as _qs

    can_use, quota_msg = _qs.assert_request_allowed(
        user_id=user_id,
        provider=provider,
        model=chat_data.model,
        estimated_tokens=int(chat_data.max_tokens or 1024),
    )
    if not can_use:
        raise HTTPException(status_code=429, detail=quota_msg or "配额不足")

    # M9: User-level model access control (deny list).
    try:
        from backend.services.auth_service import check_user_model_access

        deny_reason = check_user_model_access(user_id, chat_data.model)
        if deny_reason:
            raise HTTPException(status_code=403, detail=deny_reason)
    except HTTPException:
        raise
    except Exception:
        pass

    # M8: Best-effort quota warning notification. Non-blocking.
    try:
        _qs.maybe_warn_on_quota(user_id)
    except Exception:
        pass

    # Per-user token reservation — released inside the streaming
    # generator's terminal branch (after reconcile_stream_reserve
    # commits actual usage to usage_logs).
    request_id = uuid4().hex
    _qs.reserve_quota_reservation(
        user_id, int(chat_data.max_tokens or 1024), request_id=request_id
    )

    # Persist the user turn up-front so a mid-stream disconnect still
    # leaves the user message visible in the conversation history.
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversations (user_id, session_id, role, content, model)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (user_id, chat_data.session_id, chat_data.message, chat_data.model),
        )

    # Build context-enriched messages array with conversation history.
    history, _truncated_history = _load_conversation_history(user_id, chat_data.session_id)
    messages = history if history else []
    if not messages or messages[-1].get("content") != chat_data.message or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": chat_data.message})

    upstream_payload = {
        "model": chat_data.model,
        "messages": messages,
        "stream": True,
        "max_tokens": chat_data.max_tokens,
        "temperature": chat_data.temperature,
    }

    prompt_token_estimate = max(100, sum(len(m.get("content") or "") for m in messages) // 4)
    max_output_tokens = int(chat_data.max_tokens or 1024)
    max_cost = _max_stream_cost_chat(user_id, provider, chat_data.model, max_output_tokens, prompt_token_estimate)
    # Admins are smoke-testing their own platform — the auto-created
    # ``users`` row that backs their session has a near-infinite quota
    # but no wallet. Skipping the wallet pre-flight (and the pre-reserve
    # debit) means admin chat replies aren't blocked by the same
    # "余额不足" error regular users get. Usage is still recorded in
    # ``usage_logs`` for accounting; the only thing we skip is the
    # ``wallet_transactions`` debit.
    is_admin_session = session.get("role") == "admin"
    wallet_balance = float((get_wallet(user_id) or {}).get("balance") or 0.0)
    cost_reserve = 0.0
    if not is_admin_session:
        cost_reserve = min(max_cost, wallet_balance) if max_cost > 0 else 0.0
        if cost_reserve > 0:
            try:
                update_wallet(user_id, -cost_reserve, "reserve", related_type="stream_pre_reserve", related_id=None, note=f"{provider}/{chat_data.model}")
            except ValueError:
                raise HTTPException(status_code=402, detail="\u4f59\u989d\u4e0d\u8db3\uff0c\u8bf7\u5148\u5145\u503c")
        elif wallet_balance <= 0:
            raise HTTPException(status_code=402, detail="\u4f59\u989d\u4e0d\u8db3\uff0c\u8bf7\u5148\u5145\u503c")
    started = time.time()

    async def event_iter():
        accumulated = ""
        errored = False
        prompt_tokens = 0
        completion_tokens = 0
        reconciled = False
        stream_completed = False
        _stream_log_id = None
        try:
            try:
                async for raw in ProxyService.stream_chat(user_id, upstream_payload, provider, **({"request": request} if request is not None else {})):
                    try:
                        text = raw.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    yield raw
                    event_name = None
                    data_text = None
                    for line in text.splitlines():
                        if line.startswith("event:"):
                            event_name = line[len("event:") :].strip()
                        elif line.startswith("data:"):
                            data_text = line[len("data:") :].strip()
                    if event_name == "delta" and data_text:
                        try:
                            payload_obj = json.loads(data_text)
                        except Exception:
                            payload_obj = None
                        if isinstance(payload_obj, dict):
                            content = payload_obj.get("content")
                            if isinstance(content, str):
                                accumulated += content
                    elif event_name == "done" and data_text:
                        stream_completed = True
                        try:
                            done_data = json.loads(data_text)
                            u = (done_data or {}).get("usage") or {}
                            prompt_tokens = int(u.get("prompt_tokens") or 0)
                            completion_tokens = int(u.get("completion_tokens") or 0)
                        except Exception:
                            pass
                    elif event_name == "error":
                        errored = True
            except Exception as exc:
                logger.exception("Stream chat failed: %s", exc)
                errored = True
                yield _sse("error", {"error": str(exc), "code": 500})
        finally:
            # ── Cleanup block: always runs, even on GeneratorExit /
            # CancelledError (client disconnect). This ensures the wallet
            # reserve is reconciled and the assistant message is persisted. ──
            if not errored:
                try:
                    _q = quote_cost(user_id, provider, chat_data.model, prompt_tokens, completion_tokens)
                    _c = float(_q.get("cost_credits") or 0)
                    _stream_log_id = add_usage_log(user_id=user_id, endpoint="/chat/send/stream", model=chat_data.model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, response_time_ms=int((time.time() - started) * 1000), status_code=200, provider=provider, cost_credits=_c)
                except Exception:
                    logger.warning("Failed to add usage log for chat stream", exc_info=True)

            if not reconciled and cost_reserve > 0:
                reconciled = True
                try:
                    reconcile_stream_reserve(
                        user_id=user_id,
                        provider=provider,
                        model=chat_data.model,
                        cost_reserve=cost_reserve,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        usage_log_id=_stream_log_id,
                        messages=messages,
                        accumulated_text=accumulated,
                        stream_completed=stream_completed,
                    )
                except Exception:
                    logger.exception("stream reconcile failed for user %s", user_id)

            if not errored and accumulated:
                try:
                    with get_db_context() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """
                            INSERT INTO conversations (user_id, session_id, role, content, model)
                            VALUES (?, ?, 'assistant', ?, ?)
                            """,
                            (user_id, chat_data.session_id, accumulated, chat_data.model),
                        )
                except Exception as exc:
                    logger.warning("Failed to persist assistant turn: %s", exc)

            # Release the per-user token reservation once actual usage
            # has been committed to usage_logs.
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


def _sse(event: str, data: dict) -> bytes:
    """Format one SSE block in the internal ``event:`` / ``data:`` shape."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Edge validation: refuse non-chat models on the chat endpoints
# ---------------------------------------------------------------------------

_TYPE_LABELS = {
    "embedding": "向量嵌入（embedding）模型",
    "image": "图像生成模型",
    "audio": "语音/音频模型",
}


def _ensure_chat_capable(model: str) -> None:
    """Raise 400 if ``model`` is not chat-capable.

    Both ``/chat/send`` and ``/chat/send/stream`` unconditionally forward
    to ``/v1/chat/completions``. When the model id belongs to an
    embedding/image/audio family, the upstream returns 405 Method Not
    Allowed and the user gets a confusing error bubble. Catching the
    case here means we can return a precise message in the user's
    language. The detection logic lives in
    :func:`backend.services.model_aggregator.detect_model_type` and
    defaults to ``chat`` for unknown ids, so a brand-new chat model
    is never blocked by mistake.
    """
    from backend.services.model_aggregator import detect_model_type

    model_type = detect_model_type(model)
    if model_type == "chat":
        return
    label = _TYPE_LABELS.get(model_type, model_type)
    raise HTTPException(
        status_code=400,
        detail=f"模型 {model} 是{label}，不能用于对话。请在右上角选择支持聊天的模型后再发送。",
    )


def _check_provider_ready(provider: str) -> None:
    """Verify the resolved provider has a configured API key.

    Raises HTTPException(400) with a user-friendly message when the
    admin has not yet configured the provider. This prevents the
    confusing ``UPSTREAM_API_KEY_MISSING`` 500 error from proxy_service.
    """
    if provider.startswith("custom:"):
        from backend.services import custom_providers

        slug = provider.split(":", 1)[1]
        cfg = custom_providers.get_custom_provider(slug)
        if not cfg or not custom_providers.parse_keys(cfg):
            raise HTTPException(
                status_code=400,
                detail=f"模型 {provider} 对应的自定义平台尚未配置 API Key，请联系管理员。",
            )
        return

    api_key = get_setting(f"{provider}_api_key") or ""
    enabled = (get_setting(f"{provider}_enabled") or "true") == "true"
    if not enabled:
        raise HTTPException(
            status_code=400,
            detail=f"模型所属的 {provider} 平台已被禁用，请联系管理员。",
        )
    if not api_key or "your-" in api_key.lower():
        raise HTTPException(
            status_code=400,
            detail=f"模型所属的 {provider} 平台尚未配置 API Key，请联系管理员。",
        )


def _extract_chat_error(result: dict, status_code: int) -> str:
    """Extract a human-readable error from an upstream error response.

    Mirrors the error extraction logic in proxy_service so the
    non-streaming chat endpoint surfaces the same quality of error
    detail as the streaming variant.
    """
    if not isinstance(result, dict):
        return f"上游服务错误 (HTTP {status_code})"

    # proxy_service standard error codes
    err = result.get("error")
    if isinstance(err, str) and err:
        if err == "UPSTREAM_API_KEY_MISSING":
            return "上游平台 API Key 未配置，请联系管理员。"
        if err == "CIRCUIT_OPEN":
            return "上游平台暂时不可用（熔断），请稍后重试。"
        return f"上游错误: {err}"
    if isinstance(err, dict):
        msg = err.get("message") or err.get("detail") or err.get("type")
        if msg:
            return f"上游错误: {msg}"

    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        return detail

    return f"上游服务错误 (HTTP {status_code})"


# ---------------------------------------------------------------------------
# Conversation history loading
# ---------------------------------------------------------------------------

MAX_HISTORY_MESSAGES = 20  # ~10 full user/assistant turns


def _load_conversation_history(
    user_id: int, session_id: str, *, limit: int = MAX_HISTORY_MESSAGES
) -> tuple[list[dict[str, str]], bool]:
    """Load recent conversation turns for context enrichment.

    Returns a ``(messages, truncated)`` tuple where ``messages`` is a
    list of ``{{"role": ..., "content": ...}}`` dicts in chronological
    order (oldest first), suitable for inclusion in the upstream
    ``messages`` array, and ``truncated`` is ``True`` when the session
    has more than ``limit`` rows total — i.e. older turns were dropped
    from the context window. The limit caps the number of *past* turns
    so the combined history + new message does not blow through the
    upstream model's context window.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, content FROM conversations
             WHERE user_id = ? AND session_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (user_id, session_id, limit),
        )
        rows = cursor.fetchall()
        # Total row count for the session — drives the truncated flag
        # so callers can surface a "history truncated" hint to the user.
        cursor.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
        total = int((cursor.fetchone() or {"n": 0})["n"] or 0)
    # Rows come back newest-first; flip to chronological order.
    messages = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    truncated = total > limit
    return messages, truncated
