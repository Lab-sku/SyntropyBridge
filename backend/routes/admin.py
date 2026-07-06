import asyncio
import csv
import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from backend.config import Config
from backend.database import (
    add_audit_log,
    admin_exists,
    check_rate_limit,
    create_admin_user,
    get_admin_by_username,
    get_client_ip,
    get_db_context,
    get_setting,
    set_setting,
    update_admin_last_login,
)
from backend.models import UserCreate, UserResponse, UserUpdate
from backend.security import Security
from backend.services.channel_service import ChannelService
from backend.services.custom_providers import _validate_provider_url
from backend.services.email_service import EmailService
from backend.services.http_client import post_with_retry
from backend.services.lockout import check_allowed, record_failure, record_success
from backend.services.totp_service import TOTPService
from backend.services.user_service import UserService
from backend.session import (
    ADMIN_SESSION_COOKIE,
    CSRF_COOKIE,
    REMEMBER_ME_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    USER_SESSION_COOKIE,
    create_session,
    csrf_cookie_kwargs,
    delete_session,
    get_session,
    session_cookie_kwargs,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_admin_password_with_lockout(
    admin_id: int, admin_password: str, client_ip: str | None
) -> None:
    """Verify the admin's password for a sensitive operation and bump
    the brute-force lockout on failure.

    P1.7: every state-changing admin endpoint that re-authenticates
    with a password (reveal-API-key, kill-switch activate/release,
    revoke-all-sessions, bulk-refund, change-admin-password) goes
    through this helper so a stolen session cookie cannot be used to
    brute the admin password unimpeded. The lockout is per-admin AND
    per-IP (additive), re-using the SQLite ``auth_failures`` table
    that the user login path already uses.

    Raises:
        HTTPException(429): admin_id or client_ip is currently locked.
        HTTPException(403): password verification failed.
    """
    admin_ident = str(admin_id) if admin_id else ""
    ip_ident = client_ip or ""

    admin_lock = check_allowed(admin_ident, scope="admin_pw")
    if not admin_lock.allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ip_lock = check_allowed(ip_ident, scope="ip")
    if not ip_lock.allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT password_hash FROM admin_users WHERE id = ?", (admin_id,)
        )
        admin_row = cursor.fetchone()

    if not admin_row or not Security.verify_password(
        admin_password, admin_row["password_hash"] or ""
    ):
        record_failure(admin_ident, scope="admin_pw")
        record_failure(ip_ident, scope="ip")
        raise HTTPException(status_code=403, detail="管理员密码验证失败")

    record_success(admin_ident, scope="admin_pw")
    record_success(ip_ident, scope="ip")


class ChannelUpsertRequest(BaseModel):
    provider: str
    name: str
    base_url: str
    api_key: Optional[str] = None
    weight: int = 100
    is_active: bool = True


class ChannelPatchRequest(BaseModel):
    """Partial update — only the supplied fields are applied."""

    provider: Optional[str] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    weight: Optional[int] = None
    is_active: Optional[bool] = None


class ChannelTestRequest(BaseModel):
    model: Optional[str] = None


class ModelMapRequest(BaseModel):
    mapping: dict


class AdminInitRequest(BaseModel):
    username: str
    password: str


class AdminLoginRequest(BaseModel):
    username: str
    password: str
    # When True the session is extended to REMEMBER_ME_TTL_SECONDS
    # (default 30 days). Default False keeps the regular short
    # SESSION_TTL_SECONDS (1 hour).
    remember: bool = False
    totp_code: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, v: str):
        # P2.5: cap the password length on the admin login path so an
        # attacker can't DoS the (deliberately expensive) PBKDF2
        # verify by submitting a multi-megabyte string. 1024 chars is
        # well above any reasonable password manager output.
        if not isinstance(v, str) or len(v) > 1024:
            raise ValueError("密码长度不能超过 1024 个字符")
        return v


class AdminChangePasswordRequest(BaseModel):
    """Admin-initiated password rotation.

    Either the current password (self-service) OR an admin with
    sufficient privilege can rotate a password. We support both
    because the previous behaviour had no way to recover from a
    forgotten admin password without a DB dive.
    """

    old_password: Optional[str] = None
    new_password: str


class AdminResetPasswordRequest(BaseModel):
    """Super-admin password reset (no old_password required)."""

    new_password: str


class FreezeUserRequest(BaseModel):
    reason: str = ""


class AdminTOTPVerifyRequest(BaseModel):
    code: str


class AdminTOTPDisableRequest(BaseModel):
    password: str


def require_admin_session(request: Request) -> dict:
    session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="未授权")
    session = get_session(session_id, user_agent=request.headers.get("User-Agent"))
    if not session or session.get("role") != "admin":
        raise HTTPException(status_code=401, detail="未授权")
    return session


def require_csrf(request: Request, session: dict = Depends(require_admin_session)) -> dict:
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


@router.get("/admin/channels")
async def list_channels(
    request: Request,
    provider: Optional[str] = None,
    _session: dict = Depends(require_admin_session),
):
    return ChannelService.list_channels(provider=provider)


@router.post("/admin/channels")
async def create_channel(
    req: ChannelUpsertRequest, request: Request, session: dict = Depends(require_csrf)
):
    if not req.api_key:
        raise HTTPException(status_code=400, detail="api_key 不能为空")
    try:
        cid = ChannelService.create_channel(
            provider=req.provider,
            name=req.name,
            base_url=req.base_url,
            api_key=req.api_key,
            weight=req.weight,
            is_active=bool(req.is_active),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_CREATE_CHANNEL",
        target_type="channel",
        target_id=str(cid),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"provider": req.provider, "name": req.name},
    )
    return {"id": cid}


