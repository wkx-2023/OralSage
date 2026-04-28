(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

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
    const lines = String(markdown || '').split(/\r?\n/);
    const html = [];
    let inList = false;

    function closeList() {
      if (inList) {
        html.push('</ul>');
        inList = false;
      }
    }

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) {
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
        if (!inList) {
          html.push('<ul>');
          inList = true;
        }
        html.push(`<li>${formatInline(bullet[1])}</li>`);
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
      answer.innerHTML = renderResult(result);
      citations.innerHTML = '';
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
    const button = byId('decision-run');
    if (button) {
      button.addEventListener('click', runDecision);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
