import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowRight,
  Check,
  Coins,
  Download,
  Eye,
  EyeOff,
  KeyRound,
  Lock,
  Save,
  Shield,
  ShieldCheck,
  Trash2,
  User as UserIcon,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import { useAuthStore } from '@/stores/authStore';

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/** Map a backend 4xx/5xx message to a localised error key when possible,
 *  otherwise return the raw string. Keeps UI texts in i18n. Matches both
 *  the Chinese strings emitted by the production backend and the English
 *  strings emitted in tests / future i18n-aware backend responses. */
function pickErrorKey(message) {
  if (!message) return null;
  const m = String(message);
  const lower = m.toLowerCase();
  if (
    m.includes('已被') ||
    m.includes('使用') ||
    m.includes('已占用') ||
    lower.includes('has been') ||
    lower.includes('already') ||
    lower.includes('taken') ||
    lower.includes('exists')
  ) {
    // Backend uses "已被其他账号使用" for both username and email.
    if (m.includes('用户名') || lower.includes('username')) return 'usernameTaken';
    if (m.includes('邮箱') || lower.includes('email')) return 'emailTaken';
  }
  if (
    m.includes('2-50') ||
    m.includes('长度') ||
    lower.includes('length') ||
    lower.includes('too short') ||
    lower.includes('too long')
  ) {
    if (m.includes('用户名') || lower.includes('username')) {
      return 'usernameTooShort'; // catches short; long path returns the same key
    }
    return 'usernameTooShort';
  }
  if (m.includes('格式') || m.includes('合法') || lower.includes('invalid') || lower.includes('format')) {
    return 'emailInvalid';
  }
  return null;
}

/** Classify password strength 0..4. Mirrors the 4-bucket model we
 *  promised in the design doc: lower → medium → strong → very-strong.
 *  Bonus point for length, one per character class. */
