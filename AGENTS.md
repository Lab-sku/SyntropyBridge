# AGENTS.md — API 中转平台

This file provides guidance to the AI agent when working with code in this repository.

Multi-provider AI API proxy platform with user management, quotas, credit-based billing, subscriptions, payment integration (Stripe + USDT via NOWPayments), and a web admin dashboard. FastAPI backend + React/Vite frontend, SQLite database.

> **Recent (Phase-1 + Phase-2 + Phase-3) audit-driven changes** — see the dedicated sections near the bottom for the 15 business-logic fixes (Phases 1+2) and 7 permission-hardening fixes (Phase 3) landed since `2ac8a5d`. Highlights: per-user token-reservation ledger (migration 29), per-credit-entry expiration (migration 30), USDT payment integration, Stripe ↔ orders daily reconciliation, downgrade-on-cancel materialisation, low-balance banner, `DEFAULT_QUOTA_5H` bumped 500→3000, super-admin gate on reveal-API-key (migration 31), CSRF on every billing write endpoint, shared `admin_auth` module.

## Run commands

```bash
# Backend (from repo root)
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Frontend dev server (proxies /api, /v1 to localhost:8000)
cd frontend && npm run dev

# Frontend build (served by FastAPI as SPA)
cd frontend && npm ci && npm run build

# Daily worker (subscription lifecycle + soft-delete purge + credits sweep + Stripe recon)
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_daily_jobs()"

# Hourly worker (expired orders, pending_payment subs, expired subs, upcoming renewals, reservation sweep)
python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_hourly_jobs()"

# Stripe reconciliation (standalone — also runs inside run_daily_jobs)
python -c "from backend.services.stripe_reconciliation import StripeReconciliation; print(StripeReconciliation.run_daily_reconciliation())"
```

## Test

```bash
# Run the real pytest suite (backend/tests/)
pytest backend/tests/

# Single test
pytest backend/tests/test_usage_quota.py -k test_name

# Root-level test_*.py files are ad-hoc scripts that need a running server — NOT part of the pytest suite
```

Tests use a temp SQLite DB (conftest.py patches `DATABASE_PATH`). Each test gets a fresh DB + reset connection pool. Do not add `init_db()` calls in tests — the fixture handles schema.

- **Schema sync**: `backend/tests/conftest.py` contains a parallel copy of the schema. When adding a migration in `backend/database.py`, update conftest.py to match — tests do NOT run the migrations. Migrations 29 and 30 are *idempotent* (PRAGMA table_info guard) precisely because conftest's CREATE TABLE already includes the new columns.
- **Windows**: Close the `DatabasePool` singleton before unlinking `.db` files in tests — stale handles will break `os.unlink`.
- **Test run time**: the full suite is ~7 minutes (282 tests as of the Phase-2 audit). Use `-k` liberally during development.

## Lint

```bash
# Ruff is configured (ruff.toml) but not enforced by CI
ruff check backend/
ruff format backend/
```

No CI pipeline, no linter, no formatter enforced. Run `pytest backend/tests/` and `cd frontend && npm run build` before declaring a change done, or use the `/verify` skill.

## Auth

Session-cookie based, not Bearer tokens. Two cookie names: `mm_admin_session` (admin) and `mm_session` (user). A `mm_csrf` cookie is set alongside; all state-changing requests must send its value back as `X-CSRF-Token`. The frontend `api.js` handles this automatically. When writing ad-hoc scripts or tests that bypass the fixture helpers, include both the cookie and the CSRF header on POST/PUT/PATCH/DELETE.

- **Session store**: Server-side in `sessions` table. Sliding-window refresh, absolute timeout, UA binding.
- **Per-user concurrent-session cap**: `create_session()` evicts the user's oldest excess sessions when `plans.max_concurrent` is > 0. Admins and users whose plan has lapsed (`plan_id IS NULL`, free-fallback path) are exempt. This is a security guard, not a billing control.
- **Password hashing**: PBKDF2-HMAC-SHA256, 600k iterations. Legacy 100k format transparently upgraded.
- **Password policy**: Min 12 chars, must contain 3 of 4 character classes, rejects username-in-password.
- **Brute-force protection**: `lockout.py` — 8 failures in 15 min triggers lockout. Auto-resets on success.
- **Cookie flags**: HttpOnly, Secure (production only), SameSite=Lax. CSRF cookie is NOT HttpOnly (JS reads it).

## Architecture overview

```
Internet
   |
[Nginx]              TLS termination, rate limiting, SSE support (no buffering)
   |
[Uvicorn/FastAPI]    Single worker (SQLite constraint), serves SPA from frontend/dist/
   |
[SQLite WAL DB]      36 inline migrations, connection pool singleton (db_pool.py)
   |
[Worker]             Systemd timer runs run_daily_jobs() + run_hourly_jobs():
                       • subscription expiry + renewals + upcoming reminders
                       • soft-delete 30-day purge (UserService.purge_soft_deleted_users)
                       • credits expiry sweep (database.sweep_expired_credits)
                       • token-reservation TTL sweep (database.purge_expired_reservations)
                       • Stripe reconciliation (StripeReconciliation.run_daily_reconciliation)
```

## Backend structure

### Entry & middleware

- **`backend/main.py`** — FastAPI app factory, lifespan, route registration (12 routers), SPA fallback
- **Middleware stack** (inner to outer): RequestIdMiddleware, SecurityHeadersMiddleware, RateLimitMiddleware, CORSMiddleware
- **Exception handlers**: Structured JSON errors `{detail, code, request_id}` for HTTPException, ValidationError, and unhandled exceptions
- **SPA fallback**: `_is_spa_route()` serves `index.html` for extensionless GET paths that don't match API prefixes (`/api/`, `/assets/`, `/v1/`, `/docs`, `/health`)

### Configuration

- **`backend/config.py`** — All settings from env vars via `Config` class. `.env` loaded by python-dotenv. See the **Environment variables reference** section for the full list including the Phase-2 additions (`STRIPE_CURRENCY`, `NOWPAYMENTS_*`, `CREDITS_EXPIRE_DAYS`, `STRIPE_RECON_*`, `SOFT_DELETE_RETENTION_DAYS`, `DEFAULT_QUOTA_*`).
- **`backend/security.py`** — Static `Security` class: Fernet encryption (double-base64 wrapper), PBKDF2 password hashing, key generation
- **`backend/session.py`** — Server-side session management, cookie handling, sliding-window refresh, per-user concurrent-session cap

### Database

