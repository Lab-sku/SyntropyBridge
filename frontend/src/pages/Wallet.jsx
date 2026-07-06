import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Wallet as WalletIcon,
  Plus,
  Ticket,
  Copy,
  RefreshCw,
  History,
  Sparkles,
  Shield,
  ChevronRight,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatDate, formatNumber } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';
import PaymentDialog from '@/components/PaymentDialog';

const TOPUP_PRESETS = [
  { credits: 1000, currency: 10 },
  { credits: 4500, currency: 45, popular: true },
  { credits: 8000, currency: 80 },
  { credits: 50000, currency: 350 },
];

/**
 * User-facing wallet page.
 *
 *   - Wallet balance card (current credits, refresh action)
 *   - Top-up flow (preset amounts + custom) → POST /user/orders
 *   - Redeem code modal → POST /user/redeem
 *   - Available plans (read-only) → GET /user/plans
 *   - Transaction history → GET /user/wallet/transactions
 *
 * All copy is pulled from i18n so the same component renders in both
 * Chinese and English without duplication.
 */
export default function Wallet() {
  const { t } = useTranslation();
  const role = useAuthStore((s) => s.role);
  const [wallet, setWallet] = useState(null);
  const [tx, setTx] = useState([]);
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);
  const [topUpOpen, setTopUpOpen] = useState(false);
  const [redeemOpen, setRedeemOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [paymentOrder, setPaymentOrder] = useState(null);
  const [paymentSuccess, setPaymentSuccess] = useState(false);
  // 订单正在轮询中（Stripe 返回后等待 webhook），用于展示"重新检查"按钮
  const [pendingOrderNo, setPendingOrderNo] = useState(null);
  const [pollingTimedOut, setPollingTimedOut] = useState(false);

  // Detect return from Stripe checkout (?status=success&order=ORDxxx)
  const pollTimerRef = useRef(null);
  const pollOrderStatus = async (orderNo, { maxAttempts = 60, intervalMs = 3000 } = {}) => {
    if (!orderNo) return;
    setPendingOrderNo(orderNo);
    setPollingTimedOut(false);
    setPaymentSuccess(false);
    let attempts = 0;
    const poll = async () => {
      try {
        const res = await api.queryOrderPayment(orderNo);
        if (res?.status === 'paid') {
          setPaymentSuccess(true);
          setPendingOrderNo(null);
          setPollingTimedOut(false);
          toast.success(t('payment.success'));
          await load();
          // Clean URL
          window.history.replaceState({}, '', '/wallet');
          return;
        }
      } catch {
        // ignore polling errors
      }
      attempts++;
      if (attempts < maxAttempts) {
        pollTimerRef.current = setTimeout(poll, intervalMs);
      } else {
        // 超时 — 不静默清 URL，提示用户余额稍后更新
        setPollingTimedOut(true);
        toast.info(t('payment.pollTimeoutBanner'));
        // 仍然清理 URL（保留 pendingOrderNo 让"重新检查"按钮可见）
        window.history.replaceState({}, '', '/wallet');
      }
    };
    poll();
  };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const status = params.get('status');
    const orderNo = params.get('order');
    if (status === 'success' && orderNo) {
      // 60 次 × 3s = 3 分钟（与 PaymentDialog 对齐）
      pollOrderStatus(orderNo, { maxAttempts: 60, intervalMs: 3000 });
    } else if (status === 'cancelled' && orderNo) {
      toast.info(t('payment.cancelled'));
      window.history.replaceState({}, '', '/wallet');
    }
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const recheckPendingOrder = () => {
    if (pendingOrderNo) {
      pollOrderStatus(pendingOrderNo, { maxAttempts: 60, intervalMs: 3000 });
    }
  };

  const load = async () => {
    setLoading(true);
    try {
      const [w, transactions, list] = await Promise.all([
        api.getWallet().catch(() => null),
        api.getWalletTransactions(50).catch(() => []),
        api.getPlans().catch(() => []),
      ]);
      setWallet(w || { balance: 0, currency: 'credits' });
      setTx(Array.isArray(transactions) ? transactions : []);
      setPlans(Array.isArray(list) ? list : []);
      if (w) {
        try {
          localStorage.setItem(
            'mm:wallet:balance',
            JSON.stringify({ balance: w.balance, at: Date.now() }),
          );
        } catch {
          /* storage quota / private-mode */
        }
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // Silent periodic refresh (30s baseline) while the tab is visible.
  // Lets a user who keeps the Wallet page open see their balance drift
  // down as API calls land, without needing to click Refresh.
  //
  // Exponential backoff on failure: when the backend is unreachable
  // the polling interval doubles each failure (30s → 60s → 120s → …)
  // up to a 5 min cap, so a sustained backend outage doesn't keep
  // hammering the API. The first successful poll resets the cadence
  // back to 30s. Started once on mount with an empty dep array so the
  // timer isn't reset every time setWallet fires.
  useEffect(() => {
    let cancelled = false;
    let timerId = null;
    let failCount = 0;
    const BASE_MS = 30_000;
    const MAX_MS = 5 * 60_000; // 5 min

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        // Tab hidden — schedule the next tick at the baseline cadence
        // so we don't accumulate backoff just because the user switched
        // tabs. The visibility guard itself prevents actual fetches.
        timerId = setTimeout(tick, BASE_MS);
        return;
      }
      try {
        const w = await api.getWallet();
        if (!cancelled && w) {
          setWallet(w);
          try {
            localStorage.setItem(
              'mm:wallet:balance',
              JSON.stringify({ balance: w.balance, at: Date.now() }),
            );
          } catch {
            /* storage quota / private-mode */
          }
        }
        // Success — reset backoff.
        failCount = 0;
        timerId = setTimeout(tick, BASE_MS);
      } catch {
        // Failure — back off exponentially up to MAX_MS.
        failCount = Math.min(failCount + 1, 8);
        const delay = Math.min(BASE_MS * 2 ** failCount, MAX_MS);
        timerId = setTimeout(tick, delay);
      }
    };

    timerId = setTimeout(tick, BASE_MS);
    return () => {
      cancelled = true;
      if (timerId) clearTimeout(timerId);
    };
  }, []);

  // Cross-tab sync: when another tab writes to mm:wallet:balance, pull
  // fresh data here so two open tabs stay in lock-step after a top-up
  // or API-key charge in one of them. Debounced 300ms so a burst of
  // writes (e.g. rapid top-ups) collapses into a single refetch.
  useEffect(() => {
    let timer = null;
    const onStorage = (ev) => {
      if (ev.key !== 'mm:wallet:balance') return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        api
          .getWallet()
          .then((w) => w && setWallet(w))
          .catch(() => {});
      }, 300);
    };
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('storage', onStorage);
      if (timer) clearTimeout(timer);
    };
  }, []);

  const balance = wallet?.balance ?? 0;
  const plan = wallet?.plan_name || wallet?.plan || null;
  const currentPlanCode = wallet?.plan_code || null;
  const planExpiresAt = wallet?.plan_expires_at || null;

  return (
    <>
      <TopBar
        title={t('wallet.title')}
        subtitle={t('wallet.subtitle')}
        action={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" icon={RefreshCw} onClick={load} loading={loading}>
              {t('common.refresh')}
            </Button>
            {role !== 'admin' && (
              <Button size="sm" variant="secondary" icon={Ticket} onClick={() => setRedeemOpen(true)}>
                {t('wallet.redeem')}
              </Button>
            )}
            <Button size="sm" icon={Plus} onClick={() => setTopUpOpen(true)}>
              {t('wallet.topUp')}
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-6">
          {loading && !wallet ? (
            <CardSkeleton rows={3} />
          ) : (
            <>
              {/* ---------- Balance card ---------- */}
              <div className="card relative overflow-hidden p-5">
                <div
                  aria-hidden
                  className="pointer-events-none absolute -right-12 -top-12 h-48 w-48 rounded-full opacity-30 blur-2xl"
                  style={{ background: 'var(--gradient-accent)' }}
                />
                <div className="relative flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
                  <div>
                    <div className="mb-2 inline-flex items-center gap-2 text-[11.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
                      <WalletIcon size={13} />
                      {t('wallet.balance')}
                    </div>
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-[34px] font-bold text-ink-900 dark:text-ink-100">
                        {formatNumber(balance)}
                      </span>
                      <span className="text-[12.5px] text-ink-500 dark:text-ink-400">{t('common.currency')}</span>
                    </div>
                    {plan ? (
                      <div className="mt-2 flex items-center gap-1.5 text-[11.5px] text-ink-500 dark:text-ink-400">
                        <Shield size={11} />
                        {t('wallet.currentPlan')}:{' '}
                        <span className="font-medium text-ink-700 dark:text-ink-300">{plan}</span>
                      </div>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button variant="primary" icon={Plus} onClick={() => setTopUpOpen(true)}>
                      {t('wallet.topUp')}
                    </Button>
                    {role !== 'admin' && (
                      <Button variant="secondary" icon={Ticket} onClick={() => setRedeemOpen(true)}>
                        {t('wallet.redeem')}
                      </Button>
                    )}
                  </div>
                </div>
              </div>

              {/* ---------- Payment success banner ---------- */}
              {paymentSuccess ? (
                <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-[13px] text-emerald-800 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-400">
                  {t('payment.successBanner')}
                </div>
              ) : null}

              {/* ---------- Polling timeout banner ---------- */}
              {pollingTimedOut && pendingOrderNo ? (
                <div className="flex items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-[13px] text-amber-800 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-400">
                  <div className="flex-1">
                    <div className="font-medium">{t('payment.pollTimeoutTitle')}</div>
                    <div className="mt-0.5 text-[11.5px] text-amber-700 dark:text-amber-400/90">
                      {t('payment.pollTimeoutBanner')}
                    </div>
                  </div>
                  <Button size="sm" variant="secondary" icon={RefreshCw} onClick={recheckPendingOrder}>
                    {t('payment.recheck')}
                  </Button>
                </div>
              ) : null}

              {/* ---------- Plans (hidden for admin — managed via /admin/plans) ---------- */}
              {role !== 'admin' && plans.length > 0 ? (
                <div className="card p-5">
                  <div className="mb-3 flex items-center justify-between">
                    <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{t('wallet.plans')}</h2>
                    <span className="text-[11.5px] text-ink-500 dark:text-ink-400">{t('wallet.plansHint')}</span>
                  </div>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {plans.map((p) => (
                      <PlanCard
                        key={p.id || p.code}
                        plan={p}
                        onSubscribed={load}
                        walletBalance={balance}
                        currentPlanCode={currentPlanCode}
                        planExpiresAt={planExpiresAt}
                      />
                    ))}
                  </div>
                </div>
              ) : null}

              {/* ---------- Transactions ---------- */}
              <div className="card p-5">
                <div className="mb-3 flex items-center gap-2">
                  <History size={13} className="text-ink-500 dark:text-ink-400" />
                  <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('wallet.transactions')}
                  </h2>
                </div>
                {tx.length === 0 ? (
                  <EmptyState
                    icon={History}
                    title={t('wallet.emptyTx')}
                    description={t('wallet.emptyTxHint')}
                  />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[12.5px]">
                      <thead>
                        <tr className="border-b border-ink-200 dark:border-ink-700 text-left text-[11.5px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
                          <th className="px-2 py-2 font-medium">{t('wallet.colTime')}</th>
                          <th className="px-2 py-2 font-medium">{t('wallet.colType')}</th>
                          <th className="px-2 py-2 font-medium text-right">
                            {t('wallet.colAmount')}
                          </th>
                          <th className="px-2 py-2 font-medium">{t('wallet.colNote')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {tx.map((row) => (
                          <tr
                            key={row.id ?? `${row.created_at}-${row.amount}`}
                            className="border-b border-ink-100 dark:border-ink-800 last:border-0"
                          >
                            <td className="px-2 py-2 text-ink-700 dark:text-ink-300">{formatDate(row.created_at)}</td>
                            <td className="px-2 py-2">
                              <TxBadge kind={row.kind || row.type} t={t} />
                            </td>
                            <td
                              className={`px-2 py-2 text-right font-mono ${Number(row.amount) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}
                            >
                              {Number(row.amount) >= 0 ? '+' : ''}
                              {formatNumber(row.amount)}
                            </td>
                            <td className="px-2 py-2 text-ink-500 dark:text-ink-400">{row.note || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {topUpOpen ? (
        <TopUpDialog
          plans={plans}
          onClose={() => setTopUpOpen(false)}
          onSubmitted={async (order) => {
            setTopUpOpen(false);
            if (order?.order_no) {
              // Open the payment dialog for online payment
              setPaymentOrder(order);
            }
            await load();
          }}
        />
      ) : null}
      {paymentOrder ? (
        <PaymentDialog
          order={paymentOrder}
          onClose={() => setPaymentOrder(null)}
          onPaid={async () => {
            setPaymentOrder(null);
            setPaymentSuccess(true);
            await load();
          }}
        />
      ) : null}
      {redeemOpen ? (
        <RedeemDialog
          onClose={() => setRedeemOpen(false)}
          onRedeemed={async () => {
            setRedeemOpen(false);
            await load();
          }}
        />
      ) : null}
    </>
  );
}

function PlanCard({ plan, onSubscribed, walletBalance, currentPlanCode, planExpiresAt }) {
  const { t } = useTranslation();
  const [subscribing, setSubscribing] = useState(false);
  const [showPaymentDialog, setShowPaymentDialog] = useState(false);
  const price = plan.price ?? plan.monthly_price ?? 0;
  const credits = plan.monthly_credits ?? plan.credits ?? 0;
  const discount = plan.discount ?? 0;
  const isFree = price <= 0;
  const isCurrentPlan = currentPlanCode && plan.code === currentPlanCode;

  const handleSubscribe = async (paymentMethod) => {
    setSubscribing(true);
    try {
      const result = await api.subscribePlan({
        plan_id: plan.id,
        payment_method: paymentMethod,
      });
      if (result?.order) {
        toast.success(t('wallet.orderCreated', { no: result.order.order_no }));
      } else {
        toast.success(t('wallet.subscribeSuccess'));
      }
      onSubscribed?.();
    } catch (e) {
      toast.error(e.message || t('wallet.subscribeFailed'));
    } finally {
      setSubscribing(false);
      setShowPaymentDialog(false);
    }
  };

  // Free plan: subscribe directly
  const handleFreeSubscribe = () => handleSubscribe('free');

  // Paid plan: show payment method dialog
  const handlePaidSubscribe = () => setShowPaymentDialog(true);

  return (
    <>
      <div
        className={`relative rounded-xl border p-4 transition-all ${
          isCurrentPlan
            ? 'border-brand-500 bg-brand-50/40 ring-2 ring-brand-500/20 dark:border-brand-400 dark:bg-brand-900/10 dark:ring-brand-400/20'
            : plan.popular
              ? 'border-ink-900 bg-ink-50/50 shadow-soft dark:border-ink-100 dark:bg-ink-900/50'
              : 'border-ink-200 bg-white hover:shadow-soft dark:border-ink-700 dark:bg-ink-900'
        }`}
      >
        {/* Current plan badge */}
        {isCurrentPlan && (
          <div className="absolute right-3 top-3 inline-flex items-center gap-1 rounded-full bg-brand-500 px-2 py-0.5 text-[10px] font-semibold text-white">
            <Shield size={9} />
            {t('wallet.currentPlanBadge')}
          </div>
        )}
        {(!isCurrentPlan && (plan.popular || plan.recommended)) ? (
          <Badge variant="accent" className="absolute right-3 top-3">
            <Sparkles size={10} />
            {t('wallet.popular')}
          </Badge>
        ) : null}
        <div className={`text-[12.5px] font-semibold ${isCurrentPlan ? 'text-brand-700 dark:text-brand-300' : 'text-ink-700 dark:text-ink-300'}`}>{plan.name || plan.code}</div>
        <div className="mt-2 flex items-baseline gap-1.5">
          <span className={`font-mono text-[22px] font-bold ${isCurrentPlan ? 'text-brand-800 dark:text-brand-200' : 'text-ink-900 dark:text-ink-100'}`}>
            {isFree ? t('wallet.free') : formatNumber(price)}
          </span>
          {!isFree && <span className="text-[11.5px] text-ink-500 dark:text-ink-400">/ {t('wallet.month')}</span>}
        </div>
        {/* Show expiry date for current plan (free plan: monthly auto-renewal) */}
        {isCurrentPlan && plan.code === 'free' ? (
          <div
            className="mt-1 text-[10.5px] text-brand-600 dark:text-brand-400"
            title={t('wallet.freePlanRenewTooltip')}
          >
            {t('wallet.freePlanRenewLabel')}
          </div>
        ) : (
          isCurrentPlan && planExpiresAt && (
            <div className="mt-1 text-[10.5px] text-brand-600 dark:text-brand-400">
              {t('wallet.planExpiresAt', { date: formatDate(planExpiresAt) })}
            </div>
          )
        )}
        <ul className="mt-3 space-y-1.5 text-[12px] text-ink-600 dark:text-ink-400">
          <li className="flex items-center gap-1.5">
            <ChevronRight size={11} className="text-ink-400 dark:text-ink-500" />
            {formatNumber(credits)} {t('common.currency')} {t('wallet.perMonth')}
          </li>
          {discount ? (
            <li className="flex items-center gap-1.5">
              <ChevronRight size={11} className="text-ink-400 dark:text-ink-500" />
              {t('wallet.discount', { value: discount })}
            </li>
          ) : null}
          {plan.model_access ? (
            <li className="flex items-center gap-1.5">
              <ChevronRight size={11} className="text-ink-400 dark:text-ink-500" />
              {t('wallet.modelAccess', { value: plan.model_access })}
            </li>
          ) : null}
        </ul>
        {isCurrentPlan ? (
          <div className="mt-3 w-full rounded-lg bg-brand-500/10 py-2 text-center text-[12px] font-medium text-brand-700 dark:bg-brand-400/10 dark:text-brand-300">
            ✓ {t('wallet.currentPlanBadge')}
          </div>
        ) : (
          <Button
            size="sm"
            className="mt-3 w-full"
            onClick={isFree ? handleFreeSubscribe : handlePaidSubscribe}
            loading={subscribing}
          >
            {isFree ? t('wallet.activate') : t('wallet.subscribe')}
          </Button>
        )}
      </div>

      {showPaymentDialog && (
        <PlanPaymentDialog
          plan={plan}
          walletBalance={walletBalance}
          onClose={() => setShowPaymentDialog(false)}
          onSubmit={handleSubscribe}
          loading={subscribing}
        />
      )}
    </>
  );
}

function PlanPaymentDialog({ plan, walletBalance, onClose, onSubmit, loading }) {
  const { t } = useTranslation();
  const [method, setMethod] = useState('balance');
  const price = plan.monthly_price ?? plan.price ?? 0;
  const insufficientBalance = method === 'balance' && walletBalance < price;

  const handleSubmit = () => {
    if (method === 'balance' && insufficientBalance) {
      toast.error(t('wallet.insufficientBalance'));
      return;
    }
    onSubmit(method);
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('wallet.selectPaymentMethod')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSubmit} loading={loading} disabled={insufficientBalance}>
            {t('wallet.confirmSubscribe')}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="rounded-lg bg-ink-50 dark:bg-ink-800/50 p-3">
          <div className="text-[12px] text-ink-500 dark:text-ink-400">{t('wallet.plan')}</div>
          <div className="mt-1 text-[14px] font-semibold text-ink-900 dark:text-ink-100">
            {plan.name || plan.code}
          </div>
          <div className="mt-1 font-mono text-[18px] font-bold text-ink-900 dark:text-ink-100">
            {formatNumber(price)} {t('common.currency')}
            <span className="text-[11px] font-normal text-ink-500 dark:text-ink-400">/ {t('wallet.month')}</span>
          </div>
        </div>

        <div>
          <label className="mb-2 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('wallet.paymentMethod')}
          </label>
          <div className="space-y-2">
            {[
              { value: 'balance', label: t('wallet.method.balance'), desc: t('wallet.balance') + ': ' + formatNumber(walletBalance) + ' ' + t('common.currency') },
              { value: 'alipay', label: t('wallet.method.alipay'), desc: '' },
              { value: 'wechat', label: t('wallet.method.wechat'), desc: '' },
            ].map((m) => (
              <button
                key={m.value}
                type="button"
                onClick={() => setMethod(m.value)}
                className={`w-full rounded-lg border p-3 text-left transition-all ${
                  method === m.value
                    ? 'border-brand-500 bg-brand-50 dark:bg-brand-900/20'
                    : 'border-ink-200 bg-white hover:border-ink-300 dark:border-ink-700 dark:bg-ink-900 dark:hover:border-ink-600'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-medium text-ink-900 dark:text-ink-100">{m.label}</span>
                  <div
                    className={`h-4 w-4 rounded-full border-2 ${
                      method === m.value
                        ? 'border-brand-500 bg-brand-500'
                        : 'border-ink-300 dark:border-ink-600'
                    }`}
                  >
                    {method === m.value && (
                      <div className="h-full w-full rounded-full bg-white scale-50" />
                    )}
                  </div>
                </div>
                {m.desc && (
                  <div className={`mt-1 text-[11px] ${
                    m.value === 'balance' && insufficientBalance
                      ? 'text-rose-500'
                      : 'text-ink-500 dark:text-ink-400'
                  }`}>
                    {m.desc}
                    {m.value === 'balance' && insufficientBalance && ' (' + t('wallet.insufficient') + ')'}
                  </div>
                )}
              </button>
            ))}
          </div>
        </div>

        <p className="text-[11px] text-ink-500 dark:text-ink-400">
          {method === 'balance'
            ? t('wallet.balancePayHint')
            : t('wallet.onlinePayHint')}
        </p>
      </div>
    </Dialog>
  );
}

function TxBadge({ kind, t }) {
  const map = {
    topup: { variant: 'success', label: t('wallet.txTopup') },
    redeem: { variant: 'success', label: t('wallet.txRedeem') },
    usage: { variant: 'default', label: t('wallet.txUsage') },
    refund: { variant: 'accent', label: t('wallet.txRefund') },
    bonus: { variant: 'accent', label: t('wallet.txBonus') },
  };
  const meta = map[kind] || { variant: 'default', label: kind || t('wallet.txOther') };
  return (
    <Badge variant={meta.variant} dot>
      {meta.label}
    </Badge>
  );
}

function TopUpDialog({ plans, onClose, onSubmitted }) {
  const { t } = useTranslation();
  const [credits, setCredits] = useState(500);
  const [method, setMethod] = useState('manual');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!credits || credits <= 0) {
      toast.error(t('wallet.amountInvalid'));
      return;
    }
    setSubmitting(true);
    try {
      const order = await api.createTopUpOrder({ amount: credits, payment_method: method });
      if (order?.order_no) {
        toast.success(t('wallet.orderCreated', { no: order.order_no }));
        onSubmitted?.(order);
      } else {
        toast.success(t('wallet.topUpSubmitted'));
        onSubmitted?.(null);
      }
    } catch (e) {
      toast.error(e.message || t('wallet.topUpFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('wallet.topUp')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={Plus}>
            {t('wallet.confirmTopUp')}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('wallet.amount')}
          </label>
          <div className="grid grid-cols-4 gap-2">
            {TOPUP_PRESETS.map((p) => (
              <button
                key={p.credits}
                type="button"
                onClick={() => setCredits(p.credits)}
                className={`rounded-lg border px-2 py-2 text-[12px] font-medium transition-all ${
                  credits === p.credits
                    ? 'border-ink-900 bg-ink-900 text-white dark:border-ink-100 dark:bg-ink-100 dark:text-ink-900'
                    : 'border-ink-200 bg-white text-ink-700 hover:border-ink-300 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-300 dark:hover:border-ink-600'
                }`}
              >
                <div className="font-mono text-[14px] font-bold">{formatNumber(p.credits)}</div>
                <div className="text-[10.5px] opacity-80">{p.currency} {t('common.currency')}</div>
              </button>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <input
              type="number"
              min={1}
              value={credits}
              onChange={(e) => setCredits(Number(e.target.value) || 0)}
              className="h-9 flex-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12.5px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
              placeholder={t('wallet.customAmount')}
            />
            <span className="text-[12px] text-ink-500 dark:text-ink-400">{t('common.currency')}</span>
          </div>
        </div>
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('wallet.paymentMethod')}
          </label>
          <div className="grid grid-cols-3 gap-2">
            {['manual', 'alipay', 'wechat'].map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMethod(m)}
                className={`rounded-lg border px-3 py-2 text-[12px] font-medium transition-all ${
                  method === m
                    ? 'border-ink-900 bg-ink-900 text-white dark:border-ink-100 dark:bg-ink-100 dark:text-ink-900'
                    : 'border-ink-200 bg-white text-ink-700 hover:border-ink-300 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-300 dark:hover:border-ink-600'
                }`}
              >
                {t(`wallet.method.${m}`)}
              </button>
            ))}
          </div>
        </div>
        <p className="text-[11.5px] text-ink-500 dark:text-ink-400">{t('wallet.topUpHint')}</p>
      </div>
    </Dialog>
  );
}

function RedeemDialog({ onClose, onRedeemed }) {
  const { t } = useTranslation();
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!code.trim()) {
      toast.error(t('wallet.codeRequired'));
      return;
    }
    setSubmitting(true);
    try {
      const res = await api.redeemCode(code.trim());
      toast.success(res?.message || t('wallet.redeemOk'));
      onRedeemed?.();
    } catch (e) {
      toast.error(e.message || t('wallet.redeemFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('wallet.redeem')}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={submitting} icon={Ticket}>
            {t('wallet.confirmRedeem')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
            {t('wallet.code')}
          </label>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={t('wallet.codePlaceholder')}
            className="h-10 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[13px] uppercase tracking-wider outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/10"
            autoFocus
          />
        </div>
        <p className="text-[11.5px] text-ink-500 dark:text-ink-400">{t('wallet.redeemHint')}</p>
      </div>
    </Dialog>
  );
}
