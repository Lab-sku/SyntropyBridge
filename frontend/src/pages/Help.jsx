/**
 * Help — standalone help center page at /help with full FAQ expanded
 * and a search box that filters questions by keyword.
 *
 * Publicly accessible (no auth required).
 */
import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, HelpCircle, ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

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

export default function Help() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');

  const filteredKeys = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return FAQ_KEYS;
    return FAQ_KEYS.filter((key) => {
      const question = t(`help.faq.${key}.q`).toLowerCase();
      const answer = t(`help.faq.${key}.a`).toLowerCase();
      return question.includes(q) || answer.includes(q);
    });
  }, [query, t]);

  return (
    <div className="min-h-screen bg-white dark:bg-ink-950">
      {/* Header */}
      <div className="border-b border-ink-200 dark:border-ink-700">
        <div className="mx-auto flex max-w-3xl items-center gap-3 px-6 py-4">
          <button
            onClick={() => navigate(-1)}
            className="rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
            aria-label={t('common.back')}
          >
            <ArrowLeft size={18} />
          </button>
          <div className="flex-1">
            <h1 className="text-xl font-semibold text-ink-900 dark:text-ink-100">
              {t('help.title')}
            </h1>
            <p className="text-sm text-ink-500 dark:text-ink-400">{t('help.subtitle')}</p>
          </div>
        </div>
      </div>

      {/* Search */}
      <div className="mx-auto max-w-3xl px-6 pt-6">
        <div className="relative">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-400" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('help.searchPlaceholder')}
            className="w-full rounded-lg border border-ink-200 bg-ink-50 py-2.5 pl-9 pr-4 text-sm text-ink-900 outline-none transition-colors placeholder:text-ink-400 focus:border-ink-400 focus:ring-1 focus:ring-ink-400 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-100 dark:placeholder:text-ink-500"
          />
        </div>
      </div>

      {/* FAQ list */}
      <div className="mx-auto max-w-3xl space-y-4 px-6 py-6 pb-16">
        {filteredKeys.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <HelpCircle size={32} className="mb-3 text-ink-300 dark:text-ink-600" />
            <p className="text-sm text-ink-500 dark:text-ink-400">{t('help.noResults')}</p>
          </div>
        )}

        {filteredKeys.map((key) => (
          <div key={key} className="rounded-lg border border-ink-200 p-5 dark:border-ink-700">
            <h3 className="mb-2 flex items-start gap-2 text-sm font-semibold text-ink-900 dark:text-ink-100">
              <HelpCircle size={16} className="mt-0.5 shrink-0 text-ink-400" />
              {t(`help.faq.${key}.q`)}
            </h3>
            <p className="pl-6 text-sm leading-relaxed text-ink-600 dark:text-ink-400">
              {t(`help.faq.${key}.a`)}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
