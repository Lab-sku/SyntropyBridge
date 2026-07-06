import { useEffect, useState, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ShoppingCart,
  CheckCircle2,
  XCircle,
  RotateCcw,
  Eye,
  Filter,
  Download,
  AlertTriangle,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { TableRowSkeleton } from '@/components/Skeleton';

const STATUS_OPTIONS = [
  { value: '', key: 'allStatus' },
  { value: 'pending', key: 'pending' },
  { value: 'pending_review', key: 'pendingReview' },
  { value: 'paid', key: 'paid' },
  { value: 'failed', key: 'failed' },
  { value: 'refunded', key: 'refunded' },
];

const STATUS_BADGE = {
  pending: 'warning',
  pending_review: 'danger',
  paid: 'success',
  failed: 'danger',
  refunded: 'info',
};

export default function AdminOrders() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState(() => searchParams.get('status') || '');
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [detailOrder, setDetailOrder] = useState(null);
  const [actionDialog, setActionDialog] = useState(null);
  const [actionNote, setActionNote] = useState('');
  const [actionLoading, setActionLoading] = useState(false);
  const [usdtRefundAck, setUsdtRefundAck] = useState(false);
  const PAGE_SIZE = 50;

  const load = useCallback(
    async (reset = false) => {
      const newOffset = reset ? 0 : offset;
      if (reset) setOffset(0);
      setLoading(true);
      try {
        const filters = { status: statusFilter || undefined, limit: PAGE_SIZE, offset: newOffset };
        const data = await api.listAdminOrders(filters);
        const list = Array.isArray(data) ? data : [];
        setOrders((prev) => (reset ? list : [...prev, ...list]));
        setHasMore(list.length >= PAGE_SIZE);
        if (reset) setOffset(PAGE_SIZE);
        else setOffset(newOffset + list.length);
      } catch {
        if (reset) setOrders([]);
      } finally {
        setLoading(false);
      }
    },
    [statusFilter, offset],
  );

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  const openDetail = async (order) => {
    try {
      const detail = await api.getAdminOrder(order.id);
      setDetailOrder(detail);
    } catch {
      setDetailOrder(order);
    }
  };

  const openAction = (type, order) => {
    setActionDialog({ type, order });
    setActionNote('');
    setUsdtRefundAck(false);
  };

  // USDT 退款需要二次确认 checkbox 才允许提交
  // L13: 二次确认扩展到所有退款（不只是 USDT）。Stripe 退款虽然在
  // 网关层可逆，但本地钱包已扣减 + 订阅已激活的副作用需要管理员明确
  // 确认，避免误点。USDT 退款多一条"链上不可逆"特殊说明。
  const isRefund = actionDialog?.type === 'refund';
  const isUsdtRefund =
    isRefund && actionDialog?.order?.payment_provider === 'usdt';
  const refundDisabled = isRefund && !usdtRefundAck;

  const submitAction = async () => {
    if (!actionDialog) return;
    const { type, order } = actionDialog;
    setActionLoading(true);
    try {
      if (type === 'approve') {
        await api.approveOrder(order.id, actionNote);
        toast.success(t('adminOrders.toast.approved'));
      } else if (type === 'reject') {
        await api.rejectOrder(order.id, actionNote);
        toast.success(t('adminOrders.toast.rejected'));
      } else if (type === 'refund') {
        await api.refundOrder(order.id, actionNote);
        toast.success(t('adminOrders.toast.refunded'));
      }
      setActionDialog(null);
      load(true);
    } catch (e) {
      toast.error(e.message || t('adminOrders.toast.failed'));
    } finally {
      setActionLoading(false);
    }
  };

  const actionConfig = {
    approve: {
      title: t('adminOrders.dialog.approveTitle'),
      desc: t('adminOrders.dialog.approveDesc', { id: actionDialog?.order?.id }),
      icon: CheckCircle2,
      iconColor: 'from-emerald-500 to-green-600',
      confirmLabel: t('adminOrders.dialog.confirmApprove'),
      confirmVariant: 'success',
      labelKey: 'noteLabel',
      placeholderKey: 'notePlaceholder',
    },
    reject: {
      title: t('adminOrders.dialog.rejectTitle'),
      desc: t('adminOrders.dialog.rejectDesc', { id: actionDialog?.order?.id }),
      icon: XCircle,
      iconColor: 'from-rose-500 to-pink-600',
      confirmLabel: t('adminOrders.dialog.confirmReject'),
      confirmVariant: 'danger',
      labelKey: 'reasonLabel',
      placeholderKey: 'reasonPlaceholder',
    },
    refund: {
      title: t('adminOrders.dialog.refundTitle'),
      desc: t('adminOrders.dialog.refundDesc', { id: actionDialog?.order?.id }),
      icon: RotateCcw,
      iconColor: 'from-amber-500 to-orange-600',
      confirmLabel: t('adminOrders.dialog.confirmRefund'),
      confirmVariant: 'danger',
      labelKey: 'reasonLabel',
      placeholderKey: 'refundReasonPlaceholder',
    },
  };

  const currentAction = actionDialog ? actionConfig[actionDialog.type] : null;

  const exportCsv = () => {
    const params = new URLSearchParams();
    if (statusFilter) params.set('status', statusFilter);
    const qs = params.toString();
    window.open(`/api/admin/orders/export.csv${qs ? `?${qs}` : ''}`, '_blank');
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <>
      <TopBar
        title={t('adminOrders.title')}
        subtitle={t('adminOrders.subtitle')}
        action={
          <div className="flex items-center gap-2">
            <div className="flex h-8 items-center gap-1.5 rounded-xl border border-ink-200/40 dark:border-ink-700/40 bg-white dark:bg-ink-900 px-3 shadow-soft">
              <Filter size={12} className="text-ink-400 dark:text-ink-500" />
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="bg-transparent text-[12.5px] text-ink-900 dark:text-ink-100 outline-none"
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {t(`adminOrders.filter.${o.key}`)}
                  </option>
                ))}
              </select>
            </div>
            <Button variant="secondary" size="sm" icon={Download} onClick={exportCsv}>
              {t('orders.exportCsv')}
            </Button>
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 via-ink-50/50 to-brand-50/30">
        <div className="mx-auto max-w-7xl p-4 md:p-6">
          <div className="rounded-2xl border border-ink-200/40 shadow-soft-lg overflow-hidden">
            {/* Table header */}
            <div className="hidden grid-cols-12 gap-4 border-b border-ink-100/60 dark:border-ink-800/60 bg-gradient-to-r from-ink-50/60 to-ink-50/30 dark:from-ink-900/60 dark:to-ink-900/30 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400 md:grid">
              <div className="col-span-1">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.orderId')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.user')}
                </span>
              </div>
              <div className="col-span-1">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.amount')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.credits')}
                </span>
              </div>
              <div className="col-span-1">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.status')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminOrders.table.paymentMethod')}
                </span>
              </div>
              <div className="col-span-3 text-right">
                <span className="rounded-md bg-ink-100/60 px-2 py-0.5">{t('common.actions')}</span>
              </div>
            </div>

            {/* Loading skeleton */}
            {loading && orders.length === 0 && (
              <div className="p-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <TableRowSkeleton key={i} cols={7} />
                ))}
              </div>
            )}

            {/* Empty state */}
            {!loading && orders.length === 0 && (
              <div className="p-8">
                <EmptyState
                  icon={ShoppingCart}
                  title={t('adminOrders.empty.title')}
                  description={t('adminOrders.empty.description')}
                />
              </div>
            )}

            {/* Rows */}
            {orders.length > 0 && (
              <div>
                {orders.map((o) => (
                  <div
                    key={o.id}
                    className="group/row grid grid-cols-12 items-center gap-4 border-b border-ink-100/60 dark:border-ink-800/60 px-4 py-3 transition-colors duration-200 hover:bg-ink-50/80 dark:hover:bg-ink-900/80 last:border-b-0"
                  >
                    <div className="col-span-1 font-mono text-[11px] text-ink-700 dark:text-ink-300">#{o.id}</div>
                    <div className="col-span-2 truncate text-[12.5px] text-ink-900 dark:text-ink-100">
                      {o.username || `user#${o.user_id}`}
                    </div>
                    <div className="col-span-1 font-mono text-[12px] font-medium text-ink-900 dark:text-ink-100">
                      {o.amount != null ? `${Number(o.amount).toFixed(2)}` : '-'}
                    </div>
                    <div className="col-span-2 font-mono text-[12px] text-ink-700 dark:text-ink-300">
                      {Number(o.credits || 0).toFixed(2)}
                      {o.bonus_credits > 0 && (
                        <span className="ml-1 text-[10px] text-emerald-600 dark:text-emerald-400">
                          +{Number(o.bonus_credits).toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div className="col-span-1">
                      <Badge variant={STATUS_BADGE[o.status] || 'default'} dot>
                        {t(`adminOrders.badge.${o.status}`) || o.status}
                      </Badge>
                    </div>
                    <div className="col-span-2 text-[11.5px] text-ink-500 dark:text-ink-400">
                      {o.payment_method || '-'}
                    </div>
                    <div className="col-span-3 flex items-center justify-end gap-1.5">
                      <Button size="sm" variant="ghost" icon={Eye} onClick={() => openDetail(o)}>
                        {t('adminOrders.action.detail')}
                      </Button>
                      {o.status === 'pending' && (
                        <>
                          <Button
                            size="sm"
                            variant="success"
                            icon={CheckCircle2}
                            onClick={() => openAction('approve', o)}
                          >
                            {t('adminOrders.action.approve')}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            icon={XCircle}
                            onClick={() => openAction('reject', o)}
                          >
                            {t('adminOrders.action.reject')}
                          </Button>
                        </>
                      )}
                      {o.status === 'paid' && (
                        <Button
                          size="sm"
                          variant="danger"
                          icon={RotateCcw}
                          onClick={() => openAction('refund', o)}
                        >
                          {t('adminOrders.action.refund')}
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Load more */}
          {hasMore && (
            <div className="mt-4 text-center">
              <Button variant="secondary" onClick={() => load(false)} loading={loading}>
                {t('adminOrders.loadMore')}
              </Button>
            </div>
          )}
        </div>
      </div>

      {/* Order detail dialog */}
      {detailOrder && (
        <Dialog
          open
          onClose={() => setDetailOrder(null)}
          title={t('adminOrders.detail.title')}
          description={`#${detailOrder.id}`}
          size="lg"
          footer={<Button onClick={() => setDetailOrder(null)}>{t('common.close')}</Button>}
        >
          <div className="space-y-3 text-[13px]">
            {[
              ['orderNo', detailOrder.order_no],
              ['userId', detailOrder.user_id],
              ['username', detailOrder.username],
              ['amount', detailOrder.amount != null ? Number(detailOrder.amount).toFixed(2) : '-'],
              ['credits', Number(detailOrder.credits || 0).toFixed(2)],
              ['bonusCredits', Number(detailOrder.bonus_credits || 0).toFixed(2)],
              [
                'status',
                <Badge key="s" variant={STATUS_BADGE[detailOrder.status] || 'default'} dot>
                  {t(`adminOrders.badge.${detailOrder.status}`) || detailOrder.status}
                </Badge>,
              ],
              ['paymentMethod', detailOrder.payment_method],
              ['promoCode', detailOrder.promo_code || '-'],
              ['note', detailOrder.note || '-'],
              ['paidAt', formatDate(detailOrder.paid_at)],
              ['createdAt', formatDate(detailOrder.created_at)],
            ].map(([key, val]) => (
              <div
                key={key}
                className="flex items-center justify-between rounded-lg bg-ink-50/60 dark:bg-ink-900/60 px-3 py-2"
              >
                <span className="text-ink-500 dark:text-ink-400">{t(`adminOrders.detail.${key}`)}</span>
                <span className="font-medium text-ink-900 dark:text-ink-100">{val}</span>
              </div>
            ))}
          </div>
        </Dialog>
      )}

      {/* Action confirmation dialog */}
      {actionDialog && currentAction && (
        <Dialog
          open
          onClose={() => setActionDialog(null)}
          title={currentAction.title}
          description={currentAction.desc}
          size="md"
          footer={
            <>
              <Button variant="secondary" onClick={() => setActionDialog(null)}>
                {t('common.cancel')}
              </Button>
              <Button
                variant={currentAction.confirmVariant}
                onClick={submitAction}
                loading={actionLoading}
                icon={currentAction.icon}
                disabled={refundDisabled}
              >
                {currentAction.confirmLabel}
              </Button>
            </>
          }
        >
          <div className="space-y-3">
            {isRefund && (
              <div className="space-y-2 rounded-lg border border-rose-200 bg-rose-50/70 p-3 dark:border-rose-800/60 dark:bg-rose-900/10">
                <div className="flex items-start gap-2">
                  <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-600 dark:text-rose-400" />
                  <div className="flex-1">
                    <div className="text-[13px] font-semibold text-rose-800 dark:text-rose-300">
                      {isUsdtRefund
                        ? t('adminOrders.dialog.usdtRefundTitle')
                        : t('adminOrders.dialog.refundConfirmTitle')}
                    </div>
                    <div className="mt-1 text-[11.5px] leading-relaxed text-rose-700 dark:text-rose-400/90">
                      {isUsdtRefund
                        ? t('adminOrders.dialog.usdtRefundDesc')
                        : t('adminOrders.dialog.refundConfirmDesc')}
                    </div>
                  </div>
                </div>
                <label className="flex items-start gap-2 cursor-pointer text-[12px] text-rose-800 dark:text-rose-300">
                  <input
                    type="checkbox"
                    checked={usdtRefundAck}
                    onChange={(e) => setUsdtRefundAck(e.target.checked)}
                    className="mt-0.5 h-3.5 w-3.5 rounded border-rose-400 text-rose-600 focus:ring-rose-500"
                  />
                  <span>{t('adminOrders.dialog.refundAck')}</span>
                </label>
              </div>
            )}
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t(`adminOrders.dialog.${currentAction.labelKey}`)}
              </label>
              <input
                value={actionNote}
                onChange={(e) => setActionNote(e.target.value)}
                placeholder={t(`adminOrders.dialog.${currentAction.placeholderKey}`)}
                className={inputCls}
              />
            </div>
          </div>
        </Dialog>
      )}
    </>
  );
}
