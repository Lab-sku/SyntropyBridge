import { useRef, useCallback, useEffect } from 'react';
import { Send, Square, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export default function ChatInputBox({
  input,
  onInputChange,
  onSend,
  onStop,
  isLoading,
  selectedModel,
  currentModel,
  children,
}) {
  const { t } = useTranslation();
  const taRef = useRef(null);

  // Auto-resize textarea
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 220) + 'px';
  }, [input]);

  const onKeyDown = useCallback(
    (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        onSend();
      }
    },
    [onSend],
  );

  const handleTextareaChange = useCallback(
    (e) => {
      onInputChange(e.target.value);
    },
    [onInputChange],
  );

  return (
    <div className="border-t border-ink-100 bg-gradient-to-t from-white via-white/95 to-white/0 px-4 pb-4 pt-3 md:px-6 dark:border-ink-800 dark:from-ink-950 dark:via-ink-950/95 dark:to-transparent">
      <div className="mx-auto max-w-3xl">
        {children}
        <div
          className={`group/box relative flex items-end gap-2 rounded-2xl border bg-white p-2 shadow-soft transition-all duration-200 dark:bg-ink-900 ${
            isLoading
              ? 'border-brand-200 shadow-glow dark:border-brand-700'
              : 'border-ink-200 focus-within:border-brand-400 focus-within:shadow-glow dark:border-ink-700 dark:focus-within:border-brand-500'
          }`}
        >
          <textarea
            ref={taRef}
            value={input}
            onChange={handleTextareaChange}
            onKeyDown={onKeyDown}
            placeholder={
              currentModel
                ? t('chat.input.placeholderWithModel', {
                    model: currentModel.display_name || currentModel.name,
                  })
                : t('chat.input.selectModelPlaceholder')
            }
            rows={1}
            disabled={isLoading}
            className="min-h-[40px] max-h-[220px] flex-1 resize-none bg-transparent px-2 py-1.5 text-[15px] text-ink-900 placeholder-ink-400 outline-none disabled:opacity-50 dark:text-ink-100 dark:placeholder-ink-500"
            style={{ height: 40 }}
          />
          {isLoading ? (
            <button
              onClick={onStop}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-rose-500 to-rose-600 text-white shadow-lg shadow-rose-500/20 transition-all duration-200 hover:from-rose-600 hover:to-rose-700 active:scale-95"
              title={t('chat.input.stopGenerating')}
              aria-label={t('chat.input.stopGenerating')}
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              onClick={() => onSend()}
              disabled={!input.trim() || !selectedModel}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-brand-600 to-brand-700 text-white shadow-brand transition-all duration-200 hover:from-brand-700 hover:to-brand-800 active:scale-95 disabled:cursor-not-allowed disabled:from-ink-200 disabled:to-ink-200 disabled:text-ink-400 disabled:shadow-none"
              title={t('chat.input.send')}
            >
              <Send size={14} />
            </button>
          )}
        </div>
        <div className="mt-2 flex items-center justify-center gap-1.5 text-[11.5px] text-ink-400">
          <Sparkles size={10} />
          <span>{t('chat.input.disclaimer')}</span>
        </div>
      </div>
    </div>
  );
}
