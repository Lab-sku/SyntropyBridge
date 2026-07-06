import { useEffect, useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins, Zap, Users as UsersIcon, ServerCog, TrendingUp, ArrowUpRight } from 'lucide-react';
import { Bar, Doughnut } from 'react-chartjs-2';
import api from '@/lib/api';
import { formatNumber, formatTokens } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import { CardSkeleton } from '@/components/Skeleton';
import { chartTheme } from '@/lib/chart';

export default function Billing() {
  const { t } = useTranslation();
  const [stats, setStats] = useState(null);
  const [trend, setTrend] = useState({ labels: [], values: [] });
  const [logs, setLogs] = useState([]);
  const [models, setModels] = useState([]);
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getStats().catch(() => ({})),
      api.getAdminTrend('7d').catch(() => []),
      api.getRecentLogs().catch(() => []),
      api.getModels().catch(() => []),
      api.getUsers().catch(() => []),
    ])
      .then(([s, trendData, l, m, u]) => {
        if (!alive) return;
        setStats(s || {});
        const trendRows = Array.isArray(trendData) ? trendData : [];
        setTrend({
          labels: trendRows.map((r) => r.date || ''),
          values: trendRows.map((r) => Number(r.requests || 0)),
        });
        setLogs(Array.isArray(l) ? l : []);
        const ml = Array.isArray(m) ? m : m?.models || [];
        setModels(ml);
        setUsers(Array.isArray(u) ? u : []);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const kpis = useMemo(() => {
    const s = stats || {};
    const activeUsers = users.filter((u) => u.is_active).length;
    const activeProviders = new Set(models.map((m) => m.provider)).size;
    return [
      {
        label: t('billing.kpi.totalTokens'),
        value: formatTokens(s.total_tokens ?? 0),
        icon: Coins,
        tone: 'text-indigo-600',
      },
      {
        label: t('billing.kpi.weekRequests'),
        value: formatNumber(s.week_requests ?? s.today_requests ?? 0),
        icon: Zap,
        tone: 'text-amber-600',
      },
      {
        label: t('billing.kpi.activeUsers'),
        value: activeUsers,
        icon: UsersIcon,
        tone: 'text-emerald-600',
      },
      {
        label: t('billing.kpi.activeProviders'),
        value: activeProviders,
        icon: ServerCog,
        tone: 'text-sky-600',
      },
    ];
  }, [stats, users, models]);

  const providerPie = useMemo(() => {
    const map = {};
    for (const l of logs) {
      const k = (l.provider || l.model || 'unknown').split(':')[0] || 'unknown';
      map[k] = (map[k] || 0) + (l.total_tokens || 0);
    }
    const entries = Object.entries(map)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
    return { labels: entries.map(([k]) => k), data: entries.map(([, v]) => v) };
  }, [logs]);

  const modelUsage = useMemo(() => {
    const map = {};
    for (const l of logs) {
      const k = l.model || '—';
      map[k] = (map[k] || 0) + (l.total_tokens || 0);
    }
    return Object.entries(map)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
  }, [logs]);

  const topUsers = useMemo(() => {
    return [...users].sort((a, b) => (b.usage_week || 0) - (a.usage_week || 0)).slice(0, 5);
  }, [users]);

  return (
    <>
      <TopBar title={t('billing.title')} subtitle={t('billing.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-7xl space-y-4 p-4 md:p-6">
          {loading ? (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <CardSkeleton key={i} />
              ))}
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                {kpis.map((k) => (
                  <div key={k.label} className="kpi">
                    <div
                      className={`flex h-9 w-9 items-center justify-center rounded-lg bg-ink-100/60 dark:bg-ink-800/60 ${k.tone}`}
                    >
                      <k.icon size={16} strokeWidth={2.2} />
                    </div>
                    <div className="mt-3 text-[24px] font-semibold tracking-tight text-ink-900 dark:text-ink-100">
                      {k.value}
                    </div>
                    <div className="text-[11.5px] text-ink-500 dark:text-ink-400">{k.label}</div>
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="card p-5 lg:col-span-2">
                  <div className="mb-3 flex items-center justify-between">
                    <div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('billing.trend.title')}
                      </div>
                      <div className="text-[11.5px] text-ink-500 dark:text-ink-400">
                        {t('billing.trend.last7Days')}
                      </div>
                    </div>
                    <span className="rounded-full bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5 text-[10.5px] font-medium text-ink-600 dark:text-ink-400">
                      <TrendingUp size={10} className="mr-0.5 inline" />
                      {t('common.realtime')}
                    </span>
                  </div>
                  <div className="h-[240px]">
                    <Bar
                      data={{
                        labels: Array.isArray(trend.labels) ? trend.labels : [],
                        datasets: [
                          {
                            data: Array.isArray(trend.values) ? trend.values : [],
                            backgroundColor: '#18181b',
                            hoverBackgroundColor: '#27272a',
                            borderRadius: 6,
                            borderSkipped: false,
                            barThickness: 22,
                          },
                        ],
                      }}
                      options={chartTheme()}
                    />
                  </div>
                </div>

                <div className="card p-5">
                  <div className="mb-3">
                    <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('billing.providerPie.title')}
                    </div>
                    <div className="text-[11.5px] text-ink-500 dark:text-ink-400">
                      {t('billing.providerPie.recentRequests')}
                    </div>
                  </div>
                  <div className="h-[240px]">
                    {providerPie.data.length === 0 ? (
                      <div className="flex h-full items-center justify-center text-[12px] text-ink-400 dark:text-ink-500">
                        {t('common.noData')}
                      </div>
                    ) : (
                      <Doughnut
                        data={{
                          labels: providerPie.labels,
                          datasets: [
                            {
                              data: providerPie.data,
                              backgroundColor: [
                                '#18181b',
                                '#3f3f46',
                                '#52525b',
                                '#71717a',
                                '#4f46e5',
                                '#0ea5e9',
                                '#10b981',
                                '#f59e0b',
                              ],
                              borderWidth: 0,
                              hoverOffset: 4,
                            },
                          ],
                        }}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          cutout: '64%',
                          plugins: {
                            legend: {
                              position: 'right',
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

              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div className="card p-5">
                  <div className="mb-3">
                    <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('billing.modelUsage.title')}
                    </div>
                    <div className="text-[11.5px] text-ink-500 dark:text-ink-400">
                      {t('billing.modelUsage.totalTokens')}
                    </div>
                  </div>
                  {modelUsage.length === 0 ? (
                    <div className="py-8 text-center text-[12px] text-ink-400 dark:text-ink-500">
                      {t('common.noData')}
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {modelUsage.map(([model, tokens], i) => {
                        const max = modelUsage[0][1] || 1;
                        const pct = (tokens / max) * 100;
                        return (
                          <div key={model} className="space-y-1">
                            <div className="flex items-center justify-between text-[12px]">
                              <span className="flex items-center gap-1.5 truncate font-mono text-ink-700 dark:text-ink-300">
                                <span className="text-[10.5px] text-ink-400 dark:text-ink-500">#{i + 1}</span>
                                {model}
                              </span>
                              <span className="font-mono text-[11px] font-medium text-ink-900 dark:text-ink-100">
                                {formatTokens(tokens)}
                              </span>
                            </div>
                            <div className="h-1.5 overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                              <div
                                className="h-full rounded-full bg-ink-900 dark:bg-ink-100"
                                style={{ width: pct + '%' }}
                              />
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                <div className="card p-5">
                  <div className="mb-3">
                    <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                      {t('billing.userRanking.title')}
                    </div>
                    <div className="text-[11.5px] text-ink-500 dark:text-ink-400">
                      {t('billing.userRanking.weekConsumption')}
                    </div>
                  </div>
                  {topUsers.length === 0 ? (
                    <div className="py-8 text-center text-[12px] text-ink-400 dark:text-ink-500">
                      {t('billing.userRanking.noUsers')}
                    </div>
                  ) : (
                    <div className="divide-y divide-ink-100 dark:divide-ink-800">
                      {topUsers.map((u, i) => {
                        const max = topUsers[0]?.usage_week || 1;
                        const pct = ((u.usage_week || 0) / max) * 100;
                        return (
                          <div key={u.id} className="flex items-center gap-3 py-2.5">
                            <span className="w-5 text-center text-[11px] font-mono text-ink-400 dark:text-ink-500">
                              {i + 1}
                            </span>
                            <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-ink-700 to-ink-900 text-[11px] font-semibold text-white">
                              {u.username?.charAt(0).toUpperCase() || '?'}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
                                {u.username}
                              </div>
                              <div className="mt-1 h-1 overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                                <div
                                  className="h-full rounded-full bg-ink-700 dark:bg-ink-300"
                                  style={{ width: pct + '%' }}
                                />
                              </div>
                            </div>
                            <div className="text-right font-mono text-[11px] font-medium text-ink-700 dark:text-ink-300">
                              {formatNumber(u.usage_week || 0)}
                            </div>
                          </div>
                        );
                      })}
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
