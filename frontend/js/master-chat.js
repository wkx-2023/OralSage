(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

  const state = {
    messages: [],
    sessions: [],
    activeSessionId: createId(),
  };

  function createId() {
    return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function nowTime() {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  function formatMarkdown(text) {
    const escaped = escapeHtml(text || '');
    const lines = escaped.split(/\n/);
    const html = [];
    let inList = false;
    let inCode = false;
    let codeLines = [];

    function closeList() {
      if (inList) {
        html.push('</ul>');
        inList = false;
      }
    }

    function flushCode() {
      if (inCode) {
        html.push(`<pre><code>${codeLines.join('\n')}</code></pre>`);
        codeLines = [];
        inCode = false;
      }
    }

    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) {
          flushCode();
        } else {
          closeList();
          inCode = true;
          codeLines = [];
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      if (/^###\s+/.test(line)) {
        closeList();
        html.push(`<h3>${line.replace(/^###\s+/, '')}</h3>`);
        continue;
      }
      if (/^##\s+/.test(line)) {
        closeList();
        html.push(`<h2>${line.replace(/^##\s+/, '')}</h2>`);
        continue;
      }
      if (/^-\s+/.test(line)) {
        if (!inList) {
          html.push('<ul>');
          inList = true;
        }
        html.push(`<li>${line.replace(/^-\s+/, '')}</li>`);
        continue;
      }
      if (!line.trim()) {
        closeList();
        html.push('');
        continue;
      }
      closeList();
      html.push(`<p>${line}</p>`);
    }
    flushCode();
    closeList();
    return html.join('');
  }

  function resultAnswer(result) {
    if (!result) return '未返回内容。';
    if (typeof result.answer === 'string' && result.answer.trim()) return result.answer;
    const final = result.final_result || result.result || {};
    if (typeof final.answer === 'string') return final.answer;
    if (typeof final.summary === 'string') return final.summary;
    if (typeof final.markdown === 'string') return final.markdown;
    if (final.result?.report_text) return final.result.report_text;
    if (final.result?.markdown) return final.result.markdown;
    if (typeof final.message === 'string') return final.message;
    return JSON.stringify(final, null, 2);
  }

  function routeLabel(route) {
    if (!route) return '未生成路由';
    if (route.mode === 'clarify') return '需要补充信息';
    if (route.mode === 'multi_agent') {
      return `多 Agent 链路：${(route.steps || []).map((step) => step.agent).join(' → ')}`;
    }
    return route.target_agent || '未知 Agent';
  }

  function routeDetails(result) {
    const route = result?.route || {};
    const trace = result?.trace || {};
    const errors = trace.errors || [];
    const parts = [
      `<div class="route-pill">${escapeHtml(routeLabel(route))}</div>`,
      `<div class="route-meta">来源：${escapeHtml(trace.route_source || 'unknown')} · 置信度：${escapeHtml(String(trace.confidence ?? '-'))} · 耗时：${escapeHtml(String(trace.elapsed_ms ?? '-'))} ms</div>`,
    ];
    if (trace.reason) {
      parts.push(`<div class="route-reason">${escapeHtml(trace.reason)}</div>`);
    }
    if (errors.length) {
      parts.push(`<div class="route-errors">${escapeHtml(errors.join('；'))}</div>`);
    }
    return `<details class="route-card"><summary>总控路由</summary>${parts.join('')}</details>`;
  }

  function renderMessage(message) {
    const log = byId('agent-chat-log');
    if (!log) return;
    const row = document.createElement('article');
    row.className = `chat-message ${message.role}`;
    const roleText = message.role === 'user' ? '你' : 'Oral Master';
    const avatar = message.role === 'user' ? 'ME' : 'OM';
    const content = message.role === 'assistant' ? formatMarkdown(message.content) : escapeHtml(message.content);
    row.innerHTML = `
      <div class="message-avatar">${avatar}</div>
      <div class="message-stack">
        <div class="message-role">${roleText} · ${escapeHtml(message.time || nowTime())}</div>
        <div class="chat-bubble assistant-markdown">${content}</div>
        ${message.routeHtml || ''}
      </div>
    `;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  function setEmptyState() {
    const stage = document.querySelector('.chat-stage');
    if (!stage) return;
    stage.classList.toggle('is-empty', state.messages.length === 0);
  }

  function setBusy(isBusy) {
    const send = byId('agent-send');
    const input = byId('agent-message');
    if (send) {
      send.disabled = isBusy;
      send.textContent = isBusy ? '处理中' : '发送';
    }
    if (input) input.disabled = isBusy;
  }

  function appendMessage(message) {
    state.messages.push(message);
    renderMessage(message);
    setEmptyState();
    updateSessionTitle();
  }

  function updateSessionTitle() {
    const firstUser = state.messages.find((item) => item.role === 'user');
    const title = firstUser ? firstUser.content.slice(0, 28) : '新建对话';
    const existing = state.sessions.find((item) => item.id === state.activeSessionId);
    if (existing) {
      existing.title = title;
      existing.time = nowTime();
    } else {
      state.sessions.unshift({ id: state.activeSessionId, title, time: nowTime() });
    }
    renderSessions();
  }

  function renderSessions() {
    const list = byId('history-list');
    if (!list) return;
    list.innerHTML = state.sessions.map((session) => {
      const active = session.id === state.activeSessionId ? ' active' : '';
      return `
        <button type="button" class="history-item${active}" data-session-id="${escapeHtml(session.id)}">
          <div class="history-title">${escapeHtml(session.title)}</div>
          <div class="history-time">${escapeHtml(session.time)}</div>
        </button>
      `;
    }).join('');
  }

  function resetSession() {
    state.activeSessionId = createId();
    state.messages = [];
    const log = byId('agent-chat-log');
    if (log) log.innerHTML = '';
    const input = byId('agent-message');
    if (input) input.value = '';
    setEmptyState();
  }

  function historyPayload() {
    return state.messages.slice(-8).map((item) => ({
      role: item.role,
      content: item.content,
    }));
  }

  async function sendMessage(text = null) {
    const input = byId('agent-message');
    const raw = (text ?? input?.value ?? '').trim();
    if (!raw) return;
    if (input) input.value = '';

    appendMessage({ role: 'user', content: raw, time: nowTime() });
    const pending = {
      role: 'assistant',
      content: '正在识别意图并调度合适的口腔智能体...',
      time: nowTime(),
    };
    renderMessage(pending);
    setBusy(true);

    try {
      const result = await apiPost('/api/router/chat', {
        message: raw,
        history: historyPayload(),
        context: { page: 'master-chat' },
      });
      const log = byId('agent-chat-log');
      if (log?.lastElementChild) log.removeChild(log.lastElementChild);
      appendMessage({
        role: 'assistant',
        content: resultAnswer(result),
        routeHtml: routeDetails(result),
        time: nowTime(),
      });
    } catch (error) {
      const log = byId('agent-chat-log');
      if (log?.lastElementChild) log.removeChild(log.lastElementChild);
      appendMessage({
        role: 'assistant',
        content: `请求失败：\n\n${error.message || error}`,
        time: nowTime(),
      });
    } finally {
      setBusy(false);
      input?.focus();
    }
  }

  function bindEvents() {
    byId('agent-send')?.addEventListener('click', () => sendMessage());
    byId('agent-clear')?.addEventListener('click', resetSession);
    byId('new-session')?.addEventListener('click', resetSession);
    byId('agent-message')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    document.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', () => sendMessage(chip.dataset.prompt || chip.textContent || ''));
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      bindEvents();
      setEmptyState();
      renderSessions();
    });
  } else {
    bindEvents();
    setEmptyState();
    renderSessions();
  }
})();
