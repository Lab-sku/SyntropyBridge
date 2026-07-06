/**
 * Notifications store — polls unread count and manages the
 * notifications list / read state.
 */
import { create } from 'zustand';
import api from '@/lib/api';

let _pollingTimer = null;

const useNotificationsStore = create((set, get) => ({
  notifications: [],
  unreadCount: 0,

  fetchNotifications: async (opts = {}) => {
    try {
      const data = await api.getNotifications(opts.limit, opts.unreadOnly);
      set({ notifications: Array.isArray(data) ? data : [] });
    } catch {
      // non-fatal: leave the list as-is
    }
  },

  fetchUnreadCount: async () => {
    try {
      const data = await api.getNotificationsUnreadCount();
      set({ unreadCount: data?.count ?? 0 });
    } catch {
      // non-fatal
    }
  },

  markRead: async (id) => {
    try {
      await api.markNotificationRead(id);
      set((s) => ({
        notifications: s.notifications.map((n) => (n.id === id ? { ...n, is_read: true } : n)),
        unreadCount: Math.max(0, s.unreadCount - 1),
      }));
    } catch {
      // non-fatal
    }
  },

  markAllRead: async () => {
    try {
      await api.markAllNotificationsRead();
      set((s) => ({
        notifications: s.notifications.map((n) => ({ ...n, is_read: true })),
        unreadCount: 0,
      }));
    } catch {
      // non-fatal
    }
  },

  startPolling: () => {
    if (_pollingTimer) return;
    // Fetch immediately on start, then every 60s.
    get().fetchUnreadCount();
    _pollingTimer = setInterval(() => {
      get().fetchUnreadCount();
    }, 60000);
  },

  stopPolling: () => {
    if (_pollingTimer) {
      clearInterval(_pollingTimer);
      _pollingTimer = null;
    }
  },

  /**
   * Hard-reset: clear notifications, unread count, and stop polling.
   * Called on logout / login to prevent cross-user data leakage.
   */
  resetStore: () => {
    if (_pollingTimer) {
      clearInterval(_pollingTimer);
      _pollingTimer = null;
    }
    set({ notifications: [], unreadCount: 0 });
  },
}));

export default useNotificationsStore;
