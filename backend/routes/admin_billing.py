"""Admin billing / pricing / promo / redeem management API.

All routes require either:
* the modern session cookie ``mm_admin_session`` (set by
  ``/api/admin/login`` — what the SPA uses), or
* the legacy ``Authorization: Bearer <jwt>`` header (still supported for
  external scripts and one-off curl calls).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.config import Config
from backend.database import get_db_context, grant_credits, update_wallet
from backend.routes.admin_auth import _admin_csrf_guard, _admin_guard
from backend.services import order_service
from backend.services.audit import get_logs, log_action
from backend.session import (
    ADMIN_SESSION_COOKIE,
    USER_SESSION_COOKIE,
    get_session as get_server_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------


def _secret() -> str:
    return Config.SECRET_KEY


def verify_admin_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[Config.JWT_ALGORITHM])
        return payload.get("sub") == "admin"
    except Exception:
        return False


def _user_id_from_request(request: Request, authorization: str = Header("")) -> int:
    """Resolve the calling user id from the session cookie (preferred) or
    a legacy JWT bearer token. Raises 401 if neither is valid.
    """
    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        sess = get_server_session(user_session_id, user_agent=request.headers.get("User-Agent"))
        if sess and sess.get("role") == "user" and sess.get("user_id"):
            return int(sess["user_id"])
    if authorization:
        token = authorization.replace("Bearer ", "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="未授权")
        try:
            payload = jwt.decode(token, _secret(), algorithms=[Config.JWT_ALGORITHM])
        except Exception:
            raise HTTPException(status_code=401, detail="无效的Token")
        sub = payload.get("sub")
        if sub == "admin" or not str(sub).isdigit():
            raise HTTPException(status_code=401, detail="请使用用户Token")
        return int(sub)
    raise HTTPException(status_code=401, detail="未授权")


def _admin_id_from_request(request: Request) -> int:
    """Extract the admin user id from the session cookie.

    Returns the ``admin_id`` stored in the admin session, or 0 when the
    request uses a legacy bearer token or the session is missing.
    """
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        sess = get_server_session(admin_session_id, user_agent=request.headers.get("User-Agent"))
        if sess:
            return int(sess.get("admin_id") or 0)
    return 0


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PlanPayload(BaseModel):
    name: str
    code: str
    monthly_price: float = 0
    monthly_credits: int = 0
    discount_rate: float = 1.0
    max_api_keys: int = 1
    max_concurrent: int = 5
    rate_limit_rpm: int = 60
    rate_limit_tpm: int = 100000
    features: Optional[Any] = None
    sort_order: int = 0
    is_active: bool = True


class PricingPayload(BaseModel):
    provider: str
    model_id: str
    input_price_per_1k: float = Field(default=0, ge=0)
    output_price_per_1k: float = Field(default=0, ge=0)
    tier: str = "standard"
    note: Optional[str] = None


class PromoCodePayload(BaseModel):
    code: Optional[str] = None
    type: str = "bonus_credits"  # discount_percent | discount_fixed | bonus_credits
    value: float = 0
    bonus_credits: float = 0
    max_uses: int = 0
    per_user_limit: int = 1
    per_ip_limit: int = 3
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    is_active: bool = True


class RedeemCodePayload(BaseModel):
    count: int = 1
    prefix: str = ""
    type: str  # credits | plan_days | plan_upgrade
    value: float
    plan_id: Optional[int] = None
    max_uses: int = 1
    expires_at: Optional[str] = None


class ApproveOrderPayload(BaseModel):
    note: Optional[str] = None


class RejectOrderPayload(BaseModel):
    reason: str = ""


class WalletAdjustPayload(BaseModel):
    delta: float
    reason: str = ""


class SetUserPlanPayload(BaseModel):
    plan_id: int
    days: int = 30


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.get("/admin/plans", dependencies=[Depends(_admin_guard)])
async def list_plans():
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM plans ORDER BY sort_order ASC, id ASC")
        plans = [dict(r) for r in cursor.fetchall()]
        # Attach subscriber count (users currently on each plan).
        for plan in plans:
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE plan_id = ?",
                (plan["id"],),
            )
            row = cursor.fetchone()
            plan["subscriber_count"] = row["cnt"] if row else 0
        return plans


@router.post("/admin/plans", dependencies=[Depends(_admin_csrf_guard)])
async def create_plan(payload: PlanPayload):
    features = payload.features
    if not isinstance(features, str):
        features = json.dumps(features or [], ensure_ascii=False)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO plans
                (name, code, monthly_price, monthly_credits, discount_rate,
                 max_api_keys, max_concurrent, rate_limit_rpm, rate_limit_tpm,
                 features, sort_order, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                payload.name,
                payload.code,
                payload.monthly_price,
                payload.monthly_credits,
                payload.discount_rate,
                payload.max_api_keys,
                payload.max_concurrent,
                payload.rate_limit_rpm,
                payload.rate_limit_tpm,
                features,
                payload.sort_order,
                1 if payload.is_active else 0,
            ),
        )
        new_id = int(cursor.lastrowid)
    return {"id": new_id, **payload.dict()}


@router.patch("/admin/plans/{plan_id}", dependencies=[Depends(_admin_csrf_guard)])
async def update_plan(plan_id: int, payload: Dict[str, Any]):
    allowed = {
        "name",
        "monthly_price",
        "monthly_credits",
        "discount_rate",
        "max_api_keys",
        "max_concurrent",
        "rate_limit_rpm",
        "rate_limit_tpm",
        "features",
        "sort_order",
        "is_active",
        "code",
    }
    updates, params = [], []
    for key, val in payload.items():
        if key not in allowed:
            continue
        if key == "features" and not isinstance(val, str):
            val = json.dumps(val, ensure_ascii=False)
        if key == "is_active":
            val = 1 if val else 0
        updates.append(f"{key} = ?")
        params.append(val)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    params.append(int(plan_id))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE plans SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return {"message": "已更新"}


@router.delete("/admin/plans/{plan_id}", dependencies=[Depends(_admin_csrf_guard)])
async def delete_plan(plan_id: int):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE plan_id = ?",
            (int(plan_id),),
        )
        row = cursor.fetchone()
        if row and row["cnt"] > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete plan with active subscribers ({row['cnt']} users)",
            )
        cursor.execute("UPDATE plans SET is_active = 0 WHERE id = ?", (int(plan_id),))
    return {"message": "已软删"}


@router.post("/admin/plans/{plan_id}/preview-update", dependencies=[Depends(_admin_csrf_guard)])
async def preview_update_plan(plan_id: int, payload: Dict[str, Any]):
    """Dry-run preview of a plan update.

    Returns the number of active subscriptions on this plan and the
    projected monthly revenue delta if the new ``monthly_price`` were
    applied. Does NOT modify the plan.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT monthly_price FROM plans WHERE id = ?",
            (int(plan_id),),
        )
        plan_row = cursor.fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="套餐不存在")
        old_price = float(plan_row["monthly_price"] or 0)
        new_price = old_price
        if "monthly_price" in payload and payload["monthly_price"] is not None:
            try:
                new_price = float(payload["monthly_price"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="monthly_price 必须为数字")
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM subscriptions WHERE plan_id = ? AND status = 'active'",
            (int(plan_id),),
        )
        affected = int(cursor.fetchone()["cnt"] or 0)
    monthly_revenue_delta = round((new_price - old_price) * affected, 2)
    return {
        "affected_subscriptions": affected,
        "monthly_revenue_delta": monthly_revenue_delta,
        "old_monthly_price": old_price,
        "new_monthly_price": new_price,
    }


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@router.get("/admin/pricing", dependencies=[Depends(_admin_guard)])
async def list_pricing(
    provider: Optional[str] = None,
    model_id: Optional[str] = None,
):
    clauses = ["1=1"]
    params: List[Any] = []
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if model_id:
        clauses.append("model_id = ?")
        params.append(model_id)
    sql = f"""
        SELECT * FROM model_pricing
        WHERE {" AND ".join(clauses)}
        ORDER BY provider ASC, model_id ASC
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return [dict(r) for r in cursor.fetchall()]


@router.post("/admin/pricing", dependencies=[Depends(_admin_csrf_guard)])
async def upsert_pricing(payload: PricingPayload):
    """Create a custom row (``is_custom=1``) or update an existing one."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM model_pricing
            WHERE provider = ? AND model_id = ? AND tier = ?
        """,
            (payload.provider, payload.model_id, payload.tier),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE model_pricing
                SET input_price_per_1k = ?, output_price_per_1k = ?,
                    is_custom = 1, is_active = 1, note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """,
                (
                    payload.input_price_per_1k,
                    payload.output_price_per_1k,
                    payload.note,
                    int(existing["id"]),
                ),
            )
            return {"id": int(existing["id"]), "updated": True}
        cursor.execute(
            """
            INSERT INTO model_pricing
                (provider, model_id, input_price_per_1k, output_price_per_1k,
                 tier, is_active, is_custom, note)
            VALUES (?, ?, ?, ?, ?, 1, 1, ?)
        """,
            (
                payload.provider,
                payload.model_id,
                payload.input_price_per_1k,
                payload.output_price_per_1k,
                payload.tier,
                payload.note,
            ),
        )
        return {"id": int(cursor.lastrowid), "created": True}


