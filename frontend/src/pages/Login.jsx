import { useState, useEffect, useCallback, useRef } from 'react';
import { Eye, EyeOff, Mail, Lock, User, ArrowRight, Loader2, Sparkles } from 'lucide-react';
import LanguageToggle from '@/components/LanguageToggle';
import ThemeToggle from '@/components/ThemeToggle';
import { useNavigate } from 'react-router-dom';
import api from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

function FloatingParticles() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="absolute -left-4 top-1/4 h-64 w-64 rounded-full bg-brand-500/5 dark:bg-brand-500/10 blur-3xl" />
      <div className="absolute -right-8 top-1/3 h-80 w-80 rounded-full bg-sky-500/5 dark:bg-sky-500/10 blur-3xl" />
      <div className="absolute -bottom-8 left-1/3 h-96 w-96 rounded-full bg-brand-400/5 dark:bg-brand-400/10 blur-3xl" />
      <div
        className="absolute left-1/4 top-1/2 h-4 w-4 rounded-full bg-brand-500/20 dark:bg-brand-400/30 animate-float"
        style={{ animationDelay: '0s' }}
      />
      <div
        className="absolute left-3/4 top-1/3 h-3 w-3 rounded-full bg-sky-500/20 dark:bg-sky-400/30 animate-float"
        style={{ animationDelay: '1s' }}
      />
      <div
        className="absolute left-1/2 top-1/4 h-5 w-5 rounded-full bg-brand-400/10 dark:bg-brand-400/20 animate-float"
        style={{ animationDelay: '2s' }}
      />
    </div>
  );
}

