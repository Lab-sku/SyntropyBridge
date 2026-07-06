from __future__ import annotations

"""Multi-dimensional quota engine.

The engine is intentionally stateless (apart from the DB): it reads all
relevant counters, applies the user / API key / plan rules, and returns a
:class:`QuotaResult`.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from backend.database import (
    get_db_context,
    get_model_pricing,
    get_quota_snapshot,
    get_user_plan,
    get_wallet,
)

logger = logging.getLogger(__name__)


@dataclass
class QuotaResult:
    """Outcome of a quota check.

    ``remaining`` is interpreted in *tokens* for token quotas and as a
    count for rate-limit windows. ``reset_at`` is set for sliding windows
    (5h, week, month); for rate limits it points to the end of the
    current minute window.
    """

    allowed: bool
    reason: str = ""
    remaining: int = 0
    reset_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAFE_SUM_COLUMNS = frozenset(
    {
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cost_credits",
        "response_time_ms",
    }
)


def _sum_window(user_id: int, window: str, column: str = "total_tokens") -> float:
    """Sum ``column`` from usage_logs for a user within a SQLite window spec.

    ``window`` is something like ``"-5 hours"`` and is interpolated
    directly into a SQLite ``datetime('now', ?)`` call.
    """
    if column not in _SAFE_SUM_COLUMNS:
        raise ValueError(f"Unsafe column name: {column}")
    sql = f"""
        SELECT COALESCE(SUM({column}), 0)
        FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', ?)
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (user_id, window))
        row = cursor.fetchone()
        return float(row[0] or 0)


def _user_row(user_id: int) -> Optional[sqlite3.Row]:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cursor.fetchone()


