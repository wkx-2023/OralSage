(() => {
  const TEXT = {
    shellSubtitle: '\u63a5\u53e3\u5df2\u4fdd\u7559\uff0c\u529f\u80fd\u5f85\u91cd\u5199',
  };

  const NAV_ITEMS = [
    { key: 'home', name: '\u603b\u63a7\u5de5\u4f5c\u53f0', path: '/' },
    { key: 'master-chat', name: '\u667a\u80fd\u95ee\u7b54', path: '/pages/master-chat.html' },
    { key: 'image', name: '\u5f71\u50cf\u8bc6\u522b', path: '/pages/image.html' },
    { key: 'decision', name: '\u51b3\u7b56\u8f85\u52a9', path: '/pages/decision.html' },
    { key: 'records', name: '\u75c5\u5386\u751f\u6210', path: '/pages/records.html' },
    { key: 'stats', name: '\u95e8\u8bca\u7edf\u8ba1', path: '/pages/stats.html' },
    { key: 'literature', name: '\u524d\u6cbf\u6587\u732e', path: '/pages/literature.html' },
    { key: 'pathology', name: '\u75c5\u7406\u95ee\u7b54', path: '/pages/pathology.html' },
    { key: 'evidence', name: '\u77e5\u8bc6\u5e93\u67e5\u770b', path: '/pages/evidence.html' },
    { key: 'knowledge-graph', name: '\u8bfe\u7a0b\u77e5\u8bc6\u56fe\u8c31', path: '/pages/knowledge-graph.html' },
  ];

  const BRAND_HTML = `
    <a class="top-brand" href="/" aria-label="OralSage \u9996\u9875">
      <img src="/assets/images/oralsage-logo.jpg" alt="" aria-hidden="true">
      <span>OralSage</span>
    </a>
  `;

  const PAGE_META = {
    '/': { key: 'home', title: 'OralSage', subtitle: '\u53e3\u8154\u5168\u6d41\u7a0b\u667a\u80fd\u8bca\u7597\u8f85\u52a9\u7cfb\u7edf' },
    '/index.html': { key: 'home', title: 'OralSage', subtitle: '\u53e3\u8154\u5168\u6d41\u7a0b\u667a\u80fd\u8bca\u7597\u8f85\u52a9\u7cfb\u7edf' },
    '/pages/master-chat.html': { key: 'master-chat', title: '\u667a\u80fd\u95ee\u7b54', subtitle: TEXT.shellSubtitle },
    '/pages/image.html': { key: 'image', title: '\u5f71\u50cf\u8bc6\u522b', subtitle: TEXT.shellSubtitle },
    '/pages/decision.html': { key: 'decision', title: '\u51b3\u7b56\u8f85\u52a9', subtitle: TEXT.shellSubtitle },
    '/pages/records.html': { key: 'records', title: '\u75c5\u5386\u751f\u6210', subtitle: TEXT.shellSubtitle },
    '/pages/stats.html': { key: 'stats', title: '\u95e8\u8bca\u7edf\u8ba1', subtitle: TEXT.shellSubtitle },
    '/pages/literature.html': { key: 'literature', title: '\u524d\u6cbf\u6587\u732e', subtitle: TEXT.shellSubtitle },
    '/pages/pathology.html': { key: 'pathology', title: '\u75c5\u7406\u95ee\u7b54', subtitle: TEXT.shellSubtitle },
    '/pages/evidence.html': { key: 'evidence', title: '\u77e5\u8bc6\u5e93\u67e5\u770b', subtitle: '\u6309\u53e3\u8154\u95ee\u9898\u7ec4\u7ec7\u77e5\u8bc6\u70b9\u4e0e\u539f\u6587\u4f9d\u636e' },
    '/pages/knowledge-graph.html': { key: 'knowledge-graph', title: '', subtitle: '' },
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

    const links = NAV_ITEMS.map((item) => {
      const cls = item.key === activeKey ? 'top-nav-link active' : 'top-nav-link';
      return `<a class="${cls}" href="${item.path}">${escapeHtml(item.name)}</a>`;
    }).join('');
    nav.innerHTML = BRAND_HTML + links;
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
    apiInvokeAgent,
    boot,
    createHistoryPanel,
    shortTitle,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => boot());
  } else {
    boot();
  }
})();
