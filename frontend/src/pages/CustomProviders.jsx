import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, Plug, RefreshCw, Trash2, ExternalLink, KeyRound, CheckCircle2 } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';

function CreateDialog({ onClose, onCreated }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    name: '',
    slug: '',
    display_name: '',
    api_base: '',
    api_key: '',
    api_keys: '',
    notes: '',
  });
  const [saving, setSaving] = useState(false);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const keys = form.api_keys
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      await api.createCustomProvider({
        name: form.name,
        slug: form.slug || undefined,
        display_name: form.display_name || undefined,
        api_base: form.api_base,
        api_key: form.api_key || undefined,
        api_keys: keys.length > 0 ? keys : undefined,
        notes: form.notes || '',
      });
      toast.success(t('customProviders.toast.added'));
      onCreated?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('customProviders.toast.addFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('customProviders.create.title')}
      description={t('customProviders.create.description')}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={saving}>
            {t('customProviders.create.add')}
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
              {t('customProviders.form.platformName')}
            </label>
            <input
              required
              value={form.name}
              onChange={set('name')}
              placeholder="siliconflow"
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
              {t('customProviders.form.displayName')}
            </label>
            <input
              value={form.display_name}
              onChange={set('display_name')}
              placeholder="SiliconFlow"
              className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
            />
          </div>
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('customProviders.form.slug')}
          </label>
          <input
            value={form.slug}
            onChange={set('slug')}
            placeholder={t('customProviders.form.slugPlaceholder')}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            API Base URL *
          </label>
          <input
            required
            value={form.api_base}
            onChange={set('api_base')}
            placeholder="https://api.siliconflow.cn/v1"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('customProviders.form.primaryApiKey')}
          </label>
          <input
            type="password"
            value={form.api_key}
            onChange={set('api_key')}
            placeholder="sk-..."
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('customProviders.form.backupKeys')}
          </label>
          <textarea
            rows={3}
            value={form.api_keys}
            onChange={set('api_keys')}
            placeholder={'sk-key-1\nsk-key-2'}
            className="w-full rounded-lg border border-ink-200 bg-white px-3 py-2 font-mono text-[12px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('customProviders.form.notes')}
          </label>
          <input
            value={form.notes}
            onChange={set('notes')}
            placeholder={t('customProviders.form.optional')}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
        <div className="rounded-lg bg-ink-50/60 dark:bg-ink-900/60 p-3 text-[11.5px] text-ink-600 dark:text-ink-400">
          {t('customProviders.create.supportedPlatforms')}
        </div>
      </form>
    </Dialog>
  );
}

function DeleteDialog({ provider, onClose, onDeleted }) {
  const { t } = useTranslation();
  const [deleting, setDeleting] = useState(false);
  const del = async () => {
    setDeleting(true);
    try {
      await api.deleteCustomProvider(provider.slug);
      toast.success(t('customProviders.toast.deleted'));
      onDeleted?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('customProviders.toast.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={t('customProviders.delete.title')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" onClick={del} loading={deleting}>
            {t('customProviders.delete.confirm')}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-ink-700 dark:text-ink-300">
        {t('customProviders.delete.description', { name: provider.slug })}
      </p>
    </Dialog>
  );
}

export default function CustomProviders() {
  const { t } = useTranslation();
  const [list, setList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [testing, setTesting] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getCustomProviders();
      setList(Array.isArray(data) ? data : []);
    } catch {
      setList([]);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const refreshAll = async () => {
    setRefreshing(true);
    try {
      const res = await api.refreshAllCustomProviders();
      toast.success(res?.message || t('customProviders.toast.refreshed'));
      load();
    } catch (e) {
      toast.error(e.message || t('customProviders.toast.refreshFailed'));
    } finally {
      setRefreshing(false);
    }
  };

  const test = async (p) => {
    setTesting(p.slug);
    try {
      const res = await api.testCustomProvider(p.slug);
      if (res?.success) toast.success(res.message || t('customProviders.toast.connectSuccess'));
      else toast.error(res?.message || t('customProviders.toast.connectFailed'));
    } catch (e) {
      toast.error(e.message || t('customProviders.toast.testFailed'));
    } finally {
      setTesting(null);
    }
  };

  return (
    <>
      <TopBar
        title={t('customProviders.title')}
        subtitle={t('customProviders.subtitle')}
        action={
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={refreshAll} loading={refreshing} icon={RefreshCw}>
              {t('customProviders.refreshModels')}
            </Button>
            <Button onClick={() => setCreating(true)} icon={Plus}>
              {t('customProviders.addPlatform')}
            </Button>
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-7xl p-4 md:p-6">
          {loading ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <CardSkeleton key={i} />
              ))}
            </div>
          ) : list.length === 0 ? (
            <EmptyState
              icon={Plug}
              title={t('customProviders.empty.title')}
              description={t('customProviders.empty.description')}
              action={
                <Button onClick={() => setCreating(true)} icon={Plus}>
                  {t('customProviders.empty.addFirst')}
                </Button>
              }
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {list.map((p) => {
                const keysCount =
                  (p.api_keys || '').split(',').filter(Boolean).length || (p.api_key ? 1 : 0);
                return (
                  <div key={p.slug} className="card p-5 transition-all hover:shadow-pop">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-3">
                        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-500 to-sky-500 text-white">
                          <Plug size={15} />
                        </div>
                        <div>
                          <div className="text-[13.5px] font-semibold text-ink-900 dark:text-ink-100">
                            {p.display_name || p.slug}
                          </div>
                          <div className="mt-0.5 font-mono text-[10.5px] text-ink-400 dark:text-ink-500">
                            custom:{p.slug}
                          </div>
                        </div>
                      </div>
                      {p.is_enabled ? (
                        <Badge variant="success" dot>
                          {t('customProviders.status.enabled')}
                        </Badge>
                      ) : (
                        <Badge variant="default" dot>
                          {t('customProviders.status.disabled')}
                        </Badge>
                      )}
                    </div>

                    <div className="mt-4 space-y-1.5 border-y border-ink-100 dark:border-ink-800 py-3 text-[12px]">
                      <div className="flex items-start justify-between gap-2">
                        <span className="shrink-0 text-ink-500 dark:text-ink-400">{t('providers.baseUrl')}</span>
                        <span className="break-all text-right font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
                          {p.api_base}
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-ink-500 dark:text-ink-400">{t('customProviders.apiKeys')}</span>
                        <span className="flex items-center gap-1 font-medium text-ink-700 dark:text-ink-300">
                          <KeyRound size={11} />
                          {keysCount}
                        </span>
                      </div>
                      {p.notes && (
                        <div className="flex items-start justify-between gap-2">
                          <span className="text-ink-500 dark:text-ink-400">{t('customProviders.form.notes')}</span>
                          <span className="text-right text-ink-700 dark:text-ink-300">{p.notes}</span>
                        </div>
                      )}
                    </div>

                    <div className="mt-3 flex items-center gap-1.5">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => test(p)}
                        loading={testing === p.slug}
                      >
                        <CheckCircle2 size={12} /> {t('customProviders.card.test')}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setDeleting(p)}>
                        <Trash2 size={12} /> {t('customProviders.card.delete')}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {creating && <CreateDialog onClose={() => setCreating(false)} onCreated={load} />}
      {deleting && (
        <DeleteDialog provider={deleting} onClose={() => setDeleting(null)} onDeleted={load} />
      )}
    </>
  );
}
