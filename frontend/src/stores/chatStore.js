import { create } from 'zustand';
import api from '@/lib/api';
import i18n from '@/i18n';

export const useChatStore = create((set, get) => ({
  conversations: [],
  currentSessionId: null,
  messages: [],
  models: [],
  selectedModel: '',
  modelSearch: '',
  showModelPicker: false,
  sidebarOpen: true,
  isLoading: false,
  streaming: false,
  _userAborted: false,
  abortController: null,
  error: null,
  // 流式 delta 累积器：用 rAF 防抖提交，避免每个 delta 都 slice 整个 messages 数组（O(n²)）
  _streamAccumulator: { text: '', model: '', rafId: null },

  setSidebarOpen: (v) => set({ sidebarOpen: v }),
  setShowModelPicker: (v) => set({ showModelPicker: v }),
  setModelSearch: (q) => set({ modelSearch: q }),
  setSelectedModel: (m) => set({ selectedModel: m }),

  loadModels: async () => {
    try {
      const data = await api.getModels();
      const list = Array.isArray(data) ? data : data.models || [];
      set({ models: list });
      if (!get().selectedModel && list.length > 0) {
        // Prefer a configured provider over the built-in minimax slot
        // so users see a real upstream model first when one exists.
        // Restrict the candidates to chat-capable models so the
        // default selection never lands on an embedding / image /
        // audio id (those would 405 against /chat/send/stream).
        // L14: 移除硬编码 'MiniMax-M1' 偏好（后端重命名/下线会失效），
        // 改为 minimax 的第一个 chat 模型（按 name 排序保证稳定）。
        const isChat = (m) => (m.type || 'chat') === 'chat';
        const configured = list.find(
          (m) => m.provider && m.provider !== 'minimax' && m.enabled !== false && isChat(m),
        );
        const minimaxChat = list
          .filter((m) => m.provider === 'minimax' && isChat(m))
          .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        const preferred =
          configured ||
          minimaxChat[0] ||
          list.find(isChat) ||
          list[0];
        set({ selectedModel: preferred.name });
      }
      return list;
    } catch (e) {
      set({ error: e.message });
      return [];
    }
  },

  loadConversations: async () => {
    try {
      const list = await api.getConversations();
      set({ conversations: Array.isArray(list) ? list : [] });
    } catch (e) {
      set({ error: e.message || i18n.t('chat.loadConversationsFailed') });
    }
  },

  loadConversation: async (sid) => {
    set({ currentSessionId: sid });
    try {
      const data = await api.getConversation(sid);
      const msgs = (data || []).map((m) => ({
        role: m.role,
        content: m.content,
        model: m.model || '',
      }));
      set({ messages: msgs, error: null });

      // Restore the model that was used for the last user turn so
      // the picker shows the correct model when re-opening a conversation.
      const lastUserMsg = [...(data || [])].reverse().find((m) => m.role === 'user');
      if (lastUserMsg && lastUserMsg.model) {
        const models = get().models;
        if (models.some((m) => m.name === lastUserMsg.model)) {
          set({ selectedModel: lastUserMsg.model });
        }
      }
    } catch (e) {
      set({ messages: [], error: e.message || i18n.t('chat.loadConversationFailed') });
    }
  },

  newChat: () => set({ currentSessionId: null, messages: [], error: null }),

  /**
   * Hard-reset the entire chat store. Called on logout / login so that
   * one user's conversations, messages, and model state never leak
   * into another user's session.
   */
  resetStore: () =>
    set({
      conversations: [],
      currentSessionId: null,
      messages: [],
      models: [],
      selectedModel: '',
      modelSearch: '',
      showModelPicker: false,
      sidebarOpen: true,
      isLoading: false,
      streaming: false,
      _userAborted: false,
      abortController: null,
      error: null,
      _streamAccumulator: { text: '', model: '', rafId: null },
    }),

  deleteConversation: async (sid) => {
    try {
      await api.deleteConversation(sid);
    } catch (e) {
      set({ error: e.message || i18n.t('chat.deleteConversationFailed') });
      return; // Don't clear UI state if the server delete failed
    }
    const { currentSessionId } = get();
    if (currentSessionId === sid) set({ currentSessionId: null, messages: [] });
    await get().loadConversations();
  },

  appendUserMessage: (content) => {
    set((s) => ({ messages: [...s.messages, { role: 'user', content }] }));
  },

  appendAssistantPlaceholder: () => {
    set((s) => ({ messages: [...s.messages, { role: 'assistant', content: '' }] }));
  },

  appendDelta: (delta) => {
    // 累积 delta 到 ref，用 rAF 防抖，每帧最多提交一次，避免 O(n²) 性能问题
    const acc = get()._streamAccumulator;
    if (delta.content) acc.text += delta.content;
    if (delta.model) acc.model = delta.model;

    // 已有待执行的 rAF，等待合并
    if (acc.rafId != null) return;

    // 兼容 SSR/测试环境（无 requestAnimationFrame 时退化为 setTimeout 16ms）
    const schedule = typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame
      : (fn) => setTimeout(fn, 16);

    acc.rafId = schedule(() => {
      acc.rafId = null;
      const text = acc.text;
      const model = acc.model;
      acc.text = '';
      acc.model = '';
      if (!text && !model) return;
      set((s) => {
        const msgs = s.messages.slice();
        const last = msgs[msgs.length - 1];
        if (last && last.role === 'assistant') {
          const update = { ...last, content: (last.content || '') + text };
          if (model) update.model = model;
          msgs[msgs.length - 1] = update;
        } else if (text) {
          // 异常情况：没有 placeholder，直接 push 一条新 assistant 消息
          msgs.push({ role: 'assistant', content: text, model: model || '' });
        }
        return { messages: msgs };
      });
    });
  },

  /**
   * 立即提交累积的 delta 并取消挂起的 rAF。
   * 在 endStream / abort 前调用，确保最后的 delta 不丢失。
   */
  flushStream: () => {
    const acc = get()._streamAccumulator;
    if (acc.rafId == null) return;

    const cancel = typeof cancelAnimationFrame === 'function'
      ? cancelAnimationFrame
      : clearTimeout;
    cancel(acc.rafId);
    acc.rafId = null;

    const text = acc.text;
    const model = acc.model;
    acc.text = '';
    acc.model = '';
    if (!text && !model) return;

    set((s) => {
      const msgs = s.messages.slice();
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant') {
        const update = { ...last, content: (last.content || '') + text };
        if (model) update.model = model;
        msgs[msgs.length - 1] = update;
      } else if (text) {
        msgs.push({ role: 'assistant', content: text, model: model || '' });
      }
      return { messages: msgs };
    });
  },

  setError: (errMsg) => {
    const prefix = i18n.t('chat.errorPrefix');
    set((s) => {
      const msgs = s.messages.slice();
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant' && !last.content) {
        msgs[msgs.length - 1] = { ...last, content: `${prefix}: ${errMsg}` };
      } else if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = { ...last, content: `${last.content}\n[${prefix}: ${errMsg}]` };
      } else {
        msgs.push({ role: 'assistant', content: `${prefix}: ${errMsg}` });
      }
      return { messages: msgs, error: errMsg };
    });
  },

  beginStream: () => {
    const ctrl = new AbortController();
    set({ isLoading: true, streaming: true, abortController: ctrl, error: null, _userAborted: false });
    return ctrl;
  },

  endStream: async () => {
    // 先同步提交累积的 delta，避免最后的内容丢失
    get().flushStream();
    const userAborted = get()._userAborted;
    set({ isLoading: false, streaming: false, abortController: null, _userAborted: false });

    // Guard: if the store was reset (e.g. user logged out mid-stream)
    // or the user manually aborted the stream, skip title generation
    // and conversation reload to avoid spurious 401 errors,
    // redundant state mutations, and unnecessary API calls.
    const { currentSessionId, selectedModel, conversations, messages } = get();
    if (!currentSessionId || userAborted) return;

    // 捕获 sessionId 局部变量，防止 await 期间用户切换会话导致状态错位
    const sessionId = currentSessionId;

    // Generate title for new conversations (first assistant reply)
    if (sessionId && selectedModel) {
      const conv = conversations.find(c => c.session_id === sessionId);
      const needsTitle = !conv?.title && messages.filter(m => m.role === 'assistant').length <= 1;

      if (needsTitle) {
        try {
          const result = await api.generateTitle(sessionId, selectedModel);
          // 验证 await 期间用户未切换会话；若已切换则放弃此次标题更新
          if (get().currentSessionId !== sessionId) {
            console.warn('Session changed during title generation, skipping update');
            return;
          }
          if (result?.title) {
            // 使用最新的 conversations 而非闭包中的旧引用
            set({
              conversations: get().conversations.map(c =>
                c.session_id === sessionId ? { ...c, title: result.title } : c
              ),
            });
          }
        } catch (e) {
          // Title generation is best-effort, don't break the chat
          console.warn('Title generation failed:', e);
        }
      }
    }

    get().loadConversations();
  },

  abort: () => {
    // 先同步提交累积的 delta，再追加"已停止"标记
    get().flushStream();
    const { abortController } = get();
    if (abortController) abortController.abort();
    set((s) => {
      const msgs = s.messages.slice();
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = {
          ...last,
          content: (last.content || '') + `\n${i18n.t('chat.stoppedLabel')}`,
        };
      }
      return { messages: msgs, isLoading: false, streaming: false, abortController: null, _userAborted: true };
    });
  },
}));
