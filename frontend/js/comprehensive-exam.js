(() => {
  const state = { stream: null, exam: null, answers: {}, current: 0, userId: 'demo', cameraLost: false, isStarted: false, startedAt: 0, timerId: 0 };
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => window.oralUi?.escapeHtml?.(String(value || '')) || String(value || '').replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  const setStatus = (id, text, error = false) => { const el = $(id); if (el) { el.textContent = text; el.classList.toggle('is-error', error); } };
  const api = (url, body) => window.oralUi.apiJson(url, { method: 'POST', body: JSON.stringify(body) });
  function updateStartAction() {
    const button = $('start-exam'); const label = $('start-exam-label'); if (!button || !label) return;
    if (!state.exam) { label.textContent = '生成试卷'; button.disabled = false; }
    else if (!state.stream) { label.textContent = '开启摄像头后开始考试'; button.disabled = true; }
    else if (!state.isStarted) { label.textContent = '开始考试'; button.disabled = false; }
    else { label.textContent = '考试进行中'; button.disabled = true; }
  }

  async function resolveUser() { try { const me = await window.oralUi.apiJson('/api/auth/me'); state.userId = me?.user?.user_id || me?.user?.username || 'demo'; } catch (_) {} }
  async function enableCamera() {
    setStatus('camera-status', '正在请求摄像头权限…');
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false });
      state.cameraLost = false;
      state.stream.getVideoTracks().forEach((track) => {
        track.addEventListener('ended', () => {
          if (!state.isStarted) return;
          state.cameraLost = true;
          $('submit-exam').disabled = true;
          setStatus('exam-status', '摄像头已中断，请重新开启后再交卷。', true);
        });
      });
      const preview = $('monitor-preview');
      if (preview) preview.srcObject = state.stream;
      $('monitor-placeholder')?.classList.add('is-hidden');
      document.querySelector('.monitor-tile')?.classList.add('is-ready');
      setStatus('camera-status', '摄像头已开启，考试期间请保持开启。');
      if (state.exam && !state.isStarted) setStatus('intro-status', '摄像头已就绪，可开始考试。');
      if (state.isStarted) { setStatus('exam-status', '摄像头已恢复。'); updateProgress(); }
      updateStartAction();
    } catch (error) { setStatus('camera-status', `无法开启摄像头：${error.message || '请在浏览器设置中允许权限。'}`, true); }
  }
  function stopCamera() { state.stream?.getTracks().forEach((track) => track.stop()); state.stream = null; }
  function refreshIcons() { window.lucide?.createIcons?.(); }
  function answered(question) { return Boolean(String(state.answers[question.id] || '').trim()); }
  function renderClock() {
    const remain = Math.max(0, 30 * 60 - Math.floor((Date.now() - state.startedAt) / 1000));
    const hours = String(Math.floor(remain / 3600)).padStart(2, '0');
    const minutes = String(Math.floor((remain % 3600) / 60)).padStart(2, '0');
    const seconds = String(remain % 60).padStart(2, '0');
    $('exam-clock').textContent = `${hours}:${minutes}:${seconds}`;
  }
  function startClock() { window.clearInterval(state.timerId); state.startedAt = Date.now(); renderClock(); state.timerId = window.setInterval(renderClock, 1000); }
  function stopClock() { window.clearInterval(state.timerId); state.timerId = 0; }
  function renderQuestion() {
    const question = state.exam.questions[state.current]; if (!question) return;
    $('question-type').textContent = question.type === 'single_choice' ? '单选题' : `${question.question_kind || '简答题'} · 简答题`;
    $('question-index').textContent = `${state.current + 1} / ${state.exam.questions.length}`;
    const content = $('question-content');
    const title = `<h2 class="question-stem">${state.current + 1}. ${escapeHtml(question.stem || question.prompt)}</h2>`;
    if (question.type === 'single_choice') {
      content.innerHTML = title + `<div class="choice-list">${question.options.map((option) => { const letter = String(option).trim().charAt(0); return `<label class="choice-option"><input type="radio" name="choice" value="${letter}" ${state.answers[question.id] === letter ? 'checked' : ''}><span class="choice-letter">${letter}</span><span>${escapeHtml(String(option).replace(/^[A-E][.、\s]*/, ''))}</span></label>`; }).join('')}</div>`;
      content.querySelectorAll('input').forEach((input) => input.addEventListener('change', () => { state.answers[question.id] = input.value; renderNav(); updateProgress(); }));
    } else { content.innerHTML = title + `<textarea id="short-answer" class="answer-editor" placeholder="请输入你的答案">${escapeHtml(state.answers[question.id] || '')}</textarea>`; content.querySelector('textarea').addEventListener('input', (event) => { state.answers[question.id] = event.target.value; renderNav(); updateProgress(); }); }
    $('previous-question').disabled = state.current === 0; $('next-question').disabled = state.current === state.exam.questions.length - 1; refreshIcons();
  }
  function renderNav() {
    const renderGroup = (questions, startIndex, node) => {
      node.innerHTML = questions.map((question, offset) => {
        const index = startIndex + offset;
        return `<button type="button" class="nav-question ${index === state.current ? 'is-current' : ''} ${answered(question) ? 'is-answered' : ''}" data-index="${index}">${index + 1}</button>`;
      }).join('');
      node.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => { state.current = Number(button.dataset.index); renderQuestion(); renderNav(); }));
    };
    renderGroup(state.exam.questions.filter((question) => question.type === 'single_choice'), 0, $('choice-nav'));
    renderGroup(state.exam.questions.filter((question) => question.type !== 'single_choice'), 15, $('short-nav'));
  }
  function updateProgress() { if (!state.exam) return; const count = state.exam.questions.filter(answered).length; $('progress-count').textContent = `${count} / ${state.exam.questions.length}`; $('answer-card-count').textContent = `${count} / ${state.exam.questions.length}`; $('submit-exam').disabled = !state.isStarted || count !== state.exam.questions.length || state.cameraLost; }
  async function startExam() {
    if (!state.exam) {
      $('start-exam').disabled = true;
      setStatus('intro-status', '正在依据题库和学习画像生成试卷…');
      try {
        const body = await api('/api/learning/comprehensive-exams', { user_id: state.userId });
        state.exam = body.result;
        $('enable-camera').disabled = false;
        $('resume-camera').disabled = false;
        $('question-type').textContent = '试卷已生成';
        $('question-content').innerHTML = '<div class="preflight-state"><i data-lucide="video"></i><strong>请开启摄像头后开始考试</strong></div>';
        setStatus('intro-status', '试卷已生成，请开启摄像头。');
        updateProgress(); updateStartAction(); refreshIcons();
      } catch (error) { setStatus('intro-status', error.message || '试卷生成失败。', true); updateStartAction(); }
      return;
    }
    if (!state.stream || state.isStarted) return;
    state.isStarted = true; state.cameraLost = false;
    startClock(); renderNav(); updateProgress(); renderQuestion(); updateStartAction();
    setStatus('intro-status', ''); setStatus('exam-status', '考试已开始，请完成全部题目后交卷。');
  }
  async function submitExam() { const questions = state.exam.questions; if (state.cameraLost || questions.some((question) => !answered(question))) return; $('submit-exam').disabled = true; setStatus('exam-status', '正在评阅简答题，请勿关闭页面…'); try { const body = await api(`/api/learning/comprehensive-exams/${encodeURIComponent(state.exam.exam_id)}/submit`, { user_id: state.userId, answers: questions.map((question) => ({ question_id: question.id, answer: state.answers[question.id] || '' })) }); showResult(body.result); } catch (error) { setStatus('exam-status', error.message || '交卷失败。', true); updateProgress(); } }
  function renderResultAnalysis(question, item) {
    const tone = Number(item.score) === Number(item.max_score) ? 'is-full-score' : 'is-partial-score';
    if (question.type === 'single_choice') {
      const selected = item.selected_answer || '未作答';
      return `<section class="result-analysis ${tone}"><h4>解析</h4><p class="result-answer-line">你的答案：${escapeHtml(selected)}　正确答案：${escapeHtml(item.correct_answer || question.answer || '')}</p><p>${escapeHtml(question.explanation || item.analysis || '题库暂未提供本题解析。')}</p></section>`;
    }
    const rubric = Array.isArray(question.rubric) ? question.rubric.filter(Boolean) : Array.isArray(item.rubric) ? item.rubric.filter(Boolean) : [];
    const missing = Array.isArray(item.missing_points) ? item.missing_points.filter(Boolean) : [];
    const evidence = question.evidence?.excerpt || item.analysis || '';
    return `<section class="result-analysis ${tone}"><h4>解析</h4><p class="result-answer-line">评分评语：${escapeHtml(item.feedback || '暂无评语')}</p><h5>参考答案</h5><p>${escapeHtml(question.reference_answer || item.reference_answer || '暂无参考答案')}</p><h5>评分要点</h5>${rubric.length ? `<ul>${rubric.map((point) => `<li>${escapeHtml(point)}</li>`).join('')}</ul>` : '<p>暂无评分要点</p>'}${missing.length ? `<h5>待补充要点</h5><ul>${missing.map((point) => `<li>${escapeHtml(point)}</li>`).join('')}</ul>` : ''}${evidence ? `<h5>依据说明</h5><p>${escapeHtml(evidence)}</p>` : ''}</section>`;
  }
  function showResult(exam) { stopClock(); stopCamera(); $('exam-workspace').hidden = true; document.body.classList.add('is-result-view'); $('exam-result').hidden = false; const submission = exam.submission; $('score-value').textContent = submission.score; $('score-detail').textContent = `单选题 ${submission.choice_score} 分 · 简答题 ${submission.short_score} 分`; const byId = Object.fromEntries(exam.questions.map((q) => [q.id, q])); $('result-list').innerHTML = submission.results.map((item, index) => { const question = byId[item.question_id] || {}; const feedback = item.feedback || (item.missing_points || []).join('；'); return `<article class="result-item"><span class="result-score">${item.score}</span><div><strong>${index + 1}. ${escapeHtml(question.stem || question.prompt)}</strong><p class="result-feedback">${escapeHtml(feedback)}</p>${renderResultAnalysis(question, item)}</div><span class="result-max">/ ${item.max_score}</span></article>`; }).join(''); window.scrollTo(0, 0); refreshIcons(); }
  function reset() { window.location.reload(); }
  document.addEventListener('DOMContentLoaded', async () => { window.oralUi.boot({ key:'comprehensive-exam', title:'综合考试', subtitle:'' }); await resolveUser(); $('enable-camera').addEventListener('click', enableCamera); $('resume-camera').addEventListener('click', enableCamera); $('start-exam').addEventListener('click', startExam); $('submit-exam').addEventListener('click', submitExam); $('previous-question').addEventListener('click', () => { state.current -= 1; renderQuestion(); renderNav(); }); $('next-question').addEventListener('click', () => { state.current += 1; renderQuestion(); renderNav(); }); $('restart-exam').addEventListener('click', reset); window.addEventListener('beforeunload', stopCamera); refreshIcons(); });
})();