- **`backend/database.py`** — SQLite with WAL mode, **36** numbered inline migrations tracked in `schema_migrations` table. No Alembic. Migrations 29–36 are idempotent (PRAGMA table_info guard) so they can run against the parallel test schema.
- **`backend/utils/db_pool.py`** — Bounded connection pool singleton (16 connections), thread-local checkout, TTL recycling
- **Pragmas**: WAL journal, NORMAL sync, foreign keys ON, 5s busy timeout, 20 MB cache, MEMORY temp, 256 MB mmap
- **Credit-granting helper**: `grant_credits(user_id, amount, tx_type, *, related_type, related_id, note, expires_at="_auto")` is the single entry point every credit-side wallet change should use. It atomically increases `balance` + `total_recharged`, writes the ledger row with the operator-configured `CREDITS_EXPIRE_DAYS` horizon, and returns the post-credit balance. The legacy `update_wallet()` now also accepts an `expires_at` parameter for callers that still need its +/- delta interface.
- **Token-reservation helpers**: `reserve_tokens(user_id, tokens, ttl_seconds=300)`, `get_active_reservation(user_id)`, `release_reservation(user_id)`, `purge_expired_reservations()`. Fronted by `quota_service.reserve_quota_reservation` / `release_quota_reservation` (the public API for route handlers).

### Database schema (key tables)

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `users` | User accounts | username, email, password_hash, api_key, quota_5h/week/month, monthly_budget, plan_id, is_active |
| `usage_logs` | Per-request usage | user_id, model, provider, tokens, cost_credits, response_time_ms, status_code, channel_id, token_id |
| `usage_rollups` | Aggregated counters | user_id, bucket_minute, request_count, tokens |
| `sessions` | Server-side sessions | session_id PK, role, admin_id/user_id, csrf, expires_at, user_agent, ip_address |
| `admin_users` | Admin accounts | username, password_hash, totp_secret, **is_super_admin** |
| `tokens` | User API tokens (mmx_tk_) | user_id, token_hash, token_prefix, allowed_models/ips, rate limits, expires_at |
| `api_keys` | User API keys (sk-) | user_id, key_hash, key_prefix, monthly limits, allowed/denied models, allowed_ips |
| `channels` | Multi-key rotation | provider, api_key_encrypted, weight, cooldown_until, is_active |
| `wallets` | User wallets | user_id PK, balance (CHECK ≥ 0), frozen, total_recharged, total_consumed, auto_recharge config |
| `wallet_transactions` | Wallet ledger | user_id, type (`recharge` / `consume` / `bonus` / `refund` / `admin_adjust` / `reserve` / `expiry` / `renew` / `upgrade`), amount, balance_after, related_type/id, **expires_at** (TIMESTAMP NULL), **expiry_debited** (0/1) |
| `plans` | Subscription tiers | code, monthly_price, monthly_credits, discount_rate, rate_limit_rpm/tpm, max_api_keys, max_concurrent, features |
| `subscriptions` | User subscriptions | user_id, plan_id, status, credits_used, auto_renew, pending_plan_id, cancelled_at |
| `orders` | Top-up orders | order_no, user_id, amount, credits, bonus_credits, payment_method, payment_provider (`stripe` / `usdt` / `admin_grant` / `balance` / `auto_recharge`), payment_session_id, payment_reference, status, note |
| `model_pricing` | Per-model pricing | provider, model_id, input/output_price_per_1k, tier, is_custom |
| `promo_codes` / `promo_code_usage` | Promotions | type (discount/bonus), value, usage limits |
| `redeem_codes` / `redeem_code_usage` | Redemption codes | type (credits/plan_days/plan_upgrade), limits |
| `notifications` | In-app notifications | user_id, type, title, content, is_read, metadata |
| `audit_logs` | Admin audit trail | actor, action, target, ip, user_agent, metadata |
| `custom_providers` | User-defined providers | slug, api_base, api_keys (encrypted), is_enabled |
| `conversations` | Chat history | user_id, session_id, role, content, model, title |
| `models` | Cached model catalog | model_id, provider, context_length, is_active |
| `provider_keys` | Provider-side key pool | provider, key_hash, weight, health counters |
| `provider_health` | Provider SLA metrics | status, latency p50/p95, success_rate_1h |
| `rate_limits` | IP rate limit counters | identifier, limit_type, request_count, window_start |
| `auth_failures` | Lockout tracking | identifier, scope, failure_count |
| `idempotency_keys` | Retry protection | key, method, route, response |
| **`token_reservations`** (migration 29 + migration 32) | Per-request in-flight token reservation | **user_id**, request_id, reserved_tokens, reserved_until (TIMESTAMP), created_at. PRIMARY KEY (user_id, request_id) — multi-row design (migration 32) so concurrent requests from the same user no longer collapse. The hot-path quota gate reads the SUM of all un-expired rows via `get_quota_snapshot.reserved_tokens`. |

### Providers (`backend/providers/`)

Abstract `Provider` base class with registry pattern. `OpenAICompatibleProvider` shared base for `/v1/chat/completions` APIs.

| Provider file | Provider name | Notes |
|---------------|--------------|-------|
| `openai.py` | openai | OpenAI |
| `anthropic.py` | anthropic | Claude (custom API) |
| `google.py` | google | Gemini (custom API) |
| `minimax.py` | minimax | MiniMax (custom API) |
| `deepseek.py` | deepseek | DeepSeek |
| `moonshot.py` | moonshot | Moonshot/Kimi |
| `zhipu.py` | zhipu | Zhipu GLM |
| `aliyun.py` | aliyun | Aliyun DashScope (Qwen) |
| `doubao.py` | doubao | ByteDance Ark (Doubao) |
| `nvidia.py` | nvidia | NVIDIA NIM |
| `openrouter.py` | openrouter | OpenRouter |
| `siliconflow.py` | siliconflow | SiliconFlow |
| `mimo.py` | mimo | MiMo |
| `openai_compatible.py` | (shared) | Base for OpenAI-compatible providers |
| `base.py` | — | Abstract class, registry, `detect_provider_from_model()` |

**Provider detection** (`detect_provider_from_model()`): custom: prefix > NVIDIA namespaces > model_prefix matching > keyword map > default (minimax).

### Services (`backend/services/`)

