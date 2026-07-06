import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CreditCard, Plus, Pencil, Trash2, Search, Users } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import Badge from '@/components/Badge';

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

const CODE_RE = /^[a-zA-Z0-9_]{3,50}$/;

function validatePlan(form, isEdit) {
  if (!form.name || form.name.trim().length === 0) return 'nameRequired';
  if (form.name.length > 100) return 'nameTooLong';
  if (!isEdit) {
    if (!form.code || !CODE_RE.test(form.code)) return 'codeInvalid';
  }
  const dr = parseFloat(form.discount_rate);
  if (Number.isNaN(dr) || dr < 0 || dr > 1) return 'discountRateInvalid';
  return null;
}

// ---------------------------------------------------------------------------
// Plan form dialog (create + edit)
// ---------------------------------------------------------------------------

function PlanDialog({ plan, onClose, onSaved }) {
  const { t } = useTranslation();
  const isEdit = Boolean(plan);

  const [form, setForm] = useState({
    name: plan?.name || '',
    code: plan?.code || '',
    monthly_price: plan?.monthly_price ?? 0,
    monthly_credits: plan?.monthly_credits ?? 0,
    discount_rate: plan?.discount_rate ?? 1.0,
    max_api_keys: plan?.max_api_keys ?? 1,
    max_concurrent: plan?.max_concurrent ?? 5,
    rate_limit_rpm: plan?.rate_limit_rpm ?? 60,
    rate_limit_tpm: plan?.rate_limit_tpm ?? 100000,
    sort_order: plan?.sort_order ?? 0,
    is_active: plan?.is_active ?? true,
  });
  const [saving, setSaving] = useState(false);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    const errKey = validatePlan(form, isEdit);
    if (errKey) {
      toast.error(t(`plans.validation.${errKey}`));
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim(),
        code: form.code.trim(),
        monthly_price: parseFloat(form.monthly_price) || 0,
        monthly_credits: parseInt(form.monthly_credits) || 0,
        discount_rate: parseFloat(form.discount_rate),
        max_api_keys: parseInt(form.max_api_keys) || 1,
        max_concurrent: parseInt(form.max_concurrent) || 5,
        rate_limit_rpm: parseInt(form.rate_limit_rpm) || 60,
        rate_limit_tpm: parseInt(form.rate_limit_tpm) || 100000,
        sort_order: parseInt(form.sort_order) || 0,
        is_active: Boolean(form.is_active),
      };
      if (isEdit) {
        await api.updateAdminPlan(plan.id, payload);
        toast.success(t('plans.toast.updated'));
      } else {
        await api.createAdminPlan(payload);
        toast.success(t('plans.toast.created'));
      }
      onSaved?.();
      onClose();
    } catch (err) {
      toast.error(err.message || t('common.operationFailed'));
    } finally {
      setSaving(false);
    }
  };

  const fields = [
    { key: 'code', label: t('plans.form.code'), type: 'text', readOnly: isEdit, mono: true },
    { key: 'name', label: t('plans.form.name'), type: 'text' },
    { key: 'monthly_price', label: t('plans.form.monthlyPrice'), type: 'number' },
    { key: 'monthly_credits', label: t('plans.form.monthlyCredits'), type: 'number' },
    { key: 'discount_rate', label: t('plans.form.discountRate'), type: 'number', step: '0.01' },
    { key: 'max_api_keys', label: t('plans.form.maxApiKeys'), type: 'number' },
    { key: 'max_concurrent', label: t('plans.form.maxConcurrent'), type: 'number' },
    { key: 'rate_limit_rpm', label: t('plans.form.rateLimitRpm'), type: 'number' },
    { key: 'rate_limit_tpm', label: t('plans.form.rateLimitTpm'), type: 'number' },
    { key: 'sort_order', label: t('plans.form.sortOrder'), type: 'number' },
  ];

  return (
    <Dialog
      open
      onClose={onClose}
      title={isEdit ? t('plans.edit.title') : t('plans.create.title')}
      description={isEdit ? t('plans.edit.description') : t('plans.create.description')}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} loading={saving} icon={isEdit ? Pencil : Plus}>
            {isEdit ? t('common.save') : t('common.create')}
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          {fields.map((f) => (
            <div key={f.key}>
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
                {f.label}
              </label>
              <input
                type={f.type}
                value={form[f.key]}
                onChange={set(f.key)}
                readOnly={f.readOnly}
                step={f.step}
                className={`h-9 w-full rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13px] outline-none focus:border-ink-900 dark:focus:border-ink-100 focus:ring-2 focus:ring-ink-900/10 dark:focus:ring-ink-100/20 ${f.mono ? 'font-mono' : ''} ${f.readOnly ? 'cursor-not-allowed bg-ink-50 dark:bg-ink-800 text-ink-400 dark:text-ink-500' : ''}`}
              />
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2 pt-1">
          <input
            type="checkbox"
            id="plan-is-active"
            checked={Boolean(form.is_active)}
            onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))}
            className="h-4 w-4 rounded border-ink-300 dark:border-ink-600"
          />
          <label htmlFor="plan-is-active" className="text-[12.5px] font-medium text-ink-700 dark:text-ink-300">
            {t('plans.form.isActive')}
          </label>
        </div>
      </form>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Delete confirmation dialog
// ---------------------------------------------------------------------------

