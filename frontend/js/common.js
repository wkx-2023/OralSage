(() => {
  const TEXT = {
    shellSubtitle: '\u5de5\u5177\u7bb1\u6a21\u5757\uff0c\u4f9b\u5b66\u4e60\u5de5\u4f5c\u6d41\u6309\u9700\u8c03\u7528',
    learningChatSubtitle: '\u5b66\u4e60\u3001\u8ba1\u5212\u4e0e\u6587\u732e\u5de5\u4f5c\u6d41\u7684\u7edf\u4e00\u5bf9\u8bdd\u5165\u53e3',
  };

  const NAV_ITEMS = [
    { key: 'home', name: '\u603b\u63a7\u5de5\u4f5c\u53f0', path: '/' },
    { key: 'master-chat', name: '\u667a\u80fd\u95ee\u7b54', path: '/pages/master-chat.html' },
    { key: 'learning-plan', name: '学习计划', path: '/pages/learning-plan.html' },
    { key: 'learning-profile', name: '\u5b66\u4e60\u753b\u50cf', path: '/pages/learning-profile.html' },
    { key: 'evidence', name: '\u77e5\u8bc6\u5e93\u67e5\u770b', path: '/pages/evidence.html' },
    { key: 'knowledge-graph', name: '\u77e5\u8bc6\u56fe\u8c31\u67e5\u770b', path: '/pages/knowledge-graph.html' },
  ];

  const BRAND_HTML = `
    <a class="top-brand" href="/" aria-label="OralSage \u9996\u9875">
      <img src="/assets/images/oralsage-logo.jpg" alt="" aria-hidden="true">
      <span>OralSage</span>
    </a>
  `;

  const PAGE_META = {
    '/': { key: 'home', title: 'OralSage', subtitle: '\u53e3\u8154\u533b\u5b66\u667a\u80fd\u5b66\u4e60\u5de5\u4f5c\u53f0' },
    '/index.html': { key: 'home', title: 'OralSage', subtitle: '\u53e3\u8154\u533b\u5b66\u667a\u80fd\u5b66\u4e60\u5de5\u4f5c\u53f0' },
    '/pages/master-chat.html': { key: 'master-chat', title: '\u667a\u80fd\u95ee\u7b54', subtitle: TEXT.learningChatSubtitle },
    '/pages/weixin-account.html': { key: 'weixin-account', title: '微信连接', subtitle: '连接 ClawBot，用微信接收每日计划、打卡和修改计划' },
    '/pages/learning-plan.html': { key: 'learning-plan', title: '学习计划', subtitle: '按日记录学习计划、完成反馈和节奏记忆' },
    '/pages/learning-profile.html': { key: 'learning-profile', title: '\u5b66\u4e60\u753b\u50cf', subtitle: '\u67e5\u770b\u957f\u671f\u8bb0\u5fc6\u3001\u638c\u63e1\u5ea6\u548c\u753b\u50cf\u8bc1\u636e' },
    '/pages/image.html': { key: 'image', title: '\u5f71\u50cf\u8bc6\u522b', subtitle: TEXT.shellSubtitle },
    '/pages/decision.html': { key: 'decision', title: '\u51b3\u7b56\u8f85\u52a9', subtitle: TEXT.shellSubtitle },
    '/pages/records.html': { key: 'records', title: '\u75c5\u5386\u751f\u6210', subtitle: TEXT.shellSubtitle },
    '/pages/stats.html': { key: 'stats', title: '\u95e8\u8bca\u7edf\u8ba1', subtitle: TEXT.shellSubtitle },
    '/pages/literature.html': { key: 'literature', title: '\u524d\u6cbf\u6587\u732e', subtitle: TEXT.shellSubtitle },
    '/pages/pathology.html': { key: 'pathology', title: '\u75c5\u7406\u95ee\u7b54', subtitle: TEXT.shellSubtitle },
    '/pages/evidence.html': { key: 'evidence', title: '\u77e5\u8bc6\u5e93\u67e5\u770b', subtitle: '\u6309\u4e3b\u9898\u548c\u7ef4\u5ea6\u67e5\u770b\u53e3\u8154\u533b\u5b66\u77e5\u8bc6\u70b9\u4e0e\u539f\u6587\u4f9d\u636e' },
    '/pages/knowledge-graph.html': { key: 'knowledge-graph', title: '\u77e5\u8bc6\u56fe\u8c31\u67e5\u770b', subtitle: '\u67e5\u770b\u6559\u6750\u7ae0\u8282\u3001\u6838\u5fc3\u8282\u70b9\u548c\u77e5\u8bc6\u5173\u7cfb' },
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const message = typeof body === 'string' ? body : JSON.stringify(body);
      throw new Error(`${res.status} ${res.statusText}\n${message}`);
    }
    return body;
  }

  async function apiInvokeAgent(agentName, payload) {
    return apiPost(`/api/agents/${encodeURIComponent(agentName)}/invoke`, payload);
  }

  async function apiJson(url, options = {}) {
    const res = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const message = typeof body === 'string'
        ? body
        : body?.detail?.message || body?.message || JSON.stringify(body);
      throw new Error(message || `${res.status} ${res.statusText}`);
    }
    return body;
  }

  function ensureTopNav() {
    let nav = byId('top-nav');
    if (nav) return nav;

    const topbar = document.querySelector('.topbar');
    if (!topbar) return null;

    nav = document.createElement('nav');
    nav.id = 'top-nav';
    nav.className = 'top-nav';
    topbar.insertBefore(nav, topbar.firstChild);
    return nav;
  }

  function injectTopNav(activeKey) {
    const nav = ensureTopNav();
    if (!nav) return;

    const links = NAV_ITEMS.filter((item) => item.key !== 'weixin-account').map((item) => {
      const cls = item.key === activeKey ? 'top-nav-link active' : 'top-nav-link';
      return `<a class="${cls}" href="${item.path}">${escapeHtml(item.name)}</a>`;
    }).join('');
    nav.innerHTML = BRAND_HTML + links;
  }

  function ensureAccountMenu() {
    let account = byId('top-account');
    if (account) return account;

    const topbar = document.querySelector('.topbar');
    if (!topbar) return null;

    account = document.createElement('div');
    account.id = 'top-account';
    account.className = 'top-account';
    account.innerHTML = `
      <button id="top-account-toggle" class="top-account-toggle" type="button" aria-haspopup="menu" aria-expanded="false">
        <span id="top-account-avatar" class="top-account-avatar" aria-hidden="true">?</span>
        <span class="top-account-copy">
          <span id="top-account-name" class="top-account-name">未登录</span>
          <span id="top-account-role" class="top-account-role">账号</span>
        </span>
      </button>
      <div id="top-account-menu" class="top-account-menu" role="menu" hidden>
        <a class="top-account-menu-item" href="/pages/weixin-account.html" role="menuitem" data-account-login>账号与微信</a>
        <button class="top-account-menu-item danger" type="button" role="menuitem" data-account-logout hidden>退出登录</button>
      </div>
    `;
    topbar.appendChild(account);

    const toggle = account.querySelector('#top-account-toggle');
    const menu = account.querySelector('#top-account-menu');
    toggle?.addEventListener('click', (event) => {
      event.stopPropagation();
      const nextOpen = menu?.hidden;
      setAccountMenuOpen(Boolean(nextOpen));
    });
    account.querySelector('[data-account-logout]')?.addEventListener('click', async () => {
      await logoutCurrentUser();
    });
    document.addEventListener('click', (event) => {
      if (!account.contains(event.target)) setAccountMenuOpen(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') setAccountMenuOpen(false);
    });
    return account;
  }

  function setAccountMenuOpen(open) {
    const menu = byId('top-account-menu');
    const toggle = byId('top-account-toggle');
    if (!menu || !toggle) return;
    menu.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function renderAccount(user) {
    ensureAccountMenu();
    const account = byId('top-account');
    const name = byId('top-account-name');
    const role = byId('top-account-role');
    const avatar = byId('top-account-avatar');
    const logout = account?.querySelector('[data-account-logout]');
    const login = account?.querySelector('[data-account-login]');
    const displayName = String(user?.display_name || user?.username || user?.user_id || '').trim();
    const roleText = String(user?.role || '').trim();
    const signedIn = Boolean(user && displayName);

    if (name) name.textContent = signedIn ? displayName : '未登录';
    if (role) role.textContent = signedIn ? (roleText || 'user') : '去登录/注册';
    if (avatar) avatar.textContent = signedIn ? displayName.slice(0, 1).toUpperCase() : '?';
    if (account) account.classList.toggle('is-signed-in', signedIn);
    if (logout) logout.hidden = !signedIn;
    if (login) login.textContent = signedIn ? '账号与微信' : '登录/注册';
  }

  async function refreshAuthStatus() {
    ensureAccountMenu();
    try {
      const body = await apiJson('/api/auth/me');
      renderAccount(body?.authenticated ? body.user : null);
      return body;
    } catch (_err) {
      renderAccount(null);
      return { ok: false, authenticated: false, user: null };
    }
  }

  async function logoutCurrentUser() {
    setAccountMenuOpen(false);
    try {
      await apiJson('/api/auth/logout', {
        method: 'POST',
        body: JSON.stringify({}),
      });
    } catch (_err) {
      // Even if the server session is already gone, clear local account hints.
    }
    localStorage.removeItem('oralsage.learningProfile.userId');
    localStorage.removeItem('oralsage.learning.stageExamSessionId');
    renderAccount(null);
    window.dispatchEvent(new CustomEvent('oralsage:auth-changed', {
      detail: { authenticated: false, user: null },
    }));
  }

  function setHeader(title, subtitle) {
    const pageTitle = byId('page-title');
    const pageSubtitle = byId('page-subtitle');
    if (pageTitle) pageTitle.textContent = title || '';
    if (pageSubtitle) pageSubtitle.textContent = subtitle || '';
  }

  function boot(meta = null) {
    const pageMeta = meta || PAGE_META[window.location.pathname] || PAGE_META['/'];
    injectTopNav(pageMeta.key);
    ensureAccountMenu();
    void refreshAuthStatus();
    setHeader(pageMeta.title, pageMeta.subtitle);
  }

  function readJson(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return fallback;
      const parsed = JSON.parse(raw);
      return parsed ?? fallback;
    } catch (_err) {
      return fallback;
    }
  }

  function writeJson(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (_err) {
      return false;
    }
  }

  function shortTitle(text, fallback = '未命名记录', limit = 24) {
    const value = String(text || fallback).replace(/\s+/g, ' ').trim();
    return value.length > limit ? `${value.slice(0, limit - 1)}...` : value;
  }

  function createHistoryPanel(options = {}) {
    const {
      key,
      title = '历史记录',
      newLabel = '+ 新建',
      emptyText = '暂无历史记录',
      limit = 30,
      containerId = 'module-nav',
      onNew = null,
      onSelect = null,
    } = options;
    const container = byId(containerId);
    const storageKey = `oralsage.history.${key || window.location.pathname}`;
    let records = Array.isArray(readJson(storageKey, [])) ? readJson(storageKey, []) : [];
    let activeId = records[0]?.id || '';

    function persist() {
      records = records.slice(0, limit);
      writeJson(storageKey, records);
    }

    function render() {
      if (!container) return;
      const items = records.length
        ? records.map((item) => {
          const active = item.id === activeId ? ' active' : '';
          return `
            <button type="button" class="history-item${active}" data-history-id="${escapeHtml(item.id)}">
              <div class="history-title">${escapeHtml(item.title || '未命名记录')}</div>
              <div class="history-time">${escapeHtml(item.time || '')}</div>
            </button>
          `;
        }).join('')
        : `<div class="history-empty">${escapeHtml(emptyText)}</div>`;

      container.innerHTML = `
        <div class="module-history-head">
          <div class="history-headline">${escapeHtml(title)}</div>
          <button type="button" class="history-new compact" data-history-new>${escapeHtml(newLabel)}</button>
        </div>
        <div class="module-history-list">${items}</div>
      `;

      container.querySelector('[data-history-new]')?.addEventListener('click', () => {
        activeId = '';
        render();
        onNew?.();
      });
      container.querySelectorAll('[data-history-id]').forEach((node) => {
        node.addEventListener('click', () => {
          const id = node.getAttribute('data-history-id') || '';
          const record = records.find((item) => item.id === id);
          if (!record) return;
          activeId = id;
          onSelect?.(record);
          render();
        });
      });
    }

    function save(record = {}) {
      const id = record.id || `h-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const existingIndex = records.findIndex((item) => item.id === id);
      const next = {
        ...record,
        id,
        title: shortTitle(record.title, '未命名记录'),
        time: record.time || new Date().toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        }),
      };
      records = existingIndex >= 0
        ? [next, ...records.filter((_, index) => index !== existingIndex)]
        : [next, ...records];
      activeId = id;
      persist();
      render();
      return next;
    }

    function all() {
      return records.slice();
    }

    function setActive(id) {
      activeId = id || '';
      render();
    }

    render();

    return {
      save,
      all,
      render,
      setActive,
      storageKey,
    };
  }

  window.oralUi = {
    NAV_ITEMS,
    PAGE_META,
    byId,
    escapeHtml,
    apiPost,
    apiJson,
    apiInvokeAgent,
    boot,
    refreshAuthStatus,
    logoutCurrentUser,
    createHistoryPanel,
    shortTitle,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => boot());
  } else {
    boot();
  }
})();
