(() => {
  const oralUi = window.oralUi || {};
  const byId = oralUi.byId || ((id) => document.getElementById(id));
  const escapeHtml = oralUi.escapeHtml || ((text) => String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;'));

  function removeWeixinTopNavLink() {
    document
      .querySelectorAll('#top-nav a.top-nav-link[href="/pages/weixin-account.html"]')
      .forEach((node) => node.remove());
  }

  function installTopNavCleanup() {
    removeWeixinTopNavLink();
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', removeWeixinTopNavLink, { once: true });
    }
    [0, 100, 500, 1500].forEach((delay) => {
      window.setTimeout(removeWeixinTopNavLink, delay);
    });
    if (window.MutationObserver && document.body) {
      const observer = new MutationObserver(removeWeixinTopNavLink);
      observer.observe(document.body, { childList: true, subtree: true });
      window.setTimeout(() => observer.disconnect(), 2500);
    }
  }

  installTopNavCleanup();

  const now = new Date();
  const urlParams = new URLSearchParams(window.location.search);
  const state = {
    userId: urlParams.get('user_id')
      || localStorage.getItem('oralsage.learningProfile.userId')
      || 'demo',
    planDraftId: urlParams.get('plan_draft_id') || '',
    _draftLoaded: false,
    viewYear: now.getFullYear(),
    viewMonth: now.getMonth() + 1,
    selectedDate: dateKey(now.getFullYear(), now.getMonth() + 1, now.getDate()),
    monthData: null,
    profile: null,
    assessmentState: null,
    editingId: '',
    loading: false,
    recommendations: [],
    recommendationDate: '',
    recommendationLoading: false,
    assessmentPrompted: false,
    toastTimer: 0,
    draftBaseDate: '',
    _draftIdToFormalId: {},
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

  const TASK_TYPE_LABELS = Object.freeze({
    continue_lesson: '继续学习',
    review_weakness: '薄弱点复习',
    review_mastery: '掌握度复习',
    catch_up: '补学任务',
    practice: '练习巩固',
    review_summary: '复习总结',
    learn_chapter: '章节学习',
    review_chapter: '章节复习',
    basic_practice: '基础练习',
    advanced_practice: '进阶练习',
    stage_exam: '阶段测验',
    topic_learning: '主题学习',
    custom: '学习任务',
    recommend: '系统推荐',
  });

  function taskTypeLabel(value) {
    const key = String(value || '').trim();
    if (!key) return '学习任务';
    if (TASK_TYPE_LABELS[key]) return TASK_TYPE_LABELS[key];
    return /^[a-z0-9_-]+$/i.test(key) ? '学习任务' : key;
  }

  const INTERNAL_KG_REFERENCE_RE = /(?:kg:\/\/|core:)[^\s，。；;、：:！？!?（）()\[\]【】《》<>"']+/gi;
  const HAS_INTERNAL_KG_REFERENCE_RE = /(?:kg:\/\/|core:)[^\s，。；;、：:！？!?（）()\[\]【】《》<>"']+/i;

  function safeDisplayText(value) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return HAS_INTERNAL_KG_REFERENCE_RE.test(text) ? '' : text;
  }

  function planKnowledgeDisplayNames(item) {
    const source = item && typeof item === 'object' ? item : {};
    const scope = source.learning_scope && typeof source.learning_scope === 'object'
      ? source.learning_scope
      : {};
    const names = {};
    [source.kg_node_names, scope.kg_node_names].forEach((mapping) => {
      if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) return;
      Object.keys(mapping).forEach((nodeId) => {
        const name = safeDisplayText(mapping[nodeId]);
        if (nodeId && name && !names[nodeId]) names[nodeId] = name;
      });
    });
    return names;
  }

  function planDisplayLabel(item) {
    const source = item && typeof item === 'object' ? item : {};
    const scope = source.learning_scope && typeof source.learning_scope === 'object'
      ? source.learning_scope
      : {};
    const names = planKnowledgeDisplayNames(source);
    const nodeIds = [];
    [source.kg_node_ids, scope.kg_node_ids].forEach((values) => {
      if (!Array.isArray(values)) return;
      values.forEach((value) => {
        const nodeId = String(value || '').trim();
        if (nodeId && nodeIds.indexOf(nodeId) === -1) nodeIds.push(nodeId);
      });
    });
    for (const nodeId of nodeIds) {
      if (names[nodeId]) return names[nodeId];
    }
    const mappedNames = Object.values(names);
    if (mappedNames.length) return mappedNames[0];
    const candidates = [
      source.learning_target,
      scope.chapter_title,
      source.linked_course_title,
      scope.book_title,
    ];
    for (const candidate of candidates) {
      const text = safeDisplayText(candidate);
      if (text) return text;
    }
    return '相关知识点';
  }

  function publicPlanText(value, item, fallback) {
    let text = String(value || '').replace(/\s+/g, ' ').trim();
    if (!text || !HAS_INTERNAL_KG_REFERENCE_RE.test(text)) return text;
    const names = planKnowledgeDisplayNames(item);
    Object.keys(names)
      .sort((a, b) => b.length - a.length)
      .forEach((nodeId) => {
        text = text.split(nodeId).join(names[nodeId]);
      });
    return text.replace(
      INTERNAL_KG_REFERENCE_RE,
      fallback || planDisplayLabel(item),
    ).trim();
  }

  function normalizePlanDisplayItem(item) {
    const source = item && typeof item === 'object' ? item : {};
    const normalized = { ...source };
    const fallback = planDisplayLabel(source);
    normalized.title = publicPlanText(source.title, source, fallback) || '学习任务';
    ['learning_target', 'reason', 'notes'].forEach((field) => {
      if (Object.prototype.hasOwnProperty.call(source, field)) {
        normalized[field] = publicPlanText(source[field], source, fallback) || null;
      }
    });
    if (Object.prototype.hasOwnProperty.call(source, 'linked_course_title')) {
      const scope = source.learning_scope && typeof source.learning_scope === 'object'
        ? source.learning_scope
        : {};
      const courseFallback = safeDisplayText(scope.book_title)
        || safeDisplayText(scope.chapter_title)
        || '相关课程';
      normalized.linked_course_title = publicPlanText(
        source.linked_course_title,
        source,
        courseFallback,
      ) || null;
    }
    return normalized;
  }

  function buildStartLearningUrl(itemId) {
    return `/pages/master-chat.html?plan_item_id=${encodeURIComponent(itemId)}&user_id=${encodeURIComponent(state.userId || 'demo')}`;
  }

  function hasActiveRecommendations() {
    return state.recommendationDate === state.selectedDate && state.recommendations.length > 0;
  }

  function clearRecommendations() {
    state.recommendations = [];
    state.recommendationDate = '';
  }

  function mapDraftItemsToRecommendations(draftId, items) {
    return items.map((item, i) => {
      const scope = item.learning_scope || {};
      const rec = {
        recommendation_id: `${draftId}:${i}`,
        _draft_index: i,
        title: item.learning_target || item.title || '',
        task_type: item.task_type || 'topic_learning',
        learning_target: item.learning_target || '',
        reason: item.reason || '',
        estimated_minutes: item.estimated_minutes || 0,
        kg_node_ids: Array.isArray(scope.kg_node_ids) ? scope.kg_node_ids : [],
        learning_scope: item.learning_scope || null,
        linked_course_title: scope.book_title || scope.chapter_title || null,
        linked_course_id: item.linked_course_id || item.session_id || null,
        category: item.task_type || 'recommend',
        evidence_source: item.evidence_source || null,
        day_offset: typeof item.day_offset === 'number' ? item.day_offset : undefined,
        dependency: item.dependency != null ? item.dependency : null,
        practice_level: item.practice_level || null,
        session_id: item.session_id || null,
      };
      return normalizePlanDisplayItem(rec);
    });
  }

  async function loadPlanDraft(draftId) {
    if (!draftId) return;
    try {
      setStatus('正在加载计划草案...');
      const body = await apiGet(
        `/api/learning/plan/drafts/${encodeURIComponent(draftId)}?user_id=${encodeURIComponent(state.userId)}`,
      );
      const draft = body.result || {};
      const payload = draft.payload || {};
      const items = Array.isArray(payload.items) ? payload.items : [];
      if (items.length) {
        state.draftBaseDate = state.selectedDate;
        state._draftIdToFormalId = {};
        state.recommendations = mapDraftItemsToRecommendations(draftId, items);
        state.recommendationDate = state.selectedDate;
        state._draftLoaded = true;
        renderRecommendations();
        setStatus('');
        showPlanToast('已加载聊天生成的计划草案');
      } else {
        state._draftLoaded = true;
        setStatus('计划草案中没有任务', 'muted');
      }
    } catch (err) {
      setStatus(err.message || '计划草案加载失败', 'error');
      showPlanToast('这份计划已过期或不可用，请重新生成');
    }
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

  async function syncCurrentUser() {
    try {
      const body = await apiGet('/api/auth/me');
      const userId = String(body?.user?.user_id || body?.user?.username || '').trim();
      if (body?.authenticated && userId) {
        state.userId = userId;
        localStorage.setItem('oralsage.learningProfile.userId', userId);
      }
    } catch (_err) {
      // Anonymous/demo mode is still supported for local smoke tests.
    }
  }

  function setStatus(message, tone = 'muted') {
    const node = byId('plan-status');
    if (!node) return;
    node.textContent = message || '';
    node.dataset.tone = tone;
  }

  function showPlanToast(message) {
    const text = String(message || '').trim();
    if (!text) return;
    const book = document.querySelector('.todo-book');
    if (!book) return;
    let toast = byId('todo-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'todo-toast';
      toast.className = 'todo-toast';
      toast.setAttribute('role', 'status');
      toast.setAttribute('aria-live', 'polite');
      book.appendChild(toast);
    }
    window.clearTimeout(state.toastTimer);
    toast.textContent = text;
    toast.classList.remove('show');
    void toast.offsetWidth;
    toast.classList.add('show');
    state.toastTimer = window.setTimeout(() => {
      toast.classList.remove('show');
      state.toastTimer = 0;
    }, 1800);
  }

  async function loadProfile({ silent = false } = {}) {
    try {
      const body = await apiGet(`/api/learning/profile/${encodeURIComponent(state.userId)}`);
      state.profile = body.result?.profile || {};
      state.assessmentState = body.result?.assessment_state || null;
      renderAssessmentCard();
      if (!silent && !state.assessmentPrompted && window.oralLearnerAssessment?.needsInitialAssessment(state.profile, state.assessmentState)) {
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
    const displayItem = normalizePlanDisplayItem(item);
    return [
      displayItem.linked_course_title ? `关联课程：${displayItem.linked_course_title}` : '',
      displayItem.estimated_minutes ? `预计 ${displayItem.estimated_minutes} 分钟` : '',
    ].filter(Boolean).join(' · ');
  }

  function renderRecommendationCard(item, editable) {
    const displayItem = normalizePlanDisplayItem(item);
    const meta = recommendationMeta(displayItem);
    const disabled = !editable ? ' disabled' : '';
    return `
      <article class="recommend-card">
        <div class="recommend-top">
          <div>
            <p class="recommend-title">${escapeHtml(displayItem.title || '推荐计划')}</p>
            <p class="recommend-reason">${escapeHtml(displayItem.reason || '')}</p>
          </div>
          <span class="recommend-chip">${escapeHtml(taskTypeLabel(displayItem.category || displayItem.task_type))}</span>
        </div>
        ${meta ? `<div class="recommend-meta">${escapeHtml(meta)}</div>` : ''}
        <div class="recommend-tools">
          <button class="recommend-action" type="button" data-recommend-action="dismiss" data-recommend-id="${escapeHtml(displayItem.recommendation_id || '')}"${disabled}>忽略</button>
          <button class="recommend-action primary" type="button" data-recommend-action="accept" data-recommend-id="${escapeHtml(displayItem.recommendation_id || '')}"${disabled}>采纳</button>
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

    const heading = panel.querySelector('.recommend-head h3');
    if (heading) {
      heading.textContent = state._draftLoaded ? '聊天计划草案' : '系统推荐';
    }

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
    const assessmentState = state.assessmentState || {};
    const hasAssessment = typeof assessmentState.has_assessment_record === 'boolean'
      ? assessmentState.has_assessment_record
      : Boolean(profile.assessment_source);
    const skipped = typeof assessmentState.assessment_skipped === 'boolean'
      ? assessmentState.assessment_skipped
      : Boolean(profile.assessment_skipped);
    const needs = typeof assessmentState.needs_initial_assessment === 'boolean'
      ? assessmentState.needs_initial_assessment
      : (!hasAssessment && !skipped);
    const hasHistoryWithoutAssessment = assessmentState.reason === 'history_without_assessment';
    const learnerType = String(profile.learner_type || '').trim();
    const learnerSummary = String(profile.learner_type_summary || '').trim();
    const minutes = Number(profile.daily_available_minutes || 0);

    if (skipped) {
      title.textContent = '你已跳过初始测评';
      copy.textContent = '当前系统使用默认画像生成计划。若想获得更精准推荐，可以随时补做测评。';
      button.textContent = '立即测评';
      card.hidden = false;
      return;
    }

    if (hasAssessment) {
      title.textContent = learnerType || '学习者画像已建立';
      copy.textContent = learnerSummary
        || `当前每日学习预算约 ${minutes || 45} 分钟，可继续基于此生成更贴合的计划。`;
      button.textContent = '重新测评';
      card.hidden = false;
      return;
    }

    if (hasHistoryWithoutAssessment) {
      title.textContent = '学习画像已建立';
      copy.textContent = '已检测到历史学习数据，当前不会自动重新初始化；如需校准计划偏好，可以手动重新测评。';
      button.textContent = '重新测评';
      card.hidden = false;
      return;
    }

    if (!needs) {
      title.textContent = '学习者画像已建立';
      copy.textContent = `当前每日学习预算约 ${minutes || 45} 分钟，可继续基于此生成学习计划。`;
      button.textContent = '重新测评';
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
    };
  }

  function refineAssessmentCardPresentation() {
    const title = byId('assessment-card-title');
    const copy = byId('assessment-card-copy');
    const profile = state.profile || {};
    const learnerType = String(profile.learner_type || '').trim();
    if (!title || !copy || !learnerType) return;
    const display = learnerTypeDisplay(learnerType);
    title.textContent = display.zh;
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
      const draftBlocked = Boolean(state.planDraftId);
      recommendButton.disabled = draftBlocked || !editable || state.recommendationLoading;
      recommendButton.textContent = state.recommendationLoading ? '推荐中...' : (draftBlocked ? '已加载聊天草案' : '系统推荐');
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
    const displayItem = normalizePlanDisplayItem(item);
    const completed = displayItem.status === 'completed';
    const linkMeta = [
      displayItem.linked_course_title ? `课程：${displayItem.linked_course_title}` : '',
      displayItem.deferred_count ? `延期 ${displayItem.deferred_count} 次` : '',
    ].filter(Boolean).join(' · ');
    const isEditing = editable && state.editingId === displayItem.id;
    const checkControl = editable ? `
      <button class="todo-check-button${completed ? ' checked' : ''}" type="button" data-action="complete" data-id="${escapeHtml(displayItem.id)}" data-completed="${completed ? 'false' : 'true'}" aria-label="${completed ? '标记为未完成' : '标记为完成'}">
        ${completed ? '<i data-lucide="check"></i>' : ''}
      </button>
    ` : `
      <span class="todo-check-button${completed ? ' checked' : ''}" aria-hidden="true">
        ${completed ? '<i data-lucide="check"></i>' : ''}
      </span>
    `;
    const startLearningLink = !completed
      ? `<a class="todo-tool start-learning-link" href="${buildStartLearningUrl(displayItem.id)}" title="开始学习" aria-label="开始学习"><i data-lucide="play-circle"></i></a>`
      : '';
    const tools = editable ? `
      <div class="todo-tools">
        <button class="todo-tool" type="button" data-action="edit" data-id="${escapeHtml(displayItem.id)}" title="修改计划" aria-label="修改计划">
          <i data-lucide="pencil"></i>
        </button>
        <button class="todo-tool danger" type="button" data-action="delete" data-id="${escapeHtml(displayItem.id)}" title="删除计划" aria-label="删除计划">
          <i data-lucide="trash-2"></i>
        </button>
      </div>
    ` : '';
    return `
      <article class="todo-item${completed ? ' completed' : ''}" data-id="${escapeHtml(displayItem.id)}">
        <div class="todo-line">
          ${checkControl}
          <div class="todo-copy">
            <div class="todo-title">${escapeHtml(displayItem.title)}</div>
            ${linkMeta ? `<div class="todo-meta">${escapeHtml(linkMeta)}</div>` : ''}
          </div>
          ${startLearningLink}
          ${tools}
        </div>
        ${isEditing ? renderItemEditor(displayItem) : ''}
      </article>
    `;
  }

  function renderItemEditor(item) {
    const displayItem = normalizePlanDisplayItem(item);
    return `
      <div class="link-editor" data-editor-id="${escapeHtml(displayItem.id)}">
        <input type="text" data-link-title value="${escapeHtml(displayItem.title || '')}" placeholder="计划内容">
        <input type="text" data-link-course value="${escapeHtml(displayItem.linked_course_title || '')}" placeholder="关联课程">
        <div class="link-editor-actions">
          <button class="link-cancel" type="button" data-action="cancel-edit" data-id="${escapeHtml(displayItem.id)}">取消</button>
          <button class="link-save" type="button" data-action="save-edit" data-id="${escapeHtml(displayItem.id)}">保存</button>
        </div>
      </div>
    `;
  }

  function recommendationPayload(item) {
    const displayItem = normalizePlanDisplayItem(item);
    const planDate = (displayItem.day_offset != null && state.draftBaseDate)
      ? addDays(state.draftBaseDate, displayItem.day_offset)
      : state.selectedDate;
    const payload = {
      user_id: state.userId,
      plan_date: planDate,
      title: String(displayItem.title || '').trim(),
      notes: displayItem.notes || displayItem.reason || null,
      linked_course_id: displayItem.linked_course_id || null,
      linked_course_title: displayItem.linked_course_title || null,
      task_type: displayItem.task_type || displayItem.category || 'topic_learning',
      learning_target: displayItem.learning_target || null,
      kg_node_ids: Array.isArray(displayItem.kg_node_ids) ? displayItem.kg_node_ids : [],
    };
    if (displayItem.learning_scope) {
      payload.learning_scope = displayItem.learning_scope;
    }
    const formalDep = _resolveFormalDependency(displayItem);
    if (formalDep) {
      payload.dependency = formalDep;
    }
    if (displayItem.practice_level) {
      payload.practice_level = displayItem.practice_level;
    }
    if (displayItem.session_id) {
      payload.session_id = displayItem.session_id;
    }
    return payload;
  }

  function _resolveFormalDependency(item) {
    if (item.dependency == null) return null;
    const depIdx = typeof item.dependency === 'number' ? item.dependency : parseInt(String(item.dependency), 10);
    if (!Number.isFinite(depIdx)) return null;
    const depRecommendationId = `${state.planDraftId}:${depIdx}`;
    const mapped = state._draftIdToFormalId[depRecommendationId];
    if (mapped) return mapped;
    return null;
  }

  // ── Injectable apiPost for Node test harness ─────────────────────
  let _apiPostOverride = null;

  // ── Dependency graph validator (fail-closed) ────────────────────
  function _validateDraftDependencyGraph(items) {
    const errors = [];
    const byIndex = new Map();
    for (const entry of items) {
      const idx = entry._draft_index;
      if (idx != null) {
        if (!Number.isInteger(idx) || idx < 0) {
          errors.push(`_draft_index 无效: ${JSON.stringify(idx)}`);
        } else if (byIndex.has(idx)) {
          errors.push(`_draft_index 重复: ${idx}`);
        } else {
          byIndex.set(idx, entry);
        }
      }
    }
    for (const entry of items) {
      const idx = entry._draft_index;
      if (idx == null || !Number.isInteger(idx) || idx < 0) continue;
      const dep = entry.dependency;
      if (dep == null) continue;
      const depIdx = typeof dep === 'number' ? dep : parseInt(String(dep), 10);
      if (!Number.isInteger(depIdx)) {
        errors.push(`item ${idx} dependency 不是整数: ${JSON.stringify(dep)}`);
        continue;
      }
      if (depIdx < 0) {
        errors.push(`item ${idx} dependency 为负数: ${depIdx}`);
        continue;
      }
      if (depIdx === idx) {
        errors.push(`item ${idx} 自依赖`);
        continue;
      }
      if (depIdx > idx) {
        errors.push(`item ${idx} 前向依赖 item ${depIdx}`);
        continue;
      }
      if (!byIndex.has(depIdx)) {
        errors.push(`item ${idx} 依赖缺失索引 ${depIdx}`);
        continue;
      }
    }
    // Cycle detection (DFS tricolor)
    if (errors.length === 0) {
      const WHITE = 0; const GRAY = 1; const BLACK = 2;
      const color = new Map();
      for (const idx of byIndex.keys()) color.set(idx, WHITE);
      function dfs(v) {
        color.set(v, GRAY);
        const entry = byIndex.get(v);
        if (entry && entry.dependency != null) {
          const dIdx = typeof entry.dependency === 'number' ? entry.dependency : parseInt(String(entry.dependency), 10);
          if (Number.isInteger(dIdx) && byIndex.has(dIdx)) {
            const dc = color.get(dIdx);
            if (dc === GRAY) {
              errors.push(`检测到循环依赖，涉及 item ${v}`);
              return;
            }
            if (dc === WHITE) dfs(dIdx);
          }
        }
        color.set(v, BLACK);
      }
      for (const idx of byIndex.keys()) {
        if (color.get(idx) === WHITE) dfs(idx);
        if (errors.length > 0) break;
      }
    }
    return errors;
  }

  // ── Unified draft adoption helper ────────────────────────────────
  async function _adoptDraftItem(item) {
    const recId = item.recommendation_id;
    if (state._draftIdToFormalId[recId]) return state._draftIdToFormalId[recId];
    const body = await (_apiPostOverride || apiPost)('/api/learning/plan/items', recommendationPayload(item));
    const createdId = body?.result?.item?.id || body?.result?.id || null;
    if (!createdId) {
      throw new Error('API 未返回有效的计划项 ID，后续任务无法创建');
    }
    state._draftIdToFormalId[recId] = createdId;
    return createdId;
  }

  async function requestRecommendations() {
    if (!canEditSelectedDate()) {
      setStatus('过去的日期只支持查看', 'error');
      return;
    }
    if (state.planDraftId) {
      showPlanToast('当前页面已加载聊天计划草案，如需系统推荐请重新打开计划页');
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
      state.recommendations = Array.isArray(result.recommendations)
        ? result.recommendations.map((item) => normalizePlanDisplayItem(item))
        : [];
      state.recommendationDate = state.selectedDate;
      if (state.recommendations.length) {
        setStatus('');
        showPlanToast('已生成推荐计划，可以逐条采纳');
      } else {
        clearRecommendations();
        setStatus('');
        showPlanToast('当前没有可推荐的计划项');
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
    const target = state.recommendations.find((entry) => entry.recommendation_id === recommendationId);
    if (!target) return;

    // ── Non-draft: simple single-item adoption ──────────────────
    if (target._draft_index == null) {
      try {
        await (_apiPostOverride || apiPost)('/api/learning/plan/items', recommendationPayload(target));
      } catch (err) {
        setStatus(err.message || '采纳失败', 'error');
        return;
      }
      state.recommendations = state.recommendations.filter((entry) => entry.recommendation_id !== recommendationId);
      if (!state.recommendations.length) clearRecommendations();
      setStatus('');
      showPlanToast('已采纳一条推荐计划');
      await loadMonth({ silent: true });
      renderRecommendations();
      return;
    }

    // ── Draft: validate transitive dependency chain first ──────
    const subgraph = [];
    const seen = new Set();
    const errors = [];
    function collect(entry) {
      const idx = entry._draft_index;
      if (idx == null || seen.has(idx)) return;
      if (entry.dependency != null) {
        const dIdx = typeof entry.dependency === 'number' ? entry.dependency : parseInt(String(entry.dependency), 10);
        if (!Number.isInteger(dIdx) || dIdx < 0) {
          errors.push(`item ${idx} dependency 无效: ${JSON.stringify(entry.dependency)}`);
          return;
        }
        if (dIdx >= idx) {
          errors.push(`item ${idx} 前向/自依赖 item ${dIdx}`);
          return;
        }
        const depRecId = `${state.planDraftId}:${dIdx}`;
        if (!state._draftIdToFormalId[depRecId]) {
          const depEntry = state.recommendations.find((e) => e.recommendation_id === depRecId);
          if (!depEntry) {
            errors.push(`item ${idx} 依赖缺失索引 ${dIdx}`);
            return;
          }
          collect(depEntry);
        }
      }
      seen.add(idx);
      subgraph.push(entry);
    }
    collect(target);

    if (errors.length) {
      setStatus(`依赖校验失败：${errors[0]}`, 'error');
      return;
    }

    // Adopt in topological order via the unified helper
    const ordered = _topologicalOrder(subgraph);
    for (const entry of ordered) {
      const recId = entry.recommendation_id;
      if (state._draftIdToFormalId[recId]) continue;
      try {
        await _adoptDraftItem(entry);
      } catch (err) {
        setStatus(err.message || '采纳失败', 'error');
        return;
      }
    }

    // Remove adopted items from recommendations
    const adoptedIds = new Set(ordered.map((e) => e.recommendation_id));
    state.recommendations = state.recommendations.filter((entry) => !adoptedIds.has(entry.recommendation_id));
    if (!state.recommendations.length) clearRecommendations();
    setStatus('');
    showPlanToast('已采纳推荐计划');
    await loadMonth({ silent: true });
    renderRecommendations();
  }

  async function acceptAllRecommendations() {
    const items = state.recommendations.slice();
    if (!items.length) return;

    // ── Non-draft: simple sequential adoption ───────────────────
    if (!items.some((entry) => entry._draft_index != null)) {
      for (const entry of items) {
        try {
          await (_apiPostOverride || apiPost)('/api/learning/plan/items', recommendationPayload(entry));
        } catch (err) {
          setStatus(err.message || '批量采纳失败', 'error');
          return;
        }
      }
      clearRecommendations();
      setStatus('');
      showPlanToast('已采纳全部推荐计划');
      await loadMonth({ silent: true });
      renderRecommendations();
      return;
    }

    // ── Draft: validate complete dependency graph first ────────
    const validationErrors = _validateDraftDependencyGraph(items);
    if (validationErrors.length) {
      setStatus(`依赖校验失败：${validationErrors[0]}`, 'error');
      return;
    }

    const ordered = _topologicalOrder(items);
    for (const entry of ordered) {
      const recId = entry.recommendation_id;
      if (state._draftIdToFormalId[recId]) continue;
      try {
        await _adoptDraftItem(entry);
      } catch (err) {
        setStatus(err.message || '批量采纳失败', 'error');
        return;
      }
    }
    clearRecommendations();
    setStatus('');
    showPlanToast('已采纳全部推荐计划');
    await loadMonth({ silent: true });
    renderRecommendations();
  }

  function _topologicalOrder(items) {
    const byIndex = new Map();
    for (const entry of items) {
      const idx = entry._draft_index;
      if (idx != null) byIndex.set(idx, entry);
    }
    const ordered = [];
    const WHITE = 0; const GRAY = 1; const BLACK = 2;
    const color = new Map();
    for (const idx of byIndex.keys()) color.set(idx, WHITE);

    function visit(idx) {
      const c = color.get(idx);
      if (c === BLACK) return;
      if (c === GRAY) throw new Error(`检测到循环依赖，涉及 item ${idx}`);
      color.set(idx, GRAY);
      const entry = byIndex.get(idx);
      if (entry && entry.dependency != null) {
        const depIdx = typeof entry.dependency === 'number' ? entry.dependency : parseInt(String(entry.dependency), 10);
        if (Number.isFinite(depIdx) && byIndex.has(depIdx)) {
          visit(depIdx);
        }
      }
      color.set(idx, BLACK);
      ordered.push(entry);
    }

    for (const idx of [...byIndex.keys()].sort((a, b) => a - b)) {
      visit(idx);
    }
    return ordered;
  }

  function dismissRecommendation(recommendationId) {
    state.recommendations = state.recommendations.filter((entry) => entry.recommendation_id !== recommendationId);
    if (!state.recommendations.length) {
      clearRecommendations();
      setStatus('');
      showPlanToast('已清空推荐预览');
    } else {
      setStatus('');
      showPlanToast('已忽略一条推荐');
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
    setStatus('');
    showPlanToast('增加计划成功');
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
    setStatus('');
    showPlanToast('删除计划成功');
    await loadMonth({ silent: true });
  }

  async function saveItem(itemId) {
    const editor = document.querySelector(`[data-editor-id="${CSS.escape(itemId)}"]`);
    if (!editor) return;
    const title = editor.querySelector('[data-link-title]')?.value || '';
    const course = editor.querySelector('[data-link-course]')?.value || '';
    if (!title.trim()) {
      setStatus('计划内容不能为空', 'error');
      return;
    }
    await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/update`, {
      user_id: state.userId,
      title: title.trim(),
      linked_course_title: course.trim() || null,
    });
    state.editingId = '';
    setStatus('');
    showPlanToast('修改计划成功');
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
      setStatus('');
      showPlanToast('已清空推荐预览');
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

  async function boot() {
    await syncCurrentUser();
    initAssessmentController();
    bindEvents();
    loadProfile();
    await loadMonth();
    if (state.planDraftId) {
      await loadPlanDraft(state.planDraftId);
    }
  }

  void boot();

  // ── Test harness (Node tests only — not used in browser) ─────────
  window.oralUi = oralUi;
  oralUi.learningPlanInternals = {
    buildStartLearningUrl,
    mapDraftItemsToRecommendations,
    recommendationMeta,
    renderRecommendationCard,
    renderTodoItem,
    renderItemEditor,
    taskTypeLabel,
    normalizePlanDisplayItem,
    planDisplayLabel,
    publicPlanText,
    recommendationPayload,
    acceptRecommendation,
    acceptAllRecommendations,
    _topologicalOrder,
    _resolveFormalDependency,
    _validateDraftDependencyGraph,
    _adoptDraftItem,
    addDays,
    dateKey,
    getState: () => state,
    setApiPost: (fn) => { _apiPostOverride = fn; },
  };
})();
