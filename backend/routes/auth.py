import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, field_validator, model_validator

from backend.config import Config
from backend.database import check_rate_limit, get_client_ip, get_db_context, get_setting, sanitize_input
from backend.security import Security
from backend.services.email_service import EmailService
from backend.services.lockout import check_allowed, record_failure, record_success
from backend.services.redis_service import RedisService
from backend.services.user_service import RESERVED_USERNAMES, UserService
from backend.services.captcha_service import (
    generate_challenge as _generate_captcha,
    should_require_captcha as _should_require_captcha,
    verify as _verify_captcha,
)
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

router = APIRouter()
logger = logging.getLogger(__name__)


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str):
        if len(v) < 3 or len(v) > 30:
            raise ValueError("用户名长度必须在3-30个字符之间")
        # R4: block reserved usernames (case-insensitive) so attackers
        # cannot register "admin" / "support" / "root" etc. to
        # social-engineer other users.
        if v.lower() in RESERVED_USERNAMES:
            raise ValueError("该用户名为系统保留字，请使用其他用户名")
        # P2.6: extend the CJK allow-list beyond the basic Unified
        # Ideographs block. The previous regex (\u4e00-\u9fff) rejected
        # Extension A (\u3400-\u4dbf — rare / archaic chars used in
        # names) and the Compatibility Ideographs block
        # (\uf900-\ufaff — used by some input methods / historical
        # corpora). Including them lets users whose legal names use
        # those characters register with their actual name.
        if not re.match(r"^[a-zA-Z0-9_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+$", v):
            raise ValueError("用户名只能包含字母、数字、下划线和中文")
        return sanitize_input(v)

    @model_validator(mode="after")
    def validate_password_strength(self):
        try:
            Security.assert_strong_password(self.password, username=self.username)
        except ValueError as e:
            raise ValueError(str(e))
        return self


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str


class LoginRequest(BaseModel):
    username: str
    password: str
    # When True the session is extended to REMEMBER_ME_TTL_SECONDS
    # (30 days). See admin.login for the rationale.
    remember: bool = False
    # L17: CAPTCHA fields — required only when the backend returns
    # 423 CAPTCHA_REQUIRED (after >= 3 failed attempts from the IP).
    captcha_id: Optional[str] = None
    captcha_answer: Optional[int] = None

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, v: str):
        # P2.5: cap the password length on the login path so an
        # attacker can't DoS the (deliberately expensive) PBKDF2
        # verify by submitting a multi-megabyte string. 1024 chars is
        # well above any reasonable password manager output and below
        # the assert_strong_password cap of 128 enforced elsewhere —
        # we use a looser cap here because login must accept legacy
        # passwords that predate the strong-password policy.
        if not isinstance(v, str) or len(v) > 1024:
            raise ValueError("密码长度不能超过 1024 个字符")
        return v


class ApiKeyLoginRequest(BaseModel):
    api_key: str
    remember: bool = False


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @model_validator(mode="after")
    def validate_password_strength(self):
        try:
            Security.assert_strong_password(self.new_password)
        except ValueError as e:
            raise ValueError(str(e))
        return self


def _invalidate_all_user_credentials(user_id: int) -> None:
    """Revoke every credential belonging to *user_id*: sessions, tokens
    and api_keys.

    Used after a password reset / change so that any stolen session
    cookie OR a previously-issued API key / token immediately stops
    working. Falls back to deleting just the sessions if the tokens /
    api_keys tables are not present (defensive against schema drift on
    legacy installs).
    """
    from backend.database import release_reservation

    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            cursor.execute(
                "UPDATE tokens SET is_active=0, revoked_at=CURRENT_TIMESTAMP "
                "WHERE user_id=? AND is_active=1",
                (user_id,),
            )
            cursor.execute(
                "UPDATE api_keys SET is_active=0 "
                "WHERE user_id=? AND is_active=1",
                (user_id,),
            )
        try:
            release_reservation(user_id)
        except Exception:
            pass
    except Exception:
        # Degraded path: at minimum revoke sessions so a stolen cookie
        # cannot keep being used after a password reset.
        with get_db_context() as conn:
            conn.cursor().execute(
                "DELETE FROM sessions WHERE user_id = ?", (user_id,)
            )


