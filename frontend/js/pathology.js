(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;
  const sanitizeDisplayText = window.oralUi.sanitizeDisplayText || ((value) => String(value || '')
    .replace(/\b(?:chunk(?:_id)?|core|chapter|node|scope(?:_ref)?|document|kg)\s*[.:=]\s*[A-Za-z0-9][A-Za-z0-9._:/-]*/gi, '')
    .replace(/[\(\[（【]\s*[\)\]）】]/g, '')
    .trim());
  let historyPanel = null;

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
    const lines = sanitizeDisplayText(markdown).split(/\r?\n/);
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

    lines.forEach((rawLine, lineIndex) => {
      const line = rawLine.trim();
      if (!line) {
        const nextLine = lines.slice(lineIndex + 1).find((candidate) => candidate.trim());
        if (listOpen && nextLine && /^\s*[-*]\s+/.test(nextLine)) return;
        if (orderedOpen && nextLine && /^\s*\d+[.)、]\s*/.test(nextLine)) return;
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

      const ordered = line.match(/^(\d+)[.)、]\s*(.+)$/);
      if (ordered) {
        if (listOpen) {
          html.push('</ul>');
          listOpen = false;
        }
        if (!orderedOpen) {
          const start = Number(ordered[1]) || 1;
          html.push(start > 1 ? `<ol start="${start}">` : '<ol>');
          orderedOpen = true;
        }
        html.push(`<li>${formatInline(ordered[2])}</li>`);
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
      box.innerHTML = '<div class="timeline-item empty">本次暂未返回可展示的引用依据。</div>';
      return;
    }

    box.innerHTML = citations.slice(0, 10).map((item, index) => `
      <div class="timeline-item">
        <div class="timeline-index">${index + 1}</div>
        <div class="timeline-text">
          <div class="timeline-title">${esc(item.marker || `[依据${index + 1}]`)} ${esc(item.title || item.source || '未命名依据')}</div>
          <div>${esc(clip(item.excerpt || '', 210))}</div>
          <div class="timeline-meta">${esc(sourceLabel(item))}${item.year ? ` · ${esc(item.year)}` : ''}${item.score !== null && item.score !== undefined ? ` · ${esc(item.score)}` : ''}</div>
        </div>
      </div>
    `).join('');
  }

  function renderRefList(citations) {
    const box = byId('pathology-ref-list');
    if (!box) return;

    if (!citations.length) {
      box.innerHTML = '<article class="ref-item ref-empty"><div class="ref-title">暂无引用依据</div></article>';
      return;
    }

    box.innerHTML = citations.slice(0, 8).map((item) => {
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

  function resetToEmpty() {
    const empty = byId('pathology-empty-state');
    const userRow = byId('pathology-user-row');
    const aiRow = byId('pathology-ai-row');
    if (empty) empty.hidden = false;
    if (userRow) userRow.hidden = true;
    if (aiRow) aiRow.hidden = true;
    renderRefList([]);
  }

  async function runPathology() {
    const question = value('pathology-question');
    const answer = byId('pathology-answer');
    const button = byId('pathology-run');
    const empty = byId('pathology-empty-state');
    const userRow = byId('pathology-user-row');
    const aiRow = byId('pathology-ai-row');

    if (!question) {
      resetToEmpty();
      byId('pathology-question')?.focus();
      return;
    }

    empty.hidden = true;
    userRow.hidden = false;
    aiRow.hidden = false;
    byId('pathology-last-question').textContent = question;
    byId('pathology-question-time').textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    answer.textContent = '正在检索本地知识库并生成结构化回答...';
    renderTimeline([]);
    renderRefList([]);
    button.disabled = true;
    button.textContent = '检索中';

    try {
      const result = await apiPost('/api/pathology/ask', {
        question,
        top_k: 8,
        allow_web: true,
      });
      const citations = Array.isArray(result.citations) ? result.citations : [];
      const answerHtml = `${renderSafety(result)}${renderMarkdown(result.answer || '')}`;
      answer.innerHTML = answerHtml;
      renderTimeline(citations);
      renderRefList(citations);
      historyPanel?.save({
        title: question,
        data: {
          question,
          answerHtml,
          answer: result.answer || '',
          citations,
          safetyNotes: result.safety_notes || [],
          errors: result.errors || [],
          time: byId('pathology-question-time')?.textContent || '',
        },
      });
    } catch (err) {
      answer.innerHTML = `<h3>调用失败</h3><p>${esc(err.message || String(err))}</p>`;
      renderTimeline([]);
    } finally {
      button.disabled = false;
      button.textContent = '发送';
    }
  }

  function bind() {
    historyPanel = window.oralUi.createHistoryPanel({
      key: 'pathology',
      title: '病理问答历史',
      newLabel: '+ 新建问题',
      emptyText: '暂无病理问答记录',
      onNew: () => {
        const input = byId('pathology-question');
        if (input) input.value = '';
        resetToEmpty();
      },
      onSelect: (record) => {
        const data = record?.data || {};
        if (!data.answerHtml && !data.answer && !data.question) {
          resetToEmpty();
          const answer = byId('pathology-answer');
          if (answer) answer.innerHTML = '<h3>历史记录无法恢复</h3><p>这条记录是在历史保存功能完善前生成的，请重新提问后再查看。</p>';
          return;
        }
        const input = byId('pathology-question');
        const empty = byId('pathology-empty-state');
        const userRow = byId('pathology-user-row');
        const aiRow = byId('pathology-ai-row');
        if (input) input.value = data.question || '';
        if (empty) empty.hidden = true;
        if (userRow) userRow.hidden = false;
        if (aiRow) aiRow.hidden = false;
        byId('pathology-last-question').textContent = data.question || '';
        byId('pathology-question-time').textContent = data.time || record.time || '--:--';
        const answer = byId('pathology-answer');
        if (answer) answer.innerHTML = data.answerHtml || renderMarkdown(data.answer || '');
        renderTimeline(Array.isArray(data.citations) ? data.citations : []);
        renderRefList(Array.isArray(data.citations) ? data.citations : []);
      },
    });
    resetToEmpty();

    byId('pathology-run')?.addEventListener('click', runPathology);
    byId('pathology-question')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runPathology();
      }
    });
  }

  window.oralUi.pathologyInternals = { renderMarkdown };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
