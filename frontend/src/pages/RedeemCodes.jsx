import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Gift,
  Plus,
  Copy,
  Check,
  Search,
  CalendarClock,
  Sparkles,
  Coins,
  Trash2,
  Filter,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { copyToClipboard, formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import Badge from '@/components/Badge';

function StatusBadge({ code }) {
  const { t } = useTranslation();
  if (!code.is_active)
    return (
      <Badge variant="default" dot>
        {t('redeemCodes.status.disabled')}
      </Badge>
    );
  const used = code.used_count || 0;
  const max = code.max_uses || 1;
  if (used >= max)
    return (
      <Badge variant="warning" dot>
        {t('redeemCodes.status.usedUp')}
      </Badge>
    );
  if (code.expires_at && new Date(code.expires_at) < new Date()) {
    return (
      <Badge variant="danger" dot>
        {t('redeemCodes.status.expired')}
      </Badge>
    );
  }
  return (
    <Badge variant="success" dot>
      {t('redeemCodes.status.active')}
    </Badge>
  );
}

function CreateDialog({ onClose, onCreated }) {
  const { t } = useTranslation();
  const TYPE_OPTIONS = [
    {
      value: 'credit',
      label: t('redeemCodes.type.credit'),
      desc: t('redeemCodes.type.creditDesc'),
      icon: Coins,
    },
    {
      value: 'plan',
      label: t('redeemCodes.type.plan'),
      desc: t('redeemCodes.type.planDesc'),
      icon: Sparkles,
    },
  ];
  const [form, setForm] = useState({
    count: 1,
    type: 'credit',
    value: 100,
    prefix: 'GIFT',
    max_uses: 1,
    expires_at: '',
    plan_id: null,
  });
  const [saving, setSaving] = useState(false);
  const [issued, setIssued] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload = {
        count: Math.max(1, Math.min(500, parseInt(form.count) || 1)),
        type: form.type,
        value: parseFloat(form.value) || 0,
        prefix: (form.prefix || '').trim(),
        max_uses: parseInt(form.max_uses) || 1,
        expires_at: form.expires_at || null,
        plan_id: form.type === 'plan' ? (form.plan_id ? parseInt(form.plan_id) : null) : null,
      };
      const result = await api.createRedeemCodes(payload);
      setIssued(result);
      onCreated?.();
      toast.success(
        t('redeemCodes.toast.generated', { count: Array.isArray(result) ? result.length : 1 }),
      );
    } catch (err) {
      toast.error(err.message || t('redeemCodes.toast.generateFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={issued ? t('redeemCodes.create.titleGenerated') : t('redeemCodes.create.title')}
      description={
        issued ? t('redeemCodes.create.descGenerated') : t('redeemCodes.create.description')
      }
      size="md"
      footer={
        issued ? (
          <Button onClick={onClose}>{t('common.done')}</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button onClick={submit} loading={saving} icon={Plus}>
              {t('redeemCodes.create.generate')}
            </Button>
          </>
        )
      }
    >
      {issued ? (
        <div className="space-y-2">
          {Array.isArray(issued) ? (
            issued.map((c) => (
              <div
                key={c.id}
                className="flex items-center gap-2 rounded-lg border border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-900/20 p-2.5"
              >
                <code className="flex-1 break-all font-mono text-[12.5px] text-emerald-900 dark:text-emerald-400">
                  {c.code || c.code_value || `#${c.id}`}
                </code>
                <Button
                  size="sm"
                  variant="secondary"
                  icon={Copy}
                  onClick={async () => {
                    await copyToClipboard(c.code || c.code_value || `#${c.id}`);
                    toast.success(t('common.copied'));
                  }}
                >
                  {t('common.copy')}
                </Button>
              </div>
            ))
          ) : (
            <div className="rounded-lg border border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-900/20 p-3 text-[13px] text-emerald-700 dark:text-emerald-400">
              {t('redeemCodes.create.generated')}
            </div>
          )}
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('redeemCodes.form.count')}
              </label>
              <input
                type="number"
                min={1}
                max={500}
                value={form.count}
                onChange={set('count')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('redeemCodes.form.prefix')}
              </label>
              <input
                value={form.prefix}
                onChange={set('prefix')}
                placeholder={t('redeemCodes.form.prefixPlaceholder')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('common.type')}
            </label>
            <div className="grid grid-cols-2 gap-2">
              {TYPE_OPTIONS.map((opt) => {
                const Icon = opt.icon;
                const selected = form.type === opt.value;
                return (
                  <button
                    type="button"
                    key={opt.value}
                    onClick={() => setForm((f) => ({ ...f, type: opt.value }))}
                    className={
                      'flex items-start gap-2.5 rounded-lg border p-2.5 text-left transition-all ' +
                      (selected
                        ? 'border-ink-900 dark:border-ink-100 bg-ink-50 dark:bg-ink-800 ring-1 ring-ink-900/20 dark:ring-ink-100/20'
                        : 'border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 hover:border-ink-300 dark:hover:border-ink-600')
                    }
                  >
                    <Icon size={14} className={selected ? 'text-ink-900 dark:text-ink-100' : 'text-ink-500 dark:text-ink-400'} />
                    <div>
                      <div className="text-[12.5px] font-medium text-ink-900 dark:text-ink-100">{opt.label}</div>
                      <div className="text-[11px] text-ink-500 dark:text-ink-400">{opt.desc}</div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {form.type === 'credit'
                  ? t('redeemCodes.form.creditValue')
                  : t('redeemCodes.form.planIdOptional')}
              </label>
              <input
                type="number"
                value={form.value}
                onChange={set('value')}
                placeholder={
                  form.type === 'credit'
                    ? t('redeemCodes.form.creditPlaceholder')
                    : t('redeemCodes.form.planPlaceholder')
                }
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('redeemCodes.form.maxUses')}
              </label>
              <input
                type="number"
                min={1}
                value={form.max_uses}
                onChange={set('max_uses')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('redeemCodes.form.expiresOptional')}
            </label>
            <input
              type="datetime-local"
              value={form.expires_at}
              onChange={set('expires_at')}
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
            />
          </div>
        </form>
      )}
    </Dialog>
  );
}

export default function RedeemCodes() {
  const { t } = useTranslation();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [tab, setTab] = useState('active');
  const [showCreate, setShowCreate] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.listRedeemCodes();
      setItems(Array.isArray(list) ? list : []);
    } catch (e) {
      toast.error(e.message || t('redeemCodes.toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (items || [])
      .filter((c) => {
        if (tab === 'active') {
          if (!c.is_active) return false;
          if ((c.used_count || 0) >= (c.max_uses || 1)) return false;
          if (c.expires_at && new Date(c.expires_at) < new Date()) return false;
        } else if (tab === 'used') {
          if ((c.used_count || 0) < (c.max_uses || 1)) return false;
        } else if (tab === 'expired') {
          if (!(c.expires_at && new Date(c.expires_at) < new Date())) return false;
        } else if (tab === 'disabled') {
          if (c.is_active) return false;
        }
        if (!q) return true;
        return (
          (c.code || c.code_value || '').toLowerCase().includes(q) ||
          (c.prefix || '').toLowerCase().includes(q)
        );
      })
      .sort((a, b) => (b.id || 0) - (a.id || 0));
  }, [items, search, tab]);

  const stats = useMemo(() => {
    const total = items.length;
    const used = items.filter((c) => (c.used_count || 0) >= (c.max_uses || 1)).length;
    const active = items.filter((c) => {
      if (!c.is_active) return false;
      if ((c.used_count || 0) >= (c.max_uses || 1)) return false;
      if (c.expires_at && new Date(c.expires_at) < new Date()) return false;
      return true;
    }).length;
    const expired = items.filter((c) => c.expires_at && new Date(c.expires_at) < new Date()).length;
    return { total, used, active, expired };
  }, [items]);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-ink-50 dark:bg-ink-900">
      <TopBar
        title={t('redeemCodes.pageTitle')}
        subtitle={t('redeemCodes.subtitle')}
        action={
          <Button icon={Plus} onClick={() => setShowCreate(true)}>
            {t('redeemCodes.create.title')}
          </Button>
        }
      />

      <div className="flex-1 overflow-y-auto px-4 pb-12 pt-4 md:px-6">
        {/* Stats */}
        <div className="mb-4 grid grid-cols-2 gap-2.5 md:grid-cols-4">
          {[
            { label: t('redeemCodes.stats.total'), value: stats.total, color: 'text-ink-900' },
            {
              label: t('redeemCodes.stats.active'),
              value: stats.active,
              color: 'text-emerald-700',
            },
            { label: t('redeemCodes.stats.used'), value: stats.used, color: 'text-amber-700' },
            { label: t('redeemCodes.stats.expired'), value: stats.expired, color: 'text-rose-700' },
          ].map((s) => (
            <div
              key={s.label}
              className="rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-3 shadow-soft"
            >
              <div className="text-[11.5px] font-medium text-ink-500">{s.label}</div>
              <div className={'mt-1 text-[22px] font-semibold tabular-nums ' + s.color}>
                {s.value}
              </div>
            </div>
          ))}
        </div>

        {/* Tabs + search */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1 rounded-lg border border-ink-200 bg-white p-0.5">
            {[
              { id: 'active', label: t('redeemCodes.tab.active') },
              { id: 'used', label: t('redeemCodes.tab.used') },
              { id: 'expired', label: t('redeemCodes.tab.expired') },
              { id: 'disabled', label: t('redeemCodes.tab.disabled') },
              { id: 'all', label: t('common.all') },
            ].map((tabItem) => (
              <button
                key={tabItem.id}
                onClick={() => setTab(tabItem.id)}
                className={
                  'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors ' +
                  (tab === tabItem.id ? 'bg-ink-900 dark:bg-ink-100 text-white dark:text-ink-900' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800')
                }
              >
                {tabItem.label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-1.5 rounded-md border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5">
            <Search size={12} className="text-ink-400 dark:text-ink-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('redeemCodes.searchPlaceholder')}
              className="h-7 w-48 bg-transparent text-[12px] outline-none placeholder:text-ink-400"
            />
          </div>
        </div>

        {/* Table */}
        <div className="overflow-hidden rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 shadow-soft">
          <table className="w-full table-fixed text-[12.5px]">
            <colgroup>
              <col className="w-[40%]" />
              <col className="w-[14%]" />
              <col className="w-[14%]" />
              <col className="w-[14%]" />
              <col className="w-[18%]" />
            </colgroup>
            <thead className="border-b border-ink-200 dark:border-ink-700 bg-ink-50/60 dark:bg-ink-900/60 text-[11px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
              <tr>
                <th className="px-3 py-2 text-left">{t('redeemCodes.table.code')}</th>
                <th className="px-3 py-2 text-left">{t('common.type')}</th>
                <th className="px-3 py-2 text-left">{t('redeemCodes.table.value')}</th>
                <th className="px-3 py-2 text-left">{t('redeemCodes.table.usage')}</th>
                <th className="px-3 py-2 text-left">{t('redeemCodes.table.statusExpiry')}</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={5} className="px-3 py-8 text-center text-ink-400">
                    {t('common.loading')}
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-3 py-2">
                    <EmptyState
                      icon={Gift}
                      title={t('redeemCodes.empty.title')}
                      description={t('redeemCodes.empty.description')}
                    />
                  </td>
                </tr>
              ) : (
                filtered.map((c) => (
                  <tr
                    key={c.id}
                    className="border-b border-ink-100 dark:border-ink-800 last:border-b-0 hover:bg-ink-50/40 dark:hover:bg-ink-900/40"
                  >
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <code className="break-all rounded bg-ink-50 dark:bg-ink-900 px-1.5 py-0.5 font-mono text-[11.5px] text-ink-800 dark:text-ink-200">
                          {c.code || c.code_value || `#${c.id}`}
                        </code>
                        <button
                          onClick={async () => {
                            await copyToClipboard(c.code || c.code_value || `#${c.id}`);
                            toast.success(t('common.copied'));
                          }}
                          className="rounded p-1 text-ink-400 dark:text-ink-500 hover:bg-ink-100 dark:hover:bg-ink-800 hover:text-ink-700 dark:hover:text-ink-300"
                          title={t('common.copy')}
                        >
                          <Copy size={11} />
                        </button>
                        <button
                          onClick={async () => {
                            if (!confirm(t('redeemCodes.revoke.confirm'))) return;
                            try {
                              await api.revokeRedeemCode(c.id);
                              toast.success(t('redeemCodes.toast.disabled'));
                              load();
                            } catch (e) {
                              toast.error(e.message || t('common.operationFailed'));
                            }
                          }}
                          className="rounded p-1 text-ink-300 dark:text-ink-600 hover:bg-rose-50 dark:hover:bg-rose-900/20 hover:text-rose-600 dark:hover:text-rose-400"
                          title={t('redeemCodes.revoke.disable')}
                        >
                          <Trash2 size={11} />
                        </button>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-ink-700 dark:text-ink-300">
                      {c.type === 'plan'
                        ? t('redeemCodes.type.planLabel')
                        : t('redeemCodes.type.creditLabel')}
                    </td>
                    <td className="px-3 py-2.5 font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {c.type === 'plan' ? `#${c.plan_id || '-'}` : `${c.value || 0} cr`}
                    </td>
                    <td className="px-3 py-2.5 font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {c.used_count || 0} / {c.max_uses || 1}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <StatusBadge code={c} />
                        <span className="text-[11px] text-ink-400">
                          {c.expires_at ? formatDate(c.expires_at) : t('redeemCodes.permanent')}
                        </span>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showCreate && <CreateDialog onClose={() => setShowCreate(false)} onCreated={load} />}
    </div>
  );
}
