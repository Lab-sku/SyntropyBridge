import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Plus,
  Copy,
  Trash2,
  Check,
  KeyRound,
  Search,
  MoreHorizontal,
  User as UserIcon,
  Download,
  Snowflake,
  Unlock,
  RotateCw,
} from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { copyToClipboard, formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Badge from '@/components/Badge';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton } from '@/components/Skeleton';

const AVATAR_GRADIENTS = [
  'from-blue-500 to-indigo-600',
  'from-emerald-500 to-green-600',
  'from-amber-500 to-orange-600',
  'from-violet-500 to-purple-600',
  'from-rose-500 to-pink-600',
  'from-cyan-500 to-teal-600',
  'from-ink-700 to-ink-900',
  'from-slate-500 to-zinc-600',
];

function QuotaBar({ used, quota, label }) {
  const pct = quota > 0 ? Math.min((used / quota) * 100, 100) : 0;
  const tone =
    pct >= 90
      ? 'from-rose-500 to-rose-600'
      : pct >= 70
        ? 'from-amber-500 to-orange-500'
        : 'from-blue-500 to-indigo-500';
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-[10.5px]">
        <span className="font-medium text-ink-500 dark:text-ink-400 dark:text-ink-500">{label}</span>
        <span className="font-mono text-ink-700 dark:text-ink-300">
          {used} / {quota}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-xl bg-ink-100/80 dark:bg-ink-800/80">
        <div
          className={`h-full rounded-full bg-gradient-to-r transition-all duration-500 ${tone}`}
          style={{ width: pct + '%' }}
        />
      </div>
    </div>
  );
}

