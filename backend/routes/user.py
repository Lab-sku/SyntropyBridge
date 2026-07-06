import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.database import get_db_context, get_usage_windows
from backend.services.token_service import TokenService
from backend.services.user_service import UserService
from backend.session import CSRF_COOKIE, USER_SESSION_COOKIE, get_session

router = APIRouter()


def require_user_session(request: Request) -> dict:
    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="未授权")
    session = get_session(session_id, user_agent=request.headers.get("User-Agent"))
    if not session or session.get("role") != "user":
        raise HTTPException(status_code=401, detail="未授权")
    return session


def get_user_session_or_none(request: Request):
    """Return the user session dict if valid, else None.

    Used by read-only endpoints that may be hit incidentally from an
    admin context (e.g., NotificationsBell mounts for admins too and
    polls /user/notifications/unread-count). Returning None (instead
    of 401) lets the handler return a safe default — this avoids
    tripping the frontend's aggressive 401 → session-expired →
    redirect-to-login handler, which would otherwise log the admin
    out mid-browse.
    """
    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if not session_id:
        return None
    session = get_session(session_id, user_agent=request.headers.get("User-Agent"))
    if not session or session.get("role") != "user":
        return None
    return session


def require_user_csrf(request: Request, session: dict = Depends(require_user_session)) -> dict:
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


class TokenCreateRequest(BaseModel):
    name: str | None = None
    expires_days: int | None = None
    allowed_models: list[str] | None = None
    allowed_ips: list[str] | None = None
    rate_limit_per_minute: int | None = None
    rate_limit_per_hour: int | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    """Self-service profile update.

    The user can change their own username and email. The service layer
    enforces length (2-50 chars) and uniqueness for username, and basic
    format + uniqueness for email. Password rotation has its own
    dedicated endpoint /user/password — that one is the only place that
    accepts the old password for verification, and is the only endpoint
    allowed to touch ``password_hash``.

    P1.2: changing the email now requires ``current_password`` to be
    supplied and verified, so a stolen session cookie cannot be used
    to silently take over the account by routing the verification
    email to the attacker's address.
    """

    username: str | None = None
    email: str | None = None
    monthly_budget: float | None = None
    current_password: str | None = None