@router.patch("/admin/pricing/{pricing_id}", dependencies=[Depends(_admin_csrf_guard)])
async def update_pricing(pricing_id: int, payload: Dict[str, Any]):
    allowed = {"input_price_per_1k", "output_price_per_1k", "tier", "note", "is_active"}
    updates, params = [], []
    for k, v in payload.items():
        if k not in allowed:
            continue
        if k == "is_active":
            v = 1 if v else 0
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(pricing_id))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE model_pricing SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return {"message": "已更新"}


@router.delete("/admin/pricing/{pricing_id}", dependencies=[Depends(_admin_csrf_guard)])
async def delete_pricing(pricing_id: int):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE model_pricing SET is_active = 0 WHERE id = ?", (int(pricing_id),))
    return {"message": "已软删"}


@router.post("/admin/pricing/reset-official", dependencies=[Depends(_admin_csrf_guard)])
async def reset_official_pricing(payload: Dict[str, Any]):
    """Drop any custom rows for (provider, model_id) so the official
    defaults in ``model_pricing`` take over again."""
    provider = payload.get("provider")
    model_id = payload.get("model_id")
    if not provider or not model_id:
        raise HTTPException(status_code=400, detail="provider / model_id 必填")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM model_pricing
            WHERE provider = ? AND model_id = ? AND is_custom = 1
        """,
            (provider, model_id),
        )
        removed = int(cursor.rowcount or 0)
    return {"removed_custom_rows": removed}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@router.get("/admin/orders", dependencies=[Depends(_admin_guard)])
async def list_all_orders(
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return order_service.list_orders(
        user_id=int(user_id) if user_id is not None else None,
        status=status,
        limit=int(limit),
        offset=int(offset),
    )


@router.get("/admin/orders/export.csv", dependencies=[Depends(_admin_guard)])
async def export_orders_csv(
    request: Request,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
):
    """Export orders as CSV with optional filters."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"
        writer.writerow(
            [
                "id",
                "order_no",
                "user_id",
                "amount",
                "credits",
                "status",
                "payment_method",
                "created_at",
                "paid_at",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        clauses: list = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, order_no, user_id, amount, credits, status, "
                f"payment_method, created_at, paid_at "
                f"FROM orders {where} ORDER BY id ASC",
                params,
            )
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    writer.writerow(
                        [
                            row["id"],
                            row["order_no"],
                            row["user_id"],
                            row["amount"],
                            row["credits"],
                            row["status"],
                            row["payment_method"] or "",
                            row["created_at"] or "",
                            row["paid_at"] or "",
                        ]
                    )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orders-{today}.csv"'},
    )


