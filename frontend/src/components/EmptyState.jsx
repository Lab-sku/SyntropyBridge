export default function EmptyState({ icon: Icon, title, description, action }) {
  return (
    <div className="group flex flex-col items-center justify-center gap-4 rounded-2xl border border-dashed border-ink-200/60 bg-gradient-to-br from-white/60 to-ink-50/40 px-6 py-16 text-center backdrop-blur-sm transition-all hover:border-ink-300/60 hover:shadow-soft dark:border-ink-700/60 dark:from-ink-900/60 dark:to-ink-950/40 dark:hover:border-ink-600/60">
      {Icon && (
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-ink-100 to-ink-50 text-ink-400 transition-all duration-300 group-hover:scale-105 group-hover:from-ink-200 group-hover:to-ink-100 group-hover:text-ink-600 shadow-soft dark:from-ink-800 dark:to-ink-900 dark:text-ink-500 dark:group-hover:from-ink-700 dark:group-hover:to-ink-800 dark:group-hover:text-ink-400">
          <Icon size={20} strokeWidth={2} />
        </div>
      )}
      <div>
        <div className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{title}</div>
        {description && (
          <div className="mx-auto mt-1.5 max-w-sm text-[12.5px] text-ink-500 dark:text-ink-400">{description}</div>
        )}
      </div>
      {action}
    </div>
  );
}
