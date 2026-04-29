(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

  function value(id) {
    return (byId(id)?.value || '').trim();
  }

  function esc(text) {
    return escapeHtml(String(text ?? ''));
  }

  function clip(text, limit = 260) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (!value) return '';
    if (value.length <= limit) return value;
    return `${value.slice(0, limit - 3).trim()}...`;
  }

  function formatInline(text) {
    return esc(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').split(/\r?\n/);
    const html = [];
    let listOpen = false;
    let orderedOpen = false;

    function closeLists() {
      if (listOpen) {
        html.push('</ul>');
        listOpen = false;
      }
      if (orderedOpen) {
        html.push('</ol>');
        orderedOpen = false;
      }
    }

    lines.forEach((rawLine) => {
      const line = rawLine.trim();
      if (!line) {
        closeLists();
        return;
      }

      const heading = line.match(/^#{1,3}\s+(.+)$/);
      if (heading) {
        closeLists();
        html.push(`<h3>${formatInline(heading[1])}</h3>`);
        return;
      }

      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (bullet) {
        if (orderedOpen) {
          html.push('</ol>');
          orderedOpen = false;
        }
        if (!listOpen) {
          html.push('<ul>');
          listOpen = true;
        }
        html.push(`<li>${formatInline(bullet[1])}</li>`);
        return;
      }

      const ordered = line.match(/^\d+\.\s+(.+)$/);
      if (ordered) {
        if (listOpen) {
          html.push('</ul>');
          listOpen = false;
        }
        if (!orderedOpen) {
          html.push('<ol>');
          orderedOpen = true;
        }
        html.push(`<li>${formatInline(ordered[1])}</li>`);
        return;
      }

      closeLists();
      html.push(`<p>${formatInline(line)}</p>`);
    });

    closeLists();
    return html.join('');
  }

  function sourceLabel(item) {
    return item.source_type_label || item.source_type || 'reference';
  }

  function renderTimeline(citations) {
    const box = byId('pathology-citations');
    if (!box) return;
    if (!citations.length) {
      box.innerHTML = '<div class="timeline-item empty">本次未返回可展示的引用依据。</div>';
      return;
    }

    box.innerHTML = citations.slice(0, 10).map((item, index) => `
      <div class="timeline-item">
        <div class="timeline-index">${index + 1}</div>
        <div class="timeline-text">
          <div class="timeline-title">${esc(item.marker || `[${index + 1}]`)} ${esc(item.title || item.source || '未命名依据')}</div>
          <div>${esc(clip(item.excerpt || '', 210))}</div>
          <div class="timeline-meta">${esc(sourceLabel(item))}${item.year ? ` · ${esc(item.year)}` : ''}${item.score !== null && item.score !== undefined ? ` · ${esc(item.score)}` : ''}</div>
        </div>
      </div>
    `).join('');
  }

  function renderRefList(citations, result) {
    const box = byId('pathology-ref-list');
    if (!box) return;

    const plan = result.normalized_query || {};
    const planCard = `
      <article class="ref-item">
        <div class="ref-head">
          <div>
            <div class="ref-title">检索计划</div>
            <div class="ref-meta">${esc(plan.source || 'rules')} · ${result.mcp_used ? '已调用 MCP 文献' : '本地知识库优先'}</div>
          </div>
        </div>
        <div class="ref-snippet">${esc(clip(plan.local_query_zh || result.local_query || '', 180))}</div>
        <div class="ref-tags">
          ${(plan.entities_zh || []).slice(0, 4).map((item) => `<span class="ref-tag">${esc(item)}</span>`).join('')}
        </div>
      </article>
    `;

    const refs = citations.slice(0, 8).map((item) => {
      const link = item.link ? `<div class="ref-meta">${esc(item.link)}</div>` : '';
      return `
        <article class="ref-item">
          <div class="ref-head">
            <div>
              <div class="ref-title">${esc(item.marker || '')} ${esc(item.title || item.source || '未命名依据')}</div>
              <div class="ref-meta">${esc(item.source || '')} · ${esc(sourceLabel(item))}${item.year ? ` · ${esc(item.year)}` : ''}</div>
            </div>
            <div class="ref-score">${item.score !== null && item.score !== undefined ? esc(item.score) : '--'}</div>
          </div>
          <div class="ref-snippet">${esc(clip(item.excerpt || '', 220))}</div>
          ${link}
        </article>
      `;
    }).join('');

    box.innerHTML = planCard + (refs || '<article class="ref-item"><div class="ref-title">暂无引用依据</div></article>');
  }

  function renderSafety(result) {
    const notes = [];
    if (Array.isArray(result.safety_notes)) notes.push(...result.safety_notes);
    if (Array.isArray(result.errors)) notes.push(...result.errors.slice(0, 3));
    if (!notes.length) return '';
    return `
      <section>
        <h3>复核提示</h3>
        <ul>${notes.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>
      </section>
    `;
  }

  async function runPathology() {
    const question = value('pathology-question');
    const answer = byId('pathology-answer');
    const button = byId('pathology-run');
    const empty = byId('pathology-empty-state');
    const userRow = byId('pathology-user-row');
    const aiRow = byId('pathology-ai-row');

    if (!question) {
      answer.textContent = '请输入病理问题后发送。';
      return;
    }

    empty.hidden = true;
    userRow.hidden = false;
    aiRow.hidden = false;
    byId('pathology-last-question').textContent = question;
    byId('pathology-question-time').textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    answer.textContent = '正在规划检索、调用本地知识库与 MCP 文献工具，并生成结构化回答...';
    renderTimeline([]);
    renderRefList([], { normalized_query: {}, mcp_used: false });
    button.disabled = true;
    button.textContent = '检索中';

    try {
      const result = await apiPost('/api/pathology/ask', {
        question,
        top_k: 8,
        allow_web: true,
      });
      const citations = Array.isArray(result.citations) ? result.citations : [];
      answer.innerHTML = `${renderSafety(result)}${renderMarkdown(result.answer || '')}`;
      renderTimeline(citations);
      renderRefList(citations, result);
    } catch (err) {
      answer.innerHTML = `<h3>调用失败</h3><p>${esc(err.message || String(err))}</p>`;
      renderTimeline([]);
    } finally {
      button.disabled = false;
      button.textContent = '发送';
    }
  }

  function bind() {
    byId('pathology-run')?.addEventListener('click', runPathology);
    byId('pathology-question')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runPathology();
      }
    });
    renderTimeline([]);
    renderRefList([], { normalized_query: {}, mcp_used: false });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
