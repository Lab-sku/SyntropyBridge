import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useState, useCallback } from 'react';
import {
  LayoutDashboard,
  MessageSquare,
  Users,
  Receipt,
  KeyRound,
  ServerCog,
  PlugZap,
  ScrollText,
  CreditCard,
  LogOut,
  Gift,
  Menu,
  X,
  ShoppingCart,
  Wallet,
  FileSearch,
  Package,
  Percent,
  Server,
  Settings,
  Layers,
} from 'lucide-react';
import Logo from './Logo';
import LanguageToggle from './LanguageToggle';
import ThemeToggle from './ThemeToggle';
import { useAuthStore } from '@/stores/authStore';
import { cn } from '@/lib/utils';

export default function Sidebar({ pendingSubs = 0 }) {
  const { t } = useTranslation();
  const location = useLocation();
  const nav = useNavigate();
  const role = useAuthStore((s) => s.role);
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close mobile sidebar on navigation
  const closeMobile = useCallback(() => setMobileOpen(false), []);

  const adminNav = [
    { to: '/admin', label: t('nav.overview'), icon: LayoutDashboard, end: true },
    { to: '/chat', label: t('nav.chat'), icon: MessageSquare },
    { to: '/admin/providers', label: t('nav.providers'), icon: ServerCog },
    { to: '/admin/custom-providers', label: t('nav.customProviders'), icon: PlugZap },
    { to: '/admin/users', label: t('nav.users'), icon: Users },
    { to: '/admin/subscriptions', label: t('nav.subscriptions'), icon: KeyRound },
    { to: '/admin/billing', label: t('nav.billing'), icon: Receipt },
    { to: '/admin/redeem-codes', label: t('nav.redeemCodes'), icon: Gift },
    { to: '/admin/pricing', label: t('nav.pricing'), icon: CreditCard },
    { to: '/admin/logs', label: t('nav.logs'), icon: ScrollText },
    // Billing & Operations section
    { section: true, label: t('navOps.label') },
    { to: '/admin/orders', label: t('navOps.orders'), icon: ShoppingCart },
    { to: '/admin/wallet-adjust', label: t('navOps.walletAdjust'), icon: Wallet },
    { to: '/admin/audit-logs', label: t('navOps.auditLogs'), icon: FileSearch },
    { to: '/admin/plans', label: t('navOps.plans'), icon: Package },
    { to: '/admin/promo-codes', label: t('navOps.promoCodes'), icon: Percent },
    { to: '/admin/channels', label: t('navOps.channels'), icon: Server },
    { to: '/admin/settings', label: t('navOps.settings'), icon: Settings },
  ];

  const userNav = [
    { to: '/chat', label: t('nav.chat'), icon: MessageSquare, end: true },
    { to: '/usage', label: t('usage.title'), icon: Receipt },
    { to: '/wallet', label: t('wallet.title'), icon: CreditCard },
    { to: '/model-pool', label: t('modelPool.title'), icon: Layers },
    // Note: do NOT add an `/account` entry here. The bottom "Account"
    // section below already exposes a single, more descriptive link
    // ("Plan & API Key" / "套餐与 API Key") to the same page. Adding
    // both would cause two nav items to light up as active at once,
    // which looked like a multi-select bug.
  ];

  const items = role === 'admin' ? adminNav : userNav;

  const onLogout = async () => {
    await logout();
    nav('/login', { replace: true });
  };

  const initial =
    (user && user.username && user.username[0] && user.username[0].toUpperCase()) ||
    (role === 'admin' ? 'A' : 'U');
  const displayName = (user && user.username) || (role === 'admin' ? 'Admin' : 'User');
  const displayEmail = (user && user.email) || (role === 'admin' ? 'admin@apihub.local' : '');

  // Inner navigation content — shared between desktop and mobile panels
  const navContent = (
    <>
      <nav className="flex-1 overflow-y-auto px-2.5 py-2">
        <div className="mb-1.5 px-2 pt-1 text-[11px] font-semibold uppercase tracking-wider text-ink-400 dark:text-ink-500">
          {role === 'admin' ? t('nav.workspace') : t('sidebar.menu')}
        </div>
        {items.map((item, idx) => {
          // Section divider — render a label, not a link.
          if (item.section) {
            return (
              <div
                key={`section-${idx}`}
                className="mb-1.5 mt-5 px-2 text-[11px] font-semibold uppercase tracking-wider text-ink-400 dark:text-ink-500"
              >
                {item.label}
              </div>
            );
          }
          const Icon = item.icon;
          const active = item.end
            ? location.pathname === item.to
            : location.pathname === item.to || location.pathname.startsWith(item.to + '/');
          const showBadge = item.to === '/admin/subscriptions' && pendingSubs > 0;
          return (
            <Link
              key={item.to}
              to={item.to}
              onClick={closeMobile}
              className={cn(
                'group flex h-9 items-center gap-2.5 rounded-md px-2.5 text-[14px] font-medium transition-all duration-150',
                active
                  ? 'bg-ink-900 text-white dark:bg-ink-100 dark:text-ink-900'
                  : 'text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-ink-100',
              )}
            >
              <Icon size={15} strokeWidth={active ? 2.4 : 2} />
              <span className="flex-1">{item.label}</span>
              {showBadge && (
                <span className="rounded-full bg-rose-500 px-1.5 py-0.5 text-[11px] font-semibold text-white">
                  {pendingSubs}
                </span>
              )}
            </Link>
          );
        })}

        {role !== 'admin' && (
          <div className="mb-1.5 mt-5 px-2 text-[11px] font-semibold uppercase tracking-wider text-ink-400 dark:text-ink-500">
            {t('nav.account')}
          </div>
        )}
        {role === 'user' && (
          <Link
            to="/account"
            onClick={closeMobile}
            className={cn(
              'flex h-9 items-center gap-2.5 rounded-md px-2.5 text-[14px] font-medium transition-all duration-150',
              location.pathname === '/account' || location.pathname.startsWith('/account/')
                ? 'bg-ink-900 text-white dark:bg-ink-100 dark:text-ink-900'
                : 'text-ink-600 hover:bg-ink-100 hover:text-ink-900 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-ink-100',
            )}
          >
            <CreditCard size={15} strokeWidth={2} />
            <span>{t('nav.planAndKey')}</span>
          </Link>
        )}
      </nav>

      <div className="border-t border-ink-200 bg-ink-50/40 p-2.5 dark:border-ink-700 dark:bg-ink-900/40">
        <div className="mb-2 flex items-center gap-1.5">
          <LanguageToggle />
          <ThemeToggle mode="dropdown" />
        </div>
        <div className="flex items-center gap-2 rounded-md px-2 py-1.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-ink-700 to-ink-900 text-[11px] font-semibold text-white">
            {initial}
          </div>
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-[13px] font-medium text-ink-900 dark:text-ink-100">{displayName}</div>
            {displayEmail && (
              <div className="truncate text-[11.5px] text-ink-500 dark:text-ink-400">{displayEmail}</div>
            )}
          </div>
          <button
            onClick={onLogout}
            title={t('sidebar.signOut')}
            aria-label={t('sidebar.signOut')}
            className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
          >
            <LogOut size={14} />
          </button>
        </div>
      </div>
    </>
  );

  return (
    <>
      {/* Mobile hamburger button — visible only below md breakpoint */}
      <button
        onClick={() => setMobileOpen(true)}
        className="fixed left-3 top-3 z-40 flex h-9 w-9 items-center justify-center rounded-lg border border-ink-200 bg-white/90 shadow-soft backdrop-blur-sm transition-colors hover:bg-ink-50 dark:border-ink-700 dark:bg-ink-900/90 dark:hover:bg-ink-800 md:hidden"
        aria-label={t('sidebar.openNav')}
      >
        <Menu size={18} className="text-ink-700 dark:text-ink-300" />
      </button>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-ink-950/30 backdrop-blur-sm md:hidden"
          onClick={closeMobile}
        />
      )}

      {/* Mobile slide-in panel */}
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-ink-200 bg-white transition-transform duration-200 ease-out md:hidden dark:border-ink-700 dark:bg-ink-900',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-14 items-center justify-between px-4">
          <Link
            to={role === 'admin' ? '/admin' : '/chat'}
            onClick={closeMobile}
            className="transition-opacity hover:opacity-80"
          >
            <Logo />
          </Link>
          <button
            onClick={closeMobile}
            className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
          >
            <X size={16} />
          </button>
        </div>
        {navContent}
      </aside>

      {/* Desktop sidebar — always visible at md+ */}
      <aside className="hidden w-[232px] shrink-0 flex-col border-r border-ink-200 bg-white md:flex dark:border-ink-700 dark:bg-ink-900">
        <div className="flex h-14 items-center px-4">
          <Link
            to={role === 'admin' ? '/admin' : '/chat'}
            className="transition-opacity hover:opacity-80"
          >
            <Logo />
          </Link>
        </div>
        {navContent}
      </aside>
    </>
  );
}
