import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Plug,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Settings2,
  KeyRound,
  Cpu,
  Power,
  Eye,
  EyeOff,
  Plus,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { maskKey } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import EmptyState from '@/components/EmptyState';
import Dialog from '@/components/Dialog';
import { CardSkeleton } from '@/components/Skeleton';
import ProviderLogo, { providerLabel } from '@/components/ProviderLogo';

function ProviderCard({ p, onTest, onConfigure, onToggle, busy }) {
  const { t } = useTranslation();
  return (
    <div className="card group/p relative overflow-hidden p-5 transition-all hover:shadow-pop">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-ink-100 dark:border-ink-800 bg-white dark:bg-ink-900">
            <ProviderLogo provider={p.name} size={22} />
          </div>
          <div>
            <div className="text-[15px] font-semibold text-ink-900 dark:text-ink-100">
              {p.display_name || providerLabel(p.name)}
            </div>
            <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[11.5px] text-ink-400 dark:text-ink-500">
              {p.name}
            </div>
          </div>
        </div>
        <button
          onClick={() => onToggle(p)}
          className={`flex h-6 w-10 items-center rounded-full p-0.5 transition-colors ${
            p.enabled !== false ? 'bg-ink-900 dark:bg-ink-100' : 'bg-ink-200 dark:bg-ink-700'
          }`}
          title={p.enabled !== false ? t('common.enabled') : t('common.disabled')}
        >
          <span
            className={`h-5 w-5 rounded-full bg-white shadow transition-transform ${
              p.enabled !== false ? 'translate-x-4' : 'translate-x-0'
            }`}
          />
        </button>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2 border-y border-ink-100 dark:border-ink-800 py-3">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-400 dark:text-ink-500">
            {t('providers.card.status')}
          </div>
          <div className="mt-0.5">
            {p.configured ? (
              <Badge variant="success" dot>
                {t('providers.card.configured')}
              </Badge>
            ) : (
              <Badge variant="default" dot>
                {t('providers.card.notConfigured')}
              </Badge>
            )}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-400 dark:text-ink-500">API Key</div>
          <div className="mt-0.5 font-mono text-[11.5px] text-ink-700 dark:text-ink-300">
            {p.api_key_masked || '—'}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-400 dark:text-ink-500">
            {t('providers.card.models')}
          </div>
          <div className="mt-0.5 text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {p.model_count != null ? `${p.model_count}` : '—'}
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-1.5">
        <Button variant="secondary" size="sm" onClick={() => onConfigure(p)}>
          <KeyRound size={12} /> {t('providers.card.configureKey')}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => onTest(p)} loading={busy === p.name}>
          <CheckCircle2 size={12} /> {t('providers.card.test')}
        </Button>
      </div>
    </div>
  );
}

function KeyTag({ tag, providerName, onDeleted }) {
  const { t } = useTranslation();
  const [pinging, setPinging] = useState(false);
  const [latency, setLatency] = useState(null);
  const [pingOk, setPingOk] = useState(null);

  const handlePing = async () => {
    if (!tag.id || pinging) return;
    setPinging(true);
    setLatency(null);
    try {
      const res = await api.pingProviderKey(providerName, tag.id);
      setLatency(res.latency_ms);
      setPingOk(res.success);
    } catch {
      setPingOk(false);
    } finally {
      setPinging(false);
    }
  };

  const handleDelete = async () => {
    if (!tag.id) return;
    try {
      await api.deleteProviderKey(providerName, tag.id);
      toast.success(t('providers.keyDeleted'));
      onDeleted?.();
    } catch (e) {
      toast.error(e.message || t('providers.deleteFailed'));
    }
  };

  return (
    <span className="group/tag inline-flex items-center gap-1 rounded-md border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-800/60 px-2 py-1 font-mono text-[11px] text-ink-700 dark:text-ink-300">
      <span className={tag.is_active ? '' : 'line-through opacity-50'}>{tag.masked}</span>
      {tag.id && (
        <>
          <button
            onClick={handlePing}
            disabled={pinging}
            className={`ml-0.5 rounded px-1 py-0.5 text-[9px] font-semibold transition-colors ${
              pingOk === true
                ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-400'
                : pingOk === false
                  ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400'
                  : 'bg-ink-100 text-ink-500 hover:bg-ink-200 dark:bg-ink-700 dark:text-ink-400 dark:hover:bg-ink-600'
            }`}
            title={t('providers.pingLatency')}
          >
            {pinging ? '...' : latency !== null ? `${latency}ms` : 'ping'}
          </button>
          <button
            onClick={handleDelete}
            className="rounded p-0.5 text-ink-300 opacity-0 transition-opacity hover:text-red-500 group-hover/tag:opacity-100 dark:text-ink-600 dark:hover:text-red-400"
            title={t('providers.deleteProviderKey')}
          >
            <XCircle size={12} />
          </button>
        </>
      )}
    </span>
  );
}

