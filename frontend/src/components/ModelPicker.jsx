import { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { Search, Check, ChevronDown, ChevronRight, Cpu, Hash } from 'lucide-react';
import { cn } from '@/lib/utils';
import ProviderLogo, { providerLabel } from './ProviderLogo';
import { useTranslation } from 'react-i18next';

export default function ModelPicker({
  models,
  value,
  onChange,
  buttonClassName,
  filterType = null,
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState(new Set());
  const ref = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    const onClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 30);
  }, [open]);

  const visibleModels = useMemo(() => {
    if (!filterType) return models || [];
    return (models || []).filter((m) => (m.type || 'chat') === filterType);
  }, [models, filterType]);

  const grouped = useMemo(() => {
    const acc = {};
    for (const m of visibleModels) {
      const key = m.provider || 'other';
      if (!acc[key]) acc[key] = [];
      acc[key].push(m);
    }
    return acc;
  }, [visibleModels]);

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const result = {};
    for (const [p, items] of Object.entries(grouped)) {
      const list = q
        ? items.filter(
            (m) =>
              (m.display_name || m.name).toLowerCase().includes(q) ||
              m.name.toLowerCase().includes(q),
          )
        : items;
      if (list.length > 0) result[p] = list;
    }
    return result;
  }, [grouped, query]);

  const orderedProviders = useMemo(() => {
    return Object.keys(filteredGroups).sort((a, b) => {
      const aSel = filteredGroups[a].some((m) => m.name === value) ? 0 : 1;
      const bSel = filteredGroups[b].some((m) => m.name === value) ? 0 : 1;
      if (aSel !== bSel) return aSel - bSel;
      return providerLabel(a).localeCompare(providerLabel(b));
    });
  }, [filteredGroups, value]);

  const current = (models || []).find((m) => m.name === value);
  const totalCount = visibleModels.length;
  const hiddenCount = (models || []).length - totalCount;

  // When dropdown opens, collapse all groups except the one with the
  // selected model so the user sees a compact view by default.
  useEffect(() => {
    if (!open) return;
    const selectedProvider = (models || []).find((m) => m.name === value)?.provider;
    const next = new Set();
    for (const p of Object.keys(grouped)) {
      if (p !== selectedProvider) next.add(p);
    }
    setCollapsed(next);
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleCollapse = useCallback((provider) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(provider)) next.delete(provider);
      else next.add(provider);
      return next;
    });
  }, []);

  const handleToggle = useCallback(() => setOpen((v) => !v), []);
  const handleClear = useCallback(() => setQuery(''), []);
  const handleSelect = useCallback(
    (name) => {
      onChange(name);
      setOpen(false);
      setQuery('');
    },
    [onChange],
  );

  const typeLabel = (key) => t(`modelPicker.typeLabels.${key}`, { defaultValue: key });

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={handleToggle}
        className={cn(
          'group inline-flex h-9 items-center gap-2 rounded-lg border border-ink-200 bg-white px-2.5 text-[14px] font-medium text-ink-700 transition-all duration-150 hover:border-ink-300 hover:bg-ink-50 active:scale-[0.99] dark:border-ink-700 dark:bg-ink-900 dark:text-ink-300 dark:hover:border-ink-600 dark:hover:bg-ink-800',
          buttonClassName,
        )}
      >
        {current ? (
          <ProviderLogo provider={current.provider} size={16} />
        ) : (
          <Cpu size={14} className="text-ink-500" />
        )}
        <span className="max-w-[180px] truncate">
          {current ? current.display_name || current.name : t('modelPicker.selectModel')}
        </span>
        <ChevronDown
          size={13}
          className={cn('text-ink-400 transition-transform duration-200', open && 'rotate-180')}
        />
      </button>

      {open && (
        <div
          className="absolute right-0 top-[calc(100%+6px)] z-50 flex w-[380px] flex-col overflow-hidden rounded-xl border border-ink-200 bg-white/95 shadow-pop backdrop-blur-xl dark:border-ink-700 dark:bg-ink-900/95"
          style={{ maxHeight: 'min(560px, 70vh)' }}
        >
          <div className="border-b border-ink-100 p-2.5 dark:border-ink-700">
            <div className="flex items-center gap-2 rounded-lg border border-ink-200 bg-ink-50/60 px-2.5 dark:border-ink-700 dark:bg-ink-800/60">
              <Search size={13} className="text-ink-400" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={
                  filterType
                    ? t('modelPicker.searchPlaceholderWithType', { type: typeLabel(filterType) })
                    : t('modelPicker.searchPlaceholder')
                }
                className="h-7 w-full bg-transparent text-[13px] text-ink-900 placeholder-ink-400 outline-none dark:text-ink-100 dark:placeholder-ink-500"
              />
              {query && (
                <button
                  onClick={handleClear}
                  className="text-[11px] text-ink-400 hover:text-ink-700 transition-colors dark:text-ink-500 dark:hover:text-ink-300"
                >
                  {t('modelPicker.clear')}
                </button>
              )}
            </div>
            <div className="mt-2 flex items-center justify-between px-1 text-[10.5px] text-ink-500">
              <span>
                {t('modelPicker.platformCount', { count: Object.keys(filteredGroups).length })}
              </span>
              <span>
                {t('modelPicker.modelCount', { count: totalCount })}
                {filterType && hiddenCount > 0
                  ? ` · ${t('modelPicker.hiddenCount', { count: hiddenCount, type: typeLabel(filterType) })}`
                  : ''}
              </span>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-1.5 scrollbar-thin">
            {orderedProviders.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-ink-100 text-ink-400 dark:bg-ink-800 dark:text-ink-500">
                  <Search size={14} />
                </div>
                <div className="text-[12.5px] text-ink-500">{t('modelPicker.noMatch')}</div>
              </div>
            ) : (
              orderedProviders.map((p) => {
                const isCollapsed = !query && collapsed.has(p);
                return (
                <div key={p} className="mb-1.5">
                  <button
                    type="button"
                    onClick={() => toggleCollapse(p)}
                    className="flex w-full items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-ink-50 dark:hover:bg-ink-800/50 rounded-md"
                  >
                    {isCollapsed ? (
                      <ChevronRight size={12} className="shrink-0 text-ink-400" />
                    ) : (
                      <ChevronDown size={12} className="shrink-0 text-ink-400" />
                    )}
                    <ProviderLogo provider={p} size={16} />
                    <span className="text-[11px] font-semibold text-ink-700 dark:text-ink-300">
                      {providerLabel(p)}
                    </span>
                    <span className="ml-auto rounded-full bg-ink-100 px-1.5 py-0.5 text-[10px] font-medium text-ink-500 dark:bg-ink-800 dark:text-ink-400">
                      {filteredGroups[p].length}
                    </span>
                  </button>
                  {!isCollapsed && filteredGroups[p].map((m) => {
                    const selected = m.name === value;
                    const pricing = m.pricing || null;
                    const fmt = (v) => (v == null ? '—' : (Number(v) || 0).toFixed(2));
                    return (
                      <button
                        key={`${p}-${m.name}`}
                        onClick={() => handleSelect(m.name)}
                        className={cn(
                          'group flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors',
                          selected ? 'bg-ink-900/[0.04] dark:bg-ink-100/10' : 'hover:bg-ink-100 dark:hover:bg-ink-800',
                        )}
                      >
                        <div
                          className={cn(
                            'flex h-6 w-6 shrink-0 items-center justify-center rounded-md border',
                            selected
                              ? 'border-ink-900 bg-ink-900 text-white dark:border-ink-100 dark:bg-ink-100 dark:text-ink-900'
                              : 'border-ink-200 bg-white text-ink-500 group-hover:border-ink-300 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-400 dark:group-hover:border-ink-600',
                          )}
                        >
                          {selected ? <Check size={12} strokeWidth={3} /> : <Hash size={11} />}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13px] font-medium text-ink-900 dark:text-ink-100">
                            {m.display_name || m.name}
                          </div>
                          <div className="truncate font-mono text-[11px] text-ink-400">
                            {m.name}
                          </div>
                        </div>
                        <div className="flex shrink-0 flex-col items-end gap-0.5">
                          {m.context_length ? (
                            <span className="rounded-full bg-ink-100 px-1.5 py-0.5 text-[10px] text-ink-500 dark:bg-ink-800 dark:text-ink-400">
                              {(m.context_length / 1000).toFixed(0)}k
                            </span>
                          ) : null}
                          {pricing ? (
                            <span
                              className="rounded-full bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-medium text-emerald-700"
                              title={t('modelPicker.pricingTooltip', { input: fmt(pricing.input_per_1k), output: fmt(pricing.output_per_1k) })}
                            >
                              {fmt(pricing.input_per_1k)}/{fmt(pricing.output_per_1k)}
                              <span className="ml-0.5 opacity-60">cr</span>
                            </span>
                          ) : null}
                        </div>
                      </button>
                    );
                  })}
                </div>
              );
              })
            )}
          </div>
          <div className="border-t border-ink-100 px-3 py-2 text-[11px] text-ink-400 dark:border-ink-700 dark:text-ink-500">
            {t('modelPicker.permissionHint')}
          </div>
        </div>
      )}
    </div>
  );
}
