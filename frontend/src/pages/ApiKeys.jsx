import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  KeyRound,
  Plus,
  Copy,
  Trash2,
  RefreshCw,
  Eye,
  EyeOff,
  ShieldCheck,
  AlertTriangle,
  X,
  Calendar,
  Search,
  Users as UsersIcon,
  Code2,
  Terminal,
  ChevronDown,
  ChevronRight,
  Check,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';
import { copyToClipboard, formatDate, formatNumber } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';

/**
 * API Key management page — dual perspective.
 *
 *   - User view (``isAdmin === false``): the user manages their own
 *     sub-keys. CRUD hits ``/user/api-keys`` and returns the secret
 *     once on create / rotate.
 *   - Admin view (``isAdmin === true``): platform-wide view through
 *     ``/admin/api-keys``. Admins can list every key across every
 *     user (optionally filtered by ``user_id``) and revoke them,
 *     but they cannot reveal a key's secret (the secret is only
 *     ever returned to the user who owns it at issue time).
 */
export default function ApiKeys() {
  const { t } = useTranslation();
  const isAdmin = useAuthStore((s) => s.role === 'admin');
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [issuing, setIssuing] = useState(false);
  const [revokeId, setRevokeId] = useState(null);
  const [createdSecret, setCreatedSecret] = useState(null);
  const [userFilter, setUserFilter] = useState('');
  const [query, setQuery] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const data = isAdmin
        ? await api.listAllApiKeys().catch(() => [])
        : await api.listMyApiKeys().catch(() => []);
      setKeys(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin]);

  const onRevoke = async () => {
    if (!revokeId) return;
    try {
      if (isAdmin) {
        await api.adminRevokeApiKey(revokeId);
      } else {
        await api.revokeApiKey(revokeId);
      }
      toast.success(t('apiKeys.revokeOk'));
      setRevokeId(null);
      load();
    } catch (e) {
      toast.error(e.message || t('apiKeys.revokeFailed'));
    }
  };

  const onRotate = async (id) => {
    if (isAdmin) {
      // Admins cannot rotate on behalf of a user — the user must
      // generate a new secret themselves.
      toast.error(t('apiKeys.adminRotateHint'));
      return;
    }
    try {
      // The existing PATCH endpoint updates metadata, not the secret.
      // For "rotate" we instead issue a brand new key with the same
      // name (idempotent from the user's perspective) and let the
      // user revoke the old one if desired.
      const old = keys.find((k) => k.id === id);
      if (!old) return;
      const result = await api.createApiKey({
        name: `${old.name || 'key'} (rotated)`,
        monthly_token_limit: old.monthly_token_limit,
        monthly_credit_limit: old.monthly_credit_limit,
        allowed_models: Array.isArray(old.allowed_models) ? old.allowed_models : undefined,
        expires_at: old.expires_at,
      });
      setCreatedSecret(result?.api_key || result?.secret || null);
      toast.success(t('apiKeys.rotateOk'));
      load();
    } catch (e) {
      toast.error(e.message || t('apiKeys.rotateFailed'));
    }
  };

  const filtered = useMemo(() => {
    let rows = keys;
    if (isAdmin && userFilter) {
      const q = userFilter.toLowerCase();
      rows = rows.filter((k) =>
        String(k.username || k.user_id || '')
          .toLowerCase()
          .includes(q),
      );
    }
    if (query) {
      const q = query.toLowerCase();
      rows = rows.filter((k) => {
        return (
          String(k.name || '')
            .toLowerCase()
            .includes(q) ||
          String(k.key_prefix || '')
            .toLowerCase()
            .includes(q) ||
          String(k.key_mask || '')
            .toLowerCase()
            .includes(q)
        );
      });
    }
    return rows;
  }, [keys, isAdmin, userFilter, query]);

  return (
    <>
      <TopBar
        title={t('apiKeys.title')}
        subtitle={
          isAdmin ? `${t('apiKeys.subtitle')} · ${t('nav.overview')}` : t('apiKeys.subtitle')
        }
        action={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
              {t('common.refresh')}
            </Button>
            {!isAdmin ? (
              <Button size="sm" icon={Plus} onClick={() => setIssuing(true)}>
                {t('apiKeys.newKey')}
              </Button>
            ) : null}
          </div>
        }
      />

      <div className="flex-1 overflow-y-auto bg-ink-50 dark:bg-ink-900/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
          <div className="card p-4">
            <div className="flex items-start gap-3 text-[12px] text-ink-600 dark:text-ink-400">
              <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-600" />
              <p>{t('apiKeys.intro')}</p>
            </div>
          </div>

          {/* How to integrate */}
          {!isAdmin && <IntegrationGuide />}

          {/* Filters */}
          <div className="flex flex-wrap items-center gap-2">
            {isAdmin ? (
              <div className="flex items-center gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
                <UsersIcon size={12} className="text-ink-400 dark:text-ink-500" />
                <input
                  value={userFilter}
                  onChange={(e) => setUserFilter(e.target.value)}
                  placeholder={t('apiKeys.filterByUser')}
                  className="w-40 bg-transparent text-[12px] outline-none placeholder:text-ink-400 dark:text-ink-500"
                />
              </div>
            ) : null}
            <div className="flex flex-1 items-center gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
              <Search size={12} className="text-ink-400 dark:text-ink-500" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('apiKeys.searchPlaceholder')}
                className="flex-1 bg-transparent text-[12px] outline-none placeholder:text-ink-400 dark:text-ink-500"
              />
            </div>
          </div>

          {loading && keys.length === 0 ? (
            <CardSkeleton rows={3} />
          ) : keys.length === 0 ? (
            <EmptyState
              icon={KeyRound}
              title={t('apiKeys.empty')}
              description={t('apiKeys.emptyHint')}
              action={
                !isAdmin ? (
                  <Button icon={Plus} onClick={() => setIssuing(true)}>
                    {t('apiKeys.newKey')}
                  </Button>
                ) : null
              }
            />
          ) : filtered.length === 0 ? (
            <EmptyState icon={Search} title={t('apiKeys.noMatch')} />
          ) : (
            <div className="space-y-2">
              {filtered.map((k) => (
                <ApiKeyRow
                  key={k.id}
                  item={k}
                  isAdmin={isAdmin}
                  onRevoke={() => setRevokeId(k.id)}
                  onRotate={() => onRotate(k.id)}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {issuing && !isAdmin ? (
        <IssueDialog
          onClose={() => setIssuing(false)}
          onIssued={async (result) => {
            setIssuing(false);
            setCreatedSecret(result?.api_key || result?.secret || null);
            await load();
          }}
        />
      ) : null}

      {createdSecret ? (
        <SecretRevealDialog secret={createdSecret} onClose={() => setCreatedSecret(null)} />
      ) : null}

      {revokeId ? (
        <Dialog
          open
          onClose={() => setRevokeId(null)}
          size="sm"
          title={t('apiKeys.confirmRevokeTitle')}
          footer={
            <>
              <Button variant="secondary" onClick={() => setRevokeId(null)}>
                {t('common.cancel')}
              </Button>
              <Button variant="danger" icon={Trash2} onClick={onRevoke}>
                {t('apiKeys.revoke')}
              </Button>
            </>
          }
        >
          <div className="flex items-start gap-3 text-[12.5px] text-ink-700 dark:text-ink-300">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
            <p>{t('apiKeys.confirmRevokeBody')}</p>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function ApiKeyRow({ item, isAdmin, onRevoke, onRotate }) {
  const { t } = useTranslation();
  const [show, setShow] = useState(false);
  const masked = item.key_mask || `${item.key_prefix || ''}${'•'.repeat(20)}`;
  const isActive = item.is_active !== false && item.status !== 'revoked';
  const canRotate = !isAdmin; // admin cannot rotate

  const copy = async () => {
    await copyToClipboard(masked);
    toast.success(t('common.copied'));
  };

  return (
    <div className="card p-4 transition-all hover:shadow-soft">
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-md bg-gradient-to-br from-indigo-600 to-violet-600 text-white">
          <KeyRound size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13.5px] font-semibold text-ink-900 dark:text-ink-100 dark:text-ink-100">
              {item.name || t('apiKeys.unnamed')}
            </span>
            <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
              {item.key_prefix || 'sk-***'}
            </code>
            {isAdmin && item.username ? (
              <span className="inline-flex items-center gap-1 rounded-md bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 text-[10.5px] font-medium text-ink-700 dark:text-ink-300">
                <UsersIcon size={10} />
                {item.username}
              </span>
            ) : null}
            <Badge variant={isActive ? 'success' : 'danger'} dot>
              {isActive ? t('common.enabled') : t('common.disabled')}
            </Badge>
            {item.expires_at ? (
              <span className="inline-flex items-center gap-1 text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                <Calendar size={10} />
                {t('apiKeys.expiresAt')}: {formatDate(item.expires_at)}
              </span>
            ) : null}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
            {item.monthly_token_limit ? (
              <span>
                {t('apiKeys.monthlyTokens')}: {formatNumber(item.monthly_token_limit)}
              </span>
            ) : null}
            {item.monthly_credit_limit ? (
              <span>
                {t('apiKeys.monthlyCredits')}: {formatNumber(item.monthly_credit_limit)}
              </span>
            ) : null}
            {item.last_used_at ? (
              <span>{t('apiKeys.lastUsed')}: {formatDate(item.last_used_at)}</span>
            ) : (
              <span>{t('apiKeys.created')}: {formatDate(item.created_at)}</span>
            )}
          </div>
          {!isAdmin ? (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-dashed border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/40 dark:bg-ink-900/40 p-2">
              <code className="flex-1 truncate font-mono text-[11.5px] text-ink-700 dark:text-ink-300">
                {show ? item.key_full || masked : masked}
              </code>
              <button
                type="button"
                onClick={() => setShow((v) => !v)}
                className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:text-ink-300 dark:hover:text-ink-300"
                title={show ? t('apiKeys.hide') : t('apiKeys.show')}
              >
                {show ? <EyeOff size={12} /> : <Eye size={12} />}
              </button>
              <button
                type="button"
                onClick={copy}
                className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:text-ink-300 dark:hover:text-ink-300"
                title={t('common.copy')}
              >
                <Copy size={12} />
              </button>
            </div>
          ) : null}
        </div>
        <div className="flex items-center gap-1.5">
          {canRotate ? (
            <Button size="sm" variant="secondary" icon={RefreshCw} onClick={onRotate}>
              {t('apiKeys.rotate')}
            </Button>
          ) : null}
          <Button size="sm" variant="danger" icon={Trash2} onClick={onRevoke}>
            {t('apiKeys.revoke')}
          </Button>
        </div>
      </div>
    </div>
  );
}

function IssueDialog({ onClose, onIssued }) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [monthlyTokens, setMonthlyTokens] = useState('');
  const [monthlyCredits, setMonthlyCredits] = useState('');
  const [allowedModels, setAllowedModels] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!name.trim()) {
      toast.error(t('apiKeys.nameRequired'));
      return;
    }
    setSubmitting(true);
    try {
      const body = { name: name.trim() };
      if (monthlyTokens) body.monthly_token_limit = Number(monthlyTokens);
      if (monthlyCredits) body.monthly_credit_limit = Number(monthlyCredits);
      if (allowedModels.trim()) {
        body.allowed_models = allowedModels
          .split(',')
          .map((m) => m.trim())
          .filter(Boolean);
      }
      if (expiresAt) body.expires_at = new Date(expiresAt).toISOString();
      const res = await api.createApiKey(body);
      onIssued?.(res);
    } catch (e) {
      toast.error(e.message || t('apiKeys.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('apiKeys.newKey')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={Plus}>
            {t('apiKeys.create')}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('apiKeys.keyName')} <span className="text-rose-500">*</span>
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('apiKeys.namePlaceholder')}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('apiKeys.monthlyTokens')}
          </label>
          <input
            type="number"
            min={0}
            value={monthlyTokens}
            onChange={(e) => setMonthlyTokens(e.target.value)}
            placeholder="e.g. 1000000"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('apiKeys.monthlyCredits')}
          </label>
          <input
            type="number"
            min={0}
            value={monthlyCredits}
            onChange={(e) => setMonthlyCredits(e.target.value)}
            placeholder="e.g. 1000"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('apiKeys.allowedModels')}
          </label>
          <input
            value={allowedModels}
            onChange={(e) => setAllowedModels(e.target.value)}
            placeholder="e.g. minimax/abab6.5s-chat, nvidia/meta/llama-3.1-70b-instruct"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('apiKeys.expiresAt')}
          </label>
          <input
            type="date"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:ring-ink-100/10"
          />
        </div>
      </div>
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
      title={t('apiKeys.secretTitle')}
      size="sm"
      footer={
        <Button icon={copied ? ShieldCheck : Copy} onClick={copy}>
          {copied ? t('common.copied') : t('common.copy')}
        </Button>
      }
    >
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 dark:bg-amber-900/20 p-3 text-[12px] text-amber-700 dark:text-amber-400">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{t('apiKeys.secretHint')}</span>
        </div>
        <div className="flex items-center gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/50 dark:bg-ink-900/50 p-2.5">
          <code className="flex-1 break-all font-mono text-[12px] text-ink-800">{secret}</code>
          <button
            type="button"
            onClick={copy}
            className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:text-ink-300 dark:hover:text-ink-300"
            title={t('common.copy')}
          >
            <X size={12} className="rotate-45" />
          </button>
        </div>
      </div>
    </Dialog>
  );
}

