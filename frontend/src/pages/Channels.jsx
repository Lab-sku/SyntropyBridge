import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Plus,
  Search,
  Trash2,
  Pencil,
  Play,
  RefreshCw,
  Power,
  Eye,
  EyeOff,
  MoreHorizontal,
  Server,
  AlertTriangle,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { timeAgo, formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { TableRowSkeleton } from '@/components/Skeleton';

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

// L14: 兜底 provider 列表。仅在 /providers API 失败时使用，保证 admin
// 仍能创建 channel。正常情况下主组件会用 API 返回的动态列表覆盖它。
const FALLBACK_PROVIDERS = [
  'openai',
  'anthropic',
  'google',
  'minimax',
  'deepseek',
  'moonshot',
  'zhipu',
  'aliyun',
  'doubao',
  'nvidia',
  'openrouter',
  'siliconflow',
  'mimo',
];

/**
 * Derive a status bucket for a channel row.
 *
 *   - inactive: is_active === false
 *   - failed: has last_error AND no successful health check
 *   - cooldown: cooldown_until is in the future
 *   - active: everything else (healthy)
 */
function channelStatus(ch) {
  if (!ch.is_active) return 'inactive';
  if (ch.cooldown_until) {
    const until = new Date(ch.cooldown_until);
    if (until.getTime() > Date.now()) return 'cooldown';
  }
  if (ch.last_error && !ch.last_health_at) return 'failed';
  return 'active';
}

const STATUS_BADGE = {
  active: 'success',
  cooldown: 'warning',
  inactive: 'default',
  failed: 'danger',
};

const STATUS_DOT = {
  active: 'bg-emerald-500',
  cooldown: 'bg-amber-400',
  inactive: 'bg-ink-300 dark:bg-ink-600',
  failed: 'bg-rose-500',
};

/* ------------------------------------------------------------------ */
/*  Channel form dialog (shared between create & edit)                */
/* ------------------------------------------------------------------ */

function ChannelFormDialog({ onClose, onSaved, channel, isEdit, providers }) {
  const { t } = useTranslation();
  // L14: provider 列表由父组件通过 API 动态加载；为空时用兜底列表。
  const providerList = providers && providers.length > 0 ? providers : FALLBACK_PROVIDERS;
  const [form, setForm] = useState({
    provider: channel?.provider || providerList[0],
    name: channel?.name || '',
    base_url: channel?.base_url || '',
    api_key: '',
    weight: channel?.weight ?? 100,
    is_active: channel?.is_active ?? true,
  });
  const [saving, setSaving] = useState(false);
  const [showKey, setShowKey] = useState(false);

  const set = (k) => (e) => {
    const val = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setForm((f) => ({ ...f, [k]: val }));
  };

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const body = {
        provider: form.provider,
        name: form.name,
        base_url: form.base_url,
        weight: parseInt(form.weight) || 100,
        is_active: Boolean(form.is_active),
      };
      if (form.api_key) body.api_key = form.api_key;

      if (isEdit) {
        await api.updateChannel(channel.id, body);
        toast.success(t('channels.toast.updated'));
      } else {
        if (!form.api_key) {
          toast.error(t('channels.form.apiKeyRequired'));
          setSaving(false);
          return;
        }
        body.api_key = form.api_key;
        const res = await api.createChannel(body);
        toast.success(t('channels.toast.created'));
        // Auto-test after creation
        if (res && res.id) {
          try {
            await api.testChannel(res.id);
            toast.success(t('channels.toast.testOk'));
          } catch {
            toast.warning(t('channels.toast.testFailed'));
          }
        }
      }
      onSaved?.();
      onClose();
    } catch (e) {
      toast.error(e.message || t('channels.toast.createFailed'));
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <Dialog
      open
      onClose={onClose}
      title={isEdit ? t('channels.edit.title') : t('channels.create.title')}
      description={isEdit ? t('channels.edit.description') : t('channels.create.description')}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            loading={saving}
            onClick={submit}
            disabled={!form.name || !form.base_url || !form.provider}
          >
            {isEdit ? t('common.save') : t('channels.create.submit')}
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        {/* Provider */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-600 dark:text-ink-400">
            {t('channels.form.provider')}
          </label>
          <select value={form.provider} onChange={set('provider')} className={inputCls}>
            {providerList.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
            {/* Allow custom:* providers not in the dynamic list */}
            {!providerList.includes(form.provider) && (
              <option value={form.provider}>{form.provider}</option>
            )}
          </select>
        </div>

        {/* Name */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-600 dark:text-ink-400">
            {t('channels.form.name')}
          </label>
          <input
            type="text"
            value={form.name}
            onChange={set('name')}
            placeholder={t('channels.form.namePlaceholder')}
            className={inputCls}
            required
          />
        </div>

        {/* Base URL */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-600 dark:text-ink-400">
            {t('channels.form.baseUrl')}
          </label>
          <input
            type="url"
            value={form.base_url}
            onChange={set('base_url')}
            placeholder="https://api.example.com"
            className={inputCls}
            required
          />
          <p className="mt-1 text-[11px] text-ink-400 dark:text-ink-500">{t('channels.form.baseUrlHint')}</p>
        </div>

        {/* API Key */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-600 dark:text-ink-400">
            {t('channels.form.apiKey')}
          </label>
          <div className="relative">
            <input
              type={showKey ? 'text' : 'password'}
              value={form.api_key}
              onChange={set('api_key')}
              placeholder={isEdit ? t('channels.form.apiKeyChange') : ''}
              className={`${inputCls} pr-9`}
            />
            <button
              type="button"
              onClick={() => setShowKey((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 dark:text-ink-500 hover:text-ink-600 dark:hover:text-ink-400"
            >
              {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        {/* Weight */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-600 dark:text-ink-400">
            {t('channels.form.weight')}
          </label>
          <input
            type="number"
            min={0}
            max={10000}
            value={form.weight}
            onChange={set('weight')}
            className={inputCls}
          />
          <p className="mt-1 text-[11px] text-ink-400 dark:text-ink-500">{t('channels.form.weightHint')}</p>
        </div>

        {/* Is Active */}
        <label className="flex items-center gap-2 text-[13px] text-ink-700 dark:text-ink-300">
          <input
            type="checkbox"
            checked={form.is_active}
            onChange={set('is_active')}
            className="h-4 w-4 rounded border-ink-300 text-brand-600 focus:ring-brand-400"
          />
          {t('channels.form.isActive')}
        </label>
      </form>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/*  Delete confirmation dialog                                        */
/* ------------------------------------------------------------------ */

function DeleteConfirmDialog({ channel, onClose, onDeleted }) {
  const { t } = useTranslation();
  const [deleting, setDeleting] = useState(false);

  const confirm = async () => {
    setDeleting(true);
    try {
      await api.deleteChannel(channel.id);
      toast.success(t('channels.toast.deleted'));
      onDeleted?.();
      onClose();
    } catch (e) {
      toast.error(e.message || t('channels.toast.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('channels.delete.title')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" loading={deleting} onClick={confirm} icon={Trash2}>
            {t('common.delete')}
          </Button>
        </>
      }
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-rose-500" />
        <p className="text-[13px] text-ink-600 dark:text-ink-400">
          {t('channels.delete.confirm', { name: channel.name })}
        </p>
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/*  Inline weight editor                                               */
/* ------------------------------------------------------------------ */

function InlineWeight({ channel, onUpdated }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(channel.weight);

  if (!editing) {
    return (
      <button
        onClick={() => {
          setVal(channel.weight);
          setEditing(true);
        }}
        className="rounded-md px-1.5 py-0.5 text-[13px] font-mono text-ink-700 dark:text-ink-300 hover:bg-ink-100 dark:hover:bg-ink-800"
        title={t('channels.clickToEditWeight')}
      >
        {channel.weight}
      </button>
    );
  }

  const save = async () => {
    try {
      await api.updateChannel(channel.id, { weight: parseInt(val) || 0 });
      onUpdated?.();
    } catch {
      // silently ignore
    }
    setEditing(false);
  };

  return (
    <input
      type="number"
      autoFocus
      value={val}
      min={0}
      max={10000}
      onChange={(e) => setVal(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => {
        if (e.key === 'Enter') save();
        if (e.key === 'Escape') setEditing(false);
      }}
      className="w-16 rounded-md border border-ink-300 px-1.5 py-0.5 text-[13px] font-mono outline-none focus:border-brand-400"
    />
  );
}

/* ------------------------------------------------------------------ */
/*  Row actions dropdown                                              */
/* ------------------------------------------------------------------ */

function RowActions({ channel, onTest, onEdit, onToggle, onResetCooldown, onDelete }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  const close = () => setOpen(false);

  const items = [
    { label: t('channels.actions.test'), icon: Play, onClick: onTest },
    { label: t('channels.actions.edit'), icon: Pencil, onClick: onEdit },
    {
      label: t('channels.actions.toggleActive'),
      icon: Power,
      onClick: onToggle,
    },
    {
      label: t('channels.actions.resetCooldown'),
      icon: RefreshCw,
      onClick: onResetCooldown,
      disabled: !channel.cooldown_until,
    },
    { label: t('channels.actions.delete'), icon: Trash2, onClick: onDelete, danger: true },
  ];

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="rounded-md p-1.5 text-ink-400 dark:text-ink-500 transition-colors hover:bg-ink-100 dark:hover:bg-ink-800 hover:text-ink-700 dark:hover:text-ink-300"
      >
        <MoreHorizontal size={15} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={close} />
          <div className="absolute right-0 z-50 mt-1 w-44 overflow-hidden rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 py-1 shadow-lg">
            {items.map((item) => (
              <button
                key={item.label}
                disabled={item.disabled}
                onClick={() => {
                  close();
                  item.onClick?.();
                }}
                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px] transition-colors ${
                  item.danger ? 'text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-900/20' : 'text-ink-700 dark:text-ink-300 hover:bg-ink-50 dark:hover:bg-ink-800'
                } disabled:cursor-not-allowed disabled:opacity-40`}
              >
                <item.icon size={13} />
                {item.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main page component                                               */
/* ------------------------------------------------------------------ */

export default function Channels() {
  const { t } = useTranslation();
  const [channels, setChannels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [providerFilter, setProviderFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [search, setSearch] = useState('');
  // L14: 动态加载 provider 列表，不再依赖硬编码 PROVIDERS。
  const [providers, setProviders] = useState([]);

  // Dialog state
  const [showCreate, setShowCreate] = useState(false);
  const [editChannel, setEditChannel] = useState(null);
  const [deleteChannel, setDeleteChannel] = useState(null);

  /* ---- data loading ---- */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listChannels(
        providerFilter ? { provider: providerFilter } : undefined,
      );
      setChannels(Array.isArray(data) ? data : []);
    } catch {
      toast.error(t('channels.toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [providerFilter, t]);

  // L14: 一次性加载 provider 列表用于下拉框。失败时静默回退到
  // FALLBACK_PROVIDERS（ChannelFormDialog 内部处理）。
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.getProviders();
        if (cancelled) return;
        if (Array.isArray(list) && list.length > 0) {
          setProviders(list.map((p) => p.name).filter(Boolean));
        }
      } catch {
        // 静默失败 — ChannelFormDialog 会用 FALLBACK_PROVIDERS 兜底
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /* ---- actions ---- */
  const onTest = async (ch) => {
    try {
      await api.testChannel(ch.id);
      toast.success(t('channels.toast.testOk'));
      load();
    } catch (e) {
      toast.error(e.message || t('channels.toast.testFailed'));
      load();
    }
  };

  const onToggle = async (ch) => {
    try {
      await api.toggleChannelActive(ch.id);
      toast.success(t('channels.toast.toggled'));
      load();
    } catch {
      toast.error(t('channels.toast.toggleFailed'));
    }
  };

  const onResetCooldown = async (ch) => {
    try {
      await api.resetChannelCooldown(ch.id);
      toast.success(t('channels.toast.cooldownReset'));
      load();
    } catch {
      toast.error(t('channels.toast.cooldownResetFailed'));
    }
  };

  /* ---- filtering ---- */
  const filtered = useMemo(() => {
    let list = channels;
    if (statusFilter) {
      list = list.filter((ch) => channelStatus(ch) === statusFilter);
    }
    if (search) {
      const q = search.toLowerCase();
      list = list.filter(
        (ch) =>
          ch.name.toLowerCase().includes(q) ||
          ch.base_url.toLowerCase().includes(q) ||
          ch.provider.toLowerCase().includes(q),
      );
    }
    return list;
  }, [channels, statusFilter, search]);

  /* ---- render ---- */
  const subtitle = `${filtered.length} / ${channels.length}`;

  return (
    <div className="flex min-h-screen flex-col">
      <TopBar
        title={t('channels.title')}
        subtitle={subtitle}
        action={
          <Button variant="primary" icon={Plus} onClick={() => setShowCreate(true)}>
            {t('channels.addChannel')}
          </Button>
        }
      />

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2.5 border-b border-ink-100 dark:border-ink-800 bg-white/60 dark:bg-ink-900/60 px-4 py-2.5 md:px-6">
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400 dark:text-ink-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('common.search')}
            className="h-8 w-52 rounded-lg border border-ink-200 bg-white pl-8 pr-3 text-[12.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20"
          />
        </div>
        <select
          value={providerFilter}
          onChange={(e) => setProviderFilter(e.target.value)}
          className="h-8 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 text-[12.5px] outline-none focus:border-brand-400"
        >
          <option value="">{t('channels.filterProvider')}</option>
          {(providers.length > 0 ? providers : FALLBACK_PROVIDERS).map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="h-8 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 text-[12.5px] outline-none focus:border-brand-400"
        >
          <option value="">{t('channels.filterStatus')}</option>
          {['active', 'cooldown', 'inactive', 'failed'].map((s) => (
            <option key={s} value={s}>
              {t(`channels.status.${s}`)}
            </option>
          ))}
        </select>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-x-auto px-4 py-4 md:px-6">
        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <TableRowSkeleton key={i} cols={8} />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={Server}
            title={t('channels.empty.title')}
            description={t('channels.empty.description')}
            action={
              <Button variant="primary" icon={Plus} onClick={() => setShowCreate(true)}>
                {t('channels.addChannel')}
              </Button>
            }
          />
        ) : (
          <div className="overflow-hidden rounded-xl border border-ink-200/60 dark:border-ink-700/60 bg-white shadow-soft">
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="border-b border-ink-100 dark:border-ink-800 bg-ink-50/60 dark:bg-ink-900/60 text-[11.5px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.name')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.provider')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.baseUrl')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.weight')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.status')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.lastHealth')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.lastError')}</th>
                  <th className="px-4 py-2.5 font-medium">{t('channels.table.cooldownUntil')}</th>
                  <th className="w-12 px-4 py-2.5 font-medium">{t('channels.table.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((ch) => {
                  const status = channelStatus(ch);
                  return (
                    <tr
                      key={ch.id}
                      className="border-b border-ink-100/50 dark:border-ink-800/50 transition-colors last:border-b-0 bg-ink-50/40 dark:bg-ink-900/40"
                    >
                      {/* Name */}
                      <td className="px-4 py-3">
                        <button
                          onClick={() => setEditChannel(ch)}
                          className="font-medium text-ink-900 dark:text-ink-100 transition-colors hover:text-brand-600"
                        >
                          {ch.name}
                        </button>
                      </td>

                      {/* Provider */}
                      <td className="px-4 py-3">
                        <Badge variant="info">{ch.provider}</Badge>
                      </td>

                      {/* Base URL */}
                      <td className="max-w-[200px] px-4 py-3">
                        <span
                          className="block truncate font-mono text-[12px] text-ink-500 dark:text-ink-400"
                          title={ch.base_url}
                        >
                          {ch.base_url}
                        </span>
                      </td>

                      {/* Weight (inline editable) */}
                      <td className="px-4 py-3">
                        <InlineWeight channel={ch} onUpdated={load} />
                      </td>

                      {/* Status */}
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`inline-block h-2 w-2 rounded-full ${STATUS_DOT[status]}`}
                          />
                          <Badge variant={STATUS_BADGE[status]}>
                            {t(`channels.status.${status}`)}
                          </Badge>
                        </div>
                      </td>

                      {/* Last health check */}
                      <td className="px-4 py-3 text-[12px] text-ink-500 dark:text-ink-400">
                        {ch.last_health_at ? timeAgo(ch.last_health_at, t) : t('channels.never')}
                      </td>

                      {/* Last error */}
                      <td className="max-w-[160px] px-4 py-3">
                        {ch.last_error ? (
                          <span
                            className="block truncate text-[12px] text-rose-500"
                            title={ch.last_error}
                          >
                            {ch.last_error}
                          </span>
                        ) : (
                          <span className="text-[12px] text-ink-300 dark:text-ink-600">{t('channels.none')}</span>
                        )}
                      </td>

                      {/* Cooldown until */}
                      <td className="px-4 py-3 text-[12px] text-ink-500 dark:text-ink-400">
                        {ch.cooldown_until && new Date(ch.cooldown_until).getTime() > Date.now()
                          ? formatDate(ch.cooldown_until, {
                              hour: '2-digit',
                              minute: '2-digit',
                              second: '2-digit',
                            })
                          : t('channels.none')}
                      </td>

                      {/* Actions */}
                      <td className="px-4 py-3">
                        <RowActions
                          channel={ch}
                          onTest={() => onTest(ch)}
                          onEdit={() => setEditChannel(ch)}
                          onToggle={() => onToggle(ch)}
                          onResetCooldown={() => onResetCooldown(ch)}
                          onDelete={() => setDeleteChannel(ch)}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Dialogs */}
      {showCreate && (
        <ChannelFormDialog
          onClose={() => setShowCreate(false)}
          onSaved={load}
          isEdit={false}
          providers={providers}
        />
      )}
      {editChannel && (
        <ChannelFormDialog
          channel={editChannel}
          onClose={() => setEditChannel(null)}
          onSaved={load}
          isEdit
          providers={providers}
        />
      )}
      {deleteChannel && (
        <DeleteConfirmDialog
          channel={deleteChannel}
          onClose={() => setDeleteChannel(null)}
          onDeleted={load}
        />
      )}
    </div>
  );
}
