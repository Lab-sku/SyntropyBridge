import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

export default function NotFound() {
  const { t } = useTranslation();
  const location = useLocation();
  const isAdmin = location.pathname.startsWith('/admin');
  const homePath = isAdmin ? '/admin' : '/chat';

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-white px-6 text-ink-900 dark:bg-ink-950 dark:text-ink-100">
      <div className="mb-4 text-6xl font-bold tracking-tight text-ink-300 dark:text-ink-600">
        404
      </div>
      <h1 className="mb-2 text-xl font-semibold">
        {t('notFound.title')}
      </h1>
      <p className="mb-6 text-sm text-ink-500 dark:text-ink-400">
        {t('notFound.subtitle')}
      </p>
      <Link
        to={homePath}
        className="rounded-lg bg-brand-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-700"
      >
        {isAdmin ? t('notFound.backToAdmin') : t('notFound.backToChat')}
      </Link>
    </div>
  );
}
