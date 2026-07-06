/**
 * UserIntegrationGuide — a user-facing integration guide that shows
 * the API base URL, masked API key, and ready-to-use code examples
 * (cURL, Python, Node.js) with the user's key interpolated.
 *
 * Registered as a standalone page at /integration.
 */
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';
import api from '@/lib/api';
import { copyToClipboard } from '@/lib/utils';
import {
  Copy,
  Check,
  ExternalLink,
  ArrowLeft,
  Terminal,
  Code2,
  Globe,
  Key,
  Lightbulb,
} from 'lucide-react';

/** Base URL derived from current origin (works for both dev and prod). */
function getApiBaseUrl() {
  return `${window.location.origin}/v1`;
}

function SnippetBlock({ title, icon: Icon, code, language, onCopy, copied, t }) {
  return (
    <div className="rounded-lg border border-ink-200 dark:border-ink-700">
      <div className="flex items-center justify-between border-b border-ink-200 px-4 py-2.5 dark:border-ink-700">
        <div className="flex items-center gap-2 text-sm font-medium text-ink-700 dark:text-ink-300">
          {Icon && <Icon size={14} />}
          {title}
        </div>
        <button
          onClick={() => onCopy(code)}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800 dark:hover:text-ink-300"
        >
          {copied ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
          {copied ? t('common.copied') : t('integration.copyCode')}
        </button>
      </div>
      <pre className="overflow-x-auto p-4 text-xs leading-relaxed text-ink-800 dark:text-ink-200">
        <code className={`language-${language}`}>{code}</code>
      </pre>
    </div>
  );
}

export default function UserIntegrationGuide() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.role);
  const [apiKeys, setApiKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [copiedIdx, setCopiedIdx] = useState(-1);

  useEffect(() => {
    let mounted = true;
    api
      .listMyApiKeys()
      .then((data) => {
        if (mounted) setApiKeys(Array.isArray(data) ? data : []);
      })
      .catch(() => {
        // If the user has no keys or the call fails, show the "no key" state.
        if (mounted) setApiKeys([]);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const baseUrl = getApiBaseUrl();
  const firstKey = apiKeys.length > 0 ? apiKeys[0] : null;
  // Show the key prefix + masked suffix, or a placeholder if no key.
  const displayKey = firstKey ? `${firstKey.key_prefix || 'sk-'}••••••••••••` : 'YOUR_API_KEY';
  const exampleKey = firstKey ? `${firstKey.key_prefix || 'sk-'}YOUR_SECRET` : 'YOUR_API_KEY';

  const curlCode = `curl -X POST ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer ${exampleKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`;

  const pythonCode = `from openai import OpenAI

client = OpenAI(
    base_url="${baseUrl}",
    api_key="${exampleKey}",
)

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)`;

  const nodeCode = `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${baseUrl}",
  apiKey: "${exampleKey}",
});

const response = await client.chat.completions.create({
  model: "gpt-4o-mini",
  messages: [{ role: "user", content: "Hello!" }],
});
console.log(response.choices[0].message.content);`;

  const snippets = [
    { title: t('integration.curlExample'), icon: Terminal, code: curlCode, lang: 'bash' },
    { title: t('integration.pythonExample'), icon: Code2, code: pythonCode, lang: 'python' },
    { title: t('integration.nodeExample'), icon: Code2, code: nodeCode, lang: 'javascript' },
  ];

  const handleCopy = useCallback(async (text) => {
    await copyToClipboard(text);
    setCopiedIdx(snippets.findIndex((s) => s.code === text));
    setTimeout(() => setCopiedIdx(-1), 2000);
  }, []);

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      {/* Header */}
      <div className="border-b border-ink-200 px-6 py-4 dark:border-ink-700">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(-1)}
            className="rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
            aria-label={t('common.back')}
          >
            <ArrowLeft size={18} />
          </button>
          <div>
            <h1 className="text-lg font-semibold text-ink-900 dark:text-ink-100">
              {t('integration.title')}
            </h1>
            <p className="text-sm text-ink-500 dark:text-ink-400">{t('integration.subtitle')}</p>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="mx-auto w-full max-w-3xl space-y-6 px-6 py-6">
        {/* API Base URL */}
        <div className="rounded-lg border border-ink-200 p-4 dark:border-ink-700">
          <div className="mb-1 flex items-center gap-2 text-sm font-medium text-ink-700 dark:text-ink-300">
            <Globe size={14} />
            {t('integration.baseUrl')}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 rounded-md bg-ink-50 px-3 py-2 text-sm text-ink-800 dark:bg-ink-800 dark:text-ink-200">
              {baseUrl}
            </code>
            <button
              onClick={() => handleCopy(baseUrl)}
              className="rounded-md p-2 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:hover:bg-ink-800"
              aria-label={t('common.copy')}
            >
              <Copy size={14} />
            </button>
          </div>
        </div>

        {/* API Key */}
        <div className="rounded-lg border border-ink-200 p-4 dark:border-ink-700">
          <div className="mb-1 flex items-center gap-2 text-sm font-medium text-ink-700 dark:text-ink-300">
            <Key size={14} />
            {t('integration.yourApiKey')}
          </div>
          <code className="block rounded-md bg-ink-50 px-3 py-2 text-sm text-ink-800 dark:bg-ink-800 dark:text-ink-200">
            {displayKey}
          </code>
          <p className="mt-2 text-xs text-ink-500 dark:text-ink-400">
            {!firstKey && !loading ? t('integration.noKey') : t('integration.keyMasked')}
          </p>
        </div>

        {/* Tip */}
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-800/40 dark:bg-amber-950/20">
          <Lightbulb size={16} className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
          <p className="text-sm text-amber-800 dark:text-amber-300">{t('integration.tip')}</p>
        </div>

        {/* Code snippets */}
        <div className="space-y-4">
          {snippets.map((snippet, i) => (
            <SnippetBlock
              key={i}
              title={snippet.title}
              icon={snippet.icon}
              code={snippet.code}
              language={snippet.lang}
              onCopy={handleCopy}
              copied={copiedIdx === i}
              t={t}
            />
          ))}
        </div>

        {/* External link */}
        <div className="pb-8">
          <a
            href="https://platform.openai.com/docs/libraries"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-700 transition-colors hover:text-ink-900 dark:text-ink-300 dark:hover:text-ink-100"
          >
            <ExternalLink size={14} />
            {t('integration.viewDocs')}
          </a>
        </div>
      </div>
    </div>
  );
}
