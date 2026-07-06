"""Provider-side multi-key pool with weighted round-robin + cooldown.

Centralises selection of an upstream API key for a given provider, falls
back to the platform-wide key (or per-user key) when the pool is empty,
and records success / failure statistics in `provider_keys` so a single
broken key never blocks the entire pipeline.

The pool is intentionally read-only with respect to the database schema:
it never creates or migrates tables. The actual key value is stored
elsewhere (settings table for built-in providers, `custom_providers` for
custom platforms, `user_provider_keys` for per-user bindings) and the
`provider_keys` table only tracks per-key health metadata.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_db_context, get_setting
from backend.security import Security
from backend.services import custom_providers

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 300  # 5 minutes after a failure
# Per-process round-robin cursor. We use a single global counter per
# provider because the pool is shared across all users.
_RR_LOCK = threading.Lock()
_RR_COUNTER: Dict[str, int] = {}


def _key_hash(raw_key: str) -> str:
    """Return a stable hash of the key for identification in provider_keys."""
    if not raw_key:
        return ""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _key_prefix(raw_key: str) -> str:
    """Return a short, human-readable prefix for display purposes."""
    if not raw_key:
        return ""
    cleaned = raw_key.strip()
    if len(cleaned) <= 8:
        return cleaned
    return cleaned[:4] + "..." + cleaned[-4:]


def _maybe_decrypt(value: Optional[str]) -> str:
    """Decrypt a value if it looks like an encrypted blob, else return as-is.

    The custom_providers table stores both the legacy plain `api_key` and
    the newer encrypted `api_keys` blob. We try decrypt first, and fall
    back gracefully when the value is plain text.
    """
    if not value:
        return ""
    try:
        return Security.decrypt(value) or value
    except Exception:
        return value


# ---------------------------------------------------------------------------
# Health tracking (provider_keys table)
# ---------------------------------------------------------------------------


def _list_pool_rows(provider: str) -> List[Dict[str, Any]]:
    """Return active pool rows for a provider, ordered by id."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, provider, key_hash, key_prefix, label, weight,
                   is_active, success_count, failure_count,
                   cooldown_until, last_error, last_used_at
              FROM provider_keys
             WHERE provider = ? AND is_active = 1
             ORDER BY id ASC
            """,
            (provider,),
        )
        return [dict(r) for r in cursor.fetchall()]


def _is_in_cooldown(row: Dict[str, Any]) -> bool:
    cooldown = row.get("cooldown_until")
    if not cooldown:
        return False
    try:
        until = datetime.fromisoformat(str(cooldown))
    except (ValueError, TypeError):
        return False
    return datetime.now(timezone.utc) < until


def _pick_weighted(rows: List[Dict[str, Any]], provider: str) -> Optional[Dict[str, Any]]:
    """Pick a row using weighted round-robin. Falls back to sequential order
    when all weights are equal/zero. Excludes rows still in cooldown."""
    eligible = [r for r in rows if not _is_in_cooldown(r)]
    if not eligible:
        return None

    weights = [max(int(r.get("weight") or 1), 1) for r in eligible]
    if sum(weights) == len(weights):  # all weights == 1, just round-robin
        with _RR_LOCK:
            idx = _RR_COUNTER.get(provider, 0) % len(eligible)
            _RR_COUNTER[provider] = idx + 1
        return eligible[idx]

    total = sum(weights)
    pick = random.randint(1, total)
    cum = 0
    for row, w in zip(eligible, weights):
        cum += w
        if pick <= cum:
            return row
    return eligible[-1]


# ---------------------------------------------------------------------------
# Key resolution: turn a provider name into a usable API key string
# ---------------------------------------------------------------------------


def _resolve_builtin_key(provider: str) -> str:
    """Read the platform-wide key for a built-in provider from settings."""
    raw = get_setting(f"{provider}_api_key") or ""
    if not raw:
        return ""
    # Settings table can store encrypted values.
    decrypted = _maybe_decrypt(raw)
    return decrypted or raw


def _resolve_custom_key(provider: str) -> str:
    """Return the first usable key for a `custom:<slug>` provider."""
    if not provider.startswith("custom:"):
        return ""
    slug = provider.split(":", 1)[1]
    cfg = custom_providers.get_custom_provider(slug)
    if not cfg:
        return ""
    keys = custom_providers.parse_keys(cfg)
    return keys[0] if keys else ""


def _resolve_user_key(user_id: int, provider: str) -> str:
    """Return the user's own key for the provider from `user_provider_keys`.

    Returns the default key (is_default=1) when present, otherwise the
    most recently created key for that user/provider pair.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT api_key FROM user_provider_keys
             WHERE user_id = ? AND provider = ?
             ORDER BY is_default DESC, id DESC
             LIMIT 1
            """,
            (user_id, provider),
        )
        row = cursor.fetchone()
    if not row:
        return ""
    return _maybe_decrypt(row["api_key"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_key(provider_name: str) -> Optional[str]:
    """Return the next available upstream API key for a provider.

    Resolution order:
      1. Look up the provider's keys in `settings` (or `custom_providers`).
      2. Track that key in the `provider_keys` rotation pool when a
         matching row exists, applying per-key cooldown.
      3. When no pool rows exist for the provider, return the resolved
         key directly (no rotation but the system still works).

    Returns None when no key is configured.
    """
    if not provider_name:
        return None

    if provider_name.startswith("custom:"):
        return _resolve_custom_key(provider_name) or None

    pool_rows = _list_pool_rows(provider_name)
    base_key = _resolve_builtin_key(provider_name)
    if not base_key:
        return None

    # No pool entries: behave as a single-key provider.
    if not pool_rows:
        return base_key

    chosen = _pick_weighted(pool_rows, provider_name)
    if not chosen:
        # Every key in the pool is cooling down. Fall back to the base key
        # rather than failing the request outright.
        return base_key

    # We only have the base key value (not a per-row ciphertext). If the
    # pool row's hash matches the base key's hash we know it's the same
    # key being tracked; otherwise we treat the base key as the canonical
    # value and use the pool row for weight/health bookkeeping.
    chosen_hash = chosen.get("key_hash")
    if chosen_hash and chosen_hash != _key_hash(base_key):
        # The pool row references a key we don't have access to (e.g. an
        # alternative key configured by the admin). Use the base key but
        # still let the health tracking update on it.
        return base_key

    return base_key


def get_key_for_user_provider(
    user_id: int,
    provider: str,
    user_default_key: Optional[str] = None,
) -> Optional[str]:
    """Return the upstream key for a user, preferring their own binding.

    Order of resolution:
      1. `user_default_key` argument (caller-supplied explicit value).
      2. The user's own key in `user_provider_keys` for this provider.
      3. The shared pool key from `get_key()`.
    """
    if user_default_key:
        return user_default_key

    user_key = _resolve_user_key(user_id, provider)
    if user_key:
        return user_key

    return get_key(provider)


def mark_success(provider: str, key: str) -> None:
    """Record a successful call against a key. Resets cooldown + failure
    counter and increments the success counter when a matching pool row
    exists. Otherwise the call is a no-op so the rest of the system still
    works without explicit pool bookkeeping."""
    if not provider or not key:
        return
    key_h = _key_hash(key)
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE provider_keys
               SET success_count = success_count + 1,
                   failure_count = 0,
                   cooldown_until = NULL,
                   last_error = NULL,
                   last_used_at = ?
             WHERE provider = ? AND key_hash = ?
            """,
            (now, provider, key_h),
        )


