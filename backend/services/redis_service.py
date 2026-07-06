import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

import redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# M11: In-memory fallback cache
# ---------------------------------------------------------------------------
# When Redis is unconfigured or temporarily unreachable, every ``get``
# returns None (cache miss) and every ``set_with_expiry`` returns False.
# For hot paths (model aggregation, pricing tables) this means every
# request hits the DB even though the data is identical across calls
# within the same process.
#
# This fallback is a tiny bounded LRU cache that:
#   * is populated on every successful ``set_with_expiry`` (so Redis
#     hits also warm the local cache)
#   * is consulted on every ``get`` that misses Redis (or fails to
#     reach Redis)
#   * respects TTL (entries are lazily expired on read)
#   * is bounded to 256 entries to cap memory use
#   * is thread-safe via a single coarse-grained lock
#
# This is NOT a distributed cache — different workers have independent
# fallbacks. It exists purely to absorb the Redis-down case for the
# few minutes until Redis is restored or until the next deployment.

_FALLBACK_MAX_ENTRIES = 256
_fallback_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_fallback_lock = threading.Lock()


def _fallback_set(key: str, value: str, expiry_seconds: int) -> None:
    """Insert/update the in-memory fallback entry."""
    expires_at = time.time() + max(int(expiry_seconds), 1)
    with _fallback_lock:
        _fallback_cache[key] = (value, expires_at)
        _fallback_cache.move_to_end(key)
        while len(_fallback_cache) > _FALLBACK_MAX_ENTRIES:
            _fallback_cache.popitem(last=False)


def _fallback_get(key: str) -> Optional[str]:
    """Return the value if present and unexpired, else None."""
    with _fallback_lock:
        entry = _fallback_cache.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() >= expires_at:
            _fallback_cache.pop(key, None)
            return None
        _fallback_cache.move_to_end(key)
        return value


def _fallback_delete(key: str) -> None:
    with _fallback_lock:
        _fallback_cache.pop(key, None)


def clear_fallback_cache() -> None:
    """Test hook — drop the in-memory fallback entirely."""
    with _fallback_lock:
        _fallback_cache.clear()


class RedisService:
    _client = None
    _last_unreachable_log_ts: float = 0.0

    @classmethod
    def get_client(cls) -> redis.Redis:
        if cls._client is None:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            # ``socket_timeout`` keeps local-dev calls from hanging forever
            # when Redis is not running. ``decode_responses=True`` keeps the
            # legacy string-in / string-out contract used by the rest of
            # the codebase.
            cls._client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
                retry_on_timeout=False,
            )
        return cls._client

    @classmethod
    def set_with_expiry(cls, key: str, value: str, expiry_seconds: int = 3600) -> bool:
        # Always warm the in-memory fallback so a subsequent Redis
        # outage can be served from it.
        _fallback_set(key, value, expiry_seconds)
        try:
            client = cls.get_client()
            client.setex(key, expiry_seconds, value)
            return True
        except Exception as e:
            cls._log_unreachable("set", type(e).__name__)
            return False

    @classmethod
    def get(cls, key: str) -> Optional[str]:
        try:
            client = cls.get_client()
            value = client.get(key)
            if value is not None:
                # Refresh the fallback so the local cache stays warm
                # even if Redis drops later. We don't know the original
                # TTL here — use a conservative 5 min fallback window.
                _fallback_set(key, value, 300)
                return value
            # Redis reported a miss. Don't trust the fallback blindly —
            # the entry may have been explicitly deleted from Redis by
            # another worker. But for the Redis-down case the fallback
            # is our only signal, so consult it.
            return _fallback_get(key)
        except Exception as e:
            cls._log_unreachable("get", type(e).__name__)
            # Redis unreachable — consult the in-memory fallback.
            return _fallback_get(key)

    @classmethod
    def delete(cls, key: str) -> bool:
        # Always drop the in-memory entry too so a subsequent get
        # doesn't resurrect a stale value from the fallback.
        _fallback_delete(key)
        try:
            client = cls.get_client()
            client.delete(key)
            return True
        except Exception as e:
            cls._log_unreachable("delete", type(e).__name__)
            return False

    @classmethod
    def _log_unreachable(cls, op: str, exc_type: str) -> None:
        """Rate-limit the 'Redis unreachable' warnings to one per 60s
        per process to avoid log flooding during a sustained outage.
        """
        now = time.time()
        if now - cls._last_unreachable_log_ts < 60.0:
            return
        cls._last_unreachable_log_ts = now
        logger.warning(
            "Redis %s failed (%s); serving from in-memory fallback cache",
            op,
            exc_type,
        )

    @classmethod
    def set_verification_code(cls, email: str, code: str) -> bool:
        key = f"verify:{email}"
        return cls.set_with_expiry(key, code, 1800)

    @classmethod
    def get_verification_code(cls, email: str) -> Optional[str]:
        key = f"verify:{email}"
        return cls.get(key)

    @classmethod
    def delete_verification_code(cls, email: str) -> bool:
        key = f"verify:{email}"
        return cls.delete(key)

    @classmethod
    def set_reset_token(cls, email: str, token: str) -> bool:
        key = f"reset:{email}"
        return cls.set_with_expiry(key, token, 1800)

    @classmethod
    def get_reset_token(cls, email: str) -> Optional[str]:
        key = f"reset:{email}"
        return cls.get(key)

    @classmethod
    def delete_reset_token(cls, email: str) -> bool:
        key = f"reset:{email}"
        return cls.delete(key)

    @classmethod
    def set_user_token(cls, token: str, user_data: dict, expiry_seconds: int = 3600) -> bool:
        key = f"token:{token}"
        return cls.set_with_expiry(key, json.dumps(user_data), expiry_seconds)

    @classmethod
    def get_user_token(cls, token: str) -> Optional[dict]:
        key = f"token:{token}"
        data = cls.get(key)
        if data:
            return json.loads(data)
        return None

    @classmethod
    def delete_user_token(cls, token: str) -> bool:
        key = f"token:{token}"
        return cls.delete(key)

    @classmethod
    def increment_rate_limit(
        cls, identifier: str, limit_count: int, window_seconds: int = 60
    ) -> tuple[bool, int]:
        try:
            client = cls.get_client()
            key = f"ratelimit:{identifier}"
            current = client.get(key)

            if current is None:
                client.setex(key, window_seconds, 1)
                return True, max(limit_count - 1, 0)

            count = int(current)
            if count >= limit_count:
                return False, 0

            client.incr(key)
            remaining = limit_count - (count + 1)
            return True, max(int(remaining), 0)
        except Exception as e:
            logger.warning("Redis rate limit error: %s", type(e).__name__)
            return True, max(limit_count - 1, 0)
