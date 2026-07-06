import hmac
import ipaddress
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend.config import Config
from backend.database import check_rate_limit, get_client_ip, init_db
from backend.routes import (
    admin,
    admin_billing,
    admin_stats,
    auth,
    billing,
    chat,
    model_pool,
    openai_compat,
    platform,
    providers,
    proxy,
    usage,
    user,
)
from backend.services.http_client import aclose_async_client
from backend.services.user_service import UserService
from backend.utils.log_safety import (
    configure_logging as _configure_safe_logging,
    safe_exc_info as _safe_exc_info,
)

# Install the redacting log formatter as early as possible so any
# subsequent error path can't leak secrets via log records.
_configure_safe_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    log_format=os.getenv("LOG_FORMAT", "json" if Config.is_production() else "text").lower(),
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    UserService.init()

    # Warn about IP trust configuration in production.
    if Config.is_production() and not Config.TRUSTED_PROXIES:
        logger.warning(
            "Production mode without TRUSTED_PROXIES: all requests will be "
            "attributed to the reverse proxy IP. Rate limiting and lockout "
            "will be per-proxy, not per-client. Set TRUSTED_PROXIES env var."
        )

    yield
    await aclose_async_client()


app = FastAPI(
    title="MiniMax API Proxy",
    version="1.0.0",
    docs_url="/docs" if os.getenv("DEBUG") else None,
    lifespan=lifespan,
)

logger = logging.getLogger("backend")


def _error_payload(detail: str, code: str, request_id: str) -> dict:
    return {"detail": detail, "code": code, "request_id": request_id}