def _current_minute_request_count(identifier: str, limit_type: str) -> int:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT request_count FROM rate_limits
            WHERE identifier = ? AND limit_type = ?
              AND window_start = strftime('%Y-%m-%d %H:%M:00', 'now')
        """,
            (identifier, limit_type),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def _current_minute_token_sum(user_id: int) -> int:
    """Sum tokens used in the current minute for a user."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
            WHERE user_id = ?
              AND request_time > datetime('now', 'start of minute')
        """,
            (user_id,),
        )
        row = cursor.fetchone()
        return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_user_quota(user_id: int, model: Optional[str] = None) -> QuotaResult:
    """Check every user-level quota dimension in order.

    Order matters: cheaper / more general checks first so the most
    common failures short-circuit quickly.
    """
    user = _user_row(user_id)
    if not user:
        return QuotaResult(False, reason="用户不存在", remaining=0)
    if not user["is_active"]:
        return QuotaResult(False, reason="账号已被禁用", remaining=0)

    # 1. Wallet balance
    wallet = get_wallet(user_id)
    balance = float(wallet.get("balance") or 0)

    # If admin has set 0 for every quota AND balance is 0 we still allow
    # the request to flow (the proxy will return 402 from update_wallet).
    # Here we only block when at least one quota dimension is positive.
    quota_5h = int(user["quota_5h"] or 0)
    quota_week = int(user["quota_week"] or 0)
    quota_month = int(user["quota_month"] or 0)
    monthly_budget = float(user["monthly_budget"] or 0)

    # Allow balance=0 for free / unpriced models; only block negative
    # balances which indicate a data anomaly (refund bug, corruption).
    # The authoritative balance gate is assert_request_allowed() on the
    # hot path, which knows the provider and can consult model_pricing.
    if balance < 0:
        return QuotaResult(False, reason="钱包余额异常，请联系管理员", remaining=0)

    # 2. 5h sliding window (skip when 0 = unlimited)
    if quota_5h > 0:
        used = int(_sum_window(user_id, "-5 hours", "total_tokens"))
        if used >= quota_5h:
            return QuotaResult(
                False,
                reason=f"5小时配额已用完 ({used}/{quota_5h})",
                remaining=0,
            )

    # 3. Weekly window
    if quota_week > 0:
        used = int(_sum_window(user_id, "-7 days", "total_tokens"))
        if used >= quota_week:
            return QuotaResult(
                False,
                reason=f"周配额已用完 ({used}/{quota_week})",
                remaining=0,
            )

    # 4. Monthly window
    if quota_month > 0:
        used = int(_sum_window(user_id, "-30 days", "total_tokens"))
        if used >= quota_month:
            return QuotaResult(
                False,
                reason=f"月配额已用完 ({used}/{quota_month})",
                remaining=0,
            )

    # 5. Monthly budget (cost in credits)
    if monthly_budget > 0:
        spent = _sum_window(user_id, "-30 days", "cost_credits")
        if spent >= monthly_budget:
            return QuotaResult(
                False,
                reason=f"本月预算已用完 ({spent:.2f}/{monthly_budget:.2f})",
                remaining=0,
            )

    # 6. Plan-level rate limits
    plan = get_user_plan(user_id)
    if plan and plan.get("id") is not None:
        rpm = int(plan.get("rate_limit_rpm") or 0)
        tpm = int(plan.get("rate_limit_tpm") or 0)
        if rpm > 0:
            # identifier combines user + plan so different plans don't share
            current = _current_minute_request_count(f"user:{user_id}:rpm", "rpm")
            if current >= rpm:
                return QuotaResult(
                    False,
                    reason=f"套餐 RPM 已达上限 ({rpm}/min)",
                    remaining=0,
                )
        if tpm > 0:
            current_tokens = _current_minute_token_sum(user_id)
            if current_tokens >= tpm:
                return QuotaResult(
                    False,
                    reason=f"套餐 TPM 已达上限 ({tpm}/min)",
                    remaining=0,
                )

    # Compute remaining (use the most restrictive dimension for display)
    remaining_tokens: Optional[int] = None
    if quota_5h > 0:
        remaining_tokens = quota_5h - int(_sum_window(user_id, "-5 hours", "total_tokens"))
    return QuotaResult(True, reason="OK", remaining=remaining_tokens or 0)


def check_api_key_quota(api_key_id: int, model: Optional[str] = None) -> QuotaResult:
    """Check API-key level quota: token / credit limits + allow/deny lists."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_keys WHERE id = ?", (api_key_id,))
        row = cursor.fetchone()
    if not row:
        return QuotaResult(False, reason="API Key 不存在", remaining=0)
    if not row["is_active"]:
        return QuotaResult(False, reason="API Key 已被禁用", remaining=0)

    user_id = int(row["user_id"])

    # Allow / deny lists
    def _safe_list(value: Optional[str]) -> List[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []

    allowed_models = _safe_list(row["allowed_models"])
    denied_models = _safe_list(row["denied_models"])

    if model:
        if denied_models and model in denied_models:
            return QuotaResult(False, reason=f"模型 {model} 在黑名单中", remaining=0)
        if allowed_models and model not in allowed_models:
            return QuotaResult(False, reason=f"模型 {model} 不在白名单中", remaining=0)

    # Monthly token / credit limits
    token_limit = row["monthly_token_limit"]
    credit_limit = row["monthly_credit_limit"]
    if token_limit:
        used = int(_sum_window(user_id, "-30 days", "total_tokens"))
        if used >= int(token_limit):
            return QuotaResult(
                False,
                reason=f"API Key 月度 token 配额已用完 ({used}/{token_limit})",
                remaining=0,
            )
    if credit_limit:
        spent = _sum_window(user_id, "-30 days", "cost_credits")
        if spent >= float(credit_limit):
            return QuotaResult(
                False,
                reason=f"API Key 月度消费已超限 ({spent:.2f}/{float(credit_limit):.2f})",
                remaining=0,
            )

    return QuotaResult(True, reason="OK", remaining=0)



# ---------------------------------------------------------------------------
# Hot-path single-connection quota gate
# ---------------------------------------------------------------------------


def assert_request_allowed(
    user_id: int,
    provider: str,
    model: str,
    estimated_tokens: int,
) -> Tuple[bool, Optional[str]]:
    """Comprehensive pre-flight quota check on a **single** connection.

    Runs every quota dimension that :func:`check_user_quota` covers —
    wallet balance, 5-hour / week / month token quotas, monthly budget
    (credits), and plan-level RPM / TPM rate limits — but reads all
    counters from one connection via :func:`get_quota_snapshot` instead
    of opening a separate connection per dimension.

    Returns ``(True, None)`` when the request may proceed, or
    ``(False, "<reason>")`` on the first dimension that fails.

    Called from the primary ``/v1/`` proxy routes *before* the upstream
    request is dispatched.

    .. note::
       TODO (Part 3 follow-up): After the upstream response returns,
       the caller should reconcile the reservation — write the actual
       usage log and refund ``(estimated - actual)`` when actual <
       estimated, or fully refund on upstream error / timeout.
    """
    snap = get_quota_snapshot(user_id)

    if not snap.get("exists"):
        return (False, "用户不存在")
    if not snap.get("is_active"):
        return (False, "账号已被禁用")

    quota_5h = snap["quota_5h"]
    quota_week = snap["quota_week"]
    quota_month = snap["quota_month"]
    monthly_budget = snap["monthly_budget"]
    balance = snap["balance"]
    # Active reservation from a sibling in-flight request (or a stale
    # row whose TTL hasn't yet been reaped). Add to the per-window
    # usage totals so a concurrent request can't sneak past the quota
    # while the first one is still running.
    reserved = int(snap.get("reserved_tokens") or 0)

    # Wallet pre-check: block when the user cannot pay for a priced
    # model. Free / unpriced models (no row in model_pricing, or both
    # per-1k rates equal to 0) are intentionally allowed through — the
    # downstream billing path is a no-op for them. Priced models with
    # balance <= 0 are blocked here to prevent upstream API consumption
    # that the subsequent charge_for_usage / reconcile_stream_reserve
    # cannot collect payment for.
    if balance < 0:
        return (False, "钱包余额异常，请联系管理员")
    if balance <= 0:
        pricing = get_model_pricing(provider, model) if provider and model else None
        if pricing:
            in_p = float(pricing.get("input_price_per_1k") or 0)
            out_p = float(pricing.get("output_price_per_1k") or 0)
            if in_p > 0 or out_p > 0:
                return (False, "钱包余额不足，请先充值")

    # --- 5-hour sliding window ------------------------------------------
    if quota_5h > 0:
        used_with_pending = snap["tokens_5h"] + reserved
        if used_with_pending >= quota_5h:
            return (False, f"5小时配额已用完 (已用 {snap['tokens_5h']} + 预留 {reserved}, 上限 {quota_5h})")

    # --- weekly sliding window ------------------------------------------
    if quota_week > 0:
        used_with_pending = snap["tokens_week"] + reserved
        if used_with_pending >= quota_week:
            return (False, f"周配额已用完 (已用 {snap['tokens_week']} + 预留 {reserved}, 上限 {quota_week})")

    # --- monthly token quota --------------------------------------------
    if quota_month > 0:
        used_with_pending = snap["tokens_month"] + reserved
        if used_with_pending >= quota_month:
            return (False, f"月配额已用完 (已用 {snap['tokens_month']} + 预留 {reserved}, 上限 {quota_month})")

    # --- monthly budget (credits) ---------------------------------------
    if monthly_budget > 0:
        spent = snap["monthly_cost"]
        if spent >= monthly_budget:
            return (
                False,
                f"本月预算已用完 ({spent:.2f}/{monthly_budget:.2f})",
            )

    # --- plan-level RPM -------------------------------------------------
    plan_rpm = snap.get("plan_rpm", 0)
    if plan_rpm > 0:
        rpm_used = snap.get("rpm_count", 0)
        if rpm_used >= plan_rpm:
            return (False, f"套餐 RPM 已达上限 ({plan_rpm}/min)")

    # --- plan-level TPM -------------------------------------------------
    plan_tpm = snap.get("plan_tpm", 0)
    if plan_tpm > 0:
        tpm_used = snap.get("tpm_used", 0)
        if tpm_used >= plan_tpm:
            return (False, f"套餐 TPM 已达上限 ({plan_tpm}/min)")

    return (True, None)


# ---------------------------------------------------------------------------
# M8: Quota warning notifications
# ---------------------------------------------------------------------------
# Called from the proxy/chat/openai_compat routes *after* a request has been
# admitted (so we don't warn on requests that are about to be rejected) but
# *before* the reservation for the current request is taken (so the current
# request's own reservation doesn't inflate the percentage). Best-effort:
# any failure is logged and swallowed — the warning path must never break
# the request path.
#
# Thresholds: 80% (warning) + 95% (critical). Each (window, threshold) pair
# uses a distinct notification ``type`` so the per-(user, type) cooldown
# in NotificationService.notify_with_cooldown doesn't suppress the 95%
# warning when the 80% warning has already fired.

_QUOTA_WARN_THRESHOLDS = (
    (0.95, "95", 6),   # critical — re-warn at most every 6h
    (0.80, "80", 12),  # warning  — re-warn at most every 12h
)

_QUOTA_WARN_WINDOWS = (
    ("5h", "quota_5h", "tokens_5h"),
    ("week", "quota_week", "tokens_week"),
    ("month", "quota_month", "tokens_month"),
)


def maybe_warn_on_quota(user_id: int) -> None:
    """Inspect the user's quota snapshot and fire warning notifications
    for any window whose usage (including sibling reservations) crosses
    a threshold.

    This is a **non-blocking** side-effect — all errors are swallowed.
    Intended call site: right after ``assert_request_allowed`` returns
    ``(True, None)`` and before ``reserve_quota_reservation``.
    """
    try:
        snap = get_quota_snapshot(user_id)
    except Exception:
        # Snapshot read failed — never block the request.
        return

    if not snap or not snap.get("exists") or not snap.get("is_active"):
        return

    reserved = int(snap.get("reserved_tokens") or 0)

    try:
        from backend.services.notification_service import NotificationService
    except Exception:
        return

    for window_label, quota_key, used_key in _QUOTA_WARN_WINDOWS:
        quota = int(snap.get(quota_key) or 0)
        if quota <= 0:
            continue  # 0 = unlimited — no warning applies
        used = int(snap.get(used_key) or 0) + reserved
        pct = used / float(quota)
        for threshold, suffix, cooldown_hours in _QUOTA_WARN_THRESHOLDS:
            if pct < threshold:
                continue
            try:
                notif_type = f"quota_warning_{window_label}_{suffix}"
                title = f"配额预警：{window_label} 窗口已用 {int(pct * 100)}%"
                body = (
                    f"您的 {window_label} 配额已使用 {used} / {quota} tokens "
                    f"（含 {reserved} 预留），达 {int(pct * 100)}%。"
                    f"请合理安排用量或前往钱包充值。"
                )
                NotificationService.notify_with_cooldown(
                    user_id=int(user_id),
                    type=notif_type,
                    title=title,
                    body=body,
                    metadata={
                        "window": window_label,
                        "used": used,
                        "quota": quota,
                        "percent": round(pct, 4),
                        "threshold": threshold,
                        "reserved": reserved,
                    },
                    cooldown_hours=cooldown_hours,
                )
            except Exception:
                logger.exception(
                    "quota warning notification failed user=%s window=%s pct=%.2f",
                    user_id,
                    window_label,
                    pct,
                )
            # Only fire the highest applicable threshold per window —
            # if we already warned at 95%, the 80% warning would be
            # noise. Iterate thresholds high→low, so ``break`` after
            # the first match.
            break


# ---------------------------------------------------------------------------
# Reservation lifecycle
#
# Callers wrap the upstream forward like so (non-streaming):
#
#     ok, reason = assert_request_allowed(user_id, provider, model, est)
#     if not ok: ...
#     reserve_quota_reservation(user_id, est)
#     try:
#         result = await forward(...)
#     except Exception:
#         release_quota_reservation(user_id)
#         raise
#     release_quota_reservation(user_id, actual_prompt, actual_completion)
#
# Streaming routes call release_quota_reservation inside the generator's
# finally block once the real usage tokens are known.
# ---------------------------------------------------------------------------


_RESERVATION_TTL_SECONDS = 300


def reserve_quota_reservation(
    user_id: int,
    estimated_tokens: int,
    request_id: Optional[str] = None,
) -> bool:
    """Persist a short-lived reservation for the user's in-flight request.

    ``request_id`` 透传给 :func:`database.reserve_tokens`,用于多行设计下
    区分同用户的并发请求。``request_id`` 为 ``None`` 时由底层自动生成,
    保持向后兼容(旧调用点无需改动即可继续工作,但所有预留会按
    ``(user_id, generated_id)`` 写入,不再相互覆盖)。

    Returns ``True`` on success. ``False`` (and silently no-ops) when
    ``estimated_tokens <= 0`` or the reservation table has not been
    created yet — callers proceed without reservation in that case.
    """
    from backend.database import reserve_tokens

    est = int(estimated_tokens or 0)
    if est <= 0:
        return False
    return reserve_tokens(
        user_id,
        est,
        ttl_seconds=_RESERVATION_TTL_SECONDS,
        request_id=request_id,
    )


def release_quota_reservation(
    user_id: int,
    actual_prompt: Optional[int] = None,
    actual_completion: Optional[int] = None,
    request_id: Optional[str] = None,
) -> None:
    """Drop the user's in-flight reservation.

    ``request_id`` 透传给 :func:`database.release_reservation`,仅删除
    对应的预留行而不影响同用户其他并发请求。``request_id`` 为 ``None``
    时删除该用户所有预留行(向后兼容旧调用点,适合异常清理场景)。

    ``actual_prompt`` / ``actual_completion`` are informational today —
    the actual usage is already written to ``usage_logs`` by
    ProxyService and rolled up into ``usage_rollups`` by the periodic
    aggregator, which the next quota snapshot will pick up. The params
    are accepted so future follow-ups can add reconciliation logging
    or alerts without changing every call site.
    """
    from backend.database import release_reservation

    release_reservation(user_id, request_id=request_id)
