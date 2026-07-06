import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Mail, Save, RefreshCw, Send, CheckCircle, AlertCircle, Settings } from 'lucide-react';
import api from '@/lib/api';
import TopBar from '@/components/TopBar';
import { toast } from 'sonner';

export default function AdminSettings() {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testEmail, setTestEmail] = useState('');
  const [config, setConfig] = useState({
    smtp_host: '',
    smtp_port: '587',
    smtp_user: '',
    smtp_password: '',
    smtp_from: '',
    email_verification_enabled: false,
  });

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    setLoading(true);
    try {
      const data = await api.getConfig();
      setConfig({
        smtp_host: data.smtp_host || '',
        smtp_port: data.smtp_port || '587',
        smtp_user: data.smtp_user || '',
        smtp_password: '',  // 不加载密码，只在修改时更新
        smtp_from: data.smtp_from || '',
        email_verification_enabled: data.email_verification_enabled ?? false,
      });
    } catch (err) {
      toast.error(t('admin.settings.loadError'));
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const data = {
        smtp_host: config.smtp_host,
        smtp_port: config.smtp_port,
        smtp_user: config.smtp_user,
        smtp_from: config.smtp_from,
        email_verification_enabled: String(config.email_verification_enabled),
      };
      // 只在输入了新密码时更新
      if (config.smtp_password) {
        data.smtp_password = config.smtp_password;
      }
      await api.saveConfig(data);
      toast.success(t('admin.settings.saveSuccess'));
      // 重新加载配置以获取掩码后的密码
      loadConfig();
    } catch (err) {
      toast.error(t('admin.settings.saveError'));
    } finally {
      setSaving(false);
    }
  };

  const handleTestEmail = async () => {
    if (!testEmail) {
      toast.error(t('admin.settings.enterTestEmail'));
      return;
    }
    setTesting(true);
    try {
      const result = await api.request('/admin/test-email', {
        method: 'POST',
        body: { email: testEmail },
      });
      if (result.success) {
        toast.success(t('admin.settings.testEmailSent'));
      } else {
        toast.error(result.message || t('admin.settings.testEmailFailed'));
      }
    } catch (err) {
      toast.error(t('admin.settings.testEmailFailed'));
    } finally {
      setTesting(false);
    }
  };

  const handleChange = (field, value) => {
    setConfig(prev => ({ ...prev, [field]: value }));
  };

  if (loading) {
    return (
      <>
        <TopBar title={t('admin.settings.title')} subtitle={t('admin.settings.subtitle')} />
        <div className="flex-1 flex items-center justify-center">
          <RefreshCw className="animate-spin text-ink-400" size={24} />
        </div>
      </>
    );
  }

  return (
    <>
      <TopBar title={t('admin.settings.title')} subtitle={t('admin.settings.subtitle')} />
      <div className="flex-1 overflow-y-auto bg-ink-50/50 dark:bg-ink-900/50">
        <div className="mx-auto max-w-3xl p-4 md:p-6 space-y-6">
          
          {/* SMTP 配置卡片 */}
          <div className="card overflow-hidden rounded-2xl border border-ink-200 dark:border-ink-700 shadow-soft-lg">
            <div className="p-5 border-b border-ink-200 dark:border-ink-700 bg-gradient-to-r from-ink-50 to-white dark:from-ink-900 dark:to-ink-950">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white shadow-md">
                  <Mail size={20} />
                </div>
                <div>
                  <div className="text-[15px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('admin.settings.smtpConfig')}
                  </div>
                  <div className="text-[12px] text-ink-500 dark:text-ink-400">
                    {t('admin.settings.smtpDesc')}
                  </div>
                </div>
              </div>
            </div>

            <div className="p-5 space-y-4">
              {/* SMTP Host */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-[12.5px] font-medium text-ink-700 dark:text-ink-300 mb-1.5">
                    SMTP Host
                  </label>
                  <input
                    type="text"
                    value={config.smtp_host}
                    onChange={(e) => handleChange('smtp_host', e.target.value)}
                    placeholder="smtp.gmail.com"
                    className="w-full px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
                <div>
                  <label className="block text-[12.5px] font-medium text-ink-700 dark:text-ink-300 mb-1.5">
                    SMTP Port
                  </label>
                  <input
                    type="text"
                    value={config.smtp_port}
                    onChange={(e) => handleChange('smtp_port', e.target.value)}
                    placeholder="587"
                    className="w-full px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                  />
                </div>
              </div>

              {/* SMTP User */}
              <div>
                <label className="block text-[12.5px] font-medium text-ink-700 dark:text-ink-300 mb-1.5">
                  {t('admin.settings.smtpUser')}
                </label>
                <input
                  type="text"
                  value={config.smtp_user}
                  onChange={(e) => handleChange('smtp_user', e.target.value)}
                  placeholder="your-email@gmail.com"
                  className="w-full px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>

              {/* SMTP Password */}
              <div>
                <label className="block text-[12.5px] font-medium text-ink-700 dark:text-ink-300 mb-1.5">
                  {t('admin.settings.smtpPassword')}
                </label>
                <input
                  type="password"
                  value={config.smtp_password}
                  onChange={(e) => handleChange('smtp_password', e.target.value)}
                  placeholder="••••••••"
                  className="w-full px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
                <p className="mt-1 text-[11px] text-ink-400 dark:text-ink-500">
                  {t('admin.settings.passwordHint')}
                </p>
              </div>

              {/* From Email */}
              <div>
                <label className="block text-[12.5px] font-medium text-ink-700 dark:text-ink-300 mb-1.5">
                  {t('admin.settings.fromEmail')}
                </label>
                <input
                  type="text"
                  value={config.smtp_from}
                  onChange={(e) => handleChange('smtp_from', e.target.value)}
                  placeholder="noreply@your-domain.com"
                  className="w-full px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>
            </div>
          </div>

          {/* 邮箱验证开关卡片 */}
          <div className="card overflow-hidden rounded-2xl border border-ink-200 dark:border-ink-700 shadow-soft-lg">
            <div className="p-5 border-b border-ink-200 dark:border-ink-700 bg-gradient-to-r from-ink-50 to-white dark:from-ink-900 dark:to-ink-950">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500 to-green-600 text-white shadow-md">
                  <Settings size={20} />
                </div>
                <div>
                  <div className="text-[15px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('admin.settings.emailVerification')}
                  </div>
                  <div className="text-[12px] text-ink-500 dark:text-ink-400">
                    {t('admin.settings.emailVerificationDesc')}
                  </div>
                </div>
              </div>
            </div>

            <div className="p-5">
              <div className="flex items-center justify-between p-4 rounded-xl bg-ink-50/60 dark:bg-ink-900/60">
                <div>
                  <div className="text-[13px] font-medium text-ink-900 dark:text-ink-100">
                    {t('admin.settings.enableEmailVerification')}
                  </div>
                  <div className="text-[12px] text-ink-500 dark:text-ink-400 mt-1">
                    {t('admin.settings.enableEmailVerificationDesc')}
                  </div>
                </div>
                <button
                  onClick={() => handleChange('email_verification_enabled', !config.email_verification_enabled)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
                    config.email_verification_enabled
                      ? 'bg-blue-600'
                      : 'bg-ink-200 dark:bg-ink-700'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                      config.email_verification_enabled ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>
              
              {!config.smtp_host && (
                <div className="mt-4 flex items-center gap-2 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
                  <AlertCircle size={16} className="text-amber-600 dark:text-amber-400" />
                  <span className="text-[12px] text-amber-700 dark:text-amber-300">
                    {t('admin.settings.smtpNotConfiguredWarning')}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* 测试邮件卡片 */}
          <div className="card overflow-hidden rounded-2xl border border-ink-200 dark:border-ink-700 shadow-soft-lg">
            <div className="p-5 border-b border-ink-200 dark:border-ink-700 bg-gradient-to-r from-ink-50 to-white dark:from-ink-900 dark:to-ink-950">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-purple-600 text-white shadow-md">
                  <Send size={20} />
                </div>
                <div>
                  <div className="text-[15px] font-semibold text-ink-900 dark:text-ink-100">
                    {t('admin.settings.testEmail')}
                  </div>
                  <div className="text-[12px] text-ink-500 dark:text-ink-400">
                    {t('admin.settings.testEmailDesc')}
                  </div>
                </div>
              </div>
            </div>

            <div className="p-5">
              <div className="flex gap-3">
                <input
                  type="email"
                  value={testEmail}
                  onChange={(e) => setTestEmail(e.target.value)}
                  placeholder={t('admin.settings.testEmailPlaceholder')}
                  className="flex-1 px-3 py-2 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-900 dark:text-ink-100 text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
                <button
                  onClick={handleTestEmail}
                  disabled={testing || !config.smtp_host}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg bg-violet-600 hover:bg-violet-700 text-white text-[13px] font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {testing ? (
                    <RefreshCw size={14} className="animate-spin" />
                  ) : (
                    <Send size={14} />
                  )}
                  {t('admin.settings.sendTest')}
                </button>
              </div>
            </div>
          </div>

          {/* 保存按钮 */}
          <div className="flex justify-end gap-3">
            <button
              onClick={loadConfig}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-ink-200 dark:border-ink-700 bg-white dark:bg-ink-900 text-ink-700 dark:text-ink-300 text-[13px] font-medium hover:bg-ink-50 dark:hover:bg-ink-800 transition-colors"
            >
              <RefreshCw size={14} />
              {t('common.refresh')}
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-2 px-6 py-2.5 rounded-lg bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white text-[13px] font-medium transition-all shadow-md hover:shadow-lg disabled:opacity-50"
            >
              {saving ? (
                <RefreshCw size={14} className="animate-spin" />
              ) : (
                <Save size={14} />
              )}
              {t('admin.settings.saveConfig')}
            </button>
          </div>

        </div>
      </div>
    </>
  );
}
