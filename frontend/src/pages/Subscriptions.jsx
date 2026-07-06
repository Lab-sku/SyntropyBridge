import { useEffect, useMemo, useState, useCallback } from 'react';
import {
  Check,
  X,
  Inbox,
  Clock,
  CheckCircle2,
  XCircle,
  ArrowUpCircle,
  ArrowDownCircle,
  RefreshCw,
  Zap,
  ToggleLeft,
  ToggleRight,
  AlertTriangle,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatDate } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';

// STATUS_TABS moved inside component to use t()

function StatusBadge({ status }) {
  const { t } = useTranslation();
  if (status === 'approved')
    return (
      <Badge variant="success" dot>
        {t('subscriptions.badge.approved')}
      </Badge>
    );
  if (status === 'rejected')
    return (
      <Badge variant="danger" dot>
        {t('subscriptions.badge.rejected')}
      </Badge>
    );
  return (
    <Badge variant="warning" dot>
      {t('subscriptions.badge.pending')}
    </Badge>
  );
}

function ReviewDialog({ sub, action, onClose, onReviewed }) {
  const { t } = useTranslation();
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const isApprove = action === 'approved';
  const submit = async () => {
    setSaving(true);
    try {
      await api.reviewSubscription(sub.id, { status: action, admin_note: note });
      toast.success(
        isApprove ? t('subscriptions.status.approved') : t('subscriptions.status.rejected'),
      );
      onReviewed?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('subscriptions.toast.processFailed'));
    } finally {
      setSaving(false);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={
        isApprove ? t('subscriptions.review.approveTitle') : t('subscriptions.review.rejectTitle')
      }
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={submit}
            loading={saving}
            variant={isApprove ? 'primary' : 'danger'}
            icon={isApprove ? Check : X}
          >
            {isApprove
              ? t('subscriptions.review.confirmApprove')
              : t('subscriptions.review.confirmReject')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-3 text-[12.5px]">
          <div className="flex items-center justify-between">
            <span className="text-ink-500 dark:text-ink-400">{t('common.user')}</span>
            <span className="font-medium text-ink-900 dark:text-ink-100">{sub.username}</span>
          </div>
          <div className="mt-1 flex items-center justify-between">
            <span className="text-ink-500 dark:text-ink-400">{t('common.platform')}</span>
            <code className="font-mono text-[11.5px] text-ink-700 dark:text-ink-300">{sub.provider}</code>
          </div>
          {sub.model_id && (
            <div className="mt-1 flex items-center justify-between">
              <span className="text-ink-500 dark:text-ink-400">{t('common.model')}</span>
              <code className="font-mono text-[11.5px] text-ink-700 dark:text-ink-300">{sub.model_id}</code>
            </div>
          )}
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {isApprove
              ? t('subscriptions.review.noteOptional')
              : t('subscriptions.review.rejectReasonOptional')}
          </label>
          <textarea
            rows={3}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={
              isApprove
                ? t('subscriptions.review.notePlaceholder')
                : t('subscriptions.review.rejectReasonPlaceholder')
            }
            className="w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 py-2 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
          />
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Plan change dialog (upgrade / downgrade)
// ---------------------------------------------------------------------------

function PlanChangeDialog({ mode, subscription, plans, onClose, onDone }) {
  const { t } = useTranslation();
  const [selectedPlanId, setSelectedPlanId] = useState(null);
  const [loading, setLoading] = useState(false);
  const isUpgrade = mode === 'upgrade';

  const currentPrice = parseFloat(subscription?.plan_price || 0);
  const selectedPlan = plans.find((p) => p.id === selectedPlanId);
  const selectedPrice = selectedPlan ? parseFloat(selectedPlan.monthly_price || 0) : 0;

  // Filter plans: upgrade shows more expensive, downgrade shows cheaper
  const eligiblePlans = plans.filter((p) => {
    const price = parseFloat(p.monthly_price || 0);
    if (p.id === subscription?.plan_id) return false;
    return isUpgrade ? price > currentPrice : price < currentPrice;
  });

  const proratedRefund = currentPrice > 0 ? (currentPrice * 0.5).toFixed(2) : '0.00';
  const netCost = Math.max(0, selectedPrice - parseFloat(proratedRefund)).toFixed(2);

  const submit = async () => {
    if (!selectedPlanId) return;
    setLoading(true);
    try {
      const fn = isUpgrade ? api.upgradeSubscription : api.downgradeSubscription;
      await fn(subscription.id, { new_plan_id: selectedPlanId });
      toast.success(
        isUpgrade ? t('subscriptionMgmt.upgradeSuccess') : t('subscriptionMgmt.downgradeSuccess'),
      );
      onDone?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('subscriptionMgmt.actionFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={isUpgrade ? t('subscriptionMgmt.upgradeTitle') : t('subscriptionMgmt.downgradeTitle')}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={submit}
            loading={loading}
            disabled={!selectedPlanId}
            variant={isUpgrade ? 'primary' : 'warning'}
            icon={isUpgrade ? ArrowUpCircle : ArrowDownCircle}
          >
            {isUpgrade ? t('subscriptionMgmt.upgrade') : t('subscriptionMgmt.downgrade')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {!isUpgrade && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3 text-[12px] text-amber-800 dark:text-amber-300">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            {t('subscriptionMgmt.downgradeNote')}
          </div>
        )}
        <p className="text-[12px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.selectPlan')}</p>
        {eligiblePlans.length === 0 ? (
          <p className="text-[12px] text-ink-400 dark:text-ink-500 italic">{t('subscriptionMgmt.noActiveDesc')}</p>
        ) : (
          <div className="space-y-2">
            {eligiblePlans.map((p) => (
              <button
                key={p.id}
                onClick={() => setSelectedPlanId(p.id)}
                className={`w-full rounded-lg border p-3 text-left text-[12.5px] transition-all ${
                  selectedPlanId === p.id
                    ? 'border-ink-900 bg-ink-50 dark:bg-ink-800 ring-2 ring-ink-900/10'
                    : 'border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 hover:border-ink-300 dark:hover:border-ink-600'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-ink-900 dark:text-ink-100">{p.name}</span>
                  <span className="text-[12px] font-medium text-ink-700 dark:text-ink-300">
                    {p.monthly_price} {t('subscriptionMgmt.credits')}
                    {t('subscriptionMgmt.perMonth')}
                  </span>
                </div>
                {p.discount_rate && (
                  <div className="mt-0.5 text-[11px] text-ink-500 dark:text-ink-400">
                    {t('wallet.discount', { value: Math.round((1 - p.discount_rate) * 100) })}
                  </div>
                )}
              </button>
            ))}
          </div>
        )}
        {isUpgrade && selectedPlan && (
          <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-3 text-[12px]">
            <div className="flex justify-between">
              <span className="text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.proratedRefund')}</span>
              <span className="font-medium text-green-700 dark:text-green-400">-{proratedRefund}</span>
            </div>
            <div className="mt-1 flex justify-between">
              <span className="text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.newPrice')}</span>
              <span className="font-medium text-ink-900 dark:text-ink-100">{selectedPrice.toFixed(2)}</span>
            </div>
            <div className="mt-1 flex justify-between border-t border-ink-200 dark:border-ink-700 pt-1">
              <span className="font-medium text-ink-700 dark:text-ink-300">{t('subscriptionMgmt.netCost')}</span>
              <span className="font-semibold text-ink-900 dark:text-ink-100">{netCost}</span>
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// My Subscription card (user-facing lifecycle management)
// ---------------------------------------------------------------------------

function MySubscriptionCard({ sub, plans, onRefresh }) {
  const { t } = useTranslation();
  const [actionLoading, setActionLoading] = useState('');
  const [planDialog, setPlanDialog] = useState(null); // 'upgrade' | 'downgrade'
  const [cancelConfirm, setCancelConfirm] = useState(false);

  if (!sub) {
    return (
      <div className="card p-5">
        <h3 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{t('subscriptionMgmt.title')}</h3>
        <p className="mt-1 text-[12.5px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.noActive')}</p>
        <p className="mt-0.5 text-[11.5px] text-ink-400 dark:text-ink-500">{t('subscriptionMgmt.noActiveDesc')}</p>
      </div>
    );
  }

  const handleCancel = async () => {
    setCancelConfirm(false);
    setActionLoading('cancel');
    try {
      await api.cancelSubscription(sub.id);
      toast.success(t('subscriptionMgmt.cancelSuccess'));
      onRefresh?.();
    } catch (e) {
      toast.error(e.message || t('subscriptionMgmt.actionFailed'));
    } finally {
      setActionLoading(false);
    }
  };

  const handleRenew = async () => {
    setActionLoading('renew');
    try {
      await api.renewSubscription(sub.id);
      toast.success(t('subscriptionMgmt.renewSuccess'));
      onRefresh?.();
    } catch (e) {
      toast.error(e.message || t('subscriptionMgmt.actionFailed'));
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <>
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
              {t('subscriptionMgmt.title')}
            </h3>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-medium text-ink-800 dark:text-ink-200">
                {sub.plan_name || sub.plan_code}
              </span>
              <Badge variant={sub.auto_renew ? 'success' : 'default'}>
                {sub.auto_renew
                  ? t('subscriptionMgmt.autoRenewOn')
                  : t('subscriptionMgmt.autoRenewOff')}
              </Badge>
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button
              size="sm"
              variant="primary"
              icon={ArrowUpCircle}
              onClick={() => setPlanDialog('upgrade')}
            >
              {t('subscriptionMgmt.upgrade')}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              icon={ArrowDownCircle}
              onClick={() => setPlanDialog('downgrade')}
            >
              {t('subscriptionMgmt.downgrade')}
            </Button>
            {sub.auto_renew ? (
              <Button
                size="sm"
                variant="danger"
                icon={XCircle}
                loading={actionLoading === 'cancel'}
                onClick={() => setCancelConfirm(true)}
              >
                {t('subscriptionMgmt.cancel')}
              </Button>
            ) : (
              <Button
                size="sm"
                variant="secondary"
                icon={RefreshCw}
                loading={actionLoading === 'renew'}
                onClick={handleRenew}
              >
                {t('subscriptionMgmt.renew')}
              </Button>
            )}
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-2.5">
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.expiresAt')}</div>
            <div className="mt-0.5 text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {sub.expires_at ? formatDate(sub.expires_at) : '—'}
            </div>
          </div>
          <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-2.5">
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.creditsUsed')}</div>
            <div className="mt-0.5 text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {sub.credits_used_this_period ?? 0}
            </div>
          </div>
          <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-2.5">
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.autoRenew')}</div>
            <div className="mt-0.5 text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {sub.auto_renew
                ? t('subscriptionMgmt.autoRenewOn')
                : t('subscriptionMgmt.autoRenewOff')}
            </div>
          </div>
          <div className="rounded-lg border border-ink-200 dark:border-ink-700 bg-ink-50/40 dark:bg-ink-900/40 p-2.5">
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400">{t('wallet.currentPlan')}</div>
            <div className="mt-0.5 text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {sub.plan_price ?? 0} {t('subscriptionMgmt.credits')}
              {t('subscriptionMgmt.perMonth')}
            </div>
          </div>
        </div>

        {sub.pending_plan_id && (
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-2.5 text-[12px] text-amber-800 dark:text-amber-300">
            <AlertTriangle size={13} />
            {t('subscriptionMgmt.pendingDowngradeDesc', {
              name: sub.pending_plan_name || `Plan #${sub.pending_plan_id}`,
            })}
          </div>
        )}
      </div>

      {/* Plan change dialog */}
      {planDialog && (
        <PlanChangeDialog
          mode={planDialog}
          subscription={sub}
          plans={plans}
          onClose={() => setPlanDialog(null)}
          onDone={onRefresh}
        />
      )}

      {/* Cancel confirmation */}
      {cancelConfirm && (
        <Dialog
          open
          onClose={() => setCancelConfirm(false)}
          title={t('subscriptionMgmt.cancel')}
          size="sm"
          footer={
            <>
              <Button variant="secondary" onClick={() => setCancelConfirm(false)}>
                {t('common.cancel')}
              </Button>
              <Button
                onClick={handleCancel}
                loading={actionLoading === 'cancel'}
                variant="danger"
                icon={XCircle}
              >
                {t('common.confirm')}
              </Button>
            </>
          }
        >
          <p className="text-[12.5px] text-ink-600 dark:text-ink-400">{t('subscriptionMgmt.cancelConfirm')}</p>
        </Dialog>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Auto-recharge settings card
// ---------------------------------------------------------------------------

function AutoRechargeCard({ wallet, onRefresh }) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(false);
  const [threshold, setThreshold] = useState('');
  const [amount, setAmount] = useState('');
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (wallet && !loaded) {
      setEnabled(!!wallet.auto_recharge_enabled);
      setThreshold(String(wallet.auto_recharge_threshold || ''));
      setAmount(String(wallet.auto_recharge_amount || ''));
      setLoaded(true);
    }
  }, [wallet, loaded]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.updateAutoRecharge({
        enabled,
        threshold: threshold ? parseFloat(threshold) : null,
        amount: amount ? parseFloat(amount) : null,
      });
      toast.success(t('subscriptionMgmt.settingsSaved'));
      onRefresh?.();
    } catch (e) {
      toast.error(e.message || t('subscriptionMgmt.settingsFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card p-5">
      <div className="flex items-center gap-2">
        <Zap size={15} className="text-ink-700 dark:text-ink-300" />
        <h3 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
          {t('subscriptionMgmt.autoRecharge')}
        </h3>
      </div>
      <p className="mt-1 text-[11.5px] text-ink-500 dark:text-ink-400">{t('subscriptionMgmt.autoRechargeDesc')}</p>

      <div className="mt-3 space-y-3">
        <label className="flex cursor-pointer items-center gap-2">
          <button type="button" onClick={() => setEnabled(!enabled)} className="text-ink-700 dark:text-ink-300">
            {enabled ? <ToggleRight size={22} /> : <ToggleLeft size={22} />}
          </button>
          <span className="text-[12.5px] text-ink-700 dark:text-ink-300">
            {t('subscriptionMgmt.autoRechargeEnabled')}
          </span>
        </label>

        {enabled && (
          <>
            <div>
              <label className="mb-1 block text-[11.5px] font-medium text-ink-600 dark:text-ink-400">
                {t('subscriptionMgmt.autoRechargeThreshold')}
              </label>
              <input
                type="number"
                min="0"
                step="1"
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
                placeholder="100"
                className="w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 py-2 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
              />
              <p className="mt-0.5 text-[10.5px] text-ink-400 dark:text-ink-500">
                {t('subscriptionMgmt.autoRechargeThresholdHint')}
              </p>
            </div>
            <div>
              <label className="mb-1 block text-[11.5px] font-medium text-ink-600 dark:text-ink-400">
                {t('subscriptionMgmt.autoRechargeAmount')}
              </label>
              <input
                type="number"
                min="0"
                step="1"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="500"
                className="w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 py-2 text-[12.5px] outline-none focus:border-ink-900 focus:ring-2 focus:ring-ink-900/10"
              />
              <p className="mt-0.5 text-[10.5px] text-ink-400 dark:text-ink-500">
                {t('subscriptionMgmt.autoRechargeAmountHint')}
              </p>
            </div>
            <Button size="sm" variant="primary" loading={saving} onClick={handleSave}>
              {t('subscriptionMgmt.saveSettings')}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function Subscriptions() {
  const { t } = useTranslation();
  const role = useAuthStore((s) => s.role);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState('pending');
  const [reviewing, setReviewing] = useState(null);

  // Subscription lifecycle management state
  const [mySub, setMySub] = useState(null);
  const [mySubLoading, setMySubLoading] = useState(true);
  const [wallet, setWallet] = useState(null);
  const [plans, setPlans] = useState([]);

  const loadMySubscription = useCallback(async () => {
    setMySubLoading(true);
    try {
      const res = await api.getCurrentSubscription();
      setMySub(res?.active ? res.subscription : null);
    } catch {
      // User endpoints may not be accessible from admin session
      setMySub(null);
    } finally {
      setMySubLoading(false);
    }
  }, []);

  const loadWallet = useCallback(async () => {
    try {
      const w = await api.getWallet();
      setWallet(w);
    } catch {
      setWallet(null);
    }
  }, []);

  const loadPlans = useCallback(async () => {
    try {
      const p = await api.getPlans();
      setPlans(Array.isArray(p) ? p : []);
    } catch {
      setPlans([]);
    }
  }, []);

  useEffect(() => {
    loadMySubscription();
    loadWallet();
    loadPlans();
  }, [loadMySubscription, loadWallet, loadPlans]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getSubscriptions(tab || undefined);
      setItems(Array.isArray(data) ? data : []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, [tab]);

  const STATUS_TABS = useMemo(
    () => [
      { id: 'pending', label: t('subscriptions.status.pending'), icon: Clock, variant: 'warning' },
      {
        id: 'approved',
        label: t('subscriptions.status.approved'),
        icon: CheckCircle2,
        variant: 'success',
      },
      {
        id: 'rejected',
        label: t('subscriptions.status.rejected'),
        icon: XCircle,
        variant: 'danger',
      },
      { id: '', label: t('common.all'), icon: Inbox, variant: 'default' },
    ],
    [t],
  );

  const counts = useMemo(() => STATUS_TABS.map((st) => ({ ...st })), [STATUS_TABS]);

  const handleSubRefresh = () => {
    loadMySubscription();
    loadWallet();
  };

  return (
    <>
      <TopBar
        title={t('subscriptions.title')}
        subtitle={t('subscriptions.subtitle')}
        action={
          <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
            {STATUS_TABS.map((tabItem) => {
              const active = tab === tabItem.id;
              return (
                <button
                  key={tabItem.id}
                  onClick={() => setTab(tabItem.id)}
                  className={`flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-all ${
                    active ? 'bg-ink-900 dark:bg-ink-100 text-white dark:text-ink-900' : 'text-ink-600 dark:text-ink-400 hover:bg-ink-50 dark:hover:bg-ink-800'
                  }`}
                >
                  <tabItem.icon size={11} />
                  {tabItem.label}
                </button>
              );
            })}
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-5xl p-4 md:p-6">
          {/* Subscription lifecycle management cards (user-scoped —
              hidden for admin, who manages subs via the review table
              below; the write endpoints these cards call require a
              user API key and reject admin session cookies with 401). */}
          {role !== 'admin' && (
            <div className="mb-6 space-y-4">
              {!mySubLoading && (
                <MySubscriptionCard sub={mySub} plans={plans} onRefresh={handleSubRefresh} />
              )}
              {wallet && <AutoRechargeCard wallet={wallet} onRefresh={handleSubRefresh} />}
            </div>
          )}

          {/* Admin subscription review section */}
          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="card h-20 animate-pulse" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              icon={tab === 'pending' ? CheckCircle2 : Inbox}
              title={
                tab === 'pending'
                  ? t('subscriptions.empty.noPending')
                  : t('subscriptions.empty.noRelated')
              }
              description={
                tab === 'pending'
                  ? t('subscriptions.empty.noSubmissions')
                  : t('subscriptions.empty.switchStatus')
              }
            />
          ) : (
            <div className="space-y-2">
              {items.map((s) => (
                <div key={s.id} className="card p-4 transition-all hover:shadow-soft">
                  <div className="flex flex-wrap items-center gap-4">
                    <div className="flex h-9 w-9 items-center justify-center rounded-md bg-gradient-to-br from-ink-700 to-ink-900 text-[12px] font-semibold text-white">
                      {s.username?.charAt(0).toUpperCase() || '?'}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">{s.username}</span>
                        <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
                          {s.provider}
                        </code>
                        {s.model_id && (
                          <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
                            {s.model_id}
                          </code>
                        )}
                        <StatusBadge status={s.status} />
                      </div>
                      <div className="mt-1 text-[11.5px] text-ink-500 dark:text-ink-400">
                        {t('subscriptions.quota')} {s.requested_quota_5h ?? '—'} {t('subscriptions.quota5h')} ·{' '}
                        {s.requested_quota_week ?? '—'} {t('subscriptions.quotaWeek')}
                        {s.note ? ` · ${s.note}` : ''}
                      </div>
                      {s.admin_note && (
                        <div className="mt-1 text-[11px] text-ink-500 dark:text-ink-400">
                          <span className="font-medium text-ink-700 dark:text-ink-300">
                            {t('subscriptions.adminNote')}
                          </span>{' '}
                          {s.admin_note}
                        </div>
                      )}
                      <div className="mt-1 text-[10.5px] text-ink-400 dark:text-ink-500">
                        {formatDate(s.created_at)}
                      </div>
                    </div>
                    {s.status === 'pending' && (
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          variant="success"
                          icon={Check}
                          onClick={() => setReviewing({ sub: s, action: 'approved' })}
                        >
                          {t('subscriptions.approve')}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          icon={X}
                          onClick={() => setReviewing({ sub: s, action: 'rejected' })}
                        >
                          {t('subscriptions.reject')}
                        </Button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {reviewing && (
        <ReviewDialog
          sub={reviewing.sub}
          action={reviewing.action}
          onClose={() => setReviewing(null)}
          onReviewed={load}
        />
      )}
    </>
  );
}