@router.get("/admin/orders/{order_id}", dependencies=[Depends(_admin_guard)])
async def get_order_detail(order_id: int):
    """Return a single order with the owning user's username joined in."""
    order = order_service.get_order(int(order_id))
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    # Join the username so the admin UI can display it without a second call.
    uid = order.get("user_id")
    if uid:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM users WHERE id = ?", (int(uid),))
            urow = cursor.fetchone()
            if urow:
                order["username"] = urow["username"]
    return order


class RefundOrderPayload(BaseModel):
    reason: str = ""


@router.post("/admin/orders/{order_id}/refund", dependencies=[Depends(_admin_csrf_guard)])
async def refund_order_endpoint(order_id: int, payload: RefundOrderPayload, request: Request):
    """Refund a paid order atomically via order_service.refund_order.

    Wallet debit, wallet_transaction record, and order status update
    all happen in a single ``BEGIN IMMEDIATE`` transaction inside the
    service layer (Fix 1).
    """
    admin_id = _admin_id_from_request(request)
    try:
        ok = order_service.refund_order(
            int(order_id), admin_id=admin_id, reason=payload.reason or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(
            status_code=400, detail="订单不存在或状态不正确(只有已支付的订单可以退款)"
        )
    # Read the order to surface credit info in the audit log.
    order = order_service.get_order(int(order_id))
    total_credits = 0.0
    if order:
        total_credits = float(order.get("credits") or 0) + float(order.get("bonus_credits") or 0)
    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="order.refund",
        target_type="order",
        target_id=int(order_id),
        details={"reason": payload.reason, "credits": total_credits},
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已退款"}


@router.post("/admin/orders/{order_id}/approve", dependencies=[Depends(_admin_csrf_guard)])
async def approve_order_endpoint(order_id: int, request: Request):
    admin_id = _admin_id_from_request(request)
    ok = order_service.approve_order(int(order_id), admin_id=admin_id)
    if not ok:
        raise HTTPException(status_code=400, detail="订单不存在或已处理")
    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="order.approve",
        target_type="order",
        target_id=int(order_id),
        details=None,
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已批准"}


class ApproveMismatchedPayload(BaseModel):
    paid_amount: float


@router.post("/admin/orders/{order_id}/approve-mismatched", dependencies=[Depends(_admin_csrf_guard)])
async def approve_mismatched_order_endpoint(
    order_id: int, payload: ApproveMismatchedPayload, request: Request
):
    admin_id = _admin_id_from_request(request)
    ok = order_service.approve_mismatched_order(
        int(order_id), admin_id=admin_id, paid_amount=float(payload.paid_amount)
    )
    if not ok:
        raise HTTPException(status_code=400, detail="订单不存在或状态不正确")
    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="order.approve_mismatched",
        target_type="order",
        target_id=int(order_id),
        details={"paid_amount": float(payload.paid_amount)},
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已批准(按比例调整credits)"}


@router.post("/admin/orders/{order_id}/reject", dependencies=[Depends(_admin_csrf_guard)])
async def reject_order_endpoint(order_id: int, payload: RejectOrderPayload, request: Request):
    admin_id = _admin_id_from_request(request)
    ok = order_service.reject_order(int(order_id), admin_id=admin_id, reason=payload.reason or "")
    if not ok:
        raise HTTPException(
            status_code=400, detail="订单不存在或状态不正确(只有待处理的订单可以拒绝)"
        )
    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="order.reject",
        target_type="order",
        target_id=int(order_id),
        details={"reason": payload.reason},
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已拒绝"}


# ---------------------------------------------------------------------------
# Wallet / plan manual adjustments
# ---------------------------------------------------------------------------


@router.post("/admin/users/{user_id}/wallet", dependencies=[Depends(_admin_csrf_guard)])
async def admin_adjust_wallet(user_id: int, payload: WalletAdjustPayload, request: Request):
    delta = float(payload.delta or 0)
    if delta == 0:
        raise HTTPException(status_code=400, detail="delta 不能为 0")
    reason = payload.reason or "admin_adjust"
    abs_delta = abs(delta)
    admin_id = _admin_id_from_request(request)

    if abs_delta > 1000:
        if not payload.reason or not payload.reason.strip():
            raise HTTPException(
                status_code=400,
                detail="超过 1000 credits 的调整必须填写原因",
            )
        try:
            from backend.database import add_audit_log

            add_audit_log(
                actor_type="admin",
                actor_id=admin_id or None,
                action="wallet.adjust_critical",
                target_type="user",
                target_id=str(user_id),
                ip_address=request.client.host if request.client else None,
                metadata={
                    "delta": delta,
                    "reason": reason,
                    "severity": "critical",
                },
            )
        except Exception:
            logger.exception("critical audit log write failed for wallet adjust")
        # Push an alert so super-admins see large adjustments in real time.
        try:
            from backend.services.alert_service import AlertService

            AlertService.send_alert_sync(
                "WARNING",
                f"Large wallet adjustment: admin={admin_id} user={user_id} delta={delta}",
                {
                    "admin_id": admin_id,
                    "user_id": user_id,
                    "delta": delta,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception("alert send failed for wallet adjust")

    # Per-admin and global daily caps. The per-admin cap stops one
    # runaway admin from moving 100k credits in a day; the global cap
    # closes the N-admins × 10000 bypass (multiple admins each hitting
    # their per-admin quota would collectively exceed the intended
    # platform-wide ceiling).
    #
    # P0.3: the per-admin cap previously only counted
    # ``type='admin_adjust'`` rows (matched via ``note LIKE
    # '%admin:N%'``). ``approve_order`` writes ``type='recharge'`` and
    # ``redeem_code`` writes ``type='redeem'``, so both paths silently
    # bypassed the cap. Use ``get_admin_daily_wallet_operations`` which
    # aggregates all three credit-side paths into a single ``total``.
    per_admin_cap = float(Config.WALLET_ADJUST_DAILY_PER_ADMIN_CAP)
    global_cap = float(Config.WALLET_ADJUST_DAILY_GLOBAL_CAP)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    per_admin_used = 0.0
    if admin_id:
        per_admin_breakdown = order_service.get_admin_daily_wallet_operations(admin_id)
        per_admin_used = float(per_admin_breakdown.get("total") or 0)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(ABS(amount)), 0)
            FROM wallet_transactions
            WHERE type = 'admin_adjust'
              AND created_at >= ?
            """,
            (cutoff,),
        )
        global_used = float(cursor.fetchone()[0] or 0)

    per_admin_remaining = max(0.0, per_admin_cap - per_admin_used)
    global_remaining = max(0.0, global_cap - global_used)
    available = min(per_admin_remaining, global_remaining)
    if abs_delta > available:
        raise HTTPException(
            status_code=403,
            detail=(
                f"调整额度超过每日上限 (本次需要 {abs_delta:.0f}, "
                f"剩余可用 {available:.0f} = min(per_admin {per_admin_remaining:.0f}, "
                f"global {global_remaining:.0f}))"
            ),
        )
    if admin_id:
        reason = reason + f" [admin:{admin_id}]"

    try:
        result = update_wallet(
            int(user_id),
            delta,
            "admin_adjust",
            related_type="admin",
            related_id=None,
            note=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="wallet.adjust",
        target_type="user",
        target_id=int(user_id),
        details={"delta": delta, "reason": reason},
        ip_address=request.client.host if request.client else None,
    )
    return {"balance": result["balance"]}


@router.get("/admin/users/{user_id}/wallet-transactions", dependencies=[Depends(_admin_guard)])
async def admin_user_wallet_transactions(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
):
    """Return recent wallet transactions for a specific user."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, type, amount, balance_after, related_type, related_id,
                   note, created_at
            FROM wallet_transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        )
        return [dict(r) for r in cursor.fetchall()]


@router.post("/admin/users/{user_id}/plan", dependencies=[Depends(_admin_csrf_guard)])
async def admin_set_user_plan(user_id: int, payload: SetUserPlanPayload, request: Request):
    expires_at = datetime.now(timezone.utc) + timedelta(days=int(payload.days))
    expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
    # P1.2: capture monthly_credits + code + new sub id so we can grant
    # the plan's monthly_credits after the subscription row is committed.
    # Without this, admin-assigned plans never credited the user with
    # the plan's recurring monthly_credits.
    monthly_credits = 0.0
    plan_code = ""
    new_sub_id: Optional[int] = None
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE id = ?", (int(user_id),))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        cursor.execute(
            "SELECT id, is_active, monthly_credits, code FROM plans WHERE id = ?",
            (int(payload.plan_id),),
        )
        plan_row = cursor.fetchone()
        if not plan_row:
            raise HTTPException(status_code=404, detail="套餐不存在")
        if not plan_row["is_active"]:
            raise HTTPException(status_code=400, detail="不能分配已下架的套餐")
        monthly_credits = float(plan_row["monthly_credits"] or 0)
        plan_code = plan_row["code"] or ""
        cursor.execute(
            """
            UPDATE subscriptions SET status = 'upgraded',
                cancelled_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND status = 'active'
        """,
            (int(user_id),),
        )
        cursor.execute(
            """
            UPDATE users SET plan_id = ?, plan_expires_at = ?
            WHERE id = ?
        """,
            (int(payload.plan_id), expires_str, int(user_id)),
        )
        cursor.execute(
            """
            INSERT INTO subscriptions
                (user_id, plan_id, status, started_at, expires_at, auto_renew)
            VALUES (?, ?, 'active', CURRENT_TIMESTAMP, ?, 1)
        """,
            (int(user_id), int(payload.plan_id), expires_str),
        )
        new_sub_id = int(cursor.lastrowid) if cursor.lastrowid else None

    # P1.2: Grant the plan's monthly_credits now that the subscription
    # row is committed. grant_credits opens its own transaction and
    # atomically credits the wallet + writes the ledger row with the
    # configured CREDITS_EXPIRE_DAYS horizon. Skip when the plan has
    # no monthly_credits (e.g. the free tier).
    if monthly_credits > 0 and new_sub_id is not None:
        try:
            grant_credits(
                int(user_id),
                monthly_credits,
                "recharge",
                related_type="subscription",
                related_id=new_sub_id,
                note=f"admin plan assign: {plan_code}",
            )
        except Exception:
            logger.exception(
                "grant_credits failed for admin_set_user_plan user=%s plan=%s",
                user_id, payload.plan_id,
            )

    log_action(
        actor_id=None,
        actor_type="admin",
        action="user.set_plan",
        target_type="user",
        target_id=int(user_id),
        details={
            "plan_id": int(payload.plan_id),
            "days": int(payload.days),
            "monthly_credits": monthly_credits,
            "subscription_id": new_sub_id,
        },
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已设置", "plan_id": int(payload.plan_id), "expires_at": expires_str}


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------


def _ensure_promo_ip_columns() -> None:
    """Idempotently add ``per_ip_limit`` to ``promo_codes`` and
    ``ip_address`` to ``promo_code_usage``.

    Both columns are missing from the baseline migration. We guard with
    ``PRAGMA table_info`` so this is a no-op when the columns already
    exist — safe to call on every promo-code mutation.
    """
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(promo_codes)")
            promo_cols = {row[1] for row in cursor.fetchall()}
            if "per_ip_limit" not in promo_cols:
                cursor.execute(
                    "ALTER TABLE promo_codes ADD COLUMN per_ip_limit INTEGER DEFAULT 3"
                )
            cursor.execute("PRAGMA table_info(promo_code_usage)")
            usage_cols = {row[1] for row in cursor.fetchall()}
            if "ip_address" not in usage_cols:
                cursor.execute(
                    "ALTER TABLE promo_code_usage ADD COLUMN ip_address VARCHAR(45)"
                )
    except Exception:
        logger.debug("failed to ensure promo per_ip_limit / ip_address columns", exc_info=True)


def _get_promo_per_ip_limit(cursor, promo_id: int) -> int:
    """Return the per-IP limit for a promo code (default 3 if column missing)."""
    try:
        cursor.execute("SELECT per_ip_limit FROM promo_codes WHERE id = ?", (int(promo_id),))
        row = cursor.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return 3


def check_promo_ip_limit(promo_id: int, ip_address: str) -> bool:
    """Return True if the IP is within the 24h per-IP limit for a promo.

    Public helper so ``order_service.create_order`` (or any other
    apply-path) can import and call it before granting promo bonuses.
    Returns True (allow) when ``ip_address`` is empty — the per-user
    limit still applies in that case.
    """
    if not ip_address:
        return True
    _ensure_promo_ip_columns()
    with get_db_context() as conn:
        cursor = conn.cursor()
        limit = _get_promo_per_ip_limit(cursor, promo_id)
        if limit <= 0:
            return True
        cursor.execute(
            """
            SELECT COUNT(*) FROM promo_code_usage
            WHERE promo_code_id = ?
              AND ip_address = ?
              AND created_at > datetime('now', '-1 day')
            """,
            (int(promo_id), ip_address),
        )
        used = int(cursor.fetchone()[0] or 0)
    return used < limit


@router.get("/admin/promo-codes", dependencies=[Depends(_admin_guard)])
async def list_promo_codes(
    status: Optional[str] = None,
    search: Optional[str] = None,
):
    clauses = ["1=1"]
    params: List[Any] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if status == "active":
        clauses.append("is_active = 1")
        clauses.append("(valid_until IS NULL OR valid_until > ?)")
        params.append(now)
        clauses.append("(max_uses = 0 OR used_count < max_uses)")
    elif status == "exhausted":
        clauses.append("is_active = 1")
        clauses.append("max_uses > 0 AND used_count >= max_uses")
    elif status == "expired":
        clauses.append("valid_until IS NOT NULL AND valid_until <= ?")
        params.append(now)
    elif status == "revoked":
        clauses.append("is_active = 0")
    if search:
        clauses.append("code LIKE ?")
        params.append(f"%{search}%")
    sql = f"SELECT * FROM promo_codes WHERE {' AND '.join(clauses)} ORDER BY id DESC"
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return [dict(r) for r in cursor.fetchall()]


def _generate_promo_code() -> str:
    """Generate a random 16-char promo code split into 4 groups of 4."""
    alphabet = string.ascii_uppercase + string.digits
    chars = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"{chars[:4]}-{chars[4:8]}-{chars[8:12]}-{chars[12:16]}"


@router.post("/admin/promo-codes", dependencies=[Depends(_admin_csrf_guard)])
async def create_promo_code(payload: PromoCodePayload, request: Request):
    _ensure_promo_ip_columns()
    code = (payload.code or "").strip()
    if not code:
        code = _generate_promo_code()
    with get_db_context() as conn:
        cursor = conn.cursor()
        # Ensure uniqueness — retry once on collision.
        cursor.execute("SELECT id FROM promo_codes WHERE code = ?", (code,))
        if cursor.fetchone():
            code = _generate_promo_code()
        # Try to insert with per_ip_limit; fall back to the legacy
        # column set if the ALTER TABLE hasn't landed yet.
        try:
            cursor.execute(
                """
                INSERT INTO promo_codes
                    (code, type, value, bonus_credits, max_uses, per_user_limit,
                     per_ip_limit, valid_from, valid_until, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    code,
                    payload.type,
                    payload.value,
                    payload.bonus_credits,
                    payload.max_uses,
                    payload.per_user_limit,
                    int(payload.per_ip_limit),
                    payload.valid_from,
                    payload.valid_until,
                    1 if payload.is_active else 0,
                ),
            )
        except Exception:
            cursor.execute(
                """
                INSERT INTO promo_codes
                    (code, type, value, bonus_credits, max_uses, per_user_limit,
                     valid_from, valid_until, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    code,
                    payload.type,
                    payload.value,
                    payload.bonus_credits,
                    payload.max_uses,
                    payload.per_user_limit,
                    payload.valid_from,
                    payload.valid_until,
                    1 if payload.is_active else 0,
                ),
            )
        new_id = int(cursor.lastrowid)
    log_action(
        actor_id=None,
        actor_type="admin",
        action="promo.create",
        target_type="promo_code",
        target_id=new_id,
        details={"code": code, "type": payload.type, "per_ip_limit": int(payload.per_ip_limit)},
        ip_address=request.client.host if request.client else None,
    )
    return {"id": new_id, "code": code}


@router.patch("/admin/promo-codes/{promo_id}", dependencies=[Depends(_admin_csrf_guard)])
async def update_promo_code(promo_id: int, payload: Dict[str, Any]):
    _ensure_promo_ip_columns()
    allowed = {
        "code",
        "type",
        "value",
        "bonus_credits",
        "max_uses",
        "per_user_limit",
        "per_ip_limit",
        "valid_from",
        "valid_until",
        "is_active",
    }
    updates, params = [], []
    for k, v in payload.items():
        if k not in allowed:
            continue
        if k == "is_active":
            v = 1 if v else 0
        if k == "per_ip_limit":
            v = int(v)
        updates.append(f"{k} = ?")
        params.append(v)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    params.append(int(promo_id))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE promo_codes SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return {"message": "已更新"}


@router.delete("/admin/promo-codes/{promo_id}", dependencies=[Depends(_admin_csrf_guard)])
async def delete_promo_code(promo_id: int):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM promo_code_usage WHERE promo_code_id = ?",
            (int(promo_id),),
        )
        usage_row = cursor.fetchone()
        if usage_row and usage_row[0] > 0:
            raise HTTPException(
                status_code=409,
                detail="Promo code has been used; revoke instead of delete",
            )
        cursor.execute("DELETE FROM promo_codes WHERE id = ?", (int(promo_id),))
    return {"message": "已删除"}


