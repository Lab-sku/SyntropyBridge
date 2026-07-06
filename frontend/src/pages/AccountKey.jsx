import { useEffect, useMemo, useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  KeyRound,
  Plus,
  Copy,
  Trash2,
  RefreshCw,
  Eye,
  EyeOff,
  ShieldCheck,
  AlertTriangle,
  Calendar,
  Package,
  Sparkles,
  Crown,
  ChevronRight,
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
 * Account → Plan & API Key page (user view).
 *
 * Layout: two tabbed cards on a single page.
 *  - "Plan" tab: current subscription summary + available plans
 *    (subscribe / upgrade via /user/orders flow handled by Wallet).
 *  - "Key" tab: API Key management (list / create / rotate / revoke).
 *
 * Note: the admin view of API keys lives on ``/admin/api-keys`` — this
 * page is purely end-user self-service.
 */
export default function AccountKey() {
  const { t } = useTranslation();
  const [tab, setTab] = useState('plan'); // 'plan' | 'key'

  return (
    <>
      <TopBar
        title={t('account.plan.title')}
        subtitle={t('account.plan.subtitle')}
        action={
          <Link
            to="/account"
            className="inline-flex items-center gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 py-1.5 text-[12.5px] font-medium text-ink-700 dark:text-ink-300 transition-colors hover:bg-ink-50 dark:hover:bg-ink-800"
          >
            <ArrowLeft size={13} />
            {t('account.plan.backToProfile')}
          </Link>
        }
      />

      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 via-ink-50/50 to-brand-50/30">
        <div className="mx-auto max-w-4xl space-y-5 p-4 md:p-6">
          {/* Tab switcher */}
          <div className="inline-flex rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-1 shadow-soft">
            <TabButton
              active={tab === 'plan'}
              onClick={() => setTab('plan')}
              icon={Package}
              label={t('account.plan.tabPlan')}
            />
            <TabButton
              active={tab === 'key'}
              onClick={() => setTab('key')}
              icon={KeyRound}
              label={t('account.plan.tabKey')}
            />
          </div>

          {tab === 'plan' ? <PlanSection /> : <KeySection />}
        </div>
      </div>
    </>
  );
}

function TabButton({ active, onClick, icon: Icon, label }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        'inline-flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-[13px] font-semibold transition-all ' +
        (active
          ? 'bg-ink-900 text-white shadow-sm dark:bg-ink-100 dark:text-ink-900'
          : 'text-ink-600 dark:text-ink-400 hover:text-ink-900 dark:hover:text-ink-100')
      }
    >
      <Icon size={14} strokeWidth={active ? 2.4 : 2} />
      {label}
    </button>
  );
}

// ===========================================================================
// Plan section
// ===========================================================================