@router.put("/admin/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    req: ChannelUpsertRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    try:
        ok = ChannelService.update_channel(
            channel_id=channel_id,
            provider=req.provider,
            name=req.name,
            base_url=req.base_url,
            api_key=req.api_key,
            weight=req.weight,
            is_active=bool(req.is_active),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_UPDATE_CHANNEL",
        target_type="channel",
        target_id=str(channel_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={
            "provider": req.provider,
            "name": req.name,
            "is_active": bool(req.is_active),
            "weight": req.weight,
        },
    )
    return {"message": "ok"}


@router.patch("/admin/channels/{channel_id}")
async def patch_channel(
    channel_id: int,
    req: ChannelPatchRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """Partial update — only the supplied fields are applied."""
    # Fetch current row to merge with the patch.
    current = ChannelService.get_channel_secret(channel_id)
    if not current:
        raise HTTPException(status_code=404, detail="渠道不存在")

    provider = req.provider if req.provider is not None else current.provider
    name = req.name if req.name is not None else current.name
    base_url = req.base_url if req.base_url is not None else current.base_url
    weight = req.weight if req.weight is not None else current.weight
    is_active = req.is_active if req.is_active is not None else current.is_active
    # api_key: None means "don't change"; empty string also means "don't change".
    api_key = req.api_key if (req.api_key is not None and req.api_key.strip()) else None

    try:
        ok = ChannelService.update_channel(
            channel_id=channel_id,
            provider=provider,
            name=name,
            base_url=base_url,
            api_key=api_key,
            weight=weight,
            is_active=is_active,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_PATCH_CHANNEL",
        target_type="channel",
        target_id=str(channel_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={
            "patched_fields": [
                k
                for k in ("provider", "name", "base_url", "api_key", "weight", "is_active")
                if getattr(req, k) is not None
            ]
        },
    )
    return {"message": "ok"}


@router.post("/admin/channels/{channel_id}/reset-cooldown")
async def reset_channel_cooldown(
    channel_id: int, request: Request, session: dict = Depends(require_csrf)
):
    """Manually clear the cooldown timer so the channel re-enters rotation."""
    if not ChannelService.reset_cooldown(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_RESET_CHANNEL_COOLDOWN",
        target_type="channel",
        target_id=str(channel_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "ok"}


@router.post("/admin/channels/{channel_id}/toggle-active")
async def toggle_channel_active(
    channel_id: int, request: Request, session: dict = Depends(require_csrf)
):
    """Flip the is_active flag on a channel."""
    new_state = ChannelService.toggle_active(channel_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="渠道不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_TOGGLE_CHANNEL",
        target_type="channel",
        target_id=str(channel_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"is_active": new_state},
    )
    return {"message": "ok", "is_active": new_state}


@router.delete("/admin/channels/{channel_id}")
async def delete_channel(channel_id: int, request: Request, session: dict = Depends(require_csrf)):
    if not ChannelService.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="渠道不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_DELETE_CHANNEL",
        target_type="channel",
        target_id=str(channel_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "ok"}


@router.post("/admin/channels/{channel_id}/test")
async def test_channel(
    channel_id: int,
    body: ChannelTestRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    channel = ChannelService.get_channel_secret(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")

    # SSRF guard: reject internal/private addresses before making the
    # outbound HTTP call.
    try:
        _validate_provider_url(channel.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Per-provider chat endpoint paths.  Most providers speak the
    # OpenAI-compatible /v1/chat/completions; the exceptions are
    # MiniMax (proprietary path), Anthropic (/v1/messages), and
    # Google (model-specific generateContent URL, handled separately).
    _CHAT_PATH_BY_PROVIDER = {
        "minimax": "/v1/text/chatcompletion_v2",
        "anthropic": "/v1/messages",
    }

    def _chat_path(provider: str) -> str:
        return _CHAT_PATH_BY_PROVIDER.get(provider, "/v1/chat/completions")

    def _build_url(base_url: str, path: str) -> str:
        """Join base URL with path, deduplicating any /v1 segment."""
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return f"{base}{path}"

    provider = channel.provider
    model = body.model

    # Google uses a model-specific URL (/v1beta/models/{model}:generateContent)
    # and a different request format.  For a lightweight connectivity test we
    # hit the models list endpoint instead, which doesn't require a model id.
    if provider == "google":
        url = f"{channel.base_url.rstrip('/')}/v1beta/models"
        payload = None  # will use GET below
    else:
        url = _build_url(channel.base_url, _chat_path(provider))
        if not model:
            # Pick a reasonable default probe model per provider family.
            if provider == "minimax":
                model = "MiniMax-M1"
            elif provider == "anthropic":
                model = "claude-3-haiku-20240307"
            else:
                model = "gpt-3.5-turbo"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 1,
            "temperature": 0,
        }

    headers = {
        "Authorization": f"Bearer {channel.api_key}",
        "Content-Type": "application/json",
    }
    # Anthropic uses x-api-key instead of Bearer auth.
    if provider == "anthropic":
        headers = {
            "x-api-key": channel.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    try:
        if payload is None:
            # Google connectivity check (GET /v1beta/models)
            from backend.services.http_client import get_async_client

            client = get_async_client()
            res = await asyncio.wait_for(
                client.get(url, headers={"x-goog-api-key": channel.api_key}),
                timeout=10.0,
            )
        else:
            res = await asyncio.wait_for(
                post_with_retry(
                    url,
                    json=payload,
                    headers=headers,
                    retries=0,
                ),
                timeout=10.0,
            )
        if res.status_code != 200:
            ChannelService.mark_failed(channel_id=channel.id, error=f"health_{res.status_code}")
            raise HTTPException(status_code=400, detail="渠道测试失败")
        ChannelService.mark_healthy(channel_id=channel.id)
        add_audit_log(
            actor_type="admin",
            actor_id=session.get("admin_id"),
            actor_username=session.get("username"),
            action="ADMIN_TEST_CHANNEL",
            target_type="channel",
            target_id=str(channel_id),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
            metadata={"provider": channel.provider, "url": url},
        )
        return {"message": "ok"}
    except HTTPException:
        raise
    except Exception:
        ChannelService.mark_failed(channel_id=channel.id, error="health_error")
        raise HTTPException(status_code=400, detail="渠道测试失败")


@router.get("/admin/model-map")
async def get_model_map(request: Request, _session: dict = Depends(require_admin_session)):
    raw = get_setting("model_provider_map") or ""
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"mapping": data}


@router.put("/admin/model-map")
async def save_model_map(
    body: ModelMapRequest, request: Request, session: dict = Depends(require_csrf)
):
    mapping = body.mapping if isinstance(body.mapping, dict) else {}
    # Accept any registered provider as the value, not just
    # ``minimax``/``nvidia``. The runtime side
    # (chat.py:``_get_provider_for_model`` and proxy.py:``get_provider_for_model``)
    # routes the request to whatever valid provider the admin picks.
    # The previous whitelist silently dropped deepseek/aliyun/
    # moonshot/… entries before they were even persisted, so the
    # admin thought they had configured an override but it had
    # never been saved.
    try:
        from backend.providers.base import ProviderRegistry

        valid = set(ProviderRegistry.all().keys())
    except Exception:
        valid = {"minimax", "nvidia"}
    safe_mapping: dict[str, str] = {}
    for k, v in mapping.items():
        kk = str(k or "").strip()
        vv = str(v or "").strip().lower()
        if not kk or vv not in valid:
            continue
        safe_mapping[kk] = vv

    set_setting("model_provider_map", json.dumps(safe_mapping, ensure_ascii=False))
    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_UPDATE_MODEL_MAP",
        target_type="settings",
        target_id="model_provider_map",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"count": len(safe_mapping)},
    )
    return {"message": "ok", "mapping": safe_mapping}


@router.post("/admin/init")
async def init_admin(req: AdminInitRequest, request: Request):
    client_ip = get_client_ip(request)
    allowed, _remaining = check_rate_limit(client_ip, "admin_init_ip:300", 3, 300)
    if not allowed:
        raise HTTPException(status_code=429, detail="初始化尝试过于频繁，请稍后再试")

    if admin_exists():
        raise HTTPException(status_code=409, detail="管理员已初始化")

    username = req.username.strip()
    if not username or len(username) < 3 or len(username) > 50:
        raise HTTPException(status_code=400, detail="管理员用户名长度必须在 3-50 个字符之间")

    try:
        Security.assert_strong_password(req.password, username=username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    password_hash = Security.hash_password(req.password)
    admin_id = create_admin_user(username, password_hash)

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=username,
        action="ADMIN_INIT",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    return {"message": "管理员初始化成功"}


@router.post("/admin/login")
async def login(req: AdminLoginRequest, request: Request):
    client_ip = get_client_ip(request)
    allowed, _remaining = check_rate_limit(client_ip, "admin_login_ip:60", 5, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")

    if not admin_exists():
        raise HTTPException(status_code=409, detail="管理员未初始化，请先初始化")

    admin_ident = f"admin:{req.username.strip()}"

    # Brute-force lockout (additive layer on top of the rate limiter).
    lock_user = check_allowed(admin_ident, scope="user")
    if not lock_user.allowed:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    lock_ip = check_allowed(client_ip, scope="ip")
    if not lock_ip.allowed:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    admin = get_admin_by_username(req.username.strip())
    if not admin or not admin["is_active"]:
        record_failure(admin_ident, scope="user")
        record_failure(client_ip, scope="ip")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not Security.verify_password(req.password, admin["password_hash"]):
        record_failure(admin_ident, scope="user")
        record_failure(client_ip, scope="ip")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if admin["totp_enabled"]:
        if not req.totp_code:
            raise HTTPException(status_code=401, detail="需要两步验证", headers={"X-Need-TOTP": "true"})
        # totp_secret is stored encrypted (migration 34); decrypt for
        # verification. ``decrypt`` returns the original plaintext for
        # legacy un-encrypted values, so this is backward-compatible.
        totp_secret_plain = Security.decrypt(admin["totp_secret"]) or admin["totp_secret"]
        if not TOTPService.verify(totp_secret_plain, req.totp_code):
            record_failure(admin_ident, scope="user")
            record_failure(client_ip, scope="ip")
            raise HTTPException(status_code=401, detail="两步验证码错误")

    # Successful login — reset any accumulated failure counters.
    record_success(admin_ident, scope="user")
    record_success(client_ip, scope="ip")

    # P2.9: transparently upgrade legacy 100k-iteration hashes to the
    # current 600k format on successful login. Same rationale as the
    # user login path — this is the only moment we have the plaintext
    # password in hand. Best-effort: a failure here must not block the
    # login, the admin can re-authenticate and we'll try again.
    if Security.is_legacy_password_hash(admin["password_hash"] or ""):
        try:
            new_hash = Security.hash_password(req.password)
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE admin_users SET password_hash = ? WHERE id = ?",
                    (new_hash, int(admin["id"])),
                )
        except Exception:
            logger.exception(
                "legacy password hash upgrade failed for admin_id=%s",
                admin["id"],
            )

    update_admin_last_login(int(admin["id"]))

    # ``remember=True`` extends the session lifetime to 30 days. The
    # default is 1 hour. The cookie is HttpOnly + SameSite=Lax, so
    # this is still safe against XSS exfiltration (the JS layer
    # cannot read the value) and against CSRF on the session (state
    # changes still require the X-CSRF-Token header).
    ttl = REMEMBER_ME_TTL_SECONDS if getattr(req, "remember", False) else SESSION_TTL_SECONDS
    session_id, csrf_token = create_session(
        {"role": "admin", "admin_id": int(admin["id"]), "username": admin["username"]},
        ttl_seconds=ttl,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip,
    )

    add_audit_log(
        actor_type="admin",
        actor_id=int(admin["id"]),
        actor_username=admin["username"],
        action="ADMIN_LOGIN",
        ip_address=client_ip,
        user_agent=request.headers.get("User-Agent"),
        metadata={"remember": bool(getattr(req, "remember", False))},
    )

    resp = JSONResponse({"message": "ok", "role": "admin", "ttl": int(ttl)})
    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        delete_session(user_session_id)
        resp.delete_cookie(USER_SESSION_COOKIE, path="/")
    resp.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_id,
        **session_cookie_kwargs(ttl_seconds=ttl),
    )
    resp.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        **csrf_cookie_kwargs(ttl_seconds=ttl),
    )
    return resp


@router.post("/admin/logout")
async def logout(request: Request, session: dict = Depends(require_csrf)):
    session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if session_id:
        delete_session(session_id)

    # Also clear any residual user session to avoid cross-role residue.
    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        delete_session(user_session_id)

    resp = JSONResponse({"message": "已退出登录"})
    resp.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    resp.delete_cookie(USER_SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@router.post("/admin/password")
async def change_admin_password(
    req: AdminChangePasswordRequest, request: Request, session: dict = Depends(require_csrf)
):
    """Authenticated admin rotates their own password.

    Requires the current password (defence against an attacker who
    stole a logged-in browser session and wants to lock the real
    admin out). New password is validated with the same
    :func:`Security.assert_strong_password` rule the init flow uses.
    """
    admin_id = int(session.get("admin_id") or 0)
    if not admin_id:
        raise HTTPException(status_code=401, detail="未授权")
    if not req.old_password:
        raise HTTPException(status_code=400, detail="需要提供原密码")

    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get the same brute-force
    # protection as the login endpoint. The previous code returned a
    # bare 400 with no rate-limiting, so a stolen session cookie could
    # be used to brute the admin password unimpeded.
    _verify_admin_password_with_lockout(
        admin_id, req.old_password, get_client_ip(request)
    )

    try:
        Security.assert_strong_password(req.new_password, username=session.get("username"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin_users SET password_hash = ? WHERE id = ?",
            (Security.hash_password(req.new_password), int(admin_id)),
        )
        cursor.execute("DELETE FROM sessions WHERE admin_id = ?", (int(admin_id),))

    add_audit_log(
        actor_type="admin",
        actor_id=int(admin_id),
        actor_username=session.get("username"),
        action="ADMIN_CHANGE_PASSWORD",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "管理员密码已更新"}


@router.get("/admin/config")
async def get_config(request: Request, _session: dict = Depends(require_admin_session)):
    def mask_secret(value: str) -> str:
        if not value:
            return ""
        tail = value[-4:] if len(value) >= 4 else ""
        return f"****{tail}"

    minimax_key = get_setting("minimax_api_key") or Config.MINIMAX_API_KEY
    nvidia_key = get_setting("nvidia_api_key") or ""

    # SMTP配置
    smtp_host = get_setting("smtp_host") or ""
    smtp_port = get_setting("smtp_port") or "587"
    smtp_user = get_setting("smtp_user") or ""
    smtp_password = get_setting("smtp_password") or ""
    smtp_from = get_setting("smtp_from") or ""
    
    # 邮箱验证开关
    email_verification_enabled = get_setting("email_verification_enabled") or "false"

    return {
        "minimax_api_key": mask_secret(minimax_key) if minimax_key else "",
        "minimax_api_base": get_setting("minimax_api_base") or Config.MINIMAX_API_BASE,
        "nvidia_api_key": mask_secret(nvidia_key) if nvidia_key else "",
        "nvidia_api_base": get_setting("nvidia_api_base") or "https://integrate.api.nvidia.com",
        "enabled_providers": (get_setting("enabled_providers") or "minimax,nvidia").split(","),
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": mask_secret(smtp_password) if smtp_password else "",
        "smtp_from": smtp_from,
        "email_verification_enabled": email_verification_enabled.lower() == "true",
    }


@router.post("/admin/config")
async def save_config(config_data: dict, request: Request, session: dict = Depends(require_csrf)):
    ALLOWED_CONFIG_KEYS = frozenset(
        {
            "minimax_api_key",
            "minimax_api_base",
            "nvidia_api_key",
            "nvidia_api_base",
            "enabled_providers",
            "default_model",
            "default_provider",
            "smtp_host",
            "smtp_port",
            "smtp_user",
            "smtp_password",
            "smtp_from",
            "max_tokens_per_request",
            "global_rate_limit",
            "allow_registration",
            "allow_api_key_login",
            "email_verification_enabled",
        }
    )

    sensitive_keys = ["minimax_api_key", "nvidia_api_key", "admin_password_hash", "smtp_password"]
    updated_keys: list[str] = []

    for key, value in config_data.items():
        if key not in ALLOWED_CONFIG_KEYS:
            continue
        if isinstance(value, list):
            value = ",".join(value)
        encrypt = key in sensitive_keys
        set_setting(key, str(value), encrypt=encrypt)
        updated_keys.append(key)

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_UPDATE_CONFIG",
        target_type="settings",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"keys": updated_keys},
    )

    return {"message": "配置已保存"}


@router.get("/admin/models")
async def get_models(request: Request, _session: dict = Depends(require_admin_session)):
    models = []
    # Always expose the built-in ``minimax`` slot so the admin UI has at
    # least one row to render even when no upstream provider is wired
    # up. Anything else is sourced from the local ``models`` cache that
    # ``/api/admin/models/sync`` populates from the live provider APIs.
    enabled = (get_setting("enabled_providers") or "minimax").split(",")

    if "minimax" in enabled:
        models.append(
            {
                "provider": "minimax",
                "name": "MiniMax-M1",
                "display_name": "MiniMax-M1 (推理模型)",
                "enabled": True,
            }
        )
        models.append(
            {
                "provider": "minimax",
                "name": "MiniMax-Text-01",
                "display_name": "MiniMax-Text-01 (文本模型)",
                "enabled": True,
            }
        )

    # Also surface any models pulled from upstream providers and stored
    # in the local cache. We sort by provider then display name so the
    # admin UI groups them in a stable order.
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT model_id, display_name, provider, context_length, is_active
            FROM models
            WHERE is_active = 1 AND provider != 'minimax'
            ORDER BY provider, display_name
        """)
        for row in cursor.fetchall():
            models.append(
                {
                    "provider": row["provider"],
                    "name": row["model_id"],
                    "display_name": row["display_name"] or row["model_id"],
                    "context_length": row["context_length"] or 0,
                    "enabled": bool(row["is_active"]),
                }
            )

    return models


@router.post("/admin/models/sync")
async def sync_nvidia_models(request: Request, session: dict = Depends(require_csrf)):
    """Pull the live model catalog from every configured upstream provider.

    The previous implementation was hard-coded to NVIDIA only — a real
    problem for admins who configured DeepSeek, OpenAI, Anthropic, etc.
    We now delegate to :func:`fetch_all_provider_models` which iterates
    through ``ProviderRegistry`` (and any custom providers) and caches
    the results in the ``models`` table.
    """

    try:
        from backend.services.model_aggregator import fetch_all_provider_models

        results = await fetch_all_provider_models(force=True)
        total = sum(len(r.get("models", [])) for r in results)
        configured = [r["provider"] for r in results if r.get("configured") and r.get("models")]

        add_audit_log(
            actor_type="admin",
            actor_id=session.get("admin_id"),
            actor_username=session.get("username"),
            action="ADMIN_SYNC_MODELS",
            target_type="models",
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
            metadata={"providers": configured, "count": total},
        )
        return {
            "message": f"已同步 {len(configured)} 个平台，共 {total} 个模型",
            "count": total,
            "providers": configured,
        }
    except Exception:
        logger.exception("Admin model sync failed")
        raise HTTPException(status_code=500, detail="同步失败")


@router.post("/admin/users", response_model=UserResponse)
async def create_user(
    user_data: UserCreate, request: Request, session: dict = Depends(require_csrf)
):
    try:
        created = UserService.create_user(user_data)
        add_audit_log(
            actor_type="admin",
            actor_id=session.get("admin_id"),
            actor_username=session.get("username"),
            action="ADMIN_CREATE_USER",
            target_type="user",
            target_id=str(created.id),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
            metadata={"username": created.username},
        )
        return created
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/admin/users", response_model=List[UserResponse])
async def list_users(request: Request, _session: dict = Depends(require_admin_session)):
    return UserService.list_users()


@router.get("/admin/users/export.csv")
async def export_users_csv(request: Request, _session: dict = Depends(require_admin_session)):
    """Export all users as CSV with UTF-8 BOM for Excel compatibility."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        # BOM so Excel opens UTF-8 correctly
        yield "\ufeff"
        writer.writerow(
            ["id", "username", "email", "is_active", "quota_5h", "quota_week", "created_at"]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, username, email, is_active, quota_5h, quota_week, created_at "
                "FROM users ORDER BY id ASC"
            )
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    writer.writerow(
                        [
                            row["id"],
                            row["username"],
                            row["email"] or "",
                            1 if row["is_active"] else 0,
                            row["quota_5h"],
                            row["quota_week"],
                            row["created_at"] or "",
                        ]
                    )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="users-{today}.csv"'},
    )


@router.get("/admin/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, request: Request, _session: dict = Depends(require_admin_session)):
    user = UserService.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _ensure_users_updated_at_column() -> None:
    """Idempotently add ``updated_at`` to the ``users`` table.

    The column is missing from the baseline migration (only added in
    newer schemas). We guard with ``PRAGMA table_info`` so this is a
    no-op when the column already exists — safe to call on every
    update_user invocation.
    """
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in cursor.fetchall()}
            if "updated_at" not in cols:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                )
    except Exception:
        logger.debug("failed to ensure users.updated_at column", exc_info=True)


@router.put("/admin/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    request: Request,
    session: dict = Depends(require_csrf),
    expected_updated_at: Optional[str] = None,
):
    """Update a user. Supports optimistic locking via ``expected_updated_at``.

    When ``expected_updated_at`` is supplied (query param), the handler
    reads the current ``users.updated_at`` and returns 409 Conflict if
    it doesn't match — preventing concurrent admins from silently
    overwriting each other's edits. When omitted (the current frontend
    behaviour), the update is unconditional for backward compatibility.
    """
    _ensure_users_updated_at_column()

    if expected_updated_at:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT updated_at FROM users WHERE id = ?",
                (int(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="用户不存在")
            current = row["updated_at"]
            if current is not None and str(current) != str(expected_updated_at):
                raise HTTPException(
                    status_code=409,
                    detail="该用户已被其他管理员修改，请刷新后重试",
                )

    try:
        user = UserService.update_user(user_id, user_data)
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        # Bump updated_at so subsequent optimistic-lock checks see the
        # new version. Done in a separate transaction (UserService.update_user
        # uses its own) — there's a tiny race window but it's acceptable
        # for the "简单乐观锁" the audit asked for.
        try:
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (int(user_id),),
                )
        except Exception:
            logger.debug("failed to bump users.updated_at", exc_info=True)
        add_audit_log(
            actor_type="admin",
            actor_id=session.get("admin_id"),
            actor_username=session.get("username"),
            action="ADMIN_UPDATE_USER",
            target_type="user",
            target_id=str(user_id),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, request: Request, session: dict = Depends(require_csrf)):
    if not UserService.delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_DELETE_USER",
        target_type="user",
        target_id=str(user_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "删除成功"}


@router.post("/admin/users/{user_id}/freeze")
async def freeze_user(
    user_id: int,
    body: FreezeUserRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """Freeze a user account: set is_active=0 and invalidate sessions."""
    admin_id = session.get("admin_id")
    if not UserService.freeze_user(user_id, admin_id, body.reason or ""):
        raise HTTPException(status_code=404, detail="用户不存在")
    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_FREEZE_USER",
        target_type="user",
        target_id=str(user_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"reason": body.reason},
    )
    return {"message": "用户已冻结"}


@router.post("/admin/users/{user_id}/unfreeze")
async def unfreeze_user(
    user_id: int,
    body: FreezeUserRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """Unfreeze a user account: set is_active=1."""
    admin_id = session.get("admin_id")
    if not UserService.unfreeze_user(user_id, admin_id, body.reason or ""):
        raise HTTPException(status_code=404, detail="用户不存在")
    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_UNFREEZE_USER",
        target_type="user",
        target_id=str(user_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"reason": body.reason},
    )
    return {"message": "用户已解冻"}


class RevealApiKeyRequest(BaseModel):
    admin_password: str


@router.post("/admin/users/{user_id}/reveal-api-key")
async def admin_reveal_api_key(
    user_id: int,
    payload: RevealApiKeyRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    admin_id = session.get("admin_id")
    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get brute-force protection.
    _verify_admin_password_with_lockout(
        int(admin_id), payload.admin_password, get_client_ip(request)
    )
    # Reveal-API-key is the most sensitive admin operation — it
    # exfiltrates a user's live credential. Restrict it to super-admins
    # so a compromised junior-admin account cannot mass-reveal keys.
    with get_db_context() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_super_admin FROM admin_users WHERE id = ?",
            (admin_id,),
        )
        admin_row = cursor.fetchone()
    if not admin_row or not int(admin_row.get("is_super_admin") or 0):
        raise HTTPException(
            status_code=403,
            detail="仅超级管理员可 revealing 用户 API Key",
        )

    raw_key = UserService.reveal_api_key(user_id)
    if raw_key is None:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    # Audit: log the reveal so the security team can spot abuse.
    from backend.services.audit import log_action

    log_action(
        actor_id=int(admin_id),
        actor_type="admin",
        action="user.api_key.reveal",
        target_type="user",
        target_id=int(user_id),
        details={"reason": "super_admin reveal"},
        ip_address=request.client.host if request.client else None,
    )
    return {"api_key": raw_key}


@router.post("/admin/users/{user_id}/send-reset-email")
async def admin_send_reset_email(
    user_id: int, request: Request, session: dict = Depends(require_csrf)
):
    """Admin-initiated password reset for a user.

    If the user has an email address, sends the reset link via email.
    If the user has no email, returns the reset URL directly in the
    response so the admin can forward it out-of-band.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # Import here to avoid circular dependency at module load time.
    from backend.routes.auth import _generate_reset_token

    token = _generate_reset_token(user["id"])
    base_url = str(request.base_url).rstrip("/")
    reset_url = f"{base_url}/reset-password?token={token}"

    email_sent = False
    if user["email"]:
        email_sent = await EmailService.send_password_reset(
            email=user["email"],
            reset_url=reset_url,
            username=user["username"],
        )

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_SEND_RESET_EMAIL",
        target_type="user",
        target_id=str(user_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={
            "email_sent": email_sent,
            "has_email": bool(user["email"]),
        },
    )

    result = {
        "message": "重置邮件已发送" if email_sent else "用户没有邮箱，请手动转发重置链接",
        "email_sent": email_sent,
    }
    # If the user has no email (or email send failed), expose the URL
    # so the admin can share it out-of-band.
    if not email_sent:
        result["reset_url"] = reset_url

    return result


class AdminResetUserPasswordRequest(BaseModel):
    admin_password: str
    new_password: str


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_user_password(
    user_id: int,
    payload: AdminResetUserPasswordRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """管理员直接重置用户密码（不需要原密码）。

    安全设计：
    1. 管理员密码二次确认（防 CSRF / 误操作），走 lockout 防爆破
    2. 新密码强度校验（与注册一致：12位+3类字符，且不含用户名）
    3. 密码修改后立即吊销该用户所有 session / token / api_key
       （强制重新登录，避免旧凭证继续可用）
    4. 审计日志可追溯
    """
    admin_id = session.get("admin_id")
    client_ip = get_client_ip(request)

    # 1. 验证管理员密码（带 lockout 防爆破）
    _verify_admin_password_with_lockout(int(admin_id), payload.admin_password, client_ip)

    # 2. 查询目标用户
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = cursor.fetchone()

    if not user_row:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 3. 校验新密码强度（与注册一致）
    try:
        Security.assert_strong_password(payload.new_password, username=user_row["username"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 4. 更新密码哈希
    new_hash = Security.hash_password(payload.new_password)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_hash, user_id),
        )

    # 5. 吊销该用户所有凭证（session / token / api_key）
    #    密码已变，旧凭证必须立即失效
    from backend.routes.auth import _invalidate_all_user_credentials

    _invalidate_all_user_credentials(user_id)

    # 6. 审计日志
    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_RESET_USER_PASSWORD",
        target_type="user",
        target_id=str(user_id),
        ip_address=client_ip,
        user_agent=request.headers.get("User-Agent"),
        metadata={"target_username": user_row["username"]},
    )

    return {"message": "密码已重置，用户需要重新登录"}


@router.get("/admin/stats")
async def get_stats(request: Request, _session: dict = Depends(require_admin_session)):
    with get_db_context() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM usage_logs WHERE date(request_time) = date('now')"
        )
        today_requests = cursor.fetchone()["count"]

        cursor.execute("SELECT COALESCE(SUM(total_tokens), 0) as total FROM usage_logs")
        total_tokens = cursor.fetchone()["total"]

        cursor.execute("SELECT COALESCE(AVG(response_time_ms), 0) as avg_time FROM usage_logs")
        avg_response_time = int(cursor.fetchone()["avg_time"] or 0)

        cursor.execute("SELECT COALESCE(SUM(quota_5h), 0) as total_quota FROM users")
        total_quota = cursor.fetchone()["total_quota"] or 1

        cursor.execute(
            "SELECT COUNT(*) as used FROM usage_logs WHERE request_time > datetime('now', '-5 hours')"
        )
        used_quota = cursor.fetchone()["used"] or 0

        quota_usage = min(int((used_quota / total_quota) * 100), 100) if total_quota > 0 else 0

        return {
            "total_users": total_users,
            "today_requests": today_requests,
            "total_tokens": total_tokens,
            "avg_response_time": avg_response_time,
            "quota_usage": quota_usage,
        }


@router.get("/admin/trend")
async def get_trend(request: Request, _session: dict = Depends(require_admin_session)):
    with get_db_context() as conn:
        cursor = conn.cursor()
        days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        values = [0] * 7

        cursor.execute("""
            SELECT strftime('%w', request_time) as day, COUNT(*) as count
            FROM usage_logs
            WHERE request_time > datetime('now', '-7 days')
            GROUP BY strftime('%w', request_time)
        """)

        for row in cursor.fetchall():
            day_index = int(row["day"])
            if 0 <= day_index < 7:
                values[day_index] = row["count"]

        return {"labels": days, "values": values}


@router.get("/admin/tokens-usage")
async def get_tokens_usage(
    request: Request, limit: int = 50, _session: dict = Depends(require_admin_session)
):
    limit = max(1, min(int(limit or 50), 200))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                t.id AS token_id,
                t.token_prefix AS token_prefix,
                t.last_used_at AS last_used_at,
                u.username AS username,
                COALESCE(SUM(CASE WHEN l.request_time > datetime('now', '-24 hours') THEN l.total_tokens ELSE 0 END), 0) AS usage_24h,
                COALESCE(SUM(l.total_tokens), 0) AS usage_7d
            FROM tokens t
            JOIN users u ON u.id = t.user_id
            LEFT JOIN usage_logs l
                ON l.token_id = t.id
               AND l.request_time > datetime('now', '-7 days')
            GROUP BY t.id
            HAVING COALESCE(SUM(l.total_tokens), 0) > 0
            ORDER BY usage_24h DESC, usage_7d DESC, t.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()

    results: list[dict] = []
    for row in rows:
        results.append(
            {
                "token_id": int(row["token_id"]),
                "token_prefix": row["token_prefix"],
                "username": row["username"],
                "usage_24h": int(row["usage_24h"] or 0),
                "usage_7d": int(row["usage_7d"] or 0),
                "last_used_at": row["last_used_at"],
            }
        )
    return results


@router.get("/admin/audit-logs/export.csv")
async def export_audit_logs_csv(
    request: Request,
    action: Optional[str] = None,
    user_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    _session: dict = Depends(require_admin_session),
):
    """Export audit logs as CSV with optional filters."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"
        writer.writerow(
            ["id", "actor_id", "action", "target_type", "target_id", "details", "created_at"]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        clauses = []
        params: list = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if user_id is not None:
            clauses.append("actor_id = ?")
            params.append(int(user_id))
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("created_at <= ?")
            params.append(date_to + " 23:59:59")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, actor_id, action, target_type, target_id, metadata, created_at "
                f"FROM audit_logs {where} ORDER BY id ASC",
                params,
            )
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    writer.writerow(
                        [
                            row["id"],
                            row["actor_id"] or "",
                            row["action"],
                            row["target_type"] or "",
                            row["target_id"] or "",
                            row["metadata"] or "",
                            row["created_at"] or "",
                        ]
                    )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-logs-{today}.csv"'},
    )


@router.get("/admin/export/usage.csv")
async def export_usage_csv(
    request: Request, limit: int = 5000, _session: dict = Depends(require_admin_session)
):
    limit = max(1, min(int(limit or 5000), 20000))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                u.username, l.endpoint, l.model, l.total_tokens, l.response_time_ms, l.status_code,
                l.ip_address, l.request_time, l.token_id, l.channel_id, l.metadata
            FROM usage_logs l
            JOIN users u ON l.user_id = u.id
            ORDER BY l.request_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()

    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "username",
            "endpoint",
            "model",
            "total_tokens",
            "response_time_ms",
            "status_code",
            "ip_address",
            "request_time",
            "token_id",
            "channel_id",
            "metadata",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["username"],
                row["endpoint"],
                row["model"],
                row["total_tokens"],
                row["response_time_ms"],
                row["status_code"],
                row["ip_address"],
                row["request_time"],
                row["token_id"] if row["token_id"] is not None else "",
                row["channel_id"] if row["channel_id"] is not None else "",
                row["metadata"] or "",
            ]
        )
    content = buf.getvalue()
    return Response(content=content, media_type="text/csv")


