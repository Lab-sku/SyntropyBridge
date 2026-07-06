import { Search, Menu } from 'lucide-react';
import ThemeToggle from './ThemeToggle';
import LanguageToggle from './LanguageToggle';

/**
 * TopBar renders the page title, optional custom controls, and the
 * global theme / language toggles. The toggles live on the right edge
 * right next to the notification bell so they're always one click away
 * from any page, no matter which custom ``action`` the page also
 * wants to slot into the right side.
 *
 * Props
 * -----
 *  - title, subtitle, action, onMenu, children
 *      forwarded from the calling page (preserved 1:1).
 *  - hideGlobalToggles
 *      Pages that already embed their own toggles (e.g. the chat
 *      header) can opt out by passing ``true``.
 */
export default function TopBar({
  title,
  subtitle,
  action,
  onMenu,
  children,
  hideGlobalToggles = false,
}) {
  return (
    <div className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-ink-200 bg-white/80 px-4 backdrop-blur-md md:px-6 dark:border-ink-700 dark:bg-ink-900/80">
      {onMenu && (
        <button
          onClick={onMenu}
          className="rounded-md p-1.5 text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-900 md:hidden dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-ink-100"
        >
          <Menu size={18} />
        </button>
      )}
      <div className="min-w-0 flex-1">
        {title && (
          <div className="flex items-baseline gap-2">
            <h1 className="truncate text-[16px] font-semibold tracking-tight text-ink-900 dark:text-ink-100">
              {title}
            </h1>
            {subtitle && <span className="truncate text-[13.5px] text-ink-500 dark:text-ink-400">{subtitle}</span>}
          </div>
        )}
        {children}
      </div>
      {action}
      <div className="ml-1 flex shrink-0 items-center gap-1.5">
        <LanguageToggle size="sm" />
        <ThemeToggle size="sm" mode="dropdown" />
      </div>
    </div>
  );
}
