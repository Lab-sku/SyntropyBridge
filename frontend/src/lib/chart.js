import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
  Filler,
);

const LIGHT_GRID = '#f4f4f5';
const LIGHT_TICK = '#a1a1aa';
const DARK_GRID = '#27272a';
const DARK_TICK = '#71717a';

/**
 * Detect the current colour theme from the DOM. Returns ``'dark'`` when
 * the ``<html>`` element carries ``data-theme="dark"`` *or* the browser
 * reports ``prefers-color-scheme: dark`` and no explicit override is set.
 */
function _detectMode() {
  if (typeof document === 'undefined') return 'light';
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'dark') return 'dark';
  if (attr === 'light') return 'light';
  // No explicit attribute — fall back to the OS preference.
  if (typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches) {
    return 'dark';
  }
  return 'light';
}

/**
 * Return Chart.js options tuned for the current colour mode.
 *
 * @param {'light'|'dark'|'auto'} [mode='auto']  When ``'auto'`` (the
 *   default) the mode is read from the DOM at call time so callers
 *   that omit the argument still get dark-mode-aware options.
 */
export function chartTheme(mode = 'auto') {
  const resolved = mode === 'auto' ? _detectMode() : mode;
  const isDark = resolved === 'dark';
  const grid = isDark ? DARK_GRID : LIGHT_GRID;
  const tick = isDark ? DARK_TICK : LIGHT_TICK;
  const tooltipBg = isDark ? '#27272a' : '#18181b';
  const tooltipBorder = isDark ? '#3f3f46' : '#27272a';

  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: tooltipBg,
        titleFont: { size: 11, family: 'Inter' },
        bodyFont: { size: 11.5, family: 'Inter' },
        padding: 8,
        cornerRadius: 8,
        displayColors: false,
        borderColor: tooltipBorder,
        borderWidth: 1,
      },
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { color: tick, font: { size: 10.5, family: 'Inter' } },
        border: { display: false },
      },
      y: {
        beginAtZero: true,
        grid: { color: grid, drawBorder: false },
        ticks: { color: tick, font: { size: 10.5, family: 'Inter' } },
        border: { display: false },
      },
    },
  };
}

/**
 * Build chart.js data + options for the usage dashboard line chart.
 *
 * @param {Array<{date: string, requests: number, tokens: number, cost_credits: number}>} data
 *   Daily bucketed usage rows (ascending date order).
 * @param {'light'|'dark'|'auto'} [mode='auto']
 *   Colour mode forwarded to :func:`chartTheme`.
 * @returns {{ data: object, options: object }}
 *   Ready to spread into ``<Line data={...} options={...} />``.
 */
export function buildUsageChart(data, mode = 'auto') {
  const labels = (data || []).map((d) => d.date);
  const requests = (data || []).map((d) => d.requests ?? 0);
  const costs = (data || []).map((d) => d.cost_credits ?? 0);

  const baseOpts = chartTheme(mode);

  return {
    data: {
      labels,
      datasets: [
        {
          label: 'Requests',
          data: requests,
          borderColor: '#6366f1',
          backgroundColor: 'rgba(99, 102, 241, 0.12)',
          fill: true,
          tension: 0.35,
          pointRadius: 2,
          pointHoverRadius: 5,
          yAxisID: 'y',
        },
        {
          label: 'Cost (credits)',
          data: costs,
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34, 197, 94, 0.10)',
          fill: false,
          tension: 0.35,
          pointRadius: 2,
          pointHoverRadius: 5,
          yAxisID: 'y1',
        },
      ],
    },
    options: {
      ...baseOpts,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        ...baseOpts.plugins,
        legend: {
          display: true,
          position: 'top',
          labels: { boxWidth: 10, font: { size: 11, family: 'Inter' } },
        },
        tooltip: {
          ...baseOpts.plugins.tooltip,
          displayColors: true,
          callbacks: {
            title: (items) => items[0]?.label || '',
            label: (ctx) => {
              const idx = ctx.dataIndex;
              const row = (data || [])[idx];
              if (!row) return ctx.dataset.label;
              if (ctx.datasetIndex === 0) {
                return [
                  `Requests: ${row.requests}`,
                  `Tokens: ${(row.tokens || 0).toLocaleString()}`,
                ];
              }
              return `Cost: ${row.cost_credits?.toFixed(2)} credits`;
            },
          },
        },
      },
      scales: {
        x: baseOpts.scales.x,
        y: {
          ...baseOpts.scales.y,
          position: 'left',
          title: { display: true, text: 'Requests', font: { size: 11 } },
        },
        y1: {
          ...baseOpts.scales.y,
          position: 'right',
          title: { display: true, text: 'Cost (credits)', font: { size: 11 } },
          grid: { drawOnChartArea: false },
        },
      },
    },
  };
}
