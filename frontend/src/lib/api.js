/**
 * API client.
 *
 * The backend speaks **session-cookie auth**, not Bearer tokens:
 *  * Admin endpoints use `mm_admin_session` cookie (set by `/api/admin/login`).
 *  * User endpoints use `mm_session` cookie (set by `/api/auth/login`).
 *
 * Every request MUST include `credentials: 'include'` so the browser
 * will store the Set-Cookie header and send it back. The previous version
 * of this file forgot `credentials` *and* tried to send a Bearer token
 * that the backend never issued — the result was a permanent login loop
 * (token was always `undefined`) and a 401 storm on every admin call.
 *
 * This module is now session-only. There is no client-side token; the
 * `authStore` is the single source of truth for `isAuthenticated` /
 * `role` / `user`, derived from `/api/auth/session` and updated
 * on login / logout.
 */
import i18n from '@/i18n';

const BASE = '/api';

function getCookie(name) {
  const target = `${encodeURIComponent(name)}=`;
  for (const raw of document.cookie.split(';')) {
    const item = raw.trim();
    if (item.startsWith(target)) return decodeURIComponent(item.slice(target.length));
  }
  return '';
}

function getCsrfToken() {
  return getCookie('mm_csrf');
}

function parseRangeDays(range) {
  if (!range) return 7;
  if (range === '1d') return 1;
  if (range === '7d') return 7;
  if (range === '30d') return 30;
  if (range === 'mtd') {
    const d = new Date();
    return Math.max(1, d.getDate());
  }
  return 7;
}

async function handle(res, { onUnauthorized } = {}) {
  if (res.status === 401) {
    if (typeof onUnauthorized === 'function') onUnauthorized();
  }
  if (!res.ok) {
    let body;
    try {
      body = await res.json();
    } catch {
      body = await res.text().catch(() => '');
    }
    const message =
      (body && body.detail) ||
      (typeof body === 'string' && body) ||
      res.statusText ||
      'Request failed';
    const err = new Error(message);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    // 204 No Content returns no body — guard against ``res.json()`` failing.
    if (res.status === 204) return null;
    return res.json();
  }
  return res.text();
}

/**
 * When the backend reports the session is gone, expire the local store
 * and navigate to /login. The ProtectedRoute would also catch this on
 * the next render, but doing it eagerly avoids a flash of "logged in"
 * UI on the page the user is currently looking at.
 *
 * ``/api/auth/session`` is exempt — it's the *check* for the session
 * itself, so reporting 401 on it would create an infinite expire /
 * recheck loop.
 */
let _sessionExpiring = false;
let _expireHandlers = [];
/**
 * The authStore registers a callback here on module load. The callback
 * receives no arguments and is responsible for clearing the store and
 * navigating. This indirection keeps the dependency between the two
 * modules one-way (authStore -> api) at module-load time.
 */
export function _registerSessionExpireHandler(fn) {
  _expireHandlers.push(fn);
  return () => {
    _expireHandlers = _expireHandlers.filter((f) => f !== fn);
  };
}
function _expireSessionAndRedirect() {
  if (_sessionExpiring) return;
  _sessionExpiring = true;
  for (const fn of _expireHandlers) {
    try {
      fn();
    } catch {
      // best-effort: if one handler blows up, still let the others run
    }
  }
  // Defer the navigation a tick so the current handler can throw
  // its error first — otherwise React would unmount mid-render.
  setTimeout(() => {
    if (typeof window !== 'undefined') {
      const isAdminPath = window.location.pathname.startsWith('/admin');
      const redirectTarget = isAdminPath ? '/admin/login' : '/login';
      if (window.location.pathname !== redirectTarget) {
        window.location.assign(redirectTarget);
      }
    }
    _sessionExpiring = false;
  }, 0);
}

