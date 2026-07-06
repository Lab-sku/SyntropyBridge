import hashlib
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from backend.config import Config
from backend.database import (
    get_db_context,
    get_usage_windows,
    init_db,
    sanitize_input,
    validate_api_key_format,
)
from backend.models import AuthUserContext, UserCreate, UserResponse, UserUpdate
from backend.security import Security


logger = logging.getLogger(__name__)


# R4: reserved usernames that must not be allowed for self-service
# registration OR admin-created users OR profile updates. Prevents
# phishing / social-engineering attacks where an attacker registers a
# regular user account named "admin" or "support" and impersonates
# staff. Comparison is case-insensitive (callers do ``v.lower()``).
# Defined here (rather than in ``routes/auth.py``) because
# ``user_service`` is the lower-level shared service consumed by both
# the auth router and the admin router — avoids a circular import.
RESERVED_USERNAMES = {
    "admin", "administrator", "root", "system", "api", "support",
    "help", "info", "mod", "moderator", "staff", "official",
    "webmaster", "guest", "service", "superadmin", "master",
    "operator", "owner", "test", "null", "undefined", "sys",
    "security", "billing", "sales", "contact", "noreply",
}


class UserService:
    @staticmethod
    def init():
        init_db()

    @staticmethod
    def invalidate_all_credentials(user_id: int) -> None:
        """Revoke every credential for ``user_id``: sessions, tokens and
        api_keys.

        Called after a password change / reset so any stolen session
        cookie or previously-issued API key / token immediately stops
        working. Defensive against missing tables (schema drift on
        legacy installs) — falls back to deleting just sessions.
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
            with get_db_context() as conn:
                conn.cursor().execute(
                    "DELETE FROM sessions WHERE user_id = ?", (user_id,)
                )

    @staticmethod
    def auto_activate_free_plan(
        user_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        """Subscribe ``user_id`` to the free plan and initialise their wallet.

        When ``conn`` is provided, the operation runs within the caller's
        transaction and any failure propagates so the caller can roll
        back. When ``conn`` is ``None`` (default) the function opens its
        own connection and treats failures as best-effort (logged +
        swallowed) so legacy fire-and-forget callers (e.g.
        :meth:`create_user`) are not disturbed.

        **Fully idempotent** — if the user already has an active
        subscription the function returns immediately without granting
        duplicate credits.
        """
        if conn is not None:
            UserService._auto_activate_free_plan_impl(user_id, conn)
            return

        try:
            with get_db_context() as ctx:
                UserService._auto_activate_free_plan_impl(user_id, ctx)
        except Exception:
            logger.exception(
                "auto_activate_free_plan failed for user_id=%s", user_id
            )

    @staticmethod
    def _auto_activate_free_plan_impl(user_id: int, conn) -> None:
        """Implementation that runs inside ``conn``'s transaction.

        Routes the credit-side wallet mutation through
        :func:`grant_credits` so the resulting ``wallet_transactions``
        row gets the same ``expires_at`` stamp (driven by
        :attr:`Config.CREDITS_EXPIRE_DAYS`) as the renewal / upgrade
        paths. Without this, the free-plan initial credits would be the
        only monthly-grant that never expires — an inconsistency
        between "first grant" and "renewal grant" for the same plan.
        """
        from backend.database import grant_credits

        cursor = conn.cursor()

        # Idempotency guard: skip if the user already has an
        # active subscription (free or paid).
        cursor.execute(
            "SELECT id FROM subscriptions"
            " WHERE user_id = ? AND status = 'active' LIMIT 1",
            (user_id,),
        )
        if cursor.fetchone():
            return

        cursor.execute(
            "SELECT id, monthly_credits FROM plans"
            " WHERE code = 'free' AND is_active = 1 LIMIT 1"
        )
        plan = cursor.fetchone()
        if not plan:
            return

        if isinstance(plan, tuple):
            plan_id = plan[0]
            monthly_credits = float(plan[1] or 0)
        else:
            plan_id = plan["id"]
            monthly_credits = float(plan["monthly_credits"] or 0)

        cursor.execute(
            "INSERT OR IGNORE INTO wallets"
            " (user_id, balance, total_recharged, frozen)"
            " VALUES (?, 0, 0, 0)",
            (user_id,),
        )

        if monthly_credits > 0:
            grant_credits(
                user_id,
                monthly_credits,
                "bonus",
                related_type="subscription",
                related_id=None,
                note="Free plan initial credits",
                conn=conn,
            )

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=30)
        cursor.execute(
            """INSERT INTO subscriptions
               (user_id, plan_id, status, started_at, expires_at,
                credits_used_this_period, auto_renew)
               VALUES (?, ?, 'active', ?, ?, 0, 1)""",
            (
                user_id,
                plan_id,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                expires.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        cursor.execute(
            "UPDATE users SET plan_id = ? WHERE id = ?",
            (plan_id, user_id),
        )


    @staticmethod
    def create_user(user_data: "UserCreate") -> "UserResponse":
        username = sanitize_input(user_data.username)
        if not username or len(username) < 2 or len(username) > 50:
            raise ValueError("用户名长度必须在2-50个字符之间")
        # R4: block reserved usernames (case-insensitive) at the
        # service layer so admin-created accounts can't impersonate
        # staff ("admin", "support", "root", ...). Mirrors the
        # self-service registration guard in routes/auth.py.
        if username.lower() in RESERVED_USERNAMES:
            raise ValueError("该用户名为系统保留字，请使用其他用户名")

        api_key = Security.generate_api_key()
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        api_key_placeholder = secrets.token_hex(32)
        raw_password = (getattr(user_data, "password", None) or "").strip()
        if raw_password:
            # P1.6: enforce the same strong-password policy the
            # self-service registration flow uses, so admin-created
            # users can't land with a weak password.
            Security.assert_strong_password(raw_password, username=username)
            password_hash = Security.hash_password(raw_password)
            generated_password: Optional[str] = None
        else:
            generated_password = Security.generate_api_key()
            password_hash = Security.hash_password(generated_password)
        try:
            # Single transaction: user INSERT + wallet + subscription +
            # monthly_credits grant. If auto_activate_free_plan fails
            # (DB lock, disk full, etc.) the whole transaction rolls
            # back so we never leave a user row without a wallet /
            # subscription — which would otherwise leave the account
            # unable to call any priced model and unable to re-register
            # (username taken). Mirrors the /auth/register path.
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
                        getattr(user_data, "email", None),
                        password_hash,
                        api_key_placeholder,
                        api_key_hash,
                        user_data.quota_5h,
                        user_data.quota_week,
                    ),
                )
                user_id = cursor.lastrowid
                UserService.auto_activate_free_plan(user_id, conn=conn)
        except sqlite3.IntegrityError:
            raise ValueError("用户名或邮箱已存在")

        user = UserService.get_user(user_id)
        if user is None:
            raise ValueError("用户创建失败")
        user.api_key = api_key
        if generated_password:
            user.generated_password = generated_password  # type: ignore[attr-defined]
        return user

    @staticmethod
    def get_user(user_id: int) -> Optional[UserResponse]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return None

            usage_5h, usage_week = get_usage_windows(user_id)

            raw_key = row["api_key"] or ""
            if raw_key and len(raw_key) > 10:
                masked_key = f"{raw_key[:6]}...{raw_key[-4:]}"
            else:
                masked_key = ""

            return UserResponse(
                id=row["id"],
                username=row["username"],
                api_key=masked_key,
                quota_5h=row["quota_5h"],
                quota_week=row["quota_week"],
                usage_5h=usage_5h,
                usage_week=usage_week,
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                version=int(row["version"] if "version" in row.keys() else 1),
            )

    @staticmethod
    def get_user_by_api_key(api_key: str) -> Optional[AuthUserContext]:
        if not validate_api_key_format(api_key):
            return None
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        with get_db_context() as conn:
            cursor = conn.cursor()
            # P1.4: only the hash lookup is kept. The previous
            # full-table-scan + Python-side ``hmac.compare_digest``
            # fallback was a quadratic-cost timing-observable path
            # that bypassed the hash index. Migration 25 already
            # hashed every plaintext ``users.api_key`` into
            # ``api_key_hash``, so the hash lookup is sufficient.
            cursor.execute(
                "SELECT id, api_key, quota_5h, quota_week, is_active FROM users WHERE api_key_hash = ? AND is_active = 1",
                (key_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return AuthUserContext(
                id=row["id"],
                api_key=row["api_key"],
                quota_5h=row["quota_5h"],
                quota_week=row["quota_week"],
                is_active=bool(row["is_active"]),
            )

    @staticmethod
    def reveal_api_key(user_id: int) -> Optional[str]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT api_key FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return None
            raw = row["api_key"] or ""
            return raw if raw else None

    @staticmethod
    def list_users() -> List[UserResponse]:
        users = []
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM users ORDER BY created_at DESC
            """)
            rows = cursor.fetchall()
        for row in rows:
            usage_5h, usage_week = get_usage_windows(row["id"])
            # Mask the API key: show only prefix + suffix so a compromised
            # admin session cannot exfiltrate every user's full key.
            raw_key = row["api_key"] or ""
            if raw_key and len(raw_key) > 10:
                masked_key = f"{raw_key[:6]}...{raw_key[-4:]}"
            else:
                masked_key = ""
            users.append(
                UserResponse(
                    id=row["id"],
                    username=row["username"],
                    api_key=masked_key,
                    quota_5h=row["quota_5h"],
                    quota_week=row["quota_week"],
                    usage_5h=usage_5h,
                    usage_week=usage_week,
                    is_active=bool(row["is_active"]),
                    created_at=row["created_at"],
                    version=int(row["version"] if "version" in row.keys() else 1),
                )
            )
        return users

    @staticmethod
    def update_user(user_id: int, user_data: UserUpdate) -> Optional[UserResponse]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            updates = []
            params = []

            if user_data.username is not None:
                username = sanitize_input(user_data.username)
                if not username or len(username) < 2 or len(username) > 50:
                    raise ValueError("用户名长度必须在2-50个字符之间")
                # R4: block reserved usernames (case-insensitive) on
                # profile update so a logged-in user can't rename
                # themselves to "admin" / "support" etc. to bypass
                # the registration-time guard.
                if username.lower() in RESERVED_USERNAMES:
                    raise ValueError("该用户名为系统保留字，请使用其他用户名")
                # Uniqueness check — same pattern as email below. The
                # column carries a UNIQUE constraint already, but a
                # pre-check lets us return a clean 4xx for the
                # self-service profile endpoint instead of letting the
                # integrity-error bubble up as a 500. Applies to both
                # admin updates and user self-service (PATCH /user/profile).
                cursor.execute(
                    "SELECT id FROM users WHERE username = ? AND id <> ?",
                    (username, int(user_id)),
                )
                if cursor.fetchone():
                    raise ValueError("该用户名已被其他账号使用")
                updates.append("username = ?")
                params.append(username)
            if user_data.email is not None:
                email = sanitize_input(user_data.email or "")
                if email:
                    # Lightweight email validation — the route layer
                    # already does Pydantic's EmailStr, but the service
                    # is reachable from admin scripts too.
                    if "@" not in email or "." not in email.split("@")[-1]:
                        raise ValueError("邮箱格式不合法")
                    # Uniqueness check so an admin can't hand the same
                    # address to two users (the column has a unique
                    # index already — this just gives a 4xx instead of
                    # letting the unique-constraint error bubble up as
                    # a 500).
                    cursor.execute(
                        "SELECT id FROM users WHERE email = ? AND id <> ?",
                        (email.lower(), int(user_id)),
                    )
                    if cursor.fetchone():
                        raise ValueError("该邮箱已被其他账号使用")
                    email = email.lower()
                else:
                    email = None
                updates.append("email = ?")
                params.append(email)
            if user_data.password is not None:
                # P1.6: enforce strong-password policy at the service
                # layer so admin-created / admin-updated users can't
                # bypass the same checks the registration flow enforces.
                raw_pwd = (user_data.password or "").strip()
                if raw_pwd:
                    cursor.execute(
                        "SELECT username FROM users WHERE id = ?", (int(user_id),)
                    )
                    uname_row = cursor.fetchone()
                    uname = uname_row["username"] if uname_row else None
                    Security.assert_strong_password(raw_pwd, username=uname)
                updates.append("password_hash = ?")
                params.append(Security.hash_password(user_data.password))
                password_changed = True
            else:
                password_changed = False
            if user_data.quota_5h is not None:
                updates.append("quota_5h = ?")
                params.append(int(user_data.quota_5h))
            if user_data.quota_week is not None:
                updates.append("quota_week = ?")
                params.append(int(user_data.quota_week))
            if getattr(user_data, "quota_month", None) is not None:
                updates.append("quota_month = ?")
                params.append(int(user_data.quota_month))
            if getattr(user_data, "monthly_budget", None) is not None:
                updates.append("monthly_budget = ?")
                params.append(float(user_data.monthly_budget))
            if user_data.is_active is not None:
                updates.append("is_active = ?")
                params.append(1 if user_data.is_active else 0)
                deactivated = user_data.is_active is False
            else:
                deactivated = False

            if updates:
                # Optimistic-lock guard: when the caller supplied a
                # ``version`` (echoed back from the previous
                # UserResponse), include it in the WHERE clause so two
                # concurrent admin edits don't silently clobber each
                # other. The row's version is bumped atomically on
                # success. rowcount == 0 means another writer got
                # there first — surface as a 409-friendly ValueError.
                expected_version = getattr(user_data, "version", None)
                if expected_version is not None:
                    updates.append("version = version + 1")
                    sql = (
                        f"UPDATE users SET {', '.join(updates)} "
                        "WHERE id = ? AND version = ?"
                    )
                    params.append(int(expected_version))
                    params.append(user_id)
                    cursor.execute(sql, params)
                    if cursor.rowcount == 0:
                        raise ValueError("用户数据已被其他操作修改，请刷新后重试")
                else:
                    # Legacy path — no version supplied, last-write-wins.
                    params.append(user_id)
                    cursor.execute(
                        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                        params,
                    )

                if deactivated:
                    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        # P1.1: when the password changed, revoke ALL credentials
        # (sessions + tokens + api_keys), not just sessions. Done
        # outside the transaction above so the helper's own connection
        # does not deadlock on the same transaction.
        if password_changed:
            UserService.invalidate_all_credentials(int(user_id))

        return UserService.get_user(user_id)

    @staticmethod
    def delete_user(user_id: int) -> bool:
        with get_db_context() as conn:
            cursor = conn.cursor()

            def _delete_from(table: str) -> None:
                """DELETE FROM `table` WHERE user_id = ? IF the table
                exists and carries a user_id column. Silently no-op
                otherwise so schema drift between environments doesn't
                break the cascade."""
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if not cursor.fetchone():
                    return
                cursor.execute(f"PRAGMA table_info({table})")
                if "user_id" not in {row[1] for row in cursor.fetchall()}:
                    return
                cursor.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

            cursor.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "tokens",
                    "token_reservations",
                    "usage_rollups",
                    "usage_logs",
                    "quota_resets",
                    "conversations",
                    "api_keys",
                    "wallets",
                    "wallet_transactions",
                    "orders",
                    "subscriptions",
                    "channels",
                    "subscription_requests",
                    "user_model_access",
                    "promo_code_usage",
                    "redeem_code_usage",
                    "user_provider_keys",
                    "notifications",
                    "password_reset_tokens",
                ):
                    _delete_from(table)

                cursor.execute(
                    """
                    UPDATE promo_codes SET used_count = MAX(0, used_count - 1)
                    WHERE id IN (SELECT promo_code_id FROM promo_code_usage WHERE user_id = ?)
                    """,
                    (user_id,),
                )
                cursor.execute(
                    """
                    UPDATE redeem_codes SET used_count = MAX(0, used_count - 1)
                    WHERE id IN (SELECT redeem_code_id FROM redeem_code_usage WHERE user_id = ?)
                    """,
                    (user_id,),
                )

                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
                )
                if cursor.fetchone():
                    cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
                deleted = cursor.rowcount > 0
                cursor.execute("COMMIT")
                return deleted
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    @staticmethod
    def purge_soft_deleted_users(
        retention_days: Optional[int] = None,
    ) -> int:
        """Hard-delete users that were soft-deleted longer than
        ``retention_days`` ago.

        The self-service ``POST /user/data/delete`` endpoint rewrites
        the user row to ``is_active=0``, ``email=NULL``,
        ``username=deleted_{id}_{ts}`` and promises the user their
        data will be fully removed after 30 days. This method is the
        fulfilment of that promise: it selects candidate rows and
        feeds them to :meth:`delete_user`, which cascades through
        every related table.

        Best-effort per user — a failing cascade is logged and the
        loop continues so one bad row cannot block the purge.

        Returns the count of successfully hard-deleted users.
        """
        days = int(retention_days if retention_days is not None else Config.SOFT_DELETE_RETENTION_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_ts = int(cutoff.timestamp())

        candidates: List[int] = []
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, username FROM users
                WHERE is_active = 0
                  AND email IS NULL
                  AND username LIKE 'deleted\\_%' ESCAPE '\\'
                """
            )
            for row in cursor.fetchall():
                uname = row["username"] or ""
                parts = uname.rsplit("_", 1)
                if len(parts) != 2:
                    continue
                try:
                    deleted_ts = int(parts[-1])
                except (TypeError, ValueError):
                    continue
                if deleted_ts <= cutoff_ts:
                    candidates.append(int(row["id"]))

        purged = 0
        for uid in candidates:
            try:
                if UserService.delete_user(uid):
                    purged += 1
                    logger.info("purged soft-deleted user_id=%s", uid)
            except Exception:
                logger.exception("purge failed for soft-deleted user_id=%s", uid)
        return purged

    @staticmethod
    def check_quota_with_limits(user_id: int, quota_5h: int, quota_week: int) -> tuple[bool, str]:
        usage_5h, usage_week = get_usage_windows(user_id)

        if usage_5h >= quota_5h:
            return False, f"5小时配额已用完 ({usage_5h}/{quota_5h})"
        if usage_week >= quota_week:
            return False, f"周配额已用完 ({usage_week}/{quota_week})"

        return True, "OK"

    @staticmethod
    def check_quota(user_id: int) -> tuple[bool, str]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT quota_5h, quota_week FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return False, "User not found"

        return UserService.check_quota_with_limits(user_id, row["quota_5h"], row["quota_week"])

    @staticmethod
    def get_user_stats(user_id: int) -> dict:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*) as total_requests,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(AVG(response_time_ms), 0) as avg_response_time,
                    SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate
                FROM usage_logs
                WHERE user_id = ?
            """,
                (user_id,),
            )
            row = cursor.fetchone()
            return {
                "total_requests": row["total_requests"] or 0,
                "total_tokens": row["total_tokens"] or 0,
                "avg_response_time": round(row["avg_response_time"] or 0, 2),
                "success_rate": round(row["success_rate"] or 0, 2),
            }

    @staticmethod
    def change_password(user_id: int, old_password: str, new_password: str) -> bool:
        """Rotate a user's own password.

        The user-facing flow: caller must prove knowledge of the current
        password before we let them set a new one. Raises
        :class:`ValueError` on validation failure so the route can map
        it to a 4xx.
        """
        if not old_password or not new_password:
            raise ValueError("缺少原密码或新密码")
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash, username FROM users WHERE id = ?",
                (int(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("用户不存在")
            if not Security.verify_password(old_password, row["password_hash"] or ""):
                raise ValueError("原密码错误")
            # Fix 4: pass the username so assert_strong_password can
            # reject passwords that contain it (same as registration).
            username = row["username"] if "username" in row.keys() else None
            Security.assert_strong_password(new_password, username=username)
            cursor.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (Security.hash_password(new_password), int(user_id)),
            )
        # P1.1: revoke ALL credentials (sessions + tokens + api_keys),
        # not just sessions. Done outside the transaction above so the
        # helper's own connection does not deadlock.
        UserService.invalidate_all_credentials(int(user_id))
        return True

    # ------------------------------------------------------------------
    # Dashboard usage aggregation helpers (session-cookie endpoints)
    # ------------------------------------------------------------------

    @staticmethod
    def get_usage_summary(user_id: int) -> dict:
        """Return quota usage snapshot for the dashboard.

        Uses :func:`get_quota_snapshot` for live counters and adds
        percentage calculations and plan info.
        """
        from backend.database import get_quota_snapshot

        snap = get_quota_snapshot(user_id)
        if not snap.get("exists"):
            return {}

        def _pct(used: float, limit: float) -> float:
            if limit <= 0:
                return 0.0
            return round(min(used / limit * 100, 100.0), 2)

        quota_5h = snap["quota_5h"]
        quota_week = snap["quota_week"]
        quota_month = snap["quota_month"]
        monthly_budget = snap["monthly_budget"]

        tokens_5h = snap.get("tokens_5h", 0)
        tokens_week = snap.get("tokens_week", 0)
        tokens_month = snap.get("tokens_month", 0)
        monthly_cost = snap.get("monthly_cost", 0.0)

        # Fetch plan info
        plan_info = None
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT plan_id, plan_expires_at FROM users WHERE id = ?",
                (user_id,),
            )
            urow = cursor.fetchone()
            plan_id = urow["plan_id"] if urow else None
            plan_expires = urow["plan_expires_at"] if urow else None

            if plan_id:
                cursor.execute("SELECT code, name FROM plans WHERE id = ?", (plan_id,))
                prow = cursor.fetchone()
                if prow:
                    plan_info = {
                        "code": prow["code"],
                        "name": prow["name"],
                        "expires_at": plan_expires,
                    }

        return {
            "quota_5h": {
                "used": tokens_5h,
                "limit": quota_5h,
                "percent": _pct(tokens_5h, quota_5h),
            },
            "quota_week": {
                "used": tokens_week,
                "limit": quota_week,
                "percent": _pct(tokens_week, quota_week),
            },
            "quota_month": {
                "used": tokens_month,
                "limit": quota_month,
                "percent": _pct(tokens_month, quota_month),
            },
            "monthly_budget": {
                "used_credits": round(monthly_cost, 2),
                "limit_credits": round(monthly_budget, 2),
                "percent": _pct(monthly_cost, monthly_budget),
            },
            "wallet_balance": snap.get("balance", 0.0),
            "current_plan": plan_info,
        }

    @staticmethod
    def get_usage_chart(user_id: int, range_days: int = 30) -> list:
        """Return daily usage data for the chart (requests, tokens, cost)."""
        range_days = max(1, min(int(range_days), 365))
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT strftime('%Y-%m-%d', request_time) AS date,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(cost_credits), 0) AS cost_credits
                FROM usage_logs
                WHERE user_id = ?
                  AND request_time > datetime('now', ?)
                GROUP BY strftime('%Y-%m-%d', request_time)
                ORDER BY date ASC
                """,
                (user_id, f"-{range_days} days"),
            )
            rows = cursor.fetchall()
            return [
                {
                    "date": r["date"],
                    "requests": int(r["requests"] or 0),
                    "tokens": int(r["tokens"] or 0),
                    "cost_credits": float(r["cost_credits"] or 0),
                }
                for r in rows
            ]

    @staticmethod
    def get_usage_by_model(user_id: int, range_days: int = 30) -> list:
        """Return per-model breakdown for the dashboard."""
        range_days = max(1, min(int(range_days), 365))
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(model, 'unknown') AS model,
                       COALESCE(provider, 'unknown') AS provider,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(SUM(cost_credits), 0) AS cost_credits
                FROM usage_logs
                WHERE user_id = ?
                  AND request_time > datetime('now', ?)
                GROUP BY model, provider
                ORDER BY tokens DESC
                """,
                (user_id, f"-{range_days} days"),
            )
            rows = cursor.fetchall()
            return [
                {
                    "model": r["model"],
                    "provider": r["provider"],
                    "requests": int(r["requests"] or 0),
                    "tokens": int(r["tokens"] or 0),
                    "cost_credits": float(r["cost_credits"] or 0),
                }
                for r in rows
            ]

    @staticmethod
    def freeze_user(user_id: int, admin_id: int, reason: str) -> bool:
        """Set is_active=0, invalidate all sessions, and audit-log the action."""
        from backend.database import add_audit_log, release_reservation

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                return False
            # Count active token reservations before freezing so the
            # operator has a signal for how many in-flight streams were
            # interrupted. Best-effort: table may not exist on fresh
            # installs (migration 29/32 creates it).
            active_reservations = 0
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM token_reservations"
                    " WHERE user_id = ? AND reserved_until > datetime('now')",
                    (str(int(user_id)),),
                )
                active_reservations = int(cursor.fetchone()[0] or 0)
            except sqlite3.OperationalError:
                pass
            cursor.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
            # Invalidate all existing sessions so the frozen user is
            # immediately logged out and cannot use stale cookies.
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        # Force-release every active token reservation so the quota gate
        # blocks new requests immediately (rather than waiting for the
        # 300s TTL). In-flight streams are torn down by the per-chunk
        # ``is_active`` check in the streaming generators.
        try:
            release_reservation(user_id)
        except Exception:
            logger.warning("release_reservation failed for user %s", user_id, exc_info=True)

        logger.info(
            "freeze_user user_id=%s reason=%s active_reservations=%s",
            user_id,
            reason,
            active_reservations,
        )

        add_audit_log(
            actor_type="admin",
            actor_id=admin_id,
            action="ADMIN_FREEZE_USER",
            target_type="user",
            target_id=str(user_id),
            metadata={
                "reason": reason,
                "username": user["username"],
                "active_reservations": active_reservations,
            },
        )
        # Notify the user in-app so they see the freeze notice on next
        # login instead of discovering it via a 403 with no context.
        # Best-effort: a notification failure must not roll back the
        # freeze.
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                int(user_id),
                type="account_frozen",
                title="账户已被冻结",
                body=(
                    f"您的账户已被管理员冻结"
                    + (f"，原因：{reason}" if reason else "")
                    + "。如有疑问请联系客服。"
                ),
                metadata={"admin_id": admin_id, "reason": reason},
            )
        except Exception:
            logger.exception("freeze_user notification failed user=%s", user_id)
        return True

    @staticmethod
    def unfreeze_user(user_id: int, admin_id: int, reason: str) -> bool:
        """Set is_active=1 and audit-log the action."""
        from backend.database import add_audit_log

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                return False
            cursor.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
            # Also clear any residual sessions that may have accumulated
            # while the account was frozen (edge-case safety).
            cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        add_audit_log(
            actor_type="admin",
            actor_id=admin_id,
            action="ADMIN_UNFREEZE_USER",
            target_type="user",
            target_id=str(user_id),
            metadata={"reason": reason, "username": user["username"]},
        )
        # Mirror the freeze notice — tell the user their account is
        # usable again so they don't have to guess from a successful
        # login.
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                int(user_id),
                type="account_unfrozen",
                title="账户已解冻",
                body="您的账户已恢复使用。",
                metadata={"admin_id": admin_id, "reason": reason},
            )
        except Exception:
            logger.exception("unfreeze_user notification failed user=%s", user_id)
        return True
