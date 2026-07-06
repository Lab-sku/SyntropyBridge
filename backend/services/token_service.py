import hashlib
import json
import secrets
from datetime import datetime
from typing import Optional

from backend.database import get_db_context
from backend.models import AuthUserContext


class TokenService:
    @staticmethod
    def _to_sqlite_ts(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _normalize_json_list(value: Optional[list[str]]) -> Optional[str]:
        if not value:
            return None
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        if not cleaned:
            return None
        return json.dumps(cleaned, ensure_ascii=False)

    @staticmethod
    def _parse_json_list(raw: Optional[str]) -> list[str]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        results: list[str] = []
        for item in data:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                results.append(s)
        return results

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_token_format(token: str) -> bool:
        if not token or len(token) < 24:
            return False
        if not token.startswith("mmx_tk_"):
            return False
        return all(ch.isalnum() or ch in "-_" for ch in token)

    @staticmethod
    def generate_token() -> str:
        return f"mmx_tk_{secrets.token_urlsafe(32)}"

    @staticmethod
    def create_token(
        *,
        user_id: int,
        name: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        allowed_models: Optional[list[str]] = None,
        allowed_ips: Optional[list[str]] = None,
        rate_limit_per_minute: Optional[int] = None,
        rate_limit_per_hour: Optional[int] = None,
    ) -> dict:
        minute_limit = None
        hour_limit = None
        if rate_limit_per_minute is not None:
            try:
                v = int(rate_limit_per_minute)
                if v > 0:
                    minute_limit = v
            except Exception:
                minute_limit = None
        if rate_limit_per_hour is not None:
            try:
                v = int(rate_limit_per_hour)
                if v > 0:
                    hour_limit = v
            except Exception:
                hour_limit = None

        token = TokenService.generate_token()
        token_hash = TokenService._hash_token(token)
        token_prefix = token[:12]
        expires_at_str = TokenService._to_sqlite_ts(expires_at) if expires_at else None
        allowed_models_raw = TokenService._normalize_json_list(allowed_models)
        allowed_ips_raw = TokenService._normalize_json_list(allowed_ips)

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO tokens (
                    user_id, name, token_prefix, token_hash, is_active,
                    expires_at, allowed_models, allowed_ips,
                    rate_limit_per_minute, rate_limit_per_hour
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    (name or "").strip() or None,
                    token_prefix,
                    token_hash,
                    expires_at_str,
                    allowed_models_raw,
                    allowed_ips_raw,
                    minute_limit,
                    hour_limit,
                ),
            )
            token_id = int(cursor.lastrowid)

        return {"id": token_id, "token": token, "token_prefix": token_prefix}

    @staticmethod
    def list_tokens(*, user_id: int) -> list[dict]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    id, name, token_prefix, created_at, last_used_at,
                    is_active, revoked_at, expires_at, allowed_models, allowed_ips,
                    rate_limit_per_minute, rate_limit_per_hour
                FROM tokens
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (int(user_id),),
            )
            rows = cursor.fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "token_prefix": row["token_prefix"],
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"],
                    "is_active": bool(row["is_active"]),
                    "revoked_at": row["revoked_at"],
                    "expires_at": row["expires_at"],
                    "allowed_models": TokenService._parse_json_list(row["allowed_models"]),
                    "allowed_ips": TokenService._parse_json_list(row["allowed_ips"]),
                    "rate_limit_per_minute": row["rate_limit_per_minute"],
                    "rate_limit_per_hour": row["rate_limit_per_hour"],
                }
            )
        return results

    @staticmethod
    def disable_token(*, user_id: int, token_id: int) -> bool:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tokens
                SET is_active = 0
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (int(token_id), int(user_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def revoke_token(*, user_id: int, token_id: int) -> bool:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tokens
                SET is_active = 0, revoked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (int(token_id), int(user_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def get_user_by_token(token: str) -> Optional[tuple[AuthUserContext, int, dict]]:
        if not TokenService._validate_token_format(token):
            return None

        token_hash = TokenService._hash_token(token)
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    t.id AS token_id,
                    u.id AS user_id,
                    u.api_key,
                    u.quota_5h,
                    u.quota_week,
                    u.is_active AS user_active,
                    t.expires_at AS expires_at,
                    t.allowed_models AS allowed_models,
                    t.allowed_ips AS allowed_ips,
                    t.rate_limit_per_minute AS rate_limit_per_minute,
                    t.rate_limit_per_hour AS rate_limit_per_hour
                FROM tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_hash = ?
                  AND t.is_active = 1
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)
                  AND u.is_active = 1
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            cursor.execute(
                "UPDATE tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(row["token_id"]),),
            )

        return (
            AuthUserContext(
                id=int(row["user_id"]),
                api_key=row["api_key"],
                quota_5h=int(row["quota_5h"]),
                quota_week=int(row["quota_week"]),
                is_active=bool(row["user_active"]),
            ),
            int(row["token_id"]),
            {
                "expires_at": row["expires_at"],
                "allowed_models": TokenService._parse_json_list(row["allowed_models"]),
                "allowed_ips": TokenService._parse_json_list(row["allowed_ips"]),
                "rate_limit_per_minute": row["rate_limit_per_minute"],
                "rate_limit_per_hour": row["rate_limit_per_hour"],
            },
        )
