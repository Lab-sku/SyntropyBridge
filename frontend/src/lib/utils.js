import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';
import i18next from 'i18next';

export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

/**
 * Returns the current locale from i18next, falling back to 'zh-CN'.
 */
function _locale() {
  const lang = i18next.language || 'zh';
  if (lang.startsWith('en')) return 'en-US';
  if (lang.startsWith('zh')) return 'zh-CN';
  return lang;
}

export function formatNumber(n) {
  if (n === null || n === undefined) return '0';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

export function formatTokens(n) {
  if (n === null || n === undefined) return '0';
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + 'B';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

export function formatDate(input, opts = {}) {
  if (!input) return '';
  const d = typeof input === 'string' || typeof input === 'number' ? new Date(input) : input;
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(_locale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...opts,
  });
}

/**
 * Format a timestamp as a relative "time ago" string.
 *
 * When a translation function ``t`` is passed, the output is localized
 * via the ``timeAgo.*`` keys (justNow / minutesAgo / hoursAgo / daysAgo).
 * When ``t`` is omitted (e.g. in non-React utility contexts), the
 * function returns an English-only fallback. Callers that need
 * consistent localization should always pass ``t``.
 */
export function timeAgo(input, t) {
  if (!input) return '';
  const date = typeof input === 'string' || typeof input === 'number' ? new Date(input) : input;
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (t) {
    if (seconds < 60) return t('timeAgo.justNow');
    if (seconds < 3600) return t('timeAgo.minutesAgo', { n: Math.floor(seconds / 60) });
    if (seconds < 86400) return t('timeAgo.hoursAgo', { n: Math.floor(seconds / 3600) });
    if (seconds < 604800) return t('timeAgo.daysAgo', { n: Math.floor(seconds / 86400) });
  } else {
    if (seconds < 60) return 'just now';
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      return `${m}m ago`;
    }
    if (seconds < 86400) {
      const h = Math.floor(seconds / 3600);
      return `${h}h ago`;
    }
    if (seconds < 604800) {
      const d = Math.floor(seconds / 86400);
      return `${d}d ago`;
    }
  }
  return date.toLocaleDateString(_locale(), { month: 'short', day: 'numeric' });
}

export function maskKey(key) {
  if (!key) return '';
  if (key.length <= 8) return '••••••••';
  return key.slice(0, 3) + '••••••••' + key.slice(-4);
}

export function titleFromMessage(msg, t) {
  const fallback = t ? t('chat.header.newChat') : 'New chat';
  if (!msg) return fallback;
  const trimmed = msg.trim();
  if (!trimmed) return fallback;
  return trimmed.length > 24 ? trimmed.slice(0, 24) : trimmed;
}

export function downloadFile(content, filename, type = 'text/plain') {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const el = document.createElement('textarea');
    el.value = text;
    document.body.appendChild(el);
    el.select();
    document.execCommand('copy');
    document.body.removeChild(el);
    return true;
  }
}
