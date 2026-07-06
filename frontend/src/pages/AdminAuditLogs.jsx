import { useEffect, useState, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { ScrollText, Filter, Download, Eye, X } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { TableRowSkeleton } from '@/components/Skeleton';

const ACTION_OPTIONS = [
  'order.approve',
  'order.reject',
  'order.refund',
  'wallet.adjust',
  'user.set_plan',
  'ADMIN_LOGIN',
  'ADMIN_LOGOUT',
  'ADMIN_INIT',
  'ADMIN_CHANGE_PASSWORD',
  'ADMIN_CREATE_USER',
  'ADMIN_UPDATE_USER',
  'ADMIN_DELETE_USER',
  'ADMIN_CREATE_CHANNEL',
  'ADMIN_UPDATE_CHANNEL',
  'ADMIN_DELETE_CHANNEL',
  'api_key.revoke',
  'promo.create',
  'redeem.batch_create',
  'ADMIN_SYNC_MODELS',
  'ADMIN_UPDATE_CONFIG',
  'ADMIN_UPDATE_MODEL_MAP',
];

const TARGET_OPTIONS = [
  'user',
  'order',
  'channel',
  'settings',
  'promo_code',
  'redeem_code',
  'api_key',
  'models',
];

// Map backend action strings to i18n keys
const ACTION_I18N_MAP = {
  'order.approve': 'orderApprove',
  'order.reject': 'orderReject',
  'order.refund': 'orderRefund',
  'wallet.adjust': 'walletAdjust',
  'user.set_plan': 'setPlan',
  ADMIN_LOGIN: 'login',
  ADMIN_LOGOUT: 'logout',
  ADMIN_INIT: 'init',
  ADMIN_CHANGE_PASSWORD: 'changePassword',
  ADMIN_CREATE_USER: 'createUser',
  ADMIN_UPDATE_USER: 'updateUser',
  ADMIN_DELETE_USER: 'deleteUser',
  ADMIN_CREATE_CHANNEL: 'createChannel',
  ADMIN_UPDATE_CHANNEL: 'updateChannel',
  ADMIN_DELETE_CHANNEL: 'deleteChannel',
  'api_key.revoke': 'revokeApiKey',
  'promo.create': 'promoCreate',
  'redeem.batch_create': 'redeemBatchCreate',
  ADMIN_SYNC_MODELS: 'syncModels',
  ADMIN_UPDATE_CONFIG: 'updateConfig',
  ADMIN_UPDATE_MODEL_MAP: 'updateModelMap',
};

export default function AdminAuditLogs() {
  const { t } = useTranslation();
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionFilter, setActionFilter] = useState('');
  const [targetFilter, setTargetFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [detailLog, setDetailLog] = useState(null);
  const PAGE_SIZE = 100;

  const load = useCallback(
    async (reset = false) => {
      const newOffset = reset ? 0 : offset;
      if (reset) setOffset(0);
      setLoading(true);
      try {
        const filters = {
          action: actionFilter || undefined,
          target_type: targetFilter || undefined,
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          limit: PAGE_SIZE,
          offset: newOffset,
        };
        const data = await api.listAuditLogs(filters);
        const list = Array.isArray(data) ? data : [];
        setLogs((prev) => (reset ? list : [...prev, ...list]));
        setHasMore(list.length >= PAGE_SIZE);
        if (reset) setOffset(PAGE_SIZE);
        else setOffset(newOffset + list.length);
      } catch {
        if (reset) setLogs([]);
        toast.error(t('common.operationFailed'));
      } finally {
        setLoading(false);
      }
    },
    [actionFilter, targetFilter, dateFrom, dateTo, offset],
  );

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actionFilter, targetFilter, dateFrom, dateTo]);

  const clearFilters = () => {
    setActionFilter('');
    setTargetFilter('');
    setDateFrom('');
    setDateTo('');
  };

  const hasFilters = actionFilter || targetFilter || dateFrom || dateTo;

  // Export to CSV (server-side with proper UTF-8 BOM)
  const exportCsv = () => {
    const params = new URLSearchParams();
    if (actionFilter) params.set('action', actionFilter);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    const qs = params.toString();
    window.open(`/api/admin/audit-logs/export.csv${qs ? `?${qs}` : ''}`, '_blank');
  };

  const actionLabel = (action) => {
    const key = ACTION_I18N_MAP[action];
    if (key) return t(`adminAuditLogs.actions.${key}`);
    return action;
  };

  const targetLabel = (target) => {
    if (!target) return '-';
    const key = target.replace(/\s+/g, '_');
    return t(`adminAuditLogs.targets.${key}`) || target;
  };

  const inputCls =
    'h-8 rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[12px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <>
      <TopBar
        title={t('adminAuditLogs.title')}
        subtitle={t('adminAuditLogs.subtitle')}
        action={
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" icon={Download} onClick={exportCsv}>
              {t('adminAuditLogs.export.button')}
            </Button>
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 dark:from-ink-900/80 via-ink-50/50 dark:via-ink-900/50 to-brand-50/30 dark:to-brand-900/30">
        <div className="mx-auto max-w-7xl space-y-4 p-4 md:p-6">
          {/* Filter bar */}
          <div className="card rounded-2xl border-ink-200/40 shadow-soft-lg p-4">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex items-center gap-1.5">
                <Filter size={12} className="text-ink-400 dark:text-ink-500" />
              </div>
              <select
                value={actionFilter}
                onChange={(e) => setActionFilter(e.target.value)}
                className={`${inputCls} min-w-[140px]`}
              >
                <option value="">{t('adminAuditLogs.filter.allActions')}</option>
                {ACTION_OPTIONS.map((a) => (
                  <option key={a} value={a}>
                    {actionLabel(a)}
                  </option>
                ))}
              </select>
              <select
                value={targetFilter}
                onChange={(e) => setTargetFilter(e.target.value)}
                className={`${inputCls} min-w-[120px]`}
              >
                <option value="">{t('adminAuditLogs.filter.allTargets')}</option>
                {TARGET_OPTIONS.map((tt) => (
                  <option key={tt} value={tt}>
                    {targetLabel(tt)}
                  </option>
                ))}
              </select>
              <div className="flex items-center gap-1.5 text-[11px] text-ink-500 dark:text-ink-400">
                <span>{t('adminAuditLogs.filter.dateFrom')}</span>
                <input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                  className={inputCls}
                />
              </div>
              <div className="flex items-center gap-1.5 text-[11px] text-ink-500 dark:text-ink-400">
                <span>{t('adminAuditLogs.filter.dateTo')}</span>
                <input
                  type="date"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                  className={inputCls}
                />
              </div>
              {hasFilters && (
                <Button variant="ghost" size="sm" icon={X} onClick={clearFilters}>
                  {t('adminAuditLogs.filter.clearFilters')}
                </Button>
              )}
            </div>
          </div>

          {/* Table */}
          <div className="rounded-2xl border border-ink-200/40 shadow-soft-lg overflow-hidden">
            {/* Table header */}
            <div className="hidden grid-cols-12 gap-4 border-b border-ink-100/60 dark:border-ink-800/60 bg-gradient-to-r from-ink-50/60 to-ink-50/30 dark:from-ink-900/60 dark:to-ink-900/30 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400 md:grid">
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.timestamp')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.actor')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.action')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.target')}
                </span>
              </div>
              <div className="col-span-2">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.ipAddress')}
                </span>
              </div>
              <div className="col-span-2 text-right">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">
                  {t('adminAuditLogs.table.details')}
                </span>
              </div>
            </div>

            {/* Loading skeleton */}
            {loading && logs.length === 0 && (
              <div className="p-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <TableRowSkeleton key={i} cols={6} />
                ))}
              </div>
            )}

            {/* Empty state */}
            {!loading && logs.length === 0 && (
              <div className="p-8">
                <EmptyState
                  icon={ScrollText}
                  title={t('adminAuditLogs.empty.title')}
                  description={t('adminAuditLogs.empty.description')}
                />
              </div>
            )}

            {/* Rows */}
            {logs.length > 0 && (
              <div>
                {logs.map((l) => (
                  <div
                    key={l.id}
                    className="group/row grid grid-cols-12 items-center gap-4 border-b border-ink-100/60 dark:border-ink-800/60 px-4 py-2.5 transition-colors duration-200 hover:bg-ink-50/80 dark:hover:bg-ink-900/80 last:border-b-0"
                  >
                    <div className="col-span-2 text-[11.5px] text-ink-500 dark:text-ink-400">
                      {formatDate(l.created_at)}
                    </div>
                    <div className="col-span-2 text-[12px]">
                      <Badge variant={l.actor_type === 'admin' ? 'dark' : 'default'}>
                        {l.actor_type || 'system'}
                      </Badge>
                      {l.actor_id && (
                        <span className="ml-1 font-mono text-[10px] text-ink-400 dark:text-ink-500">
                          #{l.actor_id}
                        </span>
                      )}
                    </div>
                    <div className="col-span-2">
                      <span className="inline-flex items-center rounded-full bg-indigo-50 dark:bg-indigo-900/20 px-2.5 py-1 text-[11px] font-medium text-indigo-700 dark:text-indigo-400 ring-1 ring-indigo-200/60 dark:ring-indigo-800/60">
                        {actionLabel(l.action)}
                      </span>
                    </div>
                    <div className="col-span-2 text-[12px] text-ink-700 dark:text-ink-300">
                      {targetLabel(l.target_type)}
                      {l.target_id && (
                        <span className="ml-1 font-mono text-[10px] text-ink-400 dark:text-ink-500">
                          #{l.target_id}
                        </span>
                      )}
                    </div>
                    <div className="col-span-2 font-mono text-[11px] text-ink-500 dark:text-ink-400">
                      {l.ip_address || '-'}
                    </div>
                    <div className="col-span-2 text-right">
                      <Button size="sm" variant="ghost" icon={Eye} onClick={() => setDetailLog(l)}>
                        {t('adminOrders.action.detail')}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Load more */}
          {hasMore && (
            <div className="text-center">
              <Button variant="secondary" onClick={() => load(false)} loading={loading}>
                {t('adminAuditLogs.loadMore')}
              </Button>
            </div>
          )}
        </div>
      </div>

      {/* Detail dialog */}
      {detailLog && (
        <Dialog
          open
          onClose={() => setDetailLog(null)}
          title={t('adminAuditLogs.detail.title')}
          description={`#${detailLog.id}`}
          size="lg"
          footer={<Button onClick={() => setDetailLog(null)}>{t('common.close')}</Button>}
        >
          <div className="space-y-3 text-[13px]">
            {[
              ['logId', detailLog.id],
              ['actorType', detailLog.actor_type],
              ['actorId', detailLog.actor_id || '-'],
              ['action', actionLabel(detailLog.action)],
              ['targetType', targetLabel(detailLog.target_type)],
              ['targetId', detailLog.target_id || '-'],
              ['ipAddress', detailLog.ip_address || '-'],
              ['createdAt', formatDate(detailLog.created_at)],
            ].map(([key, val]) => (
              <div
                key={key}
                className="flex items-center justify-between rounded-lg bg-ink-50/60 dark:bg-ink-900/60 px-3 py-2"
              >
                <span className="text-ink-500 dark:text-ink-400">{t(`adminAuditLogs.detail.${key}`)}</span>
                <span className="font-medium text-ink-900 dark:text-ink-100">{val}</span>
              </div>
            ))}
            {detailLog.details && (
              <div className="rounded-lg bg-ink-50/60 dark:bg-ink-900/60 p-3">
                <div className="mb-2 text-[12px] text-ink-500 dark:text-ink-400">
                  {t('adminAuditLogs.detail.detailsJson')}
                </div>
                <pre className="max-h-48 overflow-auto rounded-lg bg-ink-900 p-3 font-mono text-[11px] text-emerald-400">
                  {JSON.stringify(detailLog.details, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </Dialog>
      )}
    </>
  );
}
