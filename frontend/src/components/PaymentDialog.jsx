import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { CreditCard, Copy, ExternalLink, Check, Wallet as WalletIcon, AlertCircle } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import { formatNumber } from '@/lib/utils';

/**
 * PaymentDialog — triggered after an order is created.
 *
 * Fetches the admin-curated list of enabled payment providers from
 * ``GET /billing/providers`` (so newly-added providers surface in the
 * UI without a frontend change) and dispatches each to its native
 * checkout flow:
 *
 *   - Stripe / Alipay / WeChat: redirect to the provider's hosted
 *     checkout page, poll on return.
 *   - USDT (NOWPayments): render an inline deposit UI (address +
 *     amount + network) and poll the order status until the IPN
 *     confirms the on-chain payment.
 *
 * Props:
 *   - order: { order_no, amount, credits }
 *   - onClose: () => void
 *   - onPaid: () => void — called after successful payment polling
 */
export default function PaymentDialog({ order, onClose, onPaid }) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [usdtSession, setUsdtSession] = useState(null);
  const [addressCopied, setAddressCopied] = useState(false);
  const [pollingStatus, setPollingStatus] = useState('idle'); // idle | waiting | success | timeout
  // L14: 加载支付方式失败时显示错误状态 + 重试按钮，不再硬编码
  // Stripe-only fallback（如果 Stripe 未配置会误导用户）。
  const [loadError, setLoadError] = useState(false);
  // 跟踪组件是否仍挂载，避免 unmount 后 pollOrder 仍更新 state / 触发 toast
  const isMountedRef = useRef(true);
  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Load available providers once on mount. L14: 失败时显示错误状态 +
  // 重试按钮，不再硬编码 Stripe-only 列表（如果 Stripe 未配置会误导
  // 用户点击后失败）。
  const loadProviders = useCallback(async () => {
    setLoadError(false);
    try {
      const list = await api.listPublicPaymentProviders();
      if (!isMountedRef.current) return;
      const safe = Array.isArray(list) ? list : [];
      setProviders(safe);
      if (safe.length > 0 && !provider) {
        // Prefer stripe, then whatever comes first.
        const preferred = safe.find((p) => p.name === 'stripe') || safe[0];
        setProvider(preferred.name);
      }
    } catch {
      if (isMountedRef.current) {
        setProviders([]);
        setProvider(null);
        setLoadError(true);
      }
    }
  }, [provider]);

  useEffect(() => {
    loadProviders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const orderNo = order?.order_no || '';
  const amount = order?.amount || 0;
  const credits = order?.credits || 0;

  // Parse a USDT checkout URL ("/wallet/crypto?payment_id=...&address=...&pay_amount=...&pay_currency=...")
  // into a structured session object the inline deposit UI can render.
  const parsedUsdt = useMemo(() => {
    if (!usdtSession?.checkout_url) return null;
    try {
      const u = new URL(usdtSession.checkout_url, window.location.origin);
      return {
        paymentId: u.searchParams.get('payment_id') || '',
        address: u.searchParams.get('address') || '',
        payAmount: u.searchParams.get('pay_amount') || '',
        payCurrency: (u.searchParams.get('pay_currency') || 'usdt').toUpperCase(),
        network: (u.searchParams.get('network') || 'TRC20').toUpperCase(),
      };
    } catch {
      return null;
    }
  }, [usdtSession]);

  const pollOrder = useCallback(
    async ({ maxAttempts = 60, intervalMs = 5000 } = {}) => {
      if (!orderNo) return false;
      setPollingStatus('waiting');
      for (let i = 0; i < maxAttempts; i++) {
        // 每次 iteration 前检查 mounted，避免 unmount 后继续轮询
        if (!isMountedRef.current) return false;
        try {
          const res = await api.queryOrderPayment(orderNo);
          // 异步操作后再次检查 mounted
          if (!isMountedRef.current) return false;
          const status = res?.status;
          if (status === 'paid' || status === 'succeeded') {
            setPollingStatus('success');
            toast.success(t('payment.success'));
            try {
              onPaid && onPaid();
            } catch {
              /* ignore */
            }
            return true;
          }
          if (status === 'cancelled' || status === 'failed') {
            setPollingStatus('idle');
            return false;
          }
        } catch {
          /* transient network error — keep polling */
        }
        // 等待期间若组件已 unmount，则停止 polling
        await new Promise((r) => setTimeout(r, intervalMs));
        if (!isMountedRef.current) return false;
      }
      if (!isMountedRef.current) return false;
      setPollingStatus('timeout');
      return false;
    },
    [orderNo, onPaid, t],
  );

  const handlePay = async () => {
    if (!orderNo || !provider) return;
    setSubmitting(true);
    try {
      const res = await api.payOrder(orderNo, { provider });
      if (provider === 'usdt') {
        // Inline deposit UI — don't redirect.
        setUsdtSession(res || null);
        setSubmitting(false);
        // Start polling as soon as the session is rendered.
        pollOrder({ maxAttempts: 120, intervalMs: 6000 });
      } else {
        // Redirect-based providers (Stripe / Alipay / WeChat)
        if (res?.checkout_url) {
          window.location.href = res.checkout_url;
        } else {
          toast.error(t('payment.noCheckoutUrl'));
          setSubmitting(false);
        }
      }
    } catch (e) {
      toast.error(e.message || t('payment.payFailed'));
      setSubmitting(false);
    }
  };

  const copyAddress = async () => {
    if (!parsedUsdt?.address) return;
    try {
      await navigator.clipboard.writeText(parsedUsdt.address);
      setAddressCopied(true);
      toast.success(t('payment.usdtCopied'));
      setTimeout(() => setAddressCopied(false), 2000);
    } catch {
      toast.error(t('payment.copyFailed'));
    }
  };

  const providerLabel = (name) =>
    t(`payment.providers.${name}`, { defaultValue: name });

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('payment.title')}
      size="sm"
      footer={
        usdtSession ? (
          // USDT deposit UI has its own close-only footer (payment
          // is automatic once the chain confirms).
          <Button variant="secondary" onClick={onClose}>
            {t('common.close')}
          </Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button
              onClick={handlePay}
              loading={submitting}
              icon={ExternalLink}
              disabled={!provider}
            >
              {t('payment.payNow')}
            </Button>
          </>
        )
      }
    >
      <div className="space-y-4">
        {/* Order summary */}
        <div className="rounded-lg border border-ink-200 bg-ink-50/50 p-3 dark:border-ink-700 dark:bg-ink-800/50">
          <div className="flex items-center justify-between text-[12px]">
            <span className="text-ink-500">{t('payment.orderNo')}</span>
            <span className="font-mono text-ink-700 dark:text-ink-300">{orderNo}</span>
          </div>
          <div className="mt-2 flex items-center justify-between text-[12px]">
            <span className="text-ink-500">{t('payment.amount')}</span>
            <span className="font-mono font-semibold text-ink-900 dark:text-ink-100">¥{amount}</span>
          </div>
          <div className="mt-1 flex items-center justify-between text-[12px]">
            <span className="text-ink-500">{t('payment.credits')}</span>
            <span className="font-mono font-semibold text-ink-900 dark:text-ink-100">
              {formatNumber(credits)} {t('common.currency')}
            </span>
          </div>
        </div>

        {usdtSession && parsedUsdt ? (
          // Inline USDT deposit UI
          <div className="space-y-3">
            {pollingStatus === 'timeout' ? (
              // 链上确认超时专属面板
              <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50/60 p-4 dark:border-amber-800/60 dark:bg-amber-900/10">
                <div className="flex items-start gap-2">
                  <AlertCircle size={16} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
                  <div className="flex-1">
                    <div className="text-[13px] font-semibold text-amber-800 dark:text-amber-300">
                      {t('payment.usdtTimeoutTitle')}
                    </div>
                    <div className="mt-1 text-[11.5px] leading-relaxed text-amber-700 dark:text-amber-400/90">
                      {t('payment.usdtTimeoutDesc', { orderNo })}
                    </div>
                  </div>
                </div>
                <div className="flex items-center justify-end gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => pollOrder({ maxAttempts: 120, intervalMs: 6000 })}
                  >
                    {t('payment.usdtTimeoutRetry')}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={onClose}>
                    {t('common.close')}
                  </Button>
                </div>
              </div>
            ) : !parsedUsdt.payAmount ? (
              // 后端未返回 pay_amount — 显示错误状态，禁用转账引导
              <div className="space-y-3 rounded-lg border border-rose-200 bg-rose-50/60 p-4 dark:border-rose-800/60 dark:bg-rose-900/10">
                <div className="flex items-start gap-2">
                  <AlertCircle size={16} className="mt-0.5 shrink-0 text-rose-600 dark:text-rose-400" />
                  <div className="flex-1">
                    <div className="text-[13px] font-semibold text-rose-800 dark:text-rose-300">
                      {t('payment.usdtAmountMissing')}
                    </div>
                    <div className="mt-1 text-[11.5px] leading-relaxed text-rose-700 dark:text-rose-400/90">
                      {t('payment.usdtAmountMissingHint')}
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <>
                <div>
                  <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-ink-500">
                    {t('payment.usdtAmount')}
                  </div>
                  <div className="flex items-center justify-between rounded-md border border-ink-200 bg-white px-3 py-2 font-mono text-[13px] font-semibold text-ink-900 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-100">
                    <span>
                      {parsedUsdt.payAmount} {parsedUsdt.payCurrency}
                    </span>
                    <span className="text-[10px] font-normal text-ink-500">
                      {t('payment.usdtNetwork')}: {parsedUsdt.network}
                    </span>
                  </div>
                </div>

                <div>
                  <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-ink-500">
                    {t('payment.usdtAddress')}
                  </div>
                  <div className="flex items-center gap-2 rounded-md border border-ink-200 bg-white px-3 py-2 dark:border-ink-700 dark:bg-ink-900">
                    <WalletIcon size={14} className="shrink-0 text-ink-500" />
                    <span className="flex-1 break-all font-mono text-[12px] text-ink-800 dark:text-ink-200">
                      {parsedUsdt.address}
                    </span>
                    <button
                      type="button"
                      onClick={copyAddress}
                      className="shrink-0 rounded p-1 text-ink-500 transition hover:bg-ink-100 hover:text-ink-900 dark:hover:bg-ink-800 dark:hover:text-ink-100"
                      aria-label={t('payment.usdtCopy')}
                    >
                      {addressCopied ? <Check size={14} /> : <Copy size={14} />}
                    </button>
                  </div>
                </div>

                <p className="text-[11.5px] text-ink-500 dark:text-ink-400">{t('payment.usdtHint')}</p>

                <div className="flex items-center gap-2 rounded-md border border-ink-200 bg-ink-50 px-3 py-2 text-[11.5px] text-ink-600 dark:border-ink-700 dark:bg-ink-800/50 dark:text-ink-300">
                  <span className="relative flex h-2 w-2">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
                  </span>
                  {pollingStatus === 'timeout'
                    ? t('payment.usdtTimeout')
                    : t('payment.usdtWaiting')}
                </div>
              </>
            )}
          </div>
        ) : (
          // Provider selector (Stripe / Alipay / WeChat / USDT if enabled)
          <div>
            <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
              {t('payment.selectProvider')}
            </label>
            {loadError ? (
              <div className="space-y-2 rounded-md border border-rose-200 bg-rose-50/70 p-3 dark:border-rose-800/60 dark:bg-rose-900/10">
                <div className="flex items-start gap-2">
                  <AlertCircle size={14} className="mt-0.5 shrink-0 text-rose-600 dark:text-rose-400" />
                  <div className="flex-1">
                    <div className="text-[12px] font-medium text-rose-800 dark:text-rose-300">
                      {t('payment.loadProvidersFailed')}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => loadProviders()}
                  className="text-[12px] font-medium text-rose-700 underline-offset-2 hover:underline dark:text-rose-400"
                >
                  {t('common.retry')}
                </button>
              </div>
            ) : providers.length === 0 ? (
              <div className="rounded-md border border-ink-200 bg-ink-50 p-3 text-[12px] text-ink-500 dark:border-ink-700 dark:bg-ink-800/50 dark:text-ink-400">
                {t('payment.comingSoon')}
              </div>
            ) : (
              <div
                className="grid gap-2"
                style={{ gridTemplateColumns: `repeat(${Math.min(providers.length, 4)}, minmax(0, 1fr))` }}
              >
                {providers.map((p) => {
                  const disabled = !p.available || !p.enabled;
                  const icon =
                    p.name === 'usdt' ? (
                      <WalletIcon size={12} />
                    ) : (
                      <CreditCard size={12} />
                    );
                  return (
                    <button
                      key={p.name}
                      type="button"
                      disabled={disabled}
                      onClick={() => !disabled && setProvider(p.name)}
                      className={`relative rounded-lg border px-3 py-2 text-[12px] font-medium transition-all ${
                        disabled
                          ? 'cursor-not-allowed border-ink-100 bg-ink-50 text-ink-300 dark:border-ink-700 dark:bg-ink-800 dark:text-ink-500'
                          : provider === p.name
                            ? 'border-ink-900 bg-ink-900 text-white dark:border-ink-100 dark:bg-ink-100 dark:text-ink-900'
                            : 'border-ink-200 bg-white text-ink-700 hover:border-ink-300 dark:border-ink-700 dark:bg-ink-900 dark:text-ink-300 dark:hover:border-ink-600'
                      }`}
                    >
                      <div className="flex items-center justify-center gap-1.5">
                        {icon}
                        {providerLabel(p.name)}
                      </div>
                      {disabled ? (
                        <div className="mt-0.5 text-[10px] opacity-60">{t('payment.comingSoon')}</div>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {!usdtSession && (
          <p className="text-[11.5px] text-ink-500 dark:text-ink-400">{t('payment.hint')}</p>
        )}
      </div>
    </Dialog>
  );
}
