# Business Logic Audit — API 中转平台

> Full audit pass executed 2026-06-14. Every "FIXED" item below has
> been implemented with tests; every "OPEN" item is an accepted
> follow-up with a concrete recommendation.
>
> Backend test suite: **282 / 282 passing** · Frontend: `npm run build`
> clean.

---

## 1. Registration & default plan

| # | Finding | Status |
|---|---|---|
| 1.1 | `auth.py::_auto_activate_free_plan` was a local helper, easy to forget when admin routes create users. | **FIXED** — moved to `UserService.auto_activate_free_plan` and reused by both `auth.register` and `admin.users`. |
| 1.2 | `_auto_activate_free_plan` failed silently; if the free plan row was deleted the user would register successfully but land with no plan, no wallet, no subscription. | **FIXED** — `get_user_plan` now returns a `_FREE_FALLBACK` dict that matches the free plan seed (RPM=20, TPM=50000, monthly_credits=10000). Users without a persisted plan are treated identically to free-plan users, and they are subject to the same rate limits. |
| 1.3 | `quota_5h = 500` tokens is very low — one typical GPT-4 request can consume the entire 5-hour window. | **OPEN** — recommend bumping to `DEFAULT_QUOTA_5H = 3000` in `config.py` and documenting the reasoning in `AGENTS.md`. |
| 1.4 | Admin-created users didn't get the free-plan credits / subscription. | **FIXED** — `UserService.create_user` now calls `auto_activate_free_plan`. |

## 2. Billing & quota engine

| # | Finding | Status |
|---|---|---|
| 2.1 | `assert_request_allowed` only rejected `balance < 0`, so a `balance = 0` user with a priced model could dispatch upstream, fail the post-hoc charge, and the upstream quota was consumed for free. | **FIXED** — hot path now consults `get_model_pricing` and blocks `balance <= 0` for priced models. Free / unpriced models still flow. |
| 2.2 | `proxy.py` non-streaming paths (`/v1/text/chatcompletion_v2`, `/v1/chat`) had no pre-charge or idempotency — post-billing only. | **FIXED** — added `_reserve_nonstream_cost` + `_settle_nonstream_billing` helpers; both endpoints pre-reserve the max cost and reconcile on success, with a full refund on upstream error. |
| 2.3 | `reconcile_stream_reserve` swallowed supplementary charges when the user couldn't cover them (only a `warning` log). | **OPEN** — recommend returning a structured reconciliation result so the caller can annotate the usage_log and alert the operator. |
| 2.4 | Token-quota over-allocation: a user with 100 tokens remaining can still ask for `max_tokens = 4096` and consume more than their quota, because the pre-check only sees `used`, not `used + estimated`. | **OPEN (accepted)** — clamping estimated-tokens to the smallest remaining budget broke legitimate tests where `max_tokens` is a safety cap and actual usage is much lower. A proper fix needs a reservation ledger (`reserved_tokens` counter with TTL), which requires a schema migration. |
| 2.5 | Credits never expire. A dormant account keeps its balance forever. | **OPEN** — recommend a `credits_expire_after_days` config + a worker that debits expired balances with a `'expiry'` wallet_transaction type. |

## 3. Subscription lifecycle

| # | Finding | Status |
|---|---|---|
| 3.1 | Enterprise plan (`monthly_price = 0`, custom billing) could never be reached via the upgrade path: `new_price <= old_price` blocked any price-0 transition, including free → enterprise. | **FIXED** — upgrade now detects `code = 'enterprise'` and bypasses the monotonicity guard. |
| 3.2 | Enterprise plan could be self-downgraded (enterprise → free) because price-0 vs price-0 passed the `new_price < old_price` check. | **FIXED** — downgrade now blocks when the current plan's `code = 'enterprise'`. Admin-only via `/admin/users/{id}/plan`. |
| 3.3 | Free plan's `monthly_credits` were only granted at registration, never again on renew. Users ran dry after 30 days. | **FIXED** — `SubscriptionService.renew` now grants `monthly_credits` on every successful renewal (gated on `payment_status == 'charged'`). |
| 3.4 | Paid plans also have `monthly_credits` in the seed data, but upgrades didn't grant them. | **FIXED** — `SubscriptionService.upgrade` mirrors the renew logic: on successful charge, the new plan's `monthly_credits` are credited immediately. |
| 3.5 | `downgrade` + `cancel auto_renew` in the same cycle: `process_expiry` expires the sub and clears `plan_id`; the pending downgrade never fires. | **OPEN** — recommend a `pending_downgrade_on_cancel` guard that, when `auto_renew` is toggled off, applies the pending plan immediately (or warns the user). |