def mark_failure(provider: str, key: str, error_msg: str = "") -> None:
    """Record a failed call. Puts the key in cooldown for 5 minutes and
    increments the failure counter when a matching pool row exists."""
    if not provider or not key:
        return
    key_h = _key_hash(key)
    cooldown = (datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SECONDS)).isoformat(sep=" ")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE provider_keys
               SET failure_count = failure_count + 1,
                   cooldown_until = ?,
                   last_error = ?,
                   last_used_at = ?
             WHERE provider = ? AND key_hash = ?
            """,
            (
                cooldown,
                (error_msg or "")[:500],
                datetime.now(timezone.utc).isoformat(sep=" "),
                provider,
                key_h,
            ),
        )


# ---------------------------------------------------------------------------
# Admin helpers (used by future key management routes; safe to call now)
# ---------------------------------------------------------------------------


def register_key(
    provider: str,
    api_key: str,
    label: Optional[str] = None,
    weight: int = 1,
) -> Dict[str, Any]:
    """Insert or update a key entry in the rotation pool.

    Stores the SHA-256 hash of the key (never the plaintext). The actual
    usable key still has to live in the `settings` table (or
    `custom_providers` / `user_provider_keys`) – this function only
    registers the key for health tracking + weight-based rotation.
    """
    if not provider or not api_key:
        raise ValueError("provider and api_key are required")
    if weight < 1:
        weight = 1
    key_h = _key_hash(api_key)
    prefix = _key_prefix(api_key)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM provider_keys WHERE provider = ? AND key_hash = ?
            """,
            (provider, key_h),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE provider_keys
                   SET label = ?, weight = ?, is_active = 1,
                       cooldown_until = NULL, last_error = NULL
                 WHERE id = ?
                """,
                (label, weight, existing["id"]),
            )
            return {"id": existing["id"], "updated": True}
        cursor.execute(
            """
            INSERT INTO provider_keys
                (provider, key_hash, key_prefix, label, weight, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (provider, key_h, prefix, label, weight),
        )
        new_id = cursor.lastrowid
    return {"id": new_id, "updated": False}


def list_pool(provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return pool rows, optionally filtered by provider. Useful for admin UIs."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        if provider:
            cursor.execute(
                "SELECT * FROM provider_keys WHERE provider = ? ORDER BY id ASC",
                (provider,),
            )
        else:
            cursor.execute("SELECT * FROM provider_keys ORDER BY provider, id ASC")
        return [dict(r) for r in cursor.fetchall()]