function CreateUserDialog({ onClose, onCreated }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    username: '',
    email: '',
    password: '',
    quota_5h: 500,
    quota_week: 5000,
  });
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState(null);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const res = await api.createUser({
        username: form.username,
        email: form.email || undefined,
        password: form.password || undefined,
        quota_5h: parseInt(form.quota_5h) || 500,
        quota_week: parseInt(form.quota_week) || 5000,
      });
      setCreated(res);
      onCreated?.();
      toast.success(t('users.toast.created'));
    } catch (e) {
      toast.error(e.message || t('users.toast.createFailed'));
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <Dialog
      open
      onClose={onClose}
      title={created ? t('users.create.titleCreated') : t('users.create.title')}
      description={created ? t('users.create.descCreated') : t('users.create.description')}
      size="md"
      footer={
        created ? (
          <Button onClick={onClose}>{t('common.done')}</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button onClick={submit} loading={saving}>
              {t('users.create.submit')}
            </Button>
          </>
        )
      }
    >
      {created ? (
        <div className="space-y-3">
          <div className="rounded-xl border border-emerald-200 dark:border-emerald-800 bg-gradient-to-r from-emerald-50 to-green-50/50 p-3 text-[13px] text-emerald-700 dark:text-emerald-400">
            {t('users.create.successMessage', { username: created.username })}
          </div>
          <div>
            <div className="mb-1.5 text-[13px] font-medium text-ink-700 dark:text-ink-300">API Key</div>
            <div className="flex items-center gap-2 rounded-xl border border-ink-200 dark:border-ink-700 bg-ink-50 dark:bg-ink-900/60 dark:bg-ink-900/60 p-2.5">
              <code className="flex-1 break-all font-mono text-[12.5px] text-ink-800">
                {created.api_key}
              </code>
              <Button
                size="sm"
                variant="secondary"
                icon={Copy}
                onClick={async () => {
                  await copyToClipboard(created.api_key);
                  toast.success(t('users.toast.copied'));
                }}
              >
                {t('common.copy')}
              </Button>
            </div>
          </div>
          {created.generated_password ? (
            <div>
              <div className="mb-1.5 text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t('users.create.initialPassword')}{' '}
                <span className="text-ink-500 dark:text-ink-400 dark:text-ink-500">{t('users.create.passwordHint')}</span>
              </div>
              <div className="flex items-center gap-2 rounded-xl border border-amber-200 dark:border-amber-800 bg-gradient-to-r from-amber-50 to-orange-50/50 p-2.5">
                <code className="flex-1 break-all font-mono text-[12.5px] text-amber-900">
                  {created.generated_password}
                </code>
                <Button
                  size="sm"
                  variant="secondary"
                  icon={Copy}
                  onClick={async () => {
                    await copyToClipboard(created.generated_password);
                    toast.success(t('users.toast.copied'));
                  }}
                >
                  {t('common.copy')}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
              {t('users.form.username')}
            </label>
            <input
              required
              value={form.username}
              onChange={set('username')}
              placeholder="alice"
              className={inputCls}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t('users.form.emailOptional')}
              </label>
              <input
                value={form.email}
                onChange={set('email')}
                placeholder="alice@example.com"
                className={inputCls}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t('users.form.initialPasswordOptional')}
              </label>
              <input
                type="text"
                value={form.password}
                onChange={set('password')}
                placeholder={t('users.form.autoGeneratePassword')}
                className={inputCls}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t('users.form.quota5h')}
              </label>
              <input
                type="number"
                value={form.quota_5h}
                onChange={set('quota_5h')}
                placeholder="500"
                className={`${inputCls} font-mono text-[13px]`}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
                {t('users.form.quotaWeek')}
              </label>
              <input
                type="number"
                value={form.quota_week}
                onChange={set('quota_week')}
                placeholder="5000"
                className={`${inputCls} font-mono text-[13px]`}
              />
            </div>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function EditUserDialog({ user, onClose, onSaved }) {
  const { t } = useTranslation();
  const [form, setForm] = useState({
    quota_5h: user.quota_5h,
    quota_week: user.quota_week,
    is_active: user.is_active,
  });
  const [saving, setSaving] = useState(false);
  const set = (k) => (e) => {
    const v = k === 'is_active' ? e.target.checked : e.target.value;
    setForm((f) => ({ ...f, [k]: v }));
  };

  const save = async () => {
    setSaving(true);
    try {
      await api.updateUser(user.id, {
        quota_5h: parseInt(form.quota_5h) || 0,
        quota_week: parseInt(form.quota_week) || 0,
        is_active: form.is_active,
      });
      toast.success(t('users.toast.saved'));
      onSaved?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('users.toast.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 font-mono text-[12.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('users.edit.title', { username: user.username })}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={save} loading={saving}>
            {t('common.save')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
              {t('users.form.quota5h')}
            </label>
            <input
              type="number"
              value={form.quota_5h}
              onChange={set('quota_5h')}
              className={inputCls}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
              {t('users.form.quotaWeek')}
            </label>
            <input
              type="number"
              value={form.quota_week}
              onChange={set('quota_week')}
              className={inputCls}
            />
          </div>
        </div>
        <label className="flex items-center gap-2.5 rounded-xl border border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 bg-ink-50 dark:bg-ink-900/40 dark:bg-ink-900/40 p-3 transition-colors hover:bg-ink-50 dark:bg-ink-900/60 dark:hover:bg-ink-800/60 dark:bg-ink-900/60">
          <input
            type="checkbox"
            checked={form.is_active}
            onChange={set('is_active')}
            className="h-4 w-4 rounded border-ink-300 dark:border-ink-600 text-brand-500 focus:ring-brand-400/30"
          />
          <div>
            <div className="text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
              {t('users.edit.enableAccount')}
            </div>
            <div className="text-[10.5px] text-ink-500 dark:text-ink-400 dark:text-ink-500">{t('users.edit.enableHint')}</div>
          </div>
        </label>
        <div className="rounded-xl border border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 bg-ink-50 dark:bg-ink-900/40 dark:bg-ink-900/40 p-2.5 text-[11px] text-ink-600 dark:text-ink-400">
          <div className="flex items-center justify-between">
            <span className="text-ink-500 dark:text-ink-400 dark:text-ink-500">{t('users.edit.used5h')}</span>
            <span className="font-mono">{user.usage_5h ?? 0}</span>
          </div>
          <div className="mt-1 flex items-center justify-between">
            <span className="text-ink-500 dark:text-ink-400 dark:text-ink-500">{t('users.edit.usedThisWeek')}</span>
            <span className="font-mono">{user.usage_week ?? 0}</span>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

function FreezeDialog({ user, action, onClose, onDone }) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const isFreeze = action === 'freeze';

  const submit = async () => {
    setSaving(true);
    try {
      if (isFreeze) {
        await api.freezeUser(user.id, reason);
      } else {
        await api.unfreezeUser(user.id, reason);
      }
      toast.success(isFreeze ? t('users.toast.frozen') : t('users.toast.unfrozen'));
      onDone?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('users.toast.freezeFailed'));
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <Dialog
      open
      onClose={onClose}
      title={isFreeze ? t('users.freeze.title') : t('users.unfreeze.title')}
      description={
        isFreeze
          ? t('users.freeze.description', { username: user.username })
          : t('users.unfreeze.description', { username: user.username })
      }
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            variant={isFreeze ? 'danger' : 'success'}
            onClick={submit}
            loading={saving}
            icon={isFreeze ? Snowflake : Unlock}
          >
            {isFreeze ? t('users.freeze.submit') : t('users.unfreeze.submit')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
            {t('users.freezeReason')}
          </label>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t('users.freezeReasonPlaceholder')}
            className={inputCls}
          />
        </div>
      </div>
    </Dialog>
  );
}

function ResetPasswordDialog({ user, onClose, onDone }) {
  const { t } = useTranslation();
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [adminPassword, setAdminPassword] = useState('');
  const [saving, setSaving] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [showAdmin, setShowAdmin] = useState(false);

  const validate = () => {
    if (!newPassword) return t('users.resetPassword.validation.newRequired');
    if (newPassword.length < 12) return t('users.resetPassword.validation.minLength');
    const pwClasses = [
      /[a-z]/.test(newPassword),
      /[A-Z]/.test(newPassword),
      /[0-9]/.test(newPassword),
      /[^A-Za-z0-9]/.test(newPassword),
    ].filter(Boolean).length;
    if (pwClasses < 3) return t('users.resetPassword.validation.complexity');
    if (newPassword.includes(user.username))
      return t('users.resetPassword.validation.containsUsername');
    if (newPassword !== confirmPassword)
      return t('users.resetPassword.validation.mismatch');
    if (!adminPassword) return t('users.resetPassword.validation.adminRequired');
    return null;
  };

  const submit = async () => {
    const err = validate();
    if (err) {
      toast.error(err);
      return;
    }
    setSaving(true);
    try {
      await api.adminResetUserPassword(user.id, {
        admin_password: adminPassword,
        new_password: newPassword,
      });
      toast.success(t('users.resetPassword.toast.success', { username: user.username }));
      onDone?.();
      onClose?.();
    } catch (e) {
      toast.error(e.message || t('users.resetPassword.toast.failed'));
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 pr-9 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('users.resetPassword.title')}
      description={t('users.resetPassword.description', { username: user.username })}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={saving} icon={RotateCw}>
            {t('users.resetPassword.submit')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
            {t('users.resetPassword.newPassword')}
          </label>
          <div className="relative">
            <input
              type={showNew ? 'text' : 'password'}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t('users.resetPassword.newPasswordPlaceholder')}
              className={inputCls}
              autoFocus
            />
            <button
              type="button"
              onClick={() => setShowNew((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 hover:text-ink-600"
            >
              {showNew ? '🙈' : '👁'}
            </button>
          </div>
        </div>
        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
            {t('users.resetPassword.confirmPassword')}
          </label>
          <div className="relative">
            <input
              type={showConfirm ? 'text' : 'password'}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder={t('users.resetPassword.confirmPasswordPlaceholder')}
              className={inputCls}
            />
            <button
              type="button"
              onClick={() => setShowConfirm((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 hover:text-ink-600"
            >
              {showConfirm ? '🙈' : '👁'}
            </button>
          </div>
        </div>
        <div className="rounded-xl border border-amber-200/60 bg-amber-50/50 px-3 py-2.5 text-[12px] text-amber-700 dark:border-amber-900/30 dark:bg-amber-900/10 dark:text-amber-300">
          {t('users.resetPassword.warning')}
        </div>
        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-ink-700 dark:text-ink-300">
            {t('users.resetPassword.adminPassword')}
          </label>
          <div className="relative">
            <input
              type={showAdmin ? 'text' : 'password'}
              value={adminPassword}
              onChange={(e) => setAdminPassword(e.target.value)}
              placeholder={t('users.resetPassword.adminPasswordPlaceholder')}
              className={inputCls}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !saving) submit();
              }}
            />
            <button
              type="button"
              onClick={() => setShowAdmin((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-400 hover:text-ink-600"
            >
              {showAdmin ? '🙈' : '👁'}
            </button>
          </div>
          <p className="mt-1.5 text-[11px] text-ink-500 dark:text-ink-400">
            {t('users.resetPassword.adminPasswordHint')}
          </p>
        </div>
      </div>
    </Dialog>
  );
}

function UserRow({ u, idx, onEdit, onDelete, onCopyKey, onFreeze, onResetPassword }) {
  const { t } = useTranslation();
  const gradient = AVATAR_GRADIENTS[idx % AVATAR_GRADIENTS.length];
  return (
    <div className="group/row grid grid-cols-12 items-center gap-4 px-4 py-3 transition-colors duration-200 hover:bg-ink-50 dark:bg-ink-900/80 dark:bg-ink-900/80 dark:hover:bg-ink-800/80 last:border-b-0">
      <div className="col-span-3 flex items-center gap-3">
        <div
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${gradient} text-[12px] font-semibold text-white shadow-sm transition-transform duration-200 group-hover/row:scale-105`}
        >
          {u.username?.charAt(0).toUpperCase() || '?'}
        </div>
        <div className="min-w-0">
          <div className="truncate text-[12.5px] font-semibold text-ink-900 dark:text-ink-100">{u.username}</div>
          <div className="font-mono text-[10.5px] text-ink-400 dark:text-ink-500">#{u.id}</div>
        </div>
        {u.is_active ? (
          <Badge variant="success" dot>
            {t('users.active')}
          </Badge>
        ) : (
          <Badge variant="danger" dot>
            {t('users.frozen')}
          </Badge>
        )}
      </div>
      <div className="col-span-6 grid grid-cols-2 gap-4">
        <QuotaBar used={u.usage_5h ?? 0} quota={u.quota_5h ?? 0} label={t('users.quota.5hours')} />
        <QuotaBar
          used={u.usage_week ?? 0}
          quota={u.quota_week ?? 0}
          label={t('users.quota.thisWeek')}
        />
      </div>
      <div className="col-span-3 flex items-center justify-end gap-1.5">
        <Button
          size="sm"
          variant="ghost"
          icon={u.is_active ? Snowflake : Unlock}
          className={`transition-all duration-200 ${
            u.is_active
              ? 'hover:bg-amber-50 dark:bg-amber-900/20/50 dark:hover:bg-amber-900/20 hover:text-amber-600 dark:hover:text-amber-400'
              : 'hover:bg-emerald-50 dark:bg-emerald-900/20/50 dark:hover:bg-emerald-900/20 hover:text-emerald-600 dark:hover:text-emerald-400'
          }`}
          onClick={() => onFreeze(u, u.is_active ? 'freeze' : 'unfreeze')}
          title={u.is_active ? t('users.freeze.title') : t('users.unfreeze.title')}
        >
          {u.is_active ? t('users.freeze.title') : t('users.unfreeze.title')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          icon={KeyRound}
          className="transition-all duration-200 hover:bg-brand-50/50 dark:hover:bg-brand-900/20 hover:text-brand-600 dark:hover:text-brand-400"
          onClick={() => onCopyKey(u)}
        >
          {t('users.row.copyKey')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          icon={RotateCw}
          className="transition-all duration-200 hover:bg-amber-50/50 dark:hover:bg-amber-900/20 hover:text-amber-600 dark:hover:text-amber-400"
          onClick={() => onResetPassword(u)}
          title={t('users.resetPassword.title')}
        >
          {t('users.resetPassword.title')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="transition-all duration-200 hover:bg-ink-100/60 dark:bg-ink-800/60 dark:hover:bg-ink-800/60"
          onClick={() => onEdit(u)}
        >
          {t('users.row.edit')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          icon={Trash2}
          className="transition-all duration-200 hover:bg-rose-50/50 dark:hover:bg-rose-900/20 hover:text-rose-600 dark:hover:text-rose-400"
          onClick={() => onDelete(u)}
        />
      </div>
    </div>
  );
}

export default function Users() {
  const { t } = useTranslation();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const [freezing, setFreezing] = useState(null); // { user, action }
  const [resettingPw, setResettingPw] = useState(null); // user

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getUsers();
      setUsers(Array.isArray(data) ? data : []);
    } catch {
      setUsers([]);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const filtered = users.filter((u) => {
    if (!search) return true;
    return u.username?.toLowerCase().includes(search.toLowerCase());
  });

  const remove = async (u) => {
    if (!confirm(t('users.delete.confirm', { username: u.username }))) return;
    try {
      await api.deleteUser(u.id);
      toast.success(t('users.toast.deleted'));
      load();
    } catch (e) {
      toast.error(e.message || t('users.toast.deleteFailed'));
    }
  };

  const copyKey = async (u) => {
    if (!u.api_key) {
      toast.error(t('users.toast.noApiKey'));
      return;
    }
    await copyToClipboard(u.api_key);
    toast.success(t('users.toast.copied'));
  };

  const openFreeze = (u, action) => {
    setFreezing({ user: u, action });
  };

  const openResetPw = (u) => {
    setResettingPw(u);
  };

  const exportCsv = () => {
    window.open('/api/admin/users/export.csv', '_blank');
  };

  return (
    <>
      <TopBar
        title={t('users.title')}
        subtitle={t('users.subtitle', { count: users.length })}
        action={
          <div className="flex items-center gap-2">
            <div className="flex h-8 items-center gap-1.5 rounded-xl border border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 bg-white dark:bg-ink-900 px-3 shadow-soft transition-all focus-within:border-brand-400/40 focus-within:ring-2 focus-within:ring-brand-400/10">
              <Search size={12} className="text-ink-400 dark:text-ink-500" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('common.search')}
                className="w-32 bg-transparent text-[12.5px] text-ink-900 dark:text-ink-100 placeholder-ink-400 outline-none"
              />
            </div>
            <Button variant="secondary" size="sm" icon={Download} onClick={exportCsv}>
              {t('users.exportCsv')}
            </Button>
            <Button onClick={() => setCreating(true)} icon={Plus}>
              {t('users.addUser')}
            </Button>
          </div>
        }
      />
      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 via-ink-50/50 to-brand-50/30">
        <div className="mx-auto max-w-7xl p-4 md:p-6">
          <div className="rounded-2xl border border-ink-200 dark:border-ink-700/40 dark:border-ink-700/40 shadow-soft-lg overflow-hidden">
            <div className="hidden grid-cols-12 gap-4 border-b border-ink-100/60 dark:border-ink-800/60 bg-gradient-to-r from-ink-50/60 to-ink-50/30 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 dark:text-ink-400 dark:text-ink-500 md:grid">
              <div className="col-span-3">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">{t('common.user')}</span>
              </div>
              <div className="col-span-6">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">{t('users.quotaUsage')}</span>
              </div>
              <div className="col-span-3 text-right">
                <span className="rounded-md bg-ink-100/60 dark:bg-ink-800/60 px-2 py-0.5">{t('common.actions')}</span>
              </div>
            </div>
            {loading ? (
              <div className="p-3">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="border-b border-ink-100/60 dark:border-ink-800/60 px-1 py-3 last:border-b-0">
                    <div className="h-10 animate-pulse rounded-lg bg-ink-100/60 dark:bg-ink-800/60" />
                  </div>
                ))}
              </div>
            ) : filtered.length === 0 ? (
              <div className="p-8">
                <EmptyState
                  icon={UserIcon}
                  title={search ? t('users.empty.noMatch') : t('users.empty.noUsers')}
                  description={search ? t('users.empty.trySearch') : t('users.empty.addFirst')}
                />
              </div>
            ) : (
              <div>
                {filtered.map((u, i) => (
                  <UserRow
                  key={u.id}
                  u={u}
                  idx={i}
                  onEdit={setEditing}
                  onDelete={remove}
                  onCopyKey={copyKey}
                  onFreeze={openFreeze}
                  onResetPassword={openResetPw}
                />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {creating && <CreateUserDialog onClose={() => setCreating(false)} onCreated={load} />}
      {editing && <EditUserDialog user={editing} onClose={() => setEditing(null)} onSaved={load} />}
      {freezing && (
        <FreezeDialog
          user={freezing.user}
          action={freezing.action}
          onClose={() => setFreezing(null)}
          onDone={load}
        />
      )}
      {resettingPw && (
        <ResetPasswordDialog
          user={resettingPw}
          onClose={() => setResettingPw(null)}
          onDone={load}
        />
      )}
    </>
  );
}
