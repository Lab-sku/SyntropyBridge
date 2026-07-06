from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.config import Config
from backend.database import get_db_context
from backend.security import Security

ADMIN_SESSION_COOKIE = "mm_admin_session"
USER_SESSION_COOKIE = "mm_session"
CSRF_COOKIE = "mm_csrf"

SESSION_TTL_SECONDS = 3600

# When the user ticks "Keep me signed in" we extend the cookie lifetime
# to a month. This is still safer than persisting a long-lived bearer
# token in localStorage because:
#  * the cookie is HttpOnly (inaccessible to JS, immune to XSS exfil)
#  * the cookie is automatically rotated on every login / logout
#  * revocation is one server-side delete_session() away
REMEMBER_ME_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# Absolute session caps — even if the idle TTL hasn't expired, a session
# is rejected after this wall-clock limit. Prevents indefinite replay of
# a stolen session cookie.
ABSOLUTE_TIMEOUT_REGULAR = 8 * 3600  # 8 hours
ABSOLUTE_TIMEOUT_REMEMBER = 30 * 24 * 3600  # 30 days (same as cookie max-age)


def _cookie_secure() -> bool:
    return Config.is_production()


def _get_session_ttl(absolute_expires_at_raw, created_at_raw) -> int:
    """Infer the idle TTL that was used when the session was created.

    We need this to compute the refresh threshold (TTL / 2) without
    requiring the TTL to be stored explicitly. Remember-me sessions have
    an absolute lifetime of ~30 days; regular sessions have ~8 hours.
    """
    if absolute_expires_at_raw and created_at_raw:
        try:
            abs_exp = datetime.fromisoformat(str(absolute_expires_at_raw))
            created = datetime.fromisoformat(str(created_at_raw))
            if abs_exp.tzinfo is None:
                abs_exp = abs_exp.replace(tzinfo=timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (abs_exp - created).total_seconds() > 24 * 3600:
                return REMEMBER_ME_TTL_SECONDS
        except Exception:
            pass
    return SESSION_TTL_SECONDS


def _maybe_refresh_session(
    conn,
    session_id: str,
    ttl_seconds: int,
    absolute_expires_at_raw=None,
) -> bool:
    """If the session is past its refresh threshold, extend expires_at.

    Sliding-window refresh: when ``time_remaining < TTL / 2`` the idle
    expiry is pushed to ``now + TTL``, capped at the absolute timeout so
    a session can never live longer than the wall-clock limit set at
    creation time.

    Returns ``True`` if the row was updated, ``False`` otherwise.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT expires_at FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    row = cursor.fetchone()
    if not row:
        return False

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    remaining = (expires_at - now).total_seconds()
    if remaining >= ttl_seconds / 2:
        return False  # still in the first half — no refresh needed

    new_expires = now + timedelta(seconds=ttl_seconds)

    # Respect the absolute cap: never extend past absolute_expires_at.
    if absolute_expires_at_raw:
        try:
            absolute = datetime.fromisoformat(str(absolute_expires_at_raw))
            if absolute.tzinfo is None:
                absolute = absolute.replace(tzinfo=timezone.utc)
            if new_expires > absolute:
                new_expires = absolute
        except Exception:
            pass

    if new_expires <= expires_at:
        return False  # cap prevents any extension — nothing to do

    cursor.execute(
        "UPDATE sessions SET expires_at = ? WHERE session_id = ?",
        (new_expires.isoformat(), session_id),
    )
    return True


def _columns_exist(conn, table: str, columns: list[str]) -> bool:
    """Check whether *all* ``columns`` are present in ``table``."""
    try:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        return all(c in existing for c in columns)
    except Exception:
        return False


def create_session(
    payload: dict,
    ttl_seconds: int = SESSION_TTL_SECONDS,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, str]:
    session_id = Security.generate_session_id()
    csrf_token = Security.generate_csrf_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    # Absolute timeout: wall-clock cap independent of idle TTL.
    if ttl_seconds >= REMEMBER_ME_TTL_SECONDS:
        absolute_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=ABSOLUTE_TIMEOUT_REMEMBER
        )
    else:
        absolute_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=ABSOLUTE_TIMEOUT_REGULAR
        )

    # Truncate UA to fit the column (512 chars max in the migration).
    ua_truncated = (user_agent or "")[:512] or None
    ip_val = (ip_address or "")[:45] or None

    with get_db_context() as conn:
        cursor = conn.cursor()
        has_new_cols = _columns_exist(
            conn, "sessions", ["user_agent", "absolute_expires_at", "ip_address"]
        )
        if has_new_cols:
            cursor.execute(
                """
                INSERT INTO sessions
                    (session_id, role, admin_id, user_id, username, email, csrf,
                     expires_at, ip_address, user_agent, absolute_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    payload.get("role"),
                    payload.get("admin_id"),
                    payload.get("user_id"),
                    payload.get("username"),
                    payload.get("email"),
                    csrf_token,
                    expires_at.isoformat(),
                    ip_val,
                    ua_truncated,
                    absolute_expires_at.isoformat(),
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO sessions (session_id, role, admin_id, user_id, username, email, csrf, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    payload.get("role"),
                    payload.get("admin_id"),
                    payload.get("user_id"),
                    payload.get("username"),
                    payload.get("email"),
                    csrf_token,
                    expires_at.isoformat(),
                ),
            )

    # Enforce per-user concurrent-session cap from the user's plan.
    # Best-effort: never blocks login; just evicts the oldest excess
    # sessions so a compromised credential cannot silently accumulate
    # an unbounded number of live sessions across devices.
    _user_id = payload.get("user_id")
    if _user_id and payload.get("role") != "admin":
        try:
            from backend.database import get_user_plan

            plan = get_user_plan(int(_user_id)) or {}
            # Only enforce when the user has a real persisted plan. The
            # _FREE_FALLBACK dict (id=None) is what get_user_plan hands
            # back to users whose plan has lapsed entirely; we don't
            # want to kick those users' sessions every login.
            if plan.get("id") is not None:
                max_concurrent = int(plan.get("max_concurrent") or 0)
                if max_concurrent > 0:
                    with get_db_context() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """
                            SELECT session_id FROM sessions
                            WHERE user_id = ? AND session_id <> ?
                            ORDER BY created_at DESC, session_id DESC
                            """,
                            (int(_user_id), session_id),
                        )
                        rows = cursor.fetchall()
                        for row in rows[max_concurrent:]:
                            cursor.execute(
                                "DELETE FROM sessions WHERE session_id = ?",
                                (row["session_id"],),
                            )
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).debug(
                "enforce_session_limit failed for user_id=%s", _user_id, exc_info=True
            )

    return session_id, csrf_token