export async function request(
  path,
  { method = 'GET', body, headers = {}, signal, onUnauthorized, _csrfRetry = false } = {},
) {
  const hdrs = {
    Accept: 'application/json',
    ...headers,
  };
  if (body !== undefined && hdrs['Content-Type'] === undefined) {
    hdrs['Content-Type'] = 'application/json';
  }
  // CSRF: state-changing methods (POST/PUT/PATCH/DELETE) must echo the
  // token issued alongside the session cookie. The backend reads the
  // ``X-CSRF-Token`` header.
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const csrf = getCsrfToken();
    if (csrf && !hdrs['X-CSRF-Token']) {
      hdrs['X-CSRF-Token'] = csrf;
    }
  }
  const opts = {
    method,
    headers: hdrs,
    credentials: 'include', // <-- the critical bit; sends + receives cookies
    signal,
  };
  if (body !== undefined) {
    opts.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opts);

  // CSRF token expired: the backend returns 403 with body code "csrf_invalid"
  // when the X-CSRF-Token header is missing or doesn't match the cookie.
  // Refresh the cookie via GET /auth/session (which rotates the CSRF token)
  // and retry the original request exactly once.
  if (res.status === 403 && !_csrfRetry && method !== 'GET' && method !== 'HEAD') {
    let csrfBody = null;
    try {
      csrfBody = await res.clone().json();
    } catch {
      /* not JSON — not a csrf_invalid response */
    }
    if (csrfBody && csrfBody.code === 'csrf_invalid') {
      try {
        await fetch(BASE + '/auth/session', { credentials: 'include' });
      } catch {
        /* best-effort refresh */
      }
      // Re-read the (now refreshed) CSRF token and retry once.
      return request(path, {
        method,
        body,
        headers,
        signal,
        onUnauthorized,
        _csrfRetry: true,
      });
    }
  }

  // Session-expiry detection: any 401 from a non-``/auth/session`` call
  // means the cookie is gone. Skip the redirect for ``/auth/session``
  // (it would create an infinite expire / recheck loop) and for the
  // login endpoints (they're allowed to be unauthenticated).
  if (
    res.status === 401 &&
    !path.endsWith('/auth/session') &&
    !path.endsWith('/auth/login') &&
    !path.endsWith('/auth/login-api-key') &&
    !path.endsWith('/admin/login') &&
    !path.endsWith('/admin/init') &&
    !path.endsWith('/auth/register') &&
    !path.endsWith('/auth/verify') &&
    !path.endsWith('/auth/forgot-password') &&
    !path.endsWith('/auth/reset-password') &&
    !path.includes('/auth/reset-password/validate')
  ) {
    // Guard: if the failing request targeted an admin API endpoint but the
    // user is currently on a *user* page (e.g. /account), the 401 means
    // "you need admin auth", NOT "your session expired".  Expiring the
    // user session here would log them out of the entire app — a P0 UX
    // regression.  Only trigger the redirect when the 401 is "expected"
    // for the current page context.
    const isAdminApi = path.startsWith('/admin/') || path === '/admin';
    const isOnAdminPage =
      typeof window !== 'undefined' && window.location.pathname.startsWith('/admin');
    if (!isAdminApi || isOnAdminPage) {
      _expireSessionAndRedirect();
    }
  }
  return handle(res, { onUnauthorized });
}

