import { cn } from '@/lib/utils';

export default function Logo({ size = 28, withText = true, className }) {
  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <div
        className="relative flex items-center justify-center rounded-lg bg-ink-900 text-white"
        style={{ width: size, height: size }}
        aria-hidden
      >
        <svg
          width={size * 0.6}
          height={size * 0.6}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="3" height="3" rx="0.6" />
          <rect x="18" y="14" width="3" height="3" rx="0.6" />
          <rect x="14" y="18" width="3" height="3" rx="0.6" />
          <rect x="18" y="18" width="3" height="3" rx="0.6" />
        </svg>
      </div>
      {withText && (
        <div className="flex flex-col leading-none">
          <span className="text-[15px] font-semibold tracking-tight text-ink-900">API Hub</span>
        </div>
      )}
    </div>
  );
}
