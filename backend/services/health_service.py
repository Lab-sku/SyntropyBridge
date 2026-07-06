from __future__ import annotations

"""Provider health monitoring.

We keep a tiny in-memory sliding window (per provider) for latency +
recent success/error events. Aggregated counters are also persisted to
the ``provider_health`` table so they survive process restarts and
surface in admin queries.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from backend.database import get_db_context

logger = logging.getLogger(__name__)

_WINDOW = 100
_ERROR_WINDOW_MINUTES = 10
_HOUR_SECONDS = 3600

_provider_previous_state: Dict[str, bool] = {}
_provider_state_lock = threading.Lock()


class _ProviderStats:
    """Per-provider in-memory state."""

    __slots__ = ("latencies", "events", "lock")

    def __init__(self) -> None:
        self.latencies: Deque[int] = deque(maxlen=_WINDOW)
        # (timestamp_seconds, success_bool)
        self.events: Deque[Tuple[float, bool]] = deque(maxlen=2000)
        # Reentrant so :func:`record_request` can call helpers that
        # also need to take the same lock.
        self.lock = threading.RLock()


_stats: Dict[str, _ProviderStats] = {}
_stats_lock = threading.Lock()


def _bucket(provider: str) -> _ProviderStats:
    with _stats_lock:
        bucket = _stats.get(provider)
        if bucket is None:
            bucket = _ProviderStats()
            _stats[provider] = bucket
        return bucket


def _percentile(values: List[int], pct: float) -> int:
    """Linear-interpolation percentile, integer rounded."""
    if not values:
        return 0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return int(sorted_vals[f])
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _trim_events(events: Deque[Tuple[float, bool]], cutoff: float) -> None:
    while events and events[0][0] < cutoff:
        events.popleft()


def _aggregate_last_hour(stats: _ProviderStats, now_ts: float) -> Tuple[int, int, float]:
    """Return ``(requests, errors, success_rate)`` for the last 60 minutes."""
    cutoff = now_ts - _HOUR_SECONDS
    with stats.lock:
        _trim_events(stats.events, cutoff)
        requests = len(stats.events)
        if requests == 0:
            return 0, 0, 1.0
        errors = sum(1 for _, ok in stats.events if not ok)
    success_rate = (requests - errors) / requests if requests else 1.0
    return requests, errors, success_rate


def _upsert_health(
    provider: str,
    latency_p50: int,
    latency_p95: int,
    requests_1h: int,
    errors_1h: int,
    success_rate: float,
    status: str,
) -> None:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO provider_health
                (provider, status, last_check, latency_p50, latency_p95,
                 success_rate_1h, requests_1h, errors_1h)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                status = excluded.status,
                last_check = CURRENT_TIMESTAMP,
                latency_p50 = excluded.latency_p50,
                latency_p95 = excluded.latency_p95,
                success_rate_1h = excluded.success_rate_1h,
                requests_1h = excluded.requests_1h,
                errors_1h = excluded.errors_1h
        """,
            (provider, status, latency_p50, latency_p95, success_rate, requests_1h, errors_1h),
        )


def _set_provider_keys_cooldown(provider: str, error_msg: Optional[str]) -> None:
    """Set 5-minute cooldown on every active key for a provider."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE provider_keys
            SET cooldown_until = datetime('now', '+5 minutes'),
                last_error = ?,
                failure_count = failure_count + 1
            WHERE provider = ? AND is_active = 1
        """,
            (error_msg, provider),
        )