function scorePassword(pw) {
  if (!pw) return 0;
  const hasLower = /[a-z]/.test(pw);
  const hasUpper = /[A-Z]/.test(pw);
  const hasDigit = /\d/.test(pw);
  const hasSymbol = /[^A-Za-z0-9]/.test(pw);
  const classes = [hasLower, hasUpper, hasDigit, hasSymbol].filter(Boolean).length;
  if (pw.length >= 16 && classes === 4) return 4; // very strong
  if (pw.length >= 12 && classes >= 3) return 3; // strong
  if (pw.length >= 8 && classes >= 2) return 2;  // medium
  return 1;                                       // weak (length>=1, not strong enough)
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Account() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const role = useAuthStore((s) => s.role);

  // -- GDPR state
  const [exporting, setExporting] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deletePassword, setDeletePassword] = useState('');
  const [deleting, setDeleting] = useState(false);

  // -- Budget state
  const [budgetDraft, setBudgetDraft] = useState('');
  const [budgetSaving, setBudgetSaving] = useState(false);

  useEffect(() => {
    if (user?.monthly_budget !== undefined && user?.monthly_budget !== null) {
      setBudgetDraft(String(user.monthly_budget));
    }
  }, [user?.monthly_budget]);

  // -- Basic info: separate draft per field so each can save independently
  const initialUsername = user?.username || '';
  const initialEmail = user?.email || '';
  const [usernameDraft, setUsernameDraft] = useState(initialUsername);
  const [emailDraft, setEmailDraft] = useState(initialEmail);
  const [usernameSaving, setUsernameSaving] = useState(false);
  const [emailSaving, setEmailSaving] = useState(false);
  const [usernameError, setUsernameError] = useState(null);
  const [emailError, setEmailError] = useState(null);

  // -- Password form
  const [pwForm, setPwForm] = useState({ current: '', next: '', confirm: '' });
  const [showPw, setShowPw] = useState({ current: false, next: false, confirm: false });
  const [pwSubmitting, setPwSubmitting] = useState(false);
  const [pwErrors, setPwErrors] = useState({}); // field → message
  const [pwFormError, setPwFormError] = useState(null);
  const pwRef = useRef(null);

  // Keep drafts in sync with the store if the user is updated elsewhere
  // (e.g. admin reset, or post-changeSession refresh).
  useEffect(() => {
    setUsernameDraft(user?.username || '');
    setEmailDraft(user?.email || '');
  }, [user?.username, user?.email]);

  const usernameDirty = usernameDraft !== (user?.username || '');
  const emailDirty = emailDraft !== (user?.email || '');

  // -- Username save
  const saveUsername = useCallback(async () => {
    setUsernameError(null);
    setUsernameSaving(true);
    try {
      await api.updateProfile({ username: usernameDraft });
      // Re-fetch the canonical session so store matches DB and
      // sidebar / TopBar all update.
      await useAuthStore.getState().checkSession();
      toast.success(t('account.profile.saved'));
    } catch (e) {
      const key = pickErrorKey(e?.message);
      setUsernameError(key ? t(`account.profile.errors.${key}`) : e?.message || t('account.profile.errors.serverError'));
    } finally {
      setUsernameSaving(false);
    }
  }, [usernameDraft, t]);

  // -- Email save
  const saveEmail = useCallback(async () => {
    setEmailError(null);
    setEmailSaving(true);
    try {
      // Normalise: empty string = clear email. Backend treats "" as null.
      const payload = { email: emailDraft.trim() };
      await api.updateProfile(payload);
      await useAuthStore.getState().checkSession();
      toast.success(t('account.profile.saved'));
    } catch (e) {
      const key = pickErrorKey(e?.message);
      setEmailError(key ? t(`account.profile.errors.${key}`) : e?.message || t('account.profile.errors.serverError'));
    } finally {
      setEmailSaving(false);
    }
  }, [emailDraft, t]);

  // -- Password strength (live)
  const strength = useMemo(() => scorePassword(pwForm.next), [pwForm.next]);
  const strengthLabelKey = ['weak', 'weak', 'medium', 'strong', 'veryStrong'][strength] || 'weak';

  // -- Password submit
  const submitPassword = useCallback(
    async (e) => {
      e?.preventDefault?.();
      setPwErrors({});
      setPwFormError(null);

      // Client-side validation mirrors the backend strong-password
      // rules: 12+ chars, 3-of-4 character classes.
      const errs = {};
      if (!pwForm.current) errs.current = t('account.profile.password.errors.currentRequired');
      if (!pwForm.next) errs.next = t('account.profile.password.errors.newRequired');
      if (!pwForm.confirm) errs.confirm = t('account.profile.password.errors.confirmRequired');
      if (pwForm.next && pwForm.next.length < 12) errs.next = t('account.profile.password.errors.tooShort');
      if (pwForm.next && scorePassword(pwForm.next) < 3) errs.next = t('account.profile.password.errors.tooWeak');
      if (pwForm.next && pwForm.next === pwForm.current) errs.next = t('account.profile.password.errors.sameAsOld');
      if (pwForm.next && pwForm.confirm && pwForm.next !== pwForm.confirm) {
        errs.confirm = t('account.profile.password.errors.mismatch');
      }
      if (Object.keys(errs).length) {
        setPwErrors(errs);
        return;
      }

      setPwSubmitting(true);
      try {
        await api.changePassword(pwForm.current, pwForm.next);
        toast.success(t('account.profile.password.success'));
        // Server invalidated every session for this user. Clear the
        // local store + bounce to login. The user re-authenticates
        // with the new password.
        try {
          // Best-effort: ask the auth store to log out. If the
          // /auth/logout call also fails (because sessions are
          // already gone), that's fine — we just redirect.
          await useAuthStore.getState().logout?.();
        } catch (_) {
          /* ignore */
        }
        // Hard redirect to login to ensure no protected route is
        // mounted in the background.
        window.location.assign('/login');
      } catch (e) {
        // Map known backend errors. Old-password mismatch has its own
        // Chinese message; otherwise show generic server error.
        const msg = e?.message || '';
        if (msg.includes('原密码') || msg.toLowerCase().includes('incorrect password')) {
          setPwErrors({ current: msg });
        } else if (msg.includes('12') || msg.includes('字符') || msg.includes('类')) {
          setPwFormError(msg);
        } else {
          setPwFormError(msg || t('account.profile.errors.serverError'));
        }
      } finally {
        setPwSubmitting(false);
      }
    },
    [pwForm, t],
  );

  return (
    <>
      <TopBar title={t('account.profile.title')} subtitle={t('account.profile.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 via-ink-50/50 to-brand-50/30">
        <div className="mx-auto max-w-3xl space-y-5 p-4 md:p-6">
          {/* ----------------------------------------------------------------
              Card 1 — Basic info
             ---------------------------------------------------------------- */}
          <div className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft transition-all hover:shadow-soft-lg">
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-blue-600 text-white shadow-md">
                <UserIcon size={18} strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('account.profile.basicInfo')}
                </h2>
                <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t('account.profile.subtitle')}
                </p>
              </div>
            </div>

            <div className="space-y-4">
              {/* Username */}
              <div>
                <label
                  htmlFor="profile-username"
                  className="mb-1.5 block text-[12px] font-semibold text-ink-700 dark:text-ink-300"
                >
                  {t('account.profile.username')}
                </label>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <input
                    id="profile-username"
                    type="text"
                    value={usernameDraft}
                    onChange={(e) => {
                      setUsernameDraft(e.target.value);
                      setUsernameError(null);
                    }}
                    maxLength={50}
                    autoComplete="username"
                    className="h-10 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20"
                  />
                  <Button
                    onClick={saveUsername}
                    loading={usernameSaving}
                    disabled={!usernameDirty}
                    icon={Save}
                    className="rounded-xl px-5 sm:w-auto w-full"
                  >
                    {t('account.profile.save')}
                  </Button>
                </div>
                <p className="mt-1.5 text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t('account.profile.usernameHelp')}
                </p>
                {usernameError && (
                  <p className="mt-1.5 text-[11.5px] text-rose-600 dark:text-rose-400">
                    {usernameError}
                  </p>
                )}
              </div>

              {/* Email */}
              <div>
                <label
                  htmlFor="profile-email"
                  className="mb-1.5 block text-[12px] font-semibold text-ink-700 dark:text-ink-300"
                >
                  {t('account.profile.email')}
                </label>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <input
                    id="profile-email"
                    type="email"
                    value={emailDraft}
                    onChange={(e) => {
                      setEmailDraft(e.target.value);
                      setEmailError(null);
                    }}
                    maxLength={100}
                    autoComplete="email"
                    className="h-10 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20"
                  />
                  <Button
                    onClick={saveEmail}
                    loading={emailSaving}
                    disabled={!emailDirty}
                    icon={Save}
                    className="rounded-xl px-5 sm:w-auto w-full"
                  >
                    {t('account.profile.save')}
                  </Button>
                </div>
                <p className="mt-1.5 text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t('account.profile.emailHelp')}
                </p>
                {emailError && (
                  <p className="mt-1.5 text-[11.5px] text-rose-600 dark:text-rose-400">
                    {emailError}
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* ----------------------------------------------------------------
              Card 2 — Change password
             ---------------------------------------------------------------- */}
          <form
            ref={pwRef}
            onSubmit={submitPassword}
            noValidate
            className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft transition-all hover:shadow-soft-lg"
          >
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-purple-600 text-white shadow-md">
                <Lock size={18} strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('account.profile.password.title')}
                </h2>
                <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t('account.profile.password.warning')}
                </p>
              </div>
            </div>

            <div className="space-y-4">
              {(['current', 'next', 'confirm']).map((field) => {
                const labelKey =
                  field === 'current'
                    ? 'currentPassword'
                    : field === 'next'
                    ? 'newPassword'
                    : 'confirmPassword';
                const autoComplete =
                  field === 'current' ? 'current-password' : 'new-password';
                return (
                  <div key={field}>
                    <label
                      htmlFor={`pw-${field}`}
                      className="mb-1.5 block text-[12px] font-semibold text-ink-700 dark:text-ink-300"
                    >
                      {t(`account.profile.password.${labelKey}`)}
                    </label>
                    <div className="relative">
                      <input
                        id={`pw-${field}`}
                        type={showPw[field] ? 'text' : 'password'}
                        value={pwForm[field]}
                        onChange={(e) => {
                          setPwForm((f) => ({ ...f, [field]: e.target.value }));
                          setPwErrors((prev) => {
                            if (!prev[field]) return prev;
                            const { [field]: _omit, ...rest } = prev;
                            return rest;
                          });
                        }}
                        autoComplete={autoComplete}
                        className="h-10 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 pl-3 pr-12 text-[13px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20"
                      />
                      <button
                        type="button"
                        onClick={() =>
                          setShowPw((s) => ({ ...s, [field]: !s[field] }))
                        }
                        className="absolute right-2 top-1/2 -translate-y-1/2 rounded-lg p-1.5 text-ink-400 dark:text-ink-500 transition-colors hover:bg-ink-100 dark:hover:bg-ink-800 hover:text-ink-700 dark:hover:text-ink-300"
                        aria-label={showPw[field] ? t('account.profile.password.hide') : t('account.profile.password.show')}
                      >
                        {showPw[field] ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    </div>
                    {pwErrors[field] && (
                      <p className="mt-1.5 text-[11.5px] text-rose-600 dark:text-rose-400">
                        {pwErrors[field]}
                      </p>
                    )}
                  </div>
                );
              })}

              {/* Strength bar (live) */}
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-[11.5px] font-medium text-ink-500 dark:text-ink-400">
                    {t('account.profile.password.strengthLabel')}
                  </span>
                  <span
                    className={
                      'text-[11.5px] font-semibold ' +
                      (strength >= 3
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : strength === 2
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-rose-600 dark:text-rose-400')
                    }
                  >
                    {t(`account.profile.password.strength.${strengthLabelKey}`)}
                  </span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
                  <div
                    className={
                      'h-full transition-all duration-300 ' +
                      (strength >= 3
                        ? 'bg-emerald-500'
                        : strength === 2
                        ? 'bg-amber-500'
                        : 'bg-rose-500')
                    }
                    style={{ width: `${(strength / 4) * 100}%` }}
                  />
                </div>
              </div>

              {pwFormError && (
                <p className="rounded-lg border border-rose-200 dark:border-rose-900/50 bg-rose-50 dark:bg-rose-950/30 px-3 py-2 text-[12px] text-rose-700 dark:text-rose-300">
                  {pwFormError}
                </p>
              )}

              <div className="flex justify-end pt-1">
                <Button
                  type="submit"
                  loading={pwSubmitting}
                  icon={ShieldCheck}
                  className="rounded-xl px-6 shadow-md transition-all hover:shadow-lg hover:-translate-y-0.5"
                >
                  {t('account.profile.password.submit')}
                </Button>
              </div>
            </div>
          </form>

          {/* ----------------------------------------------------------------
              Card 3 — Account info (read-only)
             ---------------------------------------------------------------- */}
          <div className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft transition-all hover:shadow-soft-lg">
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500 to-green-600 text-white shadow-md">
                <Shield size={18} strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('account.profile.basicInfo')}
                </h2>
                <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t(role === 'admin' ? 'account.role.admin' : 'account.role.user')}
                </p>
              </div>
            </div>

            <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-ink-200/60 dark:border-ink-700/60 bg-white/60 dark:bg-ink-900/60 p-3">
                <dt className="text-[11px] font-medium uppercase tracking-wider text-ink-400 dark:text-ink-500">
                  {t('account.profile.userId')}
                </dt>
                <dd className="mt-1 font-mono text-[13px] text-ink-800 dark:text-ink-200">
                  {user?.id ?? '—'}
                </dd>
              </div>
              <div className="rounded-xl border border-ink-200/60 dark:border-ink-700/60 bg-white/60 dark:bg-ink-900/60 p-3">
                <dt className="text-[11px] font-medium uppercase tracking-wider text-ink-400 dark:text-ink-500">
                  {t('account.profile.username')}
                </dt>
                <dd className="mt-1 font-mono text-[13px] text-ink-800 dark:text-ink-200 flex items-center gap-1.5">
                  <Check size={13} className="text-emerald-500" />
                  {user?.username || '—'}
                </dd>
              </div>
            </dl>

            <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                {t('account.profile.subtitle')}
              </p>
              <Link
                to="/account/key"
                className="inline-flex items-center gap-1.5 text-[12.5px] font-semibold text-brand-600 dark:text-brand-400 hover:underline"
              >
                <KeyRound size={13} />
                {t('account.profile.viewKey')}
                <ArrowRight size={13} />
              </Link>
            </div>
          </div>

          {/* ----------------------------------------------------------------
              Card 4 — Monthly budget
             ---------------------------------------------------------------- */}
          <div className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft transition-all hover:shadow-soft-lg">
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-amber-500 to-orange-600 text-white shadow-md">
                <Coins size={18} strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('budgetSection.setBudget')}
                </h2>
                <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                  {t('budgetSection.setBudgetDesc')}
                </p>
              </div>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                type="number"
                min="0"
                step="0.01"
                value={budgetDraft}
                onChange={(e) => setBudgetDraft(e.target.value)}
                placeholder={t('budgetSection.budgetPlaceholder')}
                className="h-10 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20"
              />
              <Button
                onClick={async () => {
                  setBudgetSaving(true);
                  try {
                    const val = parseFloat(budgetDraft);
                    if (isNaN(val) || val < 0) {
                      toast.error(t('budgetSection.budgetFailed'));
                      return;
                    }
                    await api.updateProfile({ monthly_budget: val });
                    await useAuthStore.getState().checkSession();
                    toast.success(t('budgetSection.budgetSaved'));
                  } catch (e) {
                    toast.error(e?.message || t('budgetSection.budgetFailed'));
                  } finally {
                    setBudgetSaving(false);
                  }
                }}
                loading={budgetSaving}
                icon={Save}
                className="rounded-xl px-5 sm:w-auto w-full"
              >
                {t('common.save')}
              </Button>
            </div>
          </div>

          {/* ----------------------------------------------------------------
              Card 5 — GDPR data management
             ---------------------------------------------------------------- */}
          <div className="group overflow-hidden rounded-2xl border border-ink-200/40 dark:border-ink-700/40 bg-gradient-to-br from-white to-ink-50/40 dark:from-ink-900 dark:to-ink-900/40 p-6 shadow-soft transition-all hover:shadow-soft-lg">
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-slate-500 to-gray-600 text-white shadow-md">
                <Shield size={18} strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('gdpr.title')}
                </h2>
              </div>
            </div>

            <div className="space-y-4">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between rounded-xl border border-ink-200/60 dark:border-ink-700/60 bg-white/60 dark:bg-ink-900/60 p-4">
                <div>
                  <p className="text-[13px] font-medium text-ink-800 dark:text-ink-200">
                    {t('gdpr.export')}
                  </p>
                  <p className="text-[11.5px] text-ink-500 dark:text-ink-400">
                    {t('gdpr.exportDesc')}
                  </p>
                </div>
                <Button
                  onClick={async () => {
                    setExporting(true);
                    try {
                      const res = await fetch('/api/user/data/export', { credentials: 'include' });
                      if (res.status === 429) {
                        toast.error(t('gdpr.exportRateLimited'));
                        return;
                      }
                      if (!res.ok) throw new Error('export failed');
                      const blob = await res.blob();
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url;
                      a.download = `user_data_${user?.id || 'me'}.json`;
                      document.body.appendChild(a);
                      a.click();
                      a.remove();
                      URL.revokeObjectURL(url);
                      toast.success(t('gdpr.exportSuccess'));
                    } catch (e) {
                      toast.error(t('gdpr.exportFailed'));
                    } finally {
                      setExporting(false);
                    }
                  }}
                  loading={exporting}
                  icon={Download}
                  variant="secondary"
                  className="rounded-xl px-5 sm:w-auto w-full"
                >
                  {t('gdpr.export')}
                </Button>
              </div>

              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between rounded-xl border border-rose-200/60 dark:border-rose-900/40 bg-rose-50/60 dark:bg-rose-950/20 p-4">
                <div>
                  <p className="text-[13px] font-medium text-rose-700 dark:text-rose-300">
                    {t('gdpr.delete')}
                  </p>
                  <p className="text-[11.5px] text-rose-600/80 dark:text-rose-400/80">
                    {t('gdpr.deleteDesc')}
                  </p>
                </div>
                <Button
                  onClick={() => setDeleteConfirm(true)}
                  icon={Trash2}
                  variant="danger"
                  className="rounded-xl px-5 sm:w-auto w-full"
                >
                  {t('gdpr.delete')}
                </Button>
              </div>
            </div>

            {deleteConfirm && (
              <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
                <div className="w-full max-w-sm rounded-2xl bg-white dark:bg-ink-900 p-6 shadow-xl">
                  <h3 className="text-[15px] font-semibold text-ink-900 dark:text-ink-100 mb-2">
                    {t('gdpr.confirmDelete')}
                  </h3>
                  <p className="text-[12px] text-ink-500 dark:text-ink-400 mb-4">
                    {t('gdpr.passwordRequired')}
                  </p>
                  <input
                    type="password"
                    value={deletePassword}
                    onChange={(e) => setDeletePassword(e.target.value)}
                    placeholder={t('account.profile.password.currentPassword')}
                    className="h-10 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20 mb-4"
                  />
                  <div className="flex gap-2 justify-end">
                    <Button variant="ghost" onClick={() => { setDeleteConfirm(false); setDeletePassword(''); }}>
                      {t('common.cancel')}
                    </Button>
                    <Button
                      variant="danger"
                      loading={deleting}
                      onClick={async () => {
                        if (!deletePassword) return;
                        setDeleting(true);
                        try {
                          await api.deleteUserData(deletePassword);
                          toast.success(t('gdpr.deleteSuccess'));
                          window.location.assign('/login');
                        } catch (e) {
                          toast.error(e?.message || t('gdpr.deleteFailed'));
                        } finally {
                          setDeleting(false);
                          setDeleteConfirm(false);
                          setDeletePassword('');
                        }
                      }}
                    >
                      {t('common.confirm')}
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
