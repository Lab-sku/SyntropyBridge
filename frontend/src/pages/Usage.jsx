import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  TrendingUp,
  Calendar,
  PieChart as PieIcon,
  Download,
  Coins,
  Layers,
  BarChart3,
  Globe,
  Clock,
  CheckCircle,
  AlertTriangle,
  XCircle,
  Wallet,
  Crown,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { Bar, Doughnut, Line } from 'react-chartjs-2';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatNumber, formatTokens } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';
import { chartTheme, buildUsageChart } from '@/lib/chart';

const RANGE_OPTIONS = [
  { value: '7d', label: 'usage.last7Days' },
  { value: '30d', label: 'usage.last30Days' },
  { value: '90d', label: 'usage.last90Days' },
];

const ADMIN_RANGE_OPTIONS = [
  { value: '1d', label: 'usage.today' },
  { value: '7d', label: 'usage.last7Days' },
  { value: '30d', label: 'usage.last30Days' },
  { value: 'mtd', label: 'usage.thisMonth' },
];

const LOGS_PER_PAGE = 10;

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------

/** Return a Tailwind colour class based on usage percentage. */
function pctColor(pct) {
  if (pct >= 90) return 'bg-red-500';
  if (pct >= 70) return 'bg-amber-400';
  return 'bg-emerald-500';
}
function pctTextColor(pct) {
  if (pct >= 90) return 'text-red-600';
  if (pct >= 70) return 'text-amber-600';
  return 'text-emerald-600';
}

function StatusBadge({ code }) {
  const { t } = useTranslation();
  if (!code) return null;
  if (code >= 200 && code < 300)
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
        <CheckCircle size={10} />
        {code}
      </span>
    );
  if (code >= 400 && code < 500)
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
        <AlertTriangle size={10} />
        {code}
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-700 dark:bg-red-900/30 dark:text-red-400">
      <XCircle size={10} />
      {code}
    </span>
  );
}

