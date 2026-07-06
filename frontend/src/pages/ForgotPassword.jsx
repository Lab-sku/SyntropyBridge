import { useState } from 'react';
import { Mail, Loader2, ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import LanguageToggle from '@/components/LanguageToggle';
import ThemeToggle from '@/components/ThemeToggle';
import api from '@/lib/api';
import { toast } from 'sonner';

export default function ForgotPassword() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async () => {
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      toast.error(t('login.user.validation.emailInvalid'));
      return;
    }
    setLoading(true);
    try {
      await api.forgotPassword({ email });
      setSent(true);
    } catch (e) {
      toast.error(e.message || t('forgot.sendFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-ink-50 via-white to-ink-50 dark:from-surface-950 dark:via-surface-900 dark:to-surface-950 px-4 py-8">
      <div className="absolute right-4 top-4 flex items-center gap-2">
        <LanguageToggle size="sm" />
        <ThemeToggle size="sm" />
      </div>

      <div className="relative w-full max-w-md animate-fade-in-up">
        <div className="mb-8 flex flex-col items-center">
          <h1 className="text-2xl font-bold tracking-tight text-ink-900 dark:text-surface-50">
            {t('forgot.title')}
          </h1>
          <p className="mt-1.5 text-sm text-ink-500 dark:text-surface-50/60">
            {t('forgot.subtitle')}
          </p>
        </div>

        <div className="rounded-2xl border border-ink-200/60 dark:border-surface-700/60 bg-white/80 dark:bg-surface-800/80 p-6 backdrop-blur-xl shadow-soft-lg">
          {sent ? (
            <div className="text-center">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-green-100 dark:bg-green-900/30">
                <Mail size={24} className="text-green-600 dark:text-green-400" />
              </div>
              <p className="text-[14px] text-ink-700 dark:text-surface-50/80">
                {t('forgot.sentMessage')}
              </p>
              <button
                onClick={() => navigate('/login')}
                className="mt-6 inline-flex items-center gap-1.5 text-[13px] font-medium text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 transition-colors"
              >
                <ArrowLeft size={14} />
                {t('forgot.backToLogin')}
              </button>
            </div>
          ) : (
            <>
              <div className="space-y-4">
                <div>
                  <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                    {t('forgot.email')}
                  </label>
                  <div className="relative flex items-center rounded-xl border border-ink-200 dark:border-surface-700 bg-white dark:bg-surface-800 hover:border-ink-300 dark:hover:border-surface-600 transition-all duration-200 focus-within:border-brand-500 focus-within:shadow-glow">
                    <Mail size={16} className="ml-3 text-ink-400 dark:text-surface-50/40" />
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !loading) handleSubmit();
                      }}
                      placeholder={t('forgot.emailPlaceholder')}
                      className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                    />
                  </div>
                </div>
              </div>

              <button
                onClick={handleSubmit}
                disabled={loading}
                className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand-600 to-brand-700 text-[14px] font-semibold text-white shadow-brand transition-all duration-200 hover:from-brand-700 hover:to-brand-800 hover:shadow-glow-lg active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-70"
              >
                {loading ? <Loader2 size={18} className="animate-spin" /> : t('forgot.submit')}
              </button>

              <div className="mt-4 text-center">
                <button
                  onClick={() => navigate('/login')}
                  className="inline-flex items-center gap-1.5 text-[12px] font-medium text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 transition-colors"
                >
                  <ArrowLeft size={12} />
                  {t('forgot.backToLogin')}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