def _csp_header_value(level: int, is_production: bool) -> str:
    base = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "img-src 'self' data: blob: https:",
        "font-src 'self' data:",
    ]

    if level <= 0:
        base.extend(
            [
                "script-src 'self' 'unsafe-inline'",
                "style-src 'self' 'unsafe-inline'",
                "connect-src 'self' http://localhost:* ws://localhost:* https://* wss://*",
            ]
        )
    elif level == 1:
        base.extend(
            [
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline'",
                "connect-src 'self'",
            ]
        )
    else:
        base.extend(
            [
                "script-src 'self'",
                "style-src 'self'",
                "connect-src 'self'",
            ]
        )

    if is_production:
        base.append("upgrade-insecure-requests")

    return "; ".join(base) + ";"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        csp_level = Config.CSP_LEVEL
        response.headers["Content-Security-Policy"] = _csp_header_value(
            csp_level, Config.is_production()
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path in {"/", "/index.html"} or (
            # The SPA catch-all serves index.html for any deep-link.
            # Mark those responses as ``no-store`` so the browser always
            # re-checks the (potentially updated) HTML shell.
            "text/html" in response.headers.get("content-type", "")
        ):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    # Maximum request body size: 10 MB. Requests that declare a larger
    # Content-Length are rejected up-front to protect against memory
    # exhaustion. Streaming endpoints (chat/send/stream) are exempt
    # because they may legitimately carry large message histories.
    MAX_BODY_BYTES = 10 * 1024 * 1024
    _BODY_EXEMPT_PREFIXES = ("/api/chat/send", "/v1/chat/completions")
    _BODY_EXEMPT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB cap for streaming endpoints

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(("/docs", "/openapi.json", "/health", "/api/public", "/metrics")):
            return await call_next(request)

        # Reject oversized request bodies before reading them.
        if not request.url.path.startswith(self._BODY_EXEMPT_PREFIXES):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > self.MAX_BODY_BYTES:
                        request_id = getattr(request.state, "request_id", str(uuid4()))
                        return JSONResponse(
                            status_code=413,
                            content=_error_payload("请求体过大", "PAYLOAD_TOO_LARGE", request_id),
                        )
                except ValueError:
                    pass
        else:
            # Streaming-exempt endpoints still get a generous cap to prevent
            # memory exhaustion from maliciously large payloads.
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > self._BODY_EXEMPT_MAX_BYTES:
                        request_id = getattr(request.state, "request_id", str(uuid4()))
                        return JSONResponse(
                            status_code=413,
                            content=_error_payload("请求体过大", "PAYLOAD_TOO_LARGE", request_id),
                        )
                except ValueError:
                    pass

        client_ip = get_client_ip(request)

        # L13: 白名单 IP 跳过限流（loopback 默认在内，让 Docker
        # HEALTHCHECK / 内部监控探针不被 429 卡住）。一次性把分钟
        # 与小时桶都跳过，避免后续 check_rate_limit 仍写入计数行。
        if Config.RATE_LIMIT_WHITELIST_IPS and client_ip in Config.RATE_LIMIT_WHITELIST_IPS:
            return await call_next(request)

        _is_authenticated = bool(
            request.cookies.get("mm_session")
            or request.cookies.get("mm_admin_session")
            or request.headers.get("Authorization")
            or request.headers.get("X-API-Key")
        )
        _rate_multiplier = 2 if _is_authenticated else 1

        allowed_min, remaining_min = check_rate_limit(
            client_ip, "ip:60", Config.RATE_LIMIT_PER_MINUTE * _rate_multiplier, 60
        )
        if not allowed_min:
            request_id = getattr(request.state, "request_id", str(uuid4()))
            return JSONResponse(
                status_code=429,
                content=_error_payload("请求过于频繁，请稍后再试", "RATE_LIMITED", request_id),
            )

        allowed_hour, _remaining_hour = check_rate_limit(
            client_ip, "ip:3600", Config.RATE_LIMIT_PER_HOUR * _rate_multiplier, 3600
        )
        if not allowed_hour:
            request_id = getattr(request.state, "request_id", str(uuid4()))
            return JSONResponse(
                status_code=429,
                content=_error_payload("请求过于频繁，请稍后再试", "RATE_LIMITED", request_id),
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining_min)
        response.headers["X-RateLimit-Limit"] = str(Config.RATE_LIMIT_PER_MINUTE)
        return response


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        raw = request.headers.get("X-Request-ID")
        if raw and len(raw) <= 64 and all(ch.isalnum() or ch in "-_" for ch in raw):
            request_id = raw
        else:
            request_id = str(uuid4())

        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", str(uuid4()))

    if isinstance(exc.detail, str):
        detail = exc.detail
    else:
        detail = "请求失败"

    if exc.status_code == 401:
        code = "UNAUTHORIZED"
    elif exc.status_code == 403:
        code = "FORBIDDEN"
    elif exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 429:
        code = "RATE_LIMITED"
    elif exc.status_code == 422:
        code = "VALIDATION_ERROR"
    elif exc.status_code == 500:
        code = "INTERNAL_ERROR"
        detail = "服务器内部错误"
    elif exc.status_code >= 500:
        code = "UPSTREAM_ERROR"
        detail = "上游服务错误"
    else:
        code = "BAD_REQUEST"

    return JSONResponse(
        status_code=exc.status_code, content=_error_payload(detail, code, request_id)
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    # Log the full Pydantic error breakdown server-side for debugging,
    # but do NOT echo field-level details back to the client. Detailed
    # validation errors leak schema information (field names, allowed
    # types, regex constraints) that makes parameter fuzzing and
    # reverse-engineering easier for an attacker. The client gets a
    # generic message + the request_id so support can correlate.
    try:
        logger.warning(
            "validation_error request_id=%s path=%s errors=%s",
            request_id,
            request.url.path,
            exc.errors(),
        )
    except Exception:
        # Logging must never break the response path.
        pass
    return JSONResponse(
        status_code=422,
        content=_error_payload("请求参数校验失败", "validation_error", request_id),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    logger.exception(
        "Unhandled error request_id=%s type=%s msg=%s",
        request_id,
        type(exc).__name__,
        _safe_exc_info(exc),
    )
    return JSONResponse(
        status_code=500, content=_error_payload("服务器内部错误", "INTERNAL_ERROR", request_id)
    )


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestIdMiddleware)

cors_allow_all = "*" in Config.CORS_ORIGINS
if Config.is_production():
    cors_allow_origins = [o for o in Config.CORS_ORIGINS if o != "*"]
    cors_allow_credentials = True
else:
    cors_allow_origins = ["*"] if cors_allow_all else Config.CORS_ORIGINS
    cors_allow_credentials = not cors_allow_all

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_credentials=cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Request-ID",
        "X-CSRF-Token",
    ],
    expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Limit"],
    max_age=3600,
)