| Service | Purpose |
|---------|---------|
| `proxy_service.py` | Core proxy: channel resolution, circuit breaker, retry with fallback, streaming SSE |
| `channel_service.py` | Multi-key rotation: weighted round-robin, cooldown on failure |
| `quota_service.py` | 6-dimensional quota gate (`assert_request_allowed`) + reservation lifecycle (`reserve_quota_reservation` / `release_quota_reservation`). The hot path counts `used + reserved` against each window to close the concurrent-request double-spend window. |
| `billing_service.py` | Credit system (1 CNY = 100 credits), cost quoting, atomic charging with idempotency. `reconcile_stream_reserve()` settles the streaming pre-reserve against actual usage. |
| `subscription_service.py` | Lifecycle: upgrade (prorate), downgrade (deferred), cancel, renew, daily/hourly cron jobs. `process_expiry()` now materialises pending downgrades when auto_renew is off. `run_daily_jobs()` runs process_expiry, renewals, soft-delete purge, credits sweep, and Stripe reconciliation. `run_hourly_jobs()` runs expired orders, pending_payment subs, reservation sweep. |
| `order_service.py` | Order CRUD, promo codes, approve/reject/refund, redeem code execution. `approve_order()` stamps `expires_at` on the recharge row via `_credits_expire_at()`. `_maybe_cancel_subscription_on_refund()` rolls back any subscription the refunded order activated. |
| `stripe_reconciliation.py` | **New.** Daily batch: pulls every paid Stripe Checkout Session from the last `STRIPE_RECON_LOOKBACK_HOURS`, matches to local orders by `order_no` (metadata) or `payment_session_id`, and recovers missed webhooks. Decision tree: paid+pending → auto-approve (capped by `STRIPE_RECON_MAX_AUTO_APPROVE`); paid+amount-mismatch → `pending_review` + notify; paid+terminal-local → `late_payment` audit; no-local-match → `orphan` audit. USDT orders get the configured CNY→USDT rate applied before comparison. |
| `payment/base.py` | Abstract payment provider (`PaymentProvider` ABC) with checkout session model. Three abstract methods: `create_checkout`, `verify_webhook`, `query_status`. |
| `payment/__init__.py` | Lazy registry (`_PROVIDER_CLASSES`): stripe, alipay, wechat, **usdt**. `get_provider(name)` returns a singleton, `list_providers()` reports status, `reset_providers()` is the test hook. |
| `payment/stripe_provider.py` | Stripe Checkout Sessions + webhook signature verification |
| `payment/alipay_provider.py` | Alipay stub — all methods raise `NotImplementedError` |
| `payment/wechat_provider.py` | WeChat Pay stub — all methods raise `NotImplementedError` |
| `payment/usdt_provider.py` | **New.** NOWPayments-backed USDT / crypto provider. HMAC-verified IPN webhook, `/v1/payment` checkout creation, `/v1/payment/{id}` status query. The checkout URL embeds `payment_id`, `address`, `pay_amount`, `pay_currency`, `network` as query params so the frontend can render the inline deposit UI without a second round-trip. |
| `token_service.py` | mmx_tk_ tokens: SHA-256 hashed, per-token restrictions, resolution to user |
| `api_key_service.py` | sk- keys: issue, rotate, revoke, hash lookup |
| `auth_service.py` | API key resolution for OpenAI-compat gateway (api_keys table + legacy users.api_key) |
| `user_service.py` | User CRUD, usage stats, password management, `auto_activate_free_plan(user_id)` (centralised — called from both `auth.register` and admin `create_user`), `purge_soft_deleted_users(retention_days)` (daily worker hook) |
| `usage_service.py` | Aggregation: daily/monthly, by-model/provider, top models/users, CSV export |
| `key_pool.py` | Provider-side key pool: weighted round-robin, 5-min cooldown, health tracking |
| `custom_providers.py` | Admin CRUD for custom providers, SSRF protection, dynamic class registration |
| `model_aggregator.py` | Parallel fetch from all providers, cache in models table + Redis (5 min TTL) |
| `email_service.py` | SMTP with templates (verification, password reset, orders, subscriptions). Audit log fallback in dev |
| `notification_service.py` | In-app notifications CRUD (order events, low balance, subscription lifecycle) |
| `health_service.py` | Per-provider sliding window: latency p50/p95, success rate. UP/DOWN determination |
| `http_client.py` | Shared httpx.AsyncClient singleton (connect=10s, read=30s, 50 max connections) |
| `redis_service.py` | Optional Redis for caching/verification codes. Graceful degradation to SQLite |
| `lockout.py` | Brute-force protection in auth_failures table |
| `audit.py` | Audit log writes and filtered reads. `log_action(actor_id, actor_type, action, target_type, target_id, details, ip_address)` |
| `alert_service.py` | Operator alerts (Slack/webhook) for critical events |
| `totp_service.py` | Admin 2FA TOTP generation and verification |

### Utils (`backend/utils/`)

| Util | Purpose |
|------|---------|
| `circuit_breaker.py` | Per-provider in-memory circuit breaker (5 failure threshold, 30s cooldown, half-open probe) |
| `idempotency.py` | SQLite-backed idempotency store (24h retention). Prevents double-debit on SDK retries. `check_or_reserve` for non-streaming, `check_or_reserve_stream` + `finalize_stream` for streaming. |
| `db_pool.py` | Bounded connection pool singleton (16 connections, thread-local, TTL recycling) |
| `log_safety.py` | SafeFormatter + RedactFilter: strips API keys, JWTs, emails, PII from logs |
| `provider.py` | `get_provider_for_model()`: admin model_provider_map setting > detect_provider_from_model() |

### Routes (`backend/routes/`)

