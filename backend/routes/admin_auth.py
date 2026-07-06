"""Shared admin-auth FastAPI dependencies.

The admin guard + CSRF guard are used across five routers
(``admin.py``, ``admin_billing.py``, ``admin_stats.py``,
``providers.py``, ``platform.py``). Keeping them in one module makes
it harder to drift out of sync — the original bug that prompted this
extraction was exactly that: CSRF enforcement differed subtly
between files because each had its own copy.

Two flavours are exposed:

* :func:`require_admin` / :func:`require_admin_csrf` — canonical
  dependencies. Accept either the HttpOnly admin session cookie OR the
  legacy ``Authorization: Bearer <jwt>`` header (some operators still
  hit the admin API from CLI scripts with the old flow).

* ``_admin_guard`` / ``_admin_csrf_guard`` / ``_require_admin`` /
  ``_require_admin_csrf`` — aliases for drop-in migration. Import
  whichever name matches the file you're updating; no call-site
  changes required.

Routers that don't care about the legacy JWT path
(``admin_stats.py`` today) can still use the canonical functions —
the legacy path is a no-op when no ``Authorization`` header is sent.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request

from backend.session import (
    ADMIN_SESSION_COOKIE,
    CSRF_COOKIE,
    get_session as get_server_session,
)


# ---------------------------------------------------------------------------
# Legacy JWT helpers
# ---------------------------------------------------------------------------


def _legacy_jwt_secret() -> str:
    """Resolve the JWT signing key.

    The legacy admin bearer flow predates the server-side session
    store and signed short-lived admin tokens with ``Config.SECRET_KEY``.
    Kept for backward compatibility with operators who hit the admin
    API from CLI scripts. New callers should use the session cookie.
    """
    from backend.config import Config

    return Config.SECRET_KEY or ""


def _verify_legacy_jwt(token: str) -> bool:
    """Return True when ``token`` is a valid legacy admin JWT."""
    secret = _legacy_jwt_secret()
    if not secret:
        return False
    try:
        import time

        import jwt

        payload = jwt.decode(token, secret, algorithms=["HS256"])
        # PyJWT only validates ``exp`` when the claim is *present* — a
        # token minted without ``exp`` would otherwise never expire,
        # leaving a permanent admin backdoor. Require it explicitly and
        # reject when missing or already in the past.
        exp = payload.get("exp")
        if exp is None or float(exp) < time.time():
            return False
        return payload.get("sub") == "admin"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Canonical dependencies
# ---------------------------------------------------------------------------


def require_admin(
    request: Request,
    authorization: str = Header(default=""),
) -> None:
    """FastAPI dependency: require an authenticated admin.

    Accepts EITHER the ``mm_admin_session`` cookie set by
    ``/api/admin/login`` (modern SPA path) OR a legacy
    ``Authorization: Bearer <jwt>`` header (CLI / scripting path).
    Raises :class:`HTTPException` (401) otherwise.
    """
    # 1. Session-cookie path (preferred).
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        sess = get_server_session(
            admin_session_id, user_agent=request.headers.get("User-Agent")
        )
        if sess and sess.get("role") == "admin":
            return

    # 2. Legacy JWT bearer path.
    if authorization:
        token = authorization.replace("Bearer ", "").strip()
        if token and _verify_legacy_jwt(token):
            return

    raise HTTPException(status_code=401, detail="未授权")


def require_admin_csrf(
    request: Request,
    authorization: str = Header(default=""),
) -> None:
    """FastAPI dependency: require admin auth AND CSRF on state-changing verbs.

    CSRF enforcement is cookie-path only. JWT-bearer callers never
    send the ``mm_csrf`` cookie, and the Authorization header is
    inherently CSRF-safe (browsers don't auto-attach custom headers).
    """
    require_admin(request, authorization)

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not admin_session_id:
        # JWT-bearer caller — no CSRF check needed.
        return

    sess = get_server_session(
        admin_session_id, user_agent=request.headers.get("User-Agent")
    )
    if not sess:
        # Auth passed via cookie but session is gone — re-raise 403
        # rather than 401 so the SPA can distinguish "bad CSRF" from
        # "logged out".
        raise HTTPException(status_code=403, detail="CSRF 校验失败")

    header_token = (request.headers.get("X-CSRF-Token") or "").strip()
    cookie_token = (request.cookies.get(CSRF_COOKIE) or "").strip()
    session_token = (sess.get("csrf") or "").strip()
    if (
        not header_token
        or not hmac.compare_digest(header_token, cookie_token)
        or not hmac.compare_digest(header_token, session_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


# ---------------------------------------------------------------------------
# Migration aliases (drop-in replacements for existing per-file copies)
# ---------------------------------------------------------------------------


_admin_guard = require_admin
_admin_csrf_guard = require_admin_csrf
_require_admin = require_admin
_require_admin_csrf = require_admin_csrf
