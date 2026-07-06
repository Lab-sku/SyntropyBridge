import { useEffect, useState, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Wallet, Search, AlertTriangle, ArrowDownCircle, ArrowUpCircle } from 'lucide-react';
import { toast } from 'sonner';
import api from '@/lib/api';
import { formatDate } from '@/lib/utils';
import TopBar from '@/components/TopBar';
import Button from '@/components/Button';
import Dialog from '@/components/Dialog';
import EmptyState from '@/components/EmptyState';
import { CardSkeleton, TableRowSkeleton } from '@/components/Skeleton';

export default function AdminWalletAdjust() {
  const { t } = useTranslation();
  const [users, setUsers] = useState([]);
  const [search, setSearch] = useState('');
  const [showDropdown, setShowDropdown] = useState(false);
  const [selectedUser, setSelectedUser] = useState(null);
  const [wallet, setWallet] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [loadingWallet, setLoadingWallet] = useState(false);
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [resultBalance, setResultBalance] = useState(null);
  const dropdownRef = useRef(null);

  const inputCls =
    'h-9 w-full rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 text-[13.5px] outline-none transition-all focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20';

  useEffect(() => {
    api
      .getUsers()
      .then((data) => setUsers(Array.isArray(data) ? data : []))
      .catch(() => []);
  }, []);

  const filteredUsers = users.filter((u) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      u.username?.toLowerCase().includes(q) ||
      String(u.id).includes(q) ||
      u.email?.toLowerCase().includes(q)
    );
  });

  const loadUserData = useCallback(async (userId) => {
    setLoadingWallet(true);
    try {
      const [w, tx] = await Promise.all([
        api.getAdminUserWallet(userId).catch(() => null),
        api.getAdminWalletTransactions(userId, 50).catch(() => []),
      ]);
      setWallet(w);
      setTransactions(Array.isArray(tx) ? tx : []);
    } finally {
      setLoadingWallet(false);
    }
  }, []);

  const selectUser = (u) => {
    setSelectedUser(u);
    setSearch(u.username);
    setShowDropdown(false);
    setResultBalance(null);
    setAmount('');
    setReason('');
    loadUserData(u.id);
  };

  const openConfirm = () => {
    if (!selectedUser) {
      toast.error(t('adminWalletAdjust.toast.selectUserFirst'));
      return;
    }
    if (!amount || Number(amount) === 0) return;
    setConfirmOpen(true);
  };

  const submitAdjust = async () => {
    setSubmitting(true);
    try {
      const delta = Number(amount);
      const res = await api.adjustAdminWallet(selectedUser.id, delta, reason || 'admin_adjust');
      setResultBalance(res.balance);
      toast.success(t('adminWalletAdjust.toast.adjusted'));
      setConfirmOpen(false);
      setAmount('');
      setReason('');
      loadUserData(selectedUser.id);
    } catch (e) {
      toast.error(e.message || t('adminWalletAdjust.toast.adjustFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const delta = Number(amount) || 0;
  const isDebit = delta < 0;

  useEffect(() => {
    const handler = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <>
      <TopBar title={t('adminWalletAdjust.title')} subtitle={t('adminWalletAdjust.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-gradient-to-br from-ink-50/80 via-ink-50/50 to-brand-50/30">
        <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
          {/* User selector */}
          <div className="card rounded-2xl border-ink-200/40 shadow-soft-lg p-5">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 text-white">
                <Search size={14} />
              </div>
              <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                {t('adminWalletAdjust.selectUser')}
              </div>
            </div>
            <div ref={dropdownRef} className="relative">
              <div className="flex h-10 items-center gap-2 rounded-xl border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 px-3 shadow-soft transition-all focus-within:border-brand-400 focus-within:ring-2 focus-within:ring-brand-400/20">
                <Search size={14} className="text-ink-400" />
                <input
                  value={search}
                  onChange={(e) => {
                    setSearch(e.target.value);
                    setShowDropdown(true);
                  }}
                  onFocus={() => setShowDropdown(true)}
                  placeholder={t('adminWalletAdjust.searchUser')}
                  className="flex-1 bg-transparent text-[13.5px] text-ink-900 placeholder-ink-400 outline-none"
                />
              </div>
              {showDropdown && filteredUsers.length > 0 && (
                <div className="absolute z-20 mt-1 max-h-60 w-full overflow-y-auto rounded-xl border border-ink-200/40 dark:border-ink-700/40 bg-white dark:bg-ink-900 shadow-lg">
                  {filteredUsers.slice(0, 20).map((u) => (
                    <button
                      key={u.id}
                      onClick={() => selectUser(u)}
                      className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-ink-50 dark:hover:bg-ink-800"
                    >
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-ink-700 to-ink-900 text-[11px] font-semibold text-white">
                        {u.username?.charAt(0).toUpperCase() || '?'}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[12.5px] font-medium text-ink-900 dark:text-ink-100">
                          {u.username}
                        </div>
                        <div className="truncate text-[10.5px] text-ink-500 dark:text-ink-400">
                          #{u.id}
                          {u.email ? ` · ${u.email}` : ''}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Wallet balance + adjustment form */}
          {selectedUser && (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {/* Balance display */}
              <div className="card rounded-2xl border-ink-200/40 shadow-soft-lg p-5">
                {loadingWallet ? (
                  <CardSkeleton />
                ) : wallet ? (
                  <div className="space-y-4">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-emerald-500 to-green-600 text-white">
                        <Wallet size={14} />
                      </div>
                      <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                        {t('adminWalletAdjust.currentBalance')}
                      </div>
                    </div>
                    <div className="text-[28px] font-bold tracking-tight text-ink-900 dark:text-ink-100">
                      {Number(wallet.balance || 0).toFixed(2)}{' '}
                      <span className="text-[14px] font-medium text-ink-500 dark:text-ink-400">
                        {t('common.currency')}
                      </span>
                    </div>
                    {resultBalance !== null && (
                      <div className="rounded-xl border border-emerald-200 dark:border-emerald-800 bg-gradient-to-r from-emerald-50 to-green-50/50 dark:from-emerald-900/20 dark:to-green-900/10 px-4 py-3 text-[13px] text-emerald-700 dark:text-emerald-400">
                        {t('adminWalletAdjust.result.success')} —{' '}
                        {t('adminWalletAdjust.result.newBalance')}:{' '}
                        <span className="font-bold">{Number(resultBalance).toFixed(2)}</span>
                      </div>
                    )}
                    <div className="grid grid-cols-2 gap-3">
                      <div className="rounded-xl border border-ink-200/40 bg-ink-50/40 p-3">
                        <div className="text-[10.5px] text-ink-500 dark:text-ink-400">
                          {t('adminWalletAdjust.totalRecharged')}
                        </div>
                        <div className="mt-1 font-mono text-[14px] font-semibold text-emerald-700">
                          +{Number(wallet.total_recharged || 0).toFixed(2)}
                        </div>
                      </div>
                      <div className="rounded-xl border border-ink-200/40 bg-ink-50/40 p-3">
                        <div className="text-[10.5px] text-ink-500 dark:text-ink-400">
                          {t('adminWalletAdjust.totalConsumed')}
                        </div>
                        <div className="mt-1 font-mono text-[14px] font-semibold text-rose-700">
                          -{Number(wallet.total_consumed || 0).toFixed(2)}
                        </div>
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>

              {/* Adjustment form */}
              <div className="card rounded-2xl border-ink-200/40 shadow-soft-lg p-5">
                <div className="mb-4 flex items-center gap-3">
                  <div
                    className={`flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br ${isDebit ? 'from-rose-500 to-pink-600' : 'from-blue-500 to-indigo-600'} text-white`}
                  >
                    {isDebit ? <ArrowDownCircle size={14} /> : <ArrowUpCircle size={14} />}
                  </div>
                  <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('adminWalletAdjust.form.amount')}
                  </div>
                </div>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
                      {t('adminWalletAdjust.form.amount')}
                    </label>
                    <input
                      type="number"
                      step="0.01"
                      value={amount}
                      onChange={(e) => setAmount(e.target.value)}
                      placeholder={t('adminWalletAdjust.form.amountPlaceholder')}
                      className={`${inputCls} font-mono text-[13px]`}
                    />
                    <div className="mt-1 text-[10.5px] text-ink-500 dark:text-ink-400">
                      {t('adminWalletAdjust.form.amountHint')}
                    </div>
                  </div>
                  <div>
                    <label className="mb-1.5 block text-[12px] font-medium text-ink-700 dark:text-ink-300">
                      {t('adminWalletAdjust.form.reason')}
                    </label>
                    <input
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      placeholder={t('adminWalletAdjust.form.reasonPlaceholder')}
                      className={inputCls}
                    />
                  </div>
                  <Button
                    variant={isDebit ? 'danger' : 'primary'}
                    onClick={openConfirm}
                    disabled={!amount || Number(amount) === 0}
                    className="w-full"
                  >
                    {t('adminWalletAdjust.form.submit')}
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* Transaction history */}
          {selectedUser && (
            <div className="card rounded-2xl border-ink-200/40 shadow-soft-lg overflow-hidden">
              <div className="border-b border-ink-100/60 dark:border-ink-800/60 bg-gradient-to-r from-ink-50/60 to-ink-50/30 dark:from-ink-900/60 dark:to-ink-900/30 px-5 py-3">
                <div className="text-[13px] font-semibold text-ink-900 dark:text-ink-100">
                  {t('adminWalletAdjust.transactions.title')}
                </div>
              </div>
              {loadingWallet ? (
                <div className="p-3">
                  {Array.from({ length: 3 }).map((_, i) => (
                    <TableRowSkeleton key={i} cols={5} />
                  ))}
                </div>
              ) : transactions.length === 0 ? (
                <div className="p-8">
                  <EmptyState
                    icon={Wallet}
                    title={t('adminWalletAdjust.empty.title')}
                    description={t('adminWalletAdjust.empty.description')}
                  />
                </div>
              ) : (
                <div>
                  <div className="hidden grid-cols-12 gap-4 border-b border-ink-100/60 dark:border-ink-800/60 bg-ink-50/30 dark:bg-ink-900/30 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-ink-500 md:grid">
                    <div className="col-span-2">{t('adminWalletAdjust.transactions.type')}</div>
                    <div className="col-span-2">{t('adminWalletAdjust.transactions.amount')}</div>
                    <div className="col-span-2">
                      {t('adminWalletAdjust.transactions.balanceAfter')}
                    </div>
                    <div className="col-span-4">{t('adminWalletAdjust.transactions.note')}</div>
                    <div className="col-span-2">
                      {t('adminWalletAdjust.transactions.createdAt')}
                    </div>
                  </div>
                  {transactions.map((tx) => (
                    <div
                      key={tx.id}
                      className="grid grid-cols-12 items-center gap-4 border-b border-ink-100/60 dark:border-ink-800/60 px-4 py-2.5 text-[12.5px] hover:bg-ink-50/80 dark:hover:bg-ink-900/80 last:border-b-0"
                    >
                      <div className="col-span-2">
                        <span className="inline-flex items-center gap-1 rounded-full bg-ink-100/80 dark:bg-ink-800/80 px-2 py-0.5 text-[11px] font-medium text-ink-700 dark:text-ink-300">
                          {tx.type}
                        </span>
                      </div>
                      <div
                        className={`col-span-2 font-mono text-[12px] font-medium ${Number(tx.amount) >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}
                      >
                        {Number(tx.amount) >= 0 ? '+' : ''}
                        {Number(tx.amount).toFixed(2)}
                      </div>
                      <div className="col-span-2 font-mono text-[12px] text-ink-700 dark:text-ink-300">
                        {Number(tx.balance_after || 0).toFixed(2)}
                      </div>
                      <div className="col-span-4 truncate text-[11.5px] text-ink-500 dark:text-ink-400">
                        {tx.note || '-'}
                      </div>
                      <div className="col-span-2 text-[11px] text-ink-500 dark:text-ink-400">
                        {formatDate(tx.created_at)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Confirmation dialog */}
      {confirmOpen && selectedUser && (
        <Dialog
          open
          onClose={() => setConfirmOpen(false)}
          title={t('adminWalletAdjust.confirm.title')}
          size="md"
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
                {t('common.cancel')}
              </Button>
              <Button
                variant={isDebit ? 'danger' : 'success'}
                onClick={submitAdjust}
                loading={submitting}
              >
                {t('common.confirm')}
              </Button>
            </>
          }
        >
          <div className="space-y-3">
            <div
              className={`rounded-xl border p-4 text-[13px] ${isDebit ? 'border-rose-200 dark:border-rose-800 bg-gradient-to-r from-rose-50 to-pink-50/50 dark:from-rose-900/20 dark:to-pink-900/10 text-rose-700 dark:text-rose-400' : 'border-emerald-200 dark:border-emerald-800 bg-gradient-to-r from-emerald-50 to-green-50/50 dark:from-emerald-900/20 dark:to-green-900/10 text-emerald-700 dark:text-emerald-400'}`}
            >
              {isDebit
                ? t('adminWalletAdjust.confirm.debitDesc', {
                    username: selectedUser.username,
                    amount: Math.abs(delta).toFixed(2),
                  })
                : t('adminWalletAdjust.confirm.creditDesc', {
                    username: selectedUser.username,
                    amount: Math.abs(delta).toFixed(2),
                  })}
            </div>
            {isDebit && (
              <div className="flex items-start gap-2 rounded-xl border border-amber-200 dark:border-amber-800 bg-gradient-to-r from-amber-50 to-orange-50/50 dark:from-amber-900/20 dark:to-orange-900/10 p-3 text-[12px] text-amber-700 dark:text-amber-400">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <span>{t('adminWalletAdjust.confirm.debitWarning')}</span>
              </div>
            )}
            {reason && (
              <div className="rounded-xl border border-ink-200/40 bg-ink-50/40 p-3">
                <div className="text-[10.5px] text-ink-500 dark:text-ink-400">
                  {t('adminWalletAdjust.confirm.reasonLabel')}
                </div>
                <div className="mt-1 text-[13px] font-medium text-ink-900">{reason}</div>
              </div>
            )}
          </div>
        </Dialog>
      )}
    </>
  );
}
