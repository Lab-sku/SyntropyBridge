import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Gift, Plus, Copy, Search, AlertTriangle, Ban } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { copyToClipboard, formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import Badge from '@/components/Badge';

// ---------------------------------------------------------------------------
// Status helpers
// ---------------------------------------------------------------------------

function getPromoStatus(code) {
  if (!code.is_active) return 'revoked';
  if (code.valid_until && new Date(code.valid_until) < new Date()) return 'expired';
  if (code.max_uses > 0 && (code.used_count || 0) >= code.max_uses) return 'exhausted';
  return 'active';
}

const STATUS_CONFIG = {
  active: { variant: 'success', labelKey: 'promoCodes.status.active' },
  exhausted: { variant: 'warning', labelKey: 'promoCodes.status.exhausted' },
  expired: { variant: 'danger', labelKey: 'promoCodes.status.expired' },
  revoked: { variant: 'default', labelKey: 'promoCodes.status.revoked' },
};

function StatusBadge({ code }) {
  const { t } = useTranslation();
  const status = getPromoStatus(code);
  const cfg = STATUS_CONFIG[status];
  return (
    <Badge variant={cfg.variant} dot>
      {t(cfg.labelKey)}
    </Badge>
  );
}

function maskCode(code) {
  if (!code) return '';
  const parts = code.split('-');
  if (parts.length >= 4) {
    return `${parts[0]}-****-****-${parts[parts.length - 1]}`;
  }
  if (code.length > 8) {
    return code.slice(0, 4) + '-****-****-' + code.slice(-4);
  }
  return '****';
}

// ---------------------------------------------------------------------------
// Create dialog
// ---------------------------------------------------------------------------

function CreateDialog({ onClose, onCreated }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    type: 'bonus_credits',
    value: 100,
    bonus_credits: 100,
    max_uses: 0,
    per_user_limit: 1,
    valid_until: '',
  });
  const [saving, setSaving] = useState(false);
  const [issued, setIssued] = useState(null);
  const [copied, setCopied] = useState(false);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const payload = {
        type: form.type,
        value: parseFloat(form.value) || 0,
        bonus_credits: parseFloat(form.bonus_credits) || 0,
        max_uses: parseInt(form.max_uses) || 0,
        per_user_limit: parseInt(form.per_user_limit) || 1,
        valid_until: form.valid_until || null,
      };
      const result = await api.createPromoCode(payload);
      setIssued(result);
      onCreated?.();
    } catch (err) {
      toast.error(err.message || t('promoCodes.toast.createFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={issued ? t('promoCodes.create.titleCreated') : t('promoCodes.create.title')}
      description={issued ? t('promoCodes.create.descCreated') : t('promoCodes.create.description')}
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
              {t('promoCodes.create.generate')}
            </Button>
          </>
        )
      }
    >
      {issued ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <p className="text-[12.5px] leading-relaxed text-amber-800 dark:text-amber-400">
              {t('promoCodes.create.warningOnce')}
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-900/20 p-3">
            <code className="flex-1 break-all font-mono text-[14px] font-semibold tracking-wider text-emerald-900 dark:text-emerald-400">
              {issued.code}
            </code>
            <Button
              size="sm"
              variant={copied ? 'success' : 'secondary'}
              icon={Copy}
              onClick={async () => {
                await copyToClipboard(issued.code);
                setCopied(true);
                toast.success(t('common.copied'));
                setTimeout(() => setCopied(false), 2000);
              }}
            >
              {copied ? t('common.copied') : t('common.copy')}
            </Button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('promoCodes.form.type')}
              </label>
              <select
                value={form.type}
                onChange={set('type')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              >
                <option value="bonus_credits">{t('promoCodes.type.bonusCredits')}</option>
                <option value="discount_percent">{t('promoCodes.type.discountPercent')}</option>
                <option value="discount_fixed">{t('promoCodes.type.discountFixed')}</option>
              </select>
            </div>
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('promoCodes.form.value')}
              </label>
              <input
                type="number"
                min={0}
                step="0.01"
                value={form.value}
                onChange={set('value')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('promoCodes.form.maxUses')}
              </label>
              <input
                type="number"
                min={0}
                value={form.max_uses}
                onChange={set('max_uses')}
                placeholder="0"
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
              <p className="mt-1 text-[11px] text-ink-400">{t('promoCodes.form.maxUsesHint')}</p>
            </div>
            <div>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {t('promoCodes.form.perUserLimit')}
              </label>
              <input
                type="number"
                min={1}
                value={form.per_user_limit}
                onChange={set('per_user_limit')}
                className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
              {t('promoCodes.form.expiresAt')}
            </label>
            <input
              type="datetime-local"
              value={form.valid_until}
              onChange={set('valid_until')}
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20"
            />
            <p className="mt-1 text-[11px] text-ink-400">{t('promoCodes.form.expiresHint')}</p>
          </div>
        </form>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Revoke confirmation dialog
// ---------------------------------------------------------------------------

function RevokeDialog({ code, onClose, onRevoked }) {
  const { t } = useTranslation();
  const [revoking, setRevoking] = useState(false);

  const submit = async () => {
    setRevoking(true);
    try {
      await api.revokePromoCode(code.id);
      toast.success(t('promoCodes.toast.revoked'));
      onRevoked?.();
      onClose();
    } catch (err) {
      toast.error(err.message || t('common.operationFailed'));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('promoCodes.revoke.title')}
      description={t('promoCodes.revoke.description')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" onClick={submit} loading={revoking} icon={Ban}>
            {t('promoCodes.revoke.confirm')}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-ink-600 dark:text-ink-400">
        {t('promoCodes.revoke.body', { code: maskCode(code.code) })}
      </p>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

const TABS = ['all', 'active', 'exhausted', 'expired', 'revoked'];

export default function AdminPromoCodes() {
  const { t } = useTranslation();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [tab, setTab] = useState('all');
  const [showCreate, setShowCreate] = useState(false);
  const [revoking, setRevoking] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const filters = {};
      if (tab !== 'all') filters.status = tab;
      if (search.trim()) filters.search = search.trim();
      const list = await api.listPromoCodes(filters);
      setItems(Array.isArray(list) ? list : []);
    } catch (e) {
      toast.error(e.message || t('promoCodes.toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [tab, search]);

  const stats = useMemo(() => {
    const all = items;
    const active = all.filter((c) => getPromoStatus(c) === 'active').length;
    const exhausted = all.filter((c) => getPromoStatus(c) === 'exhausted').length;
    const expired = all.filter((c) => getPromoStatus(c) === 'expired').length;
    const revoked = all.filter((c) => getPromoStatus(c) === 'revoked').length;
    return { all: all.length, active, exhausted, expired, revoked };
  }, [items]);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-ink-50 dark:bg-ink-900">
      <TopBar
        title={t('promoCodes.title')}
        subtitle={t('promoCodes.subtitle')}
        action={
          <Button icon={Plus} onClick={() => setShowCreate(true)}>
            {t('promoCodes.create.title')}
          </Button>
        }
      />

      <div className="flex-1 overflow-y-auto px-4 pb-12 pt-4 md:px-6">
        {/* Stats */}
        <div className="mb-4 grid grid-cols-2 gap-2.5 md:grid-cols-5">
          {[
            { label: t('common.total'), value: stats.all, color: 'text-ink-900' },
            {
              label: t('promoCodes.status.active'),
              value: stats.active,
              color: 'text-emerald-700',
            },
            {
              label: t('promoCodes.status.exhausted'),
              value: stats.exhausted,
              color: 'text-amber-700',
            },
            {
              label: t('promoCodes.status.expired'),
              value: stats.expired,
              color: 'text-rose-700',
            },
            {
              label: t('promoCodes.status.revoked'),
              value: stats.revoked,
              color: 'text-ink-500',
            },
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
          <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
            {TABS.map((tabId) => (
              <button
                key={tabId}
                onClick={() => setTab(tabId)}
                className={
                  'rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors ' +
                  (tab === tabId ? 'bg-ink-900 dark:bg-ink-100 text-white dark:text-ink-900' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800')
                }
              >
                {tabId === 'all' ? t('common.all') : t(`promoCodes.status.${tabId}`)}
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-1.5 rounded-md border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5">
            <Search size={12} className="text-ink-400 dark:text-ink-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('promoCodes.searchPlaceholder')}
              className="h-7 w-48 bg-transparent text-[12px] outline-none placeholder:text-ink-400 dark:placeholder:text-ink-500"
            />
          </div>
        </div>

        {/* Table */}
        <div className="overflow-x-auto rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 shadow-soft">
          <table className="w-full text-[12.5px]">
            <thead className="border-b border-ink-200 dark:border-ink-700 bg-ink-50/60 dark:bg-ink-900/60 text-[11px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
              <tr>
                <th className="px-3 py-2 text-left">{t('promoCodes.col.code')}</th>
                <th className="px-3 py-2 text-right">{t('promoCodes.col.value')}</th>
                <th className="px-3 py-2 text-right">{t('promoCodes.col.usage')}</th>
                <th className="px-3 py-2 text-left">{t('promoCodes.col.expiresAt')}</th>
                <th className="px-3 py-2 text-center">{t('common.status')}</th>
                <th className="px-3 py-2 text-left">{t('common.createdAt')}</th>
                <th className="px-3 py-2 text-center">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={7} className="px-3 py-8 text-center text-ink-400">
                    {t('common.loading')}
                  </td>
                </tr>
              ) : items.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-3 py-2">
                    <EmptyState
                      icon={Gift}
                      title={t('promoCodes.empty.title')}
                      description={t('promoCodes.empty.description')}
                    />
                  </td>
                </tr>
              ) : (
                items.map((c) => (
                  <tr
                    key={c.id}
                    className="border-b border-ink-100 dark:border-ink-800 last:border-b-0 hover:bg-ink-50/40 dark:hover:bg-ink-900/40"
                  >
                    <td className="px-3 py-2.5">
                      <code className="rounded bg-ink-50 dark:bg-ink-900 px-1.5 py-0.5 font-mono text-[11.5px] text-ink-800 dark:text-ink-200">
                        {maskCode(c.code)}
                      </code>
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {c.value || 0}
                      {c.type === 'discount_percent' ? '%' : ' cr'}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {c.used_count || 0} / {c.max_uses || t('promoCodes.unlimited')}
                    </td>
                    <td className="px-3 py-2.5 text-[11.5px] text-ink-500 dark:text-ink-400">
                      {c.valid_until ? formatDate(c.valid_until) : t('promoCodes.never')}
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <StatusBadge code={c} />
                    </td>
                    <td className="px-3 py-2.5 text-[11.5px] text-ink-500 dark:text-ink-400">
                      {formatDate(c.created_at)}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex items-center justify-center gap-1">
                        {c.is_active ? (
                          <button
                            onClick={() => setRevoking(c)}
                            className="rounded p-1.5 text-ink-300 dark:text-ink-600 hover:bg-rose-50 dark:hover:bg-rose-900/20 hover:text-rose-600 dark:hover:text-rose-400"
                            title={t('promoCodes.revoke.confirm')}
                          >
                            <Ban size={13} />
                          </button>
                        ) : (
                          <span className="text-[11px] text-ink-400">-</span>
                        )}
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
      {revoking && (
        <RevokeDialog code={revoking} onClose={() => setRevoking(null)} onRevoked={load} />
      )}
    </div>
  );
}
