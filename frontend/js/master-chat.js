(() => {
  const { byId, escapeHtml } = window.oralUi;
  const STORAGE_KEY = 'oralsage.history.master-chat';

  const state = {
    messages: [],
    sessions: loadSessions(),
    activeSessionId: createId(),
  };

  function createId() {
    return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function nowTime() {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  function sessionTime() {
    return new Date().toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function loadSessions() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }

  function saveSessions() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions.slice(0, 30)));
  }

  function normalizeMarkdownText(text) {
    let value = String(text || '').trim();
    if (/^```(?:markdown|md)?\s*[\s\S]*```$/i.test(value)) {
      value = value.replace(/^```(?:markdown|md)?\s*/i, '').replace(/\s*```$/i, '').trim();
    }
    const escapedNewlineCount = (value.match(/\\n/g) || []).length;
    if (!/\n/.test(value) && escapedNewlineCount >= 2) {
      value = value.replace(/\\n/g, '\n');
    }
    return value;
  }

  function formatMarkdown(text) {
    const lines = normalizeMarkdownText(text).split(/\r?\n/);
    const html = [];
    let listType = null;
    let inCode = false;
    let codeLines = [];
    let tableLines = [];

    function inlineMarkdown(value) {
      const escaped = escapeHtml(value || '');
      const placeholders = [];
      const keep = (htmlValue) => {
        const token = `@@MD${placeholders.length}@@`;
        placeholders.push(htmlValue);
        return token;
      };
      const protectedCode = escaped.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${code}</code>`));
      const protectedLinks = protectedCode.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => (
        keep(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`)
      ));
      const withLinks = protectedLinks.replace(
        /(https?:\/\/[^\s<]+[^\s<.,;:!?，。；：！？）\]])/g,
        (url) => keep(`<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`),
      );
      return withLinks
        .replace(/\*\*\*([\s\S]+?)\*\*\*/g, '<strong><em>$1</em></strong>')
        .replace(/___([\s\S]+?)___/g, '<strong><em>$1</em></strong>')
        .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
        .replace(/__([\s\S]+?)__/g, '<strong>$1</strong>')
        .replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
        .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>')
        .replace(/~~([\s\S]+?)~~/g, '<del>$1</del>')
        .replace(/@@MD(\d+)@@/g, (_, index) => placeholders[Number(index)] || '');
    }

    function closeList() {
      if (listType) {
        html.push(`</${listType}>`);
        listType = null;
      }
    }

    function openList(type) {
      if (listType === type) return;
      closeList();
      html.push(`<${type}>`);
      listType = type;
    }

    function flushCode() {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = [];
        inCode = false;
      }
    }

    function isTableLine(line) {
      const trimmed = line.trim();
      return trimmed.includes('|') && trimmed.split('|').length >= 3;
    }

    function isTableDivider(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }

    function splitTableRow(line) {
      return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
    }

    function flushTable() {
      if (!tableLines.length) return;
      const rowsOnly = tableLines.filter((line) => line.trim() && !isTableDivider(line));
      if (rowsOnly.length >= 2) {
        const headers = splitTableRow(rowsOnly[0]);
        const rows = rowsOnly.slice(1).map(splitTableRow);
        const colCount = Math.max(headers.length, ...rows.map((row) => row.length));
        html.push(
          `<div class="markdown-table-wrap"><table><thead><tr>${Array.from({ length: colCount }, (_, index) => `<th>${inlineMarkdown(headers[index] || '')}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${Array.from({ length: colCount }, (_, index) => `<td>${inlineMarkdown(row[index] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`,
        );
      } else {
        tableLines.forEach((line) => html.push(`<p>${inlineMarkdown(line)}</p>`));
      }
      tableLines = [];
    }

    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) {
          flushCode();
        } else {
          closeList();
          flushTable();
          inCode = true;
          codeLines = [];
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      if (!line.trim() && tableLines.length) {
        continue;
      }
      if (isTableLine(line)) {
        closeList();
        tableLines.push(line);
        continue;
      }
      flushTable();
      if (/^\s*---+\s*$/.test(line)) {
        closeList();
        html.push('<hr>');
        continue;
      }
      const heading = line.match(/^\s*(#{1,6})\s*(.+?)\s*#*\s*$/);
      if (heading) {
        closeList();
        const level = Math.min(6, Math.max(2, heading[1].length + 1));
        html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s*>\s*/.test(line)) {
        closeList();
        html.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s*/, ''))}</blockquote>`);
        continue;
      }
      if (/^\s*[-*+•]\s+/.test(line)) {
        openList('ul');
        html.push(`<li>${inlineMarkdown(line.replace(/^\s*[-*+•]\s+/, ''))}</li>`);
        continue;
      }
      if (/^\s*\d+[.)、]\s*/.test(line)) {
        openList('ol');
        html.push(`<li>${inlineMarkdown(line.replace(/^\s*\d+[.)、]\s*/, ''))}</li>`);
        continue;
      }
      if (!line.trim()) {
        closeList();
        flushTable();
        html.push('');
        continue;
      }
      closeList();
      html.push(`<p>${inlineMarkdown(line)}</p>`);
    }
    flushCode();
    flushTable();
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
    if (typeof final.review === 'string') return final.review;
    if (typeof final.report === 'string') return final.report;
    if (final.result?.report_text) return final.result.report_text;
    if (final.result?.markdown) return final.result.markdown;
    if (final.result?.review) return final.result.review;
    if (final.result?.report) return final.result.report;
    if (typeof final.message === 'string') return final.message;
    return JSON.stringify(final, null, 2);
  }

  function authorsText(article) {
    const authors = Array.isArray(article?.authors) ? article.authors.filter(Boolean) : [];
    if (!authors.length) return '未提供作者';
    return authors.slice(0, 4).join('、') + (authors.length > 4 ? ' 等' : '');
  }

  function translatedValue(article, key) {
    const translation = article?.translation || {};
    return translation.source === 'llm' ? String(translation[key] || '').replace(/\s+/g, ' ').trim() : '';
  }

  function compactText(text, fallback = '暂无内容') {
    return String(text || fallback).replace(/\s+/g, ' ').trim();
  }

  function literatureResultHtml(result) {
    const final = result?.final_result || result?.result || result || {};
    if (final.module !== 'frontier_literature') return '';

    const terms = final.terms || {};
    const articles = Array.isArray(final.chat_articles) ? final.chat_articles : (Array.isArray(final.articles) ? final.articles : []);
    const sources = Array.isArray(final.sources) && final.sources.length ? final.sources.join(' + ') : '未获得可用来源';
    const query = final.query || '';
    const months = terms.recommended_months || '-';
    const keywords = terms.recommended_keywords || query || '-';

    const articleHtml = articles.length ? articles.map((article, index) => {
      const title = compactText(translatedValue(article, 'title_zh') || article.title || 'No title');
      const englishTitle = compactText(article.title || 'No title');
      const summaryZh = compactText(translatedValue(article, 'summary_zh'), '暂未生成中文摘要翻译。');
      const summaryEn = compactText(article.summary, 'Abstract not available');
      const journal = compactText(article.journal, 'Unknown journal');
      const date = compactText(article.publish_date || article.year, 'Unknown date');
      const source = compactText(article.source, 'Unknown source');
      const score = Number(article.relevance_percent);
      const link = compactText(article.link, '');
      const doi = compactText(article.doi, '');

      return `
        <article class="literature-chat-card">
          <div class="literature-chat-rank">${index + 1}</div>
          <div class="literature-chat-body">
            <h3>${escapeHtml(title)}</h3>
            <p class="literature-chat-title-en">${escapeHtml(englishTitle)}</p>
            <div class="literature-chat-meta">
              <span>${escapeHtml(source)}</span>
              <span>${escapeHtml(journal)}</span>
              <span>${escapeHtml(date)}</span>
              ${Number.isFinite(score) && score > 0 ? `<span>相关度 ${escapeHtml(String(Math.round(score)))}%</span>` : ''}
            </div>
            <p><strong>作者：</strong>${escapeHtml(authorsText(article))}</p>
            <p><strong>中文摘要：</strong>${escapeHtml(summaryZh)}</p>
            <details class="literature-chat-abstract">
              <summary>英文摘要</summary>
              <p>${escapeHtml(summaryEn)}</p>
            </details>
            <div class="literature-chat-links">
              ${doi ? `<span>DOI：${escapeHtml(doi)}</span>` : ''}
              ${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer">打开原文</a>` : ''}
            </div>
          </div>
        </article>
      `;
    }).join('') : '<p class="literature-chat-empty">未检索到可展示的真实文献结果。</p>';

    const review = String(final.review || final.report || '').trim();
    const reviewHtml = review
      ? `<section class="literature-chat-review"><h3>综合解读</h3>${formatMarkdown(review)}</section>`
      : '';

    return `
      <section class="literature-chat-result">
        <h2>文献检索结果</h2>
        <p class="literature-chat-brief">已按“${escapeHtml(query)}”检索近 ${escapeHtml(String(months))} 个月文献，当前获得 ${escapeHtml(String(articles.length))} 篇。</p>
        <div class="literature-chat-search-meta">
          <span>关键词：${escapeHtml(keywords)}</span>
          <span>数据来源：${escapeHtml(sources)}</span>
        </div>
        <div class="literature-chat-list">${articleHtml}</div>
        ${reviewHtml}
        <p class="literature-chat-disclaimer">内容仅用于口腔医学学习与科研参考，不构成临床诊疗建议。</p>
      </section>
    `;
  }

  function renderMessage(message) {
    const log = byId('agent-chat-log');
    if (!log) return;
    const row = document.createElement('article');
    row.className = `chat-message ${message.role}`;
    const roleText = message.role === 'user' ? '你' : 'OralSage';
    const avatar = message.role === 'user' ? 'ME' : 'OS';
    const content = message.html || (message.role === 'assistant' ? formatMarkdown(message.content) : escapeHtml(message.content));
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

  function renderMessages() {
    const log = byId('agent-chat-log');
    if (!log) return;
    log.innerHTML = '';
    state.messages.forEach(renderMessage);
    setEmptyState();
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
      existing.time = sessionTime();
      existing.messages = state.messages;
    } else {
      state.sessions.unshift({ id: state.activeSessionId, title, time: sessionTime(), messages: state.messages });
    }
    saveSessions();
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
    renderSessions();
  }

  function restoreSession(sessionId) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    state.activeSessionId = session.id;
    state.messages = Array.isArray(session.messages) ? session.messages : [];
    renderMessages();
    renderSessions();
  }

  function historyPayload() {
    return state.messages.slice(-8).map((item) => ({
      role: item.role,
      content: item.content,
    }));
  }

  function learningPlaceholderAnswer(raw) {
    const escaped = escapeHtml(raw);
    return `
      <section class="learning-placeholder">
        <h2>学习总控待接入</h2>
        <p>你刚才输入的是：“${escaped}”。当前旧总控 agent 已停用，这里暂时不会再把问题路由到原来的医疗分诊链路。</p>
        <p>下一步会在这里接入新的学习型智能体，用于学习路径规划、知识点讲解、复习任务和工具调用。</p>
        <div class="learning-placeholder-actions">
          <a href="/" class="btn strong">返回总控工作台</a>
          <a href="/#toolbox" class="btn ghost">打开工具箱</a>
        </div>
      </section>
    `;
  }

  async function sendMessage(text = null) {
    const input = byId('agent-message');
    const raw = (text ?? input?.value ?? '').trim();
    if (!raw) return;
    if (input) input.value = '';

    appendMessage({ role: 'user', content: raw, time: nowTime() });
    setBusy(true);

    try {
      appendMessage({
        role: 'assistant',
        content: '',
        html: learningPlaceholderAnswer(raw),
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
    byId('history-list')?.addEventListener('click', (event) => {
      const item = event.target.closest('[data-session-id]');
      if (!item) return;
      restoreSession(item.dataset.sessionId || '');
    });
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