// -----------------------------------------------------------------------
// Quota progress card
// -----------------------------------------------------------------------
function QuotaCard({ label, used, limit, percent, icon: Icon, unit }) {
  const displayPct = Math.min(percent ?? 0, 100);
  return (
    <div className="card p-4">
      <div className="mb-2 flex items-center gap-2 text-[11.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
        <Icon size={12} className="text-ink-400 dark:text-ink-500" />
        {label}
      </div>
      <div className="mb-1.5 font-mono text-[20px] font-bold text-ink-900 dark:text-ink-100">
        {formatNumber(used)}
        <span className="text-[13px] font-normal text-ink-400 dark:text-ink-500"> / {formatNumber(limit)}</span>
      </div>
      {unit && <div className="mb-1 text-[11px] text-ink-400 dark:text-ink-500">{unit}</div>}
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
        <div
          className={`absolute inset-y-0 left-0 rounded-full transition-all ${pctColor(displayPct)}`}
          style={{ width: `${displayPct}%` }}
        />
      </div>
      <div className={`mt-1 text-right text-[11px] font-semibold ${pctTextColor(displayPct)}`}>
        {displayPct.toFixed(1)}%
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------
// Main component
// -----------------------------------------------------------------------

/**
 * Dual-perspective usage dashboard.
 *
 *   - User self-service view (no `?user_id` set): pulls from
 *     ``/user/dashboard/summary`` and ``/user/dashboard/chart`` etc.
 *   - Admin platform-wide view (``isAdmin === true``): pulls from
 *     ``/admin/stats/overview``, ``/admin/stats/trend`` and friends.
 */
export default function Usage() {
  const { t } = useTranslation();
  const isAdmin = useAuthStore((s) => s.role === 'admin');
  const rangeOptions = isAdmin ? ADMIN_RANGE_OPTIONS : RANGE_OPTIONS;

  const [range, setRange] = useState('30d');
  const [loading, setLoading] = useState(true);

  // Admin state
  const [summary, setSummary] = useState(null);
  const [daily, setDaily] = useState({ labels: [], values: [] });
  const [byModel, setByModel] = useState([]);
  const [byProvider, setByProvider] = useState([]);

  // User-dashboard state
  const [quotaSummary, setQuotaSummary] = useState(null);
  const [chartData, setChartData] = useState([]);
  const [modelData, setModelData] = useState([]);
  const [logs, setLogs] = useState([]);
  const [logPage, setLogPage] = useState(0);

  const load = async () => {
    setLoading(true);
    try {
      if (isAdmin) {
        // Admin: platform-wide aggregates
        const [overview, trend, topModels, byProv] = await Promise.all([
          api.getAdminOverview(range).catch(() => null),
          api.getAdminTrend(range).catch(() => []),
          api.getAdminTopModels(range).catch(() => []),
          api.getAdminProviderBreakdown(range).catch(() => []),
        ]);
        const tokens = Number(overview?.total_tokens_today || 0);
        const requests = Number(overview?.total_requests_today || 0);
        const cost = Number(overview?.total_cost_today || 0);
        setSummary({
          total_tokens: tokens,
          total_requests: requests,
          total_credits: cost,
        });
        setDaily({
          labels: (trend || []).map((r) => r.date),
          values: (trend || []).map((r) => Number(r.tokens || 0)),
        });
        setByModel(
          (topModels || []).map((r) => ({
            model_id: r.model,
            model: r.model,
            total_tokens: r.tokens,
          })),
        );
        setByProvider(
          (byProv || []).map((r) => ({
            provider: r.provider,
            total_tokens: r.tokens,
          })),
        );
      } else {
        // User self-service: dashboard endpoints
        const [qs, chart, models, userLogs] = await Promise.all([
          api.getDashboardSummary().catch(() => null),
          api.getDashboardChart(range).catch(() => ({ data: [] })),
          api.getDashboardByModel(range).catch(() => ({ data: [] })),
          api.getUserLogs(200).catch(() => []),
        ]);
        setQuotaSummary(qs);
        setChartData(chart?.data || []);
        setModelData(models?.data || []);
        setLogs(Array.isArray(userLogs) ? userLogs : []);
        setLogPage(0);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, isAdmin]);

  // -----------------------------------------------------------------------
  // Admin-derived chart data (unchanged from previous version)
  // -----------------------------------------------------------------------

  const kpis = useMemo(() => {
    const s = summary || {};
    return [
      {
        label: t('usage.tokens'),
        value: formatTokens(s.total_tokens ?? 0),
        icon: Coins,
        tone: 'text-indigo-600',
      },
      {
        label: t('usage.requests'),
        value: formatNumber(s.total_requests ?? 0),
        icon: Activity,
        tone: 'text-amber-600',
      },
      {
        label: t('usage.cost'),
        value: `${formatNumber(s.total_credits ?? 0)} ${t('common.currency')}`,
        icon: TrendingUp,
        tone: 'text-emerald-600',
      },
      { label: t('usage.byModel'), value: byModel.length, icon: Layers, tone: 'text-sky-600' },
    ];
  }, [summary, byModel, t]);

  const dailyChart = useMemo(
    () => ({
      labels: daily.labels,
      datasets: [
        {
          label: t('usage.tokens'),
          data: daily.values,
          backgroundColor: 'rgba(99, 102, 241, 0.78)',
          borderRadius: 6,
          maxBarThickness: 28,
        },
      ],
    }),
    [daily, t],
  );

  const modelChart = useMemo(
    () => ({
      labels: byModel.map((r) => r.model_id || r.model || 'unknown'),
      datasets: [
        {
          data: byModel.map((r) => r.total_tokens || r.tokens || 0),
          backgroundColor: [
            '#6366f1',
            '#22c55e',
            '#f59e0b',
            '#0ea5e9',
            '#ec4899',
            '#8b5cf6',
            '#14b8a6',
            '#f97316',
            '#84cc16',
            '#a855f7',
          ],
          borderWidth: 0,
        },
      ],
    }),
    [byModel],
  );

  const providerChart = useMemo(
    () => ({
      labels: byProvider.map((r) => r.provider || 'unknown'),
      datasets: [
        {
          data: byProvider.map((r) => r.total_tokens || r.tokens || 0),
          backgroundColor: [
            '#0ea5e9',
            '#10b981',
            '#f43f5e',
            '#8b5cf6',
            '#f59e0b',
            '#06b6d4',
            '#a855f7',
            '#84cc16',
          ],
          borderWidth: 0,
        },
      ],
    }),
    [byProvider],
  );

  // -----------------------------------------------------------------------
  // User-derived chart data
  // -----------------------------------------------------------------------

  const usageLineChart = useMemo(() => buildUsageChart(chartData), [chartData]);

  const modelPieChart = useMemo(
    () => ({
      labels: modelData.map((r) => r.model || 'unknown'),
      datasets: [
        {
          data: modelData.map((r) => r.cost_credits || 0),
          backgroundColor: [
            '#6366f1',
            '#22c55e',
            '#f59e0b',
            '#0ea5e9',
            '#ec4899',
            '#8b5cf6',
            '#14b8a6',
            '#f97316',
            '#84cc16',
            '#a855f7',
          ],
          borderWidth: 0,
        },
      ],
    }),
    [modelData],
  );

  const paginatedLogs = useMemo(() => {
    const start = logPage * LOGS_PER_PAGE;
    return logs.slice(start, start + LOGS_PER_PAGE);
  }, [logs, logPage]);
  const totalLogPages = Math.max(1, Math.ceil(logs.length / LOGS_PER_PAGE));

  // -----------------------------------------------------------------------
  // Export
  // -----------------------------------------------------------------------

  const onExport = async () => {
    try {
      const exportPath = isAdmin
        ? `/api/admin/usage/export?days=30`
        : `/api/user/dashboard/export.csv?range=${range}`;
      const res = await fetch(exportPath, { credentials: 'include' });
      if (!res.ok) throw new Error('export failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `usage-${isAdmin ? 'platform' : 'me'}-${range}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success(t('usage.exportOk'));
    } catch (e) {
      toast.error(e.message || t('usage.exportFailed'));
    }
  };

  const subtitle = isAdmin ? `${t('usage.subtitle')} · ${t('nav.overview')}` : t('usage.subtitle');

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <>
      <TopBar
        title={t('usage.title')}
        subtitle={subtitle}
        action={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
              {rangeOptions.map((r) => (
                <button
                  key={r.value}
                  onClick={() => setRange(r.value)}
                  className={`flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium transition-all ${
                    range === r.value ? 'bg-ink-900 text-white dark:bg-ink-100 dark:text-ink-900' : 'text-ink-600 hover:bg-ink-50 dark:text-ink-400 dark:hover:bg-ink-800'
                  }`}
                >
                  <Calendar size={11} />
                  {t(r.label)}
                </button>
              ))}
            </div>
            <Button size="sm" variant="secondary" icon={Download} onClick={onExport}>
              {isAdmin ? t('usage.export') : t('usage.exportMyData')}
            </Button>
          </div>
        }
      />

      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
          {loading && !summary && !quotaSummary ? (
            <CardSkeleton rows={2} />
          ) : isAdmin ? (
            /* ===========================================================
               ADMIN VIEW (platform-wide)
               =========================================================== */
            <>
              {/* KPI strip */}
              <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                {kpis.map((k) => (
                  <div key={k.label} className="card p-4">
                    <div className="flex items-center gap-2 text-[11.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
                      <k.icon size={12} className={k.tone} />
                      {k.label}
                    </div>
                    <div className="mt-1.5 font-mono text-[22px] font-bold text-ink-900 dark:text-ink-100">
                      {k.value}
                    </div>
                  </div>
                ))}
              </div>

              {/* Daily trend */}
              <div className="card p-5">
                <div className="mb-3 flex items-center gap-2">
                  <BarChart3 size={13} className="text-ink-500 dark:text-ink-400" />
                  <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{t('usage.byDay')}</h2>
                </div>
                {daily.values.length === 0 ? (
                  <EmptyState
                    icon={BarChart3}
                    title={t('usage.empty')}
                    description={t('usage.emptyHint')}
                  />
                ) : (
                  <div className="h-64">
                    <Bar data={dailyChart} options={chartTheme()} />
                  </div>
                )}
              </div>

              {/* Donut charts */}
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <div className="card p-5">
                  <div className="mb-3 flex items-center gap-2">
                    <PieIcon size={13} className="text-ink-500 dark:text-ink-400" />
                    <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{t('usage.byModel')}</h2>
                  </div>
                  {byModel.length === 0 ? (
                    <EmptyState icon={Layers} title={t('usage.empty')} />
                  ) : (
                    <div className="h-64">
                      <Doughnut
                        data={modelChart}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          cutout: '62%',
                          plugins: {
                            legend: {
                              position: 'right',
                              labels: { boxWidth: 10, font: { size: 11 } },
                            },
                          },
                        }}
                      />
                    </div>
                  )}
                </div>
                <div className="card p-5">
                  <div className="mb-3 flex items-center gap-2">
                    <Globe size={13} className="text-ink-500 dark:text-ink-400" />
                    <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('usage.byProvider')}
                    </h2>
                  </div>
                  {byProvider.length === 0 ? (
                    <EmptyState icon={Globe} title={t('usage.empty')} />
                  ) : (
                    <div className="h-64">
                      <Doughnut
                        data={providerChart}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          cutout: '62%',
                          plugins: {
                            legend: {
                              position: 'right',
                              labels: { boxWidth: 10, font: { size: 11 } },
                            },
                          },
                        }}
                      />
                    </div>
                  )}
                </div>
              </div>
            </>
          ) : (
            /* ===========================================================
               USER VIEW (self-service dashboard)
               =========================================================== */
            <>
              {/* Section 1: Quota cards (4 cards in a row) */}
              {quotaSummary && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <QuotaCard
                    label={t('usage.quota5h')}
                    icon={Clock}
                    used={quotaSummary.quota_5h?.used ?? 0}
                    limit={quotaSummary.quota_5h?.limit ?? 0}
                    percent={quotaSummary.quota_5h?.percent ?? 0}
                  />
                  <QuotaCard
                    label={t('usage.quotaWeek')}
                    icon={Calendar}
                    used={quotaSummary.quota_week?.used ?? 0}
                    limit={quotaSummary.quota_week?.limit ?? 0}
                    percent={quotaSummary.quota_week?.percent ?? 0}
                  />
                  <QuotaCard
                    label={t('usage.quotaMonth')}
                    icon={Activity}
                    used={quotaSummary.quota_month?.used ?? 0}
                    limit={quotaSummary.quota_month?.limit ?? 0}
                    percent={quotaSummary.quota_month?.percent ?? 0}
                  />
                  <QuotaCard
                    label={t('usage.monthlyBudget')}
                    icon={Wallet}
                    used={quotaSummary.monthly_budget?.used_credits ?? 0}
                    limit={quotaSummary.monthly_budget?.limit_credits ?? 0}
                    percent={quotaSummary.monthly_budget?.percent ?? 0}
                    unit={t('common.currency')}
                  />
                </div>
              )}

              {/* Wallet + plan info strip */}
              {quotaSummary && (
                <div className="flex flex-wrap gap-3">
                  <div className="card flex items-center gap-2 px-4 py-2.5">
                    <Wallet size={14} className="text-emerald-600 dark:text-emerald-400" />
                    <span className="text-[12px] font-medium text-ink-500 dark:text-ink-400">
                      {t('usage.walletBalance')}:
                    </span>
                    <span className="font-mono text-[14px] font-bold text-ink-900 dark:text-ink-100">
                      {(quotaSummary.wallet_balance ?? 0).toFixed(2)}
                    </span>
                  </div>
                  <div className="card flex items-center gap-2 px-4 py-2.5">
                    <Crown size={14} className="text-amber-500 dark:text-amber-400" />
                    <span className="text-[12px] font-medium text-ink-500 dark:text-ink-400">
                      {t('usage.currentPlan')}:
                    </span>
                    <span className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                      {quotaSummary.current_plan?.name || t('usage.noPlan')}
                    </span>
                  </div>
                </div>
              )}

              {/* Section 2: Usage chart (last N days, dual y-axis line chart) */}
              <div className="card p-5">
                <div className="mb-3 flex items-center gap-2">
                  <BarChart3 size={13} className="text-ink-500 dark:text-ink-400" />
                  <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('usage.chart.title')}
                  </h2>
                </div>
                {chartData.length === 0 ? (
                  <EmptyState
                    icon={BarChart3}
                    title={t('usage.empty')}
                    description={t('usage.emptyHint')}
                  />
                ) : (
                  <div className="h-72">
                    <Line data={usageLineChart.data} options={usageLineChart.options} />
                  </div>
                )}
              </div>

              {/* Section 3: Model breakdown (table + pie chart) */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
                {/* Table */}
                <div className="card overflow-hidden lg:col-span-3">
                  <div className="flex items-center gap-2 border-b border-ink-100 px-5 py-3 dark:border-ink-800">
                    <Layers size={13} className="text-ink-500 dark:text-ink-400" />
                    <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('usage.modelBreakdown')}
                    </h2>
                  </div>
                  {modelData.length === 0 ? (
                    <div className="p-5">
                      <EmptyState icon={Layers} title={t('usage.empty')} />
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-[12.5px]">
                        <thead>
                          <tr className="border-b border-ink-100 bg-ink-50/60 dark:border-ink-800 dark:bg-ink-900/40">
                            <th className="px-4 py-2 text-left font-semibold text-ink-500 dark:text-ink-400">
                              {t('common.model')}
                            </th>
                            <th className="px-4 py-2 text-left font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.provider')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.requests')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.tokens')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.costCredits')}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {modelData.map((m, idx) => (
                            <tr
                              key={m.model || idx}
                              className="border-b border-ink-50 dark:border-ink-800/50"
                            >
                              <td className="px-4 py-2 font-medium text-ink-800 dark:text-ink-200">{m.model}</td>
                              <td className="px-4 py-2 text-ink-500 dark:text-ink-400">{m.provider}</td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {formatNumber(m.requests)}
                              </td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {formatNumber(m.tokens)}
                              </td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {(m.cost_credits ?? 0).toFixed(2)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                {/* Pie chart */}
                <div className="card p-5 lg:col-span-2">
                  <div className="mb-3 flex items-center gap-2">
                    <PieIcon size={13} className="text-ink-500 dark:text-ink-400" />
                    <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('usage.costCredits')}
                    </h2>
                  </div>
                  {modelData.length === 0 ? (
                    <EmptyState icon={PieIcon} title={t('usage.empty')} />
                  ) : (
                    <div className="h-64">
                      <Doughnut
                        data={modelPieChart}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          cutout: '62%',
                          plugins: {
                            legend: {
                              position: 'bottom',
                              labels: { boxWidth: 10, font: { size: 11 } },
                            },
                          },
                        }}
                      />
                    </div>
                  )}
                </div>
              </div>

              {/* Section 4: Recent activity (paginated table) */}
              <div className="card overflow-hidden">
                <div className="flex items-center gap-2 border-b border-ink-100 px-5 py-3 dark:border-ink-800">
                  <Clock size={13} className="text-ink-500 dark:text-ink-400" />
                  <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('usage.recentActivity')}
                  </h2>
                </div>
                {logs.length === 0 ? (
                  <div className="p-5">
                    <EmptyState
                      icon={Clock}
                      title={t('usage.empty')}
                      description={t('usage.emptyHint')}
                    />
                  </div>
                ) : (
                  <>
                    <div className="overflow-x-auto">
                      <table className="w-full text-[12.5px]">
                        <thead>
                          <tr className="border-b border-ink-100 bg-ink-50/60 dark:border-ink-800 dark:bg-ink-900/40">
                            <th className="px-4 py-2 text-left font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.timestamp')}
                            </th>
                            <th className="px-4 py-2 text-left font-semibold text-ink-500 dark:text-ink-400">
                              {t('common.model')}
                            </th>
                            <th className="px-4 py-2 text-left font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.provider')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.promptTokens')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.completionTokens')}
                            </th>
                            <th className="px-4 py-2 text-right font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.costCredits')}
                            </th>
                            <th className="px-4 py-2 text-center font-semibold text-ink-500 dark:text-ink-400">
                              {t('usage.statusCode')}
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {paginatedLogs.map((log, idx) => (
                            <tr key={idx} className="border-b border-ink-50 dark:border-ink-800/50">
                              <td className="whitespace-nowrap px-4 py-2 text-ink-500 dark:text-ink-400">
                                {log.request_time
                                  ? new Date(log.request_time).toLocaleString()
                                  : '-'}
                              </td>
                              <td className="px-4 py-2 font-medium text-ink-800 dark:text-ink-200">
                                {log.model || '-'}
                              </td>
                              <td className="px-4 py-2 text-ink-500 dark:text-ink-400">
                                {log.metadata?.provider || log.endpoint?.split('/')[1] || '-'}
                              </td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {formatNumber(log.prompt_tokens ?? 0)}
                              </td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {formatNumber(log.completion_tokens ?? 0)}
                              </td>
                              <td className="px-4 py-2 text-right font-mono text-ink-700 dark:text-ink-300">
                                {log.metadata?.cost ? Number(log.metadata.cost).toFixed(2) : '-'}
                              </td>
                              <td className="px-4 py-2 text-center">
                                <StatusBadge code={log.status_code} />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {/* Pagination */}
                    {totalLogPages > 1 && (
                      <div className="flex items-center justify-between border-t border-ink-100 px-5 py-2.5 dark:border-ink-800">
                        <span className="text-[12px] text-ink-500 dark:text-ink-400">
                          {logPage + 1} / {totalLogPages}
                        </span>
                        <div className="flex gap-1">
                          <button
                            disabled={logPage === 0}
                            onClick={() => setLogPage((p) => Math.max(0, p - 1))}
                            className="rounded p-1 text-ink-500 dark:text-ink-400 transition hover:bg-ink-100 dark:hover:bg-ink-800 disabled:opacity-30"
                          >
                            <ChevronLeft size={14} />
                          </button>
                          <button
                            disabled={logPage >= totalLogPages - 1}
                            onClick={() => setLogPage((p) => Math.min(totalLogPages - 1, p + 1))}
                            className="rounded p-1 text-ink-500 dark:text-ink-400 transition hover:bg-ink-100 dark:hover:bg-ink-800 disabled:opacity-30"
                          >
                            <ChevronRight size={14} />
                          </button>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