/**
 * IntegrationGuide — collapsible "how to call the API" panel.
 *
 * The platform exposes an OpenAI-compatible endpoint at
 * ``/v1/chat/completions`` and ``/v1/models`` (see
 * ``backend.routes.openai_compat``). The user can copy any of the
 * three ready-to-paste snippets and run it against their own API
 * Key without leaving the dashboard. The base URL is derived from
 * the current origin so it works in dev, staging, and prod without
 * hardcoding.
 */
function IntegrationGuide() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState('curl');
  const [sample, setSample] = useState('sk-...');
  const [copied, setCopied] = useState(false);

  const base = typeof window !== 'undefined' ? window.location.origin : 'https://your-host';

  const codes = useMemo(() => {
    const sk = sample || 'sk-...';
    const commonBody = JSON.stringify(
      {
        model: 'nvidia/meta/llama-3.1-70b-instruct',
        messages: [
          { role: 'system', content: 'You are a helpful assistant.' },
          { role: 'user', content: 'Introduce yourself in one sentence.' },
        ],
        temperature: 0.7,
      },
      null,
      2,
    );
    return {
      curl: `# 1. List available models
curl -s "${base}/v1/models" \\
  -H "Authorization: Bearer ${sk}" | head -c 800

# 2. Start a conversation
curl -s "${base}/v1/chat/completions" \\
  -H "Authorization: Bearer ${sk}" \\
  -H "Content-Type: application/json" \\
  -d '${commonBody}'`,
      python: `# pip install openai>=1.0
from openai import OpenAI

client = OpenAI(
    api_key="${sk}",          # Your API Key
    base_url="${base}/v1",    # Platform gateway
)

resp = client.chat.completions.create(
    model="nvidia/meta/llama-3.1-70b-instruct",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Introduce yourself in one sentence."},
    ],
    temperature=0.7,
)
print(resp.choices[0].message.content)`,
      node: `// npm i openai
import OpenAI from 'openai'

const client = new OpenAI({
  apiKey: '${sk}',         // Your API Key
  baseURL: '${base}/v1',   // Platform gateway
})

const resp = await client.chat.completions.create({
  model: 'nvidia/meta/llama-3.1-70b-instruct',
  messages: [
    { role: 'system', content: 'You are a helpful assistant.' },
    { role: 'user', content: 'Introduce yourself in one sentence.' },
  ],
  temperature: 0.7,
})
console.log(resp.choices[0].message.content)`,
    };
  }, [sample, base]);

  const copy = async () => {
    await copyToClipboard(codes[tab]);
    setCopied(true);
    toast.success(t('apiKeys.copiedToClipboard'));
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="card overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-4 text-left transition-colors hover:bg-ink-50 dark:hover:bg-ink-800 dark:bg-ink-900"
      >
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-gradient-to-br from-indigo-500 to-violet-600 text-white">
          <Code2 size={16} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold text-ink-900 dark:text-ink-100 dark:text-ink-100">{t('apiKeys.guide.title')}</div>
          <div className="mt-0.5 text-[12.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
            {t('apiKeys.guide.subtitlePrefix')}{' '}
            <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 text-[11.5px]">{base}/v1</code> ·{' '}
            {t('apiKeys.guide.subtitleSuffix')}
          </div>
        </div>
        {open ? (
          <ChevronDown size={18} className="text-ink-400 dark:text-ink-500" />
        ) : (
          <ChevronRight size={18} className="text-ink-400 dark:text-ink-500" />
        )}
      </button>

      {open && (
        <div className="border-t border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/40 dark:bg-ink-900/40 p-4">
          <ol className="mb-4 grid grid-cols-1 gap-2 text-[12.5px] text-ink-700 dark:text-ink-300 md:grid-cols-3">
            <li className="flex items-start gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-2.5">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-ink-900 text-[11px] font-semibold text-white dark:bg-ink-100 dark:bg-ink-800 dark:text-ink-900 dark:text-ink-100 dark:text-ink-100">
                1
              </span>
              <span>{t('apiKeys.guide.step1')}</span>
            </li>
            <li className="flex items-start gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-2.5">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-ink-900 text-[11px] font-semibold text-white dark:bg-ink-100 dark:bg-ink-800 dark:text-ink-900 dark:text-ink-100 dark:text-ink-100">
                2
              </span>
              <span>{t('apiKeys.guide.step2')}</span>
            </li>
            <li className="flex items-start gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-2.5">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-ink-900 text-[11px] font-semibold text-white dark:bg-ink-100 dark:bg-ink-800 dark:text-ink-900 dark:text-ink-100 dark:text-ink-100">
                3
              </span>
              <span>{t('apiKeys.guide.step3')}</span>
            </li>
          </ol>

          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
              {[
                { id: 'curl', label: 'cURL', icon: Terminal },
                { id: 'python', label: 'Python', icon: Code2 },
                { id: 'node', label: 'Node.js', icon: Code2 },
              ].map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.id}
                    onClick={() => setTab(item.id)}
                    className={
                      'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors ' +
                      (tab === item.id ? 'bg-ink-900 text-white dark:bg-ink-100 dark:bg-ink-800 dark:text-ink-900 dark:text-ink-100 dark:text-ink-100' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800 dark:bg-ink-900')
                    }
                  >
                    <Icon size={12} />
                    {item.label}
                  </button>
                );
              })}
            </div>
            <div className="ml-auto flex items-center gap-2">
              <label className="text-[12px] text-ink-500 dark:text-ink-400 dark:text-ink-500">{t('apiKeys.guide.demoKey')}</label>
              <input
                value={sample}
                onChange={(e) => setSample(e.target.value)}
                placeholder="sk-..."
                className="h-8 w-48 rounded border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100"
              />
            </div>
          </div>

          <div className="relative overflow-hidden rounded-lg border border-ink-900/20 dark:border-ink-100/20 bg-ink-950">
            <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed text-ink-100 dark:text-ink-100">
              <code>{codes[tab]}</code>
            </pre>
            <button
              type="button"
              onClick={copy}
              className="absolute right-2 top-2 flex items-center gap-1 rounded-md border border-ink-100/10 dark:border-ink-800/10 bg-ink-100 dark:bg-ink-800/10 dark:bg-ink-800/10 px-2 py-1 text-[11px] font-medium text-ink-100 dark:text-ink-100 transition-colors hover:bg-ink-100 dark:bg-ink-800/20"
            >
              {copied ? <Check size={11} /> : <Copy size={11} />}
              {copied ? t('apiKeys.guide.copiedLabel') : t('apiKeys.guide.copyLabel')}
            </button>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-2 text-[12px] text-ink-500 dark:text-ink-400 dark:text-ink-500 md:grid-cols-3">
            <div className="rounded border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
              <span className="text-ink-700 dark:text-ink-300 font-semibold">GET</span>{' '}
              <code className="font-mono">/v1/models</code>
              <span className="ml-1 text-ink-400 dark:text-ink-500">{t('apiKeys.guide.endpointModels')}</span>
            </div>
            <div className="rounded border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
              <span className="text-ink-700 dark:text-ink-300 font-semibold">POST</span>{' '}
              <code className="font-mono">/v1/chat/completions</code>
              <span className="ml-1 text-ink-400 dark:text-ink-500">{t('apiKeys.guide.endpointChat')}</span>
            </div>
            <div className="rounded border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
              <span className="text-ink-700 dark:text-ink-300 font-semibold">GET</span>{' '}
              <code className="font-mono">/v1/usage</code>
              <span className="ml-1 text-ink-400 dark:text-ink-500">{t('apiKeys.guide.endpointUsage')}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