def _clear_provider_keys_cooldown(provider: str) -> None:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE provider_keys
            SET cooldown_until = NULL,
                success_count = success_count + 1
            WHERE provider = ? AND is_active = 1
        """,
            (provider,),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_request(
    provider: str, latency_ms: int, success: bool, error_msg: Optional[str] = None
) -> None:
    """Record a single upstream call. Thread-safe.

    Note: this function used to also call ``_set_provider_keys_cooldown``
    / ``_clear_provider_keys_cooldown`` to flip every active
    ``provider_keys`` row into / out of cooldown. That is the key pool's
    responsibility — ``key_pool.mark_failure`` / ``mark_success`` already
    manage per-key cooldown on the actual key that was used. Cooldown-
    ing *all* keys for a provider on a single failure starves the pool
    the moment one upstream errors, and clearing cooldowns on a single
    success un-cooldowns keys that may still be failing. Both helpers
    are kept for callers that genuinely want a provider-wide sweep, but
    ``record_request`` no longer touches them.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    stats = _bucket(provider)
    with stats.lock:
        stats.latencies.append(int(latency_ms or 0))
        stats.events.append((now_ts, bool(success)))
        latencies = list(stats.latencies)
        requests, errors, success_rate = _aggregate_last_hour(stats, now_ts)

    p50 = _percentile(latencies, 0.5)
    p95 = _percentile(latencies, 0.95)
    status = "healthy" if success_rate >= 0.9 else "degraded"

    _upsert_health(provider, p50, p95, requests, errors, success_rate, status)

    is_up = check_provider_up(provider)
    with _provider_state_lock:
        prev = _provider_previous_state.get(provider, True)
        if prev and not is_up:
            _provider_previous_state[provider] = False
            try:
                from backend.services.alert_service import AlertService

                AlertService.send_alert_sync(
                    "CRITICAL",
                    f"Provider {provider} is DOWN",
                    {
                        "provider": provider,
                        "success_rate_1h": round(success_rate, 4),
                        "requests_1h": requests,
                        "errors_1h": errors,
                        "latency_p50": p50,
                        "latency_p95": p95,
                    },
                )
            except Exception:
                logger.exception("failed to send provider DOWN alert for %s", provider)
        elif not prev and is_up:
            _provider_previous_state[provider] = True


def get_provider_health(provider: str) -> Dict:
    """Read a single provider's health row."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM provider_health WHERE provider = ?
        """,
            (provider,),
        )
        row = cursor.fetchone()
    if not row:
        return {
            "provider": provider,
            "status": "unknown",
            "latency_p50": 0,
            "latency_p95": 0,
            "success_rate_1h": 1.0,
            "requests_1h": 0,
            "errors_1h": 0,
            "up": True,
        }
    result = dict(row)
    result["up"] = check_provider_up(provider)
    return result


def get_all_providers_health() -> List[Dict]:
    """List every provider with at least one health record."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM provider_health
            ORDER BY requests_1h DESC, provider ASC
        """)
        rows = cursor.fetchall()
    out: List[Dict] = []
    for row in rows:
        item = dict(row)
        item["up"] = check_provider_up(item["provider"])
        out.append(item)
    return out


def check_provider_up(provider: str) -> bool:
    """Decide whether a provider is up based on the last 10 minutes.

    A provider is considered DOWN when it has at least 5 events in the
    window and an error rate > 50%. Providers without enough samples
    are treated as UP (we don't want to flap a provider on cold start).
    """
    stats = _bucket(provider)
    cutoff = datetime.now(timezone.utc).timestamp() - (_ERROR_WINDOW_MINUTES * 60)
    with stats.lock:
        recent = [(t, ok) for t, ok in stats.events if t >= cutoff]
    if not recent:
        # Fall back to persisted counters when in-memory state is empty
        # (e.g. after a restart) — count rows in usage_logs over the
        # same window.
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) AS errors
                FROM usage_logs
                WHERE provider = ?
                  AND request_time > datetime('now', '-10 minutes')
            """,
                (provider,),
            )
            row = cursor.fetchone()
        total = int(row["total"] or 0)
        errors = int(row["errors"] or 0)
        if total < 5:
            return True
        return errors / total <= 0.5

    total = len(recent)
    errors = sum(1 for _, ok in recent if not ok)
    if total < 5:
        return True
    return errors / total <= 0.5
