(() => {
  const byId = (window.oralUi && window.oralUi.byId) || ((id) => document.getElementById(id));
  const escapeHtml = (window.oralUi && window.oralUi.escapeHtml) || ((text) => String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;'));

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

  function needsInitialAssessment(profile = {}) {
    return !profile.assessment_source && !profile.assessment_skipped;
  }

  function skippedAssessment(profile = {}) {
    return Boolean(profile.assessment_skipped);
  }

  function createController(options = {}) {
    const config = {
      getUserId: options.getUserId || (() => 'demo'),
      onComplete: options.onComplete || (() => {}),
      onSkip: options.onSkip || (() => {}),
      onOpen: options.onOpen || (() => {}),
      onClose: options.onClose || (() => {}),
      title: options.title || '学习者测评',
    };

    const state = {
      definition: null,
      answers: {},
      submitting: false,
      loading: false,
      opened: false,
    };

    function ensureModal() {
      let root = byId('learner-assessment-modal');
      if (root) return root;
      root = document.createElement('section');
      root.id = 'learner-assessment-modal';
      root.className = 'learner-assessment-modal';
      root.hidden = true;
      root.setAttribute('aria-hidden', 'true');
      root.innerHTML = `
        <div class="learner-assessment-backdrop" data-assessment-close></div>
        <div class="learner-assessment-dialog" role="dialog" aria-modal="true" aria-labelledby="learner-assessment-title">
          <div class="learner-assessment-head">
            <div>
              <span class="learner-assessment-kicker">Onboarding</span>
              <h2 id="learner-assessment-title">${escapeHtml(config.title)}</h2>
              <p class="learner-assessment-subtitle">先花 1-2 分钟完成轻量测评，系统会据此生成更适合你的学习计划。</p>
            </div>
            <button class="learner-assessment-icon" type="button" data-assessment-close aria-label="关闭测评">×</button>
          </div>
          <div id="learner-assessment-status" class="learner-assessment-status" aria-live="polite"></div>
          <div id="learner-assessment-body" class="learner-assessment-body"></div>
          <div class="learner-assessment-footer">
            <button id="learner-assessment-skip" class="learner-assessment-btn ghost" type="button">跳过，使用默认画像</button>
            <button id="learner-assessment-submit" class="learner-assessment-btn strong" type="button">提交测评</button>
          </div>
        </div>
      `;
      document.body.appendChild(root);
      root.querySelectorAll('[data-assessment-close]').forEach((node) => {
        node.addEventListener('click', () => close());
      });
      byId('learner-assessment-skip')?.addEventListener('click', () => submit(true));
      byId('learner-assessment-submit')?.addEventListener('click', () => submit(false));
      return root;
    }

    function setStatus(message = '', tone = 'muted') {
      const node = byId('learner-assessment-status');
      if (!node) return;
      node.textContent = message;
      node.dataset.tone = tone;
    }

    function renderDefinition() {
      const body = byId('learner-assessment-body');
      if (!body) return;
      if (!state.definition) {
        body.innerHTML = '<div class="learner-assessment-empty">未加载到测评题目。</div>';
        return;
      }
      const questions = Array.isArray(state.definition.questions) ? state.definition.questions : [];
      body.innerHTML = questions.map((question, index) => {
        const options = Array.isArray(question.options) ? question.options : [];
        const selected = state.answers[question.question_id] || '';
        return `
          <article class="learner-assessment-question" data-question-id="${escapeHtml(question.question_id)}">
            <div class="learner-assessment-question-head">
              <span class="learner-assessment-question-index">Q${index + 1}</span>
              <h3>${escapeHtml(question.prompt || '')}</h3>
            </div>
            <div class="learner-assessment-options">
              ${options.map((option) => `
                <label class="learner-assessment-option${selected === option.id ? ' selected' : ''}">
                  <input type="radio" name="${escapeHtml(question.question_id)}" value="${escapeHtml(option.id)}" ${selected === option.id ? 'checked' : ''}>
                  <span class="learner-assessment-option-tag">${escapeHtml(option.id)}</span>
                  <span class="learner-assessment-option-text">${escapeHtml(option.label || '')}</span>
                </label>
              `).join('')}
            </div>
          </article>
        `;
      }).join('');

      body.querySelectorAll('input[type="radio"]').forEach((input) => {
        input.addEventListener('change', () => {
          state.answers[input.name] = input.value;
          renderDefinition();
        });
      });
    }

    async function loadDefinition() {
      if (state.definition || state.loading) return;
      state.loading = true;
      setStatus('正在加载测评题目...');
      try {
        const body = await apiGet('/api/learning/assessment');
        state.definition = body.result || null;
        renderDefinition();
        setStatus('');
      } catch (error) {
        setStatus(error.message || '测评题目加载失败', 'error');
      } finally {
        state.loading = false;
      }
    }

    function open({ resetAnswers = false } = {}) {
      ensureModal();
      if (resetAnswers) state.answers = {};
      state.opened = true;
      const root = byId('learner-assessment-modal');
      if (root) {
        root.hidden = false;
        root.setAttribute('aria-hidden', 'false');
      }
      config.onOpen();
      loadDefinition();
    }

    function close() {
      state.opened = false;
      const root = byId('learner-assessment-modal');
      if (root) {
        root.hidden = true;
        root.setAttribute('aria-hidden', 'true');
      }
      config.onClose();
    }

    function collectAnswers() {
      if (!state.definition) return [];
      return (state.definition.questions || []).map((question) => ({
        question_id: question.question_id,
        selected_option: state.answers[question.question_id] || '',
      }));
    }

    function hasAllAnswers() {
      if (!state.definition) return false;
      return (state.definition.questions || []).every((question) => Boolean(state.answers[question.question_id]));
    }

    async function submit(skipAssessment) {
      if (state.submitting) return;
      if (!skipAssessment && !hasAllAnswers()) {
        setStatus('请先完成全部题目，再提交测评。', 'error');
        return;
      }
      state.submitting = true;
      const submitButton = byId('learner-assessment-submit');
      const skipButton = byId('learner-assessment-skip');
      if (submitButton) submitButton.disabled = true;
      if (skipButton) skipButton.disabled = true;
      setStatus(skipAssessment ? '正在记录跳过状态...' : '正在生成初始画像...');
      try {
        const body = await apiPost('/api/learning/assessment/submit', {
          user_id: config.getUserId(),
          instrument_name: state.definition?.instrument_name || 'learning_onboarding_v1',
          instrument_version: state.definition?.instrument_version || 'v1',
          answers: skipAssessment ? [] : collectAnswers(),
          skip_assessment: skipAssessment,
        });
        const result = body.result || {};
        if (skipAssessment) {
          config.onSkip(result);
        } else {
          config.onComplete(result);
        }
        close();
      } catch (error) {
        setStatus(error.message || '测评提交失败', 'error');
      } finally {
        state.submitting = false;
        if (submitButton) submitButton.disabled = false;
        if (skipButton) skipButton.disabled = false;
      }
    }

    return {
      open,
      close,
      loadDefinition,
      needsInitialAssessment,
      skippedAssessment,
    };
  }

  window.oralLearnerAssessment = {
    createController,
    needsInitialAssessment,
    skippedAssessment,
  };
})();