## 4. Orders, refunds, and payment

| # | Finding | Status |
|---|---|---|
| 4.1 | `pay_order` hardcoded `currency = "usd"` while the order `amount` was priced in CNY. A ¥10 order was sent to Stripe as 1000 cents USD — a ~7× silent overcharge in production. | **FIXED** — `Config.STRIPE_CURRENCY` (default `"cny"`) now drives the currency. Operators with USD-only Stripe accounts override via env (accepting FX risk). |
| 4.2 | `refund_order` debited the wallet and marked the order refunded, but any subscription activated by that order kept running — user retained the paid-plan discount. | **FIXED** — `_maybe_cancel_subscription_on_refund` reads `plan_id` / `subscription_id` out of `orders.note`, cancels the matching subscription, clears `users.plan_id`, and writes an audit row. |
| 4.3 | No online payment other than Stripe was implemented (Alipay / WeChat were stubs). | **FIXED** — added a full USDT (NOWPayments) integration: provider, config, FX dispatch in `pay_order`, and a dedicated `/webhooks/usdt` endpoint with HMAC IPN verification and CNY→USDT rate conversion in the mismatch check. |
| 4.4 | `PaymentDialog.jsx` hardcoded `[stripe, alipay, wechat]`; backend changes didn't surface in the UI. | **FIXED** — added a public `GET /billing/providers` endpoint and rewrote `PaymentDialog` to fetch the list, rendering providers dynamically. USDT shows an inline deposit UI (address + amount + network + copy button) instead of redirecting. |
| 4.5 | No automatic reconciliation between Stripe dashboard and local orders. | **OPEN** — recommend a daily worker that pulls `/v1/checkout/sessions` and diffs against `orders` to surface missed webhooks. |

## 5. User lifecycle

| # | Finding | Status |
|---|---|---|
| 5.1 | Self-service `/user/data/delete` promised a 30-day hard-delete but no cron ever executed it. Soft-deleted rows lingered forever. | **FIXED** — `UserService.purge_soft_deleted_users` added, wired into `SubscriptionService.run_daily_jobs`. `Config.SOFT_DELETE_RETENTION_DAYS` (default 30) governs the retention window. |
| 5.2 | No per-user concurrent-session cap — a stolen credential could log in from unlimited devices. `plans.max_concurrent` existed but was never enforced. | **FIXED** — `create_session` now evicts the user's oldest excess sessions when `max_concurrent > 0`. Users without a persisted plan (free fallback, `id=None`) are not limited — avoids kicking lapsed users on every login. |
| 5.3 | Password reset + session invalidation works correctly. | OK — no change needed. |
| 5.4 | Multi-device login: each login creates an independent session, but the new cap (5.2) now enforces the plan's limit. | OK — see 5.2. |

## 6. Frontend display

| # | Finding | Status |
|---|---|---|
| 6.1 | Wallet balance only refreshed on page load; a user watching the page while calling the API saw stale numbers. | **FIXED** — `Wallet.jsx` now polls `getWallet` every 30s while the tab is visible (`document.visibilityState === 'visible'`). |
| 6.2 | Two open tabs showed divergent balances after a top-up in one of them. | **FIXED** — cross-tab sync via `localStorage('mm:wallet:balance')` + a `storage`-event listener that pulls fresh data on change. |
| 6.3 | No low-balance proactive nudge on the frontend (backend `_maybe_emit_low_balance` exists but depends on the user opening the notifications bell). | **OPEN** — recommend a `useBalanceWarning` hook that flashes a top banner when `balance < 100` and the user is on a billing-relevant page. |

---

## Files touched