export function streamChat(payload, { signal, onDelta, onDone, onError } = {}) {
  const controller = signal ? null : new AbortController();
  const sig = signal || controller.signal;
  let res;
  return (async () => {
    res = await fetch(BASE + '/chat/send/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(getCsrfToken() ? { 'X-CSRF-Token': getCsrfToken() } : {}),
      },
      credentials: 'include',
      body: JSON.stringify(payload),
      signal: sig,
    });
    if (!res.ok || !res.body) {
      // Handle 401 session expiry the same way as the regular
      // request() path so the user is redirected to login.
      if (res.status === 401) {
        _expireSessionAndRedirect();
      }
      const text = await res.text().catch(() => '');
      // Try to extract a human-readable message from the JSON body
      // that FastAPI's exception handler returns ({detail, code, request_id}).
      let msg = text || res.statusText || 'Request failed';
      try {
        const json = JSON.parse(text);
        if (json.detail) msg = json.detail;
      } catch (_) { /* not JSON — use raw text */ }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    // watchdog: 5 分钟无数据视为流式超时，避免连接挂死耗尽浏览器资源
    const STREAM_TIMEOUT_MS = 5 * 60 * 1000;
    let lastChunkTime = Date.now();
    while (true) {
      const { value, done } = await reader.read();
      const now = Date.now();
      // read 阻塞超过阈值则视为超时（连接挂死）
      if (now - lastChunkTime > STREAM_TIMEOUT_MS) {
        const timeoutErr = new Error('Stream timeout: no data received for 5 minutes');
        try { controller && controller.abort(); } catch { /* ignore */ }
        try { onError && onError(timeoutErr); } catch { /* ignore */ }
        throw timeoutErr;
      }
      if (done) break;
      // 收到数据，刷新最后活动时间
      lastChunkTime = now;
      buf += dec.decode(value, { stream: true });
      // Server-Sent Events: a frame ends at '\n\n', and a frame is
      // made of ``event: <name>`` / ``data: <json>`` lines. The backend
      // uses three internal event names: ``delta``, ``done``, ``error``
      // (see ``backend/services/proxy_service.py::stream_chat``). The
      // previous parser only looked at ``data:`` and treated every
      // event as a delta — so a ``done`` event carrying the full
      // accumulated reply was double-appended to the message bubble,
      // and an ``error`` event was misclassified as content. The fix
      // is to read the ``event:`` line as well and dispatch by name.
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let eventName = 'message';
        let dataLine = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('event:')) {
            eventName = line.slice(6).trim() || 'message';
          } else if (line.startsWith('data:')) {
            dataLine = line.slice(5).trim();
          }
        }
        if (!dataLine) continue;
        if (dataLine === '[DONE]') {
          onDone && onDone();
          return;
        }
        let parsed;
        try {
          parsed = JSON.parse(dataLine);
        } catch (e) {
          onError && onError(new Error(i18n.t('errors.sseParse', { data: dataLine })));
          continue;
        }
        if (eventName === 'error') {
          const msg = (parsed && (parsed.error || parsed.detail)) || i18n.t('errors.upstream');
          const err = new Error(msg);
          err.status = (parsed && parsed.code) || 502;
          err.body = parsed;
          onError && onError(err);
          // Keep reading: the stream may still send a trailing
          // ``done`` event with usage, which the caller's
          // ``onDone`` is responsible for closing the bubble on.
          continue;
        }
        if (eventName === 'done') {
          // Pass model/usage metadata but NOT content — the content
          // was already accumulated via delta events. Including the
          // backend's accumulated text here would double the message.
          onDelta && onDelta({ content: '', done: true, model: parsed.model, usage: parsed.usage });
          onDone && onDone(parsed);
          return;
        }
        // ``delta`` (and any legacy ``message``) — forward as-is.
        onDelta && onDelta(parsed);
      }
    }
    onDone && onDone();
  })();
}