def get_session(session_id: str, user_agent: str | None = None) -> Optional[dict]:
    from backend.database import get_setting
    if get_setting("global_freeze") == "true":
        delete_session(session_id)
        return None
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None

        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
        except Exception:
            delete_session(session_id)
            return None

        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= now:
            delete_session(session_id)
            return None

        # --- Absolute timeout enforcement ---
        # Even if the idle TTL hasn't expired, reject the session when the
        # wall-clock absolute cap has been reached.
        has_new_cols = _columns_exist(conn, "sessions", ["user_agent", "absolute_expires_at"])
        abs_raw = None
        created_at_raw = None
        if has_new_cols:
            abs_raw = row["absolute_expires_at"] if "absolute_expires_at" in row.keys() else None
            if abs_raw:
                try:
                    abs_expires = datetime.fromisoformat(abs_raw)
                    if abs_expires.tzinfo is None:
                        abs_expires = abs_expires.replace(tzinfo=timezone.utc)
                    if now >= abs_expires:
                        delete_session(session_id)
                        return None
                except Exception:
                    pass

            # --- User-Agent binding (Option B: bind to UA, tolerate IP changes) ---
            stored_ua = row["user_agent"] if "user_agent" in row.keys() else None
            if stored_ua:
                # If a UA was recorded at session creation, the caller MUST
                # provide a matching one.  Sending no User-Agent header
                # (empty/None) from an attacker who stole the cookie should
                # NOT bypass the binding — reject the session instead.
                incoming_ua = (user_agent or "")[:512]
                if not incoming_ua or stored_ua != incoming_ua:
                    delete_session(session_id)
                    return None

            created_at_raw = row["created_at"] if "created_at" in row.keys() else None

        # --- Sliding-window refresh ---
        # Extend the session if it is past the refresh threshold (50 % of
        # its idle TTL).  Pure server-side: the browser cookie is not
        # updated, but the server's expires_at is authoritative.
        ttl = _get_session_ttl(abs_raw, created_at_raw)
        _maybe_refresh_session(conn, session_id, ttl, abs_raw)

        # Check whether the owning user / admin has been frozen or
        # deactivated.  This runs on *every* request (previously only
        # every 10th) so that a freeze takes effect immediately rather
        # than allowing up to 9 unauthorised requests.
        if row["role"] == "user" and row["user_id"]:
            cursor.execute(
                "SELECT is_active FROM users WHERE id = ?", (row["user_id"],)
            )
            user_row = cursor.fetchone()
            if user_row and not user_row["is_active"]:
                delete_session(session_id)
                return None
        elif row["role"] == "admin" and row["admin_id"]:
            cursor.execute(
                "SELECT is_active FROM admin_users WHERE id = ?", (row["admin_id"],)
            )
            admin_row = cursor.fetchone()
            if admin_row and not admin_row["is_active"]:
                delete_session(session_id)
                return None

        return {
            "role": row["role"],
            "admin_id": row["admin_id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "email": row["email"],
            "csrf": row["csrf"],
        }


def delete_session(session_id: str) -> None:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def session_cookie_kwargs(ttl_seconds: int = SESSION_TTL_SECONDS) -> dict:
    return {
        "httponly": True,
        "secure": _cookie_secure(),
        "samesite": "lax",
        "path": "/",
        "max_age": ttl_seconds,
    }


def csrf_cookie_kwargs(ttl_seconds: int = SESSION_TTL_SECONDS) -> dict:
    return {
        "httponly": False,
        "secure": _cookie_secure(),
        "samesite": "lax",
        "path": "/",
        "max_age": ttl_seconds,
    }