function PlanSection() {
  const { t } = useTranslation();
  const [sub, setSub] = useState(null);
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Two endpoints: getMySubscription returns the latest subscription
      // row (history), getCurrentSubscription returns the active one
      // with plan joined. The active one is what the user actually
      // wants to see; fall back to the history if the active call
      // returns nothing.
      const [current, list] = await Promise.all([
        api.getCurrentSubscription().catch(() => ({ active: false, subscription: null })),
        api.getPlans().catch(() => []),
      ]);
      setSub(current || { active: false, subscription: null });
      setPlans(Array.isArray(list) ? list.filter((p) => p.is_active !== false) : []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-5">
      {loading ? (
        <CardSkeleton rows={2} />
      ) : (
        <CurrentPlanCard sub={sub} plans={plans} onChanged={load} />
      )}

      {loading ? (
        <CardSkeleton rows={3} />
      ) : plans.length === 0 ? (
        <div className="card p-5 text-center text-[12.5px] text-ink-500 dark:text-ink-400">
          {t('account.plan.noPlansAvailable')}
        </div>
      ) : (
        <div className="card p-5">
          <div className="mb-3 flex items-center gap-2">
            <Package size={14} className="text-ink-500 dark:text-ink-400" />
            <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
              {t('account.plan.availablePlans')}
            </h2>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {plans.map((p) => (
              <PlanChoice
                key={p.id || p.code}
                plan={p}
                currentId={sub?.subscription?.plan_id}
                onChanged={load}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function CurrentPlanCard({ sub, plans, onChanged }) {
  const { t } = useTranslation();
  const active = sub?.active && sub?.subscription;
  const s = sub?.subscription || null;

  // Compute days remaining + usage for the active subscription.
  const expiresAt = s?.expires_at ? new Date(s.expires_at) : null;
  const now = new Date();
  const daysLeft = expiresAt
    ? Math.max(0, Math.ceil((expiresAt.getTime() - now.getTime()) / 86400000))
    : null;

  const planCredits = s?.plan_credits || s?.monthly_credits || 0;
  const used = Number(s?.credits_used_this_period || 0);
  const usagePct = planCredits > 0 ? Math.min(100, Math.round((used / planCredits) * 100)) : 0;

  const planName = s?.plan_name || t('account.plan.freePlan');

  return (
    <div className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft">
      <div className="mb-5 flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-amber-500 to-orange-600 text-white shadow-md">
          <Crown size={18} strokeWidth={2} />
        </div>
        <div>
          <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
            {t('account.plan.currentPlan')}
          </h2>
          <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
            {t('account.plan.subtitle')}
          </p>
        </div>
        {active ? (
          <Badge variant="success" dot className="ml-auto">
            {t('account.plan.active')}
          </Badge>
        ) : (
          <Badge variant="default" className="ml-auto">
            {t('account.plan.inactive')}
          </Badge>
        )}
      </div>

      {active ? (
        <div className="space-y-4">
          {/* Plan name + price */}
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="text-[20px] font-bold text-ink-900 dark:text-ink-100">
                {planName}
              </div>
              {s?.plan_code ? (
                <code className="mt-0.5 inline-block rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 text-[10.5px] text-ink-600 dark:text-ink-400">
                  {s.plan_code}
                </code>
              ) : null}
            </div>
            {Number.isFinite(Number(s?.plan_price)) && s.plan_price > 0 ? (
              <div className="text-right">
                <div className="font-mono text-[18px] font-bold text-ink-900 dark:text-ink-100">
                  {formatNumber(s.plan_price)}
                </div>
                <div className="text-[11px] text-ink-500 dark:text-ink-400">
                  {t('account.plan.perMonth')}
                </div>
              </div>
            ) : null}
          </div>

          {/* Usage bar */}
          {planCredits > 0 ? (
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[11.5px] font-medium text-ink-600 dark:text-ink-400">
                  {t('account.plan.creditsUsed')}
                </span>
                <span className="font-mono text-[12px] font-semibold text-ink-700 dark:text-ink-300">
                  {formatNumber(used)} / {formatNumber(planCredits)}
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                <div
                  className={
                    'h-full transition-all duration-300 ' +
                    (usagePct >= 90
                      ? 'bg-rose-500'
                      : usagePct >= 70
                      ? 'bg-amber-500'
                      : 'bg-emerald-500')
                  }
                  style={{ width: `${usagePct}%` }}
                />
              </div>
            </div>
          ) : null}

          {/* Meta grid */}
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <MetaCell
              label={t('account.plan.expiresAt')}
              value={expiresAt ? formatDate(s.expires_at) : '—'}
            />
            <MetaCell
              label={t('account.plan.daysLeft')}
              value={daysLeft != null ? t('account.plan.daysLeftValue', { n: daysLeft }) : '—'}
            />
            <MetaCell
              label={t('account.plan.autoRenew')}
              value={s?.auto_renew ? t('common.enabled') : t('common.disabled')}
            />
          </dl>

          {/* Pending downgrade banner */}
          {s?.pending_plan_name ? (
            <div className="rounded-xl border border-amber-200 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-400">
              <Sparkles size={12} className="mr-1 inline-block" />
              {t('account.plan.pendingDowngrade', { name: s.pending_plan_name })}
            </div>
          ) : null}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-ink-200 dark:border-ink-700 bg-white/60 dark:bg-ink-900/40 p-4 text-center">
          <div className="text-[12.5px] text-ink-600 dark:text-ink-400">
            {t('account.plan.noActiveDesc')}
          </div>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
          {t('account.plan.planHint')}
        </p>
        <Link
          to="/wallet"
          className="inline-flex items-center gap-1.5 text-[12.5px] font-semibold text-brand-600 dark:text-brand-400 hover:underline"
        >
          {t('account.plan.viewWallet')}
          <ChevronRight size={13} />
        </Link>
      </div>
    </div>
  );
}

function MetaCell({ label, value }) {
  return (
    <div className="rounded-xl border border-ink-200/60 dark:border-ink-700/60 bg-white/60 dark:bg-ink-900/60 p-3">
      <dt className="text-[11px] font-medium uppercase tracking-wider text-ink-400 dark:text-ink-500">
        {label}
      </dt>
      <dd className="mt-1 text-[13px] text-ink-800 dark:text-ink-200">{value}</dd>
    </div>
  );
}

function PlanChoice({ plan, currentId, onChanged }) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState(false);
  const price = plan.price ?? plan.monthly_price ?? 0;
  const credits = plan.monthly_credits ?? plan.credits ?? 0;
  const discount = plan.discount ?? plan.discount_rate ?? 0;
  const isCurrent = currentId && plan.id === currentId;

  const handleSubscribe = async () => {
    setSubmitting(true);
    try {
      // The subscribe endpoint creates a pending order; the user
      // gets redirected to /wallet to complete payment / top up.
      await api.subscribePlan({ plan_id: plan.id });
      toast.success(t('account.plan.subscribeSuccess'));
      onChanged?.();
    } catch (e) {
      toast.error(e?.message || t('account.plan.subscribeFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className={
        'relative rounded-xl border p-4 transition-all ' +
        (isCurrent
          ? 'border-ink-900 bg-ink-50/50 shadow-soft dark:border-ink-100 dark:bg-ink-900/50'
          : plan.popular
          ? 'border-ink-900 bg-ink-50/50 shadow-soft dark:border-ink-100 dark:bg-ink-900/50'
          : 'border-ink-200 bg-white hover:shadow-soft dark:border-ink-700 dark:bg-ink-900')
      }
    >
      {plan.popular || plan.recommended ? (
        <Badge variant="accent" className="absolute right-3 top-3">
          <Sparkles size={10} />
          {t('account.plan.popular')}
        </Badge>
      ) : null}
      {isCurrent ? (
        <Badge variant="success" dot className="absolute right-3 top-3">
          {t('account.plan.current')}
        </Badge>
      ) : null}
      <div className="text-[12.5px] font-semibold text-ink-700 dark:text-ink-300">
        {plan.name || plan.code}
      </div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span className="font-mono text-[22px] font-bold text-ink-900 dark:text-ink-100">
          {formatNumber(price)}
        </span>
        <span className="text-[11.5px] text-ink-500 dark:text-ink-400">
          {t('account.plan.perMonth')}
        </span>
      </div>
      <ul className="mt-3 space-y-1.5 text-[12px] text-ink-600 dark:text-ink-400">
        <li className="flex items-center gap-1.5">
          <Check size={11} className="text-emerald-500" />
          {formatNumber(credits)} {t('common.currency')} {t('account.plan.perMonth')}
        </li>
        {discount ? (
          <li className="flex items-center gap-1.5">
            <Check size={11} className="text-emerald-500" />
            {t('account.plan.discount', { value: discount })}
          </li>
        ) : null}
        {plan.model_access ? (
          <li className="flex items-center gap-1.5">
            <Check size={11} className="text-emerald-500" />
            {t('account.plan.modelAccess', { value: plan.model_access })}
          </li>
        ) : null}
      </ul>
      <Button
        size="sm"
        className="mt-3 w-full"
        onClick={handleSubscribe}
        loading={submitting}
        disabled={isCurrent}
      >
        {isCurrent ? t('account.plan.current') : t('account.plan.subscribe')}
      </Button>
    </div>
  );
}

// ===========================================================================
// API Key section
// ===========================================================================

function KeySection() {
  const { t } = useTranslation();
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [issuing, setIssuing] = useState(false);
  const [revokeId, setRevokeId] = useState(null);
  const [createdSecret, setCreatedSecret] = useState(null);
  const [query, setQuery] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listMyApiKeys().catch(() => []);
      setKeys(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onRevoke = async () => {
    if (!revokeId) return;
    try {
      await api.revokeApiKey(revokeId);
      toast.success(t('account.plan.key.revokeOk'));
      setRevokeId(null);
      load();
    } catch (e) {
      toast.error(e?.message || t('account.plan.key.revokeFailed'));
    }
  };

  const onRotate = async (id) => {
    const old = keys.find((k) => k.id === id);
    if (!old) return;
    try {
      const result = await api.createApiKey({
        name: `${old.name || 'key'} (rotated)`,
        monthly_token_limit: old.monthly_token_limit,
        monthly_credit_limit: old.monthly_credit_limit,
        allowed_models: Array.isArray(old.allowed_models) ? old.allowed_models : undefined,
        expires_at: old.expires_at,
      });
      setCreatedSecret(result?.api_key || result?.secret || null);
      toast.success(t('account.plan.key.rotateOk'));
      load();
    } catch (e) {
      toast.error(e?.message || t('account.plan.key.rotateFailed'));
    }
  };

  const filtered = useMemo(() => {
    if (!query) return keys;
    const q = query.toLowerCase();
    return keys.filter(
      (k) =>
        String(k.name || '').toLowerCase().includes(q) ||
        String(k.key_prefix || '').toLowerCase().includes(q),
    );
  }, [keys, query]);

  return (
    <div className="space-y-5">
      {/* Intro / tip card */}
      <div className="card p-4">
        <div className="flex items-start gap-3 text-[12px] text-ink-600 dark:text-ink-400">
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-emerald-600" />
          <p>{t('account.plan.key.intro')}</p>
        </div>
      </div>

      {/* Search + new key */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-1 items-center gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5 py-1.5">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('account.plan.key.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-ink-400 dark:text-ink-500"
          />
        </div>
        <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
          {t('common.refresh')}
        </Button>
        <Button size="sm" icon={Plus} onClick={() => setIssuing(true)}>
          {t('account.plan.key.newKey')}
        </Button>
      </div>

      {loading && keys.length === 0 ? (
        <CardSkeleton rows={3} />
      ) : keys.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          title={t('account.plan.key.empty')}
          description={t('account.plan.key.emptyHint')}
          action={
            <Button icon={Plus} onClick={() => setIssuing(true)}>
              {t('account.plan.key.newKey')}
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState icon={KeyRound} title={t('account.plan.key.noMatch')} />
      ) : (
        <div className="space-y-2">
          {filtered.map((k) => (
            <ApiKeyRow
              key={k.id}
              item={k}
              onRevoke={() => setRevokeId(k.id)}
              onRotate={() => onRotate(k.id)}
            />
          ))}
        </div>
      )}

      {issuing ? (
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
          title={t('account.plan.key.confirmRevokeTitle')}
          footer={
            <>
              <Button variant="secondary" onClick={() => setRevokeId(null)}>
                {t('common.cancel')}
              </Button>
              <Button variant="danger" icon={Trash2} onClick={onRevoke}>
                {t('account.plan.key.revoke')}
              </Button>
            </>
          }
        >
          <div className="flex items-start gap-3 text-[12.5px] text-ink-700 dark:text-ink-300">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
            <p>{t('account.plan.key.confirmRevokeBody')}</p>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

function ApiKeyRow({ item, onRevoke, onRotate }) {
  const { t } = useTranslation();
  const [show, setShow] = useState(false);
  const masked = item.key_mask || `${item.key_prefix || ''}${'•'.repeat(20)}`;
  const isActive = item.is_active !== false && item.status !== 'revoked';

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
            <span className="text-[13.5px] font-semibold text-ink-900 dark:text-ink-100">
              {item.name || t('account.plan.key.unnamed')}
            </span>
            <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
              {item.key_prefix || 'sk-***'}
            </code>
            <Badge variant={isActive ? 'success' : 'danger'} dot>
              {isActive ? t('common.enabled') : t('common.disabled')}
            </Badge>
            {item.expires_at ? (
              <span className="inline-flex items-center gap-1 text-[11.5px] text-ink-500 dark:text-ink-400">
                <Calendar size={10} />
                {t('account.plan.key.expiresAt')}: {formatDate(item.expires_at)}
              </span>
            ) : null}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-[11.5px] text-ink-500 dark:text-ink-400">
            {item.monthly_token_limit ? (
              <span>
                {t('account.plan.key.monthlyTokens')}: {formatNumber(item.monthly_token_limit)}
              </span>
            ) : null}
            {item.monthly_credit_limit ? (
              <span>
                {t('account.plan.key.monthlyCredits')}: {formatNumber(item.monthly_credit_limit)}
              </span>
            ) : null}
            <span>
              {t('account.plan.key.created')}: {formatDate(item.created_at)}
            </span>
          </div>
          <div className="mt-2 flex items-center gap-2 rounded-lg border border-dashed border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/40 p-2">
            <code className="flex-1 truncate font-mono text-[11.5px] text-ink-700 dark:text-ink-300">
              {show ? item.key_full || masked : masked}
            </code>
            <button
              type="button"
              onClick={() => setShow((v) => !v)}
              className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:hover:text-ink-300"
              title={show ? t('account.plan.key.hide') : t('account.plan.key.show')}
            >
              {show ? <EyeOff size={12} /> : <Eye size={12} />}
            </button>
            <button
              type="button"
              onClick={copy}
              className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:hover:text-ink-300"
              title={t('common.copy')}
            >
              <Copy size={12} />
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="secondary" icon={RefreshCw} onClick={onRotate}>
            {t('account.plan.key.rotate')}
          </Button>
          <Button size="sm" variant="danger" icon={Trash2} onClick={onRevoke}>
            {t('account.plan.key.revoke')}
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
      toast.error(t('account.plan.key.nameRequired'));
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
      toast.error(e?.message || t('account.plan.key.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('account.plan.key.newKey')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={Plus}>
            {t('account.plan.key.create')}
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('account.plan.key.keyName')} <span className="text-rose-500">*</span>
          </label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t('account.plan.key.namePlaceholder')}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('account.plan.key.monthlyTokens')}
          </label>
          <input
            type="number"
            min={0}
            value={monthlyTokens}
            onChange={(e) => setMonthlyTokens(e.target.value)}
            placeholder="e.g. 1000000"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
          />
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('account.plan.key.monthlyCredits')}
          </label>
          <input
            type="number"
            min={0}
            value={monthlyCredits}
            onChange={(e) => setMonthlyCredits(e.target.value)}
            placeholder="e.g. 1000"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('account.plan.key.allowedModels')}
          </label>
          <input
            value={allowedModels}
            onChange={(e) => setAllowedModels(e.target.value)}
            placeholder="e.g. minimax/abab6.5s-chat"
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
          />
        </div>
        <div className="md:col-span-2">
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('account.plan.key.expiresAt')}
          </label>
          <input
            type="date"
            value={expiresAt}
            onChange={(e) => setExpiresAt(e.target.value)}
            className="h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
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
      title={t('account.plan.key.secretTitle')}
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
          <span>{t('account.plan.key.secretHint')}</span>
        </div>
        <div className="flex items-center gap-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/50 p-2.5">
          <code className="flex-1 break-all font-mono text-[12px] text-ink-800 dark:text-ink-200">
            {secret}
          </code>
          <button
            type="button"
            onClick={copy}
            className="rounded p-1 text-ink-400 dark:text-ink-500 hover:text-ink-700 dark:hover:text-ink-300"
            title={t('common.copy')}
          >
            <Copy size={12} />
          </button>
        </div>
      </div>
    </Dialog>
  );
}
