import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, X } from 'lucide-react';
import { useAuthStore } from '@/stores/authStore';
import useBalanceWarning from '@/hooks/useBalanceWarning';

const DISMISS_KEY = 'mm:low-balance-dismissed';

/**
 * Slim amber banner that warns the user when their wallet balance drops
 * below the low-balance threshold (100 credits).
 *
 * Placement: mounted inside AppShell, between the top notifications bar
 * and the <Outlet />. Only renders for authenticated non-admin users.
 *
 * Dismissal: stored in sessionStorage so it survives navigation within
 * the same tab but returns once the tab is closed.
 */
export default function LowBalanceBanner() {
  const { t } = useTranslation();
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const role = useAuthStore((s) => s.role);
  const { isLow, balance } = useBalanceWarning();

  const [dismissed, setDismissed] = useState(() => {
    try {
      return sessionStorage.getItem(DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  // When the balance recovers (e.g. after a top-up in another tab),
  // clear the dismissal flag so the banner can re-appear if it drops
  // again later.
  useEffect(() => {
    if (!isLow) {
      try {
        sessionStorage.removeItem(DISMISS_KEY);
      } catch {
        /* best-effort */
      }
      setDismissed(false);
    }
  }, [isLow]);

  const handleDismiss = useCallback(() => {
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISS_KEY, '1');
    } catch {
      /* best-effort */
    }
  }, []);

  // Guard: only show for authenticated, non-admin users with low balance.
  // balance === 0 is excluded so a brand-new user who hasn't been granted
  // their initial credits yet doesn't see a scary "low balance" banner
  // on first login.
  if (!isAuthenticated || role === 'admin' || !isLow || balance === 0 || dismissed) {
    return null;
  }

  return (
    <div className="flex items-center gap-2 border-b border-amber-300/60 bg-amber-50 px-4 py-2 text-[12.5px] text-amber-900 dark:border-amber-700/40 dark:bg-amber-900/20 dark:text-amber-200">
      <AlertTriangle size={14} className="shrink-0 text-amber-600 dark:text-amber-400" />
      <span className="flex-1">
        {t('wallet.lowBalance.message', { balance })}
      </span>
      <Link
        to="/wallet"
        className="shrink-0 rounded-md px-2 py-0.5 text-[12px] font-semibold text-amber-800 underline-offset-2 transition-colors hover:bg-amber-100 hover:underline dark:text-amber-300 dark:hover:bg-amber-800/30"
      >
        {t('wallet.lowBalance.topUp')}
      </Link>
      <button
        onClick={handleDismiss}
        aria-label={t('wallet.lowBalance.dismiss')}
        title={t('wallet.lowBalance.dismiss')}
        className="shrink-0 rounded-md p-0.5 text-amber-600 transition-colors hover:bg-amber-100 dark:text-amber-400 dark:hover:bg-amber-800/30"
      >
        <X size={14} />
      </button>
    </div>
  );
}
