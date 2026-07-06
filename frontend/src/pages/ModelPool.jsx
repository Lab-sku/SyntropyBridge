import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Layers,
  Plus,
  Trash2,
  RefreshCw,
  Copy,
  AlertTriangle,
  ShieldCheck,
  KeyRound,
  ChevronUp,
  ChevronDown,
  Pencil,
  Power,
  Server,
  Check,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';
import { copyToClipboard, formatDate, formatNumber } from '@/lib/utils';

/**
 * Model Pool management page.
 *
 * Two tabs:
 *  1. Pools — CRUD over user-owned upstream model pools, priority-ordered.
 *  2. Keys  — unified ``sk-ump_*`` keys that route across all pools by
 *             priority; the full secret is returned exactly once at create.
 */
export default function ModelPool() {
  const { t } = useTranslation();
  const [tab, setTab] = useState('pools');

  return (
    <>
      <TopBar title={t('modelPool.title')} subtitle={t('modelPool.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-ink-50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
          {/* Tab switcher */}
          <div className="inline-flex items-center gap-1 rounded-xl border border-ink-200 bg-white p-1 shadow-soft dark:border-ink-700 dark:bg-ink-900">
            <button
              onClick={() => setTab('pools')}
              className={
                'flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-medium transition-all ' +
                (tab === 'pools'
                  ? 'bg-ink-900 text-white shadow-sm dark:bg-ink-100 dark:text-ink-900'
                  : 'text-ink-600 hover:bg-ink-50 dark:text-ink-400 dark:hover:bg-ink-800')
              }
            >
              <Layers size={14} />
              {t('modelPool.tabPools')}
            </button>
            <button
              onClick={() => setTab('keys')}
              className={
                'flex items-center gap-1.5 rounded-lg px-4 py-2 text-[13px] font-medium transition-all ' +
                (tab === 'keys'
                  ? 'bg-ink-900 text-white shadow-sm dark:bg-ink-100 dark:text-ink-900'
                  : 'text-ink-600 hover:bg-ink-50 dark:text-ink-400 dark:hover:bg-ink-800')
              }
            >
              <KeyRound size={14} />
              {t('modelPool.tabKeys')}
            </button>
          </div>

          {tab === 'pools' ? <PoolsTab /> : <KeysTab />}
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab 1 — Pools
// ---------------------------------------------------------------------------

function PoolsTab() {
  const { t } = useTranslation();
  const [pools, setPools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null); // null | {} (add) | existing pool (edit)
  const [deleteTarget, setDeleteTarget] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listModelPools().catch(() => []);
      setPools(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const sorted = useMemo(() => {
    return [...pools].sort((a, b) => {
      const pa = a.priority ?? 0;
      const pb = b.priority ?? 0;
      if (pa !== pb) return pa - pb;
      return (a.id || 0) - (b.id || 0);
    });
  }, [pools]);

  const onMove = async (index, direction) => {
    const newIndex = index + direction;
    if (newIndex < 0 || newIndex >= sorted.length) return;
    const reordered = [...sorted];
    [reordered[index], reordered[newIndex]] = [reordered[newIndex], reordered[index]];
    // Reassign priorities based on new position so the list stays ordered.
    const updated = reordered.map((p, i) => ({ ...p, priority: i }));
    setPools(updated);
    try {
      await api.reorderModelPools(updated.map((p) => p.id));
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
      load(); // rollback on failure
    }
  };

  const onToggleActive = async (pool) => {
    try {
      await api.updateModelPool(pool.id, { is_active: !pool.is_active });
      setPools((prev) =>
        prev.map((p) => (p.id === pool.id ? { ...p, is_active: !p.is_active } : p)),
      );
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    }
  };

  const onDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteModelPool(deleteTarget.id);
      toast.success(t('common.delete'));
      setDeleteTarget(null);
      load();
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    }
  };

  return (
    <>
      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
          {t('common.refresh')}
        </Button>
        <Button size="sm" icon={Plus} onClick={() => setEditing({})}>
          {t('modelPool.addModel')}
        </Button>
      </div>

      {/* Usage guide */}
      <div className="card p-4">
        <div className="flex items-start gap-3 text-[12px] text-ink-600 dark:text-ink-400">
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-600" />
          <p>{t('modelPool.usageGuide')}</p>
        </div>
      </div>

      {loading && pools.length === 0 ? (
        <div className="space-y-2">
          <CardSkeleton />
          <CardSkeleton />
          <CardSkeleton />
        </div>
      ) : sorted.length === 0 ? (
        <EmptyState
          icon={Layers}
          title={t('modelPool.noPools')}
          action={
            <Button icon={Plus} onClick={() => setEditing({})}>
              {t('modelPool.addModel')}
            </Button>
          }
        />
      ) : (
        <div className="space-y-2.5">
          {sorted.map((pool, idx) => (
            <PoolCard
              key={pool.id}
              pool={pool}
              index={idx}
              total={sorted.length}
              onMove={onMove}
              onEdit={() => setEditing(pool)}
              onDelete={() => setDeleteTarget(pool)}
              onToggleActive={() => onToggleActive(pool)}
            />
          ))}
        </div>
      )}

      {editing && (
        <PoolDialog
          pool={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}

      {deleteTarget && (
        <Dialog
          open
          onClose={() => setDeleteTarget(null)}
          size="sm"
          title={t('common.delete')}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)}>
                {t('common.cancel')}
              </Button>
              <Button variant="danger" icon={Trash2} onClick={onDelete}>
                {t('common.delete')}
              </Button>
            </>
          }
        >
          <div className="flex items-start gap-3 text-[12.5px] text-ink-700 dark:text-ink-300">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
            <p>{t('modelPool.deleteConfirm')}</p>
          </div>
        </Dialog>
      )}
    </>
  );
}

function PoolCard({ pool, index, total, onMove, onEdit, onDelete, onToggleActive }) {
  const { t } = useTranslation();

  const status = pool.status || 'active';
  const statusVariant = status === 'active' ? 'success' : status === 'cooldown' ? 'warning' : 'danger';
  const statusLabel = t(`modelPool.${status}`);

  const isActive = pool.is_active !== false;
  const maxTokens = Number(pool.max_tokens || 0);
  const usedTokens = Number(pool.used_tokens || 0);
  const unlimited = maxTokens === 0;
  const pct = unlimited ? 0 : maxTokens > 0 ? Math.min(100, Math.round((usedTokens / maxTokens) * 100)) : 0;

  const apiBaseDomain = useMemo(() => {
    const url = pool.api_base || '';
    if (!url) return '';
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
  }, [pool.api_base]);

  return (
    <div className="card p-4 transition-all hover:shadow-soft">
      <div className="flex flex-wrap items-start gap-4">
        {/* Priority + drag controls */}
        <div className="flex flex-col items-center gap-1">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-gradient-to-br from-indigo-500 to-violet-600 text-[12px] font-bold text-white">
            {pool.priority ?? index}
          </div>
          <div className="flex flex-col">
            <button
              onClick={() => onMove(index, -1)}
              disabled={index === 0}
              title={t('modelPool.moveUp')}
              className="rounded p-0.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 disabled:opacity-30 disabled:hover:bg-transparent dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
            >
              <ChevronUp size={14} />
            </button>
            <button
              onClick={() => onMove(index, 1)}
              disabled={index === total - 1}
              title={t('modelPool.moveDown')}
              className="rounded p-0.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 disabled:opacity-30 disabled:hover:bg-transparent dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
            >
              <ChevronDown size={14} />
            </button>
          </div>
        </div>

        {/* Main info */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13.5px] font-semibold text-ink-900 dark:text-ink-100">
              {pool.name || '—'}
            </span>
            <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
              {pool.model_name || '—'}
            </code>
            <Badge variant="accent">{pool.provider_type || 'openai'}</Badge>
            <Badge variant={statusVariant} dot>
              {statusLabel}
            </Badge>
            {!isActive && (
              <Badge variant="danger">{t('common.disabled')}</Badge>
            )}
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-3 text-[11.5px] text-ink-500 dark:text-ink-400">
            <span className="inline-flex items-center gap-1">
              <Server size={11} />
              {apiBaseDomain || '—'}
            </span>
            <span>
              {t('modelPool.priority')}: {pool.priority ?? 0}
            </span>
          </div>

          {/* Usage progress */}
          <div className="mt-2.5">
            {unlimited ? (
              <div className="text-[11.5px] font-medium text-ink-400 dark:text-ink-500">
                {t('modelPool.unlimited')}
              </div>
            ) : (
              <div>
                <div className="mb-1 flex items-center justify-between text-[11px] text-ink-500 dark:text-ink-400">
                  <span>
                    {t('modelPool.used')}: {formatNumber(usedTokens)} / {formatNumber(maxTokens)}
                  </span>
                  <span>{pct}%</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                  <div
                    className={
                      'h-full rounded-full transition-all ' +
                      (pct >= 90
                        ? 'bg-gradient-to-r from-rose-400 to-rose-500'
                        : pct >= 70
                          ? 'bg-gradient-to-r from-amber-400 to-amber-500'
                          : 'bg-gradient-to-r from-emerald-400 to-emerald-500')
                    }
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="secondary" icon={Pencil} onClick={onEdit}>
            {t('common.edit')}
          </Button>
          <Button
            size="sm"
            variant={isActive ? 'ghost' : 'success'}
            icon={Power}
            onClick={onToggleActive}
          >
            {isActive ? t('common.disabled') : t('common.enabled')}
          </Button>
          <Button size="sm" variant="danger" icon={Trash2} onClick={onDelete}>
            {t('common.delete')}
          </Button>
        </div>
      </div>
    </div>
  );
}

function PoolDialog({ pool, onClose, onSaved }) {
  const { t } = useTranslation();
  const isEdit = Boolean(pool.id);
  const [name, setName] = useState(pool.name || '');
  const [providerType, setProviderType] = useState(pool.provider_type || 'openai');
  const [apiBase, setApiBase] = useState(pool.api_base || '');
  const [apiKey, setApiKey] = useState('');
  const [modelName, setModelName] = useState(pool.model_name || '');
  const [maxTokens, setMaxTokens] = useState(
    pool.max_tokens != null ? String(pool.max_tokens) : '',
  );
  const [priority, setPriority] = useState(
    pool.priority != null ? String(pool.priority) : '0',
  );
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!name.trim()) {
      toast.error(t('modelPool.name'));
      return;
    }
    if (!apiBase.trim()) {
      toast.error(t('modelPool.apiBase'));
      return;
    }
    if (!isEdit && !apiKey.trim()) {
      toast.error(t('modelPool.apiKey'));
      return;
    }
    if (!modelName.trim()) {
      toast.error(t('modelPool.modelName'));
      return;
    }

    const body = {
      name: name.trim(),
      provider_type: providerType,
      api_base: apiBase.trim(),
      model_name: modelName.trim(),
      max_tokens: maxTokens === '' ? 0 : Number(maxTokens),
      priority: priority === '' ? 0 : Number(priority),
    };
    if (apiKey.trim()) body.api_key = apiKey.trim();

    setSubmitting(true);
    try {
      if (isEdit) {
        await api.updateModelPool(pool.id, body);
        toast.success(t('common.save'));
      } else {
        await api.createModelPool(body);
        toast.success(t('common.create'));
      }
      onSaved?.();
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10';
  const labelCls = 'mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300';

  return (
    <Dialog
      open
      onClose={onClose}
      title={isEdit ? t('modelPool.editModel') : t('modelPool.addModel')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={isEdit ? Check : Plus}>
            {isEdit ? t('common.save') : t('common.create')}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <label className={labelCls}>
            {t('modelPool.name')} <span className="text-rose-500">*</span>
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. OpenAI-primary"
            className={inputCls}
          />
        </div>
        <div>
          <label className={labelCls}>{t('modelPool.providerType')}</label>
          <select
            value={providerType}
            onChange={(e) => setProviderType(e.target.value)}
            className={inputCls}
          >
            <option value="openai">openai</option>
            <option value="anthropic">anthropic</option>
            <option value="custom">custom</option>
          </select>
        </div>
        <div>
          <label className={labelCls}>{t('modelPool.modelName')}</label>
          <input
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            placeholder="e.g. gpt-4"
            className={`${inputCls} font-mono`}
          />
        </div>
        <div className="md:col-span-2">
          <label className={labelCls}>
            {t('modelPool.apiBase')} <span className="text-rose-500">*</span>
          </label>
          <input
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder="https://api.openai.com/v1"
            className={`${inputCls} font-mono`}
          />
        </div>
        <div className="md:col-span-2">
          <label className={labelCls}>
            {t('modelPool.apiKey')}{' '}
            {isEdit && (
              <span className="text-[11px] font-normal text-ink-400">
                ({t('channels.form.apiKeyChange')})
              </span>
            )}
            {!isEdit && <span className="text-rose-500">*</span>}
          </label>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-..."
            className={`${inputCls} font-mono`}
          />
        </div>
        <div>
          <label className={labelCls}>
            {t('modelPool.maxTokens')}{' '}
            <span className="text-[11px] font-normal text-ink-400">
              ({t('modelPool.maxTokensHint')})
            </span>
          </label>
          <input
            type="number"
            min={0}
            value={maxTokens}
            onChange={(e) => setMaxTokens(e.target.value)}
            placeholder="0"
            className={`${inputCls} font-mono`}
          />
        </div>
        <div>
          <label className={labelCls}>
            {t('modelPool.priority')}{' '}
            <span className="text-[11px] font-normal text-ink-400">
              ({t('modelPool.priorityHint')})
            </span>
          </label>
          <input
            type="number"
            min={0}
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            placeholder="0"
            className={`${inputCls} font-mono`}
          />
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Tab 2 — Unified keys
// ---------------------------------------------------------------------------

function KeysTab() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [createdSecret, setCreatedSecret] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.listModelPoolKeys().catch(() => []);
      setKeys(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const onGenerate = async (name) => {
    try {
      const res = await api.createModelPoolKey(name);
      setGenerating(false);
      setCreatedSecret(res?.key || res?.api_key || res?.secret || '');
      load();
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    }
  };

  const onDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteModelPoolKey(deleteTarget.id);
      toast.success(t('common.delete'));
      setDeleteTarget(null);
      load();
    } catch (e) {
      toast.error(e.message || t('common.operationFailed'));
    }
  };

  return (
    <>
      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
          {t('common.refresh')}
        </Button>
        <Button size="sm" icon={Plus} onClick={() => setGenerating(true)}>
          {t('modelPool.generateKey')}
        </Button>
      </div>

      {/* Usage guide */}
      <div className="card p-4">
        <div className="flex items-start gap-3 text-[12px] text-ink-600 dark:text-ink-400">
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-600" />
          <p>{t('modelPool.usageGuide')}</p>
        </div>
      </div>

      {loading && keys.length === 0 ? (
        <div className="space-y-2">
          <CardSkeleton />
          <CardSkeleton />
        </div>
      ) : keys.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          title={t('modelPool.noKeys')}
          action={
            <Button icon={Plus} onClick={() => setGenerating(true)}>
              {t('modelPool.generateKey')}
            </Button>
          }
        />
      ) : (
        <div className="space-y-2">
          {keys.map((k) => (
            <KeyRow key={k.id} item={k} onDelete={() => setDeleteTarget(k)} />
          ))}
        </div>
      )}

      {generating && (
        <GenerateKeyDialog onClose={() => setGenerating(false)} onGenerate={onGenerate} />
      )}

      {createdSecret && (
        <SecretRevealDialog secret={createdSecret} onClose={() => setCreatedSecret(null)} />
      )}

      {deleteTarget && (
        <Dialog
          open
          onClose={() => setDeleteTarget(null)}
          size="sm"
          title={t('common.delete')}
          footer={
            <>
              <Button variant="secondary" onClick={() => setDeleteTarget(null)}>
                {t('common.cancel')}
              </Button>
              <Button variant="danger" icon={Trash2} onClick={onDelete}>
                {t('common.delete')}
              </Button>
            </>
          }
        >
          <div className="flex items-start gap-3 text-[12.5px] text-ink-700 dark:text-ink-300">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
            <p>{t('modelPool.deleteKeyConfirm')}</p>
          </div>
        </Dialog>
      )}
    </>
  );
}

function KeyRow({ item, onDelete }) {
  const { t } = useTranslation();
  return (
    <div className="card p-4 transition-all hover:shadow-soft">
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-md bg-gradient-to-br from-indigo-600 to-violet-600 text-white">
          <KeyRound size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13.5px] font-semibold text-ink-900 dark:text-ink-100">
              {item.name || t('apiKeys.unnamed')}
            </span>
            <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
              {item.key_prefix || 'sk-ump_****'}
            </code>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11.5px] text-ink-500 dark:text-ink-400">
            <span>
              {t('modelPool.createdAt')}: {formatDate(item.created_at)}
            </span>
            {item.last_used_at ? (
              <span>
                {t('modelPool.lastUsedAt')}: {formatDate(item.last_used_at)}
              </span>
            ) : null}
          </div>
        </div>
        <Button size="sm" variant="danger" icon={Trash2} onClick={onDelete}>
          {t('common.delete')}
        </Button>
      </div>
    </div>
  );
}

function GenerateKeyDialog({ onClose, onGenerate }) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!name.trim()) {
      toast.error(t('apiKeys.nameRequired'));
      return;
    }
    setSubmitting(true);
    try {
      await onGenerate(name.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('modelPool.generateKey')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={Plus}>
            {t('common.create')}
          </Button>
        </>
      }
    >
      <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
        {t('apiKeys.keyName')} <span className="text-rose-500">*</span>
      </label>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder={t('apiKeys.namePlaceholder')}
        className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit();
        }}
      />
    </Dialog>
  );
}

function SecretRevealDialog({ secret, onClose }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await copyToClipboard(secret);
    setCopied(true);
    toast.success(t('common.copied'));
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('modelPool.generateKey')}
      size="md"
      footer={
        <Button icon={copied ? ShieldCheck : Copy} onClick={copy}>
          {copied ? t('modelPool.copied') : t('modelPool.copyKey')}
        </Button>
      }
    >
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 dark:bg-amber-900/20 p-3 text-[12px] text-amber-700 dark:text-amber-400">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{t('modelPool.keyOnceWarning')}</span>
        </div>
        <div className="flex items-center gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/50 p-2.5">
          <code className="flex-1 break-all font-mono text-[12px] text-ink-800 dark:text-ink-200">
            {secret}
          </code>
          <button
            type="button"
            onClick={copy}
            className="rounded p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
            title={t('common.copy')}
          >
            <Copy size={14} />
          </button>
        </div>
      </div>
    </Dialog>
  );
}
