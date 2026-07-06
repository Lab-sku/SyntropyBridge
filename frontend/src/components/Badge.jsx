import { cn } from '@/lib/utils';

const variants = {
  default: 'bg-ink-100/80 text-ink-700 backdrop-blur-sm dark:bg-ink-800/80 dark:text-ink-300',
  success:
    'bg-gradient-to-r from-emerald-50 to-emerald-100/80 text-emerald-700 ring-1 ring-emerald-200/60 shadow-sm',
  warning:
    'bg-gradient-to-r from-amber-50 to-amber-100/80 text-amber-700 ring-1 ring-amber-200/60 shadow-sm',
  danger:
    'bg-gradient-to-r from-rose-50 to-rose-100/80 text-rose-700 ring-1 ring-rose-200/60 shadow-sm',
  info: 'bg-gradient-to-r from-sky-50 to-sky-100/80 text-sky-700 ring-1 ring-sky-200/60 shadow-sm',
  accent:
    'bg-gradient-to-r from-indigo-50 to-indigo-100/80 text-indigo-700 ring-1 ring-indigo-200/60 shadow-sm',
  dark: 'bg-gradient-to-r from-ink-800 to-ink-900 text-white shadow-md',
};

export default function Badge({ variant = 'default', dot = false, children, className }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition-all',
        variants[variant],
        className,
      )}
    >
      {dot && <span className="h-2 w-2 rounded-full bg-current ring-2 ring-current/20" />}
      {children}
    </span>
  );
}
