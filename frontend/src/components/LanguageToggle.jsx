import React, { useEffect, useRef, useState } from 'react';
import { useLanguage } from '../hooks/useLanguage';

/**
 * A small dropdown that lets the user pick between the supported languages.
 *
 * The toggle is intentionally unstyled in JS — we only emit semantic
 * classes (`.lang-toggle`, `.lang-toggle__btn`, `.lang-toggle__menu`,
 * `.lang-toggle__item`) and let `styles/ui.css` own the visuals so the
 * look matches the existing dark/light surface tokens.
 *
 * Why a popover and not a native <select>?
 *   - Native <select> cannot render custom icons or active check marks.
 *   - We need the option list to look identical to the rest of the
 *     admin / user panel (rounded card, hover state, keyboard nav).
 *   - The component must close on outside click + Esc, just like the
 *     existing modals.
 */
export default function LanguageToggle({ size = 'md' }) {
  const { lang, supported, setLanguage, t } = useLanguage();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const handleClick = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setOpen(false);
      }
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
  }, [open]);

  const current = supported.find((l) => l.code === lang) || supported[0];
  const cls = ['lang-toggle', size === 'sm' ? 'lang-toggle--sm' : null].filter(Boolean).join(' ');

  return (
    <div ref={wrapRef} className={cls}>
      <button
        type="button"
        className="lang-toggle__btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t('language.toggleLabel')}
        title={t('language.toggleLabel')}
        onClick={() => setOpen((v) => !v)}
      >
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
          <circle cx="12" cy="12" r="10" />
          <path d="M2 12h20" />
          <path d="M12 2a15 15 0 0 1 0 20" />
          <path d="M12 2a15 15 0 0 0 0 20" />
        </svg>
        {/* Long label for the default size so users can
                    tell what the button is for without hovering.
                    Compact short code for the sm variant. */}
        {size === 'sm' ? (
          <span className="lang-toggle__short">{current?.short || current?.code}</span>
        ) : (
          <span className="lang-toggle__label">{current?.label || current?.code}</span>
        )}
      </button>
      {open ? (
        <ul className="lang-toggle__menu" role="listbox" aria-label={t('language.toggleLabel')}>
          {supported.map((l) => {
            const active = l.code === current?.code;
            return (
              <li
                key={l.code}
                role="option"
                aria-selected={active}
                className={['lang-toggle__item', active ? 'is-active' : null]
                  .filter(Boolean)
                  .join(' ')}
                onClick={() => {
                  setLanguage(l.code);
                  setOpen(false);
                }}
              >
                <span className="lang-toggle__item-label">{l.label}</span>
                <span className="lang-toggle__item-code">{l.short}</span>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