export const api = {
  // ------------------------------------------------------------------
  // Session
  // ------------------------------------------------------------------
  getSession: () => request('/auth/session'),

  // ------------------------------------------------------------------
  // Auth
  // ------------------------------------------------------------------
  adminLogin: (username, password, remember = false) =>
    request('/admin/login', {
      method: 'POST',
      body: { username, password, remember: Boolean(remember) },
    }),
  adminLogout: () => request('/admin/logout', { method: 'POST' }),

  userLogin: (username, password, remember = false, captcha = null) =>
    request('/auth/login', {
      method: 'POST',
      body: {
        username,
        password,
        remember: Boolean(remember),
        ...(captcha ? { captcha_id: captcha.id, captcha_answer: captcha.answer } : {}),
      },
    }),
  // L17: Fetch a math CAPTCHA challenge for the login form.
  getCaptcha: () => request('/auth/captcha'),
  userLoginApiKey: (apiKey, remember = false) =>
    request('/auth/login-api-key', {
      method: 'POST',
      body: { api_key: apiKey, remember: Boolean(remember) },
    }),
  userLogout: () => request('/auth/logout', { method: 'POST' }),

  register: (data) => request('/auth/register', { method: 'POST', body: data }),
  verifyEmail: (data) => request('/auth/verify', { method: 'POST', body: data }),
  forgotPassword: (data) => request('/auth/forgot-password', { method: 'POST', body: data }),
  resetPassword: (data) => request('/auth/reset-password', { method: 'POST', body: data }),
  validateResetToken: (token) =>
    request(`/auth/reset-password/validate?token=${encodeURIComponent(token)}`),

  getUsers: () => request('/admin/users'),
  createUser: (data) => request('/admin/users', { method: 'POST', body: data }),
  updateUser: (id, data) => request(`/admin/users/${id}`, { method: 'PUT', body: data }),
  deleteUser: (id) => request(`/admin/users/${id}`, { method: 'DELETE' }),
  adminSendResetEmail: (userId) =>
    request(`/admin/users/${userId}/send-reset-email`, { method: 'POST' }),
  freezeUser: (userId, reason) =>
    request(`/admin/users/${userId}/freeze`, { method: 'POST', body: { reason } }),
  unfreezeUser: (userId, reason) =>
    request(`/admin/users/${userId}/unfreeze`, { method: 'POST', body: { reason } }),
  adminResetUserPassword: (userId, payload) =>
    request(`/admin/users/${userId}/reset-password`, {
      method: 'POST',
      body: payload,
    }),

  getConfig: () => request('/admin/config'),
  saveConfig: (data) => request('/admin/config', { method: 'POST', body: data }),
  getModels: () => request('/user/models'),
  getProvidersSummary: () => request('/user/providers-summary'),
  syncModels: () => request('/admin/models/sync', { method: 'POST' }),

  getStats: () => request('/admin/stats'),
  getTrend: () => request('/admin/trend'),
  getRecentLogs: () => request('/admin/recent-logs'),
  getBillingOverview: () => request('/admin/billing/overview'),

  // The actual providers endpoint lives at /providers (not /admin/providers)
  // — there's no admin-namespaced mirror, so call the canonical path.
  getProviders: () => request('/providers'),
  getProviderModels: (name) => request(`/providers/${name}/models`),
  testProvider: (name) => request(`/providers/${name}/test`, { method: 'POST' }),
  saveProviderConfig: (name, data) =>
    request(`/providers/${name}/config`, { method: 'POST', body: data }),
  getProviderKeys: (name) => request(`/providers/${name}/keys`),
  deleteProviderKey: (name, channelId) =>
    request(`/providers/${name}/keys/${channelId}`, { method: 'DELETE' }),
  pingProviderKey: (name, channelId) =>
    request(`/providers/${name}/keys/${channelId}/ping`, { method: 'POST' }),
  refreshAllProviders: () => request('/providers/refresh-all', { method: 'POST' }),
  aggregateProviders: (refresh = false) => request(`/providers/aggregate?refresh=${refresh}`),

  getCustomProviders: () => request('/custom-providers'),
  createCustomProvider: (data) => request('/custom-providers', { method: 'POST', body: data }),
  updateCustomProvider: (slug, data) =>
    request(`/custom-providers/${slug}`, { method: 'PUT', body: data }),
  deleteCustomProvider: (slug) => request(`/custom-providers/${slug}`, { method: 'DELETE' }),
  testCustomProvider: (slug) => request(`/custom-providers/${slug}/test`, { method: 'POST' }),
  getCustomProviderModels: (slug) => request(`/custom-providers/${slug}/models`),
  refreshAllCustomProviders: () => request('/custom-providers/refresh-all', { method: 'POST' }),

  getSubscriptions: (status) => request(`/admin/subscriptions${status ? `?status=${status}` : ''}`),
  reviewSubscription: (id, data) =>
    request(`/admin/subscriptions/${id}/review`, { method: 'POST', body: data }),

  getConversations: () => request('/chat/conversations'),
  getConversation: (sid) => request(`/chat/conversations/${sid}`),
  deleteConversation: (sid) => request(`/chat/conversations/${sid}`, { method: 'DELETE' }),
  generateTitle: (sessionId, model) => request('/chat/generate-title', { method: 'POST', body: { session_id: sessionId, model } }),

  getUserSubscriptions: () => request('/user/subscriptions'),
  requestSubscription: (data) => request('/user/subscriptions', { method: 'POST', body: data }),
  getUserBilling: () => request('/user/billing'),

  // ------------------------------------------------------------------
  // Wallet / billing / API key (commerce surface, per /api/billing/*)
  // ------------------------------------------------------------------
  getWallet: () => request('/user/wallet'),
  getWalletTransactions: (limit = 50) => request(`/user/wallet/transactions?limit=${limit}`),
  createTopUpOrder: (data) => request('/user/orders', { method: 'POST', body: data }),
  getOrders: () => request('/user/orders'),
  getOrder: (orderNo) => request(`/user/orders/${orderNo}`),
  redeemCode: (code) => request('/user/redeem', { method: 'POST', body: { code } }),
  getPlans: () => request('/user/plans'),
  getMySubscription: () => request('/user/subscription'),
  subscribePlan: (data) => request('/user/subscription', { method: 'POST', body: data }),

  listMyApiKeys: () => request('/user/api-keys'),
  createApiKey: (data) => request('/user/api-keys', { method: 'POST', body: data }),
  revokeApiKey: (id) => request(`/user/api-keys/${id}`, { method: 'DELETE' }),
  rotateApiKey: (id, data) => request(`/user/api-keys/${id}`, { method: 'PATCH', body: data }),

  // Self-service account profile (username, email). Partial update —
  // only the fields you pass get touched; the rest stay as-is.
  updateProfile: (data) => request('/user/profile', { method: 'PATCH', body: data }),

  // Self-service password rotation. Requires the current password
  // for identity verification, and invalidates every server-side
  // session for this user on success.
  changePassword: (oldPassword, newPassword) =>
    request('/user/password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
    }),

  // GDPR self-service account deletion. Soft-deletes the user; the
  // daily worker purges the record after SOFT_DELETE_RETENTION_DAYS.
  // Requires the current password for identity verification.
  deleteUserData: (password) =>
    request('/user/data/delete', { method: 'POST', body: { password } }),

  // Usage / stats (user self-service)
  getMyUsage: (range = '7d') => request(`/user/usage/summary?range=${range}`),
  getMyUsageTrend: (range = '7d') => request(`/user/usage/daily?range=${range}`),

  // Dashboard (session-cookie auth, richer data for the Usage page)
  getDashboardSummary: () => request('/user/dashboard/summary'),
  getDashboardChart: (range = '30d') => request(`/user/dashboard/chart?range=${range}`),
  getDashboardByModel: (range = '30d') => request(`/user/dashboard/by-model?range=${range}`),
  getUserLogs: (limit = 200) => request(`/user/logs?limit=${limit}`),

  // Usage / stats (admin: platform-wide)
  getAdminOverview: (range = '7d') => request(`/admin/stats/overview?range=${range}`),
  getAdminTrend: (range = '7d') => request(`/admin/stats/trend?days=${parseRangeDays(range)}`),
  getAdminTopModels: (range = '7d', limit = 10) =>
    request(`/admin/stats/top-models?days=${parseRangeDays(range)}&limit=${limit}`),
  getAdminTopUsers: (range = '7d', limit = 10) =>
    request(`/admin/stats/top-users?days=${parseRangeDays(range)}&limit=${limit}`),
  getAdminProviderBreakdown: (range = '7d') =>
    request(`/admin/stats/by-provider?days=${parseRangeDays(range)}`),
  getAdminRevenue: (range = '7d') => request(`/admin/stats/revenue?days=${parseRangeDays(range)}`),
  getAdminReconciliationSummary: (days = 7) =>
    request(`/admin/stats/reconciliation-summary?days=${days}`),

  // L16: System health monitoring — DB pool stats + provider health.
  // /health/pools lives outside /api/ so it needs a raw fetch.
  getSystemHealth: async () => {
    const res = await fetch('/health/pools', { credentials: 'include' });
    if (!res.ok) throw new Error(`health/pools ${res.status}`);
    return res.json();
  },
  getProviderHealth: () => request('/admin/health/providers'),

  // API keys (admin: platform-wide view)
  listAllApiKeys: (userId) => request(`/admin/api-keys${userId ? `?user_id=${userId}` : ''}`),
  adminRevokeApiKey: (id) => request(`/admin/api-keys/${id}`, { method: 'DELETE' }),

  // Admin: redemption codes (兑换券 / 充值码).
  // The page is the admin counter for issuing one-off credits to
  // customers without going through the public order flow.
  listRedeemCodes: () => request('/admin/redeem-codes'),
  createRedeemCodes: (data) => request('/admin/redeem-codes', { method: 'POST', body: data }),
  revokeRedeemCode: (id) => request(`/admin/redeem-codes/${id}`, { method: 'DELETE' }),

  // Admin: promotion codes (优惠券).
  listPromoCodes: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.status) params.set('status', filters.status);
    if (filters.search) params.set('search', filters.search);
    const qs = params.toString();
    return request(`/admin/promo-codes${qs ? `?${qs}` : ''}`);
  },
  createPromoCode: (data) => request('/admin/promo-codes', { method: 'POST', body: data }),
  updatePromoCode: (id, data) =>
    request(`/admin/promo-codes/${id}`, { method: 'PATCH', body: data }),
  deletePromoCode: (id) => request(`/admin/promo-codes/${id}`, { method: 'DELETE' }),

  // Admin: pricing (per-model input/output price overrides).
  getAdminPricing: () => request('/admin/pricing'),
  createPricing: (data) => request('/admin/pricing', { method: 'POST', body: data }),
  updatePricing: (id, data) => request(`/admin/pricing/${id}`, { method: 'PATCH', body: data }),
  deletePricing: (id) => request(`/admin/pricing/${id}`, { method: 'DELETE' }),
  resetOfficialPricing: (params) =>
    request('/admin/pricing/reset-official', {
      method: 'POST',
      ...(params ? { body: params } : {}),
    }),

  // Public pricing page (no auth required) — returns the effective
  // (admin-custom OR official default) per-model price.
  getPublicPricing: () => request('/public/pricing'),

  // ------------------------------------------------------------------
  // Channels (admin: multi-key / failover management)
  // ------------------------------------------------------------------
  listChannels: (filters) => {
    const params = new URLSearchParams();
    if (filters && filters.provider) params.set('provider', filters.provider);
    const qs = params.toString();
    return request(`/admin/channels${qs ? `?${qs}` : ''}`);
  },
  createChannel: (body) => request('/admin/channels', { method: 'POST', body }),
  updateChannel: (id, body) => request(`/admin/channels/${id}`, { method: 'PATCH', body }),
  deleteChannel: (id) => request(`/admin/channels/${id}`, { method: 'DELETE' }),
  testChannel: (id) => request(`/admin/channels/${id}/test`, { method: 'POST' }),
  resetChannelCooldown: (id) => request(`/admin/channels/${id}/reset-cooldown`, { method: 'POST' }),
  toggleChannelActive: (id) => request(`/admin/channels/${id}/toggle-active`, { method: 'POST' }),

  // ------------------------------------------------------------------
  // Admin: order management (审批 / 退款)
  // ------------------------------------------------------------------
  listAdminOrders: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.status) params.set('status', filters.status);
    if (filters.user_id) params.set('user_id', filters.user_id);
    if (filters.limit) params.set('limit', filters.limit);
    if (filters.offset) params.set('offset', filters.offset);
    const qs = params.toString();
    return request(`/admin/orders${qs ? `?${qs}` : ''}`);
  },
  getAdminOrder: (id) => request(`/admin/orders/${id}`),
  approveOrder: (id, note) =>
    request(`/admin/orders/${id}/approve`, { method: 'POST', body: { note } }),
  rejectOrder: (id, reason) =>
    request(`/admin/orders/${id}/reject`, { method: 'POST', body: { reason } }),
  refundOrder: (id, reason) =>
    request(`/admin/orders/${id}/refund`, { method: 'POST', body: { reason } }),

  // ------------------------------------------------------------------
  // Admin: wallet adjustments (充值 / 扣款 + 交易记录)
  // ------------------------------------------------------------------
  getAdminUserWallet: (userId) => request(`/admin/users/${userId}/wallet`),
  adjustAdminWallet: (userId, delta, reason) =>
    request(`/admin/users/${userId}/wallet`, { method: 'POST', body: { delta, reason } }),
  getAdminWalletTransactions: (userId, limit = 50) =>
    request(`/admin/users/${userId}/wallet-transactions?limit=${limit}`),

  // ------------------------------------------------------------------
  // Admin: audit logs (审计日志)
  // ------------------------------------------------------------------
  listAuditLogs: (filters = {}) => {
    const params = new URLSearchParams();
    if (filters.actor_id) params.set('actor_id', filters.actor_id);
    if (filters.action) params.set('action', filters.action);
    if (filters.target_type) params.set('target_type', filters.target_type);
    if (filters.date_from) params.set('date_from', filters.date_from);
    if (filters.date_to) params.set('date_to', filters.date_to);
    if (filters.limit) params.set('limit', filters.limit);
    if (filters.offset) params.set('offset', filters.offset);
    const qs = params.toString();
    return request(`/admin/audit-logs${qs ? `?${qs}` : ''}`);
  },

  // ------------------------------------------------------------------
  // Admin: plan management (套餐管理)
  // ------------------------------------------------------------------
  listAdminPlans: () => request('/admin/plans'),
  createAdminPlan: (body) => request('/admin/plans', { method: 'POST', body }),
  updateAdminPlan: (id, body) => request(`/admin/plans/${id}`, { method: 'PATCH', body }),
  deleteAdminPlan: (id) => request(`/admin/plans/${id}`, { method: 'DELETE' }),

  // Admin: promo code revoke (soft-delete)
  revokePromoCode: (id) => request(`/admin/promo-codes/${id}/revoke`, { method: 'POST' }),

  // ------------------------------------------------------------------
  // Notifications (user: bell icon + drawer)
  // ------------------------------------------------------------------
  getNotifications: (limit = 50, unreadOnly = false) =>
    request(`/user/notifications?limit=${limit}&unread_only=${unreadOnly}`),
  getNotificationsUnreadCount: () => request('/user/notifications/unread-count'),
  markNotificationRead: (id) => request(`/user/notifications/${id}/read`, { method: 'POST' }),
  markAllNotificationsRead: () => request('/user/notifications/read-all', { method: 'POST' }),

  // ------------------------------------------------------------------
  // Payment gateway (online payment via Stripe / Alipay / WeChat)
  // ------------------------------------------------------------------
  payOrder: (orderNo, data) =>
    request(`/billing/orders/${orderNo}/pay`, { method: 'POST', body: data }),
  queryOrderPayment: (orderNo) => request(`/billing/orders/${orderNo}/query`, { method: 'POST' }),

  // Public: available payment providers for the PaymentDialog (admin-curated
  // via /admin/payment/providers). Disabled / unavailable ones are hidden.
  listPublicPaymentProviders: () => request('/billing/providers'),

  // Admin: payment provider management
  listPaymentProviders: () => request('/admin/payment/providers'),
  updatePaymentProvider: (name, data) =>
    request(`/admin/payment/providers/${name}`, { method: 'PATCH', body: data }),

  // ------------------------------------------------------------------
  // Subscription lifecycle management (user-facing)
  // ------------------------------------------------------------------
  getCurrentSubscription: () => request('/user/subscriptions/current'),
  upgradeSubscription: (subId, data) =>
    request(`/user/subscriptions/${subId}/upgrade`, { method: 'POST', body: data }),
  downgradeSubscription: (subId, data) =>
    request(`/user/subscriptions/${subId}/downgrade`, { method: 'POST', body: data }),
  cancelSubscription: (subId) => request(`/user/subscriptions/${subId}/cancel`, { method: 'POST' }),
  renewSubscription: (subId) => request(`/user/subscriptions/${subId}/renew`, { method: 'POST' }),
  updateAutoRecharge: (data) =>
    request('/user/wallet/auto-recharge', { method: 'PATCH', body: data }),

  // ------------------------------------------------------------------
  // Model pool (user-defined upstream model pools with priority routing)
  // ------------------------------------------------------------------
  createModelPool: (body) => request('/user/model-pools', { method: 'POST', body }),
  listModelPools: () => request('/user/model-pools'),
  updateModelPool: (id, body) => request(`/user/model-pools/${id}`, { method: 'PUT', body }),
  deleteModelPool: (id) => request(`/user/model-pools/${id}`, { method: 'DELETE' }),
  reorderModelPools: (orderedIds) =>
    request('/user/model-pools/reorder', { method: 'POST', body: { ordered_ids: orderedIds } }),

  // Unified model-pool keys (sk-ump_*): one key routes across all pools
  createModelPoolKey: (name) => request('/user/model-pool-keys', { method: 'POST', body: { name } }),
  listModelPoolKeys: () => request('/user/model-pool-keys'),
  deleteModelPoolKey: (id) => request(`/user/model-pool-keys/${id}`, { method: 'DELETE' }),
};

export default api;
