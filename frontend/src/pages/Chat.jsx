import { useEffect, useRef, useState, useCallback, useMemo, memo } from 'react';
import { useChatStore } from '@/stores/chatStore';
import { streamChat } from '@/lib/api';
import { timeAgo, titleFromMessage } from '@/lib/utils';
import {
  Plus,
  Search,
  MessageSquare,
  Trash2,
  PanelLeftClose,
  PanelLeftOpen,
  Sparkles,
  ArrowUp,
  Settings2,
  LogOut,
  Wallet as WalletIcon,
  BarChart3,
  User as UserIcon,
  BookOpen,
  Shield,
} from 'lucide-react';
import MessageBubble from '@/components/MessageBubble';
import ModelPicker from '@/components/ModelPicker';
import ChatInputBox from '@/components/chat/ChatInputBox';
import ThemeToggle from '@/components/ThemeToggle';
import LanguageToggle from '@/components/LanguageToggle';
import Logo from '@/components/Logo';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/stores/authStore';
// zustand/shallow no longer used: action selectors are split into
// individual (s) => s.action calls, each returning a stable reference.

// Extract a human-readable error message from a possible JSON error string.
function cleanErrorMsg(msg) {
  try {
    const j = JSON.parse(msg);
    if (j.detail) return j.detail;
  } catch (_) { /* not JSON */ }
  return msg;
}

// Memoised message list so only this subtree re-renders when
// ``messages`` changes (e.g. during SSE streaming deltas).
const MessageList = memo(function MessageList({ messages, models, isLoading, regenerate }) {
  return (
    <div className="mx-auto max-w-3xl pb-32 pt-4">
      {messages.map((m, i) => {
        const isLast = i === messages.length - 1;
        // Resolve display name from the per-message model id.
        const modelId = m.model || '';
        const modelInfo = modelId && models ? models.find((x) => x.name === modelId) : null;
        const displayModel = modelInfo?.display_name || (modelId ? modelId.split('/').pop() : '');
        return (
          <MessageBubble
            key={i}
            role={m.role}
            content={m.content}
            model={m.role === 'assistant' ? displayModel : ''}
            streaming={isLast && isLoading && m.role === 'assistant'}
            onRegenerate={isLast && m.role === 'assistant' && !isLoading ? regenerate : undefined}
          />
        );
      })}
    </div>
  );
});