| Router | Prefix | Key endpoints |
|--------|--------|---------------|
| `auth.py` | `/api/auth/` | register, verify-email, login, login-api-key, forgot/reset-password, me, logout, session. Both `register` and `verify_email` call `UserService.auto_activate_free_plan(user_id)` so every new user gets the free plan + wallet + initial `monthly_credits`. |
| `admin.py` | `/api/admin/` | init, login (TOTP-aware), config, models/sync, users CRUD, freeze/unfreeze, stats, audit-logs, killswitch |
| `admin_billing.py` | `/api/admin/` | plans CRUD, pricing CRUD + reset, orders + approve/reject/refund, wallet adjust (daily cap 10 000 credits/admin), promo-codes, redeem-codes, api-keys, payment providers (`known = {"stripe", "alipay", "wechat", "usdt"}`) |
| `admin_stats.py` | `/api/admin/stats/` | overview, trend, top-models, top-users, by-provider, revenue, recent-logs, health |
| `user.py` | `/api/user/` | password, profile, tokens CRUD, logs, models, notifications, dashboard, data export, data delete (soft-delete → 30-day purge) |
| `billing.py` | `/api/user/` + `/api/billing/` | wallet, transactions, auto-recharge, orders, redeem, plans, subscription lifecycle, api-keys CRUD. **New:** `GET /billing/providers` (public list for `PaymentDialog`), `POST /webhooks/usdt` (HMAC-verified NOWPayments IPN handler). **Permission hardening:** every state-changing endpoint uses `dependencies=[Depends(require_user_csrf)]` (API-key callers skip the check; session-cookie callers must echo `X-CSRF-Token`). The four subscription lifecycle endpoints additionally call `_assert_subscription_ownership(user.id, sub_id)` to close a latent IDOR on the `sub_id` path parameter. |
| `chat.py` | `/api/chat/` | conversations CRUD, send (non-streaming with pre-reserve + release), send/stream (SSE with release in generator terminal), generate-title |
| `proxy.py` | `/v1/` | Legacy MiniMax proxy (`/v1/text/chatcompletion_v2`), chat proxy (`/v1/chat`). Both non-streaming paths pre-reserve max cost via `_reserve_nonstream_cost` and reconcile via `_settle_nonstream_billing` / full refund on upstream error. |
| `openai_compat.py` | `/v1/` | models, models/{id}, chat/completions (streaming + non-streaming), completions (legacy streaming + non-streaming). Per-user token reservation wired at the top of each handler, released at every return site and inside every streaming generator. |
| `platform.py` | `/api/` | custom-providers CRUD, providers/aggregate, subscription requests, billing summary |
| `providers.py` | `/api/providers/` | list providers, test, configure, models, keys, refresh-all, aggregated models. POST test/ping now use `_require_admin_csrf` (was `_require_admin`) since they persist models / make outbound calls. |
| `usage.py` | `/api/user/usage/` | summary, daily, monthly, by-model, by-provider, export, quota |
| `admin_auth.py` | (shared) | **Not a router.** Canonical `require_admin` / `require_admin_csrf` FastAPI dependencies plus legacy-JWT helpers. Migration aliases `_admin_guard`, `_admin_csrf_guard`, `_require_admin`, `_require_admin_csrf` let existing call sites switch with a one-line import change. Used by `admin_billing.py`, `admin_stats.py`, `providers.py`, `platform.py`. `admin.py` keeps its own `require_admin_session` / `require_csrf` because those return the session dict (not `None`). |

## Frontend structure

React 18 + Vite + Tailwind. i18n via i18next (zh/en). State in Zustand stores. SPA served by FastAPI from `frontend/dist/`.

**Path alias**: `@` -> `frontend/src/`
**Dev server**: Port 3000, proxies `/api` and `/v1` to `localhost:8000`
**Build**: Manual chunks (react, charts, markdown), chunk warning limit 1500

### State management (Zustand stores)

| Store | Purpose |
|-------|---------|
| `authStore.js` | Session state (isAuthenticated, role, user). Bootstrapped via `checkSession()`. localStorage cache for instant first-paint. Handles admin/user login, logout, session expiry |
| `chatStore.js` | Chat UI state: conversations, messages, models, selectedModel, SSE streaming lifecycle. Delta accumulation, auto-title generation, model auto-selection |
| `notificationsStore.js` | Polls unread count (60s), notification list CRUD, mark-read/mark-all-read |

### API client (`src/lib/api.js`)

- Centralized `request()` wrapper: adds `/api` prefix, `credentials: 'include'`, CSRF token, 401 session expiry
- `streamChat()`: SSE parser with event dispatch (delta/done/error), AbortController support
- ~80+ named endpoint methods covering all backend APIs
- **New**: `listPublicPaymentProviders()` → `GET /billing/providers` (feeds `PaymentDialog`)

### Theme system

- Three modes: light, dark, system (OS preference)
- Applied via `data-theme` attribute + `.dark` class on `<html>`
- CSS custom properties in `tokens.css` for all theme-aware colors
- Cross-tab sync via `storage` events
- Tailwind `darkMode: 'class'`

### Wallet real-time sync

- `Wallet.jsx` polls `getWallet()` every 30s while `document.visibilityState === 'visible'` and writes `{balance, at}` to `localStorage('mm:wallet:balance')`.
- A cross-tab `storage`-event listener on the same key triggers an immediate refetch when another tab updates the balance (e.g. a top-up in tab A reflects in tab B without a manual refresh).
- `useBalanceWarning` hook reads the same key and exposes `{isLow, balance}`. "Low" means `< 100` credits.
- `<LowBalanceBanner />` is mounted inside `AppShell` (between the notifications bar and `<Outlet />`) so every protected page shows a slim amber warning when the authenticated non-admin user has low balance. Dismissible per session via `sessionStorage('mm:low-balance-dismissed')`.

### i18n

- i18next + react-i18next + browser language detector
- Languages: zh (Chinese, fallback), en (English)
- 45+ top-level translation key namespaces (newly added: `payment.providers.*`, `payment.usdt*`, `wallet.lowBalance.*`)
- Persisted to localStorage (`app_language`)

### CSS architecture (`src/styles/`)

| File | Purpose |
|------|---------|
| `tokens.css` | CSS custom properties: spacing, radius, shadows, colors (light/dark), focus ring |
| `base.css` | Global resets, font settings, `.visually-hidden` |
| `ui.css` | BEM-style component classes: cards, buttons, inputs, badges, modals, tables, language/theme toggles, chat prose |
| `layouts.css` | Page layouts: auth (centered card), shell (grid sidebar+main), chat (grid sidebar+main), dashboard grids, responsive breakpoints |
| `index.css` | Imports all above + Tailwind directives + spinner/loading-dots animations |

### Route map

