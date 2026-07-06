/**
 * OnboardingTour — a 5-step modal overlay that introduces new users to
 * the main UI areas.  Steps are plain centered modals (no element
 * cut-out) to keep the implementation simple and layout-independent.
 *
 * Auto-shows on first login (dismissedTour === false) and can be
 * replayed from the help drawer at any time.
 */
import { useEffect, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/stores/authStore';
import {
  X,
  ChevronLeft,
  ChevronRight,
  Sparkles,
  LayoutPanelLeft,
  Cpu,
  UserCircle,
  PartyPopper,
} from 'lucide-react';

const STEP_KEYS = ['welcome', 'sidebar', 'modelPicker', 'userMenu', 'done'];

const STEP_ICONS = {
  welcome: Sparkles,
  sidebar: LayoutPanelLeft,
  modelPicker: Cpu,
  userMenu: UserCircle,
  done: PartyPopper,
};

export default function OnboardingTour() {
  const { t } = useTranslation();
  const isAuthed = useAuthStore((s) => s.isAuthenticated);
  const dismissedTour = useAuthStore((s) => s.dismissedTour);
  const dismissTour = useAuthStore((s) => s.dismissTour);

  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  // Auto-open when authenticated and tour has not been dismissed.
  useEffect(() => {
    if (isAuthed && !dismissedTour) {
      setOpen(true);
      setStep(0);
    }
  }, [isAuthed, dismissedTour]);

  const close = useCallback(() => {
    setOpen(false);
    dismissTour();
  }, [dismissTour]);

  const next = useCallback(() => {
    if (step < STEP_KEYS.length - 1) {
      setStep((s) => s + 1);
    } else {
      close();
    }
  }, [step, close]);

  const prev = useCallback(() => {
    if (step > 0) setStep((s) => s - 1);
  }, [step]);

  if (!open) return null;

  const key = STEP_KEYS[step];
  const Icon = STEP_ICONS[key];
  const title = t(`onboarding.${key}.title`);
  const desc = t(`onboarding.${key}.desc`);
  const isLast = step === STEP_KEYS.length - 1;
  const isFirst = step === 0;

  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-ink-950/60 backdrop-blur-sm" onClick={close} />

      {/* Modal card */}
      <div className="relative z-10 mx-4 w-full max-w-md rounded-xl border border-ink-200 bg-white p-6 shadow-2xl dark:border-ink-700 dark:bg-ink-900">
        {/* Close button */}
        <button
          onClick={close}
          className="absolute right-3 top-3 rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
          aria-label={t('common.close')}
        >
          <X size={16} />
        </button>

        {/* Step indicator */}
        <div className="mb-4 flex items-center gap-1.5">
          {STEP_KEYS.map((_, i) => (
            <div
              key={i}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i <= step ? 'bg-ink-900 dark:bg-ink-100' : 'bg-ink-200 dark:bg-ink-700'
              }`}
            />
          ))}
        </div>

        {/* Icon */}
        <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-ink-100 dark:bg-ink-800">
          <Icon size={24} className="text-ink-700 dark:text-ink-300" />
        </div>

        {/* Content */}
        <h2 className="mb-2 text-lg font-semibold text-ink-900 dark:text-ink-100">{title}</h2>
        <p className="mb-6 text-sm leading-relaxed text-ink-600 dark:text-ink-400">{desc}</p>

        {/* Actions */}
        <div className="flex items-center justify-between">
          <button
            onClick={close}
            className="text-sm font-medium text-ink-500 transition-colors hover:text-ink-700 dark:hover:text-ink-300"
          >
            {t('onboarding.skip')}
          </button>

          <div className="flex items-center gap-2">
            {!isFirst && (
              <button
                onClick={prev}
                className="flex items-center gap-1 rounded-md border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 transition-colors hover:bg-ink-50 dark:border-ink-700 dark:text-ink-300 dark:hover:bg-ink-800"
              >
                <ChevronLeft size={14} />
                {t('onboarding.prev')}
              </button>
            )}
            <button
              onClick={isLast ? close : next}
              className="flex items-center gap-1 rounded-md bg-ink-900 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-ink-800 dark:bg-ink-100 dark:text-ink-900 dark:hover:bg-ink-200"
            >
              {isLast ? t('onboarding.finish') : t('onboarding.next')}
              {!isLast && <ChevronRight size={14} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
