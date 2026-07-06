import { useEffect, useId, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
}) {
  const { t } = useTranslation();
  const ref = useRef(null);
  const titleId = useId();

  // Escape to close + simple focus trap (Tab cycles within the dialog).
  useEffect(() => {
    if (!open) return;
    const node = ref.current;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        onClose?.();
        return;
      }
      if (e.key !== 'Tab' || !node) return;
      const focusables = node.querySelectorAll(FOCUSABLE_SELECTOR);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !node.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (active === last || !node.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  // Auto-focus the first focusable element when the dialog opens.
  useEffect(() => {
    if (!open) return;
    const node = ref.current;
    if (!node) return;
    const raf = requestAnimationFrame(() => {
      const first = node.querySelector(FOCUSABLE_SELECTOR);
      if (first) {
        first.focus();
      } else {
        // Fall back to the container itself so keyboard users have a
        // resting place even when there's nothing to focus.
        node.setAttribute('tabindex', '-1');
        node.focus();
      }
    });
    return () => cancelAnimationFrame(raf);
  }, [open]);

  if (!open) return null;
  const sizes = {
    sm: 'max-w-sm',
    md: 'max-w-md',
    lg: 'max-w-xl',
    xl: 'max-w-2xl',
  };
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-gradient-to-b from-ink-950/50 to-ink-950/60 backdrop-blur-md animate-fade-in"
        onClick={onClose}
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        className={cn(
          'relative w-full overflow-hidden rounded-2xl border border-ink-200/60 bg-white shadow-2xl animate-slide-up dark:border-ink-700 dark:bg-ink-900',
          sizes[size],
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-ink-100/60 px-6 py-5 dark:border-ink-700/60">
          <div>
            {title && (
              <h2 id={titleId} className="text-[15px] font-semibold tracking-tight text-ink-900 dark:text-ink-100">
                {title}
              </h2>
            )}
            {description && <p className="mt-1 text-[12.5px] text-ink-500 dark:text-ink-400">{description}</p>}
          </div>
          <button
            onClick={onClose}
            aria-label={t('common.close')}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-400 transition-all hover:bg-ink-100 hover:text-ink-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
          >
            <X size={14} />
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-6 py-5">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-ink-100/60 bg-ink-50/40 px-6 py-4 dark:border-ink-700/60 dark:bg-ink-800/40">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
