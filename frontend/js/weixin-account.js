(() => {
  window.oralUi?.boot({
    key: 'weixin-account',
    title: '微信连接',
    subtitle: '登录 OralSage 学习账号后连接 ClawBot，用微信接收每日计划、打卡和修改计划',
  });

  const state = {
    sessionId: '',
    pollTimer: null,
    pollInFlight: false,
    authMode: 'login',
    bound: false,
  };

  const $ = (id) => document.getElementById(id);

  function setText(id, value) {
    const element = $(id);
    if (element) {
      element.textContent = value;
    }
  }

  function refreshIcons() {
    if (window.lucide) window.lucide.createIcons();
  }

  function log(message, data = null) {
    const output = $('output');
    const statusMessage = $('status-message');
    const line = data ? `${message}\n${JSON.stringify(data, null, 2)}` : message;
    if (statusMessage) {
      statusMessage.hidden = false;
      statusMessage.textContent = message;
    }
    output.textContent = `${new Date().toLocaleTimeString()}  ${line}\n\n${output.textContent || ''}`;
  }

  function toggleDebugLog() {
    const output = $('output');
    const toggle = $('debug-toggle');
    const shouldShow = output.hidden;
    output.hidden = !shouldShow;
    toggle.setAttribute('aria-expanded', String(shouldShow));
    toggle.textContent = shouldShow ? '隐藏调试信息' : '查看调试信息';
  }

  function showQrLoading() {
    const qrBox = $('qr-box');
    const qrImg = $('qr-img');
    qrBox.hidden = false;
    qrBox.classList.add('is-loading');
    qrBox.classList.remove('is-error');
    qrImg.hidden = true;
    qrImg.removeAttribute('src');
    $('qr-link').removeAttribute('href');
    $('qr-note').textContent = '正在生成微信二维码，请稍等。';
    qrBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function showQrError(message) {
    const qrBox = $('qr-box');
    qrBox.hidden = false;
    qrBox.classList.remove('is-loading');
    qrBox.classList.add('is-error');
    $('qr-img').hidden = true;
    $('qr-note').textContent = message;
  }

  function stopPolling({ clearSession = false } = {}) {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    if (clearSession) {
      state.sessionId = '';
    }
  }

  function setAuthMode(mode) {
    state.authMode = mode === 'register' ? 'register' : 'login';
    const isRegister = state.authMode === 'register';
    $('confirm-password-field').hidden = !isRegister;
    $('password').autocomplete = isRegister ? 'new-password' : 'current-password';
    $('login-btn').textContent = isRegister ? '注册并登录' : '登录';
    $('login-tab').classList.toggle('is-active', !isRegister);
    $('register-tab').classList.toggle('is-active', isRegister);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = body?.detail?.message || body?.message || `${response.status} ${response.statusText}`;
      throw new Error(message);
    }
    return body;
  }

  async function refreshMe() {
    const me = await request('/api/auth/me');
    setText('current-user', me.user ? `${me.user.display_name || me.user.username || me.user.user_id}（${me.user.username || me.user.user_id}）` : '未登录');
    if (me.user) {
      await refreshBindStatus();
    } else {
      state.bound = false;
      setText('bind-state', '未连接');
    }
    return me;
  }

  async function refreshBindStatus() {
    const status = await request('/api/auth/weixin/bind-status');
    state.bound = Boolean(status.bound);
    setText('bind-state', status.bound
      ? `已连接：${status.weixin_user_id_masked || status.clawbot_instance_id || ''}`
      : '未连接');
    return status;
  }

  function authPayload() {
    const username = $('user-id').value.trim();
    const password = $('password').value;
    if (!username) {
      throw new Error('请输入用户名。');
    }
    if (!password) {
      throw new Error('请输入密码。');
    }
    if (state.authMode === 'register' && password !== $('confirm-password').value) {
      throw new Error('两次输入的密码不一致。');
    }
    return {
      username,
      user_id: username,
      display_name: username,
      password,
    };
  }

  async function submitAuth() {
    const payload = {
      ...authPayload(),
    };
    const result = await request(state.authMode === 'register' ? '/api/auth/register' : '/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    log(state.authMode === 'register' ? '注册成功，已登录。' : '登录成功。', result);
    await refreshMe();
    await window.oralUi?.refreshAuthStatus?.();
  }

  async function ensureLoggedIn() {
    const me = await request('/api/auth/me');
    if (me.authenticated && me.user) {
      return me.user;
    }
    await submitAuth();
    const refreshed = await request('/api/auth/me');
    if (!refreshed.authenticated || !refreshed.user) {
      throw new Error('Web login is required.');
    }
    return refreshed.user;
  }

  async function startQr() {
    if (state.bound) {
      await unbind();
      return;
    }
    const qrBtn = $('qr-btn');
    qrBtn.disabled = true;
    showQrLoading();
    log('正在生成微信二维码。');
    try {
      await ensureLoggedIn();
      const result = await request('/api/auth/weixin/qr/start', { method: 'POST' });
      state.sessionId = result.session_id;
      $('qr-img').src = result.qr_data_url;
      $('qr-img').hidden = false;
      $('qr-link').href = result.qr_data_url;
      $('qr-note').textContent = result.message || '请用微信扫描二维码完成连接。';
      $('qr-box').classList.remove('is-loading', 'is-error');
      log('二维码已生成，正在轮询扫码状态。', result);
      startPolling();
    } finally {
      qrBtn.disabled = false;
    }
  }

  function startPolling() {
    stopPolling();
    void pollQrStatus();
  }

  function scheduleNextPoll(delayMs = 2500) {
    stopPolling();
    state.pollTimer = window.setTimeout(pollQrStatus, delayMs);
  }

  async function pollQrStatus() {
    if (!state.sessionId || state.pollInFlight) return;
    let shouldContinue = true;
    state.pollInFlight = true;
    try {
      const status = await request(`/api/auth/weixin/qr/status?session_id=${encodeURIComponent(state.sessionId)}`);
      $('qr-note').textContent = status.message || '等待扫码确认。';
      if (status.bound || status.status === 'bound') {
        shouldContinue = false;
        stopPolling({ clearSession: true });
        $('qr-box').hidden = true;
        log('微信连接成功。', status.binding || status);
        await refreshBindStatus();
      } else if (status.status === 'expired' || status.status === 'error') {
        shouldContinue = false;
        stopPolling({ clearSession: true });
        log(`二维码状态：${status.status}`, status);
      }
    } catch (err) {
      log(`轮询失败：${err.message}`);
    } finally {
      state.pollInFlight = false;
      if (shouldContinue && state.sessionId) {
        scheduleNextPoll();
      }
    }
  }

  async function unbind() {
    await ensureLoggedIn();
    const result = await request('/api/auth/weixin/bind', { method: 'DELETE' });
    log('已解绑微信。', result);
    await refreshBindStatus();
  }

  async function logout() {
    stopPolling({ clearSession: true });
    $('qr-box').hidden = true;
    const result = await request('/api/auth/logout', { method: 'POST' });
    log('已退出登录。', result);
    state.bound = false;
    setText('current-user', '未登录');
    setText('bind-state', '未连接');
    await window.oralUi?.refreshAuthStatus?.();
  }

  async function pushDaily() {
    await ensureLoggedIn();
    const result = await request('/api/weixin/plan/push-daily', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    log('今日计划已推送到微信。', result);
  }

  async function pushSummary() {
    await ensureLoggedIn();
    const result = await request('/api/weixin/plan/push-summary', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    log('今日总结已推送到微信。', result);
  }

  $('login-tab').addEventListener('click', () => setAuthMode('login'));
  $('register-tab').addEventListener('click', () => setAuthMode('register'));
  $('login-btn').addEventListener('click', () => submitAuth().catch((err) => log(`${state.authMode === 'register' ? '注册' : '登录'}失败：${err.message}`)));
  $('qr-btn').addEventListener('click', () => startQr().catch((err) => {
    const message = `获取二维码失败：${err.message}`;
    showQrError(message);
    log(message);
  }));
  $('unbind-btn').addEventListener('click', () => unbind().catch((err) => log(`解绑失败：${err.message}`)));
  $('push-btn').addEventListener('click', () => pushDaily().catch((err) => log(`推送失败：${err.message}`)));
  $('summary-btn').addEventListener('click', () => pushSummary().catch((err) => log(`推送总结失败：${err.message}`)));
  $('logout-btn').addEventListener('click', () => logout().catch((err) => log(`退出登录失败：${err.message}`)));
  $('debug-toggle').addEventListener('click', toggleDebugLog);
  document.querySelectorAll('[data-proxy-click]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = $(button.dataset.proxyClick || '');
      if (target && !target.disabled) target.click();
    });
  });
  window.addEventListener('pagehide', () => stopPolling({ clearSession: true }));

  refreshMe().catch(() => {
    setText('current-user', '未登录');
    setText('bind-state', '未连接');
  });
  setAuthMode('login');
  refreshIcons();
})();
