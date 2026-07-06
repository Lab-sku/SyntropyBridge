import React from 'react';
import { useTranslation } from 'react-i18next';

/**
 * ErrorBoundary — catches unhandled React render errors and shows a
 * fallback UI instead of a white screen.
 *
 * Also wires a ``window.onerror`` + ``unhandledrejection`` listener
 * (installed once, on mount) so async errors that miss the React tree
 * are reported through the same channel.
 *
 * Reporting is best-effort: it POSTs to ``/api/client-errors`` (added
 * in the same change set) and swallows any network failure so a
 * broken backend never blocks the user from seeing the fallback.
 *
 * Props:
 *   - children: the protected subtree.
 *   - fallback (optional): a custom render-prop ``({ error, reset }) =>
 *     ReactNode``. When omitted a sensible default UI is shown.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, errorInfo: null };
    this._reset = this._reset.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, errorInfo) {
    this.setState({ errorInfo });
    // Best-effort report. Never throws — a broken reporting endpoint
    // must not block the user from seeing the fallback UI.
    try {
      const payload = {
        type: 'react',
        message: String(error && error.message ? error.message : error),
        stack: String(error && error.stack ? error.stack : ''),
        componentStack: String(errorInfo && errorInfo.componentStack || ''),
        url: typeof window !== 'undefined' ? window.location.href : '',
        ts: Date.now(),
      };
      // Fire-and-forget; we don't even await the response.
      if (typeof window !== 'undefined' && window.fetch) {
        window.fetch('/api/client-errors', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          credentials: 'include',
        }).catch(() => {});
      }
    } catch {
      // ignored — never let reporting crash the fallback
    }
  }

  componentDidMount() {
    if (typeof window === 'undefined') return;
    if (window.__errorBoundaryInstalled) return;
    window.__errorBoundaryInstalled = true;

    const report = (type, message, stack) => {
      try {
        const payload = {
          type,
          message: String(message || ''),
          stack: String(stack || ''),
          url: window.location.href,
          ts: Date.now(),
        };
        if (window.fetch) {
          window.fetch('/api/client-errors', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            credentials: 'include',
          }).catch(() => {});
        }
      } catch {
        // ignored
      }
    };

    window.addEventListener('error', (event) => {
      // Avoid duplicate reports for errors React already captured.
      if (event && event.error && this.state && this.state.error === event.error) return;
      report('window.onerror', event.message, event.error && event.error.stack);
    });

    window.addEventListener('unhandledrejection', (event) => {
      const reason = event && event.reason;
      const msg = reason && reason.message ? reason.message : String(reason || '');
      const stk = reason && reason.stack ? reason.stack : '';
      report('unhandledrejection', msg, stk);
    });
  }

  _reset() {
    this.setState({ error: null, errorInfo: null });
  }

  render() {
    const { error, errorInfo } = this.state;
    if (!error) return this.props.children;

    if (typeof this.props.fallback === 'function') {
      return this.props.fallback({ error, errorInfo, reset: this._reset });
    }

    return <DefaultFallback error={error} errorInfo={errorInfo} reset={this._reset} />;
  }
}

function DefaultFallback({ error, errorInfo, reset }) {
  const { t } = useTranslation();
  return (
    <div
      role="alert"
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '1.5rem',
        background: 'var(--color-bg, #fff)',
        color: 'var(--color-text, #1a1a1a)',
      }}
    >
      <div style={{ maxWidth: 560, textAlign: 'center' }}>
        <h1 style={{ fontSize: '1.5rem', marginBottom: '0.5rem' }}>
          {t('errors.boundaryTitle', '页面出错了')}
        </h1>
        <p style={{ opacity: 0.75, marginBottom: '1rem' }}>
          {t('errors.boundaryBody', '应用遇到意外错误，已自动上报。请刷新页面或返回上一页重试。')}
        </p>
        <pre
          style={{
            textAlign: 'left',
            background: 'rgba(0,0,0,0.05)',
            padding: '0.75rem',
            borderRadius: 6,
            fontSize: 12,
            overflowX: 'auto',
            maxHeight: 200,
            margin: '1rem 0',
          }}
        >
          {String(error && error.message ? error.message : error)}
          {errorInfo && errorInfo.componentStack
            ? '\n\nComponent stack:' + errorInfo.componentStack
            : ''}
        </pre>
        <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'center' }}>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: 6,
              border: '1px solid currentColor',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
            }}
          >
            {t('errors.reload', '刷新页面')}
          </button>
          <button
            type="button"
            onClick={reset}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: 6,
              border: '1px solid currentColor',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
            }}
          >
            {t('errors.retry', '重试')}
          </button>
        </div>
      </div>
    </div>
  );
}
