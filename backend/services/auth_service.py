"""Authentication service for the OpenAI-compatible gateway.

Resolves the raw `sk-...` key the caller sends in the `Authorization`
header (or `x_api_key`) to a fully populated context: the user id, the
managed `api_keys` row, allow/deny lists, monthly limits, and a quick
`check_key_restrictions` helper to enforce those limits on every
chat-completion request.

Supports two storage backends, in this order:
  1. The newer `api_keys` table (preferred, with restrictions).
  2. The legacy `users.api_key` column (compatibility shim for old data).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_db_context

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    """Timezone-aware UTC ``datetime`` (Python 3.12+ deprecates ``utcnow``)."""
    return datetime.now(timezone.utc)


def _hash_secret(secret: str) -> str:
    """SHA-256 digest of the normalised secret, matching `api_key_service` storage.

    The ``api_keys.key_hash`` column stores ``sha256(secret).hexdigest()``,
    not the raw value. Lookup queries MUST digest the inbound secret
    *first* — comparing the raw value to a digest column can never
    match and effectively blackholes every issued key.
    """
    s = _normalize(secret)
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _secrets_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (defence against timing attacks)."""
    try:
        return hmac.compare_digest(a or "", b or "")
    except Exception:
        return False


def _normalize(raw_key: str) -> str:
    """Strip the optional `Bearer ` prefix and whitespace."""
    if not raw_key:
        return ""
    s = raw_key.strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def _parse_csv_models(raw: Optional[str]) -> List[str]:
    """`allowed_models` / `denied_models` are stored as JSON or CSV strings."""
    if not raw:
        return []
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    # CSV fallback
    return [piece.strip() for piece in s.split(",") if piece.strip()]


def _row_to_key_info(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a raw DB row from `api_keys` to the public key_info dict."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row.get("name") or "",
        "key_prefix": row.get("key_prefix") or "",
        "key_mask": row.get("key_mask") or "",
        "is_active": bool(row.get("is_active", 1)),
        "expires_at": row.get("expires_at"),
        "allowed_models": _parse_csv_models(row.get("allowed_models")),
        "denied_models": _parse_csv_models(row.get("denied_models")),
        "allowed_ips": _parse_csv_models(row.get("allowed_ips")),
        "monthly_token_limit": row.get("monthly_token_limit"),
        "monthly_credit_limit": row.get("monthly_credit_limit"),
        "last_used_at": row.get("last_used_at"),
        "created_at": row.get("created_at"),
        "source": "api_keys",
    }


