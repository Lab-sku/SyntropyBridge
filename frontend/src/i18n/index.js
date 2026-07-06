import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import zh from './locales/zh.json';
import en from './locales/en.json';

export const SUPPORTED_LANGUAGES = [
  { code: 'zh', label: '中文', short: '中' },
  { code: 'en', label: 'English', short: 'EN' },
];

export const LANGUAGE_STORAGE_KEY = 'app_language';

const detectorOptions = {
  // Order of detection. We intentionally keep localStorage first so a user's
  // explicit choice wins over the browser's navigator language.
  order: ['localStorage', 'navigator', 'htmlTag'],
  lookupLocalStorage: LANGUAGE_STORAGE_KEY,
  caches: ['localStorage'],
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      zh: { translation: zh },
      en: { translation: en },
    },
    fallbackLng: 'zh',
    supportedLngs: SUPPORTED_LANGUAGES.map((l) => l.code),
    load: 'currentOnly',
    interpolation: {
      // React already escapes content, so disable the extra escape pass.
      escapeValue: false,
    },
    detection: detectorOptions,
    react: {
      useSuspense: false,
    },
  });

export default i18n;
