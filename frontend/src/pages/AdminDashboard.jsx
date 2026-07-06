import { useEffect, useState, useMemo, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Users,
  Zap,
  Coins,
  Timer,
  TrendingUp,
  ArrowUpRight,
  ServerCog,
  PlugZap,
  KeyRound,
  Activity,
  DollarSign,
  CreditCard,
  Sparkles,
  BarChart3,
  Shield,
  Clock,
  AlertTriangle,
} from 'lucide-react';
import { Bar, Doughnut, Line } from 'react-chartjs-2';
import api from '@/lib/api';
import { formatNumber, formatTokens, timeAgo } from '@/lib/utils';
import { CardSkeleton, TableRowSkeleton } from '@/components/Skeleton';
import TopBar from '@/components/TopBar';
import { chartTheme } from '@/lib/chart';

const RANGES = [
  { id: '1d', label: '24h' },
  { id: '7d', label: '7d' },
  { id: '30d', label: '30d' },
  { id: 'mtd', label: 'MTD' },
];

export default function AdminDashboard() {
  const { t } = useTranslation();
  const [stats, setStats] = useState(null);
  const [overview, setOverview] = useState(null);
  const [trend, setTrend] = useState({ labels: [], values: [] });
  const [revenue, setRevenue] = useState([]);
  const [topModels, setTopModels] = useState([]);
  const [topUsers, setTopUsers] = useState([]);
  const [providerBreakdown, setProviderBreakdown] = useState([]);
  const [logs, setLogs] = useState([]);
  const [providers, setProviders] = useState([]);
  const [models, setModels] = useState([]);
  const [reconSummary, setReconSummary] = useState(null);
  // L16: System health — DB pool stats + provider health.
  const [systemHealth, setSystemHealth] = useState(null);
  const [providerHealth, setProviderHealth] = useState([]);
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState('7d');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      api.getStats().catch(() => ({})),
      api.getAdminOverview(range).catch(() => ({})),
      api.getAdminTrend(range).catch(() => []),
      api.getAdminRevenue(range).catch(() => []),
      api.getAdminTopModels(range, 8).catch(() => []),
      api.getAdminTopUsers(range, 5).catch(() => []),
      api.getAdminProviderBreakdown(range).catch(() => []),
      api.getRecentLogs().catch(() => []),
      api.getProviders().catch(() => []),
      api.getModels().catch(() => []),
      api.getAdminReconciliationSummary(7).catch(() => null),
      // L16: system health (best-effort, non-blocking)
      api.getSystemHealth().catch(() => null),
      api.getProviderHealth().catch(() => []),
    ])
      .then(([s, ov, t, rv, tm, tu, pb, l, p, m, rs, sh, ph]) => {
        if (!alive) return;
        setStats(s || {});
        setOverview(ov || {});
        // getAdminTrend returns an array of {date, tokens, requests, cost}
        // objects — transform into {labels, values} for the Bar chart.
        const trendRows = Array.isArray(t) ? t : [];
        setTrend({
          labels: trendRows.map((r) => r.date || ''),
          values: trendRows.map((r) => Number(r.requests || 0)),
        });
        setRevenue(Array.isArray(rv) ? rv : []);
        setTopModels(Array.isArray(tm) ? tm : []);
        setTopUsers(Array.isArray(tu) ? tu : []);
        setProviderBreakdown(Array.isArray(pb) ? pb : []);
        setLogs(Array.isArray(l) ? l : []);
        setProviders(Array.isArray(p) ? p : []);
        const ml = Array.isArray(m) ? m : m?.models || [];
        setModels(ml);
        setReconSummary(rs || null);
        setSystemHealth(sh || null);
        setProviderHealth(Array.isArray(ph) ? ph : []);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [range]);

  // ---------------------------------------------------------------------
  // KPI cards (商业指标 - business KPIs)
  // ---------------------------------------------------------------------
  const kpis = useMemo(() => {
    if (!stats) return [];
    const ov = overview || {};
    const cards = [
      {
        label: t('admin.dashboard.totalUsers'),
        value: formatNumber(ov.total_users ?? stats.total_users ?? 0),
        delta:
          ov.active_users_24h != null
            ? `${ov.active_users_24h} ${t('admin.dashboard.active24h')}`
            : null,
        icon: Users,
        tone: 'from-blue-500 to-blue-600',
        bg: 'from-blue-50 to-indigo-50/50',
      },
      {
        label: t('admin.dashboard.totalRevenue'),
        value: `${formatNumber(ov.total_revenue ?? 0)} cr`,
        delta: revenue.length ? t('admin.dashboard.recentDays', { days: revenue.length }) : null,
        icon: DollarSign,
        tone: 'from-emerald-500 to-emerald-600',
        bg: 'from-emerald-50 to-green-50/50',
      },
      {
        label: t('admin.dashboard.todayRequests'),
        value: formatNumber(ov.total_requests_today ?? stats.today_requests ?? 0),
        delta: null,
        icon: Zap,
        tone: 'from-amber-500 to-orange-500',
        bg: 'from-amber-50 to-orange-50/50',
      },
      {
        label: t('admin.dashboard.todayTokens', 'Today\'s Tokens'),
        value: formatTokens(ov.total_tokens_today ?? 0),
        delta: t('admin.dashboard.todayTokenUsage', 'Today\'s consumption'),
        icon: Timer,
        tone: 'from-sky-500 to-cyan-600',
        bg: 'from-sky-50 to-cyan-50/50',
      },
      {
        label: t('admin.dashboard.todayCost'),
        value: `${formatNumber(ov.total_cost_today ?? 0)} cr`,
        delta: t('admin.dashboard.creditConsumption'),
        icon: Coins,
        tone: 'from-violet-500 to-purple-600',
        bg: 'from-violet-50 to-purple-50/50',
      },
    ];
    // Reconciliation anomaly card — only render when there is something
    // to review so the dashboard stays uncluttered on healthy deployments.
    if (reconSummary && (reconSummary.total ?? 0) > 0) {
      cards.push({
        label: t('admin.dashboard.reconAnomaly'),
        value: formatNumber(reconSummary.total ?? 0),
        delta: t('admin.dashboard.reconAnomalyDesc', {
          pending: reconSummary.pending_review ?? 0,
        }),
        icon: AlertTriangle,
        tone: 'from-rose-500 to-red-600',
        bg: 'from-rose-50 to-red-50/50',
        link: '/admin/orders?status=pending_review',
      });
    }
    return cards;
  }, [stats, overview, revenue, reconSummary]);

  const providerDist = useMemo(() => {
    if (providerBreakdown.length > 0) {
      return {
        labels: providerBreakdown.map((p) => p.provider || 'unknown'),
        data: providerBreakdown.map((p) => Number(p.tokens || p.requests || 0)),
      };
    }
    // fallback to model catalog
    const map = {};
    for (const m of models) {
      const k = m.provider || 'other';
      map[k] = (map[k] || 0) + 1;
    }
    const entries = Object.entries(map)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
    return { labels: entries.map(([k]) => k), data: entries.map(([, v]) => v) };
  }, [providerBreakdown, models]);

  // Merge trend with revenue (revenue could be sparser than requests).
  const revenueByDay = useMemo(() => {
    const map = {};
    for (const r of revenue) {
      if (r && r.d) map[r.d] = Number(r.revenue || 0);
    }
    return map;
  }, [revenue]);

  return (
    <>
      <TopBar title={t('nav.overview')} subtitle={t('admin.dashboard.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
          {/* Range selector */}
          <div className="flex items-center justify-between">
            <div className="text-[12px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
              {t('admin.dashboard.dataRange')} ·{' '}
              {RANGES.find((r) => r.id === range)?.label || range}
            </div>
            <div className="flex items-center gap-1 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 p-0.5">
              {RANGES.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setRange(r.id)}
                  className={
                    'rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors ' +
                    (range === r.id ? 'bg-ink-900 text-white dark:bg-ink-100 dark:bg-ink-800 dark:text-ink-900 dark:text-ink-100' : 'text-ink-600 hover:bg-ink-50 dark:hover:bg-ink-800')
                  }
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          {loading ? (
            <div className="space-y-6">
              <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
                {Array.from({ length: 5 }).map((_, i) => (
                  <CardSkeleton key={i} />
                ))}
              </div>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="card p-5 lg:col-span-2 h-[280px]">
                  <div className="h-full animate-pulse rounded bg-ink-100 dark:bg-ink-800/60 dark:bg-ink-800/60" />
                </div>
                <div className="card p-5 h-[280px]">
                  <div className="h-full animate-pulse rounded bg-ink-100 dark:bg-ink-800/60 dark:bg-ink-800/60" />
                </div>
              </div>
            </div>
          ) : (
            <>
              {/* KPIs */}
              <div
                className={`grid grid-cols-2 gap-4 ${
                  kpis.length > 5 ? 'md:grid-cols-6' : 'md:grid-cols-5'
                }`}
              >
                {kpis.map((k) => {
                  const inner = (
                    <div
                      className={`group relative overflow-hidden rounded-2xl border border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 bg-gradient-to-br ${k.bg} p-5 shadow-soft transition-all duration-300 hover:shadow-soft-lg hover:-translate-y-0.5`}
                    >
                      {/* 背景装饰 */}
                      <div
                        className={`absolute -right-4 -top-4 h-20 w-20 rounded-full bg-gradient-to-br ${k.tone} opacity-10 blur-xl transition-opacity group-hover:opacity-20`}
                      />

                      <div className="relative">
                        <div className="flex items-start justify-between">
                          <div
                            className={`flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br ${k.tone} text-white shadow-md transition-transform duration-300 group-hover:scale-110`}
                          >
                            <k.icon size={18} strokeWidth={2} />
                          </div>
                          {k.delta && (
                            <span className="rounded-full bg-white dark:bg-ink-900/80 dark:bg-ink-900/80 px-2 py-1 text-[10px] font-medium text-ink-600 dark:text-ink-400 dark:text-ink-500 backdrop-blur-sm shadow-soft">
                              {k.delta}
                            </span>
                          )}
                        </div>
                        <div className="mt-4">
                          <div className="text-[26px] font-bold tracking-tight text-ink-900 dark:text-ink-100 transition-colors group-hover:text-ink-800">
                            {k.value}
                          </div>
                          <div className="mt-1 flex items-center gap-1.5 text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                            <span className="h-1.5 w-1.5 rounded-full bg-gradient-to-r from-ink-400 to-ink-500" />
                            {k.label}
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                  return k.link ? (
                    <Link key={k.label} to={k.link} className="block">
                      {inner}
                    </Link>
                  ) : (
                    <div key={k.label}>{inner}</div>
                  );
                })}
              </div>

              {/* Request trend + Revenue trend */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5 lg:col-span-2">
                  <div className="mb-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 text-white">
                        <BarChart3 size={14} />
                      </div>
                      <div>
                        <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                          {t('admin.dashboard.requestTrend')}
                        </div>
                        <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                          {trend.labels?.length
                            ? t('admin.dashboard.recentDaysRequests', { days: trend.labels.length })
                            : t('common.noData')}
                        </div>
                      </div>
                    </div>
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 dark:bg-emerald-900/20 px-2.5 py-1 text-[10.5px] font-medium text-emerald-700 dark:text-emerald-400 shadow-sm">
                      <TrendingUp size={10} className="inline" />
                      {t('common.realtime')}
                    </span>
                  </div>
                  <div className="h-[230px]">
                    {Array.isArray(trend.values) && trend.values.some((v) => v > 0) ? (
                      <Bar
                        data={{
                          labels: trend.labels,
                          datasets: [
                            {
                              data: trend.values,
                              backgroundColor: '#18181b',
                              hoverBackgroundColor: '#27272a',
                              borderRadius: 6,
                              borderSkipped: false,
                              barThickness: 18,
                            },
                          ],
                        }}
                        options={chartTheme()}
                      />
                    ) : (
                      <div className="flex h-full items-center justify-center text-[12px] text-ink-400 dark:text-ink-500">
                        {t('admin.dashboard.noRequestData')}
                      </div>
                    )}
                  </div>
                </div>

                <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5">
                  <div className="mb-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-500 to-green-600 text-white">
                        <DollarSign size={14} />
                      </div>
                      <div>
                        <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                          {t('admin.dashboard.revenueTrend')}
                        </div>
                        <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                          {t('admin.dashboard.revenueTrendDesc')}
                        </div>
                      </div>
                    </div>
                    <Link
                      to="/admin/billing"
                      className="inline-flex items-center gap-1 rounded-lg bg-ink-100 dark:bg-ink-800 px-2 py-1 text-[11px] font-medium text-ink-600 transition-all hover:bg-ink-200 dark:hover:bg-ink-700 hover:text-ink-900 dark:text-ink-100 dark:hover:text-ink-100"
                    >
                      {t('admin.dashboard.details')} <ArrowUpRight size={10} />
                    </Link>
                  </div>
                  <div className="h-[230px]">
                    {revenue.length > 0 ? (
                      <Line
                        data={{
                          labels: revenue.map((r) => (r.d || '').slice(5)),
                          datasets: [
                            {
                              data: revenue.map((r) => Number(r.revenue || 0)),
                              borderColor: '#10b981',
                              backgroundColor: 'rgba(16,185,129,0.12)',
                              fill: true,
                              tension: 0.35,
                              pointRadius: 2,
                              pointBackgroundColor: '#10b981',
                              borderWidth: 1.5,
                            },
                          ],
                        }}
                        options={chartTheme()}
                      />
                    ) : (
                      <div className="flex h-full items-center justify-center text-[12px] text-ink-400 dark:text-ink-500">
                        {t('admin.dashboard.noRevenueData')}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Top models + Provider breakdown */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5 lg:col-span-2">
                  <div className="mb-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 text-white">
                        <Zap size={14} />
                      </div>
                      <div>
                        <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                          {t('admin.dashboard.topModels')}
                        </div>
                        <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                          {t('admin.dashboard.sortedByTokens')}
                        </div>
                      </div>
                    </div>
                    <Link
                      to="/admin/usage"
                      className="inline-flex items-center gap-1 rounded-lg bg-ink-100 dark:bg-ink-800 px-2 py-1 text-[11px] font-medium text-ink-600 transition-all hover:bg-ink-200 dark:hover:bg-ink-700 hover:text-ink-900 dark:text-ink-100 dark:hover:text-ink-100"
                    >
                      {t('common.all')} <ArrowUpRight size={10} />
                    </Link>
                  </div>
                  {topModels.length === 0 ? (
                    <div className="py-8 text-center text-[12.5px] text-ink-400 dark:text-ink-500">
                      {t('admin.dashboard.noModelData')}
                    </div>
                  ) : (
                    <div className="-mx-2 space-y-1">
                      {topModels.slice(0, 8).map((m, i) => {
                        const max = topModels[0]?.tokens || m.tokens || 1;
                        const pct = Math.max(4, Math.min(100, (m.tokens / max) * 100));
                        const gradients = [
                          'from-blue-500 to-indigo-500',
                          'from-emerald-500 to-green-500',
                          'from-amber-500 to-orange-500',
                          'from-violet-500 to-purple-500',
                          'from-rose-500 to-pink-500',
                          'from-cyan-500 to-teal-500',
                          'from-ink-700 to-ink-900',
                          'from-slate-500 to-zinc-500',
                        ];
                        return (
                          <div
                            key={`${m.model}-${i}`}
                            className="group/model flex items-center gap-3 rounded-xl px-3 py-2.5 transition-all hover:bg-ink-50 dark:hover:bg-ink-800/80 dark:hover:bg-ink-800/80 dark:bg-ink-800/80"
                          >
                            <span className="w-6 text-center text-[11px] font-bold text-ink-400 dark:text-ink-500 group-hover/model:text-ink-600 dark:group-hover/model:text-ink-400 dark:text-ink-500">
                              {i + 1}
                            </span>
                            <div className="min-w-0 flex-1">
                              <div className="truncate font-mono text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
                                {m.model || m.model_id}
                              </div>
                              <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800/80 dark:bg-ink-800/80">
                                <div
                                  className={`h-full rounded-full bg-gradient-to-r ${gradients[i % gradients.length]} transition-all duration-500`}
                                  style={{ width: `${pct}%` }}
                                />
                              </div>
                            </div>
                            <div className="w-24 text-right text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                              <div className="font-mono tabular-nums font-medium text-ink-900 dark:text-ink-100">
                                {formatTokens(m.tokens || 0)}
                              </div>
                              <div>
                                {m.requests || 0} {t('admin.dashboard.times')}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5">
                  <div className="mb-4 flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-500 to-sky-600 text-white">
                      <ServerCog size={14} />
                    </div>
                    <div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('admin.dashboard.platformUsage')}
                      </div>
                      <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                        {t('admin.dashboard.byTokenShare')}
                      </div>
                    </div>
                  </div>
                  <div className="h-[220px]">
                    {providerDist.data.length === 0 ? (
                      <div className="flex h-full items-center justify-center text-[12px] text-ink-400 dark:text-ink-500">
                        {t('common.noData')}
                      </div>
                    ) : (
                      <Doughnut
                        data={{
                          labels: providerDist.labels,
                          datasets: [
                            {
                              data: providerDist.data,
                              backgroundColor: [
                                '#18181b',
                                '#3f3f46',
                                '#52525b',
                                '#71717a',
                                '#a1a1aa',
                                '#4f46e5',
                                '#0ea5e9',
                                '#10b981',
                                '#f59e0b',
                                '#ef4444',
                              ],
                              borderWidth: 0,
                              hoverOffset: 4,
                            },
                          ],
                        }}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          cutout: '68%',
                          plugins: {
                            legend: {
                              position: 'right',
                              align: 'center',
                              labels: {
                                boxWidth: 8,
                                boxHeight: 8,
                                usePointStyle: true,
                                font: { size: 10.5, family: 'Inter' },
                                color: '#52525b',
                              },
                            },
                            tooltip: chartTheme().plugins.tooltip,
                          },
                        }}
                      />
                    )}
                  </div>
                </div>
              </div>

              {/* Top users + Quick links */}
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5 lg:col-span-2">
                  <div className="mb-4 flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-rose-500 to-pink-600 text-white">
                        <Users size={14} />
                      </div>
                      <div>
                        <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                          {t('admin.dashboard.activeUsers')}
                        </div>
                        <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                          {t('admin.dashboard.sortedByCredits')}
                        </div>
                      </div>
                    </div>
                    <Link
                      to="/admin/users"
                      className="inline-flex items-center gap-1 rounded-lg bg-ink-100 dark:bg-ink-800 px-2 py-1 text-[11px] font-medium text-ink-600 transition-all hover:bg-ink-200 dark:hover:bg-ink-700 hover:text-ink-900 dark:text-ink-100 dark:hover:text-ink-100"
                    >
                      {t('common.all')} <ArrowUpRight size={10} />
                    </Link>
                  </div>
                  {topUsers.length === 0 ? (
                    <div className="py-8 text-center text-[12.5px] text-ink-400 dark:text-ink-500">
                      {t('admin.dashboard.noUserData')}
                    </div>
                  ) : (
                    <div className="-mx-2 space-y-1">
                      {topUsers.map((u, i) => {
                        const avatarGradients = [
                          'from-blue-500 to-indigo-600',
                          'from-emerald-500 to-green-600',
                          'from-amber-500 to-orange-600',
                          'from-violet-500 to-purple-600',
                          'from-rose-500 to-pink-600',
                        ];
                        return (
                          <div
                            key={`${u.user_id || u.username || i}`}
                            className="group/user flex items-center gap-3 rounded-xl px-3 py-2.5 transition-all hover:bg-ink-50 dark:hover:bg-ink-800/80 dark:hover:bg-ink-800/80 dark:bg-ink-800/80"
                          >
                            <div
                              className={`flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br ${avatarGradients[i % avatarGradients.length]} text-[11px] font-bold text-white shadow-sm`}
                            >
                              {(u.username || 'U').slice(0, 1).toUpperCase()}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-[12.5px] font-semibold text-ink-900 dark:text-ink-100">
                                {u.username || u.email || `user #${u.user_id}`}
                              </div>
                              <div className="truncate text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                                {u.requests || 0} {t('admin.dashboard.requests')}
                              </div>
                            </div>
                            <div className="text-right text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                              <div className="font-mono tabular-nums font-semibold text-ink-900 dark:text-ink-100">
                                {formatTokens(u.tokens || 0)}
                              </div>
                              <div>{formatNumber(Math.round(u.cost || 0))} cr</div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                <div className="space-y-3">
                  <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5">
                    <div className="mb-3 flex items-center gap-2">
                      <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-ink-700 to-ink-900 text-white">
                        <Sparkles size={12} />
                      </div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('admin.dashboard.quickLinks')}
                      </div>
                    </div>
                    <div className="space-y-1">
                      {[
                        {
                          to: '/admin/providers',
                          label: t('admin.dashboard.providers'),
                          desc: `${providers.length} ${t('admin.dashboard.builtInPlatforms')}`,
                          icon: ServerCog,
                          color: 'from-blue-500 to-indigo-600',
                        },
                        {
                          to: '/admin/custom-providers',
                          label: t('admin.dashboard.customProviders'),
                          desc: t('admin.dashboard.openaiCompat'),
                          icon: PlugZap,
                          color: 'from-amber-500 to-orange-600',
                        },
                        {
                          to: '/admin/users',
                          label: t('admin.dashboard.userManagement'),
                          desc: `${stats?.total_users ?? 0} ${t('admin.dashboard.users')}`,
                          icon: Users,
                          color: 'from-emerald-500 to-green-600',
                        },
                        {
                          to: '/admin/subscriptions',
                          label: t('admin.dashboard.subscriptionApproval'),
                          desc: t('admin.dashboard.pendingRequests'),
                          icon: KeyRound,
                          color: 'from-violet-500 to-purple-600',
                        },
                        {
                          to: '/admin/redeem-codes',
                          label: t('admin.dashboard.redeemCodes'),
                          desc: t('admin.dashboard.issueCredits'),
                          icon: CreditCard,
                          color: 'from-rose-500 to-pink-600',
                        },
                        {
                          to: '/admin/pricing',
                          label: t('admin.dashboard.modelPricing'),
                          desc: t('admin.dashboard.creditPrice'),
                          icon: Sparkles,
                          color: 'from-cyan-500 to-teal-600',
                        },
                      ].map((it) => (
                        <Link
                          key={it.to}
                          to={it.to}
                          className="group/link flex items-center gap-3 rounded-xl p-2.5 transition-all hover:bg-ink-50 dark:hover:bg-ink-800/80 dark:hover:bg-ink-800/80 dark:bg-ink-800/80 hover:shadow-soft"
                        >
                          <div
                            className={`flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br ${it.color} text-white shadow-sm transition-transform duration-200 group-hover/link:scale-110`}
                          >
                            <it.icon size={14} />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="text-[12.5px] font-medium text-ink-900 dark:text-ink-100">{it.label}</div>
                            <div className="text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">{it.desc}</div>
                          </div>
                          <ArrowUpRight
                            size={12}
                            className="text-ink-400 dark:text-ink-500 transition-all group-hover/link:translate-x-0.5 group-hover/link:-translate-y-0.5 group-hover/link:text-ink-600 dark:group-hover/link:text-ink-400 dark:text-ink-500"
                          />
                        </Link>
                      ))}
                    </div>
                  </div>
                  <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5">
                    <div className="mb-3 flex items-center gap-2">
                      <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-500 to-green-600 text-white">
                        <Activity size={13} />
                      </div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('admin.dashboard.systemStatus')}
                      </div>
                    </div>
                    <div className="space-y-2 text-[12px]">
                      {[
                        {
                          label: t('admin.dashboard.apiService'),
                          value: t('admin.dashboard.running'),
                          tone: 'text-emerald-600',
                          icon: Shield,
                        },
                        {
                          label: t('admin.dashboard.modelSync'),
                          value: t('admin.dashboard.loaded'),
                          tone: 'text-ink-700',
                          icon: Zap,
                        },
                        {
                          label: t('admin.dashboard.rateLimit'),
                          value: t('admin.dashboard.loaded'),
                          tone: 'text-ink-700',
                          icon: Timer,
                        },
                        {
                          label: t('admin.dashboard.auditLog'),
                          value: t('admin.dashboard.loaded'),
                          tone: 'text-ink-700',
                          icon: Activity,
                        },
                        {
                          label: t('admin.dashboard.pricingActive'),
                          value: t('admin.dashboard.loaded'),
                          tone: 'text-ink-700',
                          icon: Coins,
                        },
                      ].map((it) => (
                        <div
                          key={it.label}
                          className="flex items-center justify-between rounded-lg bg-ink-50/60 dark:bg-ink-900/60 px-3 py-2"
                        >
                          <div className="flex items-center gap-2">
                            <it.icon size={12} className="text-ink-400 dark:text-ink-500" />
                            <span className="text-ink-500 dark:text-ink-400 dark:text-ink-500">{it.label}</span>
                          </div>
                          <span className={`flex items-center gap-1.5 font-medium ${it.tone}`}>
                            <span className="h-1.5 w-1.5 rounded-full bg-current" />
                            {it.value}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* Recent logs */}
              <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-slate-500 to-zinc-600 text-white">
                      <Clock size={14} />
                    </div>
                    <div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('admin.dashboard.recentLogs')}
                      </div>
                      <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                        {t('admin.dashboard.recent8')}
                      </div>
                    </div>
                  </div>
                  <Link
                    to="/admin/logs"
                    className="inline-flex items-center gap-1 rounded-lg bg-ink-100 dark:bg-ink-800 px-2.5 py-1.5 text-[11.5px] font-medium text-ink-600 transition-all hover:bg-ink-200 dark:hover:bg-ink-700 hover:text-ink-900 dark:text-ink-100 dark:hover:text-ink-100"
                  >
                    {t('common.all')} <ArrowUpRight size={11} />
                  </Link>
                </div>
                <div>
                  {logs.length === 0 ? (
                    <div className="py-8 text-center text-[12.5px] text-ink-400 dark:text-ink-500">
                      {t('common.noData')}
                    </div>
                  ) : (
                    <div className="-mx-2 space-y-1">
                      {logs.slice(0, 8).map((l, i) => (
                        <div
                          key={i}
                          className="group/log flex items-center gap-3 rounded-xl px-3 py-2.5 transition-all hover:bg-ink-50 dark:hover:bg-ink-800/80 dark:hover:bg-ink-800/80 dark:bg-ink-800/80"
                        >
                          <div
                            className={`h-2 w-2 shrink-0 rounded-full shadow-sm ${
                              l.status_code === 200
                                ? 'bg-emerald-500 ring-2 ring-emerald-100'
                                : 'bg-rose-500 ring-2 ring-rose-100'
                            }`}
                          />
                          <div className="min-w-0 flex-1 text-[12.5px]">
                            <span className="font-semibold text-ink-900 dark:text-ink-100">{l.username || '—'}</span>
                            <span className="ml-2 rounded bg-ink-100 dark:bg-ink-800/80 dark:bg-ink-800/80 px-1.5 py-0.5 font-mono text-[10px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                              {l.model || l.endpoint}
                            </span>
                          </div>
                          <div className="hidden text-right text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500 sm:block">
                            <div className="font-medium text-ink-900 dark:text-ink-100">
                              {l.total_tokens ?? 0} <span className="text-ink-400 dark:text-ink-500">tokens</span>
                            </div>
                          </div>
                          <div className="text-right text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                            <div className="font-medium text-ink-900 dark:text-ink-100">
                              {l.response_time_ms}
                              <span className="text-ink-400 dark:text-ink-500">ms</span>
                            </div>
                          </div>
                          <div className="w-20 text-right text-[10.5px] text-ink-400 dark:text-ink-500">
                            {timeAgo(l.request_time)}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* L16: System health — DB pool + provider health */}
              <div className="card overflow-hidden rounded-2xl border-ink-200 dark:border-ink-700/40 shadow-soft-lg p-5">
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-teal-500 to-cyan-600 text-white">
                      <Activity size={14} />
                    </div>
                    <div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('admin.dashboard.systemHealth')}
                      </div>
                      <div className="text-[11.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">
                        {t('admin.dashboard.systemHealthDesc')}
                      </div>
                    </div>
                  </div>
                </div>
                <div className="space-y-3">
                  {/* DB connection pool */}
                  {systemHealth && typeof systemHealth.in_use === 'number' ? (
                    <div>
                      <div className="mb-1 flex items-center justify-between text-[12px]">
                        <span className="text-ink-600 dark:text-ink-400">
                          {t('admin.dashboard.dbPool')}
                        </span>
                        <span className="font-mono text-ink-900 dark:text-ink-100">
                          {systemHealth.in_use}/{systemHealth.max_size}
                        </span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                        <div
                          className={`h-full rounded-full transition-all ${
                            systemHealth.in_use / systemHealth.max_size > 0.8
                              ? 'bg-rose-500'
                              : systemHealth.in_use / systemHealth.max_size > 0.5
                                ? 'bg-amber-500'
                                : 'bg-emerald-500'
                          }`}
                          style={{
                            width: `${Math.min((systemHealth.in_use / systemHealth.max_size) * 100, 100)}%`,
                          }}
                        />
                      </div>
                    </div>
                  ) : (
                    <div className="text-[12px] text-ink-400 dark:text-ink-500">
                      {t('admin.dashboard.dbPoolUnavailable')}
                    </div>
                  )}
                  {/* Provider health */}
                  {providerHealth.length > 0 && (
                    <div>
                      <div className="mb-1.5 flex items-center justify-between text-[12px]">
                        <span className="text-ink-600 dark:text-ink-400">
                          {t('admin.dashboard.providerHealth')}
                        </span>
                        <span className="font-mono text-ink-900 dark:text-ink-100">
                          {providerHealth.filter((p) => p.up !== false).length}/{providerHealth.length}{' '}
                          {t('admin.dashboard.up')}
                        </span>
                      </div>
                      {providerHealth.some((p) => p.up === false) && (
                        <div className="flex flex-wrap gap-1">
                          {providerHealth
                            .filter((p) => p.up === false)
                            .map((p) => (
                              <span
                                key={p.provider}
                                className="rounded bg-rose-100 dark:bg-rose-900/30 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-400"
                              >
                                {p.provider}
                              </span>
                            ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