@router.post("/auth/register")
async def register(req: RegisterRequest, request: Request):
    if get_setting("allow_registration") == "false":
        raise HTTPException(status_code=403, detail="注册已关闭")

    client_ip = get_client_ip(request)

    allowed, remaining = check_rate_limit(client_ip, "register_ip:60", 60, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    username = req.username
    email = req.email.lower()
    password = req.password

    # Atomic uniqueness check. The pre-check + INSERT path used here
    # has a TOCTOU race (two concurrent registrations can both pass
    # the SELECT and then one of them trips the unique index, which
    # surfaces as an opaque 500 to the user). Catching the
    # ``sqlite3.IntegrityError`` from the canonical constraint
    # enforces uniqueness *atomically* with the insert. We still
    # keep the pre-check so the common case (existing user) returns
    # the friendly 400 without needing the error path.
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="用户名或邮箱已存在")

    # 检查是否开启邮箱验证
    email_verification_enabled = get_setting("email_verification_enabled") or "false"
    
    if email_verification_enabled.lower() == "true":
        # 邮箱验证流程：发送验证码，暂存用户数据
        code = EmailService.generate_verification_code()
        # P1.3: ``set_verification_code`` returns ``False`` when Redis
        # is unavailable / misconfigured. Previously the return value
        # was ignored, so a Redis outage silently dropped the code and
        # the user was told "验证码已发送" — then every subsequent
        # /auth/verify attempt would fail with "验证码错误或已过期"
        # while the operator scratched their head. Surface the failure
        # explicitly so the operator gets a 503 instead.
        if not RedisService.set_verification_code(email, code):
            raise HTTPException(status_code=503, detail="验证服务暂不可用，请稍后再试")

        email_service = EmailService()
        success, message = email_service.send_verification_email(email, code)
        if not success:
            raise HTTPException(status_code=500, detail="发送验证邮件失败")

        user_data = {
            "username": username,
            "email": email,
            "password_hash": Security.hash_password(password),
        }
        temp_key = f"pending:{email}"
        RedisService.set_with_expiry(temp_key, json.dumps(user_data), 3600)

        return {"message": "验证码已发送到邮箱", "email": email, "need_verification": True}
    else:
        # 直接创建用户到数据库，不需要邮箱验证
        api_key = Security.generate_api_key()
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        api_key_placeholder = secrets.token_hex(32)
        password_hash = Security.hash_password(password)

        try:
            # Single transaction: user INSERT + wallet + subscription +
            # monthly_credits grant. If auto_activate_free_plan fails
            # (DB lock, disk full, etc.) the whole transaction rolls
            # back so we never leave a user row without a wallet /
            # subscription — which would otherwise leave the account
            # unable to call any priced model and unable to re-register
            # (username taken).
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    """
                    INSERT INTO users (username, email, password_hash, api_key, api_key_hash, quota_5h, quota_week, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        username,
                        email,
                        password_hash,
                        api_key_placeholder,
                        api_key_hash,
                        Config.DEFAULT_QUOTA_5H,
                        Config.DEFAULT_QUOTA_WEEK,
                    ),
                )
                user_id = int(cursor.lastrowid)
                UserService.auto_activate_free_plan(user_id, conn=conn)
        except sqlite3.IntegrityError:
            # The unique index on ``users.username`` / ``users.email``
            # catches the rare race where two registrations complete
            # concurrently for the same address. Surface it as the
            # same friendly 400 instead of letting an opaque 500 escape.
            raise HTTPException(status_code=400, detail="用户名或邮箱已存在")

        return {"message": "注册成功，请登录", "email": email, "need_verification": False}


@router.post("/auth/verify")
async def verify_email(req: VerifyRequest, request: Request):
    client_ip = get_client_ip(request)
    email = req.email.lower()

    allowed, remaining = check_rate_limit(client_ip, "verify_ip:60", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # P1.3: brute-force protection moved from a Redis counter to the
    # SQLite-backed ``auth_failures`` table (re-using ``lockout.py``).
    # The previous implementation silently no-op'd when Redis was
    # unavailable — every call to ``RedisService.get_client`` returned
    # ``None`` and the ``pipe.execute()`` branch was skipped, so the
    # ``attempts`` counter never advanced. An attacker could brute
    # the 6-digit verification code unimpeded. The lockout table is
    # created on demand by ``lockout._ensure_table`` and is therefore
    # always available as long as SQLite is up (which is the same
    # dependency the rest of the auth flow already has).
    verify_lock = check_allowed(
        email, scope="verify_email", max_failures=10, window_seconds=1800
    )
    if not verify_lock.allowed:
        raise HTTPException(status_code=429, detail="验证尝试次数过多，请稍后再试")

    stored_code = RedisService.get_verification_code(email)
    if not stored_code or not secrets.compare_digest(stored_code, req.code):
        record_failure(
            email, scope="verify_email", max_failures=10, window_seconds=1800
        )
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    temp_key = f"pending:{email}"
    temp_data = RedisService.get(temp_key)
    if not temp_data:
        # The pending data is gone — but the verification code itself
        # was correct, so reset the failure counter to avoid the
        # legitimate user sliding toward the lockout threshold.
        record_success(email, scope="verify_email")
        raise HTTPException(status_code=400, detail="注册信息已过期，请重新注册")

    user_data = json.loads(temp_data)

    try:
        # Single transaction: user INSERT + wallet + subscription +
        # monthly_credits grant. Same atomicity contract as the
        # register path — see the comment there for the rationale.
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            api_key = Security.generate_api_key()
            api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
            api_key_placeholder = secrets.token_hex(32)
            cursor.execute(
                """
                INSERT INTO users (username, email, password_hash, api_key, api_key_hash, quota_5h, quota_week, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
                (
                    user_data["username"],
                    user_data["email"],
                    user_data["password_hash"],
                    api_key_placeholder,
                    api_key_hash,
                    Config.DEFAULT_QUOTA_5H,
                    Config.DEFAULT_QUOTA_WEEK,
                ),
            )
            user_id = int(cursor.lastrowid)
            UserService.auto_activate_free_plan(user_id, conn=conn)
    except sqlite3.IntegrityError:
        # The unique index on ``users.username`` / ``users.email``
        # catches the rare race where two verify flows complete
        # concurrently for the same address. Surface it as the
        # same friendly 400 the register endpoint does, instead of
        # letting an opaque 500 escape to the SPA.
        raise HTTPException(status_code=400, detail="用户名或邮箱已存在")

    RedisService.delete_verification_code(email)
    RedisService.delete(temp_key)
    # P1.3: clear the verify_email failure counter on success so a
    # future legitimate verify flow starts with a clean slate. (The
    # Redis ``attempts_key`` no longer exists; this is now backed by
    # the ``auth_failures`` table.)
    record_success(email, scope="verify_email")

    email_service = EmailService()
    email_service.send_welcome_email(user_data["email"], user_data["username"])

    return {"message": "邮箱验证成功，请登录"}


