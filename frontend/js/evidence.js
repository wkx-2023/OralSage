(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

  const state = {
    topics: [],
    topicGroups: {},
    dimensions: [],
    sourceTypes: [],
    activeTopicId: 'impacted_wisdom_tooth',
    activeDimensionId: 'clinical_feature',
    activeSourceType: 'all',
    groups: [],
    selectedPoint: null,
  };

  function sourceTone(sourceType) {
    if (sourceType === 'guideline' || sourceType === 'clinical_norm') return 'guideline';
    if (sourceType === 'textbook') return 'textbook';
    if (sourceType === 'clinical_case') return 'case';
    return 'reference';
  }

  function scoreLabel(score) {
    const value = Number(score || 0);
    if (value >= 1.2) return '高相关';
    if (value >= 0.65) return '中相关';
    return '候选依据';
  }

  function clip(text, limit = 180) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length <= limit) return value;
    return `${value.slice(0, limit - 3).trim()}...`;
  }

  async function apiGet(url) {
    const res = await fetch(url);
    const body = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(body));
    return body;
  }

  function activeTopic() {
    return state.topics.find((topic) => topic.id === state.activeTopicId) || state.topics[0] || null;
  }

  function renderTopicList() {
    const container = byId('topic-list');
    const count = byId('topic-count');
    if (!container) return;
    const term = (byId('knowledge-search')?.value || '').trim().toLowerCase();
    const groups = state.topicGroups || {};
    const html = Object.entries(groups).map(([groupName, topics]) => {
      const filtered = topics.filter((topic) => !term || topic.name.toLowerCase().includes(term));
      if (!filtered.length) return '';
      const items = filtered.map((topic) => {
        const active = topic.id === state.activeTopicId ? ' active' : '';
        return `
          <button type="button" class="topic-btn${active}" data-topic="${escapeHtml(topic.id)}">
            <span>${escapeHtml(topic.name)}</span>
          </button>
        `;
      }).join('');
      return `
        <section class="topic-group">
          <h4>${escapeHtml(groupName)}</h4>
          ${items}
        </section>
      `;
    }).join('');

    container.innerHTML = html || '<div class="empty-note">没有匹配的问题分类。</div>';
    if (count) count.textContent = `${state.topics.length} 类`;
  }

  function populateFilters() {
    const dimension = byId('knowledge-dimension');
    if (dimension) {
      dimension.innerHTML = [
        '<option value="all">全部知识维度</option>',
        ...state.dimensions.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`),
      ].join('');
      dimension.value = state.activeDimensionId;
    }

    const source = byId('knowledge-source');
    if (source) {
      source.innerHTML = state.sourceTypes.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
      )).join('');
      source.value = state.activeSourceType;
    }
  }

  function setLoading(message = '正在从知识库整理知识点...') {
    const groups = byId('knowledge-groups');
    const count = byId('knowledge-count');
    if (count) count.textContent = message;
    if (groups) {
      groups.innerHTML = `
        <div class="loading-state">
          <span class="loading-line"></span>
          <span class="loading-line short"></span>
          <span class="loading-line"></span>
        </div>
      `;
    }
  }

  function renderPathState() {
    document.querySelectorAll('.path-step').forEach((button) => {
      button.classList.toggle('active', button.dataset.dimension === state.activeDimensionId);
    });
  }

  function pointMatches(point, term) {
    if (!term) return true;
    const haystack = [
      point.title,
      point.summary,
      point.source_badge,
      ...(point.citations || []).map((item) => `${item.title} ${item.source} ${item.excerpt}`),
    ].join(' ').toLowerCase();
    return haystack.includes(term.toLowerCase());
  }

  function renderGroups() {
    const topic = activeTopic();
    const name = byId('active-topic-name');
    const count = byId('knowledge-count');
    const container = byId('knowledge-groups');
    const term = (byId('knowledge-search')?.value || '').trim();
    if (name && topic) name.textContent = topic.name;
    if (!container) return;

    if (!state.selectedPoint) {
      state.selectedPoint = state.groups
        .flatMap((group) => group.points || [])
        .find((point) => pointMatches(point, term)) || null;
    }

    let visibleCount = 0;
    const html = state.groups.map((group) => {
      const points = (group.points || []).filter((point) => pointMatches(point, term));
      visibleCount += points.length;
      const pointHtml = points.map((point) => {
        const tone = sourceTone(point.source_type);
        const selected = state.selectedPoint && state.selectedPoint.id === point.id ? ' selected' : '';
        return `
          <article class="knowledge-card${selected}" data-point="${escapeHtml(point.id)}">
            <div class="card-topline">
              <span class="source-pill ${tone}">${escapeHtml(point.source_label || '依据')}</span>
              <span class="score-pill">${escapeHtml(scoreLabel(point.score))}</span>
            </div>
            <h4>${escapeHtml(point.title || '未命名知识点')}</h4>
            <p>${escapeHtml(clip(point.summary, 210))}</p>
            <div class="card-meta">
              <span>${escapeHtml(point.source_badge || '来源未标注')}</span>
            </div>
          </article>
        `;
      }).join('');

      return `
        <section class="knowledge-group">
          <button type="button" class="group-head" data-toggle="${escapeHtml(group.id)}">
            <span>${escapeHtml(group.name)}</span>
            <strong>${points.length}</strong>
          </button>
          <div class="group-points">
            ${pointHtml || '<div class="empty-note">当前筛选下暂无知识点。</div>'}
          </div>
        </section>
      `;
    }).join('');

    container.innerHTML = html || '<div class="empty-note">当前主题暂无可展示知识点。</div>';
    if (count) count.textContent = `${visibleCount} 条知识点`;
  }

  function renderDetail() {
    const body = byId('evidence-detail-body');
    if (!body) return;
    const point = state.selectedPoint;
    if (!point) {
      body.classList.add('empty');
      body.textContent = '选择一个知识点后，这里会显示原文摘录、来源类型、页码和相关性说明。';
      return;
    }

    body.classList.remove('empty');
    const citations = point.citations || [];
    body.innerHTML = `
      <section class="detail-topic">
        <p class="eyebrow">知识点</p>
        <h4>${escapeHtml(point.title || '未命名知识点')}</h4>
        <p>${escapeHtml(point.summary || '')}</p>
      </section>
      <section class="detail-citations">
        ${citations.map((item) => `
          <article class="citation-block">
            <div class="citation-title">
              <strong>${escapeHtml(item.marker || '依据')}</strong>
              <span>${escapeHtml(item.source_label || item.source_type || '来源')}</span>
            </div>
            <h5>${escapeHtml(item.title || item.source || '未命名来源')}</h5>
            <p class="citation-meta">
              ${escapeHtml(item.source || '')}
              ${item.page ? ` · 第${escapeHtml(String(item.page))}页` : ''}
            </p>
            <p>${escapeHtml(item.excerpt || '')}</p>
          </article>
        `).join('')}
      </section>
    `;
  }

  function findPoint(pointId) {
    for (const group of state.groups) {
      const found = (group.points || []).find((point) => point.id === pointId);
      if (found) return found;
    }
    return null;
  }

  function selectPoint(pointId, rerender = true) {
    state.selectedPoint = findPoint(pointId);
    renderDetail();
    if (rerender) renderGroups();
  }

  async function loadKnowledge() {
    setLoading();
    renderPathState();
    state.selectedPoint = null;
    try {
      const result = await apiPost('/api/knowledge/view', {
        topic_id: state.activeTopicId,
        dimension_id: state.activeDimensionId,
        source_type: state.activeSourceType,
        top_k: 4,
      });
      state.groups = result.groups || [];
      renderTopicList();
      renderGroups();
      renderDetail();
    } catch (err) {
      const groups = byId('knowledge-groups');
      const count = byId('knowledge-count');
      if (count) count.textContent = '读取失败';
      if (groups) groups.innerHTML = `<div class="empty-note error">${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  async function boot() {
    setLoading('正在读取知识主题...');
    try {
      const result = await apiGet('/api/knowledge/topics');
      state.topics = result.topics || [];
      state.topicGroups = result.topic_groups || {};
      state.dimensions = result.dimensions || [];
      state.sourceTypes = result.source_types || [{ id: 'all', name: '全部来源' }];
      if (!state.topics.find((topic) => topic.id === state.activeTopicId) && state.topics[0]) {
        state.activeTopicId = state.topics[0].id;
      }
      populateFilters();
      renderTopicList();
      await loadKnowledge();
    } catch (err) {
      const groups = byId('knowledge-groups');
      if (groups) groups.innerHTML = `<div class="empty-note error">${escapeHtml(err.message || String(err))}</div>`;
    }
  }

  document.addEventListener('click', (event) => {
    const topicButton = event.target.closest('.topic-btn');
    if (topicButton) {
      state.activeTopicId = topicButton.dataset.topic;
      loadKnowledge();
      return;
    }

    const pathButton = event.target.closest('.path-step');
    if (pathButton) {
      state.activeDimensionId = pathButton.dataset.dimension || 'all';
      const select = byId('knowledge-dimension');
      if (select) select.value = state.activeDimensionId;
      loadKnowledge();
      return;
    }

    const card = event.target.closest('.knowledge-card');
    if (card) {
      selectPoint(card.dataset.point);
      return;
    }

    const copy = event.target.closest('#copy-evidence');
    if (copy && state.selectedPoint) {
      const text = [
        state.selectedPoint.title,
        state.selectedPoint.summary,
        ...(state.selectedPoint.citations || []).map((item) => `${item.marker} ${item.source} ${item.page ? `第${item.page}页` : ''}\n${item.excerpt}`),
      ].join('\n\n');
      navigator.clipboard?.writeText(text);
      copy.textContent = '已复制';
      window.setTimeout(() => { copy.textContent = '复制'; }, 1200);
    }
  });

  document.addEventListener('input', (event) => {
    if (event.target.id === 'knowledge-search') {
      if (state.selectedPoint && !pointMatches(state.selectedPoint, event.target.value.trim())) {
        state.selectedPoint = null;
      }
      renderTopicList();
      renderGroups();
      renderDetail();
    }
  });

  document.addEventListener('change', (event) => {
    if (event.target.id === 'knowledge-dimension') {
      state.activeDimensionId = event.target.value;
      loadKnowledge();
    }
    if (event.target.id === 'knowledge-source') {
      state.activeSourceType = event.target.value;
      loadKnowledge();
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
