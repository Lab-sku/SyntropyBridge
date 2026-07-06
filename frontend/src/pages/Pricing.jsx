import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Coins,
  Plus,
  RefreshCw,
  Search,
  TrendingUp,
  Check,
  X,
  Edit3,
  RotateCcw,
  Tag,
  Filter,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import Badge from '@/components/Badge';
import { CardSkeleton } from '@/components/Skeleton';

/**
 * Admin pricing page.
 *
 * Shows every model with an effective (admin-custom OR official default)
 * price and lets the admin override individual rows. The catalog
 * ``provider / model_id`` is sourced from ``/user/models`` so the
 * admin sees the *same* list the chat picker will surface, instead
 * of an internal "everything we have ever cached" dump.
 *
 * Why two endpoints
 * -----------------
 *   * ``/admin/pricing`` — read/write the per-model override rows.
 *   * ``/user/models``   — list of *available* models (admin-visible
 *                          because the admin role passes the user-side
 *                          session guard). This is what end users
 *                          will see in the chat picker, so the admin
 *                          should be looking at the same set when
 *                          tuning prices.
 */
export default function Pricing() {
  const { t } = useTranslation();
  const [models, setModels] = useState([]);
  const [pricing, setPricing] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [provider, setProvider] = useState('all');
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [m, p] = await Promise.all([
        api.getModels().catch(() => []),
        api.getAdminPricing().catch(() => []),
      ]);
      setModels(Array.isArray(m) ? m : []);
      setPricing(Array.isArray(p) ? p : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const pricingMap = useMemo(() => {
    const out = {};
    for (const p of pricing) {
      const key = `${p.provider}/${p.model_id}`;
      out[key] = p;
    }
    return out;
  }, [pricing]);

  const providers = useMemo(() => {
    const set = new Set();
    for (const m of models) {
      if (m.provider) set.add(m.provider);
    }
    return Array.from(set).sort();
  }, [models]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (models || [])
      .filter((m) => {
        if (provider !== 'all' && m.provider !== provider) return false;
        if (!q) return true;
        return (
          (m.name || '').toLowerCase().includes(q) ||
          (m.display_name || '').toLowerCase().includes(q) ||
          (m.provider || '').toLowerCase().includes(q)
        );
      })
      .sort((a, b) => {
        if (a.provider !== b.provider) return a.provider.localeCompare(b.provider);
        return (a.display_name || a.name).localeCompare(b.display_name || b.name);
      });
  }, [models, search, provider]);

  const stats = useMemo(() => {
    const total = filtered.length;
    const customized = filtered.filter(
      (m) => pricingMap[`${m.provider}/${m.name}`]?.is_custom,
    ).length;
    const free = filtered.filter((m) => {
      const p = pricingMap[`${m.provider}/${m.name}`];
      const ip = p ? Number(p.input_price_per_1k || 0) : 0;
      const op = p ? Number(p.output_price_per_1k || 0) : 0;
      return ip === 0 && op === 0;
    }).length;
    const paid = total - free;
    return { total, customized, free, paid };
  }, [filtered, pricingMap]);

  const onReset = async (model) => {
    const name = model.display_name || model.name;
    if (!confirm(t('pricing.resetConfirm', { name }))) return;
    try {
      const key = `${model.provider}/${model.name}`;
      const p = pricingMap[key];
      if (p && p.id) {
        await api.deletePricing(p.id);
      } else {
        await api.resetOfficialPricing({ provider: model.provider, model_id: model.name });
      }
      toast.success(t('pricing.toast.resetSuccess'));
      load();
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-ink-50 dark:bg-ink-900">
      <TopBar
        title={t('pricing.title')}
        subtitle={t('pricing.subtitle')}
        action={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
              {t('common.refresh')}
            </Button>
            <Button size="sm" icon={Plus} onClick={() => setCreating(true)}>
              {t('pricing.addPricing')}
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-y-auto px-4 pb-12 pt-4 md:px-6">
        {/* Stats */}
        <div className="mb-4 grid grid-cols-2 gap-2.5 md:grid-cols-4">
          {[
            {
              label: t('pricing.stats.totalModels'),
              value: stats.total,
              icon: Tag,
              color: 'text-ink-900',
            },
            {
              label: t('pricing.stats.custom'),
              value: stats.customized,
              icon: Edit3,
              color: 'text-indigo-600',
            },
            {
              label: t('pricing.stats.free'),
              value: stats.free,
              icon: Check,
              color: 'text-emerald-700',
            },
            {
              label: t('pricing.stats.paid'),
              value: stats.paid,
              icon: Coins,
              color: 'text-amber-700',
            },
          ].map((s) => (
            <div
              key={s.label}
              className="rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-3 shadow-soft"
            >
              <div className="flex items-center gap-2 text-[11.5px] font-medium text-ink-500 dark:text-ink-400">
                <s.icon size={12} />
                {s.label}
              </div>
              <div className={`mt-1 text-[22px] font-semibold tabular-nums ${s.color}`}>
                {s.value}
              </div>
            </div>
          ))}
        </div>

        {/* Filters */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
            <button
              onClick={() => setProvider('all')}
              className={
                'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors ' +
                (provider === 'all' ? 'bg-ink-900 dark:bg-ink-100 text-white dark:text-ink-900' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800')
              }
            >
              {t('common.all')}
            </button>
            {providers.map((p) => (
              <button
                key={p}
                onClick={() => setProvider(p)}
                className={
                  'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors ' +
                  (provider === p ? 'bg-ink-900 dark:bg-ink-100 text-white dark:text-ink-900' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800')
                }
              >
                {p}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-1.5 rounded-md border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5">
            <Search size={12} className="text-ink-400 dark:text-ink-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('pricing.searchPlaceholder')}
              className="h-7 w-48 bg-transparent text-[12px] outline-none placeholder:text-ink-400"
            />
          </div>
        </div>

        {/* Table */}
        <div className="overflow-hidden rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 shadow-soft">
          {loading ? (
            <div className="p-4">
              <CardSkeleton rows={4} />
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={Tag}
              title={t('pricing.empty.title')}
              description={t('pricing.empty.description')}
            />
          ) : (
            <table className="w-full text-[12.5px]">
              <thead className="border-b border-ink-200 dark:border-ink-700 bg-ink-50/60 dark:bg-ink-900/60 text-[11px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">{t('common.model')}</th>
                  <th className="px-3 py-2 text-left font-medium">{t('common.platform')}</th>
                  <th className="px-3 py-2 text-right font-medium">
                    {t('pricing.table.inputPer1k')}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {t('pricing.table.outputPer1k')}
                  </th>
                  <th className="px-3 py-2 text-left font-medium">{t('common.status')}</th>
                  <th className="px-3 py-2 text-right font-medium">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((m) => {
                  const key = `${m.provider}/${m.name}`;
                  const p = pricingMap[key];
                  const ip = p ? Number(p.input_price_per_1k || 0) : 0;
                  const op = p ? Number(p.output_price_per_1k || 0) : 0;
                  const isCustom = !!p?.is_custom;
                  const isFree = ip === 0 && op === 0;
                  return (
                    <tr
                      key={key}
                      className="border-b border-ink-100 dark:border-ink-800 last:border-b-0 transition-colors hover:bg-ink-50/40 dark:hover:bg-ink-900/40"
                    >
                      <td className="px-3 py-2.5">
                        <div className="font-medium text-ink-900 dark:text-ink-100">{m.display_name || m.name}</div>
                        <div className="font-mono text-[10.5px] text-ink-400 dark:text-ink-500">{m.name}</div>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 text-[10.5px] font-medium text-ink-700 dark:text-ink-300">
                          {m.provider}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                        {ip.toFixed(2)}
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                        {op.toFixed(2)}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center gap-1.5">
                          {isFree ? (
                            <Badge variant="default">{t('pricing.badge.free')}</Badge>
                          ) : (
                            <Badge variant="warning" dot>
                              {t('pricing.badge.paid')}
                            </Badge>
                          )}
                          {isCustom ? (
                            <Badge variant="info">{t('pricing.badge.custom')}</Badge>
                          ) : (
                            <Badge variant="default">{t('pricing.badge.official')}</Badge>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-1.5">
                          <Button
                            size="sm"
                            variant="secondary"
                            icon={Edit3}
                            onClick={() => setEditing({ ...m, ...(p || {}) })}
                          >
                            {t('common.edit')}
                          </Button>
                          {isCustom ? (
                            <Button
                              size="sm"
                              variant="secondary"
                              icon={RotateCcw}
                              onClick={() => onReset(m)}
                            >
                              {t('pricing.reset')}
                            </Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {editing && (
        <PricingDialog
          row={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}
      {creating && (
        <PricingDialog
          row={null}
          knownProviders={providers}
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            load();
          }}
        />
      )}
    </div>
  );
}

function PricingDialog({ row, knownProviders = [], onClose, onSaved }) {
  const { t } = useTranslation();
  const isEdit = !!row;
  const [form, setForm] = useState(() => ({
    provider: row?.provider || knownProviders[0] || '',
    model_id: row?.model_id || row?.name || '',
    input_price_per_1k: row ? Number(row.input_price_per_1k || 0) : 0,
    output_price_per_1k: row ? Number(row.output_price_per_1k || 0) : 0,
    tier: row?.tier || 'standard',
    note: row?.note || '',
  }));
  const [saving, setSaving] = useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e?.preventDefault?.();
    if (!form.provider.trim() || !form.model_id.trim()) {
      toast.error(t('pricing.error.required'));
      return;
    }
    setSaving(true);
    try {
      const payload = {
        provider: form.provider.trim(),
        model_id: form.model_id.trim(),
        input_price_per_1k: Number(form.input_price_per_1k) || 0,
        output_price_per_1k: Number(form.output_price_per_1k) || 0,
        tier: form.tier || 'standard',
        note: form.note || null,
      };
      if (isEdit && row.id) {
        await api.updatePricing(row.id, payload);
      } else {
        await api.createPricing(payload);
      }
      toast.success(isEdit ? t('pricing.toast.updated') : t('pricing.toast.created'));
      onSaved?.();
    } catch (e) {
      toast.error(e.message || t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={
        isEdit
          ? t('pricing.dialog.editTitle', { name: row.display_name || row.name })
          : t('pricing.dialog.addTitle')
      }
      description={t('pricing.dialog.description')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={saving} icon={Check}>
            {t('common.save')}
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('common.platform')}
            </label>
            <input
              value={form.provider}
              onChange={set('provider')}
              disabled={isEdit}
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20 disabled:bg-ink-50 dark:disabled:bg-ink-800"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('pricing.form.modelId')}
            </label>
            <input
              value={form.model_id}
              onChange={set('model_id')}
              disabled={isEdit}
              placeholder="e.g. meta/llama-3.1-70b-instruct"
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20 disabled:bg-ink-50 dark:disabled:bg-ink-800"
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('pricing.form.inputPrice')}
            </label>
            <input
              type="number"
              step="0.01"
              min={0}
              value={form.input_price_per_1k}
              onChange={set('input_price_per_1k')}
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('pricing.form.outputPrice')}
            </label>
            <input
              type="number"
              step="0.01"
              min={0}
              value={form.output_price_per_1k}
              onChange={set('output_price_per_1k')}
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
            />
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
            {t('common.notes')}
          </label>
          <input
            value={form.note}
            onChange={set('note')}
            placeholder={t('pricing.form.notePlaceholder')}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
          />
        </div>
      </form>
    </Dialog>
  );
}