@router.post("/admin/promo-codes/{promo_id}/revoke", dependencies=[Depends(_admin_csrf_guard)])
async def revoke_promo_code(promo_id: int, request: Request):
    """Soft-revoke a promo code by setting is_active = 0."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM promo_codes WHERE id = ?", (int(promo_id),))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Promo code not found")
        cursor.execute("UPDATE promo_codes SET is_active = 0 WHERE id = ?", (int(promo_id),))
    log_action(
        actor_id=None,
        actor_type="admin",
        action="promo.revoke",
        target_type="promo_code",
        target_id=int(promo_id),
        details={},
        ip_address=request.client.host if request.client else None,
    )
    return {"message": "已撤销"}


# ---------------------------------------------------------------------------
# API keys (admin)
# ---------------------------------------------------------------------------


@router.get("/admin/api-keys", dependencies=[Depends(_admin_guard)])
async def list_api_keys(
    user_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Platform-wide view of API keys (admin only).

    Each row is joined with the owning ``users`` record so the admin can
    see at a glance which key belongs to whom. Internal hash columns are
    never returned — only the masked display form is exposed.
    """
    clauses: List[str] = ["1=1"]
    params: List[Any] = []
    if user_id is not None:
        clauses.append("k.user_id = ?")
        params.append(int(user_id))
    where = " AND ".join(clauses)
    params.extend([int(limit), int(offset)])

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT k.id, k.user_id, u.username,
                   k.name, k.key_prefix, k.key_mask,
                   k.monthly_token_limit, k.monthly_credit_limit,
                   k.allowed_models, k.is_active,
                   k.last_used_at, k.expires_at, k.created_at
              FROM api_keys k
              LEFT JOIN users u ON u.id = k.user_id
             WHERE {where}
             ORDER BY k.id DESC
             LIMIT ? OFFSET ?
        """,
            tuple(params),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    for r in rows:
        # Surface allowed models as parsed list, fall back to empty list.
        raw = r.get("allowed_models")
        if raw:
            try:
                r["allowed_models"] = json.loads(raw)
            except Exception:
                r["allowed_models"] = []
        else:
            r["allowed_models"] = []
    return rows


@router.delete("/admin/api-keys/{key_id}", dependencies=[Depends(_admin_csrf_guard)])
async def revoke_api_key(key_id: int, request: Request):
    """Soft-disable an API key on behalf of the user.

    Mirrors the user-facing endpoint, but is intended for moderation
    actions (lost laptop, abuse, plan downgrade, etc.).
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM api_keys WHERE id = ?", (int(key_id),))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="API Key 不存在")
        cursor.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (int(key_id),))
    ip_address = None
    if request is not None and getattr(request, "client", None) is not None:
        ip_address = request.client.host
    log_action(
        actor_id=None,
        actor_type="admin",
        action="api_key.revoke",
        target_type="api_key",
        target_id=int(key_id),
        details={},
        ip_address=ip_address,
    )
    return {"message": "API Key 已撤销"}


