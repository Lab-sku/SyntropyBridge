import { useCallback, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { SUPPORTED_LANGUAGES } from '../i18n';

/**
 * Thin wrapper around react-i18next that exposes the bits of the i18n
 * surface the rest of the app should rely on:
 *
 *   - the active language (with a stable `lang` short code)
 *   - a memoised list of supported languages
 *   - `setLanguage(code)` that *also* writes to localStorage so the
 *     LanguageDetector picks the same value on the next page load
 *   - `toggleLanguage()` for a quick two-language switch
 *
 * The store is intentionally tiny — react-i18next already exposes i18n,
 * but we hide that detail so components don't reach into the global
 * instance directly.
 */
export function useLanguage() {
  const { i18n, t } = useTranslation();

  const current = i18n.resolvedLanguage || i18n.language || SUPPORTED_LANGUAGES[0].code;

  const setLanguage = useCallback(
    (code) => {
      if (!code || code === i18n.resolvedLanguage) return;
      i18n.changeLanguage(code);
    },
    [i18n],
  );

  const toggleLanguage = useCallback(() => {
    const idx = SUPPORTED_LANGUAGES.findIndex((l) => l.code === current);
    const next = SUPPORTED_LANGUAGES[(idx + 1) % SUPPORTED_LANGUAGES.length];
    setLanguage(next.code);
  }, [current, setLanguage]);

  const supported = useMemo(() => SUPPORTED_LANGUAGES, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (e) => {
      if (e.key !== 'app_language') return;
      if (e.newValue && e.newValue !== i18n.resolvedLanguage) {
        i18n.changeLanguage(e.newValue);
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [i18n]);

  return { t, lang: current, supported, setLanguage, toggleLanguage };
}
