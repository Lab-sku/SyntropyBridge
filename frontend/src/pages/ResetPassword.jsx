import { useState, useEffect } from 'react';
import { Eye, EyeOff, Lock, Loader2, ArrowLeft, AlertTriangle } from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import LanguageToggle from '@/components/LanguageToggle';
import ThemeToggle from '@/components/ThemeToggle';
import api from '@/lib/api';
import { toast } from 'sonner';

export default function ResetPassword() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') || '';

  const [tokenValid, setTokenValid] = useState(null); // null = loading
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPw, setShowPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [loading, setLoading] = useState(false);

  // Validate token on mount
  useEffect(() => {
    if (!token) {
      setTokenValid(false);
      return;
    }
    api
      .validateResetToken(token)
      .then((data) => setTokenValid(!!data.valid))
      .catch(() => setTokenValid(false));
  }, [token]);

  const handleSubmit = async () => {
    if (password.length < 12) {
      toast.error(t('reset.validation.passwordMin'));
      return;
    }
    if (password !== confirmPassword) {
      toast.error(t('login.user.validation.passwordMatch'));
      return;
    }
    setLoading(true);
    try {
      await api.resetPassword({ token, new_password: password });
      toast.success(t('reset.success'));
      navigate('/login', { replace: true });
    } catch (e) {
      toast.error(e.message || t('reset.failed'));
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
            {t('reset.title')}
          </h1>
          <p className="mt-1.5 text-sm text-ink-500 dark:text-surface-50/60">
            {t('reset.subtitle')}
          </p>
        </div>

        <div className="rounded-2xl border border-ink-200/60 dark:border-surface-700/60 bg-white/80 dark:bg-surface-800/80 p-6 backdrop-blur-xl shadow-soft-lg">
          {/* Loading state */}
          {tokenValid === null && (
            <div className="flex flex-col items-center py-8">
              <Loader2 size={24} className="animate-spin text-brand-600" />
              <p className="mt-3 text-[13px] text-ink-500 dark:text-surface-50/60">
                {t('common.loading')}
              </p>
            </div>
          )}

          {/* Token invalid / expired */}
          {tokenValid === false && (
            <div className="text-center">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-red-100 dark:bg-red-900/30">
                <AlertTriangle size={24} className="text-red-600 dark:text-red-400" />
              </div>
              <p className="text-[14px] font-medium text-ink-700 dark:text-surface-50/80">
                {t('reset.expired')}
              </p>
              <button
                onClick={() => navigate('/forgot-password')}
                className="mt-4 inline-flex items-center gap-1.5 text-[13px] font-medium text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 transition-colors"
              >
                {t('reset.requestNew')}
              </button>
              <div className="mt-3">
                <button
                  onClick={() => navigate('/login')}
                  className="inline-flex items-center gap-1.5 text-[12px] text-ink-400 dark:text-surface-50/40 hover:text-ink-600 dark:hover:text-surface-50/60 transition-colors"
                >
                  <ArrowLeft size={12} />
                  {t('forgot.backToLogin')}
                </button>
              </div>
            </div>
          )}

          {/* Token valid — show form */}
          {tokenValid === true && (
            <>
              <div className="space-y-4">
                <div>
                  <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                    {t('reset.newPassword')}
                  </label>
                  <div className="relative flex items-center rounded-xl border border-ink-200 dark:border-surface-700 bg-white dark:bg-surface-800 hover:border-ink-300 dark:hover:border-surface-600 transition-all duration-200 focus-within:border-brand-500 focus-within:shadow-glow">
                    <Lock size={16} className="ml-3 text-ink-400 dark:text-surface-50/40" />
                    <input
                      type={showPw ? 'text' : 'password'}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder={t('reset.newPasswordPlaceholder')}
                      className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPw((v) => !v)}
                      className="mr-3 rounded-md p-1 text-ink-400 dark:text-surface-50/40 transition-colors hover:text-ink-600 dark:hover:text-surface-50/60"
                    >
                      {showPw ? <EyeOff size={16} /> : <Eye size={16} />}
                    </button>
                  </div>
                </div>

                <div>
                  <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                    {t('reset.confirmPassword')}
                  </label>
                  <div className="relative flex items-center rounded-xl border border-ink-200 dark:border-surface-700 bg-white dark:bg-surface-800 hover:border-ink-300 dark:hover:border-surface-600 transition-all duration-200 focus-within:border-brand-500 focus-within:shadow-glow">
                    <Lock size={16} className="ml-3 text-ink-400 dark:text-surface-50/40" />
                    <input
                      type={showConfirmPw ? 'text' : 'password'}
                      value={confirmPassword}
                      onChange={(e) => setConfirmPassword(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !loading) handleSubmit();
                      }}
                      placeholder={t('reset.confirmPasswordPlaceholder')}
                      className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => setShowConfirmPw((v) => !v)}
                      className="mr-3 rounded-md p-1 text-ink-400 dark:text-surface-50/40 transition-colors hover:text-ink-600 dark:hover:text-surface-50/60"
                    >
                      {showConfirmPw ? <EyeOff size={16} /> : <Eye size={16} />}
                    </button>
                  </div>
                </div>
              </div>

              <button
                onClick={handleSubmit}
                disabled={loading}
                className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand-600 to-brand-700 text-[14px] font-semibold text-white shadow-brand transition-all duration-200 hover:from-brand-700 hover:to-brand-800 hover:shadow-glow-lg active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-70"
              >
                {loading ? (
                  <Loader2 size={18} className="animate-spin" />
                ) : (
                  t('reset.submit')
                )}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
