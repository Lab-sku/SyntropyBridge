"""API key lifecycle helpers.

The :class:`api_keys` table stores *hashed* secrets; this module is
the single place that knows how to mint, validate, and rotate them.
Keeping the logic centralised prevents the various route files from
drifting out of sync (which is exactly how a security hole sneaks in).

Design notes
------------
* The secret returned to the caller is the *only* time the plaintext
  value exists. The hashed form is what the rest of the system uses
  for lookups and comparisons.
* ``rotate_key`` issues a new secret while preserving the row's
  configuration (allowed_models, monthly limits, etc.). The previous
  secret is invalidated atomically.
* Rate-limit / brute-force: the public-facing login flow should call
  :func:`check_and_bump_failure` before allowing another attempt and
  call :func:`reset_failures` on a successful login.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.database import get_db_context

logger = logging.getLogger(__name__)

# Key format: ``sk-`` + 43 chars of url-safe random (≈256 bits of entropy).
KEY_PREFIX_LITERAL = "sk-"
KEY_RANDOM_BYTES = 32


def _normalise(secret: str) -> str:
    s = (secret or "").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def hash_key(secret: str) -> str:
    """Return the SHA-256 hex digest used as the storage primary key.

    Constant-time comparison is performed at the database layer (via
    the ``=`` operator against an indexed column); for *in-process*
    comparisons use :func:`secrets.compare_digest` on the hex digests.
    """
    s = _normalise(secret)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    """Mint a fresh, unguessable API key secret."""
    return KEY_PREFIX_LITERAL + secrets.token_urlsafe(KEY_RANDOM_BYTES)


def _mask(secret: str) -> str:
    s = _normalise(secret)
    if len(s) < 12:
        return "****"
    return f"{s[:4]}...{s[-4:]}"


def _prefix(secret: str) -> str:
    s = _normalise(secret)
    return s[:8]


@dataclass
class IssuedKey:
    id: int
    secret: str
    prefix: str
    mask: str


def issue_key(
    user_id: int,
    name: str,
    *,
    monthly_token_limit: Optional[int] = None,
    monthly_credit_limit: Optional[float] = None,
    allowed_models: Optional[List[str]] = None,
    expires_at: Optional[str] = None,
) -> IssuedKey:
    """Mint a fresh API key row for ``user_id``.

    Returns the *plaintext* secret exactly once via :class:`IssuedKey`.
    The hash is what's persisted.
    """
    import json as _json

    secret = generate_secret()
    digest = hash_key(secret)
    prefix = _prefix(secret)
    mask = _mask(secret)
    safe_name = (name or "").strip()[:100] or "default"
    allowed_json = _json.dumps(allowed_models) if allowed_models else None

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO api_keys
                (user_id, name, key_hash, key_prefix, key_mask,
                 monthly_token_limit, monthly_credit_limit,
                 allowed_models, is_active, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                int(user_id),
                safe_name,
                digest,
                prefix,
                mask,
                monthly_token_limit,
                monthly_credit_limit,
                allowed_json,
                expires_at,
            ),
        )
        new_id = int(cursor.lastrowid)
    return IssuedKey(id=new_id, secret=secret, prefix=prefix, mask=mask)


def rotate_key(key_id: int, user_id: int) -> IssuedKey:
    """Replace the secret for an existing key row.

    Preserves all configuration (allowed_models, limits, name). The
    old secret immediately stops authenticating.
    """
    secret = generate_secret()
    digest = hash_key(secret)
    prefix = _prefix(secret)
    mask = _mask(secret)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM api_keys WHERE id = ? AND user_id = ?",
            (int(key_id), int(user_id)),
        )
        if not cursor.fetchone():
            raise ValueError("API Key 不存在或不属于当前用户")
        cursor.execute(
            """
            UPDATE api_keys
               SET key_hash = ?, key_prefix = ?, key_mask = ?,
                   is_active = 1, last_used_at = NULL
             WHERE id = ? AND user_id = ?
            """,
            (digest, prefix, mask, int(key_id), int(user_id)),
        )
    return IssuedKey(id=int(key_id), secret=secret, prefix=prefix, mask=mask)


def revoke_key(key_id: int, user_id: Optional[int] = None) -> bool:
    """Soft-revoke a key by flipping ``is_active`` to 0.

    When ``user_id`` is given, the key must belong to that user; this
    prevents user-facing routes from revoking other users' keys via
    an ID guess.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        if user_id is not None:
            cursor.execute(
                "UPDATE api_keys SET is_active = 0 WHERE id = ? AND user_id = ?",
                (int(key_id), int(user_id)),
            )
        else:
            cursor.execute(
                "UPDATE api_keys SET is_active = 0 WHERE id = ?",
                (int(key_id),),
            )
        return cursor.rowcount > 0


def lookup_by_hash(digest: str) -> Optional[Dict[str, Any]]:
    """Return the row for a given key digest, or None."""
    if not digest:
        return None
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, name, key_prefix, key_mask, is_active,
                   expires_at, allowed_models, denied_models,
                   monthly_token_limit, monthly_credit_limit,
                   last_used_at, created_at
              FROM api_keys
             WHERE key_hash = ? AND is_active = 1
            """,
            (digest,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def mask_for_display(secret: str) -> str:
    """Public helper used by the admin console to mask a secret in UIs."""
    return _mask(secret)


def verify_secret(plaintext: str, expected_digest: str) -> bool:
    """Constant-time comparison of a plaintext secret against a digest."""
    if not plaintext or not expected_digest:
        return False
    actual = hash_key(plaintext)
    return secrets.compare_digest(actual, expected_digest or "")


__all__ = [
    "IssuedKey",
    "issue_key",
    "rotate_key",
    "revoke_key",
    "lookup_by_hash",
    "hash_key",
    "verify_secret",
    "generate_secret",
    "mask_for_display",
]