# ---------------------------------------------------------------------------
# Redeem codes
# ---------------------------------------------------------------------------


@router.get("/admin/redeem-codes", dependencies=[Depends(_admin_guard)])
async def list_redeem_codes():
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM redeem_codes ORDER BY id DESC")
        return [dict(r) for r in cursor.fetchall()]


@router.post("/admin/redeem-codes", dependencies=[Depends(_admin_csrf_guard)])
async def create_redeem_codes(payload: RedeemCodePayload, request: Request):
    try:
        codes = order_service.create_redeem_codes(
            count=int(payload.count),
            code_type=payload.type,
            value=float(payload.value),
            prefix=payload.prefix or "",
            plan_id=payload.plan_id,
            max_uses=int(payload.max_uses or 1),
            expires_at=payload.expires_at,
            admin_id=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=None,
        actor_type="admin",
        action="redeem.batch_create",
        target_type="redeem_code",
        target_id=None,
        details={"count": payload.count, "type": payload.type, "value": payload.value},
        ip_address=request.client.host if request.client else None,
    )
    return {"codes": codes, "count": len(codes)}


@router.delete("/admin/redeem-codes/{redeem_id}", dependencies=[Depends(_admin_csrf_guard)])
async def delete_redeem_code(redeem_id: int):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE redeem_codes SET is_active = 0 WHERE id = ?", (int(redeem_id),))
    return {"message": "已停用"}


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------


