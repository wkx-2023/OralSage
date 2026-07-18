(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;
  const sanitizeDisplayText = window.oralUi.sanitizeDisplayText || ((value) => String(value || '')
    .replace(/\b(?:chunk(?:_id)?|core|chapter|node|scope(?:_ref)?|document|kg)\s*[.:=]\s*[A-Za-z0-9][A-Za-z0-9._:/-]*/gi, '')
    .replace(/[\(\[（【]\s*[\)\]）】]/g, '')
    .trim());
  let historyPanel = null;

  function value(id) {
    const node = byId(id);
    return node ? node.value.trim() : '';
  }

  function outputCard() {
    return byId('decision-output');
  }

  function buildCaseText() {
    const rows = [
      ['性别', value('decision-gender')],
      ['年龄', value('decision-age')],
      ['主诉', value('decision-chief')],
      ['病史', value('decision-history')],
      ['检查结果', value('decision-exam')],
      ['影像发现', value('decision-imaging')],
      ['目前处理', value('decision-current')],
    ].filter(([, val]) => val);
    return rows.map(([label, val]) => `${label}：${val}`).join('\n');
  }

  function formData() {
    return {
      gender: value('decision-gender'),
      age: value('decision-age'),
      chief: value('decision-chief'),
      history: value('decision-history'),
      exam: value('decision-exam'),
      imaging: value('decision-imaging'),
      current: value('decision-current'),
    };
  }

  function fillForm(data = {}) {
    const map = {
      'decision-gender': data.gender || '',
      'decision-age': data.age || '',
      'decision-chief': data.chief || '',
      'decision-history': data.history || '',
      'decision-exam': data.exam || '',
      'decision-imaging': data.imaging || '',
      'decision-current': data.current || '',
    };
    Object.entries(map).forEach(([id, val]) => {
      const node = byId(id);
      if (node) node.value = val;
    });
  }

  function clipText(text, limit = 320) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length <= limit) return value;
    return `${value.slice(0, limit - 1).trim()}...`;
  }

  function formatInline(text) {
    return escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }

  function renderMarkdown(markdown) {
    const lines = sanitizeDisplayText(markdown).split(/\r?\n/);
    const html = [];
    let listType = null;

    function closeList() {
      if (listType) {
        html.push(`</${listType}>`);
        listType = null;
      }
    }

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const rawLine = lines[lineIndex];
      const line = rawLine.trim();
      if (!line) {
        const nextLine = lines.slice(lineIndex + 1).find((candidate) => candidate.trim());
        if (listType === 'ul' && nextLine && /^\s*[-*]\s+/.test(nextLine)) continue;
        if (listType === 'ol' && nextLine && /^\s*\d+[.)、]\s*/.test(nextLine)) continue;
        closeList();
        continue;
      }

      const heading = line.match(/^#{1,3}\s+(.+)$/);
      if (heading) {
        closeList();
        html.push(`<h3>${escapeHtml(heading[1])}</h3>`);
        continue;
      }

      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (bullet) {
        if (listType !== 'ul') {
          closeList();
          html.push('<ul>');
          listType = 'ul';
        }
        html.push(`<li>${formatInline(bullet[1])}</li>`);
        continue;
      }

      const ordered = line.match(/^(\d+)[.)、]\s*(.+)$/);
      if (ordered) {
        if (listType !== 'ol') {
          closeList();
          const start = Number(ordered[1]) || 1;
          html.push(start > 1 ? `<ol start="${start}">` : '<ol>');
          listType = 'ol';
        }
        html.push(`<li>${formatInline(ordered[2])}</li>`);
        continue;
      }

      closeList();
      html.push(`<p>${formatInline(line)}</p>`);
    }

    closeList();
    return `<div class="decision-markdown">${html.join('')}</div>`;
  }

  function renderCitations(result) {
    const citations = result.citations || [];
    const webArticles = result.web_articles || [];
    if (!citations.length && !webArticles.length) {
      return '<section class="decision-citations"><h3>引用依据</h3><p class="muted">本次未返回可展示的引用依据。</p></section>';
    }

    const citationItems = citations.map((item, index) => {
      const labels = item.matched_task_labels && item.matched_task_labels.length
        ? item.matched_task_labels.join('、')
        : item.task_label || '依据';
      const title = item.title || item.source || '未命名依据';
      const score = item.rerank_score !== undefined && item.rerank_score !== null
        ? `；重排分数 ${escapeHtml(String(item.rerank_score))}`
        : '';
      return `
        <li>
          <p><strong>${escapeHtml(item.marker || `[依据${index + 1}]`)} ${escapeHtml(title)}</strong></p>
          <p class="citation-meta">${escapeHtml(labels)}；${escapeHtml(item.source_type || 'reference')}${score}</p>
          ${item.source && item.source !== title ? `<p class="citation-meta">${escapeHtml(item.source)}</p>` : ''}
          ${item.relevance ? `<p class="citation-meta">${escapeHtml(item.relevance)}</p>` : ''}
          <p>${escapeHtml(clipText(item.excerpt || '', 360))}</p>
        </li>
      `;
    }).join('');

    const webItems = webArticles.map((item, index) => `
      <li>
        <p><strong>文献${index + 1} ${escapeHtml(item.title || '未提供标题')}</strong></p>
        <p class="citation-meta">${escapeHtml(item.source || 'PubMed/Elsevier')}</p>
        <p>${escapeHtml(clipText(item.summary || '', 320))}</p>
        ${item.link ? `<p class="citation-meta">${escapeHtml(item.link)}</p>` : ''}
      </li>
    `).join('');

    return `
      <section class="decision-citations">
        <h3>引用依据</h3>
        <ol>
          ${citationItems}
          ${webItems}
        </ol>
      </section>
    `;
  }

  function renderWarnings(result) {
    const warnings = [];
    if ((result.missing_fields || []).length) {
      warnings.push(`建议补充：${result.missing_fields.join('、')}`);
    }
    if ((result.safety_notes || []).length) {
      warnings.push(...result.safety_notes);
    }
    const reranker = result.reranker || {};
    if (reranker.enabled === false) {
      warnings.push('Qwen rerank 未配置或不可用，当前使用词法重排兜底。');
    }
    if (!warnings.length) return '';
    return `
      <section class="decision-warnings">
        <h3>复核提示</h3>
        <ul>${warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>
      </section>
    `;
  }

  function renderResult(result) {
    return `
      ${renderWarnings(result)}
      ${renderMarkdown(result.answer || '')}
      ${renderCitations(result)}
    `;
  }

  async function runDecision() {
    const answer = byId('decision-answer');
    const citations = byId('decision-citations');
    const card = outputCard();
    const button = byId('decision-run');
    const caseText = buildCaseText();

    if (!caseText) {
      card?.classList.remove('empty');
      answer.innerHTML = '<div class="decision-markdown"><h3>病例信息为空</h3><p>请至少填写主诉、病史或检查结果。</p></div>';
      citations.innerHTML = '';
      return;
    }

    button.disabled = true;
    button.textContent = '生成中...';
    card?.classList.add('empty');
    answer.textContent = '正在规划检索任务、匹配证据并生成建议...';
    citations.innerHTML = '';

    try {
      const result = await apiPost('/api/decision/assist', {
        case_text: caseText,
        top_k: 8,
        allow_web: true,
      });
      card?.classList.remove('empty');
      const html = renderResult(result);
      answer.innerHTML = html;
      citations.innerHTML = '';
      historyPanel?.save({
        title: value('decision-chief') || caseText,
        data: {
          form: formData(),
          html,
          raw: result,
        },
      });
    } catch (err) {
      card?.classList.remove('empty');
      answer.innerHTML = `<div class="decision-markdown"><h3>调用失败</h3><p>${escapeHtml(err.message || String(err))}</p></div>`;
      citations.innerHTML = '';
    } finally {
      button.disabled = false;
      button.textContent = '生成临床建议';
    }
  }

  function boot() {
    historyPanel = window.oralUi.createHistoryPanel({
      key: 'decision',
      title: '决策辅助历史',
      newLabel: '+ 新建病例',
      emptyText: '暂无决策记录',
      onNew: () => {
        fillForm({});
        outputCard()?.classList.add('empty');
        const answer = byId('decision-answer');
        const citations = byId('decision-citations');
        if (answer) answer.textContent = '等待执行...';
        if (citations) citations.innerHTML = '';
      },
      onSelect: (record) => {
        const data = record?.data || {};
        if (!data.form && !data.html && !data.raw) {
          outputCard()?.classList.remove('empty');
          const answer = byId('decision-answer');
          if (answer) answer.innerHTML = '<div class="decision-markdown"><h3>历史记录无法恢复</h3><p>这条记录是在历史保存功能完善前生成的，请重新执行一次任务后再查看。</p></div>';
          return;
        }
        fillForm(data.form || {});
        outputCard()?.classList.remove('empty');
        const answer = byId('decision-answer');
        const citations = byId('decision-citations');
        if (answer) answer.innerHTML = data.html || renderResult(data.raw || {});
        if (citations) citations.innerHTML = '';
      },
    });
    const button = byId('decision-run');
    if (button) {
      button.addEventListener('click', runDecision);
    }
  }

  window.oralUi.decisionInternals = { renderMarkdown };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