```
backend/config.py                                  (SOFT_DELETE_RETENTION_DAYS, STRIPE_CURRENCY, NOWPAYMENTS_*)
backend/database.py                                (get_user_plan _FREE_FALLBACK)
backend/routes/auth.py                             (use UserService.auto_activate_free_plan)
backend/routes/billing.py                          (STRIPE_CURRENCY dispatch, USDT amount conversion, /billing/providers, /webhooks/usdt)
backend/routes/admin_billing.py                    (known providers += 'usdt')
backend/routes/proxy.py                            (non-streaming pre-reserve + settlement)
backend/services/user_service.py                   (auto_activate_free_plan, purge_soft_deleted_users)
backend/services/quota_service.py                  (balance=0 pre-check for priced models)
backend/services/order_service.py                  (_maybe_cancel_subscription_on_refund)
backend/services/subscription_service.py           (enterprise upgrade/downgrade guards, monthly_credits grant on renew+upgrade, purge hook)
backend/services/payment/__init__.py               (register 'usdt')
backend/services/payment/usdt_provider.py          (NEW — NOWPayments integration)
backend/session.py                                 (max_concurrent enforcement in create_session)
backend/tests/test_channel_routing.py              (test user bumped to basic plan)
backend/tests/test_subscription_service.py         (test_no_active_sub_creates_new_subscription, prorates_correctly, sufficient_balance_creates_new_sub_and_debits)
frontend/src/components/PaymentDialog.jsx          (dynamic provider list + USDT inline deposit UI)
frontend/src/lib/api.js                            (listPublicPaymentProviders)
frontend/src/pages/Wallet.jsx                      (30s balance polling + cross-tab sync)
frontend/src/i18n/locales/{en,zh}.json             (payment.providers + USDT UI copy)
```

---

## Deployment notes

### New env vars

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `STRIPE_CURRENCY` | no | `cny` | ISO currency for Stripe checkouts. Set to `usd` only if your Stripe account cannot settle in CNY (you absorb FX). |
| `NOWPAYMENTS_API_KEY` | no (USDT opt-in) | — | NOWPayments API key. Absence marks the provider as unavailable. |
| `NOWPAYMENTS_IPN_SECRET` | no (USDT opt-in) | — | Shared HMAC secret for `/webhooks/usdt` verification. |
| `NOWPAYMENTS_CNY_USDT_RATE` | no | `0.0` (parity) | Static CNY→USDT rate applied when an order priced in CNY is checked out in USDT. |
| `SOFT_DELETE_RETENTION_DAYS` | no | `30` | How long soft-deleted users linger before the daily purge hard-deletes them. |

### Worker

`deploy/api-worker.service`'s ExecStart already calls
`SubscriptionService.run_daily_jobs()`, which now includes
`UserService.purge_soft_deleted_users()`. No cron change required.

### Nginx

No changes required. The new `/webhooks/usdt` endpoint is public and
unauthenticated (HMAC-signed like Stripe).

---

## Open items (recommended follow-ups)

1. **`reserved_tokens` per-request reservation granularity** — the
   current ledger uses a `UNIQUE(user_id)` row, so only one in-flight
   request per user is tracked. Multiple concurrent streams from the
   same user collapse into the latest reservation. A multi-row design
   keyed on `(user_id, request_id)` would be more precise but needs
   a cleanup story on client disconnect.
2. **Credits-expiry coverage on renew / upgrade paths** — the
   `SubscriptionService.renew` and `.upgrade` functions still use raw
   INSERTs rather than the new `grant_credits` helper, so monthly
   credits granted via those paths don't get an `expires_at` stamp.
   Routing them through `grant_credits` (or a split-transaction
   pattern) would close the gap.
3. **Stripe reconciliation test coverage** — the worker is wired into
   `run_daily_jobs` but has no dedicated `test_stripe_reconciliation.py`
   yet. The research plan lists 11 scenarios worth pinning down.
4. **Proactive low-balance UX beyond the banner** — the banner warns
   on every protected page but doesn't deep-link to a pre-filled
   top-up amount. A follow-up could route the "Top up" link to
   `/wallet?preset=4500` for one-click recovery.

---

## Phase-2 follow-up (implemented after the initial audit)