function DeleteDialog({ plan, onClose, onDeleted }) {
  const { t } = useTranslation();
  const [deleting, setDeleting] = useState(false);

  const submit = async () => {
    setDeleting(true);
    try {
      await api.deleteAdminPlan(plan.id);
      toast.success(t('plans.toast.deleted'));
      onDeleted?.();
      onClose();
    } catch (err) {
      toast.error(err.message || t('common.operationFailed'));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={t('plans.delete.title')}
      description={t('plans.delete.description', { name: plan.name })}
      size="sm"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" onClick={submit} loading={deleting} icon={Trash2}>
            {t('common.delete')}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-ink-600">
        {t('plans.delete.confirmBody', { name: plan.name, code: plan.code })}
      </p>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function AdminPlans() {
  const { t } = useTranslation();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState(null); // null | 'new' | plan object
  const [deleting, setDeleting] = useState(null); // null | plan object

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.listAdminPlans();
      setItems(Array.isArray(list) ? list : []);
    } catch (e) {
      toast.error(e.message || t('plans.toast.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (p) => (p.code || '').toLowerCase().includes(q) || (p.name || '').toLowerCase().includes(q),
    );
  }, [items, search]);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-ink-50 dark:bg-ink-900">
      <TopBar
        title={t('plans.title')}
        subtitle={t('plans.subtitle')}
        action={
          <Button icon={Plus} onClick={() => setEditing('new')}>
            {t('plans.create.title')}
          </Button>
        }
      />

      <div className="flex-1 overflow-y-auto px-4 pb-12 pt-4 md:px-6">
        {/* Search */}
        <div className="mb-3 flex items-center gap-2">
          <div className="ml-auto flex items-center gap-1.5 rounded-md border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-2.5">
            <Search size={12} className="text-ink-400 dark:text-ink-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('plans.searchPlaceholder')}
              className="h-7 w-48 bg-transparent text-[12px] outline-none placeholder:text-ink-400 dark:placeholder:text-ink-500"
            />
          </div>
        </div>

        {/* Table */}
        <div className="overflow-x-auto rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 shadow-soft">
          <table className="w-full text-[12.5px]">
            <thead className="border-b border-ink-200 dark:border-ink-700 bg-ink-50/60 dark:bg-ink-900/60 text-[11px] uppercase tracking-wider text-ink-500 dark:text-ink-400">
              <tr>
                <th className="px-3 py-2 text-left">{t('plans.col.code')}</th>
                <th className="px-3 py-2 text-left">{t('plans.col.name')}</th>
                <th className="px-3 py-2 text-right">{t('plans.col.monthlyPrice')}</th>
                <th className="px-3 py-2 text-right">{t('plans.col.discountRate')}</th>
                <th className="px-3 py-2 text-right">{t('plans.col.monthlyCredits')}</th>
                <th className="px-3 py-2 text-right">{t('plans.col.rpm')}</th>
                <th className="px-3 py-2 text-center">{t('plans.col.subscribers')}</th>
                <th className="px-3 py-2 text-center">{t('common.status')}</th>
                <th className="px-3 py-2 text-center">{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={9} className="px-3 py-8 text-center text-ink-400">
                    {t('common.loading')}
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-3 py-2">
                    <EmptyState
                      icon={CreditCard}
                      title={t('plans.empty.title')}
                      description={t('plans.empty.description')}
                    />
                  </td>
                </tr>
              ) : (
                filtered.map((p) => (
                  <tr
                    key={p.id}
                    className="border-b border-ink-100 dark:border-ink-800 last:border-b-0 hover:bg-ink-50/40 dark:hover:bg-ink-900/40"
                  >
                    <td className="px-3 py-2.5">
                      <code className="rounded bg-ink-50 dark:bg-ink-900 px-1.5 py-0.5 font-mono text-[11.5px] text-ink-800 dark:text-ink-200">
                        {p.code}
                      </code>
                    </td>
                    <td className="px-3 py-2.5 font-medium text-ink-900 dark:text-ink-100">{p.name}</td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {p.monthly_price}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {p.discount_rate}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {p.monthly_credits}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-ink-700 dark:text-ink-300">
                      {p.rate_limit_rpm}
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <span className="inline-flex items-center gap-1 text-ink-600">
                        <Users size={12} />
                        <span className="tabular-nums">{p.subscriber_count || 0}</span>
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      {p.is_active ? (
                        <Badge variant="success" dot>
                          {t('common.enabled')}
                        </Badge>
                      ) : (
                        <Badge variant="default" dot>
                          {t('common.disabled')}
                        </Badge>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex items-center justify-center gap-1">
                        <button
                          onClick={() => setEditing(p)}
                          className="rounded p-1.5 text-ink-400 dark:text-ink-500 hover:bg-ink-100 dark:hover:bg-ink-800 hover:text-ink-700 dark:hover:text-ink-300"
                          title={t('common.edit')}
                        >
                          <Pencil size={13} />
                        </button>
                        <button
                          onClick={() => setDeleting(p)}
                          className="rounded p-1.5 text-ink-300 dark:text-ink-600 hover:bg-rose-50 dark:hover:bg-rose-900/20 hover:text-rose-600 dark:hover:text-rose-400"
                          title={t('common.delete')}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {editing && (
        <PlanDialog
          plan={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={load}
        />
      )}
      {deleting && (
        <DeleteDialog plan={deleting} onClose={() => setDeleting(null)} onDeleted={load} />
      )}
    </div>
  );
}
