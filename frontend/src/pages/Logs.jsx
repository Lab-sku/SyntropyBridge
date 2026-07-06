import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, ScrollText } from 'lucide-react';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Badge from '@/components/Badge';
import EmptyState from '@/components/EmptyState';
import { formatDate } from '@/lib/utils';

export default function Logs() {
  const { t } = useTranslation();
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');

  useEffect(() => {
    api
      .getRecentLogs()
      .then((d) => setLogs(Array.isArray(d) ? d : []))
      .finally(() => setLoading(false));
  }, []);

  const filtered = logs.filter((l) => {
    if (!q) return true;
    return JSON.stringify(l).toLowerCase().includes(q.toLowerCase());
  });

  return (
    <>
      <TopBar
        title={t('logs.title')}
        subtitle={t('logs.subtitle')}
        action={
          <div className="flex h-8 items-center gap-1.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5">
            <Search size={12} className="text-ink-400 dark:text-ink-500" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('common.search')}
              className="w-32 bg-transparent text-[12.5px] text-ink-900 dark:text-ink-100 placeholder-ink-400 outline-none"
            />
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-7xl p-4 md:p-6">
          <div className="card overflow-hidden">
            {loading ? (
              <div className="p-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="h-10 animate-pulse rounded bg-ink-100/60 dark:bg-ink-800/60" />
                ))}
              </div>
            ) : filtered.length === 0 ? (
              <div className="p-8">
                <EmptyState
                  icon={ScrollText}
                  title={t('logs.empty.title')}
                  description={t('logs.empty.description')}
                />
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-ink-100 dark:border-ink-800 bg-ink-50/60 dark:bg-ink-900/60 text-left text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400">
                      <th className="px-4 py-2">{t('common.user')}</th>
                      <th className="px-4 py-2">{t('logs.endpoint')}</th>
                      <th className="px-4 py-2">{t('common.model')}</th>
                      <th className="px-4 py-2 text-right">{t('logs.tokens')}</th>
                      <th className="px-4 py-2 text-right">{t('logs.latency')}</th>
                      <th className="px-4 py-2">{t('common.status')}</th>
                      <th className="px-4 py-2">{t('logs.time')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((l, i) => (
                      <tr
                        key={i}
                        className="border-b border-ink-100 dark:border-ink-800 text-[12.5px] transition-colors last:border-b-0 hover:bg-ink-50/60 dark:hover:bg-ink-900/60"
                      >
                        <td className="px-4 py-2.5 font-medium text-ink-900 dark:text-ink-100">
                          {l.username || '—'}
                        </td>
                        <td className="px-4 py-2.5">
                          <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
                            {l.endpoint}
                          </code>
                        </td>
                        <td className="px-4 py-2.5">
                          <code className="rounded bg-ink-100 dark:bg-ink-800 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-700 dark:text-ink-300">
                            {l.model || '—'}
                          </code>
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-ink-700 dark:text-ink-300">
                          {l.total_tokens ?? 0}
                        </td>
                        <td className="px-4 py-2.5 text-right font-mono text-ink-700 dark:text-ink-300">
                          {l.response_time_ms}ms
                        </td>
                        <td className="px-4 py-2.5">
                          {l.status_code === 200 ? (
                            <Badge variant="success" dot>
                              200
                            </Badge>
                          ) : (
                            <Badge variant="danger" dot>
                              {l.status_code}
                            </Badge>
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-[10.5px] text-ink-500 dark:text-ink-400">
                          {formatDate(l.request_time)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