@router.post("/user/password")
async def change_password(
    payload: ChangePasswordRequest, request: Request, session: dict = Depends(require_user_csrf)
):
    """Authenticated user rotates their own password.

    The user must submit their current password to prove identity. The
    new password is validated with the same strong-password rules
    that ``/api/auth/register`` uses, so users can't accidentally
    downgrade their account security. On success every server-side
    session for this user is invalidated, so the user (and anyone
    holding a stolen session) has to re-authenticate.
    """
    user_id = int(session.get("user_id"))
    from backend.database import get_client_ip
    from backend.services.audit import log_action

    try:
        UserService.change_password(
            user_id=int(user_id),
            old_password=payload.old_password,
            new_password=payload.new_password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Audit: record that a password rotation happened. We do NOT log
    # the new password (or its hash) — only the fact, with IP for
    # forensic correlation. Use get_client_ip (X-Forwarded-For aware)
    # rather than request.client.host so the audit is correct behind
    # the configured reverse proxy / load balancer.
    log_action(
        actor_id=user_id,
        actor_type="user",
        action="USER_CHANGE_PASSWORD",
        target_type="user",
        target_id=user_id,
        details=None,
        ip_address=get_client_ip(request),
    )
    return {"message": "密码已更新"}


@router.patch("/user/profile")
async def update_profile(
    payload: ProfileUpdateRequest, request: Request, session: dict = Depends(require_user_csrf)
):
    """Self-service profile update (username, email).

    Both fields are optional and partial — sending ``{"username": "x"}``
    touches only the username. The endpoint refuses to touch
    ``password_hash`` (use ``/user/password`` for that) and refuses
    anything privileged like quota / active flag / plan. All changes
    are recorded in ``audit_logs`` with the old and new values.

    P1.2: changing the email requires the current password to be
    supplied and verified, so a stolen session cookie cannot silently
    reroute the verification email to an attacker-controlled address.
    """
    user_id = int(session.get("user_id"))
    from backend.database import get_client_ip, get_db_context
    from backend.models import UserUpdate
    from backend.security import Security as _Security
    from backend.services.audit import log_action

    # Fetch old values first so the audit log carries a precise diff
    # (and so we can detect a no-op update that should not generate a
    # log row).
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, email, password_hash FROM users WHERE id = ?",
            (int(user_id),),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    old_username = row["username"]
    old_email = row["email"]

    # P1.2: if the caller is trying to change the email, require the
    # current password. Without this, a stolen session cookie could
    # silently reroute the verification email to an attacker-controlled
    # address and form an account-takeover chain.
    normalized_new_email = (payload.email or "").strip().lower() or None
    normalized_old_email = (old_email or "").strip().lower() or None
    if (
        payload.email is not None
        and normalized_new_email != normalized_old_email
    ):
        if not payload.current_password:
            raise HTTPException(
                status_code=400,
                detail="修改邮箱需要提供当前密码",
            )
        if not _Security.verify_password(
            payload.current_password, row["password_hash"] or ""
        ):
            raise HTTPException(status_code=400, detail="密码验证失败")

    try:
        UserService.update_user(
            int(user_id),
            UserUpdate(username=payload.username, email=payload.email),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if payload.monthly_budget is not None:
        if payload.monthly_budget < 0:
            raise HTTPException(status_code=400, detail="月度预算不能为负数")
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET monthly_budget = ? WHERE id = ?",
                (payload.monthly_budget, int(user_id)),
            )

    # Only write an audit row when the field actually changed. Empty
    # email payload means "clear email" — compare normalized.
    ip = get_client_ip(request)
    if payload.username is not None and payload.username != old_username:
        log_action(
            actor_id=user_id,
            actor_type="user",
            action="USER_CHANGE_USERNAME",
            target_type="user",
            target_id=user_id,
            details={"old": old_username, "new": payload.username},
            ip_address=ip,
        )
    normalized_new_email = (payload.email or "").strip().lower() or None
    normalized_old_email = (old_email or "").strip().lower() or None
    if payload.email is not None and normalized_new_email != normalized_old_email:
        log_action(
            actor_id=user_id,
            actor_type="user",
            action="USER_CHANGE_EMAIL",
            target_type="user",
            target_id=user_id,
            details={"old": normalized_old_email, "new": normalized_new_email},
            ip_address=ip,
        )
        # P1.2: best-effort notify the *old* email address so the
        # real owner has a signal if a stolen session was used to
        # silently swap the address. Failures are swallowed — a
        # notification hiccup must not roll back the profile update.
        if normalized_old_email:
            try:
                from backend.services.email_service import EmailService

                email_svc = EmailService()
                if email_svc.smtp_host and email_svc.smtp_user:
                    email_svc.send_email(
                        normalized_old_email,
                        "您的邮箱已被修改",
                        (
                            "您的账户邮箱已被修改。如果这是您本人的操作，请忽略此邮件。"
                            "如果不是您本人操作，请立即登录修改密码并联系客服。"
                        ),
                    )
            except Exception:
                pass
    return {"message": "已更新"}


@router.get("/user/stats")
async def get_user_stats(request: Request):
    session = require_user_session(request)
    user_id = int(session.get("user_id"))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT quota_5h, quota_week FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

    usage_5h, usage_week = get_usage_windows(user_id)

    return {
        "usage_5h": usage_5h,
        "usage_week": usage_week,
        "quota_5h": user["quota_5h"],
        "quota_week": user["quota_week"],
    }


@router.get("/user/config")
async def get_user_config(request: Request):
    session = require_user_session(request)
    user_id = int(session.get("user_id"))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT api_key FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

    raw_key = user["api_key"] or ""
    masked = f"{raw_key[:6]}...{raw_key[-4:]}" if len(raw_key) > 10 else ""
    return {"api_key": masked, "api_base": "/api/v1"}


@router.get("/user/tokens")
async def list_user_tokens(request: Request):
    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    return TokenService.list_tokens(user_id=user_id)


@router.get("/user/tokens-stats")
async def list_user_tokens_stats(request: Request):
    session = require_user_session(request)
    user_id = int(session.get("user_id"))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                t.id AS token_id,
                t.last_used_at AS last_used_at,
                COALESCE(SUM(CASE WHEN l.request_time > datetime('now', '-24 hours') THEN l.total_tokens ELSE 0 END), 0) AS usage_24h,
                COALESCE(SUM(CASE WHEN l.request_time > datetime('now', '-7 days') THEN l.total_tokens ELSE 0 END), 0) AS usage_7d
            FROM tokens t
            LEFT JOIN usage_logs l ON l.token_id = t.id
            WHERE t.user_id = ?
            GROUP BY t.id
            ORDER BY t.created_at DESC, t.id DESC
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

    results: list[dict] = []
    for row in rows:
        results.append(
            {
                "id": int(row["token_id"]),
                "usage_24h": int(row["usage_24h"] or 0),
                "usage_7d": int(row["usage_7d"] or 0),
                "last_used_at": row["last_used_at"],
            }
        )
    return results


@router.post("/user/tokens")
async def create_user_token(
    req: TokenCreateRequest, request: Request, session: dict = Depends(require_user_csrf)
):
    user_id = int(session.get("user_id"))
    expires_at = None
    if req.expires_days is not None:
        days = int(req.expires_days)
        if days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    result = TokenService.create_token(
        user_id=user_id,
        name=req.name,
        expires_at=expires_at,
        allowed_models=req.allowed_models,
        allowed_ips=req.allowed_ips,
        rate_limit_per_minute=req.rate_limit_per_minute,
        rate_limit_per_hour=req.rate_limit_per_hour,
    )
    return {"id": result["id"], "token": result["token"], "token_prefix": result["token_prefix"]}


@router.post("/user/tokens/{token_id}/disable")
async def disable_user_token(
    token_id: int, request: Request, session: dict = Depends(require_user_csrf)
):
    user_id = int(session.get("user_id"))
    ok = TokenService.disable_token(user_id=user_id, token_id=token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token 不存在或已吊销")
    return {"message": "已禁用"}


@router.post("/user/tokens/{token_id}/revoke")
async def revoke_user_token(
    token_id: int, request: Request, session: dict = Depends(require_user_csrf)
):
    user_id = int(session.get("user_id"))
    ok = TokenService.revoke_token(user_id=user_id, token_id=token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token 不存在或已吊销")
    return {"message": "已吊销"}


@router.get("/user/logs")
async def list_user_logs(request: Request, limit: int = 200):
    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    limit = max(1, min(int(limit or 200), 500))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT endpoint, model, total_tokens, response_time_ms, status_code, request_time, metadata
            FROM usage_logs
            WHERE user_id = ?
            ORDER BY request_time DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cursor.fetchall()

    results = []
    for row in rows:
        meta = {}
        if row.get("metadata"):
            try:
                meta = json.loads(row["metadata"]) or {}
            except Exception:
                meta = {}
        results.append(
            {
                "endpoint": row["endpoint"],
                "model": row["model"],
                "total_tokens": row["total_tokens"],
                "response_time_ms": row["response_time_ms"],
                "status_code": row["status_code"],
                "request_time": row["request_time"],
                "metadata": meta,
            }
        )
    return results


@router.get("/user/models")
async def list_user_available_models(request: Request):
    """Return the unified model catalog visible to an end-user.

    Returns *only* models from providers the admin has actually
    configured (a real API key set, an enabled flag, etc.). The
    previous version hard-coded two built-in ``minimax`` slots at the
    top of the list — that was wrong: users on a clean install saw
    the "MiniMax" models in the picker, clicked them, and got a 401
    or 502 because there was no key behind them. This endpoint now
    derives the catalog from :func:`get_cached_provider_models`,
    which already enforces the "configured" check.
    """
    # any logged-in user (admin or regular) is fine — admins will
    # already have the richer /api/admin/models endpoint, this one is
    # for the user-facing chat picker.
    sid = request.cookies.get(USER_SESSION_COOKIE) or request.cookies.get("mm_admin_session")
    if not sid:
        raise HTTPException(status_code=401, detail="未授权")

    from backend.database import get_pricing_for_model_list
    from backend.services.model_aggregator import detect_model_type, get_cached_provider_models

    out: list[dict] = []
    for entry in get_cached_provider_models():
        provider_name = entry.get("provider")
        for m in entry.get("models", []):
            model_id = m.get("model_id") or m.get("id")
            if not model_id:
                continue
            # Always ensure the provider prefix is present.  The
            # ``models`` table may store either bare ids (``MiniMax-M1``)
            # or ids that themselves contain a slash
            # (``meta/llama-3.3-70b`` from NVIDIA).  Using
            # ``startswith`` is more robust than a ``/`` presence check.
            raw = str(model_id)
            prefix = f"{provider_name}/"
            if raw.startswith(prefix):
                name = raw
            else:
                name = f"{prefix}{raw}"
            out.append(
                {
                    "provider": provider_name,
                    "name": name,
                    "display_name": m.get("display_name") or raw,
                    "context_length": m.get("context_length") or 0,
                    "enabled": bool(m.get("is_active", 1)),
                    # ``type`` lets the chat surface hide embedding / image
                    # / audio models that share the same picker — without
                    # this, picking e.g. ``nvidia/nv-embedcode-7b-v1``
                    # forwards a chat-completion request that NVIDIA
                    # rejects with ``405 Method Not Allowed``.
                    "type": detect_model_type(raw),
                }
            )

    # Attach the effective pricing for each model so the chat picker
    # can show the per-1k input / output cost next to the model name
    # without an extra round-trip. Admin-custom prices take precedence
    # over the official default.
    if out:
        pricing_map = get_pricing_for_model_list([m["name"] for m in out])
        for m in out:
            p = pricing_map.get(m["name"])
            if p:
                m["pricing"] = {
                    "input_per_1k": float(p.get("input_price_per_1k") or 0.0),
                    "output_per_1k": float(p.get("output_price_per_1k") or 0.0),
                    "currency": "credits",
                    "tier": p.get("tier"),
                }
    return out


@router.get("/user/providers-summary")
async def list_user_providers_summary(request: Request):
    """Per-provider enabled/visible status for the chat UI.

    Returns a tiny summary of which upstream providers are wired up so
    the chat page can show a hint about the model source.
    """
    sid = request.cookies.get(USER_SESSION_COOKIE) or request.cookies.get("mm_admin_session")
    if not sid:
        raise HTTPException(status_code=401, detail="未授权")
    from backend.database import get_setting
    from backend.providers import list_providers

    out = []
    for p in list_providers():
        api_key = get_setting(p["api_key_setting"]) or ""
        api_base = get_setting(p["api_base_setting"]) or p.get("default_api_base", "")
        enabled = (get_setting(f"{p['name']}_enabled") or "true") == "true"
        configured = bool(api_key) and "your-" not in api_key.lower()
        out.append(
            {
                "name": p["name"],
                "display_name": p.get("display_name", p["name"]),
                "enabled": enabled,
                "configured": configured,
                "api_base": api_base,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/user/notifications")
async def list_notifications(
    request: Request,
    limit: int = 50,
    unread_only: bool = False,
):
    """List notifications for the authenticated user.

    Returns [] for admin callers (no user session) so the
    NotificationsBell doesn't trip the 401 → logout path.
    """
    session = get_user_session_or_none(request)
    if not session:
        return []
    user_id = int(session.get("user_id"))
    from backend.services.notification_service import NotificationService

    limit = max(1, min(int(limit or 50), 200))
    return NotificationService.list_for_user(user_id, limit=limit, unread_only=bool(unread_only))


@router.get("/user/notifications/unread-count")
async def get_unread_count(request: Request):
    """Return the unread notification count for the badge.

    Returns {"count": 0} for admin caller so the bell shows no badge
    and doesn't trip the 401 → logout path.
    """
    session = get_user_session_or_none(request)
    if not session:
        return {"count": 0}
    user_id = int(session.get("user_id"))
    from backend.services.notification_service import NotificationService

    return {"count": NotificationService.unread_count(user_id)}


@router.post("/user/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    request: Request,
    session: dict = Depends(require_user_csrf),
):
    """Mark a single notification as read."""
    user_id = int(session.get("user_id"))
    from backend.services.notification_service import NotificationService

    ok = NotificationService.mark_read(user_id, notification_id)
    if not ok:
        raise HTTPException(status_code=404, detail="通知不存在或已读")
    return {"ok": True}


@router.post("/user/notifications/read-all")
async def mark_all_notifications_read(
    request: Request,
    session: dict = Depends(require_user_csrf),
):
    """Mark all notifications as read."""
    user_id = int(session.get("user_id"))
    from backend.services.notification_service import NotificationService

    count = NotificationService.mark_all_read(user_id)
    return {"updated": count}


# ---------------------------------------------------------------------------
# Usage dashboard (session-cookie auth, complements API-key usage.py routes)
# ---------------------------------------------------------------------------


@router.get("/user/dashboard/summary")
async def dashboard_summary(request: Request):
    """Quota usage snapshot for the user-facing dashboard.

    Returns used/limit/percent for 5h, week, month windows plus monthly
    budget, wallet balance, and current plan info.
    """
    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    return UserService.get_usage_summary(user_id)


@router.get("/user/dashboard/chart")
async def dashboard_chart(request: Request, range: str = "30d"):
    """Daily usage data (requests, tokens, cost) for the line chart.

    ``range`` is one of ``7d``, ``30d``, ``90d``. Defaults to 30 days.
    """
    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    range_map = {"7d": 7, "30d": 30, "90d": 90, "1d": 1}
    days = range_map.get(range, 30)
    data = UserService.get_usage_chart(user_id, range_days=days)
    return {"range": range, "data": data}


@router.get("/user/dashboard/by-model")
async def dashboard_by_model(request: Request, range: str = "30d"):
    """Per-model usage breakdown (requests, tokens, cost) for the pie chart."""
    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    range_map = {"7d": 7, "30d": 30, "90d": 90, "1d": 1}
    days = range_map.get(range, 30)
    data = UserService.get_usage_by_model(user_id, range_days=days)
    return {"data": data}


@router.get("/user/dashboard/export.csv")
async def dashboard_export_csv(request: Request, range: str = "30d"):
    """Export the user's own usage_logs as CSV.

    Strictly scoped to ``user_id = session.user_id`` — no cross-user
    leakage is possible because the WHERE clause always includes the
    session user_id.
    """
    import csv
    import io

    from fastapi.responses import PlainTextResponse

    session = require_user_session(request)
    user_id = int(session.get("user_id"))
    range_map = {"7d": 7, "30d": 30, "90d": 90, "1d": 1}
    days = range_map.get(range, 30)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, model, provider, prompt_tokens, completion_tokens,
                   cost_credits, status_code, request_time
            FROM usage_logs
            WHERE user_id = ?
              AND request_time > datetime('now', ?)
            ORDER BY request_time DESC
            """,
            (user_id, f"-{days} days"),
        )
        rows = cursor.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "model",
            "provider",
            "prompt_tokens",
            "completion_tokens",
            "cost_credits",
            "status_code",
            "request_time",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["id"],
                r["model"],
                r["provider"],
                r["prompt_tokens"],
                r["completion_tokens"],
                r["cost_credits"],
                r["status_code"],
                r["request_time"],
            ]
        )

    return PlainTextResponse(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_{user_id}.csv"},
    )


# ---------------------------------------------------------------------------
# GDPR: Data export & account deletion
# ---------------------------------------------------------------------------


class DeleteAccountRequest(BaseModel):
    password: str


@router.get("/user/data/export")
async def export_user_data(
    request: Request,
    session: dict = Depends(require_user_session),
):
    user_id = int(session.get("user_id"))

    from backend.database import check_rate_limit
    allowed, _ = check_rate_limit(f"gdpr_export:{user_id}", "gdpr_export:3600", 1, 3600)
    if not allowed:
        raise HTTPException(status_code=429, detail="导出操作过于频繁，每小时仅允许一次")

    from fastapi.responses import JSONResponse

    data = {}

    with get_db_context() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, username, email, created_at, is_active, quota_5h, quota_week, monthly_budget FROM users WHERE id = ?",
            (user_id,),
        )
        profile = cursor.fetchone()
        data["profile"] = dict(profile) if profile else {}

        cursor.execute(
            "SELECT id, model, provider, prompt_tokens, completion_tokens, total_tokens, cost_credits, status_code, request_time FROM usage_logs WHERE user_id = ? ORDER BY request_time DESC LIMIT 10000",
            (user_id,),
        )
        data["usage_logs"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT id, type, amount, balance_after, related_type, related_id, note, created_at FROM wallet_transactions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        data["wallet_transactions"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT id, role, content, model, title, created_at FROM conversations WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        data["conversations"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT id, name, key_prefix, key_mask, is_active, last_used_at, created_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        data["api_keys"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT id, type, title, content, is_read, created_at FROM notifications WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        data["notifications"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT id, name, token_prefix, is_active, last_used_at, created_at FROM tokens WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        data["tokens"] = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT balance, frozen, total_recharged, total_consumed FROM wallets WHERE user_id = ?",
            (user_id,),
        )
        wallet = cursor.fetchone()
        data["wallet"] = dict(wallet) if wallet else {}

    data["exported_at"] = datetime.now(timezone.utc).isoformat()
    data["user_id"] = user_id

    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f"attachment; filename=user_data_{user_id}.json"},
    )


@router.post("/user/data/delete")
async def delete_user_account(
    payload: DeleteAccountRequest,
    request: Request,
    session: dict = Depends(require_user_csrf),
):
    user_id = int(session.get("user_id"))

    from backend.database import get_db
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        user_row = cursor.fetchone()
    finally:
        conn.close()

    if not user_row:
        raise HTTPException(status_code=404, detail="用户不存在")

    from backend.security import Security
    if not Security.verify_password(payload.password, user_row["password_hash"] or ""):
        raise HTTPException(status_code=403, detail="密码验证失败")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("UPDATE users SET is_active = 0, email = NULL WHERE id = ?", (user_id,))
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        cursor.execute("UPDATE tokens SET is_active = 0, revoked_at = datetime('now') WHERE user_id = ?", (user_id,))
        cursor.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ?", (user_id,))
        cursor.execute(
            "UPDATE users SET username = ? WHERE id = ?",
            (f"deleted_{user_id}_{int(datetime.now(timezone.utc).timestamp())}", user_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    from backend.session import USER_SESSION_COOKIE, delete_session
    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if session_id:
        delete_session(session_id)

    return {"message": "账号已删除，数据将在 30 天后彻底清除"}
