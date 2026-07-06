import { Loader2 } from 'lucide-react';

export default function Button({
  variant = 'primary',
  size = 'md',
  loading,
  icon: Icon,
  children,
  className = '',
  ...rest
}) {
  const variants = {
    primary:
      'bg-gradient-to-r from-ink-800 to-ink-900 text-white hover:from-ink-900 hover:to-ink-950 active:scale-[0.98] shadow-soft transition-all duration-200 hover:shadow-md',
    secondary:
      'bg-white text-ink-700 border border-ink-200/60 hover:bg-ink-50 hover:border-ink-300/80 shadow-soft hover:shadow-md transition-all duration-200 dark:bg-ink-900 dark:text-ink-300 dark:border-ink-700/60 dark:hover:bg-ink-800 dark:hover:border-ink-600/80',
    ghost: 'text-ink-600 hover:bg-ink-100/60 hover:text-ink-900 transition-colors duration-150 dark:text-ink-400 dark:hover:bg-ink-800/60 dark:hover:text-ink-100',
    danger:
      'bg-gradient-to-r from-rose-50 to-rose-100/80 text-rose-700 hover:from-rose-100 hover:to-rose-200/80 border border-rose-200/60 shadow-soft transition-all duration-200',
    success:
      'bg-gradient-to-r from-emerald-50 to-emerald-100/80 text-emerald-700 hover:from-emerald-100 hover:to-emerald-200/80 border border-emerald-200/60 shadow-soft transition-all duration-200',
  };
  const sizes = {
    sm: 'h-8 px-3 text-[12px] gap-1.5',
    md: 'h-9 px-4 text-[13px] gap-2',
    lg: 'h-11 px-5 text-[14px] gap-2',
  };
  const iconSizes = { sm: 12, md: 14, lg: 16 };

  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={`inline-flex items-center justify-center rounded-xl font-medium active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed select-none ${variants[variant]} ${sizes[size]} ${className}`}
    >
      {loading ? (
        <Loader2 size={iconSizes[size] || 14} className="animate-spin" />
      ) : Icon ? (
        <Icon size={iconSizes[size] || 14} strokeWidth={2} />
      ) : null}
      {children}
    </button>
  );
}