function ConfigDialog({ provider, onClose, onSaved }) {
  const { t } = useTranslation();
  const [newKeys, setNewKeys] = useState('');
  const [existingKeys, setExistingKeys] = useState([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [apiBase, setApiBase] = useState(provider?.api_base || provider?.default_api_base || '');
  const [enabled, setEnabled] = useState(provider?.enabled !== false);
  const [saving, setSaving] = useState(false);

  const loadKeys = useCallback(async () => {
    if (!provider?.name) return;
    setKeysLoading(true);
    try {
      const res = await api.getProviderKeys(provider.name);
      setExistingKeys(res?.keys || []);
    } catch {
      setExistingKeys([]);
    } finally {
      setKeysLoading(false);
    }
  }, [provider?.name]);

  useEffect(() => {
    setNewKeys('');
    setApiBase(provider?.api_base || provider?.default_api_base || '');
    setEnabled(provider?.enabled !== false);
    loadKeys();
  }, [provider, loadKeys]);

  if (!provider) return null;

  const save = async () => {
    setSaving(true);
    try {
      // Parse input: comma-separated keys
      const keys = newKeys
        .split(',')
        .map((k) => k.trim())
        .filter(Boolean);

      const payload = {
        api_base: apiBase || null,
        enabled,
      };

      if (keys.length > 0) {
        // Append new keys to existing channels
        payload.api_keys = keys;
      }

      const res = await api.saveProviderConfig(provider.name, payload);
      const extra = res?.channels_created
        ? `, ${t('providers.channelsCreated', { count: res.channels_created })}`
        : '';
      toast.success(t('providers.toast.saved') + extra);
      setNewKeys('');
      loadKeys(); // refresh tags
      onSaved?.();
    } catch (e) {
      toast.error(e.message || t('providers.toast.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('providers.dialog.title', {
        name: provider.display_name || providerLabel(provider.name),
      })}
      description={t('providers.dialog.description')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={save} loading={saving}>
            {t('providers.dialog.saveConfig')}
          </Button>
        </>
      }
    >
      <div className="space-y-3.5">
        {/* Existing keys as tags */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('providers.configuredApiKeys')}
          </label>
          <div className="flex min-h-[32px] flex-wrap gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-2">
            {keysLoading ? (
              <span className="text-[11px] text-ink-400">{t('providers.loadingKeys')}</span>
            ) : existingKeys.length === 0 ? (
              <span className="text-[11px] text-ink-400">{t('providers.addKeyHint')}</span>
            ) : (
              existingKeys.map((tag) => (
                <KeyTag
                  key={tag.id || tag.masked}
                  tag={tag}
                  providerName={provider.name}
                  onDeleted={() => {
                    loadKeys();
                    onSaved?.();
                  }}
                />
              ))
            )}
          </div>
          <p className="mt-1 text-[10.5px] text-ink-400 dark:text-ink-500">
            {t('providers.pauseToDelete')}
          </p>
        </div>

        {/* Add new keys */}
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('providers.addProviderKey')}
          </label>
          <input
            value={newKeys}
            onChange={(e) => setNewKeys(e.target.value)}
            placeholder="sk-xxxxx, sk-yyy, ..."
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12.5px] text-ink-900 dark:text-ink-100 placeholder-ink-400 outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
          <p className="mt-1 text-[10.5px] text-ink-400 dark:text-ink-500">
            {t('providers.multiKeyHint')}
          </p>
        </div>

        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">API Base URL</label>
          <input
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder={provider.default_api_base || 'https://api.example.com/v1'}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] text-ink-900 dark:text-ink-100 placeholder-ink-400 outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <label className="flex items-center gap-2.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-3">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="h-4 w-4 rounded border-ink-300 text-ink-900 dark:text-ink-100 focus:ring-ink-900"
          />
          <div>
            <div className="text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {t('providers.dialog.enablePlatform')}
            </div>
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400">{t('providers.dialog.enableHint')}</div>
          </div>
        </label>
      </div>
    </Dialog>
  );
}

export default function Providers() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [testing, setTesting] = useState(null);
  const [editing, setEditing] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getProviders();
      setProviders(Array.isArray(data) ? data : []);
    } catch {
      setProviders([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const onTest = async (p) => {
    setTesting(p.name);
    try {
      const res = await api.testProvider(p.name);
      if (res?.success) {
        toast.success(res.message || t('providers.toast.connectSuccess'));
        // Test now auto-caches models; refresh to update model_count.
        load();
      } else {
        toast.error(res?.message || t('providers.toast.connectFailed'));
      }
    } catch (e) {
      toast.error(e.message || t('providers.toast.testFailed'));
    } finally {
      setTesting(null);
    }
  };

  const onToggle = async (p) => {
    try {
      await api.saveProviderConfig(p.name, { enabled: !(p.enabled !== false) });
      toast.success(p.enabled !== false ? t('common.disabled') : t('common.enabled'));
      load();
    } catch (e) {
      toast.error(e.message || t('providers.toast.operationFailed'));
    }
  };

  const onRefreshAll = async () => {
    setRefreshing(true);
    try {
      const res = await api.refreshAllProviders();
      toast.success(res?.message || t('providers.toast.refreshed'));
      load();
    } catch (e) {
      toast.error(e.message || t('providers.toast.refreshFailed'));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <>
      <TopBar
        title={t('providers.title')}
        subtitle={t('providers.subtitleCount', { count: providers.length })}
        action={
          <Button onClick={onRefreshAll} loading={refreshing} variant="secondary" icon={RefreshCw}>
            {t('providers.refreshAll')}
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-7xl p-4 md:p-6">
          {loading ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <CardSkeleton key={i} />
              ))}
            </div>
          ) : providers.length === 0 ? (
            <EmptyState
              icon={Plug}
              title={t('providers.empty.title')}
              description={t('providers.empty.description')}
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {providers.map((p) => (
                <ProviderCard
                  key={p.name}
                  p={p}
                  busy={testing}
                  onTest={onTest}
                  onConfigure={setEditing}
                  onToggle={onToggle}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {editing && (
        <ConfigDialog provider={editing} onClose={() => setEditing(null)} onSaved={load} />
      )}
    </>
  );
}
