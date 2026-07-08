(() => {
  const oralUi = window.oralUi || {};
  const byId = oralUi.byId || ((id) => document.getElementById(id));
  const escapeHtml = oralUi.escapeHtml || ((text) => String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;'));

  const COLORS = {
    ink: '#2f255f',
    text: '#3f356f',
    muted: '#8a80a8',
    line: '#e6e2f4',
    themePurple: '#493aa4',
    violet: '#8b5cf6',
    indigo: '#4f46e5',
    blue: '#5d66d0',
    sky: '#75c6e6',
    cyan: '#9de0e6',
    lavender: '#c3a1d7',
    pink: '#e58bc6',
    rose: '#c48e9b',
  };
  const NODE_FONT = '"LXGW WenKai Screen", "Noto Sans SC", sans-serif';

  const VALUE_LABELS = {
    unknown: '待诊断',
    course_learning: '课程学习',
    exam: '考试导向',
    exam_prep: '考试导向',
    clinical: '临床应用',
    concept_then_case: '概念后接病例',
    case_first: '病例导入',
    exam_point_first: '考点优先',
    practice_first: '练习优先',
    clinical_application: '临床应用',
    gap_repair: '查漏补缺',
    concise: '简洁要点',
    detailed: '详细推导',
    standard: '标准节奏',
    slow: '分段慢速',
    fast: '快速推进',
    beginner: '入门基础',
    intermediate: '进阶学习',
    advanced: '高阶应用',
  };

  const EVENT_TYPE_LABELS = {
    lesson_started: '开始课程',
    tutor_exposure: '学习知识点',
    tutor_step_generated: '完成讲解',
    answer_submitted: '完成题目',
    user_interruption_question: '提出问题',
    progress_updated: '学习进度更新',
    study_plan_item_created: '新增学习计划',
    study_plan_item_completed: '完成学习计划',
    study_plan_item_deferred: '延期学习计划',
  };

  const STUDY_PLAN_STATUS_LABELS = {
    open: '待完成',
    completed: '已完成',
    deferred: '已延期',
  };

  const MEMORY_SLOT_LABELS = {
    profile: '画像',
    mastery: '掌握',
    weaknesses: '薄弱',
    preferences: '偏好',
    goals: '目标',
    rhythm: '节奏',
  };

  const STYLE_DIMENSION_LABELS = {
    explanation_depth: '解释深度',
    entry_mode: '进入方式',
    interaction_tendency: '交互倾向',
    pace: '节奏偏好',
    planning_support_need: '计划引导',
  };

  const STYLE_OPTION_LABELS = {
    concise: '简洁要点',
    standard: '标准深度',
    mechanism_deep: '机制深入',
    concept_first: '概念优先',
    case_first: '病例优先',
    question_first: '问题驱动',
    practice_first: '练习驱动',
    asks_why: '追问原因',
    asks_examples: '需要例子',
    asks_exam_points: '关注考点',
    passive: '较少互动',
    slow: '偏慢',
    normal: '正常',
    low: '低',
    high: '高',
    fast: '偏快',
  };

  const STYLE_STATUS_LABELS = {
    default: '默认',
    unobserved: '未观察',
    observed: '观察',
    emerging: '显现',
    stable: '稳定',
    mixed: '混合',
    stale: '待刷新',
  };

  const PERSONALIZATION_POLICY_LABELS = {
    light: '轻量个性化',
    moderate: '适度个性化',
    strong: '强个性化',
  };

  const LEARNER_TYPE_LABELS = {
    visual_structured_learner: {
      zh: '结构化学习者',
      en: 'Visual Structured Learner',
      advice: '你更适合结构清晰、分层明确的学习组织方式。建议结合思维导图梳理重点，并在学习过程中抓住核心知识点。',
    },
    text_depth_learner: {
      zh: '深度理解学习者',
      en: 'Text Depth Learner',
      advice: '你更偏好机制和原理层面的深入拆解。建议多关注讲义中的因果链条，再配合阶段性总结巩固理解。',
    },
    case_driven_learner: {
      zh: '病例驱动学习者',
      en: 'Case-Driven Learner',
      advice: '你更适合从病例或临床情境进入，再回到概念理解。建议先抓住典型病例，再回看核心概念。',
    },
    exam_oriented_learner: {
      zh: '考试导向学习者',
      en: 'Exam-Oriented Learner',
      advice: '你更关注重点与考点。建议在学习过程中同步整理高频考点，并结合小测及时校准薄弱环节。',
    },
    practice_driven_learner: {
      zh: '练习驱动学习者',
      en: 'Practice-Driven Learner',
      advice: '你更适合边做边学。建议主动利用每小节的小测和阶段卷，在练习反馈中建立理解。',
    },
    steady_paced_learner: {
      zh: '稳步推进学习者',
      en: 'Steady-Paced Learner',
      advice: '你更适合循序渐进的学习节奏。建议把任务拆小，按天完成，再定期回顾已学内容。',
    },
  };

  const state = {
    userId: new URLSearchParams(window.location.search).get('user_id')
      || localStorage.getItem('oralsage.learningProfile.userId')
      || 'demo',
    data: null,
    stats: null,
    trend: [],
    radar: [],
    auto: true,
    timer: 0,
    timelineFilter: 'all',
    timelineSignature: '',
    timelineLayoutRaf: 0,
    timelinePauseTimer: 0,
    knownEventIds: new Set(),
    newEventIds: new Set(),
    firstLoad: true,
    loading: false,
    assessmentPrompted: false,
  };

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function num(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function clamp01(value) {
    return clamp(num(value, 0), 0, 1);
  }

  function pct(value) {
    return `${Math.round(clamp01(value) * 100)}%`;
  }

  function clip(text, limit = 80) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length <= limit) return value;
    return `${value.slice(0, Math.max(0, limit - 3))}...`;
  }

  function human(value, fallback = '待诊断') {
    const key = String(value || '').trim();
    return VALUE_LABELS[key] || key || fallback;
  }

  function eventTypeLabel(value) {
    const key = String(value || '').trim();
    return EVENT_TYPE_LABELS[key] || '学习事件';
  }

  function compactDate(value) {
    return String(value || '').trim().replace(/-/g, '/');
  }

  function studyPlanItemTitle(payload = {}) {
    return clip(payload.title || payload.linked_course_title || '学习任务', 48);
  }

  function studyPlanTags(payload = {}) {
    const tags = [];
    const status = STUDY_PLAN_STATUS_LABELS[String(payload.status || '').trim()];
    if (status) tags.push(status);
    if (payload.plan_date) tags.push(`计划 ${compactDate(payload.plan_date)}`);
    if (payload.linked_course_title) tags.push(clip(payload.linked_course_title, 18));
    const deferredCount = num(payload.deferred_count, 0);
    if (deferredCount > 0) tags.push(`已延期 ${deferredCount} 次`);
    return tags;
  }

  function studyPlanDetail(payload = {}, eventType = '') {
    if (eventType === 'study_plan_item_deferred') {
      const from = compactDate(payload.deferred_from);
      const to = compactDate(payload.deferred_to || payload.plan_date);
      if (from && to) return `从 ${from} 调整至 ${to}`;
      if (to) return `调整至 ${to}`;
    }
    if (payload.linked_course_title) {
      return `关联课程：${payload.linked_course_title}`;
    }
    if (payload.plan_date) {
      return `计划日期：${compactDate(payload.plan_date)}`;
    }
    return '';
  }

  async function apiGet(url) {
    const res = await fetch(url);
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.message || body?.result?.error || String(body));
    }
    return body;
  }

  function slotItems(slot) {
    const slotData = state.data?.learner_memory?.[slot];
    return asArray(slotData?.items).filter((item) => item && typeof item === 'object');
  }

  function allSummaries() {
    return asArray(state.data?.learning_summaries);
  }

  function allMemoryItems() {
    const memory = state.data?.learner_memory || {};
    return Object.entries(memory).flatMap(([slot, slotData]) => {
      return asArray(slotData?.items).map((item) => ({
        ...item,
        slot: item?.slot || slot,
      }));
    }).filter((item) => item && typeof item === 'object');
  }

  function profileState() {
    const value = state.data?.learner_memory?.profile_state;
    return value && typeof value === 'object' && value.style ? value : null;
  }

  function profileStateEvidenceCount(stateSnapshot = profileState()) {
    if (!stateSnapshot) return 0;
    const direct = asArray(stateSnapshot.evidence_ids).length;
    const weakness = asArray(stateSnapshot.weakness_summary)
      .reduce((sum, item) => sum + num(item?.evidence_count, 0), 0);
    const mastery = asArray(stateSnapshot.mastery_summary)
      .reduce((sum, item) => sum + num(item?.evidence_count, 0), 0);
    const style = Object.values(stateSnapshot.style || {}).reduce((sum, dimension) => {
      const options = dimension?.options || {};
      return sum + Object.values(options).reduce((inner, option) => {
        return inner + num(option?.evidence_count, 0);
      }, 0);
    }, 0);
    return Math.max(direct, weakness + mastery, style);
  }

  function hasProfileStateEvidence(stateSnapshot = profileState()) {
    return profileStateEvidenceCount(stateSnapshot) > 0;
  }

  function needsInitialAssessment(profile = {}, assessmentState = null) {
    return window.oralLearnerAssessment?.needsInitialAssessment(profile, assessmentState) || false;
  }

  function skippedAssessment(profile = {}, assessmentState = null) {
    return window.oralLearnerAssessment?.skippedAssessment(profile, assessmentState) || false;
  }

  function planItemsFromEvents() {
    const byIdMap = new Map();
    allEvents().forEach((event) => {
      if (!String(event.event_type || '').startsWith('study_plan_item_')) return;
      const payload = event.payload || {};
      const id = String(payload.plan_item_id || event.id || '').trim();
      if (!id) return;
      const previous = byIdMap.get(id) || {};
      byIdMap.set(id, {
        ...previous,
        id,
        plan_date: payload.plan_date || previous.plan_date,
        title: payload.title || previous.title || '学习任务',
        status: payload.status || previous.status || 'open',
        linked_course_title: payload.linked_course_title || previous.linked_course_title,
        kg_node_ids: asArray(payload.kg_node_ids),
        deferred_count: num(payload.deferred_count, previous.deferred_count || 0),
        updated_at: event.created_at || previous.updated_at,
      });
    });
    return Array.from(byIdMap.values());
  }

  function allPlanItems() {
    const currentItems = asArray(state.data?.study_plan_month?.items);
    return currentItems.length ? currentItems : planItemsFromEvents();
  }

  function allEvents() {
    return asArray(state.data?.recent_events).slice().sort((a, b) => {
      return timestamp(a.created_at) - timestamp(b.created_at);
    });
  }

  function timestamp(value) {
    const time = new Date(value || 0).getTime();
    return Number.isFinite(time) ? time : 0;
  }

  function dateKey(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function itemDateKey(item = {}) {
    const raw = item.plan_date || item.updated_at || item.created_at || '';
    const text = String(raw || '').trim();
    if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text.slice(0, 10);
    return dateKey(text);
  }

  function displayDay(key) {
    const parts = String(key || '').split('-');
    if (parts.length !== 3) return key || '';
    return `${Number(parts[1])}/${Number(parts[2])}`;
  }

  function addDays(date, delta) {
    const next = new Date(date);
    next.setDate(next.getDate() + delta);
    return next;
  }

  function formatTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function kgIdsFromPayload(payload = {}) {
    const ids = [
      ...asArray(payload.kg_node_ids),
      ...asArray(payload.related_kg_node_ids),
    ];
    return ids.map((id) => String(id || '').trim()).filter(Boolean);
  }

  function kgIdsFromItem(item = {}) {
    return asArray(item.kg_node_ids).map((id) => String(id || '').trim()).filter(Boolean);
  }

  function average(values, fallback = 0) {
    const nums = values.map((value) => num(value, NaN)).filter(Number.isFinite);
    if (!nums.length) return fallback;
    return nums.reduce((sum, value) => sum + value, 0) / nums.length;
  }

  async function loadProfile({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    const userId = String(state.userId || 'demo').trim() || 'demo';
    state.userId = userId;
    localStorage.setItem('oralsage.learningProfile.userId', userId);

    try {
      const now = new Date();
      const profileUrl = `/api/learning/profile/${encodeURIComponent(userId)}`;
      const planUrl = `/api/learning/plan/${encodeURIComponent(userId)}/month?year=${now.getFullYear()}&month=${now.getMonth() + 1}`;
      const body = await apiGet(profileUrl);
      const planBody = await apiGet(planUrl).catch((planErr) => ({
        ok: false,
        result: null,
        message: planErr?.message || String(planErr),
      }));
      state.data = {
        ...(body.result || {}),
        study_plan_month: planBody.result || null,
        study_plan_error: planBody.ok === false ? planBody.message || '学习计划数据暂不可用' : '',
      };
      updateNewEventState(asArray(state.data.recent_events));
      render();
      renderAssessmentCard();
      const profile = state.data?.profile || {};
      const assessmentState = state.data?.assessment_state || null;
      if (!silent && !state.assessmentPrompted && needsInitialAssessment(profile, assessmentState)) {
        state.assessmentPrompted = true;
        setTimeout(() => state.assessmentController?.open(), 120);
      }
      state.firstLoad = false;
    } catch (err) {
      renderError(err);
    } finally {
      state.loading = false;
      if (window.lucide) window.lucide.createIcons();
    }
  }

  function updateNewEventState(events) {
    const ids = new Set(events.map((event) => event?.id).filter(Boolean));
    state.newEventIds = new Set();
    if (!state.firstLoad) {
      ids.forEach((id) => {
        if (!state.knownEventIds.has(id)) state.newEventIds.add(id);
      });
    }
    state.knownEventIds = ids;
  }

  function render() {
    state.stats = computeStats();
    state.trend = buildDailyTrend();
    state.radar = buildRadarData(state.stats);
    renderMetrics(state.stats);
    renderAssessmentCard();
    renderLearningStyle();
    renderTimeline();
    renderCharts();
  }

  function computeStats() {
    const events = allEvents();
    const summaries = allSummaries();
    const profile = state.data?.profile || {};
    const stateSnapshot = profileState();
    const stateEvidenceCount = profileStateEvidenceCount(stateSnapshot);
    const masteryItems = slotItems('mastery');
    const weaknessItems = slotItems('weaknesses');
    const rhythmItems = slotItems('rhythm');
    const memoryItems = allMemoryItems();
    const planItems = allPlanItems();
    const planEvents = events.filter((event) => String(event.event_type || '').startsWith('study_plan_item_'));
    const learnedKg = new Set();
    const knownKg = new Set();
    const exposureKg = new Set();
    const learningDates = new Set();
    const recentLearningDates = new Set();
    const now = Date.now();
    const recent14Cutoff = now - 14 * 24 * 60 * 60 * 1000;

    function noteActivityDate(value) {
      const key = dateKey(value);
      const time = timestamp(value);
      if (key) learningDates.add(key);
      if (key && time && time >= recent14Cutoff && time <= now) {
        recentLearningDates.add(key);
      }
    }

    masteryItems.forEach((item) => {
      kgIdsFromItem(item).forEach((id) => {
        learnedKg.add(id);
        knownKg.add(id);
      });
    });
    weaknessItems.forEach((item) => {
      kgIdsFromItem(item).forEach((id) => knownKg.add(id));
    });

    events.forEach((event) => {
      noteActivityDate(event.created_at);
      kgIdsFromPayload(event.payload || {}).forEach((id) => {
        knownKg.add(id);
        if (event.event_type === 'tutor_exposure') {
          exposureKg.add(id);
        }
        if (['tutor_exposure', 'answer_submitted'].includes(event.event_type)) {
          learnedKg.add(id);
        }
      });
    });
    summaries.forEach((summary) => {
      noteActivityDate(summary.created_at);
    });
    planItems.forEach((item) => {
      noteActivityDate(item.updated_at || item.created_at || item.plan_date);
      kgIdsFromItem(item).forEach((id) => knownKg.add(id));
    });

    const answers = events.filter((event) => event.event_type === 'answer_submitted');
    const answerScores = answers.map((event) => num(event.payload?.score, 0));
    const correctAnswers = answers.filter((event) => {
      const payload = event.payload || {};
      if (typeof payload.is_correct === 'boolean') return payload.is_correct;
      return num(payload.score, 0) >= 0.6;
    }).length;
    const accuracy = answers.length ? correctAnswers / answers.length : 0;
    const averageScore = answers.length ? average(answerScores, 0) : 0;

    const startedCourses = events.filter((event) => event.event_type === 'lesson_started').length;
    const progressEvents = events.filter((event) => event.event_type === 'progress_updated');
    const completedCourses = progressEvents.filter((event) => {
      return event.event_type === 'progress_updated' && Boolean(event.payload?.lesson_completed);
    }).length;
    const advancedSteps = progressEvents.filter((event) => Boolean(event.payload?.advanced)).length;
    const qaCount = events.filter((event) => event.event_type === 'user_interruption_question').length;
    const planIds = new Set([
      ...planItems.map((item) => item.id || item.plan_item_id),
      ...planEvents.map((event) => event.payload?.plan_item_id),
    ].map((id) => String(id || '').trim()).filter(Boolean));
    const completedPlanIdsFromEvents = new Set(planEvents
      .filter((event) => event.event_type === 'study_plan_item_completed')
      .map((event) => String(event.payload?.plan_item_id || '').trim())
      .filter(Boolean));
    const planTotal = planItems.length || planIds.size;
    const planCompleted = planItems.length
      ? planItems.filter((item) => item.status === 'completed').length
      : completedPlanIdsFromEvents.size;
    const planPending = planTotal ? Math.max(0, planTotal - planCompleted) : 0;
    const todayKey = dateKey(new Date());
    const todayPlanItems = planItems.filter((item) => itemDateKey(item) === todayKey);
    const todayPlanTotal = todayPlanItems.length;
    const todayPlanCompleted = todayPlanItems.filter((item) => item.status === 'completed').length;
    const completedPlanEventCount = planEvents.filter((event) => event.event_type === 'study_plan_item_completed').length;
    const deferredPlanEventCount = planEvents.filter((event) => event.event_type === 'study_plan_item_deferred').length;

    const nodeCounts = events
      .filter((event) => event.event_type === 'lesson_started')
      .map((event) => num(event.payload?.node_count, 0));
    const plannedNodeCount = nodeCounts.reduce((sum, value) => sum + value, 0);
    const totalKnowledge = Math.max(
      num(state.data?.total_knowledge_points, 0),
      learnedKg.size + Array.from(knownKg).filter((id) => !learnedKg.has(id)).length,
      ...nodeCounts,
      learnedKg.size,
    );
    const nodeProgressRate = (plannedNodeCount || totalKnowledge)
      ? clamp01(advancedSteps / (plannedNodeCount || totalKnowledge))
      : 0;
    const completedCourseRate = startedCourses ? clamp01(completedCourses / startedCourses) : 0;
    const courseProgress = clamp01(Math.max(completedCourseRate, nodeProgressRate));

    const masteryAverage = masteryItems.length
      ? average(masteryItems.map((item) => item.score ?? item.confidence), 0)
      : 0;
    const confidenceItems = [
      ...masteryItems,
      ...weaknessItems,
      ...slotItems('preferences'),
      ...slotItems('goals'),
      ...rhythmItems,
    ];
    const confidenceAverage = confidenceItems.length
      ? average(confidenceItems.map((item) => item.confidence), 0)
      : 0;
    const weaknessSeverity = weaknessItems.length
      ? average(weaknessItems.map((item) => item.severity ?? item.confidence), 0.5)
      : 0;
    const weaknessControl = weaknessItems.length ? clamp01(1 - weaknessSeverity * 0.8) : 0;
    const rhythmAverage = rhythmItems.length
      ? average(rhythmItems.map((item) => item.confidence), 0)
      : 0;
    const planActivity = planTotal
      ? clamp01((planCompleted + planPending * 0.35 + deferredPlanEventCount * 0.2) / planTotal)
      : clamp01(planEvents.length / 6);
    const memorySignalCount = memoryItems.length + stateEvidenceCount;
    const backendSignalCount = events.length + summaries.length + memorySignalCount;
    const recentSignalCount = backendSignalCount + planTotal;
    const planCompletionEvidence = planTotal
      ? clamp01(completedPlanEventCount / planTotal)
      : clamp01(completedPlanEventCount / 3);
    const memoryDepth = clamp01((memorySignalCount * 2 + summaries.length) / 8);

    return {
      learningDays: learningDates.size,
      activeLearningDays14: recentLearningDates.size,
      learnedKnowledge: learnedKg.size,
      touchedKnowledge: knownKg.size,
      totalKnowledge,
      completedCourses,
      startedCourses,
      completedCourseRate,
      advancedSteps,
      courseProgress,
      accuracy,
      averageScore: clamp01(averageScore),
      answerCount: answers.length,
      knowledgeCoverage: totalKnowledge ? clamp01(knownKg.size / totalKnowledge) : 0,
      exposureCoverage: totalKnowledge ? clamp01(exposureKg.size / totalKnowledge) : 0,
      qaCount,
      qaEngagement: clamp01(qaCount / 5),
      masteryAverage: clamp01(masteryAverage),
      confidenceAverage: clamp01(confidenceAverage),
      weaknessControl,
      rhythmAverage: clamp01(rhythmAverage),
      profileConfidence: clamp01(profile.confidence ?? 0),
      eventsCount: events.length,
      summaryCount: summaries.length,
      planTotal,
      planCompleted,
      planPending,
      todayPlanTotal,
      todayPlanCompleted,
      planEventCount: planEvents.length,
      completedPlanEventCount,
      deferredPlanEventCount,
      planActivity,
      planCompletionEvidence,
      memorySignalCount,
      backendSignalCount,
      recentSignalCount,
      memoryDepth,
      weaknessCount: weaknessItems.length,
      masteryCount: masteryItems.length,
    };
  }

  function buildDailyTrend() {
    const events = allEvents();
    const dates = events.map((event) => timestamp(event.created_at)).filter(Boolean);
    const endDate = dates.length ? new Date(Math.max(...dates)) : new Date();
    const keys = Array.from({ length: 7 }, (_, index) => dateKey(addDays(endDate, index - 6)));
    const eventCounts = new Map(keys.map((key) => [key, 0]));

    events.forEach((event) => {
      const key = dateKey(event.created_at);
      if (!eventCounts.has(key)) return;
      eventCounts.set(key, (eventCounts.get(key) || 0) + 1);
    });

    return keys.map((key) => {
      return {
        key,
        label: displayDay(key),
        count: eventCounts.get(key) || 0,
      };
    });
  }

  function buildRadarData(stats) {
    const values = [
      { label: '自主规划', value: stats.planActivity },
      { label: '执行反馈', value: stats.planCompletionEvidence },
      { label: '学习节奏', value: stats.rhythmAverage },
      { label: '薄弱修正', value: stats.weaknessControl },
      { label: '知识掌握', value: stats.masteryAverage },
      { label: '练习反馈', value: stats.answerCount ? stats.averageScore : 0 },
    ];
    const score = average(values.map((item) => item.value), 0);
    const scoreEl = byId('radar-score');
    if (scoreEl) scoreEl.textContent = pct(score);
    return values;
  }

  function renderMetrics(stats) {
    const days = byId('metric-days');
    const knowledge = byId('metric-knowledge');
    const courses = byId('metric-courses');
    const accuracy = byId('metric-accuracy');
    if (days) days.textContent = String(stats.learningDays);
    if (knowledge) knowledge.textContent = `${stats.todayPlanCompleted}/${stats.todayPlanTotal}`;
    if (courses) courses.textContent = `${stats.completedPlanEventCount} 次`;
    if (accuracy) accuracy.textContent = `${num(state.data?.profile?.daily_available_minutes, 45)} 分钟`;
    const trendRange = byId('trend-range');
    if (trendRange) trendRange.textContent = '近 7 天';
  }

  function summaryText(summary = {}) {
    const payload = summary.summary || {};
    return payload.summary
      || asArray(payload.key_signals).filter(Boolean).join('；')
      || asArray(summary.source_event_ids).join('、')
      || '学习摘要';
  }

  function surfaceLabel(surface) {
    const labels = {
      qa: '答疑',
      quiz: '题目',
      lecture: '讲解',
      planning: '规划',
      study_plan: '计划',
    };
    const key = String(surface || '').trim();
    return labels[key] || key || '学习';
  }

  function quizScoreByKgNode() {
    const scores = new Map();
    allEvents()
      .filter((event) => event.event_type === 'answer_submitted')
      .forEach((event) => {
        const kgIds = kgIdsFromPayload(event.payload || {});
        if (!kgIds.length) return;
        const score = clamp01(event.payload?.score ?? (event.payload?.is_correct ? 1 : 0));
        kgIds.forEach((kgId) => {
          const list = scores.get(kgId) || [];
          list.push(score);
          scores.set(kgId, list);
        });
      });
    return new Map(Array.from(scores.entries()).map(([kgId, values]) => [kgId, average(values, 0)]));
  }

  function masteryValueForItem(item, quizScores, fallback) {
    const kgIds = kgIdsFromItem(item);
    const matchedScores = kgIds
      .map((kgId) => quizScores.get(kgId))
      .filter((value) => Number.isFinite(value));
    if (matchedScores.length) return clamp01(average(matchedScores, 0));
    return clamp01(item.score ?? item.confidence ?? fallback);
  }

  function buildStyleDimensionRows(stateSnapshot = profileState()) {
    const style = stateSnapshot?.style || {};
    return Object.entries(STYLE_DIMENSION_LABELS).map(([dimension, label]) => {
      const data = style[dimension] || {};
      const dominant = String(data.dominant || '').trim();
      if (!dominant) return null;
      const option = data.options?.[dominant] || {};
      const status = data.status || option.status || 'unobserved';
      if (status === 'unobserved' || status === 'default' || num(option.evidence_count, 0) <= 0) return null;
      return {
        dimension,
        label,
        value: styleOptionLabel(dimension, dominant),
        status: STYLE_STATUS_LABELS[status] || status,
        confidence: pct(data.confidence ?? option.confidence ?? 0),
        evidence: `${num(option.evidence_count, 0)} 证据`,
        sort: clamp01(data.confidence ?? option.confidence ?? 0),
      };
    }).filter(Boolean).sort((a, b) => b.sort - a.sort);
  }

  function topStyleDimension(stateSnapshot = profileState()) {
    const rows = buildStyleDimensionRows(stateSnapshot);
    return rows[0] || null;
  }

  function styleOptionLabel(dimension, option) {
    if (dimension === 'pace' && option === 'standard') return '标准节奏';
    if (dimension === 'explanation_depth' && option === 'standard') return '标准深度';
    return STYLE_OPTION_LABELS[option] || human(option, option);
  }

  function styleDimensionText(stateSnapshot, dimension) {
    const rows = buildStyleDimensionRows(stateSnapshot);
    const row = rows.find((item) => item.dimension === dimension);
    return row ? `${row.label}：${row.value}（${row.status}）` : '';
  }

  function formatWeaknessSummary(item = {}) {
    const label = item.label || item.text || '薄弱点';
    const status = {
      active: '活跃',
      improving: '改善中',
      resolved: '已缓解',
    }[item.status] || item.status || '';
    const severity = Number.isFinite(Number(item.severity)) ? `严重度 ${pct(item.severity)}` : '';
    return [label, status, severity].filter(Boolean).join(' · ');
  }

  function slotTexts(slot, limit = 4) {
    return slotItems(slot)
      .map((item) => item.text)
      .filter(Boolean)
      .slice(0, limit);
  }

  function findMemoryItem(slot, aspect) {
    return slotItems(slot).find((item) => item.aspect === aspect) || null;
  }

  function renderStyleText(targetId, rows, fallback = '暂无画像信号') {
    const target = byId(targetId);
    if (!target) return;
    const items = rows.filter(Boolean).slice(0, 5);
    if (!items.length) {
      target.textContent = fallback;
      return;
    }
    target.textContent = items.map((item) => clip(item, 86)).join('；');
  }

  function renderTimeline() {
    const target = byId('event-timeline');
    const count = byId('timeline-count');
    if (!target) return;

    const items = buildTimelineItems().filter((item) => {
      return state.timelineFilter === 'all' || item.category === state.timelineFilter;
    });
    target.classList.toggle('is-empty', !items.length);
    if (count) count.textContent = String(items.length);
    if (!items.length) {
      stopTimelineAutoScroll(target);
      state.timelineSignature = `empty:${state.timelineFilter}`;
      target.innerHTML = '<div class="empty-state">暂无学习事件</div>';
      return;
    }

    const visibleItems = items.slice(0, 40);
    const signature = timelineSignature(visibleItems);
    if (signature === state.timelineSignature && target.querySelector('[data-loop-track]')) {
      ensureTimelineAutoScroll(target);
      return;
    }

    stopTimelineAutoScroll(target);
    state.timelineSignature = signature;
    target.dataset.timelineSignature = signature;
    const eventHtml = visibleItems.map((item) => timelineItemHtml(item)).join('');
    const loopHtml = visibleItems.map((item) => timelineItemHtml(item, { clone: true })).join('');
    target.innerHTML = `
      <div class="event-loop-track" data-loop-track>
        <div class="event-loop-group" data-loop-group>${eventHtml}</div>
        <div class="event-loop-group event-loop-copy" data-loop-group data-loop-copy aria-hidden="true">${loopHtml}</div>
      </div>
    `;
    target.querySelectorAll('.event-item.is-new').forEach((node) => {
      node.addEventListener('animationend', (event) => {
        if (event.animationName === 'newGlow') node.classList.remove('is-new');
      }, { once: true });
    });
    startTimelineAutoScroll(target);
  }

  function timelineSignature(items) {
    return [
      state.timelineFilter,
      ...items.map((item) => [
        item.id || '',
        item.category || '',
        item.typeLabel || '',
        item.time || '',
        item.title || '',
        item.detail || '',
        item.color || '',
        item.tags.join('|'),
      ].join('\u001f')),
    ].join('\u001e');
  }

  function timelineItemHtml(item, { clone = false } = {}) {
    const isNew = !clone && item.isNew;
    return `
      <article class="event-item${isNew ? ' is-new' : ''}" data-event-id="${escapeHtml(item.id || '')}" style="--event-color:${item.color}">
        <div class="event-topline">
          <span class="event-type">${escapeHtml(item.typeLabel)}</span>
          <span class="event-time">${escapeHtml(item.time)}</span>
        </div>
        <div class="event-title">${escapeHtml(item.title)}</div>
        ${item.detail ? `<div class="event-detail">${escapeHtml(item.detail)}</div>` : ''}
        ${item.tags.length ? `<div class="event-tags">${item.tags.map((tag) => `<span class="event-tag">${escapeHtml(tag)}</span>`).join('')}</div>` : ''}
      </article>
    `;
  }

  function stopTimelineAutoScroll(target = byId('event-timeline')) {
    if (state.timelineLayoutRaf) {
      window.cancelAnimationFrame(state.timelineLayoutRaf);
      state.timelineLayoutRaf = 0;
    }
    window.clearTimeout(state.timelinePauseTimer);
    state.timelinePauseTimer = 0;
    target?.classList.remove('is-auto-scrolling', 'is-paused');
    const track = target?.querySelector('[data-loop-track]');
    track?.querySelectorAll('[data-loop-fill]').forEach((node) => node.remove());
    track?.style.removeProperty('--timeline-duration');
    track?.style.removeProperty('--timeline-distance');
    if (track) track.dataset.loopLayoutKey = '';
  }

  function timelineLoopLayoutKey(target, track) {
    return [
      target?.dataset.timelineSignature || '',
      Math.round(target?.clientWidth || 0),
      Math.round(target?.clientHeight || 0),
    ].join(':');
  }

  function isTimelineLoopReady(target) {
    const track = target?.querySelector('[data-loop-track]');
    if (!target || !track) return false;
    if (!target.classList.contains('is-auto-scrolling')) return false;
    if (!track.style.getPropertyValue('--timeline-distance')) return false;
    return track.dataset.loopLayoutKey === timelineLoopLayoutKey(target, track);
  }

  function ensureTimelineAutoScroll(target) {
    if (isTimelineLoopReady(target)) return;
    startTimelineAutoScroll(target);
  }

  function startTimelineAutoScroll(target) {
    if (!target) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    if (isTimelineLoopReady(target)) return;
    if (state.timelineLayoutRaf) {
      window.cancelAnimationFrame(state.timelineLayoutRaf);
    }
    state.timelineLayoutRaf = window.requestAnimationFrame(() => {
      state.timelineLayoutRaf = 0;
      const track = target.querySelector('[data-loop-track]');
      if (!track) return;

      const groups = [...track.querySelectorAll('[data-loop-group]')];
      const firstGroup = groups[0];
      const copyGroup = groups[1];
      if (!firstGroup || !copyGroup) return;
      const layoutKey = timelineLoopLayoutKey(target, track);

      groups.forEach((group) => {
        group.querySelectorAll('[data-loop-fill]').forEach((node) => node.remove());
      });
      track.style.removeProperty('--timeline-duration');
      track.style.removeProperty('--timeline-distance');
      target.classList.remove('is-auto-scrolling');

      const originalItemCount = firstGroup.querySelectorAll('.event-item').length;
      const fillHtml = copyGroup.innerHTML;
      if (firstGroup.scrollHeight <= 0 || originalItemCount < 1) return;

      let copyCount = 0;
      while (firstGroup.scrollHeight <= target.clientHeight * 1.35 && copyCount < 10) {
        groups.forEach((group) => {
          const fill = document.createElement('div');
          fill.className = 'event-loop-fill';
          fill.dataset.loopFill = 'true';
          fill.innerHTML = fillHtml;
          fill.querySelectorAll('.is-new').forEach((node) => node.classList.remove('is-new'));
          group.appendChild(fill);
        });
        copyCount += 1;
      }

      const loopHeight = firstGroup.scrollHeight;
      const duration = clamp(loopHeight / 7, 72, 220);
      track.style.setProperty('--timeline-duration', `${duration}s`);
      track.style.setProperty('--timeline-distance', `${loopHeight}px`);
      track.dataset.loopLayoutKey = layoutKey;
      target.classList.add('is-auto-scrolling');
    });
  }

  function pauseTimelineAutoScroll(ms = 3200) {
    const target = byId('event-timeline');
    if (!target) return;
    target.classList.add('is-paused');
    window.clearTimeout(state.timelinePauseTimer);
    state.timelinePauseTimer = window.setTimeout(() => {
      target.classList.remove('is-paused');
      state.timelinePauseTimer = 0;
    }, ms);
  }

  function buildTimelineItems() {
    const events = allEvents()
      .map(eventToTimelineItem)
      .filter(Boolean);
    const summaries = allSummaries()
      .map(summaryToTimelineItem)
      .filter(Boolean);
    return [...events, ...summaries].sort((a, b) => b.sortTime - a.sortTime);
  }

  function eventToTimelineItem(event) {
    const payload = event.payload || {};
    const kgIds = kgIdsFromPayload(payload);
    const base = {
      id: event.id,
      time: formatTime(event.created_at),
      sortTime: timestamp(event.created_at),
      tags: kgIds.slice(0, 3),
      isNew: state.newEventIds.has(event.id),
    };

    if (String(event.event_type || '').startsWith('study_plan_item_')) {
      return {
        ...base,
        category: 'plan',
        typeLabel: '学习计划',
        title: `${eventTypeLabel(event.event_type)}：${studyPlanItemTitle(payload)}`,
        detail: studyPlanDetail(payload, event.event_type),
        tags: studyPlanTags(payload),
        color: COLORS.ink,
      };
    }

    if (event.event_type === 'lesson_started') {
      return {
        ...base,
        category: 'lesson',
        typeLabel: '课程',
        title: `开始学习：${payload.topic || '未命名课程'}`,
        detail: `${num(payload.node_count, 0)} 个学习节点，${num(payload.resource_count, 0)} 个资源`,
        color: COLORS.violet,
      };
    }
    if (event.event_type === 'tutor_exposure') {
      return {
        ...base,
        category: 'lesson',
        typeLabel: '讲课',
        title: `学习知识点：${payload.handout_section_title || event.outline_node_id || '当前小节'}`,
        detail: kgIds.length ? `覆盖 ${kgIds.length} 个知识点` : '',
        color: COLORS.sky,
      };
    }
    if (event.event_type === 'tutor_step_generated') {
      return {
        ...base,
        category: 'lesson',
        typeLabel: '讲课',
        title: `完成讲解：${payload.outline_node_id || event.outline_node_id || '当前小节'}`,
        detail: payload.content_status ? `内容状态：${payload.content_status}` : '',
        color: COLORS.cyan,
      };
    }
    if (event.event_type === 'answer_submitted') {
      const correct = typeof payload.is_correct === 'boolean' ? payload.is_correct : num(payload.score, 0) >= 0.6;
      return {
        ...base,
        category: 'quiz',
        typeLabel: '题目',
        title: `完成题目：${clip(payload.question_stem || payload.question_id || '练习题', 52)}`,
        detail: payload.remedial_suggestion || payload.misconception_summary || '',
        tags: [
          correct ? '正确' : '需复盘',
          `得分 ${Math.round(num(payload.score, 0) * 100)}%`,
          ...asArray(payload.mistake_tags).slice(0, 2),
          ...kgIds.slice(0, 2),
        ],
        color: correct ? COLORS.blue : COLORS.pink,
      };
    }
    if (event.event_type === 'user_interruption_question') {
      return {
        ...base,
        category: 'qa',
        typeLabel: '答疑',
        title: `提出问题：${clip(payload.question || '课堂提问', 58)}`,
        detail: clip(payload.answer || '', 86),
        color: COLORS.lavender,
      };
    }
    if (event.event_type === 'progress_updated') {
      return {
        ...base,
        category: 'lesson',
        typeLabel: payload.lesson_completed ? '完成' : '进度',
        title: payload.lesson_completed ? '完成课程' : payload.advanced ? '学习进度推进' : '学习进度保持',
        detail: payload.action ? `策略：${payload.action}` : '',
        tags: [payload.advanced ? '已推进' : '', payload.lesson_completed ? '课程完成' : ''].filter(Boolean),
        color: payload.lesson_completed ? COLORS.rose : COLORS.indigo,
      };
    }
    return {
      ...base,
      category: 'lesson',
      typeLabel: '学习',
      title: eventTypeLabel(event.event_type),
      detail: '',
      color: COLORS.ink,
    };
  }

  function summaryToTimelineItem(summary) {
    const payload = summary.summary || {};
    const slots = asArray(payload.slots);
    const sourceIds = asArray(summary.source_event_ids);
    return {
      id: summary.id,
      category: 'memory',
      typeLabel: '记忆摘要',
      time: formatTime(summary.created_at),
      sortTime: timestamp(summary.created_at),
      title: `沉淀摘要：${clip(summaryText(summary), 58)}`,
      detail: slots.length ? `写入槽位：${slots.map((slot) => MEMORY_SLOT_LABELS[slot] || slot).join('、')}` : '',
      tags: [
        surfaceLabel(summary.surface),
        ...asArray(payload.key_signals).slice(0, 1),
        sourceIds.length ? `${sourceIds.length} 条证据` : '',
      ].filter(Boolean).map((tag) => clip(tag, 24)),
      color: COLORS.themePurple,
      isNew: false,
    };
  }

  function renderCharts() {
    drawTrendChart(byId('learning-trend'), state.trend);
    drawRadarChart(byId('profile-radar'), state.radar);
  }

  function prepareCanvas(canvas, minWidth = 240, minHeight = 180) {
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(minWidth, rect.width || canvas.parentElement?.clientWidth || minWidth);
    const height = Math.max(minHeight, rect.height || canvas.parentElement?.clientHeight || minHeight);
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx, width, height };
  }

  function drawTrendChart(canvas, data) {
    const prepared = prepareCanvas(canvas, 260, 130);
    if (!prepared) return;
    const { ctx, width, height } = prepared;
    const items = data?.length ? data : [];
    const margin = { top: 16, right: 16, bottom: 25, left: 34 };
    const plotW = Math.max(1, width - margin.left - margin.right);
    const plotH = Math.max(1, height - margin.top - margin.bottom);
    const maxValue = Math.max(1, ...items.map((item) => item.count));

    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(138, 128, 168, 0.18)';
    ctx.fillStyle = COLORS.muted;
    ctx.font = `11px ${NODE_FONT}`;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 3; i += 1) {
      const y = margin.top + plotH * (i / 3);
      ctx.beginPath();
      ctx.moveTo(margin.left, y);
      ctx.lineTo(width - margin.right, y);
      ctx.stroke();
      const label = Math.round(maxValue * (1 - i / 3));
      ctx.fillText(String(label), margin.left - 8, y);
    }

    const points = items.map((item, index) => {
      const x = margin.left + (items.length === 1 ? plotW / 2 : plotW * (index / (items.length - 1)));
      const y = margin.top + plotH * (1 - item.count / maxValue);
      return { x, y, ...item };
    });

    if (points.length) {
      const gradient = ctx.createLinearGradient(0, margin.top, 0, height - margin.bottom);
      gradient.addColorStop(0, 'rgba(73, 58, 164, 0.2)');
      gradient.addColorStop(1, 'rgba(73, 58, 164, 0.02)');
      ctx.beginPath();
      points.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.lineTo(points[points.length - 1].x, margin.top + plotH);
      ctx.lineTo(points[0].x, margin.top + plotH);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();

      ctx.beginPath();
      points.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.strokeStyle = COLORS.themePurple;
      ctx.lineWidth = 2.4;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();

      points.forEach((point) => {
        ctx.beginPath();
        ctx.arc(point.x, point.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#ffffff';
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = COLORS.themePurple;
        ctx.stroke();
      });

      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = COLORS.muted;
      ctx.font = `11px ${NODE_FONT}`;
      points.forEach((point) => {
        ctx.fillText(point.label, point.x, height - margin.bottom + 10);
      });
    }
  }

  function drawRadarChart(canvas, data) {
    const prepared = prepareCanvas(canvas, 260, 260);
    if (!prepared) return;
    const { ctx, width, height } = prepared;
    const items = data?.length ? data : [];
    const cx = width / 2;
    const cy = height / 2 + 8;
    const radius = Math.max(70, Math.min(width, height) * 0.31);
    const levels = 5;

    ctx.lineWidth = 1;
    for (let level = 1; level <= levels; level += 1) {
      const r = radius * (level / levels);
      drawPolygon(ctx, items.length, cx, cy, r);
      ctx.strokeStyle = level === levels ? 'rgba(139, 92, 246, 0.24)' : 'rgba(138, 128, 168, 0.16)';
      ctx.stroke();
    }

    items.forEach((item, index) => {
      const angle = radarAngle(index, items.length);
      const x = cx + Math.cos(angle) * radius;
      const y = cy + Math.sin(angle) * radius;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(x, y);
      ctx.strokeStyle = 'rgba(138, 128, 168, 0.16)';
      ctx.stroke();

      const lx = cx + Math.cos(angle) * (radius + 28);
      const ly = cy + Math.sin(angle) * (radius + 28);
      ctx.fillStyle = COLORS.text;
      ctx.font = `12px ${NODE_FONT}`;
      ctx.textAlign = Math.cos(angle) > 0.3 ? 'left' : Math.cos(angle) < -0.3 ? 'right' : 'center';
      ctx.textBaseline = Math.sin(angle) > 0.45 ? 'top' : Math.sin(angle) < -0.45 ? 'bottom' : 'middle';
      ctx.fillText(item.label, lx, ly);
    });

    const points = items.map((item, index) => {
      const angle = radarAngle(index, items.length);
      const r = radius * clamp01(item.value);
      return {
        x: cx + Math.cos(angle) * r,
        y: cy + Math.sin(angle) * r,
      };
    });
    if (points.length) {
      ctx.beginPath();
      points.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.closePath();
      ctx.fillStyle = 'rgba(139, 92, 246, 0.18)';
      ctx.strokeStyle = COLORS.violet;
      ctx.lineWidth = 2;
      ctx.fill();
      ctx.stroke();

      points.forEach((point) => {
        ctx.beginPath();
        ctx.arc(point.x, point.y, 3.8, 0, Math.PI * 2);
        ctx.fillStyle = '#eee8ff';
        ctx.fill();
        ctx.strokeStyle = COLORS.violet;
        ctx.lineWidth = 1.8;
        ctx.stroke();
      });
    }
  }

  function radarAngle(index, total) {
    return -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(1, total);
  }

  function drawPolygon(ctx, total, cx, cy, radius) {
    ctx.beginPath();
    for (let index = 0; index < total; index += 1) {
      const angle = radarAngle(index, total);
      const x = cx + Math.cos(angle) * radius;
      const y = cy + Math.sin(angle) * radius;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
  }

  function renderError(err) {
    const timeline = byId('event-timeline');
    if (timeline) {
      timeline.innerHTML = `<div class="empty-state">${escapeHtml(err.message || err)}</div>`;
    }
  }

  function initAssessmentController() {
    if (!window.oralLearnerAssessment) return;
    state.assessmentController = window.oralLearnerAssessment.createController({
      getUserId: () => state.userId,
      title: '学习者测评',
      onComplete: async () => {
        await loadProfile({ silent: true });
      },
      onSkip: async () => {
        await loadProfile({ silent: true });
      },
    });
  }

  function startTimer() {
    window.clearInterval(state.timer);
    if (!state.auto) return;
    state.timer = window.setInterval(() => {
      if (document.hidden) return;
      loadProfile({ silent: true });
    }, 5000);
  }

  function bindEvents() {
    document.querySelectorAll('.timeline-chip').forEach((button) => {
      button.addEventListener('click', () => {
        state.timelineFilter = button.getAttribute('data-filter') || 'all';
        document.querySelectorAll('.timeline-chip').forEach((node) => {
          node.classList.toggle('active', node === button);
        });
        renderTimeline();
      });
    });
    const timeline = byId('event-timeline');
    timeline?.addEventListener('focusin', () => {
      timeline.classList.add('is-paused');
    });
    timeline?.addEventListener('focusout', () => {
      timeline.classList.remove('is-paused');
    });
    ['wheel', 'touchstart', 'pointerdown'].forEach((type) => {
      timeline?.addEventListener(type, () => pauseTimelineAutoScroll(), { passive: true });
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.auto) loadProfile({ silent: true });
    });
    window.addEventListener('resize', () => {
      renderCharts();
      startTimelineAutoScroll(byId('event-timeline'));
    });
    byId('profile-assessment-open')?.addEventListener('click', () => {
      state.assessmentController?.open({ resetAnswers: true });
    });
  }

  function ensureStyleDimensionToggle() {
    const list = byId('style-dimension-list');
    if (!list) return null;
    let toggle = byId('style-dimension-toggle');
    if (toggle) return toggle;
    toggle = document.createElement('details');
    toggle.id = 'style-dimension-toggle';
    toggle.className = 'style-dimension-toggle';
    toggle.innerHTML = '<summary>查看画像推理细节</summary>';
    const stylePanel = document.querySelector('.style-panel');
    const styleLines = stylePanel?.querySelector('.style-lines');
    if (stylePanel && styleLines) {
      stylePanel.insertBefore(toggle, styleLines);
    } else {
      list.parentNode?.insertBefore(toggle, list);
    }
    toggle.appendChild(list);
    return toggle;
  }

  function renderStyleDimensions(stateSnapshot = profileState()) {
    const target = byId('style-dimension-list');
    if (!target) return;
    const toggle = ensureStyleDimensionToggle();
    const rows = buildStyleDimensionRows(stateSnapshot);
    if (!rows.length) {
      if (toggle) toggle.hidden = true;
      target.innerHTML = '<div class="style-dimension-empty">等待更多交互证据</div>';
      return;
    }
    if (toggle) toggle.hidden = false;
    target.innerHTML = rows.map((row) => `
      <div class="style-dimension-item">
        <div class="style-dimension-top">
          <span>${escapeHtml(row.label)}</span>
          <strong>${escapeHtml(row.value)}</strong>
        </div>
        <div class="style-dimension-meta">
          <span>${escapeHtml(row.status)}</span>
          <span>${escapeHtml(row.confidence)}</span>
          <span>${escapeHtml(row.evidence)}</span>
        </div>
      </div>
    `).join('');
  }

  function renderLearningStyle() {
    const profile = state.data?.profile || {};
    const stateSnapshot = profileState();
    const hasStateEvidence = hasProfileStateEvidence(stateSnapshot);
    const weaknesses = slotTexts('weaknesses', 4);
    const goals = slotTexts('goals', 4);
    const legacyWeaknesses = [
      ...asArray(profile.known_weak_points),
      ...asArray(profile.mistake_patterns),
    ].filter(Boolean);
    const stateWeaknessRows = asArray(stateSnapshot?.weakness_summary)
      .map(formatWeaknessSummary)
      .filter(Boolean);
    const weaknessRows = stateWeaknessRows.length
      ? stateWeaknessRows
      : weaknesses.length ? weaknesses : legacyWeaknesses;
    const preferenceRows = hasStateEvidence ? [`解释偏好：${human(profile.preferred_explanation)}`] : [];
    const goalRows = goals.length
      ? goals
      : hasStateEvidence ? [`目标导向：${human(profile.goal)}`] : [];
    const paceRow = styleDimensionText(stateSnapshot, 'pace');
    const rhythmRows = [paceRow || (hasStateEvidence ? `学习节奏：${human(profile.pace)}` : '')].filter(Boolean);

    renderStyleDimensions(stateSnapshot);
    renderStyleText('profile-weaknesses', weaknessRows, '暂无易错点记录');
    renderStyleText('profile-preferences', preferenceRows, '暂无画像信号');
    renderStyleText('profile-goals', goalRows, '暂无画像信号');
    renderStyleText('profile-rhythm', rhythmRows, '暂无画像信号');
  }

  function renderAssessmentCard() {
    const title = byId('profile-assessment-title');
    const copy = byId('profile-assessment-copy');
    const button = byId('profile-assessment-open');
    if (!title || !copy || !button) return;
    const profile = state.data?.profile || {};
    const assessmentState = state.data?.assessment_state || {};
    const learnerType = String(profile.learner_type || '').trim();
    const summary = String(profile.learner_type_summary || '').trim();
    const skipped = skippedAssessment(profile, assessmentState);
    const needs = needsInitialAssessment(profile, assessmentState);
    const hasHistoryWithoutAssessment = assessmentState.reason === 'history_without_assessment';
    const minutes = num(profile.daily_available_minutes, 45);

    if (learnerType) {
      const display = LEARNER_TYPE_LABELS[learnerType] || {
        zh: '学习画像已建立',
        advice: `当前画像已建立，每日学习预算约 ${minutes} 分钟。`,
      };
      title.textContent = display.zh;
      copy.textContent = summary || display.advice;
      button.textContent = '重新测评';
      return;
    }
    if (skipped) {
      title.textContent = '你已跳过初始测评';
      copy.textContent = '当前画像使用默认值。若想获得更贴合你的学习计划，可以随时重新测评。';
      button.textContent = '立即测评';
      return;
    }
    if (hasHistoryWithoutAssessment) {
      title.textContent = '学习画像已建立';
      copy.textContent = '已检测到历史学习数据，当前不会自动重新初始化；如需校准偏好，可以手动重新测评。';
      button.textContent = '重新测评';
      return;
    }
    if (needs) {
      title.textContent = '学习者标签待建立';
      copy.textContent = '完成轻量测评后，这里会展示你的学习者类型、时间预算和计划引导偏好。';
      button.textContent = '进行测评';
      return;
    }
    title.textContent = '学习画像已建立';
    copy.textContent = `当前画像已可用，每日学习预算约 ${minutes} 分钟。`;
    button.textContent = '重新测评';
  }

  function boot() {
    initAssessmentController();
    bindEvents();
    loadProfile();
    startTimer();
    if (window.lucide) window.lucide.createIcons();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
