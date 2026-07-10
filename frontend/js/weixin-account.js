(() => {
  window.oralUi?.boot({
    key: 'weixin-account',
    title: '微信连接',
    subtitle: '连接 ClawBot，用微信接收每日计划、打卡和修改计划',
  });

  const state = {
    sessionId: '',
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  function log(message, data = null) {
    const output = $('output');
    const line = data ? `${message}\n${JSON.stringify(data, null, 2)}` : message;
    output.textContent = `${new Date().toLocaleTimeString()}  ${line}\n\n${output.textContent || ''}`;
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
    $('current-user').textContent = me.user ? `${me.user.display_name || me.user.user_id}（${me.user.user_id}）` : '未登录';
    if (me.user) {
      await refreshBindStatus();
    } else {
      $('bind-state').textContent = '未连接';
    }
    return me;
  }

  async function refreshBindStatus() {
    const status = await request('/api/auth/weixin/bind-status');
    $('bind-state').textContent = status.bound
      ? `已连接：${status.weixin_user_id_masked || status.clawbot_instance_id || ''}`
      : '未连接';
    return status;
  }

  async function login() {
    const payload = {
      user_id: $('user-id').value.trim() || 'demo',
      display_name: $('display-name').value.trim() || $('user-id').value.trim() || 'demo',
    };
    const result = await request('/api/auth/web-login', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    log('登录成功', result);
    await refreshMe();
  }

  async function ensureLoggedIn() {
    const me = await request('/api/auth/me');
    if (me.authenticated && me.user) {
      return me.user;
    }
    await login();
    const refreshed = await request('/api/auth/me');
    if (!refreshed.authenticated || !refreshed.user) {
      throw new Error('Web login is required.');
    }
    return refreshed.user;
  }

  async function startQr() {
    await ensureLoggedIn();
    const result = await request('/api/auth/weixin/qr/start', { method: 'POST' });
    state.sessionId = result.session_id;
    $('qr-img').src = result.qr_data_url;
    $('qr-link').href = result.qr_data_url;
    $('qr-note').textContent = result.message || '请用微信扫描二维码完成连接。';
    $('qr-box').hidden = false;
    log('二维码已生成，正在轮询扫码状态。', result);
    startPolling();
  }

  function startPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
    }
    state.pollTimer = window.setInterval(pollQrStatus, 2500);
    void pollQrStatus();
  }

  async function pollQrStatus() {
    if (!state.sessionId) return;
    try {
      const status = await request(`/api/auth/weixin/qr/status?session_id=${encodeURIComponent(state.sessionId)}`);
      $('qr-note').textContent = status.message || '等待扫码确认。';
      if (status.bound || status.status === 'bound') {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        $('qr-box').hidden = true;
        log('微信连接成功。', status.binding || status);
        await refreshBindStatus();
      } else if (status.status === 'expired' || status.status === 'error') {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        log(`二维码状态：${status.status}`, status);
      }
    } catch (err) {
      log(`轮询失败：${err.message}`);
    }
  }

  async function unbind() {
    await ensureLoggedIn();
    const result = await request('/api/auth/weixin/bind', { method: 'DELETE' });
    log('已解绑微信。', result);
    await refreshBindStatus();
  }

  async function pushDaily() {
    await ensureLoggedIn();
    const result = await request('/api/weixin/plan/push-daily', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    log('今日计划已推送到微信。', result);
  }

  $('login-btn').addEventListener('click', () => login().catch((err) => log(`登录失败：${err.message}`)));
  $('qr-btn').addEventListener('click', () => startQr().catch((err) => log(`获取二维码失败：${err.message}`)));
  $('unbind-btn').addEventListener('click', () => unbind().catch((err) => log(`解绑失败：${err.message}`)));
  $('push-btn').addEventListener('click', () => pushDaily().catch((err) => log(`推送失败：${err.message}`)));

  refreshMe().catch(() => {
    $('current-user').textContent = '未登录';
    $('bind-state').textContent = '未连接';
  });
})();
