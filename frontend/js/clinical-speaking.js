(() => {
  'use strict';

  const { byId, escapeHtml, apiJson } = window.oralUi;
  const userId = new URLSearchParams(window.location.search).get('user_id') || 'demo';
  const COMPOSER_MIN_HEIGHT = 30;
  const COMPOSER_MAX_HEIGHT = 160;
  const state = {
    readiness: null,
    session: null,
    busy: false,
    avatar: { instance: null, ready: false, config: null },
    recorder: null,
    inputMode: 'typed',
    history: [],
    pendingRequests: {},
    optimisticTurn: null,
  };

  function createRequestId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function requestIdFor(key, fingerprint) {
    const pending = state.pendingRequests[key];
    if (pending?.fingerprint === fingerprint) return pending.requestId;
    const requestId = createRequestId();
    state.pendingRequests[key] = { fingerprint, requestId };
    return requestId;
  }

  function clearRequestId(key) {
    delete state.pendingRequests[key];
  }

  function refreshIcons() {
    window.lucide?.createIcons?.();
  }

  function setBusy(busy, message = '') {
    state.busy = busy;
    document.body.classList.toggle('is-busy', busy);
    if (message) setSystemStatus(message);
    updateControls();
  }

  function setSystemStatus(message, error = false) {
    const node = byId('system-status');
    if (!node) return;
    node.textContent = message;
    node.classList.toggle('is-error', error);
  }

  function setAvatarStatus(message, kind = '') {
    const node = byId('avatar-status');
    if (!node) return;
    node.textContent = message;
    node.classList.toggle('is-ready', kind === 'ready');
    node.classList.toggle('is-error', kind === 'error');
  }

  function updateControls() {
    const ready = Boolean(state.readiness?.ready && state.avatar.ready);
    const sessionStatus = state.session?.status || '';
    const canRecord = ready && !state.busy && ['consultation', 'oral_exam'].includes(sessionStatus);
    byId('new-training').disabled = !ready || state.busy;
    byId('record-toggle').disabled = !canRecord;
    byId('doctor-transcript').disabled = !canRecord;
    byId('submit-transcript').disabled = !canRecord || !byId('doctor-transcript').value.trim();
    byId('stop-avatar').disabled = !state.avatar.ready;
  }

  function resizeTranscriptInput(input = byId('doctor-transcript')) {
    if (!input) return;
    input.style.height = 'auto';
    const scrollHeight = Math.max(Number(input.scrollHeight) || 0, COMPOSER_MIN_HEIGHT);
    const nextHeight = Math.min(scrollHeight, COMPOSER_MAX_HEIGHT);
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = scrollHeight > COMPOSER_MAX_HEIGHT ? 'auto' : 'hidden';
  }

  async function loadAvatarModule(src) {
    if (!src) throw new Error('未配置数字人 SDK 地址');
    return import(src);
  }

  async function initAvatar() {
    try {
      setAvatarStatus('连接中');
      const body = await apiJson('/api/avatar/config');
      const config = body?.result || {};
      state.avatar.config = config;
      if (!config.configured) {
        throw new Error(`数字人配置缺失：${(config.missing || []).join('、')}`);
      }
      const sdk = await loadAvatarModule(config.sdk_url);
      const AvatarPlatform = sdk.default;
      if (typeof AvatarPlatform !== 'function') throw new Error('数字人 SDK 未导出 AvatarPlatform');
      const stage = byId('patient-avatar-stage');
      stage.replaceChildren();
      const host = document.createElement('div');
      host.id = 'xfyun-clinical-avatar-stage';
      stage.appendChild(host);
      const instance = new AvatarPlatform({ useInlinePlayer: true });
      const eventName = (name) => sdk.SDKEvents?.[name] || name;
      instance.on?.(eventName('frame_start'), () => setAvatarStatus('回答中', 'ready'));
      instance.on?.(eventName('frame_stop'), () => setAvatarStatus('', 'ready'));
      instance.on?.(eventName('error'), () => {
        state.avatar.ready = false;
        setAvatarStatus('连接异常', 'error');
        setSystemStatus('数字人运行失败，训练已暂停', true);
        updateControls();
      });
      instance.setApiInfo({
        appId: config.app_id,
        sceneId: config.scene_id,
        signedUrl: config.signed_url,
      });
      instance.setGlobalParams({
        stream: {
          protocol: config.protocol || 'xrtc',
          alpha: (config.protocol || 'xrtc') === 'xrtc' ? 1 : 0,
        },
        avatar: {
          avatar_id: config.avatar_id,
          width: Number(config.width) || 720,
          height: Number(config.height) || 1280,
        },
        tts: { vcn: config.vcn },
        avatar_dispatch: { interactive_mode: 1, enable_action_status: 1 },
        air: { air: 1, add_nonsemantic: 1 },
      });
      await instance.start({ wrapper: host });
      state.avatar.instance = instance;
      state.avatar.ready = true;
      setAvatarStatus('', 'ready');
      updateControls();
    } catch (error) {
      state.avatar.ready = false;
      setAvatarStatus('不可用', 'error');
      console.error('数字人初始化失败', error);
      setSystemStatus('数字人初始化失败，训练不可用', true);
      updateControls();
    }
  }

  async function stopAvatar() {
    if (!state.avatar.instance) return;
    await (state.avatar.instance.interrupt?.() || Promise.resolve());
    setAvatarStatus('', 'ready');
  }

  async function speakPatient(text) {
    const content = String(text || '').replace(/\s+/g, ' ').trim();
    if (!content) return;
    if (!state.avatar.ready || typeof state.avatar.instance?.writeText !== 'function') {
      throw new Error('数字人未就绪，无法播放患者回答');
    }
    await stopAvatar();
    setAvatarStatus('回答中', 'ready');
    try {
      await state.avatar.instance.writeText(content, {
        nlp: false,
        avatar_dispatch: { interactive_mode: 1, enable_action_status: 1 },
        air: { air: 1, add_nonsemantic: 1 },
      });
    } catch (error) {
      state.avatar.ready = false;
      setAvatarStatus('播放失败', 'error');
      updateControls();
      console.error('数字人患者播放失败', error);
      throw new Error('数字人患者播放失败，训练已停止');
    }
  }

  async function unlockAvatarMedia() {
    const media = Array.from(byId('patient-avatar-stage')?.querySelectorAll('video, audio') || []);
    const results = await Promise.allSettled(media.map((node) => node.play()));
    if (results.some((result) => result.status === 'rejected')) {
      throw new Error('浏览器未授权数字人媒体播放，请再次点击开始训练');
    }
  }

  function renderStages(session) {
    const order = ['consultation', 'diagnosis_confirmation', 'oral_exam', 'completed'];
    const current = Math.max(0, order.indexOf(session.status));
    document.querySelectorAll('[data-stage]').forEach((node) => {
      const index = order.indexOf(node.dataset.stage);
      node.classList.toggle('is-active', index === current);
      node.classList.toggle('is-complete', index < current);
    });
    const examStage = document.querySelector('[data-stage="oral_exam"]');
    if (examStage) {
      const currentQuestion = Math.min((session.question_index || 0) + 1, session.question_count || 5);
      examStage.textContent = session.status === 'oral_exam'
        ? `考核 ${currentQuestion}/${session.question_count || 5}`
        : '考核';
    }
  }

  function renderAssessment(item) {
    const missing = [...(item.missing_points || []), ...(item.errors || [])];
    return `
      <article class="dialogue-turn judge feedback-turn">
        <div class="turn-avatar" aria-hidden="true"><i data-lucide="scale"></i></div>
        <div class="turn-body">
          <div class="turn-role">判官评分</div>
          <div class="turn-content assessment-feedback">
            <div class="assessment-head">
              <span>${escapeHtml(item.label)}</span>
              <strong>${escapeHtml(String(item.score))}<small>/ ${escapeHtml(String(item.max_score))}</small></strong>
            </div>
            <p>${escapeHtml(item.critique)}</p>
            ${missing.length ? `
              <details class="assessment-points">
                <summary>改进点 ${missing.length}</summary>
                <ul>${missing.map((point) => `<li>${escapeHtml(point)}</li>`).join('')}</ul>
              </details>
            ` : ''}
          </div>
        </div>
      </article>
    `;
  }

  function renderDialogue(session) {
    const log = byId('dialogue-log');
    const turns = [
      ...(session.turns || []),
      ...(state.optimisticTurn ? [state.optimisticTurn] : []),
    ];
    if (!turns?.length) return;
    const labels = { patient: '患者', doctor: '你', judge: '判官' };
    const icons = { patient: 'user-round', doctor: 'stethoscope', judge: 'scale' };
    const assessments = new Map((session.assessments || []).map((item) => [item.question_id, item]));
    const questionLabels = new Map((session.assessments || []).map((item) => [item.question_id, item.label]));
    if (session.current_question) questionLabels.set(session.current_question.question_id, session.current_question.label);
    const messages = turns.map((turn) => {
      const roleLabel = turn.pending
        ? '你 · 发送中'
        : turn.role === 'judge' && turn.question_id && questionLabels.get(turn.question_id)
        ? `判官 · ${questionLabels.get(turn.question_id)}`
        : (labels[turn.role] || turn.role);
      const message = `
      <article class="dialogue-turn ${escapeHtml(turn.role)}${turn.pending ? ' is-pending' : ''}">
        <div class="turn-avatar" aria-hidden="true"><i data-lucide="${escapeHtml(icons[turn.role] || 'message-circle')}"></i></div>
        <div class="turn-body">
          <div class="turn-role">${escapeHtml(roleLabel)}</div>
          <div class="turn-content">${escapeHtml(String(turn.content || ''))}</div>
        </div>
      </article>
      `;
      const assessment = turn.role === 'doctor' && turn.question_id
        ? assessments.get(turn.question_id)
        : null;
      return message + (assessment ? renderAssessment(assessment) : '');
    });
    if (session.summary) {
      messages.push(`
        <article class="dialogue-turn judge feedback-turn">
          <div class="turn-avatar" aria-hidden="true"><i data-lucide="badge-check"></i></div>
          <div class="turn-body">
            <div class="turn-role">判官</div>
            <div class="turn-content assessment-feedback final-score">
              <strong>${escapeHtml(String(session.summary.score))}</strong>
              <span>/ 100 · 本次训练完成</span>
            </div>
          </div>
        </article>
      `);
    }
    log.innerHTML = messages.join('');
    log.scrollTop = log.scrollHeight;
  }

  function renderSession(session) {
    state.session = session;
    renderStages(session);
    renderDialogue(session);
    byId('case-target').textContent = sessionTitle(session, 34);
    byId('case-exam').textContent = session.case?.clinical_findings?.exam || '未提供';
    byId('case-aux').textContent = session.case?.clinical_findings?.aux_exam || '未提供';
    const input = byId('doctor-transcript');
    if (session.status === 'oral_exam') {
      input.placeholder = '口述当前考核问题的答案';
    } else if (session.status === 'consultation') {
      input.placeholder = '询问病程、诱因、既往史，或提交初步诊断';
    } else {
      input.placeholder = '当前阶段不接受语音输入';
    }
    updateControls();
    resizeTranscriptInput();
    markActiveHistory(session.id);
    refreshIcons();
  }

  const historyStatusLabels = {
    consultation: '问诊中',
    diagnosis_confirmation: '待确认诊断',
    oral_exam: '考核中',
    completed: '已完成',
  };

  function formatHistoryTime(value) {
    const parsed = new Date(value || '');
    if (Number.isNaN(parsed.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(parsed);
  }

  function sessionTitle(session, maxLength = 22) {
    if (session.summary?.diagnosis) return session.summary.diagnosis;
    const complaint = (session.turns || []).find(
      (turn) => turn.role === 'patient' && turn.stage === 'consultation' && turn.content,
    )?.content || '';
    const normalized = String(complaint).replace(/\s+/g, ' ').trim();
    if (!normalized) return '未完成问诊';
    return normalized.length > maxLength
      ? `${normalized.slice(0, maxLength).replace(/[，。；、！？,.!?]+$/u, '')}…`
      : normalized;
  }

  function markActiveHistory(sessionId) {
    document.querySelectorAll('.history-item').forEach((node) => {
      node.classList.toggle('is-active', node.dataset.sessionId === sessionId);
    });
  }

  function renderHistory(sessions) {
    state.history = Array.isArray(sessions) ? sessions : [];
    const container = byId('session-history');
    if (!state.history.length) {
      container.innerHTML = '<div class="history-empty">暂无问诊记录</div>';
      return;
    }
    container.innerHTML = state.history.map((session) => `
      <button class="history-item${state.session?.id === session.id ? ' is-active' : ''}" type="button" data-session-id="${escapeHtml(session.id)}">
        <span class="history-item-title" title="${escapeHtml(sessionTitle(session, 120))}">${escapeHtml(sessionTitle(session))}</span>
        <span class="history-item-meta">
          <span>${escapeHtml(historyStatusLabels[session.status] || '进行中')}</span>
          <time>${escapeHtml(formatHistoryTime(session.updated_at))}</time>
        </span>
      </button>
    `).join('');
  }

  async function loadHistory() {
    try {
      const query = new URLSearchParams({ user_id: userId });
      const body = await apiJson(`/api/clinical-speaking/sessions?${query.toString()}`);
      renderHistory(body.result || []);
    } catch (error) {
      byId('session-history').innerHTML = `<div class="history-empty">${escapeHtml(error?.message || '历史记录加载失败')}</div>`;
    }
  }

  async function openHistorySession(sessionId) {
    if (!sessionId || state.busy || state.session?.id === sessionId) return;
    setBusy(true, '正在载入历史问诊');
    try {
      const query = new URLSearchParams({ user_id: userId });
      const body = await apiJson(`/api/clinical-speaking/sessions/${encodeURIComponent(sessionId)}?${query.toString()}`);
      renderSession(body.result);
      if (body.result.status === 'diagnosis_confirmation' && body.result.diagnosis_candidate) {
        byId('diagnosis-name').textContent = body.result.diagnosis_candidate.display_name;
        byId('diagnosis-dialog').showModal();
      }
      setSystemStatus('');
    } catch (error) {
      setSystemStatus(error?.message || '历史问诊加载失败', true);
    } finally {
      setBusy(false);
    }
  }

  function renderReadiness(readiness) {
    const labels = {
      spark_agents: '讯飞星火患者与判官 Agent',
      xfyun_iat: '讯飞实时语音听写',
      xfyun_avatar: '讯飞实时数字人',
      clinical_cases: '临床病例库',
    };
    byId('readiness-list').innerHTML = Object.entries(readiness.checks || {}).filter(([, ok]) => !ok).map(([key, ok]) => `
      <div class="readiness-row">
        <span>${escapeHtml(labels[key] || key)}</span>
        <strong class="${ok ? 'ok' : 'fail'}">${ok ? '' : '未配置'}</strong>
      </div>
    `).join('');
  }

  async function checkReadiness() {
    try {
      const body = await apiJson('/api/clinical-speaking/readiness');
      state.readiness = body.result;
      renderReadiness(state.readiness);
      if (state.readiness.ready) {
        setSystemStatus('');
      } else {
        setSystemStatus('训练依赖未就绪', true);
        byId('readiness-dialog').showModal();
      }
    } catch (error) {
      state.readiness = { ready: false, checks: {} };
      setSystemStatus(error?.message || '服务检查失败', true);
    }
    updateControls();
  }

  async function startTraining() {
    if (!state.readiness?.ready || !state.avatar.ready || state.busy) return;
    try {
      await unlockAvatarMedia();
      setBusy(true, '正在准备病例');
      byId('doctor-transcript').value = '';
      byId('transcript-corrections').textContent = '';
      const requestId = requestIdFor('start', userId);
      const body = await apiJson('/api/clinical-speaking/sessions', {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, request_id: requestId }),
      });
      clearRequestId('start');
      renderSession(body.result);
      void loadHistory();
      const opening = [...body.result.turns].reverse().find((turn) => turn.role === 'patient');
      await speakPatient(opening?.content || '');
      setSystemStatus('');
    } catch (error) {
      setSystemStatus(error?.message || '训练启动失败', true);
    } finally {
      setBusy(false);
    }
  }

  function bytesToBase64(bytes) {
    let binary = '';
    const chunk = 0x8000;
    for (let index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
    }
    return btoa(binary);
  }

  function int16ToBase64(samples) {
    return bytesToBase64(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength));
  }

  function downsample(input, inputRate, outputRate = 16000) {
    if (inputRate === outputRate) return Int16Array.from(input, (sample) => Math.max(-1, Math.min(1, sample)) * 0x7fff);
    const ratio = inputRate / outputRate;
    const length = Math.max(1, Math.floor(input.length / ratio));
    const output = new Int16Array(length);
    for (let index = 0; index < length; index += 1) {
      const start = Math.floor(index * ratio);
      const end = Math.min(input.length, Math.floor((index + 1) * ratio));
      let sum = 0;
      for (let cursor = start; cursor < end; cursor += 1) sum += input[cursor];
      const sample = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
      output[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    return output;
  }

  function appendPcm(recorder, samples) {
    recorder.pcm.push(samples);
  }

  function pullPcm(recorder, requested, drain = false) {
    const available = recorder.pcm.reduce((total, item) => total + item.length, 0) - recorder.pcmOffset;
    if (!drain && available < requested) return null;
    const count = drain ? available : Math.min(requested, available);
    const output = new Int16Array(Math.max(0, count));
    let written = 0;
    while (written < count && recorder.pcm.length) {
      const head = recorder.pcm[0];
      const start = recorder.pcmOffset;
      const take = Math.min(count - written, head.length - start);
      output.set(head.subarray(start, start + take), written);
      written += take;
      recorder.pcmOffset += take;
      if (recorder.pcmOffset >= head.length) {
        recorder.pcm.shift();
        recorder.pcmOffset = 0;
      }
    }
    return output;
  }

  function iatFrame(config, status, samples) {
    const frame = {
      data: {
        status,
        format: config.audio.format,
        audio: int16ToBase64(samples || new Int16Array()),
        encoding: config.audio.encoding,
      },
    };
    if (status === 0) {
      frame.common = { app_id: config.app_id };
      frame.business = config.business;
    }
    return frame;
  }

  function applyIatResult(recorder, payload) {
    const result = payload?.data?.result;
    if (!result) return;
    if (result.pgs === 'rpl' && Array.isArray(result.rg)) {
      for (let index = Number(result.rg[0]); index <= Number(result.rg[1]); index += 1) recorder.segments.delete(index);
    }
    const text = (result.ws || []).map((word) => word?.cw?.[0]?.w || '').join('');
    recorder.segments.set(Number(result.sn || recorder.segments.size), text);
    byId('doctor-transcript').value = Array.from(recorder.segments.entries())
      .sort((a, b) => a[0] - b[0])
      .map((entry) => entry[1])
      .join('');
    resizeTranscriptInput();
    updateControls();
  }

  async function startRecording() {
    if (state.recorder || !state.session) return;
    let stream;
    let audioContext;
    try {
      const body = await apiJson('/api/clinical-speaking/iat-config');
      const config = body.result;
      if (!config.configured || !config.signed_url) throw new Error('讯飞语音听写未配置');
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const recorder = {
        config,
        stream,
        audioContext,
        source: null,
        processor: null,
        silentGain: null,
        socket: null,
        timer: null,
        pcm: [],
        pcmOffset: 0,
        firstFrame: true,
        stopping: false,
        segments: new Map(),
      };
      state.recorder = recorder;
      state.inputMode = 'speech';
      const socket = new WebSocket(config.signed_url);
      recorder.socket = socket;
      socket.onopen = () => {
        recorder.source = audioContext.createMediaStreamSource(stream);
        recorder.processor = audioContext.createScriptProcessor(4096, 1, 1);
        recorder.silentGain = audioContext.createGain();
        recorder.silentGain.gain.value = 0;
        recorder.processor.onaudioprocess = (event) => {
          appendPcm(recorder, downsample(event.inputBuffer.getChannelData(0), audioContext.sampleRate));
        };
        recorder.source.connect(recorder.processor);
        recorder.processor.connect(recorder.silentGain);
        recorder.silentGain.connect(audioContext.destination);
        recorder.timer = window.setInterval(() => {
          if (recorder.stopping || socket.readyState !== WebSocket.OPEN) return;
          const samples = pullPcm(recorder, 640);
          if (!samples) return;
          const status = recorder.firstFrame ? 0 : 1;
          recorder.firstFrame = false;
          socket.send(JSON.stringify(iatFrame(config, status, samples)));
        }, 40);
      };
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (Number(payload.code || 0) !== 0) {
          finishRecording(new Error(`讯飞语音听写错误 ${payload.code}: ${payload.message || ''}`));
          return;
        }
        applyIatResult(recorder, payload);
        if (Number(payload?.data?.status) === 2) finishRecording();
      };
      socket.onerror = () => finishRecording(new Error('讯飞语音听写连接失败'));
      byId('record-toggle').classList.add('is-recording');
      byId('record-toggle').innerHTML = '<i data-lucide="square"></i><span>结束录音</span>';
      byId('recording-meter').classList.add('is-active');
      byId('doctor-transcript').value = '';
      resizeTranscriptInput();
      setSystemStatus('正在实时转写');
      refreshIcons();
    } catch (error) {
      stream?.getTracks().forEach((track) => track.stop());
      await audioContext?.close?.();
      state.recorder = null;
      setSystemStatus(error?.message || '无法启动语音听写', true);
    }
  }

  function stopRecording() {
    const recorder = state.recorder;
    if (!recorder || recorder.stopping) return;
    recorder.stopping = true;
    window.clearInterval(recorder.timer);
    recorder.processor?.disconnect();
    recorder.source?.disconnect();
    recorder.silentGain?.disconnect();
    recorder.stream?.getTracks().forEach((track) => track.stop());
    const finalSamples = pullPcm(recorder, 0, true) || new Int16Array();
    if (recorder.socket?.readyState === WebSocket.OPEN) {
      if (recorder.firstFrame) {
        recorder.socket.send(JSON.stringify(iatFrame(recorder.config, 0, finalSamples)));
        recorder.firstFrame = false;
        recorder.socket.send(JSON.stringify(iatFrame(recorder.config, 2, new Int16Array())));
      } else {
        recorder.socket.send(JSON.stringify(iatFrame(recorder.config, 2, finalSamples)));
      }
      window.setTimeout(() => {
        if (state.recorder === recorder) finishRecording(new Error('讯飞语音听写等待超时'));
      }, 9000);
    } else {
      finishRecording(new Error('讯飞语音听写连接已断开'));
    }
  }

  async function finishRecording(error = null) {
    const recorder = state.recorder;
    if (!recorder) return;
    state.recorder = null;
    window.clearInterval(recorder.timer);
    recorder.processor?.disconnect();
    recorder.source?.disconnect();
    recorder.silentGain?.disconnect();
    recorder.stream?.getTracks().forEach((track) => track.stop());
    recorder.socket?.close();
    await recorder.audioContext?.close?.().catch(() => {});
    byId('record-toggle').classList.remove('is-recording');
    byId('record-toggle').innerHTML = '<i data-lucide="mic"></i><span>开始说话</span>';
    byId('recording-meter').classList.remove('is-active');
    if (error) {
      setSystemStatus(error.message, true);
    } else if (byId('doctor-transcript').value.trim()) {
      setSystemStatus('转写完成，请确认术语后提交');
    } else {
      setSystemStatus('未识别到有效语音', true);
    }
    refreshIcons();
    updateControls();
  }

  function showCorrections(session) {
    const lastDoctor = [...(session.turns || [])].reverse().find((turn) => turn.role === 'doctor');
    const corrections = lastDoctor?.corrections || [];
    byId('transcript-corrections').textContent = corrections.length
      ? `术语校对：${corrections.map((item) => `${item.from} → ${item.to}`).join('；')}`
      : '';
  }

  async function submitTranscript() {
    const input = byId('doctor-transcript');
    const transcript = input.value.trim();
    if (!transcript || !state.session || state.busy) return;
    const status = state.session.status;
    const inputMode = state.inputMode;
    const path = status === 'oral_exam'
      ? `/api/clinical-speaking/sessions/${encodeURIComponent(state.session.id)}/exam-answers`
      : `/api/clinical-speaking/sessions/${encodeURIComponent(state.session.id)}/turns`;
    const fingerprint = `${state.session.id}:${status}:${inputMode}:${transcript}`;
    const requestId = requestIdFor('transcript', fingerprint);
    input.value = '';
    state.inputMode = 'typed';
    resizeTranscriptInput(input);
    state.optimisticTurn = {
      role: 'doctor',
      stage: status === 'oral_exam' ? 'oral_exam' : 'consultation',
      content: transcript,
      pending: true,
    };
    renderDialogue(state.session);
    refreshIcons();
    setBusy(true, status === 'oral_exam' ? '判官正在核验知识库证据' : '患者正在理解你的问题');
    let committed = false;
    let avatarPlaybackFailed = false;
    try {
      const body = await apiJson(path, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, transcript, request_id: requestId, input_mode: inputMode }),
      });
      committed = true;
      clearRequestId('transcript');
      const result = body.result;
      state.optimisticTurn = null;
      renderSession(result.session);
      void loadHistory();
      showCorrections(result.session);
      if (result.patient_reply) {
        try {
          await speakPatient(result.patient_reply);
        } catch (avatarError) {
          avatarPlaybackFailed = true;
          setSystemStatus(avatarError?.message || '数字人患者播放失败', true);
        }
      }
      if (result.diagnosis_candidate) {
        byId('diagnosis-name').textContent = result.diagnosis_candidate.display_name;
        byId('diagnosis-dialog').showModal();
      }
      if (!avatarPlaybackFailed) setSystemStatus('');
    } catch (error) {
      state.optimisticTurn = null;
      renderDialogue(state.session);
      if (!committed) {
        input.value = transcript;
        state.inputMode = inputMode;
        resizeTranscriptInput(input);
      }
      setSystemStatus(error?.message || '提交失败', true);
    } finally {
      setBusy(false);
    }
  }

  async function resolveDiagnosis(confirmed) {
    if (!state.session) return;
    setBusy(true, confirmed ? '正在进入判官考核' : '正在返回问诊');
    const fingerprint = `${state.session.id}:${confirmed}`;
    const requestId = requestIdFor('diagnosis', fingerprint);
    try {
      const body = await apiJson(`/api/clinical-speaking/sessions/${encodeURIComponent(state.session.id)}/diagnosis`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, confirmed, request_id: requestId }),
      });
      clearRequestId('diagnosis');
      renderSession(body.result.session);
      void loadHistory();
      setSystemStatus('');
    } catch (error) {
      setSystemStatus(error?.message || '诊断确认失败', true);
    } finally {
      setBusy(false);
    }
  }

  byId('new-training')?.addEventListener('click', startTraining);
  byId('session-history')?.addEventListener('click', (event) => {
    const item = event.target.closest('.history-item');
    if (item) void openHistorySession(item.dataset.sessionId || '');
  });
  byId('stop-avatar')?.addEventListener('click', () => void stopAvatar());
  byId('record-toggle')?.addEventListener('click', () => {
    if (state.recorder) stopRecording();
    else void startRecording();
  });
  byId('submit-transcript')?.addEventListener('click', submitTranscript);
  byId('doctor-transcript')?.addEventListener('input', (event) => {
    if (!state.recorder) state.inputMode = 'typed';
    resizeTranscriptInput(event.currentTarget);
    updateControls();
  });
  byId('doctor-transcript')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submitTranscript();
    }
  });
  byId('diagnosis-dialog')?.addEventListener('close', (event) => {
    const confirmed = event.currentTarget.returnValue === 'confirm';
    void resolveDiagnosis(confirmed);
  });

  refreshIcons();
  Promise.all([checkReadiness(), initAvatar(), loadHistory()]).finally(updateControls);
})();
