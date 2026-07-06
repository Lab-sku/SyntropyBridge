/**
 * NotificationsDrawer — right-side slide-in panel listing user
 * notifications with read/unread state and relative timestamps.
 */
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
  X,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Info,
  Megaphone,
  Clock,
  RefreshCw,
} from 'lucide-react';
import useNotificationsStore from '@/stores/notificationsStore';

const ICON_MAP = {
  order_approved: CheckCircle,
  order_rejected: XCircle,
  order_refunded: RefreshCw,
  low_balance: AlertTriangle,
  subscription_expiring: Clock,
  subscription_expired: Clock,
  admin_announcement: Megaphone,
};

const ICON_COLOR_MAP = {
  order_approved: 'text-green-500',
  order_rejected: 'text-red-500',
  order_refunded: 'text-blue-500',
  low_balance: 'text-amber-500',
  subscription_expiring: 'text-amber-500',
  subscription_expired: 'text-red-500',
  admin_announcement: 'text-indigo-500',
};

function relativeTime(dateStr, t) {
  if (!dateStr) return '';
  const now = Date.now();
  const then = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z').getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return t('notifications.relativeTime.justNow');
  if (diffMin < 60) return t('notifications.relativeTime.minutesAgo', { n: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t('notifications.relativeTime.hoursAgo', { n: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  return t('notifications.relativeTime.daysAgo', { n: diffDay });
}

function navigateTarget(notification) {
  const type = notification?.type;
  const meta = notification?.metadata || {};
  if (type === 'order_approved' || type === 'order_rejected' || type === 'order_refunded') {
    return meta.order_no ? `/billing?order=${meta.order_no}` : '/billing';
  }
  if (type === 'low_balance') return '/billing';
  return null;
}

export default function NotificationsDrawer({ open, onClose }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const notifications = useNotificationsStore((s) => s.notifications);
  const fetchNotifications = useNotificationsStore((s) => s.fetchNotifications);
  const markRead = useNotificationsStore((s) => s.markRead);
  const markAllRead = useNotificationsStore((s) => s.markAllRead);

  useEffect(() => {
    if (open) fetchNotifications();
  }, [open, fetchNotifications]);

  const handleClick = (notif) => {
    if (!notif.is_read) {
      markRead(notif.id);
    }
    const target = navigateTarget(notif);
    if (target) {
      navigate(target);
      onClose();
    }
  };

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-[950] bg-ink-950/30 backdrop-blur-sm transition-opacity"
          onClick={onClose}
        />
      )}

      {/* Drawer */}
      <div
        className={`fixed inset-y-0 right-0 z-[960] flex w-full max-w-sm flex-col border-l border-ink-200 bg-white shadow-2xl transition-transform duration-300 ease-out dark:border-ink-700 dark:bg-ink-900 ${
          open ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-ink-200 px-4 py-3 dark:border-ink-700">
          <h2 className="text-base font-semibold text-ink-900 dark:text-ink-100">
            {t('notifications.title')}
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={markAllRead}
              className="rounded-md px-2 py-1 text-xs text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800 dark:hover:text-ink-200"
            >
              {t('notifications.markAllRead')}
            </button>
            <button
              onClick={onClose}
              className="rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
              aria-label={t('common.close')}
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto">
          {notifications.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-ink-400">
              <Info size={32} className="mb-2" />
              <p className="text-sm">{t('notifications.empty')}</p>
            </div>
          ) : (
            <ul className="divide-y divide-ink-100 dark:divide-ink-800">
              {notifications.map((notif) => {
                const Icon = ICON_MAP[notif.type] || Info;
                const iconColor = ICON_COLOR_MAP[notif.type] || 'text-ink-400';
                return (
                  <li
                    key={notif.id}
                    onClick={() => handleClick(notif)}
                    className={`flex cursor-pointer gap-3 px-4 py-3 transition-colors hover:bg-ink-50 dark:hover:bg-ink-800 ${
                      !notif.is_read ? 'bg-ink-50/50 dark:bg-ink-800/30' : ''
                    }`}
                  >
                    {/* Icon */}
                    <div className={`mt-0.5 shrink-0 ${iconColor}`}>
                      <Icon size={18} />
                    </div>

                    {/* Content */}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-sm font-medium text-ink-900 dark:text-ink-100">
                          {notif.title}
                        </p>
                        {!notif.is_read && (
                          <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-blue-500" />
                        )}
                      </div>
                      <p className="mt-0.5 text-xs text-ink-500 dark:text-ink-400 line-clamp-2">
                        {notif.body}
                      </p>
                      <p className="mt-1 text-[11px] text-ink-400 dark:text-ink-500">
                        {relativeTime(notif.created_at, t)}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </>
  );
}
