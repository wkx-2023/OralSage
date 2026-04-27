(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

  function value(id) {
    const node = byId(id);
    return node ? node.value.trim() : '';
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

  function formatInline(text) {
    return escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
  }

  function renderCitations(result) {
    const citations = result.citations || [];
    const webArticles = result.web_articles || [];
    const items = [];

    citations.forEach((item, index) => {
      items.push(`
        <article class="citation-card">
          <div class="citation-head">
            <span class="citation-index">${escapeHtml(item.marker || `[依据${index + 1}]`)}</span>
            <span class="citation-source">${escapeHtml(item.source_type || 'reference')}</span>
          </div>
          <div class="citation-source">${escapeHtml(item.source || '')}</div>
          ${item.title ? `<div class="citation-title">${escapeHtml(item.title)}</div>` : ''}
          <div class="citation-snippet">${escapeHtml(item.excerpt || '')}</div>
        </article>
      `);
    });

    webArticles.forEach((item, index) => {
      const title = item.title || '未提供标题';
      const link = item.link || '';
      items.push(`
        <article class="citation-card web">
          <div class="citation-head">
            <span class="citation-index">文献${index + 1}</span>
            <span class="citation-source">${escapeHtml(item.source || 'PubMed/Elsevier')}</span>
          </div>
          <div class="citation-title">${escapeHtml(title)}</div>
          <div class="citation-snippet">${escapeHtml(item.summary || '')}</div>
          ${link ? `<div class="citation-source">${escapeHtml(link)}</div>` : ''}
        </article>
      `);
    });

    return items.join('') || '<div class="result-empty">本次未返回可展示的引用依据。</div>';
  }

  function prependWarnings(result, answerHtml) {
    const warnings = [];
    if ((result.missing_fields || []).length) {
      warnings.push(`建议补充：${result.missing_fields.join('、')}`);
    }
    if ((result.safety_notes || []).length) {
      warnings.push(...result.safety_notes);
    }
    if (!warnings.length) return answerHtml;

    const list = warnings.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
    return `<div class="decision-markdown"><h3>复核提示</h3><ul>${list}</ul></div>${answerHtml}`;
  }

  async function runDecision() {
    const answer = byId('decision-answer');
    const citations = byId('decision-citations');
    const button = byId('decision-run');
    const caseText = buildCaseText();

    if (!caseText) {
      answer.classList.remove('empty');
      answer.innerHTML = '<div class="decision-markdown"><h3>病例信息为空</h3><p>请至少填写主诉、病史或检查结果。</p></div>';
      citations.innerHTML = '<div class="result-empty">等待病例信息。</div>';
      return;
    }

    button.disabled = true;
    button.textContent = '生成中...';
    answer.classList.add('empty');
    answer.textContent = '正在检索本地知识库并生成建议...';
    citations.innerHTML = '<div class="result-empty">检索中...</div>';

    try {
      const result = await apiPost('/api/decision/assist', {
        case_text: caseText,
        top_k: 8,
        allow_web: true,
      });
      answer.classList.remove('empty');
      answer.innerHTML = prependWarnings(result, renderMarkdown(result.answer || ''));
      citations.innerHTML = renderCitations(result);
    } catch (err) {
      answer.classList.remove('empty');
      answer.innerHTML = `<div class="decision-markdown"><h3>调用失败</h3><p>${escapeHtml(err.message || String(err))}</p></div>`;
      citations.innerHTML = '<div class="result-empty">未完成检索。</div>';
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