@router.get("/admin/audit-logs", dependencies=[Depends(_admin_guard)])
async def list_audit_logs(
    actor_id: Optional[int] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return get_logs(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=int(target_id) if target_id is not None else None,
        date_from=date_from,
        date_to=date_to,
        limit=int(limit),
        offset=int(offset),
    )


# ---------------------------------------------------------------------------
# M9: User-level model access control (deny list management)
# ---------------------------------------------------------------------------
# The ``user_model_access`` table historically held only ``allow`` rows
# written by the subscription-approval flow, but was never read at
# runtime — making it a write-only "audit" table. M9 wires the table
# into ``auth_service.check_user_model_access`` (read on every API key
# gated request) and exposes admin CRUD here so operators can:
#   * deny a specific model to a user (e.g. block access to a premium
#     model after a refund)
#   * deny an entire provider to a user (model_id = "openai/*")
#   * list current deny rows for a user
#   * clear a deny row when the situation is resolved
#
# ``allow`` rows are still written by subscription-approval and remain
# informational (the default is "allow everything"); we don't expose
# CRUD for ``allow`` rows here because there's no runtime behaviour to
# tune — they're an audit trail, not a switch.


class UserModelAccessPayload(BaseModel):
    model_id: str = Field(..., description="Model identifier (e.g. 'openai/gpt-4o') or provider-wide wildcard (e.g. 'openai/*')")
    access_type: str = Field("deny", pattern="^(allow|deny)$")


@router.get(
    "/admin/users/{user_id}/model-access",
    dependencies=[Depends(_admin_guard)],
)
async def list_user_model_access(user_id: int):
    """List all user_model_access rows for the given user."""
    with get_db_context() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, model_id, access_type, granted_at, granted_by
            FROM user_model_access
            WHERE user_id = ?
            ORDER BY access_type DESC, model_id ASC
            """,
            (int(user_id),),
        )
        rows = cursor.fetchall()
    return {"count": len(rows), "items": rows}


@router.post(
    "/admin/users/{user_id}/model-access",
    dependencies=[Depends(_admin_csrf_guard)],
)
async def set_user_model_access(user_id: int, payload: UserModelAccessPayload):
    """Insert or update a user_model_access row.

    ``granted_by`` is left NULL because admin endpoints here don't
    resolve the actor id from the session — the audit_logs table is
    the canonical record of "who did what" and is written separately
    by callers that have the actor context.
    """
    model_id = (payload.model_id or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")
    if not model_id.endswith("/*") and "/" not in model_id:
        # Encourage the operator to use the ``provider/model`` form so
        # the matching logic in check_user_model_access works. We don't
        # hard-reject — a bare ``gpt-4o`` is technically valid — but we
        # warn so the admin realises the convention.
        pass
    access_type = payload.access_type or "deny"
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                """
                INSERT INTO user_model_access (user_id, model_id, access_type, granted_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, model_id) DO UPDATE SET
                    access_type = excluded.access_type,
                    granted_at = CURRENT_TIMESTAMP
                """,
                (int(user_id), model_id, access_type),
            )
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
    return {
        "message": "已设置",
        "user_id": int(user_id),
        "model_id": model_id,
        "access_type": access_type,
    }


@router.delete(
    "/admin/users/{user_id}/model-access/{model_id:path}",
    dependencies=[Depends(_admin_csrf_guard)],
)
async def delete_user_model_access(user_id: int, model_id: str):
    """Remove a user_model_access row. ``model_id`` is URL-path-encoded
    and may contain slashes (e.g. ``openai/gpt-4o``).
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_model_access WHERE user_id = ? AND model_id = ?",
            (int(user_id), model_id),
        )
        deleted = cursor.rowcount
    return {"message": "已删除" if deleted else "无匹配行", "deleted": int(deleted)}


