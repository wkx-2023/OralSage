(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;

  const state = {
    summary: {},
    textbook: {},
    textbooks: [],
    chapters: [],
    overview: { nodes: [], edges: [] },
    graph: { nodes: [], edges: [] },
    selectedId: '',
    activeBookTitle: '',
    activeChapter: null,
    search: '',
    width: 900,
    height: 620,
    scale: 0.54,
    panX: 0,
    panY: 0,
    dragging: null,
    cardDragging: null,
    layoutKey: '',
    laidOutGraph: null,
    overviewLayout: null,
    overviewLayoutKey: '',
    viewAnimating: false,
    viewAnimationFrame: 0,
    viewAnimationResolve: null,
    transformFrame: 0,
    bookFlightId: 0,
    entranceActive: false,
    entrancePlayed: false,
    entranceDuration: 0,
    entranceFrame: 0,
    entranceToken: 0,
    visualGraph: null,
    detailVisible: false,
    bookPromptVisible: true,
  };

  const ENTRANCE_DURATION = 4200;
  const MIN_GRAPH_SCALE = 0.24;
  const MAX_GRAPH_SCALE = 1.9;
  const OVERVIEW_MIN_SCALE = 0.2;
  const OVERVIEW_MAX_SCALE = 0.42;

  const GRAPH_NODE_COLORS = [
    '#5D66D0',
    '#A961C4',
    '#C3A1D7',
    '#6E88E5',
    '#B488E8',
    '#E58BC6',
    '#C48E9B',
    '#A6CEE3',
    '#9FB1E6',
    '#A9C3E6',
    '#C2C6E6',
    '#95D1E5',
    '#9DE0E6',
    '#75C6E6',
  ];
  const FALLBACK_BOOK_COLOR = GRAPH_NODE_COLORS[0];

  document.body?.classList.add('kg-entrance-pending');

  const TYPE_LABELS = {
    Disease: '疾病',
    DiseaseSubtype: '疾病亚型',
    Etiology: '病因',
    Pathogen: '病原',
    RiskFactor: '危险因素',
    Symptom: '症状',
    ClinicalSign: '体征',
    ImagingFeature: '影像表现',
    PathologicalFeature: '病理特征',
    PathologicalProcess: '病理过程',
    DiagnosticMethod: '诊断方法',
    Treatment: '治疗',
    Complication: '并发症',
    Anatomy: '解剖部位',
  };

  const RELATION_LABELS = {
    HAS_ETIOLOGY: '病因',
    HAS_PATHOGEN: '病原',
    HAS_RISK_FACTOR: '危险因素',
    HAS_SYMPTOM: '症状',
    HAS_CLINICAL_SIGN: '体征',
    HAS_IMAGING_FEATURE: '影像表现',
    HAS_PATHOLOGICAL_FEATURE: '病理特征',
    HAS_PATHOLOGICAL_PROCESS: '病理过程',
    HAS_COMPLICATION: '并发症',
    DIAGNOSED_BY: '诊断方法',
    TREATED_BY: '治疗',
    LOCATED_IN: '部位',
    DIFFERENTIAL_FROM: '鉴别',
    IS_SUBTYPE_OF: '分型',
    SHARES_ETIOLOGY: '共享病因',
    SHARES_PATHOGEN: '共享病原',
    SHARES_RISK_FACTOR: '共享危险因素',
    SHARES_SYMPTOM: '症状重叠',
    SHARES_CLINICAL_SIGN: '体征重叠',
    SHARES_IMAGING_FEATURE: '影像相似',
    SHARES_PATHOLOGICAL_FEATURE: '病理相似',
    SHARES_PATHOLOGICAL_PROCESS: '机制相似',
    SHARES_COMPLICATION: '并发相关',
    SHARES_SUPPORT: '共享知识点',
  };

  const PREDICATE_GROUPS = [
    ['HAS_ETIOLOGY', 'HAS_PATHOGEN', 'HAS_RISK_FACTOR'],
    ['HAS_SYMPTOM', 'HAS_CLINICAL_SIGN'],
    ['HAS_IMAGING_FEATURE', 'HAS_PATHOLOGICAL_FEATURE', 'HAS_PATHOLOGICAL_PROCESS'],
    ['DIAGNOSED_BY', 'TREATED_BY'],
    ['HAS_COMPLICATION', 'LOCATED_IN', 'DIFFERENTIAL_FROM', 'IS_SUBTYPE_OF'],
  ];

  function apiGet(url) {
    return fetch(url).then(async (res) => {
      const body = await res.json();
      if (!res.ok) throw new Error(body.message || JSON.stringify(body));
      return body;
    });
  }

  function clip(text, limit = 36) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (value.length <= limit) return value;
    return `${value.slice(0, limit - 1)}...`;
  }

  function setEmpty(text, hidden = false) {
    const empty = byId('kg-empty');
    if (!empty) return;
    empty.textContent = text || '';
    empty.classList.toggle('hidden', hidden);
  }

  function prefersReducedMotion() {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  }

  function setDetailPanelOpen(open) {
    state.detailVisible = Boolean(open);
    document.body?.classList.toggle('kg-detail-open', state.detailVisible);
    const detail = byId('kg-detail-body')?.closest('.kg-detail');
    if (!detail) return;
    detail.setAttribute('aria-hidden', state.detailVisible ? 'false' : 'true');
    if (!state.detailVisible) return;
    detail.classList.remove('minimized');
    const button = byId('kg-detail-minimize');
    if (button) {
      button.setAttribute('aria-pressed', 'false');
      button.innerHTML = '<i data-lucide="minus"></i>';
      if (window.lucide) window.lucide.createIcons();
    }
  }

  function closeDetailPanel(clearSelection = false) {
    if (clearSelection) state.selectedId = '';
    setDetailPanelOpen(false);
  }

  function beginGraphEntrance() {
    document.body?.classList.remove('kg-entrance-pending');
    if (state.entrancePlayed || prefersReducedMotion()) {
      state.entranceActive = false;
      state.entrancePlayed = true;
      document.body?.classList.add('kg-entrance-complete');
      return false;
    }
    state.entranceActive = true;
    document.body?.classList.remove('kg-entrance-complete');
    document.body?.classList.add('kg-entrance-active');
    return true;
  }

  function setVisualCanvasVisible(visible) {
    const canvas = byId('kg-entrance-canvas');
    if (!canvas) return;
    canvas.classList.toggle('active', Boolean(visible));
  }

  function clearVisualCanvas() {
    const canvas = byId('kg-entrance-canvas');
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    ctx.clearRect(0, 0, canvas.width || 0, canvas.height || 0);
  }

  function prepareVisualCanvas() {
    const canvas = byId('kg-entrance-canvas');
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return null;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const nextWidth = Math.floor(state.width * ratio);
    const nextHeight = Math.floor(state.height * ratio);
    if (canvas.width !== nextWidth) canvas.width = nextWidth;
    if (canvas.height !== nextHeight) canvas.height = nextHeight;
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return ctx;
  }

  function cancelGraphEntranceFrame() {
    window.clearTimeout(window.__kgEntranceTimer);
    if (state.entranceFrame) {
      window.cancelAnimationFrame(state.entranceFrame);
      state.entranceFrame = 0;
    }
    state.entranceToken += 1;
  }

  function completeGraphEntrance() {
    cancelGraphEntranceFrame();
    state.entranceActive = false;
    state.entrancePlayed = true;
    const viewport = byId('kg-canvas')?.querySelector('.kg-viewport');
    viewport?.classList.add('settled');
    document.body?.classList.remove('kg-entrance-pending');
    document.body?.classList.remove('kg-entrance-active');
    document.body?.classList.add('kg-entrance-complete');
    renderVisualGraph();
  }

  function scheduleGraphEntrance() {
    if (!state.entranceActive) {
      document.body?.classList.remove('kg-entrance-pending');
      document.body?.classList.add('kg-entrance-complete');
      return;
    }
    window.clearTimeout(window.__kgEntranceTimer);
    window.__kgEntranceTimer = window.setTimeout(completeGraphEntrance, (state.entranceDuration || ENTRANCE_DURATION) + 900);
  }

  function canvasPoint(x, y) {
    return {
      x: state.width / 2 + state.panX + (x - state.width / 2) * state.scale,
      y: state.height / 2 + state.panY + (y - state.height / 2) * state.scale,
    };
  }

  function drawGraphCanvas(ctx, graph, timings = null, elapsed = Number.POSITIVE_INFINITY) {
    ctx.clearRect(0, 0, state.width, state.height);
    ctx.save();
    ctx.lineCap = 'round';

    graph.edges.forEach((edge) => {
      const delay = timings?.edgeDelay?.get(graphEdgeKey(edge)) || 0;
      const t = timings ? clamp((elapsed - delay) / 520, 0, 1) : 1;
      if (t <= 0) return;
      const a = canvasPoint(edge.sourceNode.x, edge.sourceNode.y);
      const b = canvasPoint(edge.targetNode.x, edge.targetNode.y);
      const sameBook = edge.sourceNode.properties?.book_title === edge.targetNode.properties?.book_title;
      const color = sameBook ? colorForNode(edge.sourceNode) : '#7468A6';
      ctx.globalAlpha = (sameBook ? 0.14 : 0.065) * t;
      ctx.strokeStyle = color;
      ctx.lineWidth = (['IS_SUBTYPE_OF', 'DIFFERENTIAL_FROM'].includes(edge.type) ? 0.44 : 0.28) * Math.max(0.72, state.scale);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t);
      ctx.stroke();
    });

    graph.nodes.forEach((node) => {
      const delay = timings?.nodeDelay?.get(node.id) || 0;
      const t = timings ? clamp((elapsed - delay) / 420, 0, 1) : 1;
      if (t <= 0) return;
      const p = canvasPoint(node.x, node.y);
      const color = graphNodeColor(node);
      const baseRadius = node.radius * state.scale * (0.72 + t * 0.28);
      const radius = Math.max(0.58, baseRadius);
      const prominence = clamp((node.radius - 1.45) / 15.35, 0, 1);
      const glowRadius = radius * (1.55 + prominence * 0.58);
      const glow = ctx.createRadialGradient(p.x, p.y, radius * 0.7, p.x, p.y, glowRadius);
      glow.addColorStop(0, hexToRgba(color, 0.14 + prominence * 0.14));
      glow.addColorStop(0.48, hexToRgba(color, 0.045 + prominence * 0.055));
      glow.addColorStop(1, hexToRgba(color, 0));
      ctx.globalAlpha = t;
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(p.x, p.y, glowRadius, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = (0.58 + prominence * 0.36) * t;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      ctx.fill();
    });

    const showLabels = !timings && (
      Boolean(state.activeChapter)
      || Boolean(state.selectedId)
      || state.scale > 1.3
      || graph.nodes.length <= 42
    );
    if (showLabels) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.font = `${Math.max(10, Math.min(13, 10.5 * Math.max(1, state.scale * 0.92)))}px "LXGW WenKai Screen", "Noto Sans SC", sans-serif`;
      graph.nodes.forEach((node) => {
        const selected = node.id === state.selectedId;
        if (!selected && !state.activeChapter && graph.nodes.length > 42 && state.scale <= 1.3) return;
        const p = canvasPoint(node.x, node.y);
        const radius = Math.max(1.4, node.radius * state.scale);
        const label = clip(node.label, selected ? 18 : 14);
        const y = p.y + radius + 6;
        ctx.globalAlpha = selected ? 0.98 : 0.8;
        ctx.fillStyle = selected ? '#342061' : 'rgba(71, 58, 118, 0.82)';
        ctx.fillText(label, p.x, y);
      });
      ctx.globalAlpha = 1;
    }

    ctx.restore();
    ctx.globalAlpha = 1;
  }

  function renderVisualGraph() {
    if (!state.visualGraph?.nodes?.length) {
      clearVisualCanvas();
      return;
    }
    const ctx = prepareVisualCanvas();
    if (!ctx) return;
    setVisualCanvasVisible(true);
    drawGraphCanvas(ctx, state.visualGraph);
  }

  function startGraphEntrance(graph, timings) {
    if (!state.entranceActive || state.entrancePlayed || prefersReducedMotion()) {
      completeGraphEntrance();
      return;
    }

    cancelGraphEntranceFrame();
    const ctx = prepareVisualCanvas();
    if (!ctx || !graph?.nodes?.length) {
      completeGraphEntrance();
      return;
    }

    clearVisualCanvas();
    setVisualCanvasVisible(true);

    const token = state.entranceToken;
    const startedAt = performance.now();

    const tick = (now) => {
      if (token !== state.entranceToken || !state.entranceActive) return;
      const elapsed = now - startedAt;
      drawGraphCanvas(ctx, graph, timings, elapsed);
      if (elapsed >= (state.entranceDuration || ENTRANCE_DURATION)) {
        state.entranceFrame = 0;
        completeGraphEntrance();
        return;
      }
      state.entranceFrame = window.requestAnimationFrame(tick);
    };

    state.entranceFrame = window.requestAnimationFrame(tick);
    scheduleGraphEntrance();
  }

  function relationLabel(type) {
    return RELATION_LABELS[type] || type || '相关';
  }

  function typeLabel(type) {
    return TYPE_LABELS[type] || type || '核心';
  }

  function colorForNode(nodeOrType) {
    if (typeof nodeOrType === 'string') return nodeOrType || FALLBACK_BOOK_COLOR;
    const title = nodeOrType?.properties?.book_title || nodeOrType?.book_title || '';
    if (title) return bookColor(title);
    return nodeOrType?.properties?.book_color || nodeOrType?.book_color || FALLBACK_BOOK_COLOR;
  }

  function graphNodeColor(node) {
    const title = node?.properties?.book_title || node?.book_title || '';
    return bookColor(title);
  }

  function hexToRgba(hex, alpha = 1) {
    const value = String(hex || '').replace('#', '').trim();
    if (!/^[0-9a-f]{6}$/i.test(value)) return `rgba(109, 93, 246, ${alpha})`;
    const r = parseInt(value.slice(0, 2), 16);
    const g = parseInt(value.slice(2, 4), 16);
    const b = parseInt(value.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function chapterKey(chapter) {
    return chapter.key || `${chapter.book_title || ''}::${chapter.number || ''}::${chapter.title || ''}`;
  }

  function bookColor(bookTitle) {
    if (!bookTitle) return FALLBACK_BOOK_COLOR;
    const bookIndex = state.textbooks.findIndex((book) => book.title === bookTitle);
    const paletteIndex = bookIndex >= 0
      ? bookIndex % GRAPH_NODE_COLORS.length
      : Math.floor(hashNumber(bookTitle) * GRAPH_NODE_COLORS.length) % GRAPH_NODE_COLORS.length;
    return GRAPH_NODE_COLORS[paletteIndex] || FALLBACK_BOOK_COLOR;
  }

  function ensureActiveBookTitle() {
    if (state.activeBookTitle && state.textbooks.some((book) => book.title === state.activeBookTitle)) {
      return state.activeBookTitle;
    }
    state.activeBookTitle = state.textbooks[0]?.title || '';
    return state.activeBookTitle;
  }

  function renderBookPicker() {
    const select = byId('kg-book-select');
    const trigger = byId('kg-book-trigger');
    const triggerText = byId('kg-book-trigger-text');
    const options = byId('kg-book-options');
    if (!select) return;
    const activeBookTitle = state.bookPromptVisible ? '' : ensureActiveBookTitle();
    const placeholder = state.bookPromptVisible
      ? '<option value="" selected disabled>选择教材以展开大纲</option>'
      : '';
    select.innerHTML = placeholder + (state.textbooks || []).map((book) => `
      <option value="${escapeHtml(book.title)}"${book.title === activeBookTitle ? ' selected' : ''}>
        ${escapeHtml(book.title)}
      </option>
    `).join('');
    select.disabled = !state.textbooks.length;
    if (triggerText) triggerText.textContent = activeBookTitle || '选择教材以展开大纲';
    if (trigger) {
      trigger.disabled = !state.textbooks.length;
      trigger.setAttribute('aria-expanded', options && !options.hidden ? 'true' : 'false');
    }
    if (options) {
      options.innerHTML = (state.textbooks || []).map((book) => {
        const selected = book.title === activeBookTitle;
        const color = bookColor(book.title);
        return `
          <button type="button" class="kg-book-option${selected ? ' selected' : ''}" role="option" aria-selected="${selected ? 'true' : 'false'}" data-book-title="${escapeHtml(book.title)}">
            <span class="kg-book-option-dot" style="background:${escapeHtml(color)}"></span>
            <span class="kg-book-option-title">${escapeHtml(book.title)}</span>
          </button>
        `;
      }).join('');
    }
  }

  function setBookPickerOpen(open) {
    const picker = byId('kg-book-options')?.closest('.kg-book-picker');
    const trigger = byId('kg-book-trigger');
    const options = byId('kg-book-options');
    if (!picker || !trigger || !options) return;
    const nextOpen = Boolean(open) && !trigger.disabled;
    picker.classList.toggle('open', nextOpen);
    trigger.setAttribute('aria-expanded', nextOpen ? 'true' : 'false');
    options.hidden = !nextOpen;
  }

  function chapterMatches(chapter, cores, term) {
    if (!term) return true;
    const text = [
      chapter.book_title,
      chapter.title,
      ...cores.map((item) => `${item.name} ${item.type_label || item.type || ''}`),
    ].join(' ').toLowerCase();
    return text.includes(term);
  }

  function renderChapters() {
    const container = byId('kg-chapter-list');
    if (!container) return;
    const chaptersPanel = container.closest('.kg-chapters');
    chaptersPanel?.classList.toggle('book-prompt', state.bookPromptVisible);
    if (state.bookPromptVisible) {
      container.innerHTML = '';
      return;
    }
    const term = state.search.trim().toLowerCase();
    const activeBookTitle = ensureActiveBookTitle();
    const rows = [];

    state.chapters.forEach((chapter) => {
      const bookTitle = chapter.book_title || state.textbook.title || '口腔医学教材';
      if (bookTitle !== activeBookTitle) return;
      const cores = chapter.cores || [];
      const visibleCores = cores.filter((core) => {
        if (!term) return true;
        return `${bookTitle} ${chapter.title} ${core.name} ${core.type_label || core.type || ''}`.toLowerCase().includes(term);
      });
      if (!chapterMatches(chapter, visibleCores, term) || (term && !visibleCores.length)) return;
      rows.push({ chapter, visibleCores, allCores: cores });
    });

    const html = rows.length ? `
      <section class="kg-book-block">
        ${rows.map(({ chapter, visibleCores, allCores }) => {
          const key = chapterKey(chapter);
          const active = key === state.activeChapter ? ' active' : '';
          const number = Number.isFinite(Number(chapter.number)) && Number(chapter.number) !== 9999
            ? String(chapter.number).padStart(2, '0')
            : 'CH';
          return `
            <article class="kg-chapter${active}">
              <button type="button" class="kg-chapter-btn" data-chapter-key="${escapeHtml(key)}">
                <span class="kg-chapter-no">${escapeHtml(number)}</span>
                <span class="kg-chapter-main">
                  <strong>${escapeHtml(chapter.title || '未命名章节')}</strong>
                  <em>${escapeHtml(visibleCores.length || allCores.length)} 个核心知识点</em>
                </span>
              </button>
              <div class="kg-card-list">
                ${visibleCores.map((core) => `
                  <button type="button" class="kg-card-link${core.id === state.selectedId ? ' selected' : ''}" data-core-id="${escapeHtml(core.id)}">
                    <span class="kg-card-dot" style="background:${colorForNode(core)}"></span>
                    <span>${escapeHtml(core.name)}</span>
                  </button>
                `).join('') || '<div class="kg-panel-empty compact">本章暂无核心节点</div>'}
              </div>
            </article>
          `;
        }).join('')}
      </section>
    ` : '';

    container.innerHTML = html || '<div class="kg-panel-empty">当前教材没有匹配的核心知识点。</div>';
  }

  function normalizeGraph(nodes, edges) {
    const nodeMap = new Map();
    nodes.forEach((node) => {
      if (!node?.id) return;
      nodeMap.set(node.id, {
        ...node,
        x: 0,
        y: 0,
        vx: 0,
        vy: 0,
        radius: nodeRadius(node),
      });
    });
    const normalizedEdges = edges
      .filter((edge) => nodeMap.has(edge.source) && nodeMap.has(edge.target))
      .map((edge) => ({ ...edge, sourceNode: nodeMap.get(edge.source), targetNode: nodeMap.get(edge.target) }));
    return { nodes: Array.from(nodeMap.values()), edges: normalizedEdges };
  }

  function nodeRadius(node) {
    const props = node.properties || {};
    const support = Number((props.support_relations || []).length || 0);
    const chapters = Number((props.chapter_sources || []).length || 0);
    const coreLinks = Number((props.core_relations || []).length || 0);
    const concepts = Number((props.concepts || []).length || 0);
    const weight = support * 0.58 + chapters * 3.25 + coreLinks * 2.85 + concepts * 0.42;
    return clamp(1.45 + Math.pow(weight, 0.72) * 1.32, 1.45, 16.8);
  }

  function hashNumber(text) {
    let hash = 2166136261;
    const value = String(text || '');
    for (let i = 0; i < value.length; i += 1) {
      hash ^= value.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0) / 4294967295;
  }

  function graphEdgeKey(edge) {
    return edge?.id || `${edge?.source || ''}::${edge?.target || ''}::${edge?.type || edge?.label || ''}`;
  }

  function entranceTimings(graph) {
    const active = state.entranceActive && !state.entrancePlayed;
    const timings = {
      nodeDelay: new Map(),
      edgeDelay: new Map(),
      clusterDelay: new Map(),
      duration: ENTRANCE_DURATION,
    };
    if (!active) return timings;

    const clusterByTitle = new Map((graph.clusters || []).map((cluster) => [cluster.title, cluster]));
    const degree = new Map(graph.nodes.map((node) => [node.id, 0]));
    graph.edges.forEach((edge) => {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    });
    const maxDegree = Math.max(1, ...Array.from(degree.values()));

    const sortedClusters = (graph.clusters || []).slice().sort((a, b) => {
      const aIndex = state.textbooks.findIndex((book) => book.title === a.title);
      const bIndex = state.textbooks.findIndex((book) => book.title === b.title);
      return (aIndex < 0 ? Number.MAX_SAFE_INTEGER : aIndex) - (bIndex < 0 ? Number.MAX_SAFE_INTEGER : bIndex);
    });
    sortedClusters.forEach((cluster, index) => {
      timings.clusterDelay.set(cluster.title, 80 + index * 42);
    });

    const sortedNodes = graph.nodes.map((node) => {
      const title = node.properties?.book_title || node.book_title || '';
      const cluster = clusterByTitle.get(title) || { x: state.width / 2, y: state.height / 2, radius: Math.min(state.width, state.height) * 0.34 };
      const dx = node.x - cluster.x;
      const dy = node.y - cluster.y;
      const radius = Math.max(1, cluster.radius || 1);
      const ring = Math.sqrt(dx * dx + dy * dy) / radius;
      const centrality = (degree.get(node.id) || 0) / maxDegree;
      const bookIndex = state.textbooks.findIndex((book) => book.title === title);
      const bookPhase = bookIndex >= 0 ? bookIndex / Math.max(1, state.textbooks.length) : hashNumber(title || node.id);
      const jitter = hashNumber(`${node.id}:entrance`) * 0.08;
      return {
        id: node.id,
        rank: ring - centrality * 0.38 + bookPhase * 0.18 + jitter,
      };
    }).sort((a, b) => a.rank - b.rank || String(a.id).localeCompare(String(b.id)));

    const nodeBatchCount = graph.nodes.length > 900 ? 44 : graph.nodes.length > 450 ? 34 : graph.nodes.length > 180 ? 24 : 14;
    const nodeBatchSize = Math.max(1, Math.ceil(sortedNodes.length / nodeBatchCount));
    sortedNodes.forEach((item, index) => {
      const batch = Math.min(nodeBatchCount - 1, Math.floor(index / nodeBatchSize));
      timings.nodeDelay.set(item.id, 120 + batch * 48);
    });

    const edgeSubBatchStep = 36;
    graph.edges.forEach((edge) => {
      const sourceDelay = timings.nodeDelay.get(edge.source) || 120;
      const targetDelay = timings.nodeDelay.get(edge.target) || 120;
      const subBatch = Math.floor(hashNumber(`${graphEdgeKey(edge)}:trace`) * 5);
      timings.edgeDelay.set(graphEdgeKey(edge), Math.max(sourceDelay, targetDelay) + 650 + subBatch * edgeSubBatchStep);
    });
    const maxNodeDelay = Math.max(0, ...Array.from(timings.nodeDelay.values()));
    const maxEdgeDelay = Math.max(0, ...Array.from(timings.edgeDelay.values()));
    timings.duration = Math.max(
      ENTRANCE_DURATION,
      maxNodeDelay + 760,
      maxEdgeDelay + 1160,
    );

    return timings;
  }

  function currentGraphSource() {
    const term = state.search.trim().toLowerCase();
    const source = state.graph;
    if (!term) return source;
    const keep = new Set();
    source.nodes.forEach((node) => {
      const props = node.properties || {};
      const supportText = (props.support_relations || [])
        .map((item) => `${item.name} ${item.label} ${item.type_label}`)
        .join(' ');
      const evidenceText = (props.support_relations || [])
        .flatMap((item) => item.evidence || [])
        .map((item) => `${item.exact_quote || ''} ${item.support_explanation || ''}`)
        .join(' ');
      const conceptsText = (props.concepts || []).map((item) => item.name || item.canonical_name || '').join(' ');
      const haystack = [
        node.label,
        node.group,
        props.name,
        props.type_label,
        props.book_title,
        ...(props.section_titles || []),
        supportText,
        evidenceText,
        conceptsText,
      ].join(' ').toLowerCase();
      if (haystack.includes(term)) keep.add(node.id);
    });
    source.edges.forEach((edge) => {
      const edgeText = [
        edge.label,
        edge.type,
        edge.properties?.reason,
        ...(edge.properties?.shared_supports || []).map((item) => item.name),
      ].join(' ').toLowerCase();
      if (edgeText.includes(term) || keep.has(edge.source) || keep.has(edge.target)) {
        keep.add(edge.source);
        keep.add(edge.target);
      }
    });
    return {
      nodes: source.nodes.filter((node) => keep.has(node.id)),
      edges: source.edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target)),
    };
  }

  function layoutGraph(graph) {
    const width = state.width;
    const height = state.height;
    const nodes = graph.nodes;
    const edges = graph.edges;
    if (!nodes.length) return graph;

    const centerX = width / 2;
    const centerY = height / 2;
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const degree = new Map(nodes.map((node) => [node.id, 0]));
    edges.forEach((edge) => {
      degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
    });
    const maxDegree = Math.max(1, ...Array.from(degree.values()));
    const bookTitles = (state.textbooks || [])
      .map((book) => book.title)
      .filter((title) => nodes.some((node) => node.properties?.book_title === title));
    nodes.forEach((node) => {
      const title = node.properties?.book_title || '口腔医学教材';
      if (!bookTitles.includes(title)) bookTitles.push(title);
    });

    const bookBuckets = new Map(bookTitles.map((title) => [title, []]));
    nodes.forEach((node) => {
      const title = node.properties?.book_title || '口腔医学教材';
      if (!bookBuckets.has(title)) bookBuckets.set(title, []);
      bookBuckets.get(title).push(node);
    });

    const minViewport = Math.min(width, height);
    const orbitRadius = bookTitles.length <= 1 ? 0 : Math.max(430, minViewport * 0.7);
    const orbitRx = orbitRadius * (width > height * 1.35 ? 1.72 : 1.38);
    const orbitRy = orbitRadius * 1.28;
    const clusterMap = new Map();
    bookTitles.forEach((title, index) => {
      const count = bookBuckets.get(title)?.length || 1;
      const baseAngle = (index / Math.max(bookTitles.length, 1)) * Math.PI * 2 - Math.PI / 2;
      const angle = baseAngle + (hashNumber(`${title}:orbit-angle`) - 0.5) * 0.42;
      const radius = Math.max(360, Math.min(760, 230 + Math.sqrt(count) * 37));
      const orbitalJitter = (hashNumber(`${title}:orbit`) - 0.5) * 184;
      const driftX = (hashNumber(`${title}:drift-x`) - 0.5) * 238;
      const driftY = (hashNumber(`${title}:drift-y`) - 0.5) * 196;
      clusterMap.set(title, {
        x: centerX + Math.cos(angle) * (orbitRx + orbitalJitter) + driftX,
        y: centerY + Math.sin(angle) * (orbitRy + orbitalJitter * 0.62) + driftY,
        radius,
        title,
      });
    });

    bookBuckets.forEach((bucket, title) => {
      const cluster = clusterMap.get(title) || { x: centerX, y: centerY, radius: 180 };
      const sorted = bucket.slice().sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0));
      const rotation = hashNumber(`${title}:rotation`) * Math.PI * 2;
      const eccentricity = 0.72 + hashNumber(`${title}:ecc`) * 0.42;
      sorted.forEach((node, index) => {
        const normalizedDegree = (degree.get(node.id) || 0) / maxDegree;
        const ringSeed = Math.pow(hashNumber(`${node.id}:ring`), 0.54);
        const centralPull = Math.pow(normalizedDegree, 0.72);
        const outerBias = Math.pow(index / Math.max(1, sorted.length - 1), 0.72);
        const radial = cluster.radius * (0.22 + ringSeed * 1.46 + outerBias * 0.3) * (1 - centralPull * 0.16);
        const angle = rotation
          + hashNumber(`${node.id}:angle`) * Math.PI * 2
          + Math.sin(index * 1.37 + hashNumber(`${title}:phase`) * Math.PI * 2) * 0.38;
        const warp = 0.95 + hashNumber(`${node.id}:warp`) * 0.74;
        const jitterX = (hashNumber(`${node.id}:jx`) - 0.5) * 136;
        const jitterY = (hashNumber(`${node.id}:jy`) - 0.5) * 136;
        node.anchorX = cluster.x + Math.cos(angle) * radial * warp + jitterX;
        node.anchorY = cluster.y + Math.sin(angle) * radial * eccentricity + jitterY;
        node.x = node.anchorX;
        node.y = node.anchorY;
        node.clusterX = cluster.x;
        node.clusterY = cluster.y;
        node.clusterRadius = cluster.radius;
      });
    });

    const ticks = nodes.length > 900 ? 66 : nodes.length > 650 ? 82 : nodes.length > 420 ? 102 : nodes.length > 260 ? 156 : nodes.length > 120 ? 212 : 292;
    const repulseBase = nodes.length > 900 ? 7200 : nodes.length > 420 ? 7800 : nodes.length > 260 ? 9200 : nodes.length > 80 ? 11200 : 7600;
    const useSpatialRepulsion = nodes.length > 520;
    const applyPairRepulsion = (a, b) => {
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let dist2 = dx * dx + dy * dy;
      if (dist2 < 0.1) {
        dx = hashNumber(`${a.id}:${b.id}:dx`) - 0.5;
        dy = hashNumber(`${a.id}:${b.id}:dy`) - 0.5;
        dist2 = dx * dx + dy * dy;
      }
      const dist = Math.sqrt(dist2);
      const sameBook = a.properties?.book_title === b.properties?.book_title;
      const closeCrossBook = !sameBook && dist < Math.max(a.clusterRadius || 220, b.clusterRadius || 220) * 1.48;
      if (!sameBook && !closeCrossBook) return;
      const personalSpace = 1 + ((a.radius || 3) + (b.radius || 3)) * 0.2;
      const force = Math.min(sameBook ? 10.6 : 12.4, ((sameBook ? repulseBase * 1.08 : repulseBase * 1.55) * personalSpace) / dist2);
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx -= fx;
      a.vy -= fy;
      b.vx += fx;
      b.vy += fy;
    };
    const applySpatialRepulsion = () => {
      const cellSize = nodes.length > 900 ? 210 : 188;
      const neighborSpan = 2;
      const buckets = new Map();
      nodes.forEach((node, index) => {
        const cellX = Math.floor(node.x / cellSize);
        const cellY = Math.floor(node.y / cellSize);
        const key = `${cellX}:${cellY}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(index);
      });
      buckets.forEach((indexes, key) => {
        const [cellX, cellY] = key.split(':').map(Number);
        indexes.forEach((aIndex) => {
          const a = nodes[aIndex];
          for (let ox = -neighborSpan; ox <= neighborSpan; ox += 1) {
            for (let oy = -neighborSpan; oy <= neighborSpan; oy += 1) {
              const neighborIndexes = buckets.get(`${cellX + ox}:${cellY + oy}`);
              if (!neighborIndexes) continue;
              neighborIndexes.forEach((bIndex) => {
                if (bIndex <= aIndex) return;
                applyPairRepulsion(a, nodes[bIndex]);
              });
            }
          }
        });
      });
    };
    for (let tick = 0; tick < ticks; tick += 1) {
      if (useSpatialRepulsion) {
        applySpatialRepulsion();
      } else {
        for (let i = 0; i < nodes.length; i += 1) {
          for (let j = i + 1; j < nodes.length; j += 1) {
            applyPairRepulsion(nodes[i], nodes[j]);
          }
        }
      }

      edges.forEach((edge) => {
        const a = edge.sourceNode;
        const b = edge.targetNode;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const sameBook = a.properties?.book_title === b.properties?.book_title;
        const desired = sameBook
          ? edge.type === 'IS_SUBTYPE_OF' ? 232 : edge.type === 'DIFFERENTIAL_FROM' ? 322 : 284
          : edge.type === 'DIFFERENTIAL_FROM' ? orbitRadius * 1.54 : orbitRadius * 1.34;
        const force = (dist - desired) * (sameBook ? 0.00142 : 0.00018);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      });

      nodes.forEach((node) => {
        const title = node.properties?.book_title || '口腔医学教材';
        const cluster = clusterMap.get(title) || { x: centerX, y: centerY, radius: 180 };
        const normalizedDegree = (degree.get(node.id) || 0) / maxDegree;
        const anchorPull = 0.00092 + (1 - normalizedDegree) * 0.00026;
        const clusterPull = 0.00005 + normalizedDegree * 0.00026;
        node.vx += ((node.anchorX || cluster.x) - node.x) * anchorPull;
        node.vy += ((node.anchorY || cluster.y) - node.y) * anchorPull;
        node.vx += (cluster.x - node.x) * clusterPull + (centerX - node.x) * 0.000026;
        node.vy += (cluster.y - node.y) * clusterPull + (centerY - node.y) * 0.000026;
        node.vx *= 0.86;
        node.vy *= 0.86;
        node.x += node.vx;
        node.y += node.vy;

        const dx = node.x - cluster.x;
        const dy = node.y - cluster.y;
        const clusterDistance = Math.sqrt(dx * dx + dy * dy);
        const maxClusterDistance = cluster.radius * 2.74;
        if (clusterDistance > maxClusterDistance) {
          const pull = (clusterDistance - maxClusterDistance) / clusterDistance * 0.07;
          node.x -= dx * pull;
          node.y -= dy * pull;
        }

        const normalizedX = (node.x - centerX) / (width * 1.38);
        const normalizedY = (node.y - centerY) / (height * 1.24);
        const graphDistance = Math.sqrt(normalizedX * normalizedX + normalizedY * normalizedY);
        if (graphDistance > 1) {
          const pull = (graphDistance - 1) * 0.035;
          node.x += (centerX - node.x) * pull;
          node.y += (centerY - node.y) * pull;
        }
      });
    }

    graph.clusters = Array.from(clusterMap.values());
    return graph;
  }

  function graphLayoutKey(source) {
    return [
      state.width,
      state.height,
      source.nodes.map((node) => node.id).sort().join(','),
      source.edges.map((edge) => graphEdgeKey(edge)).sort().join(','),
    ].join('|');
  }

  function preserveLayoutSubset(source, baseGraph) {
    if (!baseGraph?.nodes?.length || !source?.nodes?.length) return null;
    const baseNodeById = new Map(baseGraph.nodes.map((node) => [node.id, node]));
    const nodes = [];
    for (const node of source.nodes) {
      const baseNode = baseNodeById.get(node.id);
      if (!baseNode) return null;
      nodes.push({
        ...node,
        x: baseNode.x,
        y: baseNode.y,
        vx: 0,
        vy: 0,
        radius: baseNode.radius || nodeRadius(node),
        anchorX: baseNode.anchorX,
        anchorY: baseNode.anchorY,
        clusterX: baseNode.clusterX,
        clusterY: baseNode.clusterY,
        clusterRadius: baseNode.clusterRadius,
      });
    }
    const nodeMap = new Map(nodes.map((node) => [node.id, node]));
    const edges = source.edges
      .filter((edge) => nodeMap.has(edge.source) && nodeMap.has(edge.target))
      .map((edge) => ({ ...edge, sourceNode: nodeMap.get(edge.source), targetNode: nodeMap.get(edge.target) }));
    const titles = new Set(nodes.map((node) => node.properties?.book_title || node.book_title || '口腔医学教材'));
    const clusters = (baseGraph.clusters || [])
      .filter((cluster) => titles.has(cluster.title))
      .map((cluster) => ({ ...cluster }));
    return { nodes, edges, clusters };
  }

  function overviewLayout() {
    const source = state.overview || { nodes: [], edges: [] };
    if (!source.nodes?.length) return null;
    const key = graphLayoutKey(source);
    if (state.overviewLayout && state.overviewLayoutKey === key) return state.overviewLayout;
    state.overviewLayoutKey = key;
    state.overviewLayout = layoutGraph(normalizeGraph(source.nodes || [], source.edges || []));
    return state.overviewLayout;
  }

  function layoutEdgeLabels(edges) {
    const placed = [];
    return edges.map((edge, index) => {
      const label = edge.label || relationLabel(edge.type);
      const midX = (edge.sourceNode.x + edge.targetNode.x) / 2;
      const midY = (edge.sourceNode.y + edge.targetNode.y) / 2;
      const dx = edge.targetNode.x - edge.sourceNode.x;
      const dy = edge.targetNode.y - edge.sourceNode.y;
      const length = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const normalX = -dy / length;
      const normalY = dx / length;
      const width = Math.max(34, String(label).length * 10.2);
      const height = 15;
      const preferredSide = index % 2 === 0 ? 1 : -1;
      let best = { x: midX, y: midY, label, width, height };

      for (let step = 0; step < 10; step += 1) {
        const side = step % 2 === 0 ? preferredSide : -preferredSide;
        const distance = Math.ceil(step / 2) * 12;
        const x = midX + normalX * distance * side;
        const y = midY + normalY * distance * side;
        const candidate = { x, y, label, width, height };
        const overlaps = placed.some((item) => (
          Math.abs(item.x - candidate.x) < (item.width + candidate.width) / 2 + 6
          && Math.abs(item.y - candidate.y) < (item.height + candidate.height) / 2 + 4
        ));
        best = candidate;
        if (!overlaps) break;
      }

      placed.push(best);
      return [edge.id, best];
    });
  }

  function graphTransform() {
    const tx = state.width / 2 + state.panX;
    const ty = state.height / 2 + state.panY;
    return `translate(${tx.toFixed(1)} ${ty.toFixed(1)}) scale(${state.scale.toFixed(3)}) translate(${-state.width / 2} ${-state.height / 2})`;
  }

  function applyGraphTransform() {
    const viewport = byId('kg-canvas')?.querySelector('.kg-viewport');
    if (!viewport) return;
    viewport.setAttribute('transform', graphTransform());
    if (state.entrancePlayed && !state.entranceActive) renderVisualGraph();
  }

  function scheduleGraphTransform() {
    if (state.transformFrame) return;
    state.transformFrame = window.requestAnimationFrame(() => {
      state.transformFrame = 0;
      applyGraphTransform();
    });
  }

  function cancelViewAnimation() {
    if (state.viewAnimationFrame) {
      window.cancelAnimationFrame(state.viewAnimationFrame);
      state.viewAnimationFrame = 0;
    }
    state.viewAnimating = false;
    if (state.viewAnimationResolve) {
      const resolve = state.viewAnimationResolve;
      state.viewAnimationResolve = null;
      resolve(false);
    }
  }

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function animateViewTo(target, duration = 860) {
    cancelViewAnimation();
    if (prefersReducedMotion()) {
      state.scale = target.scale;
      state.panX = target.panX;
      state.panY = target.panY;
      applyGraphTransform();
      return Promise.resolve();
    }

    const start = {
      scale: state.scale,
      panX: state.panX,
      panY: state.panY,
    };
    const startedAt = performance.now();
    state.viewAnimating = true;
    return new Promise((resolve) => {
      state.viewAnimationResolve = resolve;
      const tick = (now) => {
        const t = Math.min(1, (now - startedAt) / duration);
        const eased = easeInOutCubic(t);
        state.scale = start.scale + (target.scale - start.scale) * eased;
        state.panX = start.panX + (target.panX - start.panX) * eased;
        state.panY = start.panY + (target.panY - start.panY) * eased;
        applyGraphTransform();
        if (t < 1) {
          state.viewAnimationFrame = window.requestAnimationFrame(tick);
          return;
        }
        state.viewAnimationFrame = 0;
        state.viewAnimating = false;
        state.viewAnimationResolve = null;
        resolve(true);
      };

      state.viewAnimationFrame = window.requestAnimationFrame(tick);
    });
  }

  function viewForBounds(nodes, scaleLimits = {}) {
    if (!nodes?.length) {
      return { scale: state.scale, panX: state.panX, panY: state.panY };
    }
    const padding = scaleLimits.padding ?? 130;
    const minScale = scaleLimits.minScale ?? 0.54;
    const maxScale = scaleLimits.maxScale ?? 1.2;
    const bounds = nodes.reduce((acc, node) => ({
      minX: Math.min(acc.minX, node.x - (node.radius || 0)),
      maxX: Math.max(acc.maxX, node.x + (node.radius || 0)),
      minY: Math.min(acc.minY, node.y - (node.radius || 0)),
      maxY: Math.max(acc.maxY, node.y + (node.radius || 0)),
    }), {
      minX: Number.POSITIVE_INFINITY,
      maxX: Number.NEGATIVE_INFINITY,
      minY: Number.POSITIVE_INFINITY,
      maxY: Number.NEGATIVE_INFINITY,
    });
    const width = Math.max(1, bounds.maxX - bounds.minX);
    const height = Math.max(1, bounds.maxY - bounds.minY);
    const availableWidth = Math.max(220, state.width - padding * 2);
    const availableHeight = Math.max(220, state.height - padding * 2);
    const scale = clamp(Math.min(availableWidth / width, availableHeight / height), minScale, maxScale);
    const centerX = (bounds.minX + bounds.maxX) / 2;
    const centerY = (bounds.minY + bounds.maxY) / 2;
    return {
      scale,
      panX: (state.width / 2 - centerX) * scale,
      panY: (state.height / 2 - centerY) * scale,
    };
  }

  function fitOverviewView(graph) {
    if (!graph?.nodes?.length) return;
    const nextView = viewForBounds(graph.nodes, {
      minScale: OVERVIEW_MIN_SCALE,
      maxScale: OVERVIEW_MAX_SCALE,
      padding: Math.max(42, Math.min(88, Math.min(state.width, state.height) * 0.07)),
    });
    state.scale = nextView.scale;
    state.panX = nextView.panX;
    state.panY = nextView.panY;
  }

  function renderGraph(options = {}) {
    const svg = byId('kg-canvas');
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    state.width = Math.max(720, Math.floor(rect.width || 900));
    state.height = Math.max(560, Math.floor(rect.height || 620));
    svg.setAttribute('viewBox', `0 0 ${state.width} ${state.height}`);

    const source = currentGraphSource();
    const layoutKey = graphLayoutKey(source);
    if (state.layoutKey !== layoutKey || !state.laidOutGraph) {
      state.layoutKey = layoutKey;
      state.laidOutGraph = options.preservedLayout || layoutGraph(normalizeGraph(source.nodes, source.edges));
      if (!state.search.trim() && source === state.overview) {
        state.overviewLayout = state.laidOutGraph;
        state.overviewLayoutKey = layoutKey;
      }
    }
    const graph = state.laidOutGraph;
    if (options.fitOverview) fitOverviewView(graph);
    if (!graph.nodes.length) {
      svg.innerHTML = '';
      state.visualGraph = null;
      clearVisualCanvas();
      setEmpty('没有匹配的核心知识点。');
      const graphMeta = byId('kg-graph-meta');
      if (graphMeta) graphMeta.textContent = '0 个核心';
      return;
    }
    setEmpty('', true);
    const graphMeta = byId('kg-graph-meta');
    if (graphMeta) graphMeta.textContent = `${graph.nodes.length} 个核心 · ${graph.edges.length} 条联系`;

    const transform = graphTransform();
    const selectedEdges = new Set();
    graph.edges.forEach((edge) => {
      if (edge.source === state.selectedId || edge.target === state.selectedId) selectedEdges.add(edge.id);
    });
    const labeledEdges = graph.edges.filter((edge) => selectedEdges.has(edge.id));
    const edgeLabels = new Map(layoutEdgeLabels(labeledEdges));
    const entrance = entranceTimings(graph);
    const entranceActive = state.entranceActive && !state.entrancePlayed && !prefersReducedMotion();
    if (entranceActive) {
      state.entranceDuration = entrance.duration || ENTRANCE_DURATION;
    }

    const edgeHtml = graph.edges.map((edge) => {
      const selected = selectedEdges.has(edge.id);
      const strong = ['IS_SUBTYPE_OF', 'DIFFERENTIAL_FROM'].includes(edge.type);
      const label = edge.label || relationLabel(edge.type);
      const labelPlacement = edgeLabels.get(edge.id);
      const sameBook = edge.sourceNode.properties?.book_title === edge.targetNode.properties?.book_title;
      const edgeColor = sameBook ? colorForNode(edge.sourceNode) : '#7468A6';
      return `
        <g class="kg-edge-group${sameBook ? '' : ' cross-book'}${strong ? ' strong' : ''}${selected ? ' selected' : ''}" style="--edge-color:${escapeHtml(edgeColor)}">
          <line class="kg-edge" pathLength="1" x1="${edge.sourceNode.x.toFixed(1)}" y1="${edge.sourceNode.y.toFixed(1)}" x2="${edge.targetNode.x.toFixed(1)}" y2="${edge.targetNode.y.toFixed(1)}"></line>
          ${selected && labelPlacement ? `<text class="kg-edge-label" text-anchor="middle" x="${labelPlacement.x.toFixed(1)}" y="${labelPlacement.y.toFixed(1)}">${escapeHtml(label)}</text>` : ''}
        </g>
      `;
    }).join('');

    const clusterHtml = (graph.clusters || []).map((cluster) => {
      const color = bookColor(cluster.title);
      return `
        <g class="kg-cluster" style="--cluster-color:${escapeHtml(color)}">
          <circle class="kg-cluster-aura" cx="${cluster.x.toFixed(1)}" cy="${cluster.y.toFixed(1)}" r="${(cluster.radius * 1.72).toFixed(1)}" fill="${escapeHtml(color)}"></circle>
          <circle class="kg-cluster-glow" cx="${cluster.x.toFixed(1)}" cy="${cluster.y.toFixed(1)}" r="${(cluster.radius * 1.18).toFixed(1)}" fill="${escapeHtml(color)}"></circle>
        </g>
      `;
    }).join('');

    const nodeHtml = graph.nodes.map((node) => {
      const selected = node.id === state.selectedId ? ' selected' : '';
      const label = selected || state.scale > 1.3 || graph.nodes.length <= 28 ? clip(node.label, 16) : '';
      const color = graphNodeColor(node);
      const glowRadius = node.radius * (selected ? 1.32 : 1.18);
      const labelY = node.radius + 11;
      return `
        <g class="kg-node core${selected}" data-core-id="${escapeHtml(node.id)}" transform="translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})" style="--node-color:${escapeHtml(color)}">
          <circle class="kg-node-glow" r="${glowRadius.toFixed(1)}" fill="${escapeHtml(color)}"></circle>
          <circle class="kg-node-core" r="${node.radius.toFixed(1)}" fill="${escapeHtml(color)}"></circle>
          ${label ? `<text text-anchor="middle" y="${labelY.toFixed(1)}">${escapeHtml(label)}</text>` : ''}
        </g>
      `;
    }).join('');

    const viewportClass = [
      'kg-viewport',
      state.entrancePlayed || !state.entranceActive ? 'settled' : '',
    ].filter(Boolean).join(' ');

    svg.innerHTML = `
      <rect class="kg-pan-surface" width="${state.width}" height="${state.height}"></rect>
      <g class="${viewportClass}" transform="${transform}">
        <g class="kg-layer kg-layer-clusters">${clusterHtml}</g>
        <g class="kg-layer kg-layer-edges">${edgeHtml}</g>
        <g class="kg-layer kg-layer-nodes">${nodeHtml}</g>
      </g>
    `;

    state.visualGraph = graph;
    if (entranceActive) startGraphEntrance(graph, entrance);
    else renderVisualGraph();
  }

  function renderDetail(result) {
    const node = result.node || result.center || null;
    const card = result.card || node?.properties || null;
    if (!node || !card) return false;
    state.selectedId = node.id;
    state.activeChapter = Array.isArray(card.chapter_keys) ? card.chapter_keys[0] : state.activeChapter;
    byId('kg-detail-title').textContent = card.name || node.label || node.id;
    byId('kg-detail-type').textContent = card.type_label || typeLabel(card.type) || '核心';

    const relationRows = detailRelationRows(result, node.id);
    const related = detailRelatedNodes(result, node.id, relationRows);

    byId('kg-detail-body').innerHTML = tabbedCard(card, relationRows, related);
    renderChapters();
    renderGraph();
    return true;
  }

  function detailRelationRows(result, centerId) {
    const directRows = [...(result.outgoing || []), ...(result.incoming || [])];
    const fallbackRows = (result.edges || []).filter((edge) => edge.source === centerId || edge.target === centerId);
    const nodeLookup = new Map([
      ...(result.nodes || []),
      ...(result.related_nodes || []),
    ].filter(Boolean).map((item) => [item.id, item]));
    const seen = new Set();
    return (directRows.length ? directRows : fallbackRows)
      .filter((edge) => edge?.source && edge?.target)
      .filter((edge) => {
        const key = edge.id || `${edge.source}->${edge.target}:${edge.type || ''}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .map((edge) => {
        const adjacentId = edge.source === centerId ? edge.target : edge.source;
        const adjacentNode = nodeLookup.get(adjacentId);
        return {
          ...edge,
          adjacent_id: adjacentId,
          adjacent_label: adjacentNode?.label || adjacentNode?.properties?.name || adjacentId,
          adjacent_type_label: adjacentNode?.properties?.type_label || adjacentNode?.group || '核心',
        };
      })
      .slice(0, 32);
  }

  function detailRelatedNodes(result, centerId, relationRows) {
    const nodeLookup = new Map([
      ...(result.related_nodes || []),
      ...(result.nodes || []),
    ].filter(Boolean).map((item) => [item.id, item]));
    const related = [];
    const seen = new Set([centerId]);

    relationRows.forEach((edge) => {
      const adjacentId = edge.source === centerId ? edge.target : edge.source;
      const item = nodeLookup.get(adjacentId);
      if (!item || seen.has(item.id)) return;
      seen.add(item.id);
      related.push(item);
    });

    if (!related.length) {
      (result.related_nodes || []).forEach((item) => {
        if (!item?.id || seen.has(item.id)) return;
        seen.add(item.id);
        related.push(item);
      });
    }

    return related.slice(0, 14);
  }

  function tabbedCard(card, relationRows, related) {
    const tabs = [
      {
        key: 'overview',
        label: '概览',
        html: [
          summarySection(card),
          evidenceOverviewSection(card),
        ].join(''),
      },
      {
        key: 'medical',
        label: '医学关系',
        html: supportGroupSection(card, PREDICATE_GROUPS[0], '病因与风险')
          + supportGroupSection(card, PREDICATE_GROUPS[2], '影像与病理'),
      },
      {
        key: 'clinical',
        label: '诊断表现',
        html: supportGroupSection(card, PREDICATE_GROUPS[1], '症状与体征')
          + supportGroupSection(card, PREDICATE_GROUPS[3], '诊断与治疗')
          + supportGroupSection(card, ['HAS_COMPLICATION', 'LOCATED_IN'], '并发与部位')
          + supportGroupSection(card, ['DIFFERENTIAL_FROM', 'IS_SUBTYPE_OF'], '鉴别与分型'),
      },
      {
        key: 'network',
        label: '图谱联系',
        html: [
          relationRows.length ? relationSection(relationRows, card.id) : '',
          related.length ? relatedSection(related) : '',
        ].join(''),
      },
    ].map((tab) => ({
      ...tab,
      html: tab.html.trim() || '<div class="kg-card-empty">这张书签暂无可展示内容。</div>',
    }));

    return `
      <div class="kg-card-tabs" role="tablist" aria-label="核心知识卡片书签">
        ${tabs.map((tab, index) => `
          <button
            type="button"
            class="kg-card-tab${index === 0 ? ' active' : ''}"
            role="tab"
            aria-selected="${index === 0 ? 'true' : 'false'}"
            data-card-tab="${escapeHtml(tab.key)}">
            ${escapeHtml(tab.label)}
          </button>
        `).join('')}
      </div>
      <div class="kg-card-stage">
        ${tabs.map((tab, index) => `
          <section
            class="kg-card-page${index === 0 ? ' active' : ''}"
            role="tabpanel"
            data-card-panel="${escapeHtml(tab.key)}"
            ${index === 0 ? '' : 'hidden'}>
            ${tab.html}
          </section>
        `).join('')}
      </div>
    `;
  }

  function summarySection(card) {
    const chapters = (card.chapter_sources || []).map((item) => item.title).filter(Boolean);
    const sections = (card.section_titles || []).filter(Boolean);
    const aliases = (card.aliases || []).filter(Boolean);
    return `
      <section class="kg-detail-card kg-summary-card">
        <h4>核心知识点</h4>
        <p>${escapeHtml(card.name || card.canonical_name || '未命名核心知识点')}</p>
        <div class="kg-mini-meta">
          <span style="background:${escapeHtml(card.book_soft_color || '#F1E3F0')};color:${escapeHtml(card.book_color || '#5F526E')}">${escapeHtml(card.book_title || '口腔医学教材')}</span>
          <span>${escapeHtml(card.type_label || typeLabel(card.type))}</span>
          ${chapters.slice(0, 2).map((item) => `<span>${escapeHtml(item)}</span>`).join('')}
        </div>
      </section>
      ${aliases.length ? listSection('别名', aliases) : ''}
      ${sections.length ? listSection('来源位置', sections) : ''}
    `;
  }

  function listSection(title, values) {
    const items = Array.isArray(values) ? values.filter(Boolean) : [];
    if (!items.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>${escapeHtml(title)}</h4>
        <ul class="kg-point-list">
          ${items.map((item) => `<li>${escapeHtml(typeof item === 'string' ? item : JSON.stringify(item))}</li>`).join('')}
        </ul>
      </section>
    `;
  }

  function evidenceOverviewSection(card) {
    const evidence = (card.support_relations || [])
      .flatMap((relation) => (relation.evidence || []).map((item) => ({ ...item, relation })))
      .filter((item) => item.exact_quote || item.support_explanation)
      .slice(0, 3);
    if (!evidence.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>教材概述</h4>
        <div class="kg-evidence-summary">
          ${evidence.map((item) => `
            <p>${escapeHtml(item.support_explanation || item.exact_quote || '')}</p>
          `).join('')}
        </div>
      </section>
    `;
  }

  function supportGroupSection(card, predicates, title) {
    const supportByPredicate = card.support_by_predicate || {};
    const blocks = predicates.map((predicate) => {
      const rows = supportByPredicate[predicate] || [];
      if (!rows.length) return '';
      return `
        <div class="kg-support-block">
          <strong>${escapeHtml(relationLabel(predicate))}</strong>
          <div class="kg-chip-list">
            ${rows.slice(0, 18).map((item) => `
              <span class="kg-support-chip">${escapeHtml(item.name || item.canonical_name || '')}</span>
            `).join('')}
          </div>
        </div>
      `;
    }).join('');
    if (!blocks.trim()) return '';
    return `
      <section class="kg-detail-card">
        <h4>${escapeHtml(title)}</h4>
        ${blocks}
      </section>
    `;
  }

  function sourceSection(card) {
    const sources = Array.isArray(card.source_structures) ? card.source_structures.slice(0, 8) : [];
    if (!sources.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>来源结构</h4>
        <div class="kg-source-list">
          ${sources.map((item) => `
            <article class="kg-source-item">
              <strong>${escapeHtml(item.chapter_title || '未命名章节')}</strong>
              <span>${escapeHtml(item.book_title || '')}${item.section_title ? ` · ${escapeHtml(item.section_title)}` : ''}</span>
            </article>
          `).join('')}
        </div>
      </section>
    `;
  }

  function evidenceSection(card) {
    const evidence = (card.support_relations || [])
      .flatMap((relation) => (relation.evidence || []).map((item) => ({ ...item, relation })))
      .filter((item) => item.exact_quote || item.support_explanation)
      .slice(0, 10);
    const spanIds = (card.source_span_ids || []).slice(0, 16);
    if (!evidence.length && !spanIds.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>教材证据</h4>
        ${evidence.length ? `
          <div class="kg-evidence-list">
            ${evidence.map((item) => `
              <article class="kg-evidence">
                <span>${escapeHtml(item.printed_page_number || item.pdf_page_index || item.span_id || '-')}</span>
                <p>${escapeHtml(item.exact_quote || item.support_explanation || '')}</p>
              </article>
            `).join('')}
          </div>
        ` : `<p>当前样例未携带 exact_quote；完整草稿 JSON 接入后会在这里展示证据摘录。</p>`}
        ${spanIds.length ? `<div class="kg-mini-meta">${spanIds.map((id) => `<span>${escapeHtml(id)}</span>`).join('')}</div>` : ''}
      </section>
    `;
  }

  function relationSection(edges, centerId) {
    return `
      <section class="kg-detail-card">
        <h4>核心联系</h4>
        <div class="kg-relation-list">
          ${edges.map((edge) => {
            const shared = edge.properties?.shared_supports || [];
            const direction = edge.source === centerId ? '指向' : '来自';
            const adjacent = edge.adjacent_label || edge.adjacent_id || '';
            const reason = shared.length
              ? shared.slice(0, 4).map((item) => item.name).join('、')
              : edge.properties?.reason
                || (edge.properties?.evidence || []).map((item) => item.support_explanation || item.exact_quote).filter(Boolean)[0]
                || '';
            return `
              <article class="kg-relation-item">
                <strong>${escapeHtml(edge.label || relationLabel(edge.type))}</strong>
                <span>${escapeHtml(adjacent ? `${direction}：${adjacent}` : direction)}</span>
                ${edge.adjacent_type_label ? `<em>${escapeHtml(edge.adjacent_type_label)}</em>` : ''}
                <p>${escapeHtml(reason)}</p>
              </article>
            `;
          }).join('')}
        </div>
      </section>
    `;
  }

  function relatedSection(related) {
    return `
      <section class="kg-detail-card">
        <h4>相邻核心</h4>
        <div class="kg-related-list">
          ${related.map((item) => `
            <button type="button" class="kg-related-btn" data-related-id="${escapeHtml(item.id)}">
              ${escapeHtml(item.label || item.properties?.name || item.id)}
              <span>${escapeHtml(item.properties?.type_label || item.group || '核心')}</span>
            </button>
          `).join('')}
        </div>
      </section>
    `;
  }

  function conceptsSection(card) {
    const concepts = Array.isArray(card.concepts) ? card.concepts.slice(0, 12) : [];
    if (!concepts.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>展示层概念</h4>
        <div class="kg-chip-list">
          ${concepts.map((item) => `<span class="kg-support-chip">${escapeHtml(item.name || item.canonical_name || '')}</span>`).join('')}
        </div>
      </section>
    `;
  }

  function droppedSection(card) {
    const rows = Array.isArray(card.dropped_items) ? card.dropped_items.slice(0, 8) : [];
    if (!rows.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>暂不入图</h4>
        <ul class="kg-point-list">
          ${rows.map((item) => `<li>${escapeHtml(item.candidate || item.name || '')}</li>`).join('')}
        </ul>
      </section>
    `;
  }

  function reviewSection(card) {
    const summaries = Array.isArray(card.review_summaries) ? card.review_summaries : [];
    if (!summaries.length) return '';
    return `
      <section class="kg-detail-card">
        <h4>复核摘要</h4>
        <div class="kg-source-list">
          ${summaries.slice(0, 3).map((item) => `
            <article class="kg-source-item">
              <strong>${escapeHtml(item.verdict || '待复核')}</strong>
              <span>${escapeHtml((item.warnings || []).join('；') || '暂无警告')}</span>
            </article>
          `).join('')}
        </div>
      </section>
    `;
  }

  async function showOverview() {
    cancelViewAnimation();
    state.bookFlightId += 1;
    state.activeChapter = null;
    state.selectedId = '';
    state.activeBookTitle = '';
    state.bookPromptVisible = true;
    closeDetailPanel();
    state.graph = {
      nodes: state.overview.nodes || [],
      edges: state.overview.edges || [],
    };
    const graphTitle = byId('kg-graph-title');
    if (graphTitle) graphTitle.textContent = '全书核心关系网';
    byId('kg-overview-btn')?.classList.add('active');
    state.scale = OVERVIEW_MAX_SCALE;
    state.panX = 0;
    state.panY = 0;
    state.layoutKey = '';
    renderBookPicker();
    renderChapters();
    renderGraph({ fitOverview: true });
  }

  async function selectBook(bookTitle) {
    if (!bookTitle) return;
    if (state.entranceActive) completeGraphEntrance();
    cancelViewAnimation();
    const flightId = state.bookFlightId + 1;
    state.bookFlightId = flightId;
    const baseLayout = overviewLayout() || state.laidOutGraph;
    const book = state.textbooks.find((item) => item.title === bookTitle);
    const coreIds = new Set(book?.core_ids || []);
    const neighborIds = new Set(coreIds);
    (state.overview.edges || []).forEach((edge) => {
      if (coreIds.has(edge.source) || coreIds.has(edge.target)) {
        neighborIds.add(edge.source);
        neighborIds.add(edge.target);
      }
    });
    const targetGraph = {
      nodes: (state.overview.nodes || []).filter((node) => neighborIds.has(node.id)),
      edges: (state.overview.edges || []).filter((edge) => neighborIds.has(edge.source) && neighborIds.has(edge.target)),
    };
    const preservedLayout = preserveLayoutSubset(targetGraph, baseLayout);
    const targetNodes = (preservedLayout?.nodes || []).filter((node) => (
      (node.properties?.book_title || node.book_title || '') === bookTitle
    ));
    if (targetNodes.length) {
      await animateViewTo(viewForBounds(targetNodes, {
        minScale: 0.42,
        maxScale: 0.9,
        padding: 220,
      }), 940);
    }
    if (flightId !== state.bookFlightId) return;
    state.activeBookTitle = bookTitle;
    state.activeChapter = null;
    state.selectedId = '';
    state.bookPromptVisible = false;
    closeDetailPanel();
    state.graph = targetGraph;
    const graphTitle = byId('kg-graph-title');
    if (graphTitle) graphTitle.textContent = `${bookTitle}核心关系网`;
    byId('kg-overview-btn')?.classList.remove('active');
    state.layoutKey = '';
    renderBookPicker();
    renderChapters();
    renderGraph({ preservedLayout });
  }

  async function selectChapter(key) {
    const chapter = state.chapters.find((item) => chapterKey(item) === key);
    if (!chapter) return;
    cancelViewAnimation();
    state.bookFlightId += 1;
    const coreIds = new Set(chapter.core_ids || (chapter.cores || []).map((item) => item.id));
    state.activeBookTitle = chapter.book_title || state.activeBookTitle;
    state.activeChapter = chapterKey(chapter);
    state.selectedId = '';
    state.bookPromptVisible = false;
    closeDetailPanel();
    byId('kg-overview-btn')?.classList.remove('active');
    const graphTitle = byId('kg-graph-title');
    if (graphTitle) graphTitle.textContent = chapter.title || '章节核心关系网';
    const nodes = (state.overview.nodes || []).filter((node) => coreIds.has(node.id));
    const neighborIds = new Set(coreIds);
    (state.overview.edges || []).forEach((edge) => {
      if (coreIds.has(edge.source) || coreIds.has(edge.target)) {
        neighborIds.add(edge.source);
        neighborIds.add(edge.target);
      }
    });
    state.graph = {
      nodes: (state.overview.nodes || []).filter((node) => neighborIds.has(node.id)),
      edges: (state.overview.edges || []).filter((edge) => neighborIds.has(edge.source) && neighborIds.has(edge.target)),
    };
    if (!state.graph.nodes.length) state.graph.nodes = nodes;
    state.scale = 0.72;
    state.panX = 0;
    state.panY = 0;
    state.layoutKey = '';
    renderBookPicker();
    renderChapters();
    renderGraph();
  }

  async function selectNode(nodeId, expand = true) {
    if (!nodeId) return;
    if (state.entranceActive) completeGraphEntrance();
    cancelViewAnimation();
    state.bookFlightId += 1;
    state.selectedId = nodeId;
    const result = expand
      ? await apiPost('/api/knowledge-graph/neighborhood', { node_id: nodeId, depth: 1, limit: 120 })
      : await apiGet(`/api/knowledge-graph/node/${encodeURIComponent(nodeId)}`);
    const cardBookTitle = result.card?.book_title || result.node?.properties?.book_title || result.center?.properties?.book_title || '';
    if (cardBookTitle) state.activeBookTitle = cardBookTitle;
    if (cardBookTitle) state.bookPromptVisible = false;
    if (expand) {
      state.graph = {
        nodes: result.nodes || [],
        edges: result.edges || [],
      };
      const graphTitle = byId('kg-graph-title');
      if (graphTitle) graphTitle.textContent = result.card?.name || result.center?.label || '核心邻域';
      byId('kg-overview-btn')?.classList.remove('active');
      state.scale = 1.02;
      state.panX = 0;
      state.panY = 0;
      state.layoutKey = '';
    }
    renderBookPicker();
    if (renderDetail(result)) setDetailPanelOpen(true);
  }

  function zoomBy(delta) {
    cancelViewAnimation();
    const factor = delta > 0 ? 1.12 : 1 / 1.12;
    state.scale = clamp(state.scale * factor, MIN_GRAPH_SCALE, MAX_GRAPH_SCALE);
    applyGraphTransform();
  }

  function zoomAt(delta, clientX, clientY) {
    cancelViewAnimation();
    const svg = byId('kg-canvas');
    const rect = svg?.getBoundingClientRect();
    if (!rect) {
      zoomBy(delta);
      return;
    }
    const previousScale = state.scale;
    const factor = delta > 0 ? 1.08 : 1 / 1.08;
    const nextScale = clamp(state.scale * factor, MIN_GRAPH_SCALE, MAX_GRAPH_SCALE);
    if (nextScale === previousScale) return;
    const sx = clientX - rect.left;
    const sy = clientY - rect.top;
    const graphX = state.width / 2 + (sx - state.width / 2 - state.panX) / previousScale;
    const graphY = state.height / 2 + (sy - state.height / 2 - state.panY) / previousScale;
    state.scale = nextScale;
    state.panX = sx - state.width / 2 - (graphX - state.width / 2) * nextScale;
    state.panY = sy - state.height / 2 - (graphY - state.height / 2) * nextScale;
    applyGraphTransform();
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function moveDetailCard(left, top) {
    const detail = byId('kg-detail-body')?.closest('.kg-detail');
    const workspace = document.querySelector('.kg-workspace');
    if (!detail || !workspace) return;
    const workspaceRect = workspace.getBoundingClientRect();
    const cardRect = detail.getBoundingClientRect();
    const margin = 12;
    const maxLeft = Math.max(margin, workspaceRect.width - cardRect.width - margin);
    const maxTop = Math.max(margin, workspaceRect.height - cardRect.height - margin);
    detail.style.left = `${clamp(left, margin, maxLeft)}px`;
    detail.style.top = `${clamp(top, margin, maxTop)}px`;
    detail.style.right = 'auto';
    detail.style.bottom = 'auto';
  }

  function resetDetailCardIfNeeded() {
    const detail = byId('kg-detail-body')?.closest('.kg-detail');
    const workspace = document.querySelector('.kg-workspace');
    if (!detail || !workspace || !detail.style.left || !detail.style.top) return;
    moveDetailCard(parseFloat(detail.style.left), parseFloat(detail.style.top));
  }

  function toggleDetailMinimized() {
    const detail = byId('kg-detail-body')?.closest('.kg-detail');
    const button = byId('kg-detail-minimize');
    if (!detail || !button) return;
    const minimized = !detail.classList.contains('minimized');
    detail.classList.toggle('minimized', minimized);
    button.setAttribute('aria-pressed', minimized ? 'true' : 'false');
    button.setAttribute('title', minimized ? '展开知识卡片' : '最小化知识卡片');
    button.setAttribute('aria-label', minimized ? '展开知识卡片' : '最小化知识卡片');
    button.innerHTML = minimized ? '<i data-lucide="maximize-2"></i>' : '<i data-lucide="minus"></i>';
    if (window.lucide) window.lucide.createIcons();
    resetDetailCardIfNeeded();
  }

  function bindEvents() {
    document.addEventListener('click', (event) => {
      const cardTab = event.target.closest('[data-card-tab]');
      if (cardTab) {
        switchCardTab(cardTab.dataset.cardTab);
        return;
      }

      if (event.target.closest('#kg-detail-minimize')) {
        toggleDetailMinimized();
        return;
      }

      const bookTrigger = event.target.closest('#kg-book-trigger');
      if (bookTrigger) {
        setBookPickerOpen(bookTrigger.getAttribute('aria-expanded') !== 'true');
        return;
      }

      const bookOption = event.target.closest('[data-book-title]');
      if (bookOption) {
        const title = bookOption.dataset.bookTitle;
        setBookPickerOpen(false);
        if (title) selectBook(title).catch((err) => setEmpty(err.message || String(err)));
        return;
      }

      if (!event.target.closest('.kg-book-picker')) setBookPickerOpen(false);

      const chapter = event.target.closest('[data-chapter-key]');
      if (chapter) {
        selectChapter(chapter.dataset.chapterKey).catch((err) => setEmpty(err.message || String(err)));
        return;
      }

      const core = event.target.closest('[data-core-id]');
      if (core) {
        selectNode(core.dataset.coreId).catch((err) => setEmpty(err.message || String(err)));
        return;
      }

      const related = event.target.closest('[data-related-id]');
      if (related) {
        selectNode(related.dataset.relatedId).catch((err) => setEmpty(err.message || String(err)));
        return;
      }

      if (event.target.closest('#kg-overview-btn')) {
        showOverview().catch((err) => setEmpty(err.message || String(err)));
        return;
      }
      if (event.target.closest('#kg-zoom-in')) zoomBy(0.15);
      if (event.target.closest('#kg-zoom-out')) zoomBy(-0.15);
      if (event.target.closest('#kg-reset-view')) {
        if (state.activeChapter === null && state.laidOutGraph?.nodes?.length) {
          animateViewTo(viewForBounds(state.laidOutGraph.nodes, {
            minScale: OVERVIEW_MIN_SCALE,
            maxScale: OVERVIEW_MAX_SCALE,
            padding: Math.max(42, Math.min(88, Math.min(state.width, state.height) * 0.07)),
          }), 620);
        } else {
          animateViewTo({
            scale: 0.82,
            panX: 0,
            panY: 0,
          }, 620);
        }
      }
    });

    document.addEventListener('input', (event) => {
      if (event.target.id === 'kg-search') {
        state.search = event.target.value || '';
        state.layoutKey = '';
        renderChapters();
        renderGraph();
      }
    });

    document.addEventListener('change', (event) => {
      if (event.target.id === 'kg-book-select') {
        if (!event.target.value) return;
        selectBook(event.target.value).catch((err) => setEmpty(err.message || String(err)));
      }
    });

    const svg = byId('kg-canvas');
    svg?.addEventListener('selectstart', (event) => {
      event.preventDefault();
    });

    svg?.addEventListener('wheel', (event) => {
      event.preventDefault();
      zoomAt(event.deltaY > 0 ? -0.08 : 0.08, event.clientX, event.clientY);
    }, { passive: false });

    svg?.addEventListener('pointerdown', (event) => {
      window.getSelection?.().removeAllRanges();
      if (event.target.closest('.kg-node')) return;
      event.preventDefault();
      cancelViewAnimation();
      state.dragging = { x: event.clientX, y: event.clientY, panX: state.panX, panY: state.panY };
      svg.setPointerCapture(event.pointerId);
    });
    svg?.addEventListener('pointermove', (event) => {
      if (!state.dragging) return;
      event.preventDefault();
      state.panX = state.dragging.panX + event.clientX - state.dragging.x;
      state.panY = state.dragging.panY + event.clientY - state.dragging.y;
      scheduleGraphTransform();
    });
    svg?.addEventListener('pointerup', () => {
      state.dragging = null;
    });

    const detail = byId('kg-detail-body')?.closest('.kg-detail');
    const detailHead = detail?.querySelector('.kg-detail-head');
    detailHead?.addEventListener('pointerdown', (event) => {
      if (event.target.closest('#kg-detail-minimize')) return;
      if (event.button !== undefined && event.button !== 0) return;
      const workspace = document.querySelector('.kg-workspace');
      if (!workspace || !detail) return;
      const workspaceRect = workspace.getBoundingClientRect();
      const cardRect = detail.getBoundingClientRect();
      state.cardDragging = {
        offsetX: event.clientX - cardRect.left,
        offsetY: event.clientY - cardRect.top,
        workspaceLeft: workspaceRect.left,
        workspaceTop: workspaceRect.top,
      };
      detail.classList.add('dragging');
      detailHead.setPointerCapture(event.pointerId);
    });
    detailHead?.addEventListener('pointermove', (event) => {
      if (!state.cardDragging) return;
      event.preventDefault();
      const left = event.clientX - state.cardDragging.workspaceLeft - state.cardDragging.offsetX;
      const top = event.clientY - state.cardDragging.workspaceTop - state.cardDragging.offsetY;
      moveDetailCard(left, top);
    });
    detailHead?.addEventListener('pointerup', () => {
      state.cardDragging = null;
      detail?.classList.remove('dragging');
    });
    detailHead?.addEventListener('pointercancel', () => {
      state.cardDragging = null;
      detail?.classList.remove('dragging');
    });

    window.addEventListener('resize', () => {
      window.clearTimeout(window.__kgResizeTimer);
      window.__kgResizeTimer = window.setTimeout(() => {
        state.layoutKey = '';
        state.overviewLayoutKey = '';
        state.overviewLayout = null;
        resetDetailCardIfNeeded();
        renderGraph();
      }, 120);
    });
  }

  function switchCardTab(key) {
    const body = byId('kg-detail-body');
    if (!body || !key) return;
    body.querySelectorAll('[data-card-tab]').forEach((tab) => {
      const active = tab.getAttribute('data-card-tab') === key;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    body.querySelectorAll('[data-card-panel]').forEach((panel) => {
      const active = panel.getAttribute('data-card-panel') === key;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });
  }

  async function boot() {
    try {
      setEmpty('', true);
      const result = await apiGet('/api/knowledge-graph/overview');
      state.summary = result.summary || {};
      state.textbook = result.textbook || {};
      state.textbooks = result.textbooks || [];
      state.chapters = result.chapters || [];
      state.overview = result.overview || { nodes: [], edges: [] };
      state.overviewLayout = null;
      state.overviewLayoutKey = '';
      beginGraphEntrance();
      renderBookPicker();
      renderChapters();
      await showOverview();
      scheduleGraphEntrance();
      setEmpty('', true);
      if (window.lucide) window.lucide.createIcons();
    } catch (err) {
      setEmpty(err.message || String(err));
    }
  }

  bindEvents();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
