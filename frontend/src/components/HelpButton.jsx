/**
 * HelpButton — a floating "?" button fixed to the bottom-right corner.
 * Renders at the App.jsx root so it's visible on every authenticated
 * page.  Clicking it opens the HelpDrawer.
 */
import { useState } from 'react';
import { HelpCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import HelpDrawer from './HelpDrawer';
import { useAuthStore } from '@/stores/authStore';

export default function HelpButton() {
  const { t } = useTranslation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const isAuthed = useAuthStore((s) => s.isAuthenticated);

  if (!isAuthed) return null;

  return (
    <>
      <button
        onClick={() => setDrawerOpen(true)}
        className="fixed bottom-5 right-5 z-[900] flex h-11 w-11 items-center justify-center rounded-full bg-ink-900 text-white shadow-lg transition-all hover:scale-105 hover:bg-ink-800 active:scale-95 dark:bg-ink-100 dark:text-ink-900 dark:hover:bg-ink-200"
        aria-label={t('help.title')}
        title={t('help.title')}
      >
        <HelpCircle size={20} />
      </button>

      <HelpDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  );
}