def _row_legacy_to_key_info(row: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a `users.api_key` row into the key_info shape."""
    return {
        "id": None,
        "user_id": row["id"],
        "name": row.get("username") or "",
        "key_prefix": (row.get("api_key") or "")[:8] + "...",
        "key_mask": None,
        "is_active": bool(row.get("is_active", 1)),
        "expires_at": None,
        "allowed_models": [],
        "denied_models": [],
        "allowed_ips": [],
        "monthly_token_limit": None,
        "monthly_credit_limit": None,
        "last_used_at": None,
        "created_at": row.get("created_at"),
        "source": "users.api_key",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_api_key(raw_key: str) -> Optional[Dict[str, Any]]:
    """Resolve the raw `sk-...` value sent by the caller to a key context.

    Returns None when the key is empty, malformed, or unknown. The
    returned dict matches the schema documented in the task brief.

    Security note: the ``key_hash`` column stores SHA-256(secret), so
    we MUST digest the inbound secret before the lookup. Comparing the
    raw value against a digest column can never match.
    """
    s = _normalize(raw_key)
    if not s or len(s) < 8:
        return None

    digest = _hash_secret(s)
    with get_db_context() as conn:
        cursor = conn.cursor()
        # First try the managed-keys table by digest.
        cursor.execute(
            """
            SELECT id, user_id, name, key_hash, key_prefix, key_mask,
                   monthly_token_limit, monthly_credit_limit,
                   allowed_models, denied_models, allowed_ips, is_active,
                   last_used_at, expires_at, created_at
              FROM api_keys
             WHERE key_hash = ? AND is_active = 1
             LIMIT 1
            """,
            (digest,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT id, user_id, name, key_hash, key_prefix, key_mask,
                       monthly_token_limit, monthly_credit_limit,
                       allowed_models, denied_models, allowed_ips, is_active,
                       last_used_at, expires_at, created_at
                  FROM api_keys
                 WHERE key_prefix = ? AND is_active = 1
                 LIMIT 1
                """,
                (s[:8],),
            )
            row = cursor.fetchone()
        if row:
            info = _row_to_key_info(dict(row))
            # Defence-in-depth: verify the matched prefix row's hash
            # actually matches the inbound secret before trusting it.
            # (Avoids a known-prefix from a different key colluding.)
            if info.get("source") == "api_keys":
                stored = dict(row).get("key_hash")
                if stored and not _secrets_compare(stored, digest):
                    logger.warning("auth: prefix collision rejected for row id=%s", row["id"])
                    return None
            return info

        # Fallback: legacy `users.api_key_hash` column. Migration 25
        # already hashed every plaintext api_key into this column, so
        # the digest lookup below is the only legacy path we keep.
        # The plaintext `users.api_key = ?` query that used to live
        # here was removed (P1.4) — it allowed timing-observable
        # reads of the plaintext column and a full-table-scan fallback
        # that bypassed the hash index. Operators with pre-migration
        # data should run
        #   UPDATE users SET api_key = NULL WHERE api_key_hash IS NOT NULL
        # so the plaintext column is no longer readable.
        legacy_digest = hashlib.sha256(s.encode("utf-8")).hexdigest()
        cursor.execute(
            "SELECT id, username, api_key, is_active, created_at FROM users WHERE api_key_hash = ? AND is_active = 1",
            (legacy_digest,),
        )
        legacy = cursor.fetchone()
        if legacy:
            logger.warning(
                "legacy users.api_key_hash fallback used for user_id=%s; "
                "consider migrating this user to the api_keys table",
                legacy["id"],
            )
            return _row_legacy_to_key_info(dict(legacy))

    return None


def update_last_used(api_key_id: Optional[int]) -> None:
    """Bump the `last_used_at` timestamp of an api_keys row.

    Silently no-ops when the row is a legacy user key (api_key_id is None)
    or when the table is not yet populated for that id.
    """
    if not api_key_id:
        return
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (_now_utc().isoformat(sep=" "), api_key_id),
            )
    except Exception as e:
        logger.warning("update_last_used failed for id=%s: %s", api_key_id, e)


# ---------------------------------------------------------------------------
# Usage / limit enforcement
# ---------------------------------------------------------------------------


def _monthly_usage_tokens(user_id: int) -> int:
    """Return tokens consumed by this user in the current calendar month."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS used
              FROM usage_logs
             WHERE user_id = ?
               AND strftime('%Y-%m', request_time) = strftime('%Y-%m', 'now')
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row["used"] or 0) if row else 0


def _monthly_usage_credits(user_id: int) -> float:
    """Return credits consumed by this user in the current calendar month."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(cost_credits), 0) AS used
              FROM usage_logs
             WHERE user_id = ?
               AND strftime('%Y-%m', request_time) = strftime('%Y-%m', 'now')
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return float(row["used"] or 0.0) if row else 0.0


