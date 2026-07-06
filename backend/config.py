import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


_DEV_SECRET_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".dev-secret")


class Config:
    ENV = os.getenv("ENV", "development").lower()

    IS_PRODUCTION = ENV in {"prod", "production", "staging"}

    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./minimax_proxy.db")

    MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_API_BASE = os.getenv("MINIMAX_API_BASE", "https://api.minimaxi.com")

    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

    # Per-window token quotas applied to newly-registered users. The
    # 5h window was 500 tokens in the initial deployment which was
    # too tight to survive a single typical GPT-4 request — new users
    # hit the wall on their first call despite having 10000 credits
    # in their wallet. 3000 tokens ≈ a handful of small requests,
    # enough to evaluate the platform before the sliding window
    # reopens. Operators can override via DEFAULT_QUOTA_5H / _WEEK.
    DEFAULT_QUOTA_5H = int(os.getenv("DEFAULT_QUOTA_5H", "3000") or 3000)
    DEFAULT_QUOTA_WEEK = int(os.getenv("DEFAULT_QUOTA_WEEK", "5000") or 5000)

    # Days to retain a soft-deleted account (is_active=0, email=NULL,
    # username="deleted_{id}_{ts}") before the daily purge wipes it
    # via UserService.delete_user. Matches the promise the
    # /user/data/delete endpoint makes to the user.
    SOFT_DELETE_RETENTION_DAYS = int(os.getenv("SOFT_DELETE_RETENTION_DAYS", "30") or 30)

    # ISO 4217 currency used when handing the order amount to Stripe.
    # The internal credit ledger is priced in CNY (1 CNY = 100 credits),
    # so by default the Stripe checkout is created in CNY so the
    # customer sees the same ¥ amount they were quoted. Override via
    # STRIPE_CURRENCY only if the Stripe account cannot settle in CNY;
    # in that case the raw numeric value is reused (10 CNY becomes 10
    # USD in the checkout) and the operator absorbs the FX gap.
    STRIPE_CURRENCY = os.getenv("STRIPE_CURRENCY", "cny").lower().strip() or "cny"

    # NOWPayments (USDT / other crypto) — operator deploys with a
    # NOWPayments account and an IPN (Instant Payment Notification)
    # secret. Leave NOWPAYMENTS_API_KEY unset to mark the provider as
    # unavailable (the registry reports it via list_providers).
    NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "").strip() or None
    NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip() or None
    # Static CNY → USDT rate used when the order currency is CNY but
    # the checkout is created in USDT. Operator sets the rate to absorb
    # the FX spread. 0 / unset = fall back to 1:1 (parity).
    _raw_rate = os.getenv("NOWPAYMENTS_CNY_USDT_RATE", "").strip()
    try:
        NOWPAYMENTS_CNY_USDT_RATE = float(_raw_rate) if _raw_rate else 0.0
    except ValueError:
        NOWPAYMENTS_CNY_USDT_RATE = 0.0

    # Days after which unused credits expire. Each credit-side
    # wallet_transaction (bonus / recharge / refund) carries its own
    # ``expires_at = created_at + CREDITS_EXPIRE_DAYS``. The daily
    # sweep_expired_credits job debits the wallet and flips the
    # expiry_debited flag. 0 disables expiration entirely (previous
    # behaviour).
    CREDITS_EXPIRE_DAYS = int(os.getenv("CREDITS_EXPIRE_DAYS", "0") or 0)

    # Stripe daily reconciliation (backend/services/stripe_reconciliation.py).
    # Recovers paid Checkout Sessions whose webhook was missed, flags
    # amount-mismatched sessions for human review.
    STRIPE_RECON_ENABLED = os.getenv("STRIPE_RECON_ENABLED", "true").lower() == "true"
    STRIPE_RECON_LOOKBACK_HOURS = int(os.getenv("STRIPE_RECON_LOOKBACK_HOURS", "48") or 48)
    STRIPE_RECON_MAX_AUTO_APPROVE = int(os.getenv("STRIPE_RECON_MAX_AUTO_APPROVE", "50") or 50)
    STRIPE_RECON_AMOUNT_TOLERANCE = float(os.getenv("STRIPE_RECON_AMOUNT_TOLERANCE", "0.01") or 0.01)

    # USDT (NOWPayments) daily reconciliation. Mirrors STRIPE_RECON_* —
    # queries NOWPayments for every pending USDT order in the lookback
    # window and recovers the ones whose IPN webhook was missed.
    USDT_RECON_ENABLED = os.getenv("USDT_RECON_ENABLED", "true").lower() == "true"
    USDT_RECON_LOOKBACK_HOURS = int(os.getenv("USDT_RECON_LOOKBACK_HOURS", "48") or 48)
    USDT_RECON_MAX_AUTO_APPROVE = int(os.getenv("USDT_RECON_MAX_AUTO_APPROVE", "50") or 50)

    SECRET_KEY = os.getenv("SECRET_KEY")
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRATION_HOURS = 24

    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

    CORS_ORIGINS = [
        origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()
    ]

    TRUSTED_PROXIES = [
        ip.strip() for ip in os.getenv("TRUSTED_PROXIES", "").split(",") if ip.strip()
    ]

    ALLOW_LEGACY_X_API_KEY = _env_bool("ALLOW_LEGACY_X_API_KEY", True)
    ALLOW_API_KEY_LOGIN = _env_bool("ALLOW_API_KEY_LOGIN", True)

    RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "1000"))

    # L13: 限流白名单。CSV 列表，命中即跳过 RateLimitMiddleware。
    # 默认包含 loopback，让本地健康检查/Docker HEALTHCHECK/监控
    # 探针不被 429 卡住。生产部署可追加内网网段，如
    # "127.0.0.1,::1,10.0.0.5,192.168.1.20"。
    RATE_LIMIT_WHITELIST_IPS = [
        ip.strip()
        for ip in os.getenv("RATE_LIMIT_WHITELIST_IPS", "127.0.0.1,::1").split(",")
        if ip.strip()
    ]

    CHANNEL_COOLDOWN_SECONDS = int(os.getenv("CHANNEL_COOLDOWN_SECONDS", "60"))
    CHANNEL_FALLBACK_MAX = int(os.getenv("CHANNEL_FALLBACK_MAX", "1"))

    CSP_LEVEL = int(os.getenv("CSP_LEVEL", "1" if IS_PRODUCTION else "0"))

    SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    ALERT_EMAIL = os.getenv("ALERT_EMAIL", "").strip()
    LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if IS_PRODUCTION else "text").lower()

    # Daily cap on the total credits any single admin can debit/credit
    # via /admin/users/{id}/wallet. Per-admin guard against runaway
    # adjustments — but with multiple admins each gets their own quota,
    # so WALLET_ADJUST_DAILY_GLOBAL_CAP caps the aggregate across all
    # admins in a 24h window to close the N×10000 bypass.
    WALLET_ADJUST_DAILY_PER_ADMIN_CAP = int(
        os.getenv("WALLET_ADJUST_DAILY_PER_ADMIN_CAP", "10000") or 10000
    )
    WALLET_ADJUST_DAILY_GLOBAL_CAP = int(
        os.getenv("WALLET_ADJUST_DAILY_GLOBAL_CAP", "20000") or 20000
    )

    # Days to retain audit_logs rows before the daily purge sweeps them.
    # 365 keeps enough history for compliance / forensics while keeping
    # the table (and backups) from growing unbounded.
    AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "365") or 365)

    # Days to retain usage_logs rows before the daily sweep deletes them.
    # usage_logs is a high-write table (one row per API request); keeping
    # it bounded preserves quota-query and aggregation performance.
    USAGE_LOG_RETENTION_DAYS = int(os.getenv("USAGE_LOG_RETENTION_DAYS", "90") or 90)

    # Days to retain read notifications before the daily sweep deletes them.
    # Only is_read=1 rows are swept — unread notifications are preserved
    # even past this window so important subscription / billing events are
    # not silently dropped.
    NOTIFICATION_RETENTION_DAYS = int(
        os.getenv("NOTIFICATION_RETENTION_DAYS", "90") or 90
    )

    # Days to retain audit_logs rows before the daily sweep deletes them.
    # Mirrors AUDIT_RETENTION_DAYS (used by services.audit.purge_old_audit_logs);
    # kept as a separate knob so sweep_old_audit_logs can be tuned
    # independently if an operator wants the two purge paths to differ.
    AUDIT_LOG_RETENTION_DAYS = int(
        os.getenv("AUDIT_LOG_RETENTION_DAYS", "365") or 365
    )

    # Days to retain conversations rows before the daily sweep deletes them.
    # Conversations are user content — confirm the product policy (and any
    # export offering) before lowering this. Default 90 aligns with
    # USAGE_LOG_RETENTION_DAYS.
    CONVERSATION_RETENTION_DAYS = int(
        os.getenv("CONVERSATION_RETENTION_DAYS", "90") or 90
    )

    @classmethod
    def is_production(cls) -> bool:
        return cls.IS_PRODUCTION

    @classmethod
    def validate_startup(cls) -> None:
        if not cls.SECRET_KEY:
            if cls.IS_PRODUCTION:
                raise RuntimeError("Missing required environment variables: SECRET_KEY")
            if os.path.isfile(_DEV_SECRET_PATH):
                try:
                    cls.SECRET_KEY = open(_DEV_SECRET_PATH).read().strip()
                except Exception:
                    pass
            if not cls.SECRET_KEY:
                cls.SECRET_KEY = os.urandom(32).hex()
                try:
                    with open(_DEV_SECRET_PATH, "w") as f:
                        f.write(cls.SECRET_KEY)
                except Exception:
                    pass

        if cls.IS_PRODUCTION:
            missing: list[str] = []
            if not cls.ENCRYPTION_KEY:
                missing.append("ENCRYPTION_KEY")
            if not cls.CORS_ORIGINS or "*" in cls.CORS_ORIGINS:
                missing.append("CORS_ORIGINS")
            if missing:
                raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        if cls.IS_PRODUCTION and cls.CSP_LEVEL < 1:
            logger.critical(
                "CSP_LEVEL=0 is not allowed in production. Forcing CSP_LEVEL=1."
            )
            cls.CSP_LEVEL = 1