class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post("/auth/resend-verification")
async def resend_verification(req: ResendVerificationRequest, request: Request):
    client_ip = get_client_ip(request)
    email = req.email.lower()

    allowed, remaining = check_rate_limit(client_ip, "resend_verify_ip:60", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    resend_lock_key = f"resend_lock:{email}"
    if RedisService.get(resend_lock_key):
        raise HTTPException(status_code=429, detail="请60秒后再试")

    hourly_key = f"resend_hourly:{email}"
    raw_hourly = RedisService.get(hourly_key)
    hourly_count = int(raw_hourly) if raw_hourly else 0
    if hourly_count >= 3:
        raise HTTPException(status_code=429, detail="发送次数已达上限，请1小时后再试")

    temp_key = f"pending:{email}"
    temp_data = RedisService.get(temp_key)
    if not temp_data:
        return {"message": "如果存在待验证的注册信息，验证邮件已重新发送"}

    user_data = json.loads(temp_data)

    code = EmailService.generate_verification_code()
    code_stored = RedisService.set_verification_code(email, code)
    if not code_stored:
        raise HTTPException(status_code=500, detail="验证服务暂不可用，请稍后再试")

    email_service = EmailService()
    if not email_service.smtp_host or not email_service.smtp_user:
        RedisService.delete_verification_code(email)
        return {"message": "如果存在待验证的注册信息，验证邮件已重新发送"}

    success, message = email_service.send_verification_email(email, code)
    if not success:
        RedisService.delete_verification_code(email)
        raise HTTPException(status_code=500, detail="发送验证邮件失败")

    RedisService.set_with_expiry(resend_lock_key, "1", 60)
    try:
        redis_client = RedisService.get_client()
        if redis_client:
            pipe = redis_client.pipeline()
            pipe.incr(hourly_key)
            pipe.expire(hourly_key, 3600)
            pipe.execute()
    except Exception:
        pass

    return {"message": "如果存在待验证的注册信息，验证邮件已重新发送"}


@router.get("/auth/captcha")
async def get_captcha():
    """L17: Return a fresh math CAPTCHA challenge.

    Called by the frontend when the login endpoint returns 423
    CAPTCHA_REQUIRED. The challenge is one-shot and expires after
    5 minutes.
    """
    captcha_id, question = _generate_captcha()
    return {"captcha_id": captcha_id, "question": question}


@router.post("/auth/login")
async def login(req: LoginRequest, request: Request):
    client_ip = get_client_ip(request)

    allowed, remaining = check_rate_limit(client_ip, "login_ip:1800", 10, 1800)
    if not allowed:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请30分钟后再试")

    username = sanitize_input(req.username)
    # P3.8: if the supplied identifier looks like an email, lowercase
    # it before lookup. The register / verify / forgot-password paths
    # all store emails lowercased, but the login path was matching
    # case-sensitively — so a user who registered as "alice@x.com" and
    # typed "Alice@X.com" at login would get a spurious 401. Only
    # lowercase when the input contains "@" so plain usernames (which
    # are case-sensitive by design) keep their case.
    if "@" in username:
        username = username.lower()

    # Brute-force lockout (per-username AND per-IP, additive layer on
    # top of the rate limiter above).
    lock_user = check_allowed(username, scope="user")
    if not lock_user.allowed:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    lock_ip = check_allowed(client_ip, scope="ip")
    if not lock_ip.allowed:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # L17: CAPTCHA challenge — when the IP has accumulated >= 3 failed
    # attempts (but is not yet hard-locked at 8), require a CAPTCHA
    # answer. Returns 423 with a fresh challenge so the frontend can
    # render it without a separate round-trip.
    #
    # Multi-dim review fix: a failed CAPTCHA answer also advances the
    # lockout counters. Without this an attacker could retry the
    # CAPTCHA indefinitely (answer space is only ~26 values) without
    # ever reaching the 8-failure hard lock. Recording the failure
    # ensures the CAPTCHA brute-force path converges to lockout at
    # the same rate as the password brute-force path.
    if _should_require_captcha(max(lock_ip.failure_count, lock_user.failure_count)):
        if not _verify_captcha(req.captcha_id, req.captcha_answer):
            record_failure(username, scope="user")
            record_failure(client_ip, scope="ip")
            captcha_id, question = _generate_captcha()
            raise HTTPException(
                status_code=423,
                detail={
                    "code": "CAPTCHA_REQUIRED",
                    "captcha_id": captcha_id,
                    "question": question,
                },
            )

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ? OR email = ?", (username, username))
        user = cursor.fetchone()

        if not user:
            Security.verify_password(
                "dummy",
                "600000$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            )
            record_failure(username, scope="user")
            record_failure(client_ip, scope="ip")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        # P1.5: check is_active BEFORE password verification so a
        # frozen account returns the same 401 as a wrong password.
        # Returning 403 here would leak the fact that the password
        # was correct, enabling account-enumeration via the response
        # code. We still bump the failure counter so the lockout
        # policy kicks in on repeated probes.
        if not user["is_active"]:
            record_failure(username, scope="user")
            record_failure(client_ip, scope="ip")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        if not Security.verify_password(req.password, user["password_hash"]):
            record_failure(username, scope="user")
            record_failure(client_ip, scope="ip")
            raise HTTPException(status_code=401, detail="用户名或密码错误")

        # P2.9: transparently upgrade legacy 100k-iteration hashes to
        # the current 600k format on successful login — the only time
        # we have the plaintext password in hand. Best-effort: a
        # failure here (DB lock, disk full) must not block the login,
        # the user can re-authenticate and we'll try again.
        if Security.is_legacy_password_hash(user["password_hash"] or ""):
            try:
                new_hash = Security.hash_password(req.password)
                cursor.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (new_hash, int(user["id"])),
                )
            except Exception:
                logger.exception(
                    "legacy password hash upgrade failed for user_id=%s",
                    user["id"],
                )

    # R1 mitigation: cross-check that the user's username does not also
    # exist in ``admin_users``. If an operator accidentally creates a
    # regular user account with the same username + password as an
    # admin account, an attacker can log in via /auth/login (which has
    # no TOTP challenge) instead of /admin/login — bypassing the
    # admin 2FA. This check is best-effort: it never blocks the login,
    # only logs a warning + audit row so the operator can investigate
    # and remove the password-reuse misconfiguration.
    try:
        with get_db_context() as conn:
            admin_row = conn.cursor().execute(
                "SELECT id FROM admin_users WHERE username = ?",
                (user["username"],),
            ).fetchone()
            if admin_row:
                logger.warning(
                    "User %r logged in via /auth/login but username exists in admin_users — "
                    "possible password reuse. Admin should use /admin/login with TOTP.",
                    user["username"],
                )
                from backend.services.audit import log_action

                log_action(
                    actor_id=int(user["id"]),
                    actor_type="user",
                    action="user.login.admin_name_collision",
                    target_type="admin_users",
                    target_id=admin_row[0],
                    details={
                        "username": user["username"],
                        "warning": "password_reuse_detected",
                    },
                    ip_address=client_ip,
                )
    except Exception:
        pass  # Don't block login on audit failure

    # Successful login — reset any accumulated failure counters.
    record_success(username, scope="user")
    record_success(client_ip, scope="ip")

    ttl = REMEMBER_ME_TTL_SECONDS if getattr(req, "remember", False) else SESSION_TTL_SECONDS
    session_id, csrf_token = create_session(
        {
            "role": "user",
            "user_id": int(user["id"]),
            "username": user["username"],
            "email": user["email"],
        },
        ttl_seconds=ttl,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip,
    )

    resp = JSONResponse(
        {
            "message": "ok",
            "role": "user",
            "user": {"id": user["id"], "username": user["username"], "email": user["email"]},
            "ttl": int(ttl),
        }
    )
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        delete_session(admin_session_id)
        resp.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    resp.set_cookie(USER_SESSION_COOKIE, session_id, **session_cookie_kwargs(ttl_seconds=ttl))
    resp.set_cookie(CSRF_COOKIE, csrf_token, **csrf_cookie_kwargs(ttl_seconds=ttl))
    return resp


@router.post("/auth/login-api-key")
async def login_api_key(req: ApiKeyLoginRequest, request: Request):
    client_ip = get_client_ip(request)

    if not Config.ALLOW_API_KEY_LOGIN:
        raise HTTPException(status_code=403, detail="API Key 登录已关闭")

    allowed, _remaining = check_rate_limit(client_ip, "login_key_ip:1800", 20, 1800)
    if not allowed:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")

    api_key = sanitize_input(req.api_key)
    api_key_prefix = api_key[:8] if len(api_key) >= 8 else api_key

    # Brute-force lockout for API key login (per-key AND per-IP).
    lock_key = check_allowed(api_key_prefix, scope="key")
    if not lock_key.allowed:
        raise HTTPException(status_code=429, detail="API Key 锁定，请稍后再试")
    lock_ip = check_allowed(client_ip, scope="ip")
    if not lock_ip.allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    user_ctx = UserService.get_user_by_api_key(api_key)
    if not user_ctx:
        record_failure(api_key_prefix, scope="key")
        record_failure(client_ip, scope="ip")
        raise HTTPException(status_code=401, detail="无效的API Key")
    # P1.5: frozen accounts get the same 401 as an unknown key so the
    # response code does not leak the fact that the API key was valid.
    # ``get_user_by_api_key`` already filters ``is_active = 1`` so this
    # branch is defence-in-depth for any future code path that returns
    # an inactive user.
    if not user_ctx.is_active:
        record_failure(api_key_prefix, scope="key")
        record_failure(client_ip, scope="ip")
        raise HTTPException(status_code=401, detail="无效的API Key")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email FROM users WHERE id = ?", (int(user_ctx.id),))
        row = cursor.fetchone()
        if not row:
            record_failure(api_key_prefix, scope="key")
            record_failure(client_ip, scope="ip")
            raise HTTPException(status_code=401, detail="无效的API Key")

    # Successful login — reset any accumulated failure counters.
    record_success(api_key_prefix, scope="key")
    record_success(client_ip, scope="ip")

    ttl = REMEMBER_ME_TTL_SECONDS if getattr(req, "remember", False) else SESSION_TTL_SECONDS
    session_id, csrf_token = create_session(
        {
            "role": "user",
            "user_id": int(row["id"]),
            "username": row["username"],
            "email": row["email"],
        },
        ttl_seconds=ttl,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip,
    )

    resp = JSONResponse(
        {
            "message": "ok",
            "role": "user",
            "user": {"id": row["id"], "username": row["username"], "email": row["email"]},
            "ttl": int(ttl),
        }
    )
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        delete_session(admin_session_id)
        resp.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    resp.set_cookie(USER_SESSION_COOKIE, session_id, **session_cookie_kwargs(ttl_seconds=ttl))
    resp.set_cookie(CSRF_COOKIE, csrf_token, **csrf_cookie_kwargs(ttl_seconds=ttl))
    return resp


def _generate_reset_token(user_id: int) -> str:
    """Create a password_reset_tokens row and return the plain token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO password_reset_tokens (token, user_id, expires_at)
            VALUES (?, ?, ?)
            """,
            (token, user_id, expires_at.isoformat()),
        )
    return token


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    client_ip = get_client_ip(request)
    email = req.email.lower()

    allowed, remaining = check_rate_limit(client_ip, "forgot_ip:60", 5, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # P2.2: per-email rate limit (3 / hour). The IP limiter above
    # catches a single host bombarding the endpoint; without an
    # email-keyed limiter a distributed attacker can still drive a
    # targeted user's inbox full of reset mails (or use the email
    # volume itself as a harassment vector). 3/hour is plenty for a
    # legitimate user (who rarely needs >1) while capping the abuse
    # surface. The check runs *before* the user lookup so the response
    # time + 429 status don't leak whether the email exists.
    allowed_email, _ = check_rate_limit(email, "forgot_email:3600", 3, 3600)
    if not allowed_email:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, email, is_active FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

    if user and not user["is_active"]:
        user = None

    if user:
        token = _generate_reset_token(user["id"])
        base_url = str(request.base_url).rstrip("/")
        reset_url = f"{base_url}/reset-password?token={token}"
        await EmailService.send_password_reset(
            email=email, reset_url=reset_url, username=user["username"]
        )

    # Always return the same message — never leak whether the email exists.
    return {"message": "如果邮箱已注册，重置链接已发送"}


@router.get("/auth/reset-password/validate")
async def validate_reset_token(token: str = Query(...), request: Request = None):
    """Check whether a reset token is valid without consuming it.

    Used by the frontend to show a "token expired" page instead of the
    password form.
    """
    if request:
        client_ip = get_client_ip(request)
        allowed, _remaining = check_rate_limit(client_ip, "validate_token:60", 10, 60)
        if not allowed:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT expires_at FROM password_reset_tokens
            WHERE token = ? AND used_at IS NULL
            """,
            (token,),
        )
        row = cursor.fetchone()

    if not row:
        return {"valid": False, "expires_at": None}

    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except Exception:
        return {"valid": False, "expires_at": None}

    now = datetime.now(timezone.utc)
    if expires_at <= now:
        return {"valid": False, "expires_at": row["expires_at"]}

    return {"valid": True, "expires_at": row["expires_at"]}


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    # NOTE: No CSRF check here — this is a public, token-authenticated
    # endpoint.  The user arrives via a reset link in their email and may
    # not have a session or CSRF cookie.  The one-time token itself is
    # the authorization mechanism; rate limiting + token validation
    # provide sufficient protection.
    client_ip = get_client_ip(request)

    allowed, remaining = check_rate_limit(client_ip, "reset_ip:60", 5, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, expires_at FROM password_reset_tokens
            WHERE token = ? AND used_at IS NULL
            """,
            (req.token,),
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=400, detail="无效的重置链接")

        # Check expiry
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="重置链接已过期，请重新申请")

        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="重置链接已过期，请重新申请")

        user_id = row["user_id"]

        cursor.execute("SELECT is_active FROM users WHERE id = ?", (user_id,))
        active_row = cursor.fetchone()
        if not active_row or not active_row["is_active"]:
            raise HTTPException(status_code=400, detail="账号已被禁用")

        # Defense-in-depth: re-validate password with username so that
        # passwords containing the username are rejected (the model
        # validator cannot do this because the username is unknown at
        # validation time).
        cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        user_row = cursor.fetchone()
        if user_row:
            try:
                Security.assert_strong_password(req.new_password, username=user_row["username"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        # Hash + update password
        new_password_hash = Security.hash_password(req.new_password)
        cursor.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_password_hash, user_id),
        )

        # Mark token as used
        cursor.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?",
            (datetime.now(timezone.utc).isoformat(), req.token),
        )

    # Invalidate ALL existing credentials for that user (security: if
    # someone reset the password, all sessions / tokens / api_keys are
    # suspect).
    _invalidate_all_user_credentials(user_id)

    return {"message": "密码重置成功，请使用新密码登录"}