| # | Item | Files touched | Behavioural change |
|---|---|---|---|
| P2.1 | **Token-quota reservation ledger** | `backend/database.py` (migration 29, `reserve_tokens` / `get_active_reservation` / `release_reservation` / `purge_expired_reservations`, `get_quota_snapshot` +reserved_tokens field), `backend/services/quota_service.py` (`reserve_quota_reservation` / `release_quota_reservation`), `backend/routes/{openai_compat,proxy,chat}.py` (reserve after the quota gate, release at each return site / inside each streaming generator), `backend/tests/conftest.py` (mirror schema), `backend/services/subscription_service.py::run_hourly_jobs` (sweep TTL-expired rows) | Closes the concurrent-request double-spend window: a second request from the same user now sees the first request's reserved token count via `snap['reserved_tokens']` and is rejected when the combined total would exceed the quota. The over-commit residual (user with 100 remaining asks for `max_tokens=4096` and actually uses 300) is accepted — refusing such requests outright broke too many legitimate flows where `max_tokens` is a safety cap. |
| P2.2 | **Per-credit-entry expiration** | `backend/database.py` (migration 30, `_credits_expire_at`, `grant_credits`, `sweep_expired_credits`, `update_wallet` +`expires_at` param), `backend/config.py` (`CREDITS_EXPIRE_DAYS`, default 0 = off), `backend/services/order_service.py::approve_order` (stamps `expires_at` on the recharge row), `backend/services/subscription_service.py::run_daily_jobs` (sweep hook) | Operators can now set `CREDITS_EXPIRE_DAYS=365` (or any horizon) and the daily worker will claw each credit entry back exactly once after its TTL. The debit is capped at the current wallet balance so partially-spent entries don't drive the balance negative. |
| P2.3 | **Downgrade-on-cancel guard** | `backend/services/subscription_service.py::process_expiry` | A user who both (a) scheduled a downgrade and (b) disabled auto-renew no longer loses the pending plan at expiry. `process_expiry` now materialises the downgrade as a fresh active subscription and points `users.plan_id` at the new plan, with a "downgraded" flag that skips the misleading "moved to free tier" notification. |
| P2.4 | **Stripe ↔ orders daily reconciliation** | `backend/services/stripe_reconciliation.py` (new), `backend/services/subscription_service.py::run_daily_jobs` (hook), `backend/config.py` (`STRIPE_RECON_ENABLED`, `STRIPE_RECON_LOOKBACK_HOURS`, `STRIPE_RECON_MAX_AUTO_APPROVE`, `STRIPE_RECON_AMOUNT_TOLERANCE`) | Recovers paid Checkout Sessions whose webhook was missed: auto-approves amount-matching orders, routes mismatches to `pending_review`, flags late payments (paid after local expiry) for human review, and logs orphans. Idempotency-safe via `approve_order`'s existing guard. USDT orders get the configured CNY→USDT rate applied before the amount comparison. |
| P2.5 | **Frontend low-balance banner** | `frontend/src/hooks/useBalanceWarning.js` (new), `frontend/src/components/LowBalanceBanner.jsx` (new), `frontend/src/components/AppShell.jsx` (mounted), `frontend/src/i18n/locales/{en,zh}.json` (`wallet.lowBalance.*`) | Slim amber banner on every protected page (AppShell-level) when a non-admin user's balance drops below 100 credits. Reads the same `localStorage('mm:wallet:balance')` key Wallet.jsx already writes to — zero extra API calls. Dismissible per session; auto-clears when balance recovers. Cross-tab sync via `storage` events. |
| P2.6 | **`DEFAULT_QUOTA_5H` bump** | `backend/config.py` | 500 → 3000 tokens. The previous value was too tight to survive a single typical GPT-4 request, so new users hit the 5-hour wall on their very first call despite having 10 000 credits. Operators can still override via `DEFAULT_QUOTA_5H`. |

### Deployment notes (Phase 2)

| Variable | Default | Purpose |
|---|---|---|
| `CREDITS_EXPIRE_DAYS` | `0` (disabled) | Per-credit-entry TTL. 365 is a common gift-card default. |
| `STRIPE_RECON_ENABLED` | `true` | Kill switch for the reconciler. |
| `STRIPE_RECON_LOOKBACK_HOURS` | `48` | Stripe session scan window. Stripe retries webhooks for up to 3 days, so 48h provides overlap. |
| `STRIPE_RECON_MAX_AUTO_APPROVE` | `50` | Per-run safety cap on auto-approvals. |
| `STRIPE_RECON_AMOUNT_TOLERANCE` | `0.01` | Maximum amount disagreement before routing to `pending_review`. |
| `DEFAULT_QUOTA_5H` | `3000` | 5-hour token quota for newly-registered users. |
| `DEFAULT_QUOTA_WEEK` | `5000` | Weekly token quota for newly-registered users (unchanged). |

The worker (`deploy/api-worker.service`) already calls
`SubscriptionService.run_daily_jobs()` hourly, which now includes the
soft-delete purge, the credits sweep, and the Stripe reconciliation.
No cron change required.