export default function Chat() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const logout = useAuthStore((s) => s.logout);
  const role = useAuthStore((s) => s.role);

  // --- State selectors (each re-renders only when its value changes) ---
  const conversations = useChatStore((s) => s.conversations);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const messages = useChatStore((s) => s.messages);
  const models = useChatStore((s) => s.models);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const sidebarOpen = useChatStore((s) => s.sidebarOpen);
  const isLoading = useChatStore((s) => s.isLoading);
  const streaming = useChatStore((s) => s.streaming);

  // --- Action selectors (stable refs; one selector each so no bulk
  // object-literal is returned, which avoids the React 18 useSyncExternalStore
  // "result of getSnapshot should be cached" infinite-loop invariant). ---
  const loadModels = useChatStore((s) => s.loadModels);
  const loadConversations = useChatStore((s) => s.loadConversations);
  const loadConversation = useChatStore((s) => s.loadConversation);
  const newChat = useChatStore((s) => s.newChat);
  const deleteConversation = useChatStore((s) => s.deleteConversation);
  const appendUserMessage = useChatStore((s) => s.appendUserMessage);
  const appendAssistantPlaceholder = useChatStore((s) => s.appendAssistantPlaceholder);
  const appendDelta = useChatStore((s) => s.appendDelta);
  const setError = useChatStore((s) => s.setError);
  const beginStream = useChatStore((s) => s.beginStream);
  const endStream = useChatStore((s) => s.endStream);
  const abort = useChatStore((s) => s.abort);
  const setSidebarOpen = useChatStore((s) => s.setSidebarOpen);
  const setSelectedModel = useChatStore((s) => s.setSelectedModel);

  const SUGGESTIONS = useMemo(
    () => [
      {
        title: t('chat.suggestions.writeCode.title'),
        desc: t('chat.suggestions.writeCode.desc'),
        icon: '⌘',
      },
      {
        title: t('chat.suggestions.explain.title'),
        desc: t('chat.suggestions.explain.desc'),
        icon: '?',
      },
      {
        title: t('chat.suggestions.translate.title'),
        desc: t('chat.suggestions.translate.desc'),
        icon: '✎',
      },
      {
        title: t('chat.suggestions.analyze.title'),
        desc: t('chat.suggestions.analyze.desc'),
        icon: '◇',
      },
    ],
    [t],
  );

  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [autoScroll, setAutoScroll] = useState(true);
  const messagesEndRef = useRef(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    loadModels();
    loadConversations();
  }, [loadModels, loadConversations]);

  useEffect(() => {
    if (autoScroll) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages, autoScroll]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setAutoScroll(distFromBottom < 80);
  }, []);

  const filteredConvs = useMemo(() => {
    if (!search) return conversations;
    const q = search.toLowerCase();
    return conversations.filter((c) => (c.last_message || '').toLowerCase().includes(q));
  }, [conversations, search]);

  const send = useCallback(
    async (text) => {
      const content = (text ?? input).trim();
      if (!content || isLoading) return;

      const selectedModelInfo = models.find((m) => m.name === selectedModel);
      if (!selectedModelInfo) {
        toast.error(t('chat.toast.selectModelFirst'));
        return;
      }
      if ((selectedModelInfo.type || 'chat') !== 'chat') {
        toast.error(
          `${selectedModelInfo.display_name || selectedModel} ${t('chat.toast.notSupported')}`,
        );
        return;
      }

      const sessionId = currentSessionId || 'sess_' + Date.now();
      if (!currentSessionId) useChatStore.setState({ currentSessionId: sessionId });

      appendUserMessage(content);
      appendAssistantPlaceholder();
      setInput('');

      const ctrl = beginStream();
      try {
        await streamChat(
          { message: content, model: selectedModel, session_id: sessionId },
          {
            signal: ctrl.signal,
            onDelta: (d) => {
              appendDelta(d);
            },
            onDone: () => endStream(),
            onError: (err) => {
              setError(cleanErrorMsg(err.message));
              endStream();
            },
          },
        );
      } catch (e) {
        if (e.name !== 'AbortError') {
          setError(cleanErrorMsg(e.message));
        }
        endStream();
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [
      input,
      isLoading,
      models,
      selectedModel,
      currentSessionId,
      appendUserMessage,
      appendAssistantPlaceholder,
      beginStream,
      endStream,
      setError,
      appendDelta,
      t,
    ],
  );

  const stop = useCallback(() => abort(), [abort]);

  const regenerate = useCallback(async () => {
    if (messages.length < 2 || isLoading) return;
    const lastUserIdx = [...messages].reverse().findIndex((m) => m.role === 'user');
    if (lastUserIdx === -1) return;
    const realIdx = messages.length - 1 - lastUserIdx;
    const userMsg = messages[realIdx];
    // Trim to include the user message, drop the old assistant reply.
    const trimmed = messages.slice(0, realIdx + 1);
    useChatStore.setState({ messages: trimmed });

    // Re-request without calling appendUserMessage (user msg already in state).
    const selectedModelInfo = models.find((m) => m.name === selectedModel);
    if (!selectedModelInfo) return;
    const sessionId = currentSessionId || 'sess_' + Date.now();
    if (!currentSessionId) useChatStore.setState({ currentSessionId: sessionId });

    appendAssistantPlaceholder();
    const ctrl = beginStream();
    try {
      await streamChat(
        { message: userMsg.content, model: selectedModel, session_id: sessionId },
        {
          signal: ctrl.signal,
          onDelta: (d) => appendDelta(d),
          onDone: () => endStream(),
          onError: (err) => {
            setError(cleanErrorMsg(err.message));
            endStream();
          },
        },
      );
    } catch (e) {
      if (e.name !== 'AbortError') setError(cleanErrorMsg(e.message));
      endStream();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, isLoading, models, selectedModel, currentSessionId]);

  const handleLogout = async () => {
    // authStore.logout() already calls api.userLogout() internally,
    // so we must NOT call api.userLogout() here first — that would
    // send a duplicate request and risk a 401 race condition.
    await logout();
    navigate('/login', { replace: true });
  };

  const currentModel = useMemo(
    () => models.find((m) => m.name === selectedModel),
    [models, selectedModel],
  );
  const currentTitle = useMemo(
    () => {
      if (!currentSessionId) return null;
      const conv = conversations.find((c) => c.session_id === currentSessionId);
      return conv?.title || conv?.last_message || null;
    },
    [currentSessionId, conversations],
  );

  const isEmpty = messages.length === 0;

  return (
    <div className="flex h-screen w-full bg-gradient-to-br from-ink-25 via-white to-ink-25 text-ink-900 dark:from-ink-950 dark:via-ink-950 dark:to-ink-950 dark:text-ink-100">
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-ink-950/20 backdrop-blur-sm md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[280px] flex-col border-r border-ink-200/60 bg-ink-50/70 backdrop-blur-xl transition-transform duration-250 ease-spring md:static md:translate-x-0 dark:border-ink-700/60 dark:bg-ink-900/70 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full md:hidden'
        }`}
      >
        <div className="flex h-14 items-center justify-between px-3">
          <Link to="/chat" className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-600 to-brand-800 text-white shadow-brand/50">
              <Sparkles size={14} strokeWidth={2} />
            </div>
            <span className="text-sm font-semibold text-ink-900 dark:text-ink-100">{t('app.name')}</span>
          </Link>
          <button
            onClick={() => setSidebarOpen(false)}
            className="rounded-md p-1.5 text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-700 md:hidden dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
          >
            <PanelLeftClose size={16} />
          </button>
        </div>

        <div className="px-3">
          <button
            onClick={() => {
              newChat();
              setTimeout(() => {}, 0);
            }}
            className="flex h-9 w-full items-center justify-between rounded-xl bg-gradient-to-r from-brand-600 to-brand-700 px-3 text-[13px] font-medium text-white shadow-brand/30 transition-all duration-200 hover:from-brand-700 hover:to-brand-800 hover:shadow-brand/40 active:scale-[0.98]"
          >
            <span className="flex items-center gap-2">
              <Plus size={14} />
              {t('chat.sidebar.newChat')}
            </span>
            <kbd className="rounded bg-white/20 px-1.5 py-0.5 font-mono text-[10px] text-white/80">
              ⌘N
            </kbd>
          </button>
        </div>

        <div className="px-3 pt-3">
          <div className="flex items-center gap-1.5 rounded-xl border border-ink-200/60 bg-white/60 px-3 transition-colors focus-within:border-brand-300 focus-within:shadow-glow/30 dark:border-ink-700/60 dark:bg-ink-800/60 dark:focus-within:border-brand-500">
            <Search size={12} className="text-ink-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('chat.sidebar.searchPlaceholder')}
              className="h-8 w-full bg-transparent text-[12px] text-ink-800 placeholder-ink-400 outline-none dark:text-ink-200 dark:placeholder-ink-500"
            />
          </div>
        </div>

        <div className="mt-3 flex-1 overflow-y-auto px-2 scrollbar-thin">
          <div className="px-2 pb-1 pt-0.5 text-[10.5px] font-semibold uppercase tracking-wider text-ink-400 dark:text-ink-500">
            {t('chat.sidebar.history')}
          </div>
          {filteredConvs.length === 0 ? (
            <div className="px-3 py-8 text-center">
              <div className="mx-auto mb-2 flex h-10 w-10 items-center justify-center rounded-full bg-ink-100 text-ink-300 dark:bg-ink-800 dark:text-ink-500">
                <MessageSquare size={16} />
              </div>
              <div className="text-[12px] text-ink-400">
                {conversations.length === 0
                  ? t('chat.sidebar.noConversations')
                  : t('chat.sidebar.noMatching')}
              </div>
            </div>
          ) : (
            <div className="space-y-0.5">
              {filteredConvs.map((c) => {
                const active = c.session_id === currentSessionId;
                return (
                  <div
                    key={c.session_id}
                    onClick={() => loadConversation(c.session_id)}
                    className={`group/conv flex cursor-pointer items-center gap-2 rounded-xl px-2.5 py-2 transition-all duration-150 ${
                      active
                        ? 'bg-white shadow-soft ring-1 ring-ink-200/50 dark:bg-ink-800 dark:ring-ink-700/50'
                        : 'hover:bg-white/70 hover:shadow-soft/50 dark:hover:bg-ink-800/70'
                    }`}
                  >
                    <div
                      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg transition-colors ${
                        active
                          ? 'bg-brand-100 text-brand-600 dark:bg-brand-900/40 dark:text-brand-400'
                          : 'bg-ink-100 text-ink-400 group-hover/conv:bg-ink-200/70 dark:bg-ink-800 dark:text-ink-500 dark:group-hover/conv:bg-ink-700/70'
                      }`}
                    >
                      <MessageSquare size={12} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[12.5px] font-medium text-ink-800 dark:text-ink-200">
                        {c.title || titleFromMessage(c.last_message)}
                      </div>
                      <div className="flex items-center gap-1.5">
                        <span className="text-[10.5px] text-ink-400">{timeAgo(c.last_time)}</span>
                        {c.model && (
                          <span className="truncate text-[10px] text-ink-300 dark:text-ink-500" title={c.model}>
                            {c.model.split('/').pop()}
                          </span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={async (e) => {
                        e.stopPropagation();
                        if (!confirm(t('chat.sidebar.deleteConfirm'))) return;
                        await deleteConversation(c.session_id);
                        toast.success(t('chat.toast.deleted'));
                      }}
                      className="rounded-lg p-1 text-ink-300 opacity-0 transition-all hover:bg-rose-50 hover:text-rose-500 group-hover/conv:opacity-100 active:scale-90 dark:text-ink-600 dark:hover:bg-rose-900/30 dark:hover:text-rose-400"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Sidebar Footer */}
        <div className="border-t border-ink-200/60 p-2.5 dark:border-ink-700/60">
          <div className="mb-2 flex items-center gap-1.5 rounded-xl bg-white/60 p-1 dark:bg-ink-800/60">
            <LanguageToggle size="sm" />
            <ThemeToggle size="sm" mode="dropdown" />
          </div>
          {role !== 'admin' && (
            <div className="mb-1 grid grid-cols-2 gap-1">
              <Link
                to="/usage"
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11.5px] font-medium text-ink-700 transition-colors hover:bg-white/80 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800/80 dark:hover:text-ink-100"
              >
                <BarChart3 size={12} />
                <span className="truncate">{t('nav.usage')}</span>
              </Link>
              <Link
                to="/wallet"
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11.5px] font-medium text-ink-700 transition-colors hover:bg-white/80 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800/80 dark:hover:text-ink-100"
              >
                <WalletIcon size={12} />
                <span className="truncate">{t('nav.wallet')}</span>
              </Link>
              <Link
                to="/account"
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11.5px] font-medium text-ink-700 transition-colors hover:bg-white/80 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800/80 dark:hover:text-ink-100"
              >
                <UserIcon size={12} />
                <span className="truncate">{t('nav.account')}</span>
              </Link>
              <Link
                to="/integration"
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11.5px] font-medium text-ink-700 transition-colors hover:bg-white/80 hover:text-ink-900 dark:text-ink-300 dark:hover:bg-ink-800/80 dark:hover:text-ink-100"
              >
                <BookOpen size={12} />
                <span className="truncate">{t('nav.integration')}</span>
              </Link>
            </div>
          )}
          {role === 'admin' && (
            <Link
              to="/admin"
              className="group/admin flex items-center gap-2.5 rounded-xl px-2.5 py-2 transition-colors hover:bg-white/80 dark:hover:bg-ink-800/80"
            >
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-ink-700 to-ink-900 text-[11px] font-semibold text-white shadow-soft">
                <Shield size={12} />
              </div>
              <div className="min-w-0 flex-1 leading-tight">
                <div className="truncate text-[12px] font-medium text-ink-900 dark:text-ink-100">
                  {t('chat.sidebar.adminDashboard')}
                </div>
                <div className="truncate text-[10.5px] text-ink-400">
                  {t('chat.sidebar.adminDesc')}
                </div>
              </div>
              <Settings2
                size={13}
                className="text-ink-300 transition-colors group-hover/admin:text-ink-600 dark:text-ink-600 dark:group-hover/admin:text-ink-400"
              />
            </Link>
          )}
          <button
            onClick={handleLogout}
            className="group/logout mt-1 flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-ink-400 transition-colors hover:bg-white/80 hover:text-rose-500 dark:text-ink-500 dark:hover:bg-ink-800/80 dark:hover:text-rose-400"
          >
            <LogOut size={13} />
            <span className="text-[12px] font-medium">{t('chat.sidebar.logout')}</span>
          </button>
        </div>
      </aside>

      <main className="relative flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-3 border-b border-ink-100/80 bg-white/60 px-4 backdrop-blur-xl md:px-6 dark:border-ink-800/80 dark:bg-ink-950/60">
          <div className="flex min-w-0 items-center gap-2">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="rounded-lg p-1.5 text-ink-400 transition-all hover:bg-ink-100 hover:text-ink-700 active:scale-95 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-ink-300"
            >
              {sidebarOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
            </button>
            <div className="min-w-0">
              <div className="truncate text-[14.5px] font-semibold tracking-tight text-ink-900 dark:text-ink-100">
                {currentTitle || t('chat.header.newChat')}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <ModelPicker
              models={models}
              value={selectedModel}
              onChange={setSelectedModel}
              filterType="chat"
            />
          </div>
        </header>

        <div ref={scrollRef} onScroll={onScroll} className="relative flex-1 overflow-y-auto">
          {isEmpty ? (
            <div className="mx-auto flex h-full max-w-3xl flex-col items-center justify-center px-6 py-10">
              <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-brand animate-float">
                <Sparkles size={28} strokeWidth={1.5} />
              </div>
              <h2 className="text-[32px] font-semibold tracking-tight text-ink-900 dark:text-ink-100">
                {t('chat.empty.title')}
              </h2>
              <p className="mt-2 text-[14.5px] text-ink-500">{t('chat.empty.subtitle')}</p>

              <div className="mt-10 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                {SUGGESTIONS.map((s, i) => (
                  <button
                    key={s.title}
                    onClick={() => send(s.desc)}
                    className="group/sug animate-fade-in-up flex items-start gap-3 rounded-xl border border-ink-200/60 bg-white/60 p-4 text-left backdrop-blur-sm transition-all duration-200 hover:border-brand-200 hover:bg-white hover:shadow-soft-lg active:scale-[0.98] dark:border-ink-700/60 dark:bg-ink-900/60 dark:hover:border-brand-700 dark:hover:bg-ink-800"
                    style={{ animationDelay: `${i * 60}ms`, animationFillMode: 'backwards' }}
                  >
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-ink-100 font-mono text-[14px] font-medium text-ink-600 transition-all duration-200 group-hover/sug:bg-gradient-to-br group-hover/sug:from-brand-600 group-hover/sug:to-brand-700 group-hover/sug:text-white group-hover/sug:shadow-brand/30 dark:bg-ink-800 dark:text-ink-400">
                      {s.icon}
                    </div>
                    <div>
                      <div className="text-[14px] font-semibold text-ink-900 dark:text-ink-100">{s.title}</div>
                      <div className="mt-0.5 text-[12.5px] text-ink-500 dark:text-ink-400">{s.desc}</div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              <MessageList
                messages={messages}
                models={models}
                isLoading={isLoading}
                regenerate={regenerate}
              />
              <div ref={messagesEndRef} />
            </>
          )}

          {!autoScroll && messages.length > 0 && (
            <button
              onClick={() => {
                setAutoScroll(true);
                messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
              }}
              className="fixed bottom-28 left-1/2 z-10 -translate-x-1/2 rounded-full border border-ink-200/60 bg-white/80 px-3 py-1.5 text-[12px] font-medium text-ink-600 shadow-pop backdrop-blur-sm transition-all duration-200 hover:bg-white hover:shadow-soft-lg active:scale-95 dark:border-ink-700/60 dark:bg-ink-900/80 dark:text-ink-400 dark:hover:bg-ink-800"
            >
              <ArrowUp size={12} className="mr-1 inline" />
              {t('chat.scrollToBottom')}
            </button>
          )}
        </div>

        <ChatInputBox
          input={input}
          onInputChange={setInput}
          onSend={() => send()}
          onStop={stop}
          isLoading={isLoading}
          selectedModel={selectedModel}
          currentModel={currentModel}
        />
      </main>
    </div>
  );
}