| Path | Page | Auth | Layout |
|------|------|------|--------|
| `/` | RootRedirect | Public | Redirects by auth state |
| `/login` | Login | PublicOnly | Auth layout |
| `/admin/login` | AdminLogin | PublicOnly | Auth layout |
| `/forgot-password` | ForgotPassword | Public | Auth layout |
| `/reset-password` | ResetPassword | Public | Auth layout |
| `/help` | Help | Public | Standalone |
| `/chat` | Chat | Protected | Own sidebar (not AppShell) |
| `/usage` | Usage | Protected | AppShell |
| `/wallet` | Wallet | Protected | AppShell |
| `/account` | Account | Protected | AppShell |
| `/integration` | UserIntegrationGuide | Protected | AppShell |
| `/admin` | AdminDashboard | Admin | AppShell |
| `/admin/providers` | Providers | Admin | AppShell |
| `/admin/custom-providers` | CustomProviders | Admin | AppShell |
| `/admin/users` | Users | Admin | AppShell |
| `/admin/subscriptions` | Subscriptions | Admin | AppShell |
| `/admin/billing` | Billing | Admin | AppShell |
| `/admin/logs` | Logs | Admin | AppShell |
| `/admin/api-keys` | ApiKeys | Admin | AppShell |
| `/admin/redeem-codes` | RedeemCodes | Admin | AppShell |
| `/admin/pricing` | Pricing | Admin | AppShell |
| `/admin/orders` | AdminOrders | Admin | AppShell |
| `/admin/wallet-adjust` | AdminWalletAdjust | Admin | AppShell |
| `/admin/audit-logs` | AdminAuditLogs | Admin | AppShell |
| `/admin/plans` | AdminPlans | Admin | AppShell |
| `/admin/promo-codes` | AdminPromoCodes | Admin | AppShell |
| `/admin/channels` | Channels | Admin | AppShell |
| `*` | NotFound | Public | Standalone |

### Key components

| Component | Purpose |
|-----------|---------|
| `AppShell` | Layout: Sidebar + NotificationsBell + **LowBalanceBanner** + Outlet |
| `Sidebar` | Role-based nav, mobile slide-in, Logo, LanguageToggle, ThemeToggle |
| `TopBar` | Sticky header: title, subtitle, action slot |
| `ProtectedRoute` | Auth guard with role check, FullPageLoader |
| `Button` | 5 variants (primary/secondary/ghost/danger/success), 3 sizes, loading state |
| `Badge` | 7 variants, optional dot indicator |
| `Dialog` | Modal with backdrop blur, Escape key, 4 sizes |
| `ModelPicker` | Dropdown with search, provider grouping, type filtering |
| `MessageBubble` | Markdown rendering, syntax-highlighted code, DOMPurify, copy, regenerate |
| `ChatInputBox` | Auto-resize textarea, Enter to send, send/stop toggle |
| `ProviderLogo` | Brand SVG logos: remote > inline > colored initial |
| `PaymentDialog` | **Dynamic** provider list fetched from `GET /billing/providers`. Stripe/Alipay/WeChat redirect to the hosted checkout; USDT renders an inline deposit UI (address + amount + network + copy button) and polls `queryOrderPayment` until the IPN confirms. |
| `LowBalanceBanner` | **New.** Slim amber warning on every protected page when `balance < 100` credits. Reads from `localStorage('mm:wallet:balance')`, dismissible per session, auto-clears when balance recovers. |
| `NotificationsBell` | Bell icon with count badge, polling |
| `OnboardingTour` | 5-step modal tour, auto-shows on first login |

### Custom hooks

| Hook | Purpose |
|------|---------|
| `useLanguage` | i18n language toggle, persists to `app_language` |
| `useBalanceWarning` | **New.** Returns `{isLow, balance}` from `localStorage('mm:wallet:balance')` with cross-tab sync + 300ms debounce. |

## Deployment

### Docker

- **Dockerfile**: Two-stage build (node:20-alpine for frontend, python:3.11-slim for runtime). Non-root user. HEALTHCHECK. Single worker.
- **docker-compose.yml**: `api` service (port 8000, 2 CPU / 1GB) + `worker` sidecar (hourly subscription cron, 0.5 CPU / 256MB). Shared `api-data` volume.

### Bare metal

