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
    { key: 'evidence', name: '\u77e5\u8bc6\u4f9d\u636e\u67e5\u770b', path: '/pages/evidence.html' },
  ];

  const PAGE_META = {
    '/': { key: 'home', title: 'OralPath Workspace', subtitle: TEXT.shellSubtitle },
    '/index.html': { key: 'home', title: 'OralPath Workspace', subtitle: TEXT.shellSubtitle },
    '/pages/master-chat.html': { key: 'master-chat', title: '\u667a\u80fd\u95ee\u7b54', subtitle: TEXT.shellSubtitle },
    '/pages/image.html': { key: 'image', title: '\u5f71\u50cf\u8bc6\u522b', subtitle: TEXT.shellSubtitle },
    '/pages/decision.html': { key: 'decision', title: '\u51b3\u7b56\u8f85\u52a9', subtitle: TEXT.shellSubtitle },
    '/pages/records.html': { key: 'records', title: '\u75c5\u5386\u751f\u6210', subtitle: TEXT.shellSubtitle },
    '/pages/stats.html': { key: 'stats', title: '\u95e8\u8bca\u7edf\u8ba1', subtitle: TEXT.shellSubtitle },
    '/pages/literature.html': { key: 'literature', title: '\u524d\u6cbf\u6587\u732e', subtitle: TEXT.shellSubtitle },
    '/pages/pathology.html': { key: 'pathology', title: '\u75c5\u7406\u95ee\u7b54', subtitle: TEXT.shellSubtitle },
    '/pages/evidence.html': { key: 'evidence', title: '\u77e5\u8bc6\u4f9d\u636e\u67e5\u770b', subtitle: TEXT.shellSubtitle },
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

    nav.innerHTML = NAV_ITEMS.map((item) => {
      const cls = item.key === activeKey ? 'top-nav-link active' : 'top-nav-link';
      return `<a class="${cls}" href="${item.path}">${escapeHtml(item.name)}</a>`;
    }).join('');
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

  window.oralUi = {
    NAV_ITEMS,
    PAGE_META,
    byId,
    escapeHtml,
    apiPost,
    apiInvokeAgent,
    boot,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => boot());
  } else {
    boot();
  }
})();