@router.post("/admin/test-email")
async def test_email(body: dict, request: Request, session: dict = Depends(require_csrf)):
    """发送测试邮件以验证SMTP配置"""
    email = body.get("email", "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="邮箱地址不能为空")

    try:
        email_service = EmailService()
        email_service.reload_config()  # 重新加载配置
        
        # 发送测试邮件
        subject = "MiniMax 中转平台 - SMTP配置测试"
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body { font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }
                .container { max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
                .header { background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }
                .header h1 { color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }
                .content { padding: 40px; text-align: center; }
                .text { color: #64748b; font-size: 14px; line-height: 1.6; }
                .success { background: #d1fae5; color: #065f46; padding: 12px 20px; border-radius: 10px; font-weight: 600; margin: 20px 0; }
                .footer { padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }
                .footer p { color: #94a3b8; font-size: 12px; margin: 0; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>MiniMax 中转平台</h1>
                </div>
                <div class="content">
                    <p class="text">您好，</p>
                    <div class="success">SMTP配置测试成功！</div>
                    <p class="text">如果您收到此邮件，说明SMTP服务器配置正确。</p>
                </div>
                <div class="footer">
                    <p>此邮件由系统自动发送，请勿回复。</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        success, message = email_service.send_email(email, subject, html_content)
        
        if success:
            add_audit_log(
                actor_type="admin",
                actor_id=session.get("admin_id"),
                actor_username=session.get("username"),
                action="ADMIN_TEST_EMAIL",
                target_type="email",
                target_id=email,
                ip_address=get_client_ip(request),
                user_agent=request.headers.get("User-Agent"),
            )
            return {"success": True, "message": "测试邮件已发送"}
        else:
            return {"success": False, "message": message}
    except Exception as e:
        logger.error(f"发送测试邮件失败: {e}")
        return {"success": False, "message": f"发送失败: {str(e)}"}


@router.post("/admin/2fa/setup")
async def setup_2fa(request: Request, session: dict = Depends(require_csrf)):
    admin_id = int(session.get("admin_id") or 0)
    if not admin_id:
        raise HTTPException(status_code=401, detail="未授权")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT totp_enabled, username FROM admin_users WHERE id = ?",
            (admin_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="管理员不存在")
        if row["totp_enabled"]:
            raise HTTPException(status_code=400, detail="两步验证已启用")

    secret = TOTPService.generate_secret()
    username = row["username"]
    uri = TOTPService.get_provisioning_uri(secret, username)
    qr_base64 = TOTPService.generate_qr_base64(uri)

    with get_db_context() as conn:
        cursor = conn.cursor()
        # Store the secret encrypted at rest (migration 34) so a DB
        # leak alone cannot be used to forge valid 2FA codes.
        cursor.execute(
            "UPDATE admin_users SET totp_secret = ? WHERE id = ?",
            (Security.encrypt(secret), admin_id),
        )

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=username,
        action="ADMIN_2FA_SETUP",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    return {"secret": secret, "qr_uri": uri, "qr_base64": qr_base64}


@router.post("/admin/2fa/verify")
async def verify_2fa(
    req: AdminTOTPVerifyRequest, request: Request, session: dict = Depends(require_csrf)
):
    admin_id = int(session.get("admin_id") or 0)
    if not admin_id:
        raise HTTPException(status_code=401, detail="未授权")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT totp_secret, totp_enabled FROM admin_users WHERE id = ?",
            (admin_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="管理员不存在")
        if row["totp_enabled"]:
            raise HTTPException(status_code=400, detail="两步验证已启用")
        if not row["totp_secret"]:
            raise HTTPException(status_code=400, detail="请先设置两步验证")

        # Decrypt the stored secret (migration 34). ``decrypt`` returns
        # the original value for legacy plaintext rows, so this is
        # backward-compatible with secrets written before encryption.
        totp_secret_plain = Security.decrypt(row["totp_secret"]) or row["totp_secret"]
        if not TOTPService.verify(totp_secret_plain, req.code):
            raise HTTPException(status_code=400, detail="两步验证码错误")

        cursor.execute(
            "UPDATE admin_users SET totp_enabled = 1 WHERE id = ?",
            (admin_id,),
        )

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_2FA_VERIFY",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    return {"message": "两步验证已启用"}


@router.post("/admin/2fa/disable")
async def disable_2fa(
    req: AdminTOTPDisableRequest, request: Request, session: dict = Depends(require_csrf)
):
    admin_id = int(session.get("admin_id") or 0)
    if not admin_id:
        raise HTTPException(status_code=401, detail="未授权")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT password_hash, totp_enabled FROM admin_users WHERE id = ?",
            (admin_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="管理员不存在")
        if not row["totp_enabled"]:
            raise HTTPException(status_code=400, detail="两步验证未启用")

        if not Security.verify_password(req.password, row["password_hash"] or ""):
            raise HTTPException(status_code=400, detail="密码错误")

        cursor.execute(
            "UPDATE admin_users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?",
            (admin_id,),
        )

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_2FA_DISABLE",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    return {"message": "两步验证已禁用"}


# ---------------------------------------------------------------------------
# Chat endpoints have moved to ``backend/routes/chat.py``.
#
# Why this block was deleted
# --------------------------
# The four handlers below used to live here and were the only way the
# SPA could call ``/api/chat/send``, ``/api/chat/conversations`` and
# friends. The catch: they hard-required an admin session, so the
# moment a regular user opened ``/chat`` every list / send call 401'd
# and the chat sidebar went blank. The new ``routes.chat`` module
# accepts *either* session cookie and resolves the underlying
# ``user_id``, so the same endpoint works for both audiences.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class KillSwitchRequest(BaseModel):
    admin_password: str


@router.post("/admin/killswitch")
async def activate_kill_switch(
    payload: KillSwitchRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    admin_id = session.get("admin_id")
    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get brute-force protection.
    _verify_admin_password_with_lockout(
        int(admin_id), payload.admin_password, get_client_ip(request)
    )

    set_setting("global_freeze", "true")

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_KILLSWITCH_ACTIVATE",
        target_type="settings",
        target_id="global_freeze",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "全局冻结已启用"}


@router.post("/admin/killswitch/release")
async def release_kill_switch(
    payload: KillSwitchRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    admin_id = session.get("admin_id")
    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get brute-force protection.
    _verify_admin_password_with_lockout(
        int(admin_id), payload.admin_password, get_client_ip(request)
    )

    set_setting("global_freeze", "false")

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_KILLSWITCH_RELEASE",
        target_type="settings",
        target_id="global_freeze",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "全局冻结已解除"}


@router.get("/admin/killswitch/status")
async def killswitch_status(_session: dict = Depends(require_admin_session)):
    frozen = get_setting("global_freeze") == "true"
    return {"global_freeze": frozen}


# ---------------------------------------------------------------------------
# Session audit
# ---------------------------------------------------------------------------


@router.get("/admin/sessions")
async def list_admin_sessions(
    request: Request,
    _session: dict = Depends(require_admin_session),
):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, role, admin_id, user_id, username,
                   user_agent, ip_address, created_at, expires_at
            FROM sessions
            WHERE expires_at > datetime('now')
            ORDER BY created_at DESC
            """
        )
        rows = cursor.fetchall()

    results = []
    for row in rows:
        sid = row["session_id"] or ""
        masked = f"{sid[:8]}...{sid[-4:]}" if len(sid) > 12 else "****"
        # Desensitise ip_address: show first 3 octets + .xxx for IPv4.
        # For non-IPv4 (IPv6 / unknown), show only a coarse prefix.
        raw_ip = row["ip_address"] or ""
        if raw_ip and raw_ip.count(".") == 3:
            parts = raw_ip.split(".")
            masked_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.xxx"
        elif raw_ip and ":" in raw_ip:
            # IPv6 — show only the first group.
            masked_ip = raw_ip.split(":")[0] + ":xxx"
        else:
            masked_ip = ""
        # Truncate user_agent to 100 chars to limit PII / version leak.
        raw_ua = row["user_agent"] or ""
        masked_ua = raw_ua[:100] if len(raw_ua) > 100 else raw_ua
        results.append({
            "session_id": masked,
            "session_id_full_hash": hashlib.sha256(sid.encode()).hexdigest()[:16],
            "role": row["role"],
            "admin_id": row["admin_id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "user_agent": masked_ua,
            "ip_address": masked_ip,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        })
    return results


@router.delete("/admin/sessions/{session_hash}")
async def revoke_session(
    session_hash: str,
    request: Request,
    session: dict = Depends(require_csrf),
):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT session_id FROM sessions WHERE expires_at > datetime('now')"
        )
        rows = cursor.fetchall()

    target_sid = None
    for row in rows:
        sid = row["session_id"] or ""
        h = hashlib.sha256(sid.encode()).hexdigest()[:16]
        if h == session_hash:
            target_sid = sid
            break

    if not target_sid:
        raise HTTPException(status_code=404, detail="会话不存在")

    current_sid = request.cookies.get(ADMIN_SESSION_COOKIE) or request.cookies.get(USER_SESSION_COOKIE)
    if target_sid == current_sid:
        raise HTTPException(status_code=400, detail="不能吊销当前会话")

    delete_session(target_sid)

    add_audit_log(
        actor_type="admin",
        actor_id=session.get("admin_id"),
        actor_username=session.get("username"),
        action="ADMIN_REVOKE_SESSION",
        target_type="session",
        target_id=session_hash,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    return {"message": "会话已吊销"}


@router.delete("/admin/sessions")
async def revoke_all_sessions(
    payload: KillSwitchRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    admin_id = session.get("admin_id")
    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get brute-force protection.
    _verify_admin_password_with_lockout(
        int(admin_id), payload.admin_password, get_client_ip(request)
    )

    current_sid = request.cookies.get(ADMIN_SESSION_COOKIE) or ""

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM sessions WHERE session_id != ? AND expires_at > datetime('now')",
            (current_sid,),
        )
        count = cursor.rowcount

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_REVOKE_ALL_SESSIONS",
        target_type="session",
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"count": count},
    )
    return {"message": f"已吊销 {count} 个会话", "revoked": count}


# ---------------------------------------------------------------------------
# Admin user management (super-admin)
# ---------------------------------------------------------------------------


@router.get("/admin/admins")
async def list_admins(
    request: Request,
    session: dict = Depends(require_admin_session),
):
    """List all admin accounts with their super-admin flag.

    Sensitive columns (password_hash, totp_secret) are never returned.
    """
    with get_db_context() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, username, is_active, is_super_admin,
                   totp_enabled, last_login, created_at
            FROM admin_users
            ORDER BY id ASC
            """
        )
        rows = cursor.fetchall()

    return [
        {
            "id": int(row["id"]),
            "username": row["username"],
            "is_active": bool(row["is_active"]),
            "is_super_admin": bool(row["is_super_admin"] or 0),
            "totp_enabled": bool(row["totp_enabled"] or 0),
            "last_login": row["last_login"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _require_super_admin(session: dict) -> None:
    """Raise 403 if the caller is not a super-admin."""
    admin_id = int(session.get("admin_id") or 0)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_super_admin FROM admin_users WHERE id = ?",
            (admin_id,),
        )
        row = cursor.fetchone()
    if not row or not int(row["is_super_admin"] or 0):
        raise HTTPException(status_code=403, detail="仅超级管理员可执行此操作")


@router.post("/admin/admins/{admin_id}/promote-super")
async def promote_super_admin(
    admin_id: int,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """Promote an admin to super-admin (super-admin only)."""
    _require_super_admin(session)
    caller_id = int(session.get("admin_id") or 0)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM admin_users WHERE id = ?", (int(admin_id),))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="管理员不存在")
        cursor.execute(
            "UPDATE admin_users SET is_super_admin = 1 WHERE id = ?",
            (int(admin_id),),
        )

    add_audit_log(
        actor_type="admin",
        actor_id=caller_id,
        actor_username=session.get("username"),
        action="ADMIN_PROMOTE_SUPER",
        target_type="admin",
        target_id=str(admin_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"target_username": row["username"] if row else None},
    )
    return {"message": "已提升为超级管理员"}


@router.post("/admin/admins/{admin_id}/demote-super")
async def demote_super_admin(
    admin_id: int,
    request: Request,
    session: dict = Depends(require_csrf),
):
    """Demote a super-admin to regular admin (super-admin only, cannot demote self)."""
    _require_super_admin(session)
    caller_id = int(session.get("admin_id") or 0)
    if int(admin_id) == caller_id:
        raise HTTPException(status_code=400, detail="不能降级自己的超级管理员权限")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM admin_users WHERE id = ?", (int(admin_id),))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="管理员不存在")
        cursor.execute(
            "UPDATE admin_users SET is_super_admin = 0 WHERE id = ?",
            (int(admin_id),),
        )

    add_audit_log(
        actor_type="admin",
        actor_id=caller_id,
        actor_username=session.get("username"),
        action="ADMIN_DEMOTE_SUPER",
        target_type="admin",
        target_id=str(admin_id),
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"target_username": row["username"] if row else None},
    )
    return {"message": "已撤销超级管理员权限"}


# ---------------------------------------------------------------------------
# Bulk refund
# ---------------------------------------------------------------------------


class BulkRefundRequest(BaseModel):
    admin_password: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    status: Optional[str] = "paid"
    payment_provider: Optional[str] = None


@router.post("/admin/orders/bulk-refund")
async def bulk_refund(
    payload: BulkRefundRequest,
    request: Request,
    session: dict = Depends(require_csrf),
):
    admin_id = session.get("admin_id")
    client_ip = get_client_ip(request)
    # P1.7: route password verification through the lockout helper so
    # repeated wrong-password attempts get brute-force protection.
    _verify_admin_password_with_lockout(
        int(admin_id), payload.admin_password, client_ip
    )

    allowed, _ = check_rate_limit(f"bulk_refund:{admin_id}", "bulk_refund:3600", 1, 3600)
    if not allowed:
        raise HTTPException(status_code=429, detail="批量退款操作过于频繁，请稍后再试")

    clauses = ["1=1"]
    params: list = []
    if payload.date_from:
        clauses.append("created_at >= ?")
        params.append(payload.date_from)
    if payload.date_to:
        clauses.append("created_at <= ?")
        params.append(payload.date_to + " 23:59:59")
    if payload.status:
        clauses.append("status = ?")
        params.append(payload.status)
    if payload.payment_provider:
        clauses.append("payment_provider = ?")
        params.append(payload.payment_provider)

    where = " AND ".join(clauses)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, order_no, user_id, amount, status FROM orders WHERE {where}",
            tuple(params),
        )
        orders = [dict(r) for r in cursor.fetchall()]

    from backend.services import order_service

    refunded = 0
    failed = 0
    for order in orders:
        if order.get("status") != "paid":
            failed += 1
            continue
        try:
            ok = order_service.refund_order(int(order["id"]), admin_id, reason="bulk_refund")
            if ok:
                refunded += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    add_audit_log(
        actor_type="admin",
        actor_id=admin_id,
        actor_username=session.get("username"),
        action="ADMIN_BULK_REFUND",
        target_type="order",
        ip_address=client_ip,
        user_agent=request.headers.get("User-Agent"),
        metadata={
            "refunded": refunded,
            "failed": failed,
            "filters": {
                "date_from": payload.date_from,
                "date_to": payload.date_to,
                "status": payload.status,
                "payment_provider": payload.payment_provider,
            },
        },
    )
    return {
        "message": f"批量退款完成：成功 {refunded}，失败 {failed}",
        "refunded": refunded,
        "failed": failed,
        "total_matched": len(orders),
    }
