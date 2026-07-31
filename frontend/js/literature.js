(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    articles: [],
    translations: new Map(),
    review: '',
    activeIndex: 0,
    indexView: 'list',
    terms: null,
    reviewSource: '',
    historyPanel: null,
  };

  function escape(text) {
    return window.oralUi.escapeHtml(text ?? '');
  }

  function value(id) {
    return ($(id)?.value || '').trim();
  }

  function authors(article) {
    const list = Array.isArray(article.authors) ? article.authors.filter(Boolean) : [];
    if (!list.length) return '\u672a\u63d0\u4f9b\u4f5c\u8005';
    return list.slice(0, 3).join('\u3001') + (list.length > 3 ? ' \u7b49' : '');
  }

  function clip(text, limit = 220) {
    const raw = String(text || '').trim();
    if (raw.length <= limit) return raw || '\u65e0\u6458\u8981';
    return `${raw.slice(0, limit - 3)}...`;
  }

  function articleKey(article) {
    return article.identifier || article.doi || article.link || article.title || '';
  }

  function relevance(article) {
    const percent = Number(article.relevance_percent);
    if (Number.isFinite(percent) && percent > 0) return Math.round(percent);
    return null;
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').split(/\r?\n/);
    const html = [];
    let listOpen = false;
    let tableOpen = false;

    function closeList() {
      if (listOpen) {
        html.push('</ul>');
        listOpen = false;
      }
    }

    function closeTable() {
      if (tableOpen) {
        html.push('</tbody></table>');
        tableOpen = false;
      }
    }

    function inline(text) {
      return escape(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    }

    function isTableSeparator(line) {
      return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line);
    }

    function parseTableRow(line) {
      return line.replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
    }

    lines.forEach((line) => {
      const trimmed = line.trim();
      if (trimmed === '---') {
        closeList();
        closeTable();
        return;
      }
      if (!trimmed) {
        closeList();
        closeTable();
        return;
      }
      if (trimmed.includes('|') && !isTableSeparator(trimmed)) {
        closeList();
        const cells = parseTableRow(trimmed);
        if (cells.length > 1) {
          if (!tableOpen) {
            html.push('<table class="lit-review-table"><tbody>');
            tableOpen = true;
          }
          html.push(`<tr>${cells.map((cell) => `<td>${inline(cell)}</td>`).join('')}</tr>`);
          return;
        }
      }
      if (isTableSeparator(trimmed)) return;
      closeTable();
      if (/^#\s+/.test(trimmed)) {
        closeList();
        html.push(`<h3>${inline(trimmed.replace(/^#\s+/, ''))}</h3>`);
        return;
      }
      if (/^#{2,3}\s+/.test(trimmed)) {
        closeList();
        html.push(`<h4>${inline(trimmed.replace(/^#{2,3}\s+/, ''))}</h4>`);
        return;
      }
      if (/^[-*]\s+/.test(trimmed)) {
        if (!listOpen) {
          html.push('<ul>');
          listOpen = true;
        }
        html.push(`<li>${inline(trimmed.replace(/^[-*]\s+/, ''))}</li>`);
        return;
      }
      if (/^\d+\.\s+/.test(trimmed)) {
        closeList();
        html.push(`<p class="lit-review-numbered">${inline(trimmed)}</p>`);
        return;
      }
      closeList();
      html.push(`<p>${inline(trimmed)}</p>`);
    });
    closeList();
    closeTable();
    return html.join('');
  }

  function setStatus(label, count) {
    const queryLabel = $('lit-query-label');
    const resultCount = $('lit-result-count');
    if (queryLabel) queryLabel.textContent = label;
    if (resultCount) resultCount.textContent = count;
  }

  function setIndexView(view) {
    state.indexView = view;
    document.querySelectorAll('.lit-index-tab').forEach((tab) => {
      tab.classList.toggle('active', tab.dataset.view === view);
    });
    $('lit-bookmarks')?.classList.toggle('hidden', view !== 'list');
    $('lit-review')?.classList.toggle('hidden', view !== 'review');
  }

  function renderList() {
    const box = $('lit-bookmarks');
    if (!box) return;

    if (!state.articles.length) {
      box.innerHTML = '<div class="lit-card-empty">\u6682\u65e0\u6587\u732e\u7ed3\u679c</div>';
      return;
    }

    box.innerHTML = state.articles.map((article, index) => {
      const active = index === state.activeIndex ? ' active' : '';
      const score = relevance(article);
      return `
        <button type="button" class="lit-paper-card${active}" data-index="${index}">
          <div class="lit-paper-rank">
            <span>${index + 1}</span>
            <small class="lit-paper-label">${escape(article.source || 'Source')}</small>
          </div>
          <div class="lit-paper-body">
            <div class="lit-card-topline">
              <span>${escape(article.journal || 'Unknown journal')}</span>
              <span>${escape(article.publish_date || article.year || 'Unknown date')}</span>
            </div>
            <h3>${escape(article.title || 'No title')}</h3>
            <div class="lit-authors">${escape(authors(article))}</div>
            <p>${escape(clip(article.summary, 180))}</p>
          </div>
          <div class="lit-paper-score">
            <strong>${score ? escape(score) : '--'}</strong>
            <span>\u76f8\u5173\u5ea6</span>
          </div>
        </button>
      `;
    }).join('');

    box.querySelectorAll('.lit-paper-card').forEach((node) => {
      node.addEventListener('click', () => {
        state.activeIndex = Number(node.dataset.index || 0);
        renderList();
        renderDetail();
        ensureTranslation(state.articles[state.activeIndex]);
      });
    });
  }

  function renderDetail(loading = false) {
    const paper = state.articles[state.activeIndex];
    const box = $('lit-active-paper');
    if (!box) return;

    if (!paper) {
      box.innerHTML = '<p class="lit-empty">\u7b49\u5f85\u68c0\u7d22\u6587\u732e\u8be6\u60c5...</p>';
      return;
    }

    const translation = state.translations.get(articleKey(paper));
    const link = paper.link
      ? `<a class="lit-detail-link primary" href="${escape(paper.link)}" target="_blank" rel="noreferrer">\u6253\u5f00\u539f\u6587</a>`
      : '';
    const translatedTitle = translation?.title_zh || (loading ? '\u6b63\u5728\u7ffb\u8bd1\u6807\u9898...' : '\u7b49\u5f85\u7ffb\u8bd1');
    const translatedSummary = translation?.summary_zh || (loading ? '\u6b63\u5728\u7ffb\u8bd1\u6458\u8981...' : '\u70b9\u51fb\u6587\u732e\u540e\u81ea\u52a8\u7ffb\u8bd1');
    const points = Array.isArray(translation?.key_points) ? translation.key_points : [];

    box.innerHTML = `
      <div class="lit-detail-badges">
        <span class="lit-badge">${escape(paper.source || 'Unknown source')}</span>
        <span class="lit-badge soft">${escape(paper.year || paper.publish_date || 'Unknown date')}</span>
      </div>
      <h2 class="lit-detail-title">${escape(paper.title || 'No title')}</h2>
      <div class="lit-detail-authors">${escape(authors(paper))}</div>
      <div class="lit-detail-meta">
        <span>${escape(paper.journal || 'Unknown journal')}</span>
        <span>${escape(paper.publish_date || '')}</span>
        ${paper.doi ? `<span>DOI: ${escape(paper.doi)}</span>` : ''}
      </div>
      <div class="lit-detail-actions">${link}</div>

      <section class="lit-detail-abstract translated">
        <h4>\u6807\u9898\u7ffb\u8bd1</h4>
        <p>${escape(translatedTitle)}</p>
      </section>

      <section class="lit-detail-abstract translated">
        <h4>\u6458\u8981\u7ffb\u8bd1</h4>
        <p>${escape(translatedSummary)}</p>
        ${points.length ? `<ul class="lit-key-points">${points.map((item) => `<li>${escape(item)}</li>`).join('')}</ul>` : ''}
      </section>

      <section class="lit-detail-abstract">
        <h4>\u82f1\u6587\u6458\u8981</h4>
        <p>${escape(paper.summary || 'Abstract not available')}</p>
      </section>
    `;
  }

  function renderReview() {
    const box = $('lit-review');
    if (!box) return;
    if (!state.review) {
      box.textContent = '\u7b49\u5f85\u68c0\u7d22\u540e\u751f\u6210\u7efc\u8ff0...';
      return;
    }
    const terms = state.terms || {};
    const meta = [
      `\u82f1\u6587\u5173\u952e\u8bcd\uff1a${terms.recommended_keywords || '-'}`,
      `\u65f6\u95f4\u7a97\u53e3\uff1a${terms.recommended_months || '-'} \u4e2a\u6708`,
      `\u7efc\u8ff0\u6765\u6e90\uff1a${state.reviewSource || '-'}`,
    ];
    box.innerHTML = `
      <div class="lit-review-meta">${meta.map((item) => `<span>${escape(item)}</span>`).join('')}</div>
      <article class="lit-review-content">${renderMarkdown(state.review)}</article>
    `;
  }

  async function ensureTranslation(article) {
    if (!article) return;
    const key = articleKey(article);
    if (state.translations.has(key)) {
      renderDetail(false);
      return;
    }

    renderDetail(true);
    try {
      const result = await window.oralUi.apiPost('/api/literature/translate', { article });
      state.translations.set(key, result.translation || {});
    } catch (err) {
      state.translations.set(key, {
        title_zh: '\u7ffb\u8bd1\u5931\u8d25',
        summary_zh: err.message || String(err),
        key_points: [],
        source: 'error',
      });
    }
    if (state.articles[state.activeIndex] && articleKey(state.articles[state.activeIndex]) === key) {
      renderDetail(false);
    }
  }

  function saveHistory(query, months, limit) {
    state.historyPanel?.save({
      title: query,
      data: {
        query,
        months,
        limit,
        articles: state.articles,
        translations: Array.from(state.translations.entries()),
        review: state.review,
        reviewSource: state.reviewSource,
        terms: state.terms,
        activeIndex: state.activeIndex,
        indexView: state.indexView,
      },
    });
  }

  function restoreHistory(record) {
    const data = record?.data || {};
    if (!Array.isArray(data.articles) && !data.review) {
      setStatus('历史记录无法恢复', '请重新检索');
      $('lit-bookmarks').innerHTML = '<div class="lit-card-empty">这条历史记录没有可恢复内容，请重新执行一次检索后再查看。</div>';
      $('lit-active-paper').innerHTML = '<p class="lit-empty">未保存文献详情</p>';
      return;
    }
    const query = $('lit-query');
    const months = $('lit-months');
    const limit = $('lit-limit');
    if (query) query.value = data.query || '';
    if (months) months.value = String(data.months || 6);
    if (limit) limit.value = String(data.limit || 5);
    state.articles = Array.isArray(data.articles) ? data.articles : [];
    state.translations = new Map(Array.isArray(data.translations) ? data.translations : []);
    state.review = data.review || '';
    state.reviewSource = data.reviewSource || '';
    state.terms = data.terms || null;
    state.activeIndex = Number(data.activeIndex || 0);
    setStatus(`关键词：${state.terms?.recommended_keywords || data.query || '-'}`, `共 ${state.articles.length} 篇`);
    renderList();
    renderDetail();
    renderReview();
    setIndexView(data.indexView || 'list');
  }

  async function runSearch() {
    const query = value('lit-query');
    if (!query) {
      setStatus('\u8bf7\u8f93\u5165\u68c0\u7d22\u4e3b\u9898', '\u672a\u6267\u884c');
      return;
    }

    const months = Number(value('lit-months') || 6);
    const limit = Number(value('lit-limit') || 5);
    setStatus(`\u68c0\u7d22\u4e2d\uff1a${query}`, '\u8bf7\u6c42 PubMed / Elsevier');
    $('lit-run')?.setAttribute('disabled', 'disabled');
    $('lit-review').textContent = '\u6b63\u5728\u751f\u6210\u6587\u732e\u7efc\u8ff0...';
    setIndexView('list');

    try {
      const result = await window.oralUi.apiPost('/api/literature/search', { query, months, limit });
      state.articles = Array.isArray(result.articles) ? result.articles : [];
      state.translations = new Map();
      state.review = result.review || result.report || '';
      state.reviewSource = result.review_source || result.report_source || '';
      state.terms = result.terms || null;
      state.activeIndex = 0;
      setStatus(`\u5173\u952e\u8bcd\uff1a${result.terms?.recommended_keywords || query}`, `\u5171 ${state.articles.length} \u7bc7`);
      renderList();
      renderDetail();
      renderReview();
      saveHistory(query, months, limit);
      ensureTranslation(state.articles[0]);
    } catch (err) {
      state.articles = [];
      state.review = '';
      setStatus('\u68c0\u7d22\u5931\u8d25', '\u8bf7\u67e5\u770b\u9519\u8bef');
      $('lit-bookmarks').innerHTML = `<div class="lit-card-empty">${escape(err.message || err)}</div>`;
      $('lit-active-paper').innerHTML = '<p class="lit-empty">\u672a\u83b7\u53d6\u5230\u6587\u732e\u8be6\u60c5</p>';
      $('lit-review').textContent = err.message || String(err);
    } finally {
      $('lit-run')?.removeAttribute('disabled');
    }
  }

  function bind() {
    state.historyPanel = window.oralUi.createHistoryPanel({
      key: 'literature',
      title: '前沿文献历史',
      newLabel: '+ 新建检索',
      emptyText: '暂无文献检索记录',
      onNew: () => {
        const query = $('lit-query');
        if (query) query.value = '';
        state.articles = [];
        state.translations = new Map();
        state.review = '';
        state.reviewSource = '';
        state.terms = null;
        state.activeIndex = 0;
        setStatus('尚未检索', '等待检索');
        $('lit-bookmarks').innerHTML = '<div class="lit-card-empty">\u8f93\u5165\u4e3b\u9898\u540e\u5f00\u59cb\u68c0\u7d22\u771f\u5b9e\u6587\u732e</div>';
        $('lit-active-paper').innerHTML = '<p class="lit-empty">\u7b49\u5f85\u68c0\u7d22\u6587\u732e\u8be6\u60c5...</p>';
        renderReview();
        setIndexView('list');
      },
      onSelect: restoreHistory,
    });
    $('lit-run')?.addEventListener('click', runSearch);
    document.querySelectorAll('.lit-index-tab').forEach((tab) => {
      tab.addEventListener('click', () => setIndexView(tab.dataset.view || 'list'));
    });
    $('lit-query')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') runSearch();
    });
    $('lit-bookmarks').innerHTML = '<div class="lit-card-empty">\u8f93\u5165\u4e3b\u9898\u540e\u5f00\u59cb\u68c0\u7d22\u771f\u5b9e\u6587\u732e</div>';
    setIndexView('list');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
