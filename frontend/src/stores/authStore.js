/**
 * Session-based auth store.
 *
 * The backend is the source of truth. This store mirrors the result of
 * `GET /api/auth/session` (cached in localStorage only for the *intent*
 * of "have we ever logged in" — the actual cookie lives in the browser
 * and is sent automatically via `credentials: 'include'`).
 *
 * The previous implementation kept a fake `adminToken` in localStorage
 * that was always `undefined` (the backend never returns a token), so
 * `ProtectedRoute` always redirected back to `/login`. The new flow
 * is: bootstrap session once on app start, then trust the result.
 */
import { create } from 'zustand';
import api, { _registerSessionExpireHandler } from '@/lib/api';
import i18n from '@/i18n';
import { useChatStore } from '@/stores/chatStore';
import useNotificationsStore from '@/stores/notificationsStore';

const CACHE_KEY = 'app_session_cache_v1';
const TOUR_KEY = 'app_tour_dismissed_v1';

function readTourDismissed() {
  try {
    return localStorage.getItem(TOUR_KEY) === '1';
  } catch {
    return false;
  }
}

function readCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed;
    return null;
  } catch {
    return null;
  }
}

function writeCache(state) {
  try {
    if (state && state.isAuthenticated) {
      localStorage.setItem(
        CACHE_KEY,
        JSON.stringify({
          isAuthenticated: true,
          role: state.role,
          user: state.user,
          // The cache is *only* for UX smoothness on the first paint.
          // The next request will re-confirm with the backend.
          ts: Date.now(),
        }),
      );
    } else {
      localStorage.removeItem(CACHE_KEY);
    }
  } catch {
    // localStorage may be unavailable; fall through silently.
  }
}

