"""User-defined model pool service.

Lets a user plug in **their own** upstream credentials (API key + base
URL + model name) and expose them through a single ``sk-ump_…`` key on
the OpenAI-compatible gateway.  The platform never sees the user's
upstream credentials in cleartext after the create/update call: every
row in ``user_model_pools`` stores ``api_base`` and ``api_key`` encrypted
via :class:`backend.security.Security`.

The platform's own quota / billing pipeline is bypassed entirely for
``sk-ump_`` requests — the request is forwarded to the user's upstream
with the user's own key, so the platform does not pay for it and the
user's wallet is not charged.  The only bookkeeping we keep is
``used_tokens`` per pool entry (for the user's own visibility) and a
cooldown flag (so we can fail over to the next pool entry when one
upstream returns 429 / 402).

The selection policy (delegated to :func:`database.get_next_model_pool`)
is:

1. Order by ``priority`` ascending (lower = earlier).
2. Skip entries whose ``cooldown_until`` is in the future.
3. Skip entries whose ``used_tokens >= max_tokens`` (when ``max_tokens``
   is configured and > 0).
4. Take the first match.  If none match, fall back to a random active
   entry so the user is not hard-blocked when every pool has hit its
   token ceiling (they pay their own upstream either way).

This module is a thin orchestration layer over the DB helpers in
:mod:`backend.database`.  It owns:

* URL validation (SSRF guard) before persisting.
* Encryption of ``api_base`` / ``api_key`` before calling the DB layer.
* SHA-256 hashing + ``sk-ump_`` prefix for the unified key surface.
* The ``record_usage`` success/failure bookkeeping (increment tokens +
  arm/clear cooldown).
* ``select_fallback_model`` (random active, optionally excluding one),
  which the DB layer does not expose directly.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import socket
import urllib.parse
from typing import Any, Dict, List, Optional

import ipaddress

from backend.database import (
    create_model_pool_key,
    create_user_model_pool,
    delete_model_pool_key,
    delete_user_model_pool,
    get_model_pool_key_by_hash,
    get_next_model_pool,
    get_user_model_pool,
    get_user_model_pool_keys,
    get_user_model_pools,
    increment_model_pool_usage,
    reorder_user_model_pools,
    set_model_pool_cooldown,
    touch_model_pool_key,
    update_user_model_pool,
)
from backend.security import Security

logger = logging.getLogger(__name__)


# Cooldown applied to a pool entry when its upstream returns 429 / 402
# or the connection outright fails.  Short enough that a transient rate
# limit recovers within minutes, long enough that a flaky upstream does
# not get retried on every single request.
DEFAULT_COOLDOWN_SECONDS = 60


# ---------------------------------------------------------------------------
# URL validation (SSRF guard)
# ---------------------------------------------------------------------------


_BLOCKED_HOSTNAMES = frozenset(
    {
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "metadata.google.internal",
        "169.254.169.254",
    }
)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
    ipaddress.ip_network("::ffff:10.0.0.0/104"),
    ipaddress.ip_network("::ffff:172.16.0.0/108"),
    ipaddress.ip_network("::ffff:192.168.0.0/112"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
]


def _validate_api_base(url: str) -> str:
    """Validate that *url* is a safe, publicly-routable HTTP(S) endpoint.

    Mirrors :func:`backend.services.custom_providers._validate_provider_url`
    but kept local so this module has no cross-service coupling.  Returns
    the normalised URL (trailing slash stripped) on success and raises
    ``ValueError`` with a Chinese message on failure.
    """
    if not url or not isinstance(url, str):
        raise ValueError("API Base URL 不能为空")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("API Base URL 缺少主机名")

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"不允许使用内部地址: {hostname}")

    if hostname in ("localhost", "ip6-localhost"):
        raise ValueError(f"不允许使用内部地址: {hostname}")

    try:
        infos = socket.getaddrinfo(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError(f"无法解析主机名: {hostname} ({exc})")
    except OSError as exc:
        raise ValueError(f"DNS 解析失败: {hostname} ({exc})")

    if not infos:
        raise ValueError(f"主机名无法解析到任何地址: {hostname}")

    for _family, _type, _proto, _canonname, sockaddr in infos:
        raw_ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        for net in _PRIVATE_NETWORKS:
            if addr in net:
                raise ValueError(
                    f"不允许使用内部/私有地址: {hostname} 解析到 {raw_ip}"
                )

    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ModelPoolService:
    """CRUD + dispatch for user-defined model pools.

    All persistence is delegated to the helpers in
    :mod:`backend.database`; this class owns validation, encryption, and
    the dispatch bookkeeping that doesn't have a DB-layer equivalent.
    """

    # ------------------------------------------------------------------ CRUD

    @staticmethod
    def create_pool(
        user_id: int,
        name: str,
        provider_type: str,
        api_base: str,
        api_key: str,
        model_name: str,
        priority: int = 0,
        max_tokens: int = 0,
    ) -> int:
        """Create a new pool entry. Returns the new row id.

        ``api_base`` and ``api_key`` are encrypted at rest.  Validates the
        URL is publicly routable before persisting so a misconfigured
        (or malicious) entry cannot be used to pivot to internal
        services via the proxy path.
        """
        if not name or not name.strip():
            raise ValueError("名称不能为空")
        if not provider_type or not provider_type.strip():
            raise ValueError("provider_type 不能为空")
        if not model_name or not model_name.strip():
            raise ValueError("model_name 不能为空")
        if not api_key or not api_key.strip():
            raise ValueError("API Key 不能为空")
        normalised_base = _validate_api_base(api_base)
        encrypted_base = Security.encrypt(normalised_base)
        encrypted_key = Security.encrypt(api_key)
        return create_user_model_pool(
            user_id=int(user_id),
            name=name.strip(),
            provider_type=provider_type.strip(),
            api_base_encrypted=encrypted_base,
            api_key_encrypted=encrypted_key,
            model_name=model_name.strip(),
            priority=int(priority or 0),
            max_tokens=int(max_tokens or 0),
        )

    @staticmethod
    def list_pools(user_id: int) -> List[Dict[str, Any]]:
        """Return all pool entries for *user_id*, decrypted for the owner.

        The owner sees their own ``api_base`` / ``api_key`` in cleartext
        so they can verify the configuration; admins never get here
        because the route requires ``require_user_session``.
        """
        return get_user_model_pools(int(user_id))

    @staticmethod
    def get_pool(pool_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        return get_user_model_pool(int(pool_id), int(user_id))

    @staticmethod
    def update_pool(pool_id: int, user_id: int, **fields: Any) -> bool:
        """Update a pool entry. Re-encrypts api_base / api_key when present.

        Field names match the route schema (``api_base`` / ``api_key`` in
        cleartext); they are encrypted here and mapped to the DB column
        names (``api_base`` / ``api_key_encrypted``) before delegating to
        :func:`database.update_user_model_pool`.
        """
        allowed = {
            "name",
            "provider_type",
            "api_base",
            "api_key",
            "model_name",
            "priority",
            "max_tokens",
            "is_active",
        }
        db_fields: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if value is None:
                continue
            if key == "api_base":
                normalised = _validate_api_base(str(value))
                # Column name in DB is ``api_base``; value is encrypted.
                db_fields["api_base"] = Security.encrypt(normalised)
            elif key == "api_key":
                db_fields["api_key_encrypted"] = Security.encrypt(str(value))
            elif key == "is_active":
                db_fields["is_active"] = 1 if value else 0
            elif key in ("priority", "max_tokens"):
                db_fields[key] = int(value)
            else:
                db_fields[key] = str(value).strip() if isinstance(value, str) else value

        if not db_fields:
            return False

        return update_user_model_pool(int(pool_id), int(user_id), **db_fields)

    @staticmethod
    def delete_pool(pool_id: int, user_id: int) -> bool:
        return delete_user_model_pool(int(pool_id), int(user_id))

    @staticmethod
    def reorder_pools(user_id: int, ordered_ids: List[int]) -> int:
        """Re-assign ``priority`` so the supplied ids are visited in order.

        Returns the number of rows the DB layer reported as updated.
        ``reorder_user_model_pools`` returns ``None`` (it commits a
        best-effort transaction), so we re-derive a count from the
        supplied list for the API envelope.
        """
        if not ordered_ids:
            return 0
        reorder_user_model_pools(int(user_id), [int(i) for i in ordered_ids])
        return len(ordered_ids)

    # ------------------------------------------------------------------ Keys

    @staticmethod
    def generate_key(user_id: int, name: Optional[str] = None) -> str:
        """Mint a new ``sk-ump_…`` key.  The cleartext is returned exactly
        once; only the SHA-256 hash + prefix survive in the DB."""
        raw = secrets.token_urlsafe(32)
        display_key = f"sk-ump_{raw}"
        key_hash = hashlib.sha256(display_key.encode("utf-8")).hexdigest()
        key_prefix = display_key[:16]
        label = (name or "").strip() or "default"
        create_model_pool_key(
            user_id=int(user_id),
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=label,
        )
        return display_key

    @staticmethod
    def list_keys(user_id: int) -> List[Dict[str, Any]]:
        return get_user_model_pool_keys(int(user_id))

    @staticmethod
    def delete_key(key_id: int, user_id: int) -> bool:
        return delete_model_pool_key(int(key_id), int(user_id))

    @staticmethod
    def resolve_key(display_key: str) -> Optional[int]:
        """Resolve a ``sk-ump_…`` cleartext to a user id.

        Returns ``None`` when the key is unknown or inactive.  On
        success, bumps ``last_used_at`` so the user can see when each
        key was last exercised.
        """
        if not display_key or not display_key.startswith("sk-ump_"):
            return None
        key_hash = hashlib.sha256(display_key.encode("utf-8")).hexdigest()
        record = get_model_pool_key_by_hash(key_hash)
        if not record:
            return None
        if not bool(record.get("is_active", 1)):
            return None
        try:
            touch_model_pool_key(int(record["id"]))
        except Exception:
            logger.warning(
                "touch_model_pool_key failed for id=%s",
                record.get("id"),
                exc_info=True,
            )
        return int(record["user_id"])

    # ------------------------------------------------------------------ Dispatch

    @staticmethod
    def get_next_model_for_user(user_id: int) -> Optional[Dict[str, Any]]:
        """Pick the next pool entry to use, in priority order.

        Delegates to :func:`database.get_next_model_pool`, which already
        implements the priority + cooldown + max_tokens gate and the
        "all exhausted → random active" fallback.
        """
        return get_next_model_pool(int(user_id))

    @staticmethod
    def select_fallback_model(
        user_id: int, exclude_pool_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Pick a random active pool entry, excluding *exclude_pool_id*.

        Used by the dispatch loop in ``openai_compat.py`` when the
        primary pick has just failed and we want to try a different
        entry without re-walking the priority list (which would return
        the same cooled-down entry).
        """
        import random

        pools = get_user_model_pools(int(user_id))
        if exclude_pool_id is not None:
            pools = [p for p in pools if int(p.get("id") or 0) != int(exclude_pool_id)]
        if not pools:
            return None
        return random.choice(pools)

    @staticmethod
    def record_usage(
        pool_id: int,
        tokens_used: int,
        success: bool = True,
        error_msg: Optional[str] = None,
    ) -> None:
        """Record token usage and optionally arm the cooldown.

        On success, clears any stale cooldown + last_error so the entry
        is eligible for the next pick.  On failure, arms the cooldown
        (default 60 s) and stores the truncated error message.
        """
        if int(tokens_used or 0) > 0:
            try:
                increment_model_pool_usage(int(pool_id), int(tokens_used))
            except Exception:
                logger.warning(
                    "increment_model_pool_usage failed for pool=%s",
                    pool_id,
                    exc_info=True,
                )
        if success:
            # ``update_user_model_pool`` does not allow ``cooldown_until``
            # / ``last_error`` in its allow-list, so we clear them with a
            # direct UPDATE.  The pool_id is already known to be valid
            # (the dispatch loop just fetched it), so a direct UPDATE by
            # id is safe — the caller is the user who owns the pool.
            from backend.database import get_db_context

            try:
                with get_db_context() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE user_model_pools SET cooldown_until = NULL, "
                        "last_error = NULL, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (int(pool_id),),
                    )
                    conn.commit()
            except Exception:
                logger.warning(
                    "clear_model_pool_cooldown failed for pool=%s",
                    pool_id,
                    exc_info=True,
                )
        else:
            try:
                set_model_pool_cooldown(
                    int(pool_id),
                    DEFAULT_COOLDOWN_SECONDS,
                    (error_msg or "")[:500],
                )
            except Exception:
                logger.warning(
                    "set_model_pool_cooldown failed for pool=%s",
                    pool_id,
                    exc_info=True,
                )

    @staticmethod
    def reset_cooldown(pool_id: int, user_id: int) -> bool:
        """Manually clear the cooldown / last_error on a pool entry.

        Used by the user-facing UI when they want to retry a flaky
        upstream before the 60-second cooldown elapses.
        """
        # ``update_user_model_pool`` does not allow ``cooldown_until`` /
        # ``last_error`` in its allow-list, so we use a direct UPDATE
        # that respects ownership (``user_id`` in the WHERE clause).
        from backend.database import get_db_context

        try:
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE user_model_pools SET cooldown_until = NULL, "
                    "last_error = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND user_id = ?",
                    (int(pool_id), int(user_id)),
                )
                affected = cursor.rowcount
                conn.commit()
            return affected > 0
        except Exception:
            logger.warning(
                "reset_cooldown failed for pool=%s user=%s",
                pool_id,
                user_id,
                exc_info=True,
            )
            return False