@router.get("/auth/me")
async def get_current_user(request: Request):
    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="未授权")
    session = get_session(session_id, user_agent=request.headers.get("User-Agent"))
    if not session or session.get("role") != "user":
        raise HTTPException(status_code=401, detail="未授权")
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "email": session.get("email"),
    }


async def _require_logout_csrf(request: Request) -> None:
    """CSRF gate for ``POST /auth/logout``.

    The logout endpoint serves both admin (``mm_admin_session``) and
    user (``mm_session``) sessions, so the shared
    :func:`backend.routes.billing.require_user_csrf` dependency — which
    only validates against the user session — is not a drop-in fit.
    This local dependency mirrors its triple-compare logic
    (``X-CSRF-Token`` header ↔ ``mm_csrf`` cookie ↔ server-side session
    csrf) but accepts a match against EITHER session.

    A malicious site can make the browser send cookies cross-origin but
    cannot read the ``mm_csrf`` value (SameSite=Lax + the header is the
    only way to echo it back), so requiring the header to match the
    cookie defeats CSRF-to-logout nuisance attacks.
    """
    header_token = (request.headers.get("X-CSRF-Token") or "").strip()
    cookie_token = (request.cookies.get(CSRF_COOKIE) or "").strip()
    if not header_token or not cookie_token:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    if not hmac.compare_digest(header_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")

    # Belt-and-suspenders: also compare against the server-side session
    # csrf value, so a stolen mm_csrf cookie alone (without a valid
    # session) cannot pass the check. Accept a match against either the
    # admin or the user session.
    ua = request.headers.get("User-Agent")
    session_csrf = ""
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        admin_sess = get_session(admin_session_id, user_agent=ua)
        session_csrf = (admin_sess or {}).get("csrf") or ""
    if not session_csrf:
        user_session_id = request.cookies.get(USER_SESSION_COOKIE)
        if user_session_id:
            user_sess = get_session(user_session_id, user_agent=ua)
            session_csrf = (user_sess or {}).get("csrf") or ""
    if not session_csrf or not hmac.compare_digest(header_token, session_csrf):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


@router.post("/auth/logout", dependencies=[Depends(_require_logout_csrf)])
async def logout(request: Request):
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        delete_session(admin_session_id)
    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if session_id:
        delete_session(session_id)
    resp = JSONResponse({"message": "已退出登录"})
    resp.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    resp.delete_cookie(USER_SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@router.get("/auth/session")
async def get_session_info(request: Request):
    ua = request.headers.get("User-Agent")
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        session = get_session(admin_session_id, user_agent=ua)
        if session and session.get("role") == "admin":
            return {
                "authenticated": True,
                "role": "admin",
                "username": session.get("username"),
                "admin_id": session.get("admin_id"),
            }

    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        session = get_session(user_session_id, user_agent=ua)
        if session and session.get("role") == "user":
            return {
                "authenticated": True,
                "role": "user",
                "username": session.get("username"),
                "email": session.get("email"),
                "user_id": session.get("user_id"),
            }
    return {"authenticated": False, "role": None}
