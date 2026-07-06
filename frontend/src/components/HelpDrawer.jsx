/**
 * HelpDrawer — a right-side sliding panel with quick links, an FAQ
 * accordion, and version info.  Triggered by the floating HelpButton.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';
import { X, Play, BookOpen, HelpCircle, ChevronDown, Info } from 'lucide-react';

const FAQ_KEYS = [
  'quotas',
  'quotaHit',
  'billing',
  'errorCodes',
  'channels',
  'rotateKey',
  'usageHistory',
  'contactSupport',
];

export default function HelpDrawer({ open, onClose }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.role);
  const replayTour = useAuthStore((s) => s.replayTour);
  const [expandedFaq, setExpandedFaq] = useState(null);

  const handleReplayTour = () => {
    replayTour();
    onClose();
  };

  const handleOpenGuide = () => {
    navigate('/integration');
    onClose();
  };

  const handleViewFaq = () => {
    navigate('/help');
    onClose();
  };

  const toggleFaq = (key) => {
    setExpandedFaq((prev) => (prev === key ? null : key));
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
            {t('help.title')}
          </h2>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
            aria-label={t('common.close')}
          >
            <X size={18} />
          </button>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {/* Quick links */}
          <div className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
              {t('help.quickLinks')}
            </h3>
            <div className="space-y-1">
              <button
                onClick={handleReplayTour}
                className="flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm text-ink-700 transition-colors hover:bg-ink-50 dark:text-ink-300 dark:hover:bg-ink-800"
              >
                <Play size={14} className="shrink-0 text-ink-400" />
                {t('help.replayTour')}
              </button>
              <button
                onClick={handleOpenGuide}
                className="flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm text-ink-700 transition-colors hover:bg-ink-50 dark:text-ink-300 dark:hover:bg-ink-800"
              >
                <BookOpen size={14} className="shrink-0 text-ink-400" />
                {t('help.openGuide')}
              </button>
              <button
                onClick={handleViewFaq}
                className="flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm text-ink-700 transition-colors hover:bg-ink-50 dark:text-ink-300 dark:hover:bg-ink-800"
              >
                <HelpCircle size={14} className="shrink-0 text-ink-400" />
                {t('help.viewFaq')}
              </button>
            </div>
          </div>

          {/* FAQ accordion */}
          <div className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
              FAQ
            </h3>
            <div className="space-y-1">
              {FAQ_KEYS.map((key) => {
                const isOpen = expandedFaq === key;
                return (
                  <div key={key} className="rounded-md border border-ink-200 dark:border-ink-700">
                    <button
                      onClick={() => toggleFaq(key)}
                      className="flex w-full items-center justify-between px-3 py-2.5 text-left text-sm font-medium text-ink-800 transition-colors hover:bg-ink-50 dark:text-ink-200 dark:hover:bg-ink-800"
                    >
                      <span className="pr-2">{t(`help.faq.${key}.q`)}</span>
                      <ChevronDown
                        size={14}
                        className={`shrink-0 text-ink-400 transition-transform ${isOpen ? 'rotate-180' : ''}`}
                      />
                    </button>
                    {isOpen && (
                      <div className="border-t border-ink-200 px-3 py-2.5 text-sm leading-relaxed text-ink-600 dark:border-ink-700 dark:text-ink-400">
                        {t(`help.faq.${key}.a`)}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Version info */}
          <div>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-400">
              {t('help.versionInfo')}
            </h3>
            <div className="rounded-md border border-ink-200 px-3 py-2.5 dark:border-ink-700">
              <div className="flex items-center gap-2 text-xs text-ink-500 dark:text-ink-400">
                <Info size={12} />
                <span>
                  {t('help.version')}: 1.0.0 &middot; {t('help.env')}:{' '}
                  {import.meta.env.MODE || 'development'} &middot; {t('help.role')}: {role || '—'}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
