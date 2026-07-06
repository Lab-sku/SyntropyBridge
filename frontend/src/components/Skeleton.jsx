import { cn } from '@/lib/utils';

export function Skeleton({ className }) {
  return (
    <div
      className={cn(
        'animate-pulse rounded-xl bg-gradient-to-r from-ink-100 via-ink-200/60 to-ink-100 bg-[length:200%_100%] dark:from-ink-800 dark:via-ink-700/60 dark:to-ink-800',
        className,
      )}
    />
  );
}

export function CardSkeleton() {
  return (
    <div className="card rounded-2xl border border-ink-200/40 bg-white p-5 shadow-soft dark:border-ink-700/40 dark:bg-ink-900">
      <div className="flex items-center gap-3">
        <Skeleton className="h-10 w-10 rounded-xl" />
        <div className="flex-1 space-y-2.5">
          <Skeleton className="h-3 w-28" />
          <Skeleton className="h-5 w-36" />
        </div>
      </div>
    </div>
  );
}

export function TableRowSkeleton({ cols = 4 }) {
  return (
    <div className="flex items-center gap-4 border-b border-ink-100/60 px-4 py-3.5 dark:border-ink-700/60">
      {Array.from({ length: cols }).map((_, i) => (
        <Skeleton key={i} className="h-3 flex-1 rounded-lg" />
      ))}
    </div>
  );
}