export const useAuthStore = create((set, get) => ({
  // Cache from localStorage so the first paint can decide the right
  // landing route without waiting for the network round-trip.
  // The store starts in ``ready=true`` and *no* cached session.
  isAuthenticated: false,
  role: null, // 'admin' | 'user' | null
  user: null, // { id, username, email, ... }
  ready: false, // becomes true after the first /auth/session call returns
  loading: false,
  error: null,
  dismissedTour: readTourDismissed(), // true once the user has seen/skipped the tour

  /**
   * Mark the onboarding tour as dismissed so it won't auto-show again.
   * Persists to localStorage.
   */
  dismissTour: () => {
    try {
      localStorage.setItem(TOUR_KEY, '1');
    } catch {
      // best-effort
    }
    set({ dismissedTour: true });
  },

  /**
   * Reset the tour flag so it auto-shows on next render.
   * Used by the "Replay tour" action in the help drawer.
   */
  replayTour: () => {
    try {
      localStorage.removeItem(TOUR_KEY);
    } catch {
      // best-effort
    }
    set({ dismissedTour: false });
  },

  /**
   * Bootstrap the session by asking the backend whether the current
   * browser cookies are valid. Called once on app mount.
   */
  checkSession: async () => {
    set({ loading: true, error: null });
    try {
      const data = await api.getSession();
      const next = {
        isAuthenticated: Boolean(data && data.authenticated),
        role: data && data.role ? data.role : null,
        // The session endpoint now returns username / email so the UI
        // can show a real display name instead of a placeholder.
        user:
          data && data.authenticated
            ? {
                id: data.user_id || data.admin_id || null,
                username: data.username || null,
                email: data.email || null,
                role: data.role,
              }
            : null,
      };
      set({ ...next, ready: true, loading: false, error: null });
      writeCache(next);
      return next;
    } catch (e) {
      // Network / server error: treat as logged-out but mark ``ready``
      // so the UI can render the login page instead of an infinite
      // loader.
      const next = { isAuthenticated: false, role: null, user: null };
      set({ ...next, ready: true, loading: false });
      writeCache(next);
      return next;
    }
  },

  /**
   * Admin login — calls /api/admin/login, then re-checks the session so
   * the store reflects the cookie that the backend just set.
   *
   * @param {string} username
   * @param {string} password
   * @param {boolean} [remember=false] — when true, the backend extends
   *   the session cookie lifetime to ~30 days. The *password* is still
   *   not stored anywhere on the client; the only thing that survives
   *   a browser restart is the server-side session (HttpOnly cookie).
   */
  adminLogin: async (username, password, remember = false) => {
    // Reset chat state so admin never sees a previous user's messages.
    useChatStore.getState().resetStore();
    useNotificationsStore.getState().resetStore();
    set({ loading: true, error: null });
    try {
      await api.adminLogin(username, password, remember);
      await get().checkSession();
      return get();
    } catch (e) {
      set({ loading: false, error: e.message || i18n.t('auth.loginFailed') });
      throw e;
    }
  },

  /**
   * User login — /api/auth/login. Same flow as admin login but the
   * resulting role is ``user``.
   */
  userLogin: async (username, password, remember = false, captcha = null) => {
    // Reset chat state so the new user never sees a previous user's messages.
    useChatStore.getState().resetStore();
    useNotificationsStore.getState().resetStore();
    set({ loading: true, error: null });
    try {
      await api.userLogin(username, password, remember, captcha);
      await get().checkSession();
      return get();
    } catch (e) {
      set({ loading: false, error: e.message || i18n.t('auth.loginFailed') });
      throw e;
    }
  },

  userLoginApiKey: async (apiKey, remember = false) => {
    // Reset chat state so the new user never sees a previous user's messages.
    useChatStore.getState().resetStore();
    useNotificationsStore.getState().resetStore();
    set({ loading: true, error: null });
    try {
      await api.userLoginApiKey(apiKey, remember);
      await get().checkSession();
      return get();
    } catch (e) {
      set({ loading: false, error: e.message || i18n.t('auth.loginFailed') });
      throw e;
    }
  },

  logout: async () => {
    // Abort any in-flight stream before tearing down.
    useChatStore.getState().abort?.();
    try {
      const role = get().role;
      if (role === 'admin') {
        await api.adminLogout().catch(() => {});
      } else if (role === 'user') {
        await api.userLogout().catch(() => {});
      }
    } finally {
      const next = { isAuthenticated: false, role: null, user: null };
      set({ ...next, loading: false, error: null });
      writeCache(next);
      // Reset chat state so the next login starts with a clean slate.
      useChatStore.getState().resetStore();
      useNotificationsStore.getState().resetStore();
    }
  },

  /**
   * Hard-reset the store without calling the server. Used when the
   * backend explicitly returns 401 on a guarded call so the UI can
   * immediately send the user back to /login.
   */
  expireSession: () => {
    const next = { isAuthenticated: false, role: null, user: null };
    set({ ...next, loading: false, error: null });
    writeCache(next);
    // Reset chat state so a forced session expiry never leaks messages.
    useChatStore.getState().resetStore();
    useNotificationsStore.getState().resetStore();
  },
}));

/**
 * Selector helpers — keep components subscribed to a single field so
 * they only re-render when *that* field changes.
 */
export const selectIsAuthed = (s) => s.isAuthenticated;
export const selectRole = (s) => s.role;
export const selectReady = (s) => s.ready;

/**
 * Initialise the store from the localStorage cache *synchronously* so
 * the very first render can already show the right thing. The network
 * call in ``App.jsx`` will overwrite this with the authoritative state.
 */
function bootstrapFromCache() {
  const cached = readCache();
  if (cached && cached.isAuthenticated) {
    useAuthStore.setState({
      isAuthenticated: true,
      role: cached.role || null,
      user: cached.user || null,
      ready: true, // Allow instant first paint; background checkSession will refresh
    });
  }
}
bootstrapFromCache();

/**
 * Register a one-way callback that api.js will invoke when it sees a
 * 401 from a non-public endpoint. We use the indirection in api.js
 * instead of an import cycle so the dependency arrow is one-way:
 *     authStore  --imports-->  api
 * The handler clears the local store state; the actual navigation
 * happens inside api.js (with a setTimeout to avoid mid-render
 * unmounts).
 */
_registerSessionExpireHandler(() => {
  useAuthStore.getState().expireSession();
});