# ---------------------------------------------------------------------------
# Payment provider configuration
# ---------------------------------------------------------------------------


class PaymentProviderPayload(BaseModel):
    is_enabled: Optional[bool] = None
    api_key: Optional[str] = None
    webhook_secret: Optional[str] = None


@router.get("/admin/payment/providers", dependencies=[Depends(_admin_guard)])
async def list_payment_providers():
    """List configured payment providers and their status.

    Each entry includes:
      - ``name``: provider slug (stripe, alipay, wechat)
      - ``available``: whether the provider SDK is configured
      - ``enabled``: whether the admin has enabled this provider
      - ``error``: error message when unavailable
    """
    from backend.database import get_setting
    from backend.services.payment import list_providers

    providers = list_providers()
    result = []
    for name, info in providers.items():
        enabled_key = f"payment_provider_{name}_enabled"
        is_enabled_raw = get_setting(enabled_key)
        is_enabled = is_enabled_raw == "1" if is_enabled_raw else info["available"]
        result.append(
            {
                "name": name,
                "available": info["available"],
                "enabled": is_enabled,
                "error": info.get("error"),
            }
        )
    return result


@router.patch("/admin/payment/providers/{name}", dependencies=[Depends(_admin_csrf_guard)])
async def update_payment_provider(name: str, payload: PaymentProviderPayload, request: Request):
    """Enable/disable a payment provider and optionally update API keys.

    API keys and webhook secrets are stored encrypted in the
    ``settings`` table via ``set_setting``.
    """
    from backend.database import get_setting, set_setting

    name = name.lower().strip()
    known = {"stripe", "alipay", "wechat", "usdt"}
    if name not in known:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {name}")

    admin_id = _admin_id_from_request(request)
    changes = {}

    if payload.is_enabled is not None:
        enabled_key = f"payment_provider_{name}_enabled"
        old_value = get_setting(enabled_key)
        new_value = "1" if payload.is_enabled else "0"
        set_setting(enabled_key, new_value)
        changes["enabled"] = {"old": old_value, "new": new_value}

    if payload.api_key is not None:
        key_name = f"payment_provider_{name}_api_key"
        old_value = "***" if get_setting(key_name) else None
        set_setting(key_name, payload.api_key, encrypt=True)
        changes["api_key"] = {"old": old_value, "new": "***"}

    if payload.webhook_secret is not None:
        secret_name = f"payment_provider_{name}_webhook_secret"
        old_value = "***" if get_setting(secret_name) else None
        set_setting(secret_name, payload.webhook_secret, encrypt=True)
        changes["webhook_secret"] = {"old": old_value, "new": "***"}

    log_action(
        actor_id=admin_id or None,
        actor_type="admin",
        action="config_update",
        target_type="payment_provider",
        target_id=None,
        details={"provider": name, "changes": changes},
        ip_address=request.client.host if request.client else None,
    )

    return {"message": "已更新", "name": name}


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------


@router.get("/admin/wallet-transactions/export.csv", dependencies=[Depends(_admin_guard)])
async def export_wallet_transactions_csv(
    request: Request,
    user_id: Optional[int] = None,
    type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Export wallet transactions as CSV with optional filters."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"
        writer.writerow(
            [
                "id",
                "user_id",
                "type",
                "amount",
                "balance_after",
                "related_type",
                "related_id",
                "note",
                "created_at",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        clauses: list = []
        params: list = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        if type:
            clauses.append("type = ?")
            params.append(type)
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("created_at <= ?")
            params.append(date_to + " 23:59:59")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, user_id, type, amount, balance_after, "
                f"related_type, related_id, note, created_at "
                f"FROM wallet_transactions {where} ORDER BY id ASC",
                params,
            )
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for row in rows:
                    writer.writerow(
                        [
                            row["id"],
                            row["user_id"],
                            row["type"],
                            row["amount"],
                            row["balance_after"],
                            row["related_type"] or "",
                            row["related_id"] or "",
                            row["note"] or "",
                            row["created_at"] or "",
                        ]
                    )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="wallet-transactions-{today}.csv"'},
    )
