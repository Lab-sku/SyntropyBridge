import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../hooks/useTheme';

/**
 * Theme toggle.
 *
 *   <ThemeToggle />                  → full dropdown (light / dark / system)
 *   <ThemeToggle mode="simple" />    → single button that just toggles
 *                                       between light and dark (back-compat
 *                                       with the old icon button that the
 *                                       existing screens still use)
 *
 * Why a dropdown? Three reasons:
 *
 *   1. The dark/light *system* option is important — without it users on
 *      macOS / Windows who flip the OS theme at sunset end up out of
 *      sync.
 *   2. The single-icon button is fine for power users but new users
 *      need a label so they can tell what clicking will do.
 *   3. It mirrors the LanguageToggle UX so the corner of the screen
 *      stays visually consistent.
 */
export default function ThemeToggle({
  size = 'md',
  mode = 'dropdown',
  onToggle,
  resolvedTheme: resolvedOverride,
}) {
  const { t } = useTranslation();
  const ctx = useTheme();
  const { theme, resolvedTheme, setTheme } = ctx;
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);

  // Allow the *simple* API (`onToggle` / `resolvedTheme` props) to keep
  // working for callers that haven't been upgraded yet.
  const onToggleFn = onToggle || ctx.toggleTheme;
  const resolved = resolvedOverride || resolvedTheme;

  useEffect(() => {
    if (mode !== 'dropdown' || !open) return undefined;
    const handleClick = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    const handleKey = (e) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open, mode]);

  if (mode === 'simple') {
    return (
      <button
        type="button"
        className="ui-icon-btn"
        onClick={onToggleFn}
        aria-label={t('theme.toggleLabel')}
        title={t('theme.toggleLabel')}
      >
        {resolved === 'dark' ? <MoonIcon /> : <SunIcon />}
      </button>
    );
  }

  const cls = ['theme-toggle', size === 'sm' ? 'theme-toggle--sm' : null].filter(Boolean).join(' ');

  return (
    <div ref={wrapRef} className={cls}>
      <button
        type="button"
        className="theme-toggle__btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t('theme.toggleLabel')}
        title={t('theme.toggleLabel')}
        onClick={() => setOpen((v) => !v)}
      >
        {resolved === 'dark' ? <MoonIcon /> : <SunIcon />}
      </button>
      {open ? (
        <ul className="theme-toggle__menu" role="listbox" aria-label={t('theme.toggleLabel')}>
          {[
            {
              value: 'light',
              icon: <SunIcon />,
              label: t('theme.light'),
              hint: t('theme.lightHint'),
            },
            {
              value: 'dark',
              icon: <MoonIcon />,
              label: t('theme.dark'),
              hint: t('theme.darkHint'),
            },
            {
              value: 'system',
              icon: <SystemIcon />,
              label: t('theme.system'),
              hint: t('theme.systemHint'),
            },
          ].map((opt) => {
            const active = theme === opt.value;
            return (
              <li
                key={opt.value}
                role="option"
                aria-selected={active}
                className={['theme-toggle__item', active ? 'is-active' : null]
                  .filter(Boolean)
                  .join(' ')}
                onClick={() => {
                  setTheme(opt.value);
                  setOpen(false);
                }}
              >
                <span className="theme-toggle__item-icon" aria-hidden="true">
                  {opt.icon}
                </span>
                <span className="theme-toggle__item-label">
                  <span>{opt.label}</span>
                  <span className="theme-toggle__item-hint">{opt.hint}</span>
                </span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

function SunIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="4" />
      <line x1="12" y1="20" x2="12" y2="22" />
      <line x1="4.93" y1="4.93" x2="6.34" y2="6.34" />
      <line x1="17.66" y1="17.66" x2="19.07" y2="19.07" />
      <line x1="2" y1="12" x2="4" y2="12" />
      <line x1="20" y1="12" x2="22" y2="12" />
      <line x1="4.93" y1="19.07" x2="6.34" y2="17.66" />
      <line x1="17.66" y1="6.34" x2="19.07" y2="4.93" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function SystemIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <line x1="8" y1="20" x2="16" y2="20" />
      <line x1="12" y1="16" x2="12" y2="20" />
    </svg>
  );
}
