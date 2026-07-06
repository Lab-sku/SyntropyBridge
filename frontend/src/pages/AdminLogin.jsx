import { useState, useCallback } from 'react';
import { Eye, EyeOff, Lock, ArrowRight, Loader2, Shield } from 'lucide-react';
import LanguageToggle from '@/components/LanguageToggle';
import ThemeToggle from '@/components/ThemeToggle';
import { Link, useNavigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

function FloatingParticles() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute -left-4 top-1/4 h-64 w-64 rounded-full bg-rose-500/5 dark:bg-rose-500/10 blur-3xl" />
      <div className="absolute -right-8 top-1/3 h-80 w-80 rounded-full bg-indigo-500/5 dark:bg-indigo-500/10 blur-3xl" />
      <div className="absolute -bottom-8 left-1/3 h-96 w-96 rounded-full bg-rose-400/5 dark:bg-rose-400/10 blur-3xl" />
      <div
        className="absolute left-1/4 top-1/2 h-4 w-4 rounded-full bg-rose-500/20 dark:bg-rose-400/30 animate-float"
        style={{ animationDelay: '0s' }}
      />
      <div
        className="absolute left-3/4 top-1/3 h-3 w-3 rounded-full bg-indigo-500/20 dark:bg-indigo-400/30 animate-float"
        style={{ animationDelay: '1s' }}
      />
    </div>
  );
}

export default function AdminLogin() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const adminLogin = useAuthStore((s) => s.adminLogin);
  const [form, setForm] = useState({ username: '', password: '' });
  const [showPw, setShowPw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(null);

  const setField = useCallback((field, value) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleSubmit = async () => {
    if (!form.username) return toast.error(t('login.admin.validation.usernameRequired'));
    if (!form.password) return toast.error(t('login.admin.validation.passwordRequired'));

    setLoading(true);
    try {
      await adminLogin(form.username, form.password);
      toast.success(t('login.admin.toast.welcome'));
      navigate('/admin', { replace: true });
    } catch (e) {
      toast.error(e.message || t('login.admin.toast.error'));
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !loading) handleSubmit();
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gradient-to-br from-ink-50 via-white to-ink-50 dark:from-surface-950 dark:via-surface-900 dark:to-surface-950 px-4 py-8">
      <FloatingParticles />

      <div className="absolute right-4 top-4 flex items-center gap-2">
        <LanguageToggle size="sm" />
        <ThemeToggle size="sm" />
      </div>

      <div className="relative w-full max-w-md animate-fade-in-up">
        <div className="mb-8 flex flex-col items-center">
          <div className="relative">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-slate-700 to-ink-900 text-white shadow-lg">
              <Shield size={24} strokeWidth={2} />
            </div>
            <div className="absolute -bottom-1 -right-1 h-5 w-5 rounded-full bg-rose-500 p-0.5 text-white shadow-sm">
              <div className="flex h-full w-full items-center justify-center text-[8px] font-bold">
                A
              </div>
            </div>
          </div>
          <h1 className="mt-4 text-2xl font-bold tracking-tight text-ink-900 dark:text-surface-50">
            {t('login.admin.title')}
          </h1>
          <p className="mt-1.5 text-sm text-ink-500 dark:text-surface-50/60">
            {t('login.admin.subtitle')}
          </p>
        </div>

        <div
          onKeyDown={handleKeyDown}
          className="rounded-2xl border border-ink-200/60 dark:border-surface-700/60 bg-white/80 dark:bg-surface-800/80 p-6 backdrop-blur-xl shadow-soft-lg"
        >
          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                {t('login.admin.username')}
              </label>
              <div
                className={`relative flex items-center rounded-xl border bg-white dark:bg-surface-800 transition-all duration-200 ${
                  focused === 'username'
                    ? 'border-brand-500 shadow-glow'
                    : 'border-ink-200 dark:border-surface-700 hover:border-ink-300 dark:hover:border-surface-600'
                }`}
              >
                <Shield
                  size={16}
                  className={`ml-3 transition-colors ${focused === 'username' ? 'text-brand-500' : 'text-ink-400 dark:text-surface-50/40'}`}
                />
                <input
                  type="text"
                  value={form.username}
                  onChange={(e) => setField('username', e.target.value)}
                  onFocus={() => setFocused('username')}
                  onBlur={() => setFocused(null)}
                  placeholder={t('login.admin.usernamePlaceholder')}
                  className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                />
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                {t('login.admin.password')}
              </label>
              <div
                className={`relative flex items-center rounded-xl border bg-white dark:bg-surface-800 transition-all duration-200 ${
                  focused === 'password'
                    ? 'border-brand-500 shadow-glow'
                    : 'border-ink-200 dark:border-surface-700 hover:border-ink-300 dark:hover:border-surface-600'
                }`}
              >
                <Lock
                  size={16}
                  className={`ml-3 transition-colors ${focused === 'password' ? 'text-brand-500' : 'text-ink-400 dark:text-surface-50/40'}`}
                />
                <input
                  type={showPw ? 'text' : 'password'}
                  value={form.password}
                  onChange={(e) => setField('password', e.target.value)}
                  onFocus={() => setFocused('password')}
                  onBlur={() => setFocused(null)}
                  placeholder={t('login.admin.passwordPlaceholder')}
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
          </div>

          <button
            onClick={handleSubmit}
            disabled={loading}
            className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-slate-700 to-ink-900 text-[14px] font-semibold text-white shadow-lg transition-all duration-200 hover:from-ink-800 hover:to-ink-950 hover:shadow-xl active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-70"
          >
            {loading ? (
              <Loader2 size={18} className="animate-spin" />
            ) : (
              <>
                {t('login.admin.loginButton')}
                <ArrowRight size={16} strokeWidth={2.5} />
              </>
            )}
          </button>
        </div>

        <div className="mt-6 flex justify-center">
          <Link
            to="/login"
            className="group flex items-center gap-1.5 text-[12px] text-ink-400 dark:text-surface-50/40 transition-colors hover:text-ink-600 dark:hover:text-surface-50/60"
          >
            <ArrowRight
              size={12}
              className="rotate-180 transition-transform group-hover:-translate-x-0.5"
            />
            <span>{t('login.admin.backToUser')}</span>
          </Link>
        </div>
      </div>
    </div>
  );
}
