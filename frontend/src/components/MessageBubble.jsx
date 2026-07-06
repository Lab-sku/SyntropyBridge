import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
// 使用 PrismLight 按需注册语言，避免全量 Prism 拖累 markdown chunk
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import js from 'react-syntax-highlighter/dist/esm/languages/prism/javascript';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import markdownLang from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import jsxLang from 'react-syntax-highlighter/dist/esm/languages/prism/jsx';
import tsxLang from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import { Check, Copy, RefreshCw, User, Sparkles } from 'lucide-react';
import { useState, memo } from 'react';
import DOMPurify from 'dompurify';
import { copyToClipboard } from '@/lib/utils';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/stores/authStore';

// 按需注册常用语言；未注册的语言会以纯文本显示
SyntaxHighlighter.registerLanguage('javascript', js);
SyntaxHighlighter.registerLanguage('typescript', typescript);
SyntaxHighlighter.registerLanguage('python', python);
SyntaxHighlighter.registerLanguage('json', json);
SyntaxHighlighter.registerLanguage('bash', bash);
SyntaxHighlighter.registerLanguage('markdown', markdownLang);
SyntaxHighlighter.registerLanguage('sql', sql);
SyntaxHighlighter.registerLanguage('jsx', jsxLang);
SyntaxHighlighter.registerLanguage('tsx', tsxLang);

function CodeBlock({ language, value }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    await copyToClipboard(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="group/codeblock my-3 overflow-hidden rounded-lg border border-ink-200 bg-ink-50/40 dark:border-ink-700 dark:bg-ink-800/40">
      <div className="flex items-center justify-between border-b border-ink-200/70 bg-white/60 px-3 py-1.5 dark:border-ink-700/70 dark:bg-ink-800/60">
        <span className="font-mono text-[10.5px] font-medium text-ink-500">
          {language || 'text'}
        </span>
        <button
          onClick={onCopy}
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-700 dark:text-ink-400 dark:hover:bg-ink-700 dark:hover:text-ink-300"
        >
          {copied ? <Check size={11} /> : <Copy size={11} />}
          {copied ? t('common.copied') : t('common.copy')}
        </button>
      </div>
      <SyntaxHighlighter
        language={language || 'text'}
        style={oneLight}
        customStyle={{
          margin: 0,
          padding: '12px 14px',
          background: 'transparent',
          fontSize: '12.5px',
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
        }}
        wrapLongLines
      >
        {value}
      </SyntaxHighlighter>
    </div>
  );
}

export default memo(MessageBubble);

function MessageBubble({ role, content, model, streaming, onRegenerate, onCopy }) {
  const { t } = useTranslation();
  const authRole = useAuthStore((s) => s.role);
  const isUser = role === 'user';
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    if (onCopy) return onCopy();
    await copyToClipboard(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  // Resolve the speaker label for the user's own messages. When an
  // admin is using the chat (the page is reachable by both admin and
  // regular user sessions — see backend/routes/chat.py) we want the
  // bubble to read "管理员" / "Admin" instead of the generic
  // "你" / "You", so the role is unambiguous on screen.
  const userLabel =
    isUser && authRole === 'admin'
      ? t('chat.messageBubble.admin')
      : isUser
        ? t('chat.messageBubble.you')
        : t('chat.messageBubble.assistant');

  return (
    <div
      className={`group/message flex w-full gap-3 px-4 py-4 md:px-8 ${isUser ? '' : 'bg-ink-50/40 dark:bg-ink-900/40'}`}
    >
      <div
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md ${
          isUser ? 'bg-ink-900 text-white' : 'bg-white text-ink-900 ring-1 ring-ink-200 dark:bg-ink-800 dark:text-ink-100 dark:ring-ink-700'
        }`}
      >
        {isUser ? <User size={14} strokeWidth={2.2} /> : <Sparkles size={14} strokeWidth={2.2} />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2">
          <span className="text-[12px] font-semibold text-ink-900 dark:text-ink-100">
            {userLabel}
          </span>
          {!isUser && model && (
            <span className="font-mono text-[10.5px] text-ink-400">{model}</span>
          )}
        </div>
        <div className="prose-chat max-w-none text-[14px] leading-[1.7] text-ink-800 dark:text-ink-200">
          {isUser ? (
            <div className="whitespace-pre-wrap break-words">{content}</div>
          ) : content ? (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                code({ inline, className, children, ...props }) {
                  const match = /language-(\w+)/.exec(className || '');
                  const value = String(children).replace(/\n$/, '');
                  if (!inline && (match || value.includes('\n'))) {
                    return <CodeBlock language={match?.[1]} value={value} />;
                  }
                  return (
                    <code className={className} {...props}>
                      {children}
                    </code>
                  );
                },
                a({ href, children, ...props }) {
                  const isSafe = href && !/^(javascript|data|vbscript):/i.test(href);
                  if (!isSafe) return <span>{children}</span>;
                  return (
                    <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
                      {children}
                    </a>
                  );
                },
              }}
            >
              {DOMPurify.sanitize(content, { USE_PROFILES: { html: false } })}
            </ReactMarkdown>
          ) : (
            <div className="flex items-center gap-1.5 py-1">
              <span
                className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ink-400"
                style={{ animationDelay: '0ms' }}
              />
              <span
                className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ink-400"
                style={{ animationDelay: '150ms' }}
              />
              <span
                className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ink-400"
                style={{ animationDelay: '300ms' }}
              />
            </div>
          )}
        </div>
        {!isUser && content && !streaming && (
          <div className="mt-2 flex items-center gap-1 opacity-0 transition-opacity group-hover/message:opacity-100">
            <button
              onClick={handleCopy}
              className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-ink-500 transition-all duration-150 hover:bg-ink-100 hover:text-ink-800 active:scale-95 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-ink-200"
            >
              {copied ? <Check size={11} className="text-emerald-500" /> : <Copy size={11} />}
              {copied ? t('common.copied') : t('common.copy')}
            </button>
            {onRegenerate && (
              <button
                onClick={onRegenerate}
                className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-ink-500 transition-all duration-150 hover:bg-ink-100 hover:text-ink-800 active:scale-95 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-ink-200"
              >
                <RefreshCw size={11} />
                {t('chat.messageBubble.regenerate')}
              </button>
            )}
          </div>
        )}
        {!isUser && streaming && !content && (
          <div className="mt-2">
            <div className="flex items-center gap-1.5">
              <div className="h-1.5 w-1.5 rounded-full bg-brand-400 animate-pulse-ring" />
              <span className="text-[11px] text-ink-400">{t('chat.messageBubble.thinking')}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