export default function Login() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const userLogin = useAuthStore((s) => s.userLogin);
  const [mode, setMode] = useState('login');
  const [form, setForm] = useState({ username: '', password: '', email: '', confirm_password: '' });
  const [showPw, setShowPw] = useState(false);
  const [showConfirmPw, setShowConfirmPw] = useState(false);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(null);
  const passwordRef = useRef(null);
  const confirmPasswordRef = useRef(null);
  // L17: CAPTCHA state — shown when backend returns 423 CAPTCHA_REQUIRED.
  const [captcha, setCaptcha] = useState(null); // { id, question }
  const [captchaAnswer, setCaptchaAnswer] = useState('');

  const setField = useCallback((field, value) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleUsernameKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      passwordRef.current?.focus();
    }
  };

  const handlePasswordKeyDown = (e) => {
    if (e.key === 'Enter' && !loading) {
      e.preventDefault();
      if (mode === 'register') {
        confirmPasswordRef.current?.focus();
      } else {
        handleSubmit();
      }
    }
  };

  const handleConfirmPasswordKeyDown = (e) => {
    if (e.key === 'Enter' && !loading) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const validate = () => {
    const { username, password, email, confirm_password } = form;
    if (mode === 'login') {
      if (!username) return toast.error(t('login.user.validation.usernameRequired'));
      if (!password) return toast.error(t('login.user.validation.passwordRequired'));
      return true;
    }
    if (!username || username.length < 3)
      return toast.error(t('login.user.validation.usernameMin'));
    if (!/^[a-zA-Z0-9_\u4e00-\u9fff]+$/.test(username))
      return toast.error(t('login.user.validation.usernameChars'));
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
      return toast.error(t('login.user.validation.emailInvalid'));
    if (password.length < 12) return toast.error(t('login.user.validation.passwordMin'));
    const pwClasses = [/[a-z]/.test(password), /[A-Z]/.test(password), /[0-9]/.test(password), /[^A-Za-z0-9]/.test(password)].filter(Boolean).length;
    if (pwClasses < 3) return toast.error(t('login.user.validation.passwordComplexity'));
    if (password !== confirm_password) return toast.error(t('login.user.validation.passwordMatch'));
    return true;
  };

  const handleSubmit = async () => {
    if (!validate()) return;
    // L17: require captcha answer when the challenge is shown
    if (captcha && !captchaAnswer) {
      toast.error(t('login.user.validation.captchaRequired'));
      return;
    }
    setLoading(true);
    try {
      if (mode === 'login') {
        const captchaPayload =
          captcha ? { id: captcha.id, answer: parseInt(captchaAnswer, 10) } : null;
        const result = await userLogin(
          form.username,
          form.password,
          false,
          captchaPayload,
        );
        const name = result?.user?.username || form.username;
        toast.success(t('login.user.toast.welcome', { name }));
        navigate(result?.role === 'admin' ? '/admin' : '/chat', { replace: true });
      } else {
        await api.register({ username: form.username, email: form.email, password: form.password });
        toast.success(t('login.user.toast.registerSuccess'));
        setMode('login');
        setForm({ username: '', password: '', email: '', confirm_password: '' });
        // Multi-dim review fix: clear stale CAPTCHA state on mode switch
        setCaptcha(null);
        setCaptchaAnswer('');
      }
    } catch (e) {
      // L17: 423 CAPTCHA_REQUIRED — backend returns a fresh challenge
      // in the detail payload. Render the captcha input and pre-fill it.
      const detail = e.body?.detail || e.body;
      if (
        e.status === 423 &&
        detail &&
        typeof detail === 'object' &&
        detail.code === 'CAPTCHA_REQUIRED'
      ) {
        setCaptcha({ id: detail.captcha_id, question: detail.question });
        setCaptchaAnswer('');
        toast.info(t('login.user.toast.captchaRequired'));
      } else {
        toast.error(e.message || t('login.user.toast.error'));
      }
    } finally {
      setLoading(false);
    }
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
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-600 to-brand-800 text-white shadow-brand">
              <Sparkles size={24} strokeWidth={2} />
            </div>
            <div className="absolute -bottom-1 -right-1 h-5 w-5 rounded-full bg-sky-500 p-0.5 text-white shadow-sm">
              <div className="flex h-full w-full items-center justify-center text-[8px] font-bold">
                API
              </div>
            </div>
          </div>
          <h1 className="mt-4 text-2xl font-bold tracking-tight text-ink-900 dark:text-surface-50">
            {t('app.name')}
          </h1>
          <p className="mt-1.5 text-sm text-ink-500 dark:text-surface-50/60">
            {mode === 'login' ? t('login.user.subtitle') : t('login.user.registerSubtitle')}
          </p>
        </div>

        <div className="rounded-2xl border border-ink-200/60 dark:border-surface-700/60 bg-white/80 dark:bg-surface-800/80 p-6 backdrop-blur-xl shadow-soft-lg">
          <div className="mb-6 flex rounded-xl bg-ink-100/60 dark:bg-surface-700/60 p-1">
            <button
              onClick={() => {
                setMode('login');
                setForm({ username: '', password: '', email: '', confirm_password: '' });
                // Multi-dim review fix: clear stale CAPTCHA state on mode switch
                setCaptcha(null);
                setCaptchaAnswer('');
              }}
              className={`flex-1 rounded-lg py-2 text-sm font-medium transition-all duration-200 ${
                mode === 'login'
                  ? 'bg-white dark:bg-surface-800 text-ink-900 dark:text-surface-50 shadow-soft'
                  : 'text-ink-500 dark:text-surface-50/60 hover:text-ink-700 dark:hover:text-surface-50/80'
              }`}
            >
              {t('login.user.tab.login')}
            </button>
            <button
              onClick={() => {
                setMode('register');
                setForm({ username: '', password: '', email: '', confirm_password: '' });
                setCaptcha(null);
                setCaptchaAnswer('');
              }}
              className={`flex-1 rounded-lg py-2 text-sm font-medium transition-all duration-200 ${
                mode === 'register'
                  ? 'bg-white dark:bg-surface-800 text-ink-900 dark:text-surface-50 shadow-soft'
                  : 'text-ink-500 dark:text-surface-50/60 hover:text-ink-700 dark:hover:text-surface-50/80'
              }`}
            >
              {t('login.user.tab.register')}
            </button>
          </div>

          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                {t('login.user.username')}
              </label>
              <div
                className={`relative flex items-center rounded-xl border bg-white dark:bg-surface-800 transition-all duration-200 ${
                  focused === 'username'
                    ? 'border-brand-500 shadow-glow'
                    : 'border-ink-200 dark:border-surface-700 hover:border-ink-300 dark:hover:border-surface-600'
                }`}
              >
                <User
                  size={16}
                  className={`ml-3 transition-colors ${focused === 'username' ? 'text-brand-500' : 'text-ink-400 dark:text-surface-50/40'}`}
                />
                <input
                  type="text"
                  value={form.username}
                  onChange={(e) => setField('username', e.target.value)}
                  onFocus={() => setFocused('username')}
                  onBlur={() => setFocused(null)}
                  onKeyDown={handleUsernameKeyDown}
                  placeholder={t('login.user.usernamePlaceholder')}
                  className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                />
              </div>
            </div>

            {mode === 'register' && (
              <div className="animate-fade-in">
                <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                  {t('login.user.email')}
                </label>
                <div
                  className={`relative flex items-center rounded-xl border bg-white dark:bg-surface-800 transition-all duration-200 ${
                    focused === 'email'
                      ? 'border-brand-500 shadow-glow'
                      : 'border-ink-200 dark:border-surface-700 hover:border-ink-300 dark:hover:border-surface-600'
                  }`}
                >
                  <Mail
                    size={16}
                    className={`ml-3 transition-colors ${focused === 'email' ? 'text-brand-500' : 'text-ink-400 dark:text-surface-50/40'}`}
                  />
                  <input
                    type="email"
                    value={form.email}
                    onChange={(e) => setField('email', e.target.value)}
                    onFocus={() => setFocused('email')}
                    onBlur={() => setFocused(null)}
                    placeholder={t('login.user.emailPlaceholder')}
                    className="w-full border-none bg-transparent px-3 py-2.5 text-[14px] text-ink-900 dark:text-surface-50 placeholder-ink-400 dark:placeholder-surface-50/40 outline-none"
                  />
                </div>
              </div>
            )}

            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                {t('login.user.password')}
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
                  onKeyDown={handlePasswordKeyDown}
                  ref={passwordRef}
                  placeholder={t('login.user.passwordPlaceholder')}
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

            {/* L17: CAPTCHA challenge — shown after >= 3 failed attempts */}
            {mode === 'login' && captcha && (
              <div className="animate-fade-in">
                <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                  {t('login.user.captchaLabel')}
                </label>
                <div className="flex items-center gap-3">
                  <div className="rounded-xl border border-ink-200 bg-ink-50 px-3 py-2.5 text-[14px] font-mono font-semibold text-ink-900 dark:border-surface-700 dark:bg-surface-800 dark:text-surface-50">
                    {captcha.question}
                  </div>
                  <input
                    type="number"
                    value={captchaAnswer}
                    onChange={(e) => setCaptchaAnswer(e.target.value)}
                    onFocus={() => setFocused('captcha')}
                    onBlur={() => setFocused(null)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !loading) {
                        e.preventDefault();
                        handleSubmit();
                      }
                    }}
                    placeholder={t('login.user.captchaPlaceholder')}
                    className="w-full rounded-xl border border-ink-200 bg-white px-3 py-2.5 text-[14px] text-ink-900 outline-none transition-all dark:border-surface-700 dark:bg-surface-800 dark:text-surface-50 focus:border-brand-500 focus:shadow-glow"
                    autoFocus
                  />
                </div>
              </div>
            )}

            {mode === 'register' && (
              <div className="animate-fade-in">
                <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-surface-50/80">
                  {t('login.user.confirmPassword')}
                </label>
                <div
                  className={`relative flex items-center rounded-xl border bg-white dark:bg-surface-800 transition-all duration-200 ${
                    focused === 'confirm_password'
                      ? 'border-brand-500 shadow-glow'
                      : 'border-ink-200 dark:border-surface-700 hover:border-ink-300 dark:hover:border-surface-600'
                  }`}
                >
                  <Lock
                    size={16}
                    className={`ml-3 transition-colors ${focused === 'confirm_password' ? 'text-brand-500' : 'text-ink-400 dark:text-surface-50/40'}`}
                  />
                  <input
                    type={showConfirmPw ? 'text' : 'password'}
                    value={form.confirm_password}
                    onChange={(e) => setField('confirm_password', e.target.value)}
                    onFocus={() => setFocused('confirm_password')}
                    onBlur={() => setFocused(null)}
                    onKeyDown={handleConfirmPasswordKeyDown}
                    ref={confirmPasswordRef}
                    placeholder={t('login.user.confirmPasswordPlaceholder')}
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
            )}
          </div>

          <button
            onClick={handleSubmit}
            disabled={loading}
            className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand-600 to-brand-700 text-[14px] font-semibold text-white shadow-brand transition-all duration-200 hover:from-brand-700 hover:to-brand-800 hover:shadow-glow-lg active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-70"
          >
            {loading ? (
              <Loader2 size={18} className="animate-spin" />
            ) : (
              <>
                {mode === 'login' ? t('login.user.loginButton') : t('login.user.registerButton')}
                <ArrowRight size={16} strokeWidth={2.5} />
              </>
            )}
          </button>

          {mode === 'login' && (
            <div className="mt-3 text-center">
              <button
                onClick={() => navigate('/forgot-password')}
                className="text-[12px] text-ink-400 dark:text-surface-50/40 hover:text-brand-600 dark:hover:text-brand-400 transition-colors"
              >
                {t('auth.forgotPassword', 'Forgot password?')}
              </button>
            </div>
          )}

          <div className="mt-4 text-center text-[12px] text-ink-400 dark:text-surface-50/40">
            {mode === 'login' ? (
              <span>
                {t('login.user.noAccount')}{' '}
                <button
                  onClick={() => {
                    setMode('register');
                    setCaptcha(null);
                    setCaptchaAnswer('');
                  }}
                  className="font-medium text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 transition-colors"
                >
                  {t('login.user.registerLink')}
                </button>
              </span>
            ) : (
              <span>
                {t('login.user.hasAccount')}{' '}
                <button
                  onClick={() => {
                    setMode('login');
                    setCaptcha(null);
                    setCaptchaAnswer('');
                  }}
                  className="font-medium text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 transition-colors"
                >
                  {t('login.user.loginLink')}
                </button>
              </span>
            )}
          </div>
        </div>

        <div className="mt-6 flex justify-center">
          <button
            onClick={() => navigate('/admin/login')}
            className="group flex items-center gap-1.5 text-[12px] text-ink-400 dark:text-surface-50/40 transition-colors hover:text-ink-600 dark:hover:text-surface-50/60"
          >
            <span>{t('login.adminLink')}</span>
            <ArrowRight size={12} className="transition-transform group-hover:translate-x-0.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
