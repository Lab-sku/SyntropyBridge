/**
 * NotificationsBell — bell icon with unread badge. Clicking opens the
 * NotificationsDrawer. Polls unread count on mount.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell } from 'lucide-react';
import useNotificationsStore from '@/stores/notificationsStore';
import NotificationsDrawer from './NotificationsDrawer';

export default function NotificationsBell() {
  const { t } = useTranslation();
  const unreadCount = useNotificationsStore((s) => s.unreadCount);
  const startPolling = useNotificationsStore((s) => s.startPolling);
  const stopPolling = useNotificationsStore((s) => s.stopPolling);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    startPolling();
    return () => stopPolling();
  }, [startPolling, stopPolling]);

  return (
    <>
      <button
        onClick={() => setDrawerOpen(true)}
        className="relative rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800 dark:hover:text-ink-200"
        aria-label={t('notifications.title')}
      >
        <Bell size={18} />
        {unreadCount > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold leading-none text-white">
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
      </button>
      <NotificationsDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  );
}
