import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import NotificationsBell from './NotificationsBell';
import LowBalanceBanner from './LowBalanceBanner';
import { useEffect, useState } from 'react';
import api from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export default function AppShell() {
  const [pendingSubs, setPendingSubs] = useState(0);
  const role = useAuthStore((s) => s.role);

  useEffect(() => {
    // Pending subscriptions is an admin-only endpoint; skip for regular
    // users to avoid a guaranteed 401 that the global handler would
    // interpret as an expired session.
    if (role !== 'admin') return;
    let mounted = true;
    api
      .getSubscriptions('pending')
      .then((d) => mounted && setPendingSubs(Array.isArray(d) ? d.length : 0))
      .catch((err) => {
        // 401 is handled globally by api.js (it hard-navigates to
        // /login) — don't try to recover here, just swallow the error
        // so we don't log a useless stack trace.
        if (err && err.status !== 401) {
          // non-auth failure: leave the badge at 0
        }
      });
    return () => {
      mounted = false;
    };
  }, [role]);

  return (
    <div className="flex h-screen w-full bg-ink-50 text-ink-900 dark:bg-ink-950 dark:text-ink-100">
      <Sidebar pendingSubs={pendingSubs} />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-end border-b border-ink-200 bg-white px-4 py-1.5 dark:border-ink-700 dark:bg-ink-900">
          <NotificationsBell />
        </div>
        <LowBalanceBanner />
        <Outlet />
      </div>
    </div>
  );
}