def check_key_restrictions(key_info: Dict[str, Any], model: str) -> Optional[str]:
    """Enforce the active/expired/allow/deny/limit checks for a key.

    Returns None when the request is allowed, otherwise a short human
    readable rejection reason that gets surfaced to the caller in the
    OpenAI-standard error envelope.
    """
    if not key_info:
        return "无效的 API Key"

    if not key_info.get("is_active", True):
        return "API Key 已被禁用"

    expires_at = key_info.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at))
            # Normalise naive datetimes to UTC for an apples-to-apples compare.
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if _now_utc() > exp_dt:
                return "API Key 已过期"
        except (ValueError, TypeError):
            # Tolerate malformed timestamps: don't fail the request.
            pass

    allowed = key_info.get("allowed_models") or []
    if allowed and model and model not in allowed:
        return f"模型 {model} 不在允许列表中"

    denied = key_info.get("denied_models") or []
    if denied and model and model in denied:
        return f"模型 {model} 已被禁用"

    # M9: User-level model access control. The ``user_model_access`` table
    # holds per-user allow/deny rows (written by subscription-approval
    # flows or directly by admins). We only enforce ``deny`` rows here —
    # ``allow`` rows are informational (the default is already "allow
    # everything"), so a missing row never blocks a request. This keeps
    # the change backward-compatible: existing users with no rows in
    # ``user_model_access`` see no behaviour change.
    user_id = key_info.get("user_id")
    if user_id and model:
        reason = check_user_model_access(int(user_id), model)
        if reason:
            return reason

    token_limit = key_info.get("monthly_token_limit")
    if token_limit and user_id:
        used = _monthly_usage_tokens(int(user_id))
        if used >= int(token_limit):
            return f"本月 Token 配额已用完 ({used}/{token_limit})"

    credit_limit = key_info.get("monthly_credit_limit")
    if credit_limit and user_id:
        used = _monthly_usage_credits(int(user_id))
        if used >= float(credit_limit):
            return f"本月额度已用完 ({used:.4f}/{credit_limit})"

    return None


def check_user_model_access(user_id: int, model: str) -> Optional[str]:
    """Inspect the ``user_model_access`` table for an explicit deny row
    matching the requested model.

    Returns ``None`` when the request is allowed (the common case —
    no matching deny row), or a rejection reason string when a deny
    row matches.

    Matching rules (in priority order):
      1. Exact match on ``model_id`` (e.g. ``openai/gpt-4o``).
      2. Provider-wide match (e.g. ``openai/*`` matches any model
         whose ``model_id`` starts with ``openai/``).

    ``allow`` rows are ignored at runtime — the default is already
    "allow everything" (the table is empty for most users). ``allow``
    rows exist purely as an audit trail of subscription approvals.
    """
    if not user_id or not model:
        return None
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            # Match either:
            #   * exact model_id (e.g. ``openai/gpt-4o``)
            #   * provider-wide wildcard (e.g. ``openai/*`` matches any
            #     ``openai/...`` model). The wildcard form is detected
            #     by the trailing ``/*`` suffix; the prefix is then
            #     extracted via substr and matched with ``LIKE prefix/%``.
            cursor.execute(
                """
                SELECT access_type, model_id FROM user_model_access
                WHERE user_id = ?
                  AND access_type = 'deny'
                  AND (
                      model_id = ?
                      OR (
                          model_id LIKE '%/*'
                          AND ? LIKE substr(model_id, 1, length(model_id) - 2) || '/%'
                      )
                  )
                LIMIT 1
                """,
                (int(user_id), model, model),
            )
            row = cursor.fetchone()
            if row:
                return f"模型 {model} 已被管理员禁用"
    except Exception:
        # DB error reading the access table — fail open (allow) to
        # match the existing default-deny-never behaviour. A failed
        # read should not break a previously-working request.
        return None
    return None


def get_monthly_summary(user_id: int) -> Dict[str, Any]:
    """Convenience helper used by the `/v1/usage` endpoint."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(prompt_tokens), 0)  AS prompt,
                   COALESCE(SUM(completion_tokens), 0) AS completion,
                   COALESCE(SUM(total_tokens), 0)  AS total,
                   COALESCE(SUM(cost_credits), 0) AS cost
              FROM usage_logs
             WHERE user_id = ?
               AND strftime('%Y-%m', request_time) = strftime('%Y-%m', 'now')
            """,
            (user_id,),
        )
        row = cursor.fetchone()

    # ``sqlite3.Row`` is dict-like but does NOT implement ``.get``.
    # The previous code did ``row.get("prompt")`` which raised
    # ``AttributeError`` whenever ``row`` was non-None, breaking
    # every call to ``/v1/usage``. Wrap each lookup in a helper
    # that handles both shapes.
    def _val(key: str, default=0):
        try:
            v = row[key] if row is not None else None
        except (IndexError, KeyError):
            v = None
        return v if v is not None else default

    return {
        "prompt_tokens": int(_val("prompt", 0) or 0),
        "completion_tokens": int(_val("completion", 0) or 0),
        "total_tokens": int(_val("total", 0) or 0),
        "total_cost": float(_val("cost", 0.0) or 0.0),
    }