- **deploy/api.service**: Systemd unit with security hardening (NoNewPrivileges, ProtectSystem=strict, ReadWritePaths restricted)
- **deploy/api-worker.service** + **api-worker.timer**: Hourly oneshot. ExecStart calls both `run_daily_jobs()` (expiry + renewals + soft-delete purge + credits sweep + Stripe recon) and `run_hourly_jobs()` (expired orders + pending_payment + upcoming + reservation sweep).
- **deploy/nginx.conf**: TLS termination (Let's Encrypt), rate limiting (10r/s), gzip, SSE support (no buffering), 300s timeouts. Must allow `POST /api/webhooks/usdt` unauthenticated. **Permission-aware health probes**: `/health` and `/health/live` are public (cheap liveness); `/health/ready` and `/health/pools` are restricted to `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8` — the detailed readiness probe leaks DB / Redis / provider state and should not be exposed to the internet.
- **deploy/backup.sh**: SQLite online backup with integrity check, gzip compression, 30-day retention
- **deploy/DEPLOYMENT.md**: Full deployment runbook

## Permission hardening (Phase 3 audit, landed 2026-06-14)

A focused security pass on the admin-vs-user boundary produced the following fixes, all of which are live in the codebase and covered by regression tests:

| # | Severity | Change | Where |
|---|---|---|---|
| P3.1 | HIGH | **All 13 state-changing endpoints in `billing.py`** now declare `dependencies=[Depends(require_user_csrf)]`. The dependency skips the check for API-key callers (Authorization/X-API-Key header present) and enforces X-CSRF-Token ↔ mm_csrf ↔ session csrf (triple compare, `hmac.compare_digest`) for session-cookie callers. | `backend/routes/billing.py::require_user_csrf` |
| P3.2 | MEDIUM | **Subscription lifecycle endpoints validate `sub_id` ownership.** `_assert_subscription_ownership(user.id, sub_id)` is called at the top of `upgrade` / `downgrade` / `cancel` / `renew`. Returns 404 when the `sub_id` doesn't belong to the caller — closes a latent IDOR (the service layer already filtered by `user.id`, but the path parameter was cosmetic). | `backend/routes/billing.py::_assert_subscription_ownership` |
| P3.3 | MEDIUM | **`/admin/users/{id}/reveal-api-key` is now super-admin-only.** Migration 31 adds `admin_users.is_super_admin` (default 0, auto-promotes the earliest admin). Non-super-admins get 403 even with the correct admin password. Audit row (`user.api_key.reveal`) is written on success. | `backend/routes/admin.py::admin_reveal_api_key`, `backend/database.py::_migration_31_admin_super_admin` |
| P3.4 | LOW | **`POST /providers/{name}/test` and `/keys/{channel_id}/ping` switched to `_require_admin_csrf`** (was `_require_admin`). Both endpoints mutate state (persist models, emit outbound HTTP). | `backend/routes/providers.py` |
| P3.5 | LOW | **`/health/live` added as an explicit public liveness probe.** `/health/ready` now has Nginx-layer IP restriction to internal networks. Defence-in-depth with the existing backend-side `/health/pools` admin gate. | `backend/main.py::health_live`, `deploy/nginx.conf` |
| P3.6 | LOW | **`POST /admin/logout` switched to `require_csrf`** (was `require_admin_session`). Prevents CSRF-to-logout nuisance attacks. | `backend/routes/admin.py::logout` |
| P3.7 | INFO | **Shared `admin_auth` module extracted.** `backend/routes/admin_auth.py` holds the canonical `require_admin` / `require_admin_csrf` dependencies. `admin_billing.py`, `admin_stats.py`, `providers.py`, `platform.py` now import the shared copies via migration aliases (`_admin_guard`, `_admin_csrf_guard`, `_require_admin`, `_require_admin_csrf`). `admin.py` keeps its own `require_admin_session` / `require_csrf` because those return the session dict (handlers read `session["admin_id"]`). | `backend/routes/admin_auth.py` |

**Still open (low priority):** `/api/public/status` reveals `allow_legacy_x_api_key` and `allow_api_key_login` feature flags — move behind auth if the SPA doesn't need them pre-login. No state-changing GETs were found. No admin impersonation / privilege escalation paths exist. Webhook signatures (`/webhooks/stripe`, `/webhooks/usdt`) are mandatory and use `hmac.compare_digest`.

## Key design patterns

1. **Credit-based billing**: 1 CNY = 100 credits, 1 USD ~ 700 credits. Atomic wallet operations with `BEGIN IMMEDIATE` transactions. Central `grant_credits()` helper for every credit-side change.
2. **Channel-based key rotation**: Multiple API keys per provider, weighted selection, automatic cooldown on failure.
3. **Circuit breaker per provider**: In-memory, 5-failure threshold, prevents cascading upstream failures.
4. **Idempotency store**: SQLite-backed, 24h retention. Prevents double-charging on SDK retries.
5. **Provider abstraction**: Abstract base + registry. OpenAICompatibleProvider handles most providers. Anthropic, Google, MiniMax have custom implementations. Registry is extensible — add a one-line entry in `_PROVIDER_CLASSES` and implement three abstract methods.
6. **Custom providers**: Dynamic registration of any OpenAI-compatible endpoint with SSRF protection.
7. **Graceful degradation**: Redis is optional, payment providers are lazy-loaded, model aggregation continues on individual provider failures.
8. **Log safety**: All logging through redacting formatter (API keys, JWTs, emails, PII stripped).
9. **Schema migrations**: Inline numbered migrations in `database.py` (currently 30). `schema_migrations` table tracks applied versions. Migrations 29 and 30 are idempotent (PRAGMA table_info guard) so they're safe against the parallel test schema.
10. **Per-user token-reservation ledger**: `token_reservations` table (migration 29) holds a single row per user with `reserved_tokens` + `reserved_until` (TTL). Hot-path quota gate reads `snap['reserved_tokens']` and counts `used + reserved` against each window — closes the concurrent-request double-spend window. TTL-swept by `run_hourly_jobs()`; explicit release at every handler return site and inside every streaming generator.
11. **Per-credit-entry expiration**: Each credit-side `wallet_transactions` row carries its own `expires_at` timestamp (`created_at + CREDITS_EXPIRE_DAYS`) and an `expiry_debited` flag. `sweep_expired_credits()` (daily worker) debits each exactly once, capping the debit at the current wallet balance so partially-spent entries don't drive the balance negative.
12. **Stripe reconciliation**: Daily batch pulls paid Checkout Sessions from the last `STRIPE_RECON_LOOKBACK_HOURS`, matches to local orders, and auto-approves / routes to `pending_review` / flags orphans + late payments. Belt-and-suspenders with the webhook — the two paths are idempotent-safe via `approve_order`'s guard.
13. **Downgrade-on-cancel materialisation**: `process_expiry()` detects `auto_renew=0 AND pending_plan_id IS NOT NULL` and creates a fresh active subscription for the downgraded plan instead of silently dropping it.

## Environment variables reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | auto-generated in dev | Session signing key |
| `ENCRYPTION_KEY` | auto-generated in dev | Fernet key for API keys / channel keys |
| `CORS_ORIGINS` | `*` (dev) | Comma-separated. Production must not include `*`. |
| `DATABASE_PATH` | `minimax_proxy.db` | SQLite file location |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | Initial admin bootstrap |
| `DEFAULT_QUOTA_5H` | `3000` | 5-hour token quota for new users (was 500; bumped because 500 didn't survive a single GPT-4 request) |
| `DEFAULT_QUOTA_WEEK` | `5000` | Weekly token quota for new users |
| `RATE_LIMIT_PER_MINUTE` | `60` | IP rate limit (middleware) |
| `RATE_LIMIT_PER_HOUR` | `1000` | IP rate limit (middleware) |
| `STRIPE_SECRET_KEY` | — | Stripe API key (`sk_test_…` or `sk_live_…`) |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret (`whsec_…`) |
| `STRIPE_CURRENCY` | `cny` | ISO currency for Stripe checkouts. Set to `usd` only if your Stripe account cannot settle in CNY (you absorb the FX spread). |
| `NOWPAYMENTS_API_KEY` | — | NOWPayments API key. Absence marks the USDT provider as unavailable. |
| `NOWPAYMENTS_IPN_SECRET` | — | HMAC secret for `/api/webhooks/usdt` verification. |
| `NOWPAYMENTS_CNY_USDT_RATE` | `0.0` (parity) | Static CNY → USDT rate applied when a CNY-priced order is checked out in USDT. |
| `CREDITS_EXPIRE_DAYS` | `0` (disabled) | Per-credit-entry TTL. Each credit-side `wallet_transactions` row gets `expires_at = created_at + N days`. The daily sweep debits the wallet once the TTL elapses. `365` is a common gift-card default. |
| `SOFT_DELETE_RETENTION_DAYS` | `30` | How long soft-deleted users linger before `UserService.purge_soft_deleted_users()` hard-deletes them. Matches the promise the `/user/data/delete` endpoint makes. |
| `USAGE_LOG_RETENTION_DAYS` | `90` | Days to retain `usage_logs` rows before the daily sweep deletes them. High-write table (one row per API request); keep bounded for quota-query performance. |
| `NOTIFICATION_RETENTION_DAYS` | `90` | Days to retain read (`is_read=1`) `notifications` rows before the daily sweep deletes them. Unread notifications are preserved past this window. |
| `STRIPE_RECON_ENABLED` | `true` | Kill switch for the Stripe daily reconciliation worker. |
| `STRIPE_RECON_LOOKBACK_HOURS` | `48` | Stripe session scan window. Stripe retries webhooks for up to 3 days; 48h provides overlap. |
| `STRIPE_RECON_MAX_AUTO_APPROVE` | `50` | Per-run safety cap on auto-approvals. Beyond this count something is catastrophically wrong. |
| `STRIPE_RECON_AMOUNT_TOLERANCE` | `0.01` | Max absolute amount disagreement before routing to `pending_review`. |
| `ALLOW_API_KEY_LOGIN` | `true` | Whether the `/auth/login-api-key` endpoint is enabled |
| `CHANNEL_COOLDOWN_SECONDS` | `60` | Cooldown applied to a channel after an upstream failure |
| `MINIMAX_API_KEY` / `MINIMAX_API_BASE` | — | Default fallback provider credentials |

## Gotchas

- **Production requires**: `SECRET_KEY`, `ENCRYPTION_KEY`, `CORS_ORIGINS` (non-`*`). Auto-generates keys in dev mode
- **CORS**: Production strips `*` origins. Dev allows all
- **Frontend must be built**: `npm run build` in frontend/ — backend returns 503 if `frontend/dist/` missing
- **SPA catch-all**: Only fires for extensionless GET paths that don't match API prefixes
- **Rate limiting**: IP-based, applied as middleware. Configurable via `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_PER_HOUR`
- **httpx client**: Shared process-wide singleton with 30s read timeout. Closed on app shutdown
- **Schema migrations**: When adding a migration in `database.py`, ALSO update `backend/tests/conftest.py` to match — tests do NOT run the migrations. Migrations 29 (token_reservations), 30 (wallet_credits_expiry), 31 (admin_super_admin), 32 (token_reservations multi-row), 33 (subscriptions_unique_active), 34 (admin_totp_encryption), 35 (user_model_pools), and 36 (notification_cooldowns) use `PRAGMA table_info` guards specifically because conftest's CREATE TABLE already includes those columns/tables.
- **SQLite single-writer**: Uvicorn must run with `--workers 1`. Multi-process writes cause database locked errors
- **Wallet operations**: Always use `BEGIN IMMEDIATE` transactions. Never read-then-write without a lock. Prefer `grant_credits()` (credit-side) or `update_wallet()` (general +/- delta) over raw SQL.
- **Wallets.balance CHECK**: `CHECK (balance >= 0)` is enforced at the DB layer. The credits-expiry sweep caps each debit at the current balance so partially-spent entries don't violate the constraint.
- **Provider prefix stripping**: Model names may have provider prefixes (e.g., `openai/gpt-4`) that get stripped before forwarding upstream
- **Encryption**: `Security.encrypt()` uses double-base64 wrapping around Fernet. Legacy plaintext values returned as-is for backward compatibility
- **Session binding**: Sessions are bound to User-Agent string. Mismatched UA invalidates the session. The per-user concurrent-session cap also evicts the oldest excess sessions on every new login.
- **CSRF on billing write endpoints**: All 13 state-changing `billing.py` endpoints (`POST /user/orders`, `/user/redeem`, `/user/subscription`, four subscription-lifecycle ops, `PATCH /user/wallet/auto-recharge`, three API-key ops, `POST /billing/orders/{no}/pay`, `/billing/orders/{no}/query`) require `X-CSRF-Token` when the caller is authenticated via session cookie. API-key callers (`Authorization: Bearer …` or `X-API-Key` header) skip the check. Tests that hit these endpoints with a cookie-auth client must include the header.
- **Super-admin gate on reveal-API-key**: `POST /admin/users/{id}/reveal-api-key` rejects non-super-admins with 403. The first admin is auto-promoted by migration 31 and `create_admin_user()`; subsequent admins must be promoted via `UPDATE admin_users SET is_super_admin = 1 WHERE id = ?`.
- **/health/ready is internal-only**: Nginx restricts `/health/ready` and `/health/pools` to RFC-1918 + loopback. Use `/health` or `/health/live` for public liveness probes.
- **Token reservations are per-request**: `token_reservations` has `PRIMARY KEY (user_id, request_id)` (migration 32 rebuilt the table from the original single-row-per-user design). Each concurrent request from the same user gets its own row; the quota gate sums all un-expired rows. Explicit release at every return site is still mandatory — a missed release leaves the row pinned until the 300s TTL.
- **Reservation release is mandatory**: every call site that takes a reservation (6 in openai_compat/proxy/chat) must release it at every return point and inside every streaming generator. A missed release leaves the user pinned until the 300s TTL elapses.
- **`get_user_plan` fallback matches the free plan seed**: the `_FREE_FALLBACK` dict (RPM=20, TPM=50000, monthly_credits=10000) must stay in lockstep with `DEFAULT_PLANS`' free entry. Users whose plan has lapsed should not get looser limits than real free-plan users.
- **Enterprise plan (`code='enterprise'`, `monthly_price=0`) has bespoke upgrade/downgrade rules**: upgrade bypasses the price-monotonicity guard; self-service downgrade is blocked entirely (admin-only via `/admin/users/{id}/plan`).
- **Refund → subscription cancellation**: `refund_order()` automatically cancels any subscription the refunded order activated. If you add new paths that activate subscriptions, remember to add a symmetric cancellation hook in the refund path.
- **Stripe currency must match what your Stripe account settles in**: `STRIPE_CURRENCY` defaults to `cny`. Sending `"usd"` for a CNY-priced order charges 10 USD for a ¥10 order — a ~7× silent over-charge.
- **USDT payments are non-custodial and irreversible**: `refund_order` only debits the user's credits; there's no programmatic on-chain refund. Document this in the admin UI before enabling USDT for an operator.
- **`payments` stub providers raise `NotImplementedError`**: `AlipayProvider` / `WechatProvider` methods throw — the frontend shows "即将上线". Do not call them directly without checking `list_providers()[name]['available']`.

## Phase-1 + Phase-2 audit changelog

> Full detail in `docs/business-logic-audit.md`. This section is a concise index for AI agents reviewing the repo.

**Phase 1 (initial audit — 9 items)**

1. `assert_request_allowed` now blocks `balance <= 0` for priced models (free / unpriced still flow).
2. `get_user_plan` fallback matches the free-plan seed (RPM=20, TPM=50000, monthly_credits=10000).
3. `UserService.auto_activate_free_plan()` centralised; called from both `auth.register` and admin `create_user`.
4. `proxy.py` non-streaming paths (`/v1/text/chatcompletion_v2`, `/v1/chat`) pre-reserve max cost + reconcile on success / full refund on upstream error.
5. `Config.STRIPE_CURRENCY` (default `cny`) replaces the previous hardcoded `usd` in `pay_order`.
6. `refund_order()` calls `_maybe_cancel_subscription_on_refund()` which reads `plan_id`/`subscription_id` out of `orders.note` and cancels the matching subscription.
7. `UserService.purge_soft_deleted_users()` wired into `run_daily_jobs()`; fulfils the 30-day hard-delete promise of `/user/data/delete`.
8. Enterprise upgrade bypasses price-monotonicity; enterprise self-service downgrade blocked.
9. `SubscriptionService.renew()` and `.upgrade()` grant the plan's `monthly_credits` on successful charge.

**USDT integration (3 items)**

10. `backend/services/payment/usdt_provider.py` — NOWPayments-backed provider; HMAC-verified IPN.
11. `backend/routes/billing.py::usdt_webhook` — dedicated `POST /api/webhooks/usdt` handler; CNY→USDT rate applied before the amount-mismatch comparison.
12. `backend/routes/billing.py::list_public_payment_providers` — new `GET /billing/providers` for the dynamic `PaymentDialog`.

**Frontend (3 items)**

13. `PaymentDialog.jsx` rewritten to fetch providers from `/billing/providers`; USDT renders an inline deposit UI instead of redirecting.
14. `Wallet.jsx` polls `getWallet()` every 30s (visible tab only) + cross-tab sync via `localStorage('mm:wallet:balance')`.
15. `backend/session.py::create_session()` enforces `plans.max_concurrent` by evicting the user's oldest excess sessions.

**Phase 2 (follow-up — 6 items)**

16. **Migration 29**: `token_reservations` table + `reserve_tokens` / `release_reservation` / `purge_expired_reservations` helpers + `get_quota_snapshot.reserved_tokens` field. Route handlers (6 sites in `openai_compat.py`, 4 in `proxy.py`, 2 in `chat.py`) reserve after the quota gate and release at every return / inside every streaming generator.
17. **Migration 30**: `wallet_transactions.expires_at` + `expiry_debited`. `_credits_expire_at()` helper, `grant_credits()` atomic credit primitive, `update_wallet()` +`expires_at` parameter, `sweep_expired_credits()` daily worker. `approve_order` stamps `expires_at` on recharge rows.
18. `process_expiry()` now materialises `pending_plan_id` as a fresh active subscription when `auto_renew=0`, with a `downgraded=True` flag that suppresses the misleading "moved to free tier" notification.
19. `backend/services/stripe_reconciliation.py` — new Stripe ↔ orders daily reconciliation. Wired into `run_daily_jobs()`.
20. `frontend/src/hooks/useBalanceWarning.js` + `frontend/src/components/LowBalanceBanner.jsx` mounted in `AppShell`.
21. `Config.DEFAULT_QUOTA_5H` bumped 500 → 3000 tokens (env-overridable).

## Known follow-ups (not blocking, recorded for future work)

- ~~`token_reservations` → multi-row `(user_id, request_id)` design~~ — **done** (migration 32 rebuilt the table with `PRIMARY KEY (user_id, request_id)`).
- ~~`SubscriptionService.renew` / `.upgrade` (and `order_service._handle_subscription_activation`) route credit grants through `grant_credits`~~ — **done** (all three credit-grant paths now use the `grant_credits` helper, so monthly credits get a proper `expires_at` stamp).
- `test_stripe_reconciliation.py` — 11 scenarios planned (auto-approve, mismatch, orphan, late_payment, dedup, disabled, API failure, …).
- Banner "Top up" link → `/wallet?preset=4500` for one-click recovery.
- `AlipayProvider` / `WechatProvider` still stubs; real integration awaits operator decision.
- `/api/public/status` feature flags (`allow_legacy_x_api_key`, `allow_api_key_login`) could be hidden behind auth if the SPA doesn't need them pre-login.

---

## Phase-3 permission-audit changelog (2026-06-14, 7 items)

| # | Severity | Item | Files touched |
|---|---|---|---|
| P3.1 | HIGH | CSRF protection on all 13 state-changing `billing.py` endpoints via a new `require_user_csrf` dependency. Skips API-key callers, enforces triple-compare (`X-CSRF-Token` ↔ `mm_csrf` cookie ↔ session csrf) with `hmac.compare_digest` for session-cookie callers. | `backend/routes/billing.py` (new dependency + 13 route decorators) |
| P3.2 | MEDIUM | Subscription lifecycle endpoints (`upgrade` / `downgrade` / `cancel` / `renew`) now call `_assert_subscription_ownership(user.id, sub_id)` before the service method. Returns 404 when the `sub_id` doesn't belong to the caller. | `backend/routes/billing.py` (new helper + 4 call sites) |
| P3.3 | MEDIUM | `/admin/users/{id}/reveal-api-key` is super-admin-only. Migration 31 adds `admin_users.is_super_admin` (default 0) and auto-promotes the earliest admin. `create_admin_user()` also auto-promotes when it's the first admin. Audit row `user.api_key.reveal` is written on success. | `backend/database.py` (migration 31 + `create_admin_user`), `backend/tests/conftest.py` (schema mirror), `backend/routes/admin.py` (super-admin gate + audit log) |
| P3.4 | LOW | `POST /providers/{name}/test` and `/keys/{channel_id}/ping` switched from `_require_admin` to `_require_admin_csrf`. | `backend/routes/providers.py` |
| P3.5 | LOW | `/health/live` added as an explicit public liveness probe. `/health/ready` and `/health/pools` restricted at the Nginx layer to `10/8`, `172.16/12`, `192.168/16`, `127/8`. | `backend/main.py::health_live`, `deploy/nginx.conf` |
| P3.6 | LOW | `POST /admin/logout` switched from `require_admin_session` to `require_csrf`. Prevents CSRF-to-logout nuisance attacks. | `backend/routes/admin.py::logout` |
| P3.7 | INFO | Shared `backend/routes/admin_auth.py` module extracted. `require_admin` / `require_admin_csrf` are the canonical FastAPI dependencies; `_admin_guard`, `_admin_csrf_guard`, `_require_admin`, `_require_admin_csrf` are migration aliases for drop-in replacement. `admin_billing.py`, `admin_stats.py`, `providers.py`, `platform.py` now import the shared copies. `admin.py` keeps its own `require_admin_session` / `require_csrf` (they return the session dict, not `None`). | `backend/routes/admin_auth.py` (new), `backend/routes/{admin_billing,admin_stats,providers,platform}.py` |
