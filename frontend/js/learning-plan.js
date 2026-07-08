(() => {
  const oralUi = window.oralUi || {};
  const byId = oralUi.byId || ((id) => document.getElementById(id));
  const escapeHtml = oralUi.escapeHtml || ((text) => String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;'));

  const now = new Date();
  const state = {
    userId: new URLSearchParams(window.location.search).get('user_id')
      || localStorage.getItem('oralsage.learningProfile.userId')
      || 'demo',
    viewYear: now.getFullYear(),
    viewMonth: now.getMonth() + 1,
    selectedDate: dateKey(now.getFullYear(), now.getMonth() + 1, now.getDate()),
    monthData: null,
    profile: null,
    editingId: '',
    loading: false,
    recommendations: [],
    recommendationDate: '',
    recommendationLoading: false,
    assessmentPrompted: false,
  };

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  function dateKey(year, month, day) {
    return `${year}-${pad(month)}-${pad(day)}`;
  }

  function parseDateKey(key) {
    const [year, month, day] = String(key || '').split('-').map((part) => Number(part));
    return {
      year: Number.isFinite(year) ? year : now.getFullYear(),
      month: Number.isFinite(month) ? month : now.getMonth() + 1,
      day: Number.isFinite(day) ? day : now.getDate(),
    };
  }

  function daysInMonth(year, month) {
    return new Date(year, month, 0).getDate();
  }

  function todayKey() {
    const date = new Date();
    return dateKey(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  function addDays(key, delta) {
    const { year, month, day } = parseDateKey(key);
    const date = new Date(year, month - 1, day);
    date.setDate(date.getDate() + delta);
    return dateKey(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  function compactDate(key) {
    const { month, day } = parseDateKey(key);
    return `${month}/${day}`;
  }

  function humanDate(key) {
    const { year, month, day } = parseDateKey(key);
    const date = new Date(year, month - 1, day);
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      weekday: 'short',
    });
  }

  function selectedDateTitle(key) {
    const base = humanDate(key);
    if (key === todayKey()) return `${base}（今天）`;
    if (key === addDays(todayKey(), 1)) return `${base}（明天）`;
    return base;
  }

  function isPastDate(key) {
    return String(key || '') < todayKey();
  }

  function canEditSelectedDate() {
    return !isPastDate(state.selectedDate);
  }

  function kgText(item) {
    return Array.isArray(item?.kg_node_ids) ? item.kg_node_ids.join(', ') : '';
  }

  function hasActiveRecommendations() {
    return state.recommendationDate === state.selectedDate && state.recommendations.length > 0;
  }

  function clearRecommendations() {
    state.recommendations = [];
    state.recommendationDate = '';
  }

  async function apiGet(url) {
    const res = await fetch(url);
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  async function apiDelete(url) {
    const res = await fetch(url, { method: 'DELETE' });
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  function setStatus(message, tone = 'muted') {
    const node = byId('plan-status');
    if (!node) return;
    node.textContent = message || '';
    node.dataset.tone = tone;
  }

  async function loadProfile({ silent = false } = {}) {
    try {
      const body = await apiGet(`/api/learning/profile/${encodeURIComponent(state.userId)}`);
      state.profile = body.result?.profile || {};
      renderAssessmentCard();
      if (!silent && !state.assessmentPrompted && window.oralLearnerAssessment?.needsInitialAssessment(state.profile)) {
        state.assessmentPrompted = true;
        setTimeout(() => state.assessmentController?.open(), 120);
      }
    } catch (err) {
      if (!silent) setStatus(err.message || '画像加载失败', 'error');
    }
  }

  async function loadMonth({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    localStorage.setItem('oralsage.learningProfile.userId', state.userId);
    if (!silent) setStatus('正在同步计划...');
    try {
      const body = await apiGet(
        `/api/learning/plan/${encodeURIComponent(state.userId)}/month?year=${state.viewYear}&month=${state.viewMonth}`,
      );
      state.monthData = body.result || {};
      render();
      if (!silent) setStatus('');
    } catch (err) {
      setStatus(err.message || '计划加载失败', 'error');
    } finally {
      state.loading = false;
      if (window.lucide) window.lucide.createIcons();
    }
  }

  function setView(year, month, selectedDate = null) {
    const day = selectedDate ? parseDateKey(selectedDate).day : parseDateKey(state.selectedDate).day;
    const clampedDay = Math.min(day, daysInMonth(year, month));
    state.viewYear = year;
    state.viewMonth = month;
    state.selectedDate = selectedDate || dateKey(year, month, clampedDay);
    state.editingId = '';
    if (state.recommendationDate !== state.selectedDate) clearRecommendations();
    loadMonth();
  }

  function render() {
    renderSelectors();
    renderCalendar();
    renderTodo();
    renderAssessmentCard();
    refineAssessmentCardPresentation();
    renderRecommendations();
  }

  function renderSelectors() {
    const monthLabel = byId('month-label');
    if (monthLabel) monthLabel.textContent = `${state.viewYear}年 ${state.viewMonth}月`;
  }

  function renderCalendar() {
    const grid = byId('calendar-grid');
    if (!grid) return;

    const first = new Date(state.viewYear, state.viewMonth - 1, 1);
    const firstDay = first.getDay() || 7;
    const leading = firstDay - 1;
    const totalDays = daysInMonth(state.viewYear, state.viewMonth);
    const prevMonth = state.viewMonth === 1 ? 12 : state.viewMonth - 1;
    const prevYear = state.viewMonth === 1 ? state.viewYear - 1 : state.viewYear;
    const nextMonth = state.viewMonth === 12 ? 1 : state.viewMonth + 1;
    const nextYear = state.viewMonth === 12 ? state.viewYear + 1 : state.viewYear;
    const prevTotalDays = daysInMonth(prevYear, prevMonth);
    const cells = [];

    for (let i = 0; i < leading; i += 1) {
      const day = prevTotalDays - leading + i + 1;
      const key = dateKey(prevYear, prevMonth, day);
      cells.push(renderCalendarCell({ key, day, inMonth: false }));
    }

    for (let day = 1; day <= totalDays; day += 1) {
      const key = dateKey(state.viewYear, state.viewMonth, day);
      cells.push(renderCalendarCell({ key, day, inMonth: true }));
    }

    const trailing = Math.max(0, 42 - cells.length);
    for (let i = 1; i <= trailing; i += 1) {
      const key = dateKey(nextYear, nextMonth, i);
      cells.push(renderCalendarCell({ key, day: i, inMonth: false }));
    }

    grid.innerHTML = cells.join('');
  }

  function renderCalendarCell({ key, day, inMonth }) {
    const dayData = state.monthData?.days?.[key] || { total: 0, completed: 0, pending: 0 };
    const classes = ['calendar-cell', inMonth ? 'in-month' : 'out-month'];
    if (key === todayKey()) classes.push('today');
    if (key === state.selectedDate) classes.push('selected');
    if (inMonth && dayData.total && dayData.completed === dayData.total) classes.push('completed');
    if (inMonth && dayData.pending) classes.push('pending');
    const meta = inMonth && dayData.total ? `${dayData.completed}/${dayData.total}` : '';
    return `
      <button class="${classes.join(' ')}" type="button" data-date="${key}" aria-label="${escapeHtml(humanDate(key))}">
        <span class="date-tile">
          <span class="day-number">${day}</span>
          <span class="day-meta">${escapeHtml(meta)}</span>
        </span>
      </button>
    `;
  }

  function selectedDayItems() {
    const day = state.monthData?.days?.[state.selectedDate];
    return Array.isArray(day?.items) ? day.items.slice() : [];
  }

  function recommendationMeta(item) {
    return [
      item.linked_course_title ? `关联课程：${item.linked_course_title}` : '',
      kgText(item) ? `知识点：${kgText(item)}` : '',
      item.estimated_minutes ? `预计 ${item.estimated_minutes} 分钟` : '',
    ].filter(Boolean).join(' · ');
  }

  function renderRecommendationCard(item, editable) {
    const meta = recommendationMeta(item);
    const disabled = !editable ? ' disabled' : '';
    return `
      <article class="recommend-card">
        <div class="recommend-top">
          <div>
            <p class="recommend-title">${escapeHtml(item.title || '推荐计划')}</p>
            <p class="recommend-reason">${escapeHtml(item.reason || '')}</p>
          </div>
          <span class="recommend-chip">${escapeHtml(item.category || 'recommend')}</span>
        </div>
        ${meta ? `<div class="recommend-meta">${escapeHtml(meta)}</div>` : ''}
        <div class="recommend-tools">
          <button class="recommend-action" type="button" data-recommend-action="dismiss" data-recommend-id="${escapeHtml(item.recommendation_id || '')}"${disabled}>忽略</button>
          <button class="recommend-action primary" type="button" data-recommend-action="accept" data-recommend-id="${escapeHtml(item.recommendation_id || '')}"${disabled}>采纳</button>
        </div>
      </article>
    `;
  }

  function renderRecommendations() {
    const panel = byId('recommend-panel');
    const list = byId('recommend-list');
    const clearButton = byId('recommend-clear');
    const acceptAllButton = byId('recommend-accept-all');
    if (!panel || !list) return;

    const visible = hasActiveRecommendations();
    const editable = canEditSelectedDate();
    panel.hidden = !visible;
    if (clearButton) clearButton.hidden = !visible;
    if (acceptAllButton) acceptAllButton.disabled = !editable || !state.recommendations.length;

    if (!visible) {
      list.innerHTML = '';
      return;
    }
    if (!state.recommendations.length) {
      list.innerHTML = '<p class="recommend-empty">当前没有可推荐项。</p>';
      return;
    }
    list.innerHTML = state.recommendations.map((item) => renderRecommendationCard(item, editable)).join('');
  }

  function renderAssessmentCard() {
    const card = byId('assessment-card');
    const title = byId('assessment-card-title');
    const copy = byId('assessment-card-copy');
    const button = byId('assessment-open');
    if (!card || !title || !copy || !button) return;

    const profile = state.profile || {};
    const hasAssessment = Boolean(profile.assessment_source);
    const skipped = Boolean(profile.assessment_skipped);
    const learnerType = String(profile.learner_type || '').trim();
    const learnerSummary = String(profile.learner_type_summary || '').trim();
    const minutes = Number(profile.daily_available_minutes || 0);

    if (hasAssessment) {
      title.textContent = learnerType || '学习者画像已建立';
      copy.textContent = learnerSummary
        || `当前每日学习预算约 ${minutes || 45} 分钟，可继续基于此生成更贴合的计划。`;
      button.textContent = '重新测评';
      card.hidden = false;
      return;
    }

    if (skipped) {
      title.textContent = '你已跳过初始测评';
      copy.textContent = '当前系统使用默认画像生成计划。若想获得更精准推荐，可以随时补做测评。';
      button.textContent = '立即测评';
      card.hidden = false;
      return;
    }

    title.textContent = '先完成学习者测评';
    copy.textContent = '完成轻量测评后，系统会按你的时间预算和学习偏好生成更合适的学习计划。';
    button.textContent = '开始测评';
    card.hidden = false;
  }

  function learnerTypeDisplay(learnerType) {
    const mapping = {
      visual_structured_learner: {
        zh: '结构化学习者',
        en: 'Visual Structured Learner',
      },
      text_depth_learner: {
        zh: '深度理解学习者',
        en: 'Text Depth Learner',
      },
      case_driven_learner: {
        zh: '病例驱动学习者',
        en: 'Case-Driven Learner',
      },
      exam_oriented_learner: {
        zh: '考试导向学习者',
        en: 'Exam-Oriented Learner',
      },
      practice_driven_learner: {
        zh: '练习驱动学习者',
        en: 'Practice-Driven Learner',
      },
      steady_paced_learner: {
        zh: '稳步推进学习者',
        en: 'Steady-Paced Learner',
      },
    };
    return mapping[String(learnerType || '').trim()] || {
      zh: '学习者画像已建立',
      en: String(learnerType || '').trim() || 'Learner Profile Ready',
    };
  }

  function refineAssessmentCardPresentation() {
    const title = byId('assessment-card-title');
    const copy = byId('assessment-card-copy');
    const profile = state.profile || {};
    const learnerType = String(profile.learner_type || '').trim();
    if (!title || !copy || !learnerType) return;
    const display = learnerTypeDisplay(learnerType);
    title.innerHTML = `
      <span class="assessment-inline-title-cn">${escapeHtml(display.zh)}</span>
      <span class="assessment-inline-title-en">${escapeHtml(display.en)}</span>
    `;
    copy.textContent = String(profile.learner_type_summary || '').trim()
      || '你更适合结构清晰、分层明确的学习组织方式。建议结合思维导图梳理重点，并在计划推进中有的放矢地抓住核心知识点。';
  }

  function renderTodo() {
    const items = selectedDayItems();
    const completed = items.filter((item) => item.status === 'completed').length;
    const editable = canEditSelectedDate();
    const todoDate = byId('todo-date');
    const todoStats = byId('todo-stats');
    const planForm = byId('plan-form');
    const recommendButton = byId('recommend-button');

    if (todoDate) todoDate.textContent = selectedDateTitle(state.selectedDate);
    if (todoStats) todoStats.textContent = items.length ? `${completed}/${items.length}` : '0/0';
    if (planForm) {
      planForm.hidden = !editable;
      planForm.classList.toggle('disabled', !editable);
    }
    if (recommendButton) {
      recommendButton.disabled = !editable || state.recommendationLoading;
      recommendButton.textContent = state.recommendationLoading ? '推荐中...' : '系统推荐';
    }

    const list = byId('todo-list');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = `<p class="todo-empty">${escapeHtml(compactDate(state.selectedDate))} 还没有计划</p>`;
      return;
    }
    list.innerHTML = items.map((item) => renderTodoItem(item, editable)).join('');
  }

  function renderTodoItem(item, editable) {
    const completed = item.status === 'completed';
    const linkMeta = [
      item.linked_course_title ? `课程：${item.linked_course_title}` : '',
      kgText(item) ? `知识点：${kgText(item)}` : '',
      item.deferred_count ? `延期 ${item.deferred_count} 次` : '',
    ].filter(Boolean).join(' · ');
    const isEditing = editable && state.editingId === item.id;
    const checkControl = editable ? `
      <button class="todo-check-button${completed ? ' checked' : ''}" type="button" data-action="complete" data-id="${escapeHtml(item.id)}" data-completed="${completed ? 'false' : 'true'}" aria-label="${completed ? '标记为未完成' : '标记为完成'}">
        ${completed ? '<i data-lucide="check"></i>' : ''}
      </button>
    ` : `
      <span class="todo-check-button${completed ? ' checked' : ''}" aria-hidden="true">
        ${completed ? '<i data-lucide="check"></i>' : ''}
      </span>
    `;
    const tools = editable ? `
      <div class="todo-tools">
        <button class="todo-tool" type="button" data-action="edit" data-id="${escapeHtml(item.id)}" title="修改计划" aria-label="修改计划">
          <i data-lucide="pencil"></i>
        </button>
        <button class="todo-tool danger" type="button" data-action="delete" data-id="${escapeHtml(item.id)}" title="删除计划" aria-label="删除计划">
          <i data-lucide="trash-2"></i>
        </button>
      </div>
    ` : '';
    return `
      <article class="todo-item${completed ? ' completed' : ''}" data-id="${escapeHtml(item.id)}">
        <div class="todo-line">
          ${checkControl}
          <div class="todo-copy">
            <div class="todo-title">${escapeHtml(item.title)}</div>
            ${linkMeta ? `<div class="todo-meta">${escapeHtml(linkMeta)}</div>` : ''}
          </div>
          ${tools}
        </div>
        ${isEditing ? renderItemEditor(item) : ''}
      </article>
    `;
  }

  function renderItemEditor(item) {
    return `
      <div class="link-editor" data-editor-id="${escapeHtml(item.id)}">
        <input type="text" data-link-title value="${escapeHtml(item.title || '')}" placeholder="计划内容">
        <input type="text" data-link-course value="${escapeHtml(item.linked_course_title || '')}" placeholder="关联课程">
        <input type="text" data-link-kg value="${escapeHtml(kgText(item))}" placeholder="知识点 ID，逗号分隔">
        <div class="link-editor-actions">
          <button class="link-cancel" type="button" data-action="cancel-edit" data-id="${escapeHtml(item.id)}">取消</button>
          <button class="link-save" type="button" data-action="save-edit" data-id="${escapeHtml(item.id)}">保存</button>
        </div>
      </div>
    `;
  }

  function recommendationPayload(item) {
    return {
      user_id: state.userId,
      plan_date: state.selectedDate,
      title: String(item.title || '').trim(),
      notes: item.notes || item.reason || null,
      linked_course_id: item.linked_course_id || null,
      linked_course_title: item.linked_course_title || null,
      kg_node_ids: Array.isArray(item.kg_node_ids) ? item.kg_node_ids : [],
    };
  }

  async function requestRecommendations() {
    if (!canEditSelectedDate()) {
      setStatus('过去的日期只支持查看', 'error');
      return;
    }
    if (!state.profile) {
      await loadProfile({ silent: true });
    }
    state.recommendationLoading = true;
    renderTodo();
    try {
      const body = await apiPost('/api/learning/plan/recommend', {
        user_id: state.userId,
        plan_date: state.selectedDate,
        max_items: 4,
      });
      const result = body.result || {};
      state.recommendations = Array.isArray(result.recommendations) ? result.recommendations : [];
      state.recommendationDate = state.selectedDate;
      if (state.recommendations.length) {
        setStatus('已生成推荐计划，可以逐条采纳。');
      } else {
        clearRecommendations();
        setStatus('当前没有可推荐的计划项。');
      }
      renderRecommendations();
    } catch (err) {
      setStatus(err.message || '推荐生成失败', 'error');
    } finally {
      state.recommendationLoading = false;
      renderTodo();
      if (window.lucide) window.lucide.createIcons();
    }
  }

  async function acceptRecommendation(recommendationId) {
    const item = state.recommendations.find((entry) => entry.recommendation_id === recommendationId);
    if (!item) return;
    await apiPost('/api/learning/plan/items', recommendationPayload(item));
    state.recommendations = state.recommendations.filter((entry) => entry.recommendation_id !== recommendationId);
    if (!state.recommendations.length) clearRecommendations();
    setStatus('已采纳一条推荐计划。');
    await loadMonth({ silent: true });
    renderRecommendations();
  }

  async function acceptAllRecommendations() {
    const pending = state.recommendations.slice();
    if (!pending.length) return;
    for (const item of pending) {
      await apiPost('/api/learning/plan/items', recommendationPayload(item));
    }
    clearRecommendations();
    setStatus('已采纳全部推荐计划。');
    await loadMonth({ silent: true });
    renderRecommendations();
  }

  function dismissRecommendation(recommendationId) {
    state.recommendations = state.recommendations.filter((entry) => entry.recommendation_id !== recommendationId);
    if (!state.recommendations.length) {
      clearRecommendations();
      setStatus('已清空推荐预览。');
    } else {
      setStatus('已忽略一条推荐。');
    }
    renderRecommendations();
  }

  function initAssessmentController() {
    if (!window.oralLearnerAssessment) return;
    state.assessmentController = window.oralLearnerAssessment.createController({
      getUserId: () => state.userId,
      title: '学习者测评',
      onComplete: async (result) => {
        state.profile = result.profile_after || state.profile || {};
        renderAssessmentCard();
        setStatus('已完成测评，正在按新的画像刷新计划推荐。');
        await loadMonth({ silent: true });
        if (canEditSelectedDate()) {
          await requestRecommendations();
        }
      },
      onSkip: async (result) => {
        state.profile = result.profile_after || state.profile || {};
        renderAssessmentCard();
        setStatus('已跳过测评，当前将按默认画像生成计划。');
        await loadMonth({ silent: true });
      },
    });
  }

  async function createItem(title) {
    await apiPost('/api/learning/plan/items', {
      user_id: state.userId,
      plan_date: state.selectedDate,
      title,
    });
    if (byId('plan-title')) byId('plan-title').value = '';
    setStatus('已添加计划。');
    await loadMonth({ silent: true });
  }

  async function completeItem(itemId, completed) {
    const body = await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/complete`, {
      user_id: state.userId,
      completed,
    });
    const memoryUpdated = body.result?.learner_memory_update?.updated;
    setStatus(
      completed
        ? (memoryUpdated ? '已完成，并写入学习记忆。' : '已完成。')
        : '已恢复为未完成。',
    );
    await loadMonth({ silent: true });
  }

  async function deleteItem(itemId) {
    await apiDelete(`/api/learning/plan/items/${encodeURIComponent(itemId)}?user_id=${encodeURIComponent(state.userId)}`);
    setStatus('已删除计划。');
    await loadMonth({ silent: true });
  }

  async function saveItem(itemId) {
    const editor = document.querySelector(`[data-editor-id="${CSS.escape(itemId)}"]`);
    if (!editor) return;
    const title = editor.querySelector('[data-link-title]')?.value || '';
    const course = editor.querySelector('[data-link-course]')?.value || '';
    const kgRaw = editor.querySelector('[data-link-kg]')?.value || '';
    const kgIds = kgRaw
      .split(/[\n,，]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!title.trim()) {
      setStatus('计划内容不能为空', 'error');
      return;
    }
    await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/update`, {
      user_id: state.userId,
      title: title.trim(),
      linked_course_title: course.trim() || null,
      kg_node_ids: kgIds,
    });
    state.editingId = '';
    setStatus('已更新计划。');
    await loadMonth({ silent: true });
  }

  function bindEvents() {
    byId('prev-month')?.addEventListener('click', () => {
      const month = state.viewMonth === 1 ? 12 : state.viewMonth - 1;
      const year = state.viewMonth === 1 ? state.viewYear - 1 : state.viewYear;
      setView(year, month);
    });

    byId('next-month')?.addEventListener('click', () => {
      const month = state.viewMonth === 12 ? 1 : state.viewMonth + 1;
      const year = state.viewMonth === 12 ? state.viewYear + 1 : state.viewYear;
      setView(year, month);
    });

    byId('today-button')?.addEventListener('click', () => {
      const today = todayKey();
      const { year, month } = parseDateKey(today);
      setView(year, month, today);
    });

    byId('recommend-button')?.addEventListener('click', async () => {
      await requestRecommendations();
    });

    byId('recommend-clear')?.addEventListener('click', () => {
      clearRecommendations();
      renderRecommendations();
      setStatus('已清空推荐预览。');
    });

    byId('recommend-accept-all')?.addEventListener('click', async () => {
      try {
        await acceptAllRecommendations();
      } catch (err) {
        setStatus(err.message || '批量采纳失败', 'error');
      }
    });

    byId('assessment-open')?.addEventListener('click', () => {
      state.assessmentController?.open({ resetAnswers: true });
    });

    byId('recommend-list')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-recommend-action]');
      if (!button) return;
      const action = button.getAttribute('data-recommend-action');
      const recommendationId = button.getAttribute('data-recommend-id') || '';
      try {
        if (action === 'accept') {
          await acceptRecommendation(recommendationId);
        } else if (action === 'dismiss') {
          dismissRecommendation(recommendationId);
        }
      } catch (err) {
        setStatus(err.message || '推荐操作失败', 'error');
      }
    });

    byId('calendar-grid')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-date]');
      if (!button) return;
      const key = button.getAttribute('data-date') || state.selectedDate;
      const { year, month } = parseDateKey(key);
      if (year !== state.viewYear || month !== state.viewMonth) {
        setView(year, month, key);
        return;
      }
      state.selectedDate = key;
      state.editingId = '';
      if (state.recommendationDate !== state.selectedDate) clearRecommendations();
      render();
      if (window.lucide) window.lucide.createIcons();
    });

    byId('plan-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!canEditSelectedDate()) {
        setStatus('过去的日期只支持查看', 'error');
        return;
      }
      const input = byId('plan-title');
      const title = String(input?.value || '').trim();
      if (!title) {
        setStatus('请先写下计划内容', 'error');
        return;
      }
      try {
        await createItem(title);
      } catch (err) {
        setStatus(err.message || '添加失败', 'error');
      }
    });

    byId('todo-list')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const action = button.getAttribute('data-action');
      const itemId = button.getAttribute('data-id');
      try {
        if (!canEditSelectedDate()) {
          setStatus('过去的日期只支持查看', 'error');
          return;
        }
        if (action === 'complete') {
          await completeItem(itemId, button.getAttribute('data-completed') === 'true');
        } else if (action === 'delete') {
          if (window.confirm('删除这项计划？')) await deleteItem(itemId);
        } else if (action === 'edit') {
          state.editingId = state.editingId === itemId ? '' : itemId;
          renderTodo();
          if (window.lucide) window.lucide.createIcons();
        } else if (action === 'cancel-edit') {
          state.editingId = '';
          renderTodo();
          if (window.lucide) window.lucide.createIcons();
        } else if (action === 'save-edit') {
          await saveItem(itemId);
        }
      } catch (err) {
        setStatus(err.message || '操作失败', 'error');
      }
    });
  }

  initAssessmentController();
  bindEvents();
  loadProfile();
  loadMonth();
})();