# ---------------------------------------------------------------------------
# /metrics 端点（M40 修复）
# ---------------------------------------------------------------------------
# Prometheus 指标默认对外暴露会泄露路由分布、QPS、延迟分布等运营敏感数据，
# 必须加一层鉴权。优先用 ``METRICS_TOKEN`` 环境变量做 Bearer token 校验；
# 未配置时回退到只允许内网 IP（loopback + RFC1918）访问，与 /health/ready
# 的 Nginx 层 IP 限制保持一致的边界。
_METRICS_TOKEN = os.getenv("METRICS_TOKEN", "").strip()
_metrics_instrumentator = None
try:
    from prometheus_fastapi_instrumentator import Instrumentator

    # instrument() 收集指标；不再调用 .expose() —— 改为下方自定义端点带鉴权暴露。
    _metrics_instrumentator = Instrumentator().instrument(app)
except ImportError:
    logger.warning("prometheus-fastapi-instrumentator not installed, /metrics disabled")


def _is_internal_ip(ip_str: str) -> bool:
    """判断 IP 是否属于内网（loopback 或 RFC1918）。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_loopback():
        return True
    if ip.version != 4:
        # IPv6 没有 RFC1918 等价物；只放行 loopback。
        return False
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        if ip in ipaddress.ip_network(cidr):
            return True
    return False


if _metrics_instrumentator is not None:

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request):
        """Prometheus 指标端点。需要 METRICS_TOKEN 或内网 IP。"""
        if _METRICS_TOKEN:
            auth_header = request.headers.get("Authorization", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[len("Bearer "):].strip()
            if not token or not hmac.compare_digest(token, _METRICS_TOKEN):
                request_id = getattr(request.state, "request_id", str(uuid4()))
                return JSONResponse(
                    status_code=401,
                    content=_error_payload("Unauthorized", "UNAUTHORIZED", request_id),
                )
        else:
            # 未配置 token：回退到内网 IP allowlist。
            client_ip = get_client_ip(request)
            if not _is_internal_ip(client_ip):
                request_id = getattr(request.state, "request_id", str(uuid4()))
                return JSONResponse(
                    status_code=403,
                    content=_error_payload("Forbidden", "FORBIDDEN", request_id),
                )
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

            return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
        except Exception as exc:
            logger.warning("metrics generation failed: %s", exc)
            request_id = getattr(request.state, "request_id", str(uuid4()))
            return JSONResponse(
                status_code=500,
                content=_error_payload("metrics unavailable", "INTERNAL_ERROR", request_id),
            )

Config.validate_startup()

# Note: init_db() and UserService.init() are called in the FastAPI lifespan
# (see ``lifespan`` above). Calling them here as well would be redundant
# and is intentionally avoided to prevent double-initialization.

app.include_router(proxy.router, tags=["Proxy"])
app.include_router(admin.router, prefix="/api", tags=["Admin"])
app.include_router(auth.router, prefix="/api", tags=["Auth"])
app.include_router(user.router, prefix="/api", tags=["User"])
# Chat surface: shared between admin and regular users. The router
# lives at ``backend/routes/chat.py`` and accepts either session
# cookie, so a user (or admin) on ``/chat`` can talk to the proxy
# without bouncing through ``/v1/chat``.
app.include_router(chat.router, prefix="/api", tags=["Chat"])
# v2: commercial surfaces — wallets, orders, pricing, OpenAI-compatible gateway
app.include_router(billing.router, prefix="/api", tags=["Billing"])
app.include_router(admin_billing.router, prefix="/api", tags=["AdminBilling"])
app.include_router(admin_stats.router, prefix="/api", tags=["AdminStats"])
app.include_router(usage.router, prefix="/api", tags=["Usage"])
app.include_router(openai_compat.router, tags=["OpenAI-Compat"])
app.include_router(platform.router, prefix="/api", tags=["Platform"])
app.include_router(providers.router, prefix="/api", tags=["Providers"])
# User-defined model pool: per-user upstream credentials + unified sk-ump_ key.
# Mounted under /api so it sits next to the other authenticated user routes
# (the router itself defines /user/model-pools and /user/model-pool-keys).
app.include_router(model_pool.router, prefix="/api", tags=["ModelPool"])

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
FRONTEND_DIST_DIR = os.path.join(FRONTEND_DIR, "dist")
FRONTEND_INDEX = (
    os.path.join(FRONTEND_DIST_DIR, "index.html")
    if os.path.exists(FRONTEND_DIST_DIR)
    else os.path.join(FRONTEND_DIR, "index.html")
)
FRONTEND_DIST_AVAILABLE = os.path.isfile(FRONTEND_INDEX)

if os.path.exists(os.path.join(FRONTEND_DIST_DIR, "assets")):
    app.mount(
        "/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST_DIR, "assets")), name="assets"
    )


# ---------------------------------------------------------------------------
# SPA (Single-Page Application) fallback
# ---------------------------------------------------------------------------
# The React app uses BrowserRouter, so it owns the URL space below the
# server's API surface. Concretely this means the user can hit the
# backend at ``/admin``, ``/admin/users``, ``/chat``, ``/login`` (or
# anything else we add later) by either *navigating* inside the app
# (cheap — the SPA already has the HTML) or by *refreshing* the page
# (expensive — the browser asks the server for that path, and the
# server must respond with the SPA shell so React Router can take over).
#
# Without the catch-all below, a hard refresh on ``/admin`` falls into
# the implicit 404 handler and returns ``{"detail": "Not Found"}`` —
# which is what the user was seeing before this fix.
#
# The catch-all:
#   * only handles GET (POST/PUT/DELETE are always API calls and must
#     404 cleanly so we don't accidentally swallow them as HTML),
#   * only handles paths without a file extension (so a request for
#     ``/robots.txt`` still gets a real 404 instead of an HTML body),
#   * is registered *after* the API routers, so it never shadows an
#     actual API endpoint.
# ---------------------------------------------------------------------------
# NOTE: the SPA-catch-all helpers (``_API_PREFIXES`` / ``_is_spa_route``)
# are defined just above the ``@app.get("/")`` handler below. They are
# only used by the catch-all route at the bottom of this file, so the
# definitions are co-located with the only place that needs them.


# ---------------------------------------------------------------------------
# SPA catch-all helpers
# ---------------------------------------------------------------------------
#
# The catch-all route at the bottom of this file only fires for paths
# none of the API routers claimed. Even so, we want a second line of
# defence: a path with a file extension (``/robots.txt``, ``/logo.png``)
# is almost certainly a request for a real file, and an API-shaped
# prefix (``/api/``, ``/openapi.json``) is an API call that we missed
# (which is itself a bug, but a clean 404 is better than a leaked HTML
# body). Only paths that look like SPA deep-links (``/chat``,
# ``/admin/users``) should fall through to the shell.
#
# The previous version of this file declared these three names twice
# (once above the catch-all and once inside it); the refactor that
# "de-duplicated" them ended up deleting both copies. Restoring them
# here so the catch-all has something to call.

_API_PREFIXES = (
    "/api/",
    "/assets/",
    "/docs",
    "/openapi.json",
    "/health",
    "/metrics",
    "/favicon.ico",
    "/v1/",
)


def _is_spa_route(full_path: str) -> bool:
    """True if the GET catch-all should serve the SPA shell for ``full_path``."""
    if not full_path:
        return True
    for prefix in _API_PREFIXES:
        if full_path == prefix.rstrip("/") or full_path.startswith(prefix):
            return False
    # Reject anything with a file extension so requests for real
    # files (e.g. ``/robots.txt``, ``/sitemap.xml``) get a 404
    # instead of a hidden HTML body.
    base = full_path.rsplit("/", 1)[-1]
    if "." in base and not base.startswith("."):
        return False
    return True


@app.get("/")
async def root():
    if not FRONTEND_DIST_AVAILABLE:
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend bundle not built. Run `npm run build` in frontend/."},
        )
    return FileResponse(FRONTEND_INDEX)


_readiness_cache: dict = {"ts": 0, "result": None, "status_code": 200}
_readiness_cache_ttl = 10


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/health/live")
async def health_live():
    """Liveness probe — cheap, public, no infra detail.

    Mirrors ``/health``. Use this for load-balancer / k8s liveness
    checks; keep ``/health/ready`` (which leaks DB/Redis/provider
    state) restricted to internal networks at the Nginx layer.
    """
    return {"status": "ok", "timestamp": time.time()}


@app.get("/health/ready")
async def health_ready(request: Request):
    # Defence-in-depth: the Nginx layer already restricts /health/ready
    # to RFC-1918 + loopback, but the readiness payload leaks DB /
    # Redis / provider state and shouldn't be reachable from the
    # public internet even if Nginx is misconfigured or bypassed.
    # Mirrors the /metrics gate's allowlist (10/8, 172.16/12, 192.168/16, 127/8).
    client_ip = get_client_ip(request)
    if not _is_internal_ip(client_ip):
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return JSONResponse(
            status_code=403,
            content=_error_payload("Forbidden", "FORBIDDEN", request_id),
        )

    now = time.time()
    if (
        _readiness_cache["result"] is not None
        and (now - _readiness_cache["ts"]) < _readiness_cache_ttl
    ):
        return JSONResponse(
            status_code=_readiness_cache["status_code"],
            content=_readiness_cache["result"],
        )

    checks: dict = {}
    critical_ok = True

    try:
        from backend.database import get_db

        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            checks["database"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        critical_ok = False

    try:
        redis_url = os.getenv("REDIS_URL", "").strip()
        if redis_url:
            from backend.services.redis_service import RedisService

            client = RedisService.get_client()
            client.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_configured"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        critical_ok = False

    try:
        from backend.services.health_service import get_all_providers_health

        provider_health = get_all_providers_health()
        down_providers = [p for p in provider_health if not p.get("up", True)]
        checks["providers"] = {
            "total": len(provider_health),
            "down": len(down_providers),
            "down_list": [p["provider"] for p in down_providers],
        }
    except Exception as exc:
        checks["providers"] = f"error: {type(exc).__name__}"

    status_code = 200 if critical_ok else 503
    result = {
        "status": "ok" if critical_ok else "degraded",
        "timestamp": time.time(),
        "checks": checks,
    }

    _readiness_cache["ts"] = time.time()
    _readiness_cache["result"] = result
    _readiness_cache["status_code"] = status_code

    return JSONResponse(status_code=status_code, content=result)


@app.get("/health/pools")
async def health_pools(request: Request, _session: dict = Depends(admin.require_admin_session)):
    """Return the database connection pool statistics.

    Useful for the load balancer / autoscaler to detect connection
    exhaustion before it cascades into request failures.
    Requires admin authentication.
    """
    try:
        from backend.utils.db_pool import get_pool

        return get_pool().stats()
    except Exception:
        return {"error": "internal_error"}


@app.get("/api/public/status")
async def public_status():
    # ``admin_initialized`` tells the SPA whether to show the init
    # wizard on first run. Keeping it on the public status endpoint
    # means the page can render the right UI without an authenticated
    # round-trip.
    #
    # M41 修复：原先还暴露了 ``allow_legacy_x_api_key`` 和
    # ``allow_api_key_login`` 两个 feature flag。这两个是平台安全
    # 策略开关，未登录的访客没有理由知道当前是否允许 API Key 登录
    # 或 X-API-Key 透传 —— 暴露给攻击者便于探测攻击面。已确认前端
    # ``frontend/src/lib/api.js`` 不消费这两个字段，安全移除。
    # 如果未来 SPA 需要根据这些 flag 调整 UI，应改为已认证端点
    # （例如 /api/user/me/features）获取。
    try:
        from backend.database import admin_exists

        admin_initialized = bool(admin_exists())
    except Exception:
        admin_initialized = True  # fail closed: assume initialised
    return {
        "status": "ok",
        "version": app.version,
        "env": Config.ENV,
        "admin_initialized": admin_initialized,
    }


@app.get("/api/public/pricing")
async def public_pricing(provider: str = None):
    """Return the **effective** pricing for every (provider, model).

    The "effective" price is the admin custom row when present, else
    the official default. This is what the public frontend uses to
    show the user the price they'll actually be charged — so the
    defaults are always the platform's official pricing, and admin
    customisations are layered on top.
    """
    from backend.database import list_effective_pricing

    rows = list_effective_pricing(provider=provider)
    return {
        "count": len(rows),
        "currency": "credits",
        "unit": "1k_tokens",
        "items": rows,
    }


# ---------------------------------------------------------------------------
# /api/client-errors — frontend error reporting endpoint (M6+M7)
# ---------------------------------------------------------------------------
# The React ErrorBoundary POSTs unhandled render errors + window.onerror
# + unhandledrejection events here so we have server-side visibility into
# client-side crashes. The endpoint is unauthenticated (the boundary may
# fire before login) but rate-limited per-IP at the middleware layer.
#
# Design notes:
#   * Payload is capped at 16 KB to prevent a hostile client from
#     using the endpoint as a log-injection vector.
#   * We log at WARNING level — a single client error may be benign,
#     but a sudden burst is a real signal.
#   * We do NOT echo the payload back. Just a 204.
#   * We do NOT persist to the DB — log volume is unbounded and
#     operators already have log aggregation. If we ever need
#     structured client-error analytics we can add a dedicated table.
_CLIENT_ERROR_MAX_BYTES = 16 * 1024
_client_error_logger = logging.getLogger("client_errors")


@app.post("/api/client-errors", include_in_schema=False)
async def client_errors(request: Request):
    """Receive a client-side error report from the React ErrorBoundary.

    Body shape (best-effort, all fields optional):
        {
          "type": "react" | "window.onerror" | "unhandledrejection",
          "message": str,
          "stack": str,
          "componentStack": str,   # React-only
          "url": str,
          "ts": int
        }
    """
    request_id = getattr(request.state, "request_id", str(uuid4()))
    client_ip = get_client_ip(request)

    # Read the body with a hard cap. A malicious client could try to
    # flush a 100 MB stack trace; reject before parsing.
    raw = await request.body()
    if len(raw) > _CLIENT_ERROR_MAX_BYTES:
        return JSONResponse(
            status_code=413,
            content=_error_payload("请求体过大", "PAYLOAD_TOO_LARGE", request_id),
        )

    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    err_type = str(payload.get("type") or "unknown")[:64]
    message = str(payload.get("message") or "")[:2000]
    stack = str(payload.get("stack") or "")[:8000]
    component_stack = str(payload.get("componentStack") or "")[:8000]
    url = str(payload.get("url") or "")[:500]
    user_agent = request.headers.get("user-agent", "")[:300]

    _client_error_logger.warning(
        "client_error request_id=%s ip=%s type=%s url=%s message=%s",
        request_id,
        client_ip,
        err_type,
        url,
        message,
    )
    # Stack traces are logged at DEBUG to keep the WARNING line
    # scannable; operators who need the full stack can grep DEBUG
    # for the same request_id.
    _client_error_logger.debug(
        "client_error_stack request_id=%s stack=%s component_stack=%s ua=%s",
        request_id,
        stack,
        component_stack,
        user_agent,
    )

    return JSONResponse(status_code=204, content=None)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers auto-request /favicon.ico; return 204 instead of 404.

    Returning a real icon would be nicer, but the cost (extra asset,
    extra bytes per page load) isn't worth it for a dev console. The
    204 is silent and keeps the dev tools clean.
    """
    return Response(status_code=204)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """Serve the SPA shell for any non-API, non-asset GET path.

    This is the linchpin of the refresh-on-deep-link fix. See the
    comment block above for the full design.
    """
    if not _is_spa_route(full_path):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if not FRONTEND_DIST_AVAILABLE:
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend bundle not built. Run `npm run build` in frontend/."},
        )
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    ssl_keyfile = os.getenv("SSL_KEYFILE")
    ssl_certfile = os.getenv("SSL_CERTFILE")

    if ssl_keyfile and ssl_certfile:
        uvicorn.run(
            "backend.main:app",
            host=host,
            port=port,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            reload=bool(os.getenv("DEBUG")),
        )
    else:
        uvicorn.run("backend.main:app", host=host, port=port, reload=bool(os.getenv("DEBUG")))
