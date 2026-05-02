(() => {
  const $ = (id) => document.getElementById(id);
  const colors = ['#39368d', '#5f55ad', '#7b68d8', '#9b8bf0', '#b8a7cf', '#e2c9df', '#6f5bd8'];
  const filterDefs = [
    ['months', '月份', 'months'],
    ['disease_groups', '疾病大类', 'disease_groups'],
    ['diagnosis_names', '诊断名称', 'diagnosis_names'],
    ['operation_names', '操作名称', 'operation_names'],
    ['departments', '科室', 'departments'],
    ['register_types', '挂号类别', 'register_types'],
  ];
  const state = {
    options: {},
    filters: Object.fromEntries(filterDefs.map(([key]) => [key, []])),
    latest: null,
    historyReady: false,
  };

  function esc(value) {
    return window.oralUi.escapeHtml(String(value ?? ''));
  }

  function svgEl(name, attrs = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function addText(svg, text, attrs = {}) {
    const node = svgEl('text', attrs);
    node.textContent = text;
    svg.appendChild(node);
    return node;
  }

  function withTitle(node, text) {
    const title = svgEl('title');
    title.textContent = text;
    node.appendChild(title);
    return node;
  }

  function shortLabel(label, size = 12) {
    const text = String(label ?? '');
    return text.length > size ? `${text.slice(0, size)}...` : text;
  }

  function numberValue(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function createSvg(panel, viewBox = '0 0 920 360') {
    if (!panel) return null;
    panel.innerHTML = '';
    const svg = svgEl('svg', { viewBox, preserveAspectRatio: 'xMidYMid meet', class: 'stats-inline-svg' });
    panel.appendChild(svg);
    return svg;
  }

  function setStatus(message) {
    const scope = $('stats-scope-text');
    if (scope) scope.textContent = message;
  }

  function cloneFilters(filters = state.filters) {
    return Object.fromEntries(filterDefs.map(([key]) => [key, [...(filters[key] || [])]]));
  }

  function emptyFilters() {
    return Object.fromEntries(filterDefs.map(([key]) => [key, []]));
  }

  function hasActiveFilters(filters = state.filters) {
    return filterDefs.some(([key]) => (filters[key] || []).length > 0);
  }

  function syncHistory(mode = 'push') {
    if (!state.historyReady) return;
    const snapshot = cloneFilters();
    const url = window.location.pathname + window.location.search;
    const method = mode === 'replace' ? 'replaceState' : 'pushState';
    window.history[method]({ statsFilters: snapshot }, '', url);
  }

  function setSingleFilter(key, value) {
    filterDefs.forEach(([filterKey]) => { state.filters[filterKey] = []; });
    state.filters[key] = [value];
    renderFilterControls(state.options);
    syncHistory('push');
    loadDashboard().catch((err) => {
      setStatus(`统计失败：${err.message || err}`);
    });
  }

  function setTrendInsight(panel, detail) {
    const box = panel.querySelector('.trend-insight');
    if (!box || !detail) return;
    const rows = (items) => (items || []).slice(0, 4).map((row) => `
      <li>
        <span>${esc(row[0])}</span>
        <strong>${esc(row[1])}</strong>
      </li>
    `).join('');
    box.innerHTML = `
      <div class="trend-insight-head">
        <span>当前月份</span>
        <strong>${esc(detail.month)}</strong>
        <em>${esc(detail.count)} 例</em>
      </div>
      <div class="trend-insight-grid">
        <div>
          <b>高频诊断</b>
          <ul>${rows(detail.top_diagnoses)}</ul>
        </div>
        <div>
          <b>高频操作</b>
          <ul>${rows(detail.top_operations)}</ul>
        </div>
      </div>
    `;
  }

  function drawGrid(svg, left, top, width, height, ticks = 4) {
    for (let i = 0; i <= ticks; i += 1) {
      const y = top + (height / ticks) * i;
      svg.appendChild(svgEl('line', { x1: left, y1: y, x2: left + width, y2: y, class: 'svg-grid-line' }));
    }
  }

  function drawPulseLine(svg, chart, panel) {
    const labels = chart.labels || [];
    const values = chart.values || [];
    const max = Math.max(...values, 1);
    const left = 64;
    const top = 46;
    const width = 800;
    const height = 228;
    const step = labels.length > 1 ? width / (labels.length - 1) : width;
    const points = values.map((value, idx) => [left + idx * step, top + height - (value / max) * height]);
    drawGrid(svg, left, top, width, height);
    const defs = svgEl('defs');
    const gradient = svgEl('linearGradient', { id: 'pulseArea', x1: 0, y1: top, x2: 0, y2: top + height, gradientUnits: 'userSpaceOnUse' });
    gradient.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#6f5bd8', 'stop-opacity': 0.28 }));
    gradient.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#6f5bd8', 'stop-opacity': 0.02 }));
    defs.appendChild(gradient);
    svg.appendChild(defs);
    if (points.length) {
      const area = `${left},${top + height} ${points.map((point) => point.join(',')).join(' ')} ${left + width},${top + height}`;
      const pointText = points.map((point) => point.join(',')).join(' ');
      svg.appendChild(svgEl('polygon', { points: area, fill: 'url(#pulseArea)' }));
      svg.appendChild(svgEl('polyline', { points: pointText, fill: 'none', stroke: '#b8adff', 'stroke-width': 8, opacity: 0.26, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      svg.appendChild(svgEl('polyline', { points: pointText, fill: 'none', stroke: '#6f5bd8', 'stroke-width': 3.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    }
    points.forEach(([x, y], idx) => {
      const node = withTitle(svgEl('circle', { cx: x, cy: y, r: 5.2, fill: '#ffffff', stroke: '#6f5bd8', 'stroke-width': 2.3, class: 'svg-hotspot' }), `${labels[idx]}：${values[idx]} 例`);
      node.addEventListener('mouseenter', () => setTrendInsight(panel, chart.monthly_details?.[labels[idx]]));
      svg.appendChild(node);
      addText(svg, values[idx], { x, y: y - 14, 'text-anchor': 'middle', class: 'svg-value' });
      addText(svg, String(labels[idx]).replace('2025-', ''), { x, y: top + height + 28, 'text-anchor': 'middle', class: 'svg-label' });
    });
    const defaultMonth = labels[values.indexOf(Math.max(...values))] || labels[0];
    setTrendInsight(panel, chart.monthly_details?.[defaultMonth]);
  }

  function drawOrbitBubbles(svg, chart) {
    const labels = (chart.labels || []).slice(0, 8);
    const values = (chart.values || []).slice(0, 8);
    const max = Math.max(...values, 1);
    const cx = 460;
    const cy = 190;
    [76, 136, 196].forEach((r) => svg.appendChild(svgEl('circle', { cx, cy, r, class: 'svg-orbit' })));
    labels.forEach((label, idx) => {
      const single = labels.length === 1;
      const angle = -Math.PI / 2 + (Math.PI * 2 * idx) / Math.max(labels.length, 1);
      const radius = single ? 0 : 98 + (idx % 3) * 46;
      const x = cx + Math.cos(angle) * radius;
      const y = cy + Math.sin(angle) * radius;
      const bubble = single ? 56 : 18 + (values[idx] / max) * 42;
      svg.appendChild(svgEl('circle', { cx: x, cy: y, r: bubble + 12, fill: colors[idx % colors.length], opacity: 0.1 }));
      const node = withTitle(svgEl('circle', { cx: x, cy: y, r: bubble, fill: colors[idx % colors.length], opacity: 0.86, class: 'svg-hotspot' }), `${label}：${values[idx]} 例`);
      if (label !== '其他未归类诊断') {
        node.addEventListener('click', () => setSingleFilter('disease_groups', label));
      }
      svg.appendChild(node);
      addText(svg, values[idx], { x, y: y + 4, 'text-anchor': 'middle', class: 'svg-bubble-value' });
      addText(svg, shortLabel(label, 9), { x, y: y + bubble + 22, 'text-anchor': 'middle', class: 'svg-label' });
    });
  }

  function drawScanRank(svg, chart) {
    const labels = (chart.labels || []).slice(0, 10);
    const values = (chart.values || []).slice(0, 10);
    const max = Math.max(...values, 1);
    const left = 245;
    const top = 52;
    const width = 585;
    const rowHeight = 27;
    labels.forEach((label, idx) => {
      const y = top + idx * rowHeight;
      const bar = Math.max(4, (values[idx] / max) * width);
      addText(svg, shortLabel(label, 16), { x: left - 16, y: y + 5, 'text-anchor': 'end', class: 'svg-label' });
      svg.appendChild(svgEl('rect', { x: left, y: y - 8, width, height: 12, rx: 6, class: 'svg-track' }));
      svg.appendChild(withTitle(svgEl('rect', { x: left, y: y - 8, width: bar, height: 12, rx: 6, fill: colors[idx % colors.length], opacity: 0.9, class: 'svg-hotspot' }), `${label}：${values[idx]} 例`));
      addText(svg, values[idx], { x: left + bar + 12, y: y + 5, class: 'svg-value' });
    });
  }

  function drawStraightRankBars(svg, chart) {
    const labels = (chart.labels || []).slice(0, 10);
    const values = (chart.values || []).slice(0, 10);
    const max = Math.max(...values, 1);
    const left = 310;
    const top = 50;
    const width = 520;
    const rowHeight = 30;
    labels.forEach((label, idx) => {
      const value = values[idx] || 0;
      const y = top + idx * rowHeight;
      const bar = Math.max(2, (value / max) * width);
      addText(svg, shortLabel(label, 18), { x: left - 18, y: y + 16, 'text-anchor': 'end', class: 'svg-label' });
      svg.appendChild(svgEl('rect', { x: left, y, width, height: 18, rx: 0, class: 'svg-track' }));
      svg.appendChild(withTitle(svgEl('rect', { x: left, y, width: bar, height: 18, rx: 0, fill: colors[idx % colors.length], opacity: 0.92, class: 'svg-hotspot' }), `${label}：${value} 例`));
      addText(svg, value, { x: left + bar + 14, y: y + 16, class: 'svg-value' });
    });
  }

  function polar(cx, cy, r, angle) {
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  }

  function drawDonut(svg, chart) {
    const labels = (chart.labels || []).slice(0, 6);
    const values = (chart.values || []).slice(0, 6);
    const total = values.reduce((sum, value) => sum + value, 0) || 1;
    const cx = 330;
    const cy = 166;
    const rOuter = 132;
    const rInner = 82;
    let start = -Math.PI / 2;
    values.forEach((value, idx) => {
      const angle = (value / total) * Math.PI * 2;
      const end = start + angle;
      const [x1, y1] = polar(cx, cy, rOuter, start);
      const [x2, y2] = polar(cx, cy, rOuter, end);
      const [ix2, iy2] = polar(cx, cy, rInner, end);
      const [ix1, iy1] = polar(cx, cy, rInner, start);
      const large = angle > Math.PI ? 1 : 0;
      const path = `M ${x1} ${y1} A ${rOuter} ${rOuter} 0 ${large} 1 ${x2} ${y2} L ${ix2} ${iy2} A ${rInner} ${rInner} 0 ${large} 0 ${ix1} ${iy1} Z`;
      svg.appendChild(withTitle(svgEl('path', { d: path, fill: colors[idx % colors.length], opacity: 0.9, class: 'svg-hotspot' }), `${labels[idx]}：${value} 例`));
      start = end;
    });
    addText(svg, total, { x: cx, y: cy + 4, 'text-anchor': 'middle', class: 'svg-donut-total' });
    labels.forEach((label, idx) => {
      const y = 64 + idx * 36;
      svg.appendChild(svgEl('rect', { x: 590, y: y - 13, width: 16, height: 16, rx: 4, fill: colors[idx % colors.length] }));
      addText(svg, shortLabel(label, 16), { x: 618, y, class: 'svg-label' });
      addText(svg, values[idx], { x: 852, y, 'text-anchor': 'end', class: 'svg-value' });
    });
  }

  function drawRegisterBars(svg, chart) {
    const labels = (chart.labels || []).slice(0, 8);
    const values = (chart.values || []).slice(0, 8);
    const max = Math.max(...values, 1);
    const total = values.reduce((sum, value) => sum + value, 0) || 1;
    const left = 220;
    const top = 60;
    const width = 540;
    const rowHeight = 48;
    labels.forEach((label, idx) => {
      const value = values[idx] || 0;
      const bar = Math.max(2, (value / max) * width);
      const y = top + idx * rowHeight;
      const percent = ((value / total) * 100).toFixed(1);
      addText(svg, shortLabel(label, 14), { x: left - 22, y: y + 18, 'text-anchor': 'end', class: 'svg-label' });
      svg.appendChild(svgEl('rect', { x: left, y, width, height: 18, rx: 0, class: 'svg-track' }));
      svg.appendChild(withTitle(svgEl('rect', { x: left, y, width: bar, height: 18, rx: 0, fill: colors[idx % colors.length], opacity: 0.92, class: 'svg-hotspot' }), `${label}：${value} 例，占比 ${percent}%`));
      addText(svg, value, { x: left + bar + 16, y: y + 15, class: 'svg-value' });
      addText(svg, `${percent}%`, { x: left + width + 88, y: y + 15, class: 'svg-label' });
    });
  }

  function drawHeatmap(svg, chart) {
    const xs = chart.x_labels || [];
    const ys = chart.y_labels || [];
    const max = chart.max || 1;
    const left = 235;
    const top = 18;
    const cellW = Math.min(54, 610 / Math.max(xs.length, 1));
    const cellH = Math.min(30, 245 / Math.max(ys.length, 1));
    const valueMap = new Map((chart.values || []).map((item) => [`${item.x}-${item.y}`, item.value]));
    ys.forEach((label, yIdx) => addText(svg, shortLabel(label, 14), { x: left - 12, y: top + yIdx * cellH + cellH * 0.65, 'text-anchor': 'end', class: 'svg-label' }));
    xs.forEach((label, xIdx) => addText(svg, String(label).replace('2025-', ''), { x: left + xIdx * cellW + cellW / 2, y: top + ys.length * cellH + 26, 'text-anchor': 'middle', class: 'svg-label' }));
    ys.forEach((_, yIdx) => {
      xs.forEach((_, xIdx) => {
        const value = valueMap.get(`${xIdx}-${yIdx}`) || 0;
        const intensity = value / max;
        const fill = intensity > 0.66 ? '#6f5bd8' : intensity > 0.33 ? '#9b8bf0' : '#eeeaff';
        svg.appendChild(withTitle(svgEl('rect', { x: left + xIdx * cellW, y: top + yIdx * cellH, width: cellW - 5, height: cellH - 5, rx: 6, fill, opacity: 0.32 + intensity * 0.62, class: 'svg-hotspot' }), `${xs[xIdx]} / ${ys[yIdx]}：${value} 例`));
        if (value) addText(svg, value, { x: left + xIdx * cellW + cellW / 2 - 2, y: top + yIdx * cellH + cellH * 0.62, 'text-anchor': 'middle', class: 'svg-heat-value' });
      });
    });
  }

  function drawBar(svg, chart) {
    const labels = (chart.labels || []).slice(0, 10);
    const values = (chart.values || []).slice(0, 10);
    const max = Math.max(...values, 1);
    const left = 76;
    const top = 54;
    const width = 760;
    const height = 220;
    const gap = 13;
    const barWidth = Math.max(24, (width - gap * Math.max(labels.length - 1, 0)) / Math.max(labels.length, 1));
    drawGrid(svg, left, top, width, height);
    labels.forEach((label, idx) => {
      const value = values[idx] || 0;
      const h = Math.max(2, (value / max) * height);
      const x = left + idx * (barWidth + gap);
      const y = top + height - h;
      svg.appendChild(withTitle(svgEl('rect', { x, y, width: barWidth, height: h, rx: 7, fill: colors[idx % colors.length], opacity: 0.86, class: 'svg-hotspot' }), `${label}：${value} 例`));
      addText(svg, value, { x: x + barWidth / 2, y: y - 10, 'text-anchor': 'middle', class: 'svg-value' });
      addText(svg, shortLabel(label, 7), { x: x + barWidth / 2, y: top + height + 26, 'text-anchor': 'middle', class: 'svg-label' });
    });
  }

  function drawStackedBar(svg, chart) {
    const labels = chart.labels || [];
    const series = (chart.series || []).slice(0, 5);
    const totals = labels.map((_, labelIndex) => series.reduce((sum, item) => sum + (Number(item.values?.[labelIndex]) || 0), 0));
    const max = Math.max(...totals, 1);
    const left = 120;
    const top = 52;
    const width = 520;
    const height = 230;
    const barGap = 16;
    const barWidth = Math.max(24, Math.min(48, (width - barGap * Math.max(labels.length - 1, 0)) / Math.max(labels.length, 1)));
    const stackColors = ['#39368d', '#5f55ad', '#8373c3', '#ad99ce', '#e2c9df'];
    svg.appendChild(svgEl('line', { x1: left, y1: top + height, x2: left + width + 12, y2: top + height, class: 'svg-axis-line' }));
    svg.appendChild(svgEl('line', { x1: left, y1: top, x2: left, y2: top + height, class: 'svg-axis-line' }));
    labels.forEach((label, labelIndex) => {
      const x = left + labelIndex * (barWidth + barGap);
      let yCursor = top + height;
      series.forEach((item, seriesIndex) => {
        const value = Number(item.values?.[labelIndex]) || 0;
        const h = value ? Math.max(2, (value / max) * height) : 0;
        if (!h) return;
        yCursor -= h;
        svg.appendChild(withTitle(svgEl('rect', {
          x,
          y: yCursor,
          width: barWidth,
          height: h,
          fill: stackColors[seriesIndex % stackColors.length],
          opacity: 0.92,
          class: 'svg-hotspot',
        }), `${label} / ${item.name}：${value} 例`));
      });
      addText(svg, String(label).replace('2025-', ''), { x: x + barWidth / 2, y: top + height + 28, 'text-anchor': 'middle', class: 'svg-label svg-month-label' });
      if (totals[labelIndex]) addText(svg, totals[labelIndex], { x: x + barWidth / 2, y: yCursor - 10, 'text-anchor': 'middle', class: 'svg-value' });
    });
    series.forEach((item, index) => {
      const y = 92 + index * 34;
      svg.appendChild(svgEl('rect', { x: 705, y: y - 14, width: 14, height: 14, fill: stackColors[index % stackColors.length] }));
      addText(svg, shortLabel(item.name, 18), { x: 728, y, class: 'svg-legend-label' });
    });
  }

  function chartBy(result, type, title = '') {
    return (result.charts || []).find((chart) => chart.type === type && (!title || chart.title === title));
  }

  function tableBy(result, title) {
    return (result.tables || []).find((table) => table.title === title);
  }

  function renderRankList(table, limit = 5) {
    const rows = (table?.rows || []).slice(0, limit);
    const max = Math.max(...rows.map((row) => Number(row[1]) || 0), 1);
    return rows.map((row, index) => {
      const width = Math.max(4, ((Number(row[1]) || 0) / max) * 100);
      return `
        <li class="screen-rank-row">
          <span class="rank-index">${index + 1}</span>
          <span class="rank-name">${esc(row[0])}</span>
          <span class="rank-track"><i style="width:${width}%"></i></span>
          <strong>${esc(row[1])}</strong>
        </li>
      `;
    }).join('');
  }

  function renderSpectrumStrip(chart) {
    const labels = (chart?.labels || []).slice(0, 22);
    const values = (chart?.values || []).slice(0, 22);
    const max = Math.max(...values, 1);
    const words = labels.map((label, index) => {
      const size = 15 + ((values[index] || 0) / max) * 19;
      return `<span style="font-size:${size.toFixed(1)}px;color:${colors[index % colors.length]}" title="${esc(label)}：${esc(values[index])}例">${esc(label)}</span>`;
    }).join('');
    return `${words}${words}`;
  }

  function drawTopologyMap(container, diseaseChart, trendChart) {
    const svg = createSvg(container, '0 0 980 650');
    if (!svg) return;
    const trendLabels = trendChart?.labels || [];
    const trendValues = trendChart?.values || [];
    const trendMax = Math.max(...trendValues, 1);
    const diseaseLabels = (diseaseChart?.labels || []).slice(0, 6);
    const diseaseValues = (diseaseChart?.values || []).slice(0, 6);
    const diseaseMax = Math.max(...diseaseValues, 1);
    const left = 50;
    const top = 62;
    const width = 850;
    const height = 316;
    const step = trendLabels.length > 1 ? width / (trendLabels.length - 1) : width;
    const points = trendValues.map((value, index) => [
      left + index * step,
      top + height - (value / trendMax) * height,
    ]);
    const defs = svgEl('defs');
    const areaGradient = svgEl('linearGradient', { id: 'centerPulseArea', x1: 0, y1: top, x2: 0, y2: top + height, gradientUnits: 'userSpaceOnUse' });
    areaGradient.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#39368d', 'stop-opacity': 0.44 }));
    areaGradient.appendChild(svgEl('stop', { offset: '46%', 'stop-color': '#7b68d8', 'stop-opacity': 0.22 }));
    areaGradient.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#7b68d8', 'stop-opacity': 0 }));
    const lineGradient = svgEl('linearGradient', { id: 'centerPulseLine', x1: left, y1: 0, x2: left + width, y2: 0, gradientUnits: 'userSpaceOnUse' });
    lineGradient.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#39368d' }));
    lineGradient.appendChild(svgEl('stop', { offset: '56%', 'stop-color': '#5f55ad' }));
    lineGradient.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#7b68d8' }));
    defs.appendChild(areaGradient);
    defs.appendChild(lineGradient);
    svg.appendChild(defs);

    addText(svg, '高频疾病焦点', { x: left, y: 505, class: 'svg-map-note' });
    drawGrid(svg, left, top, width, height, 5);
    [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
      const y = top + height - ratio * height;
      addText(svg, Math.round(trendMax * ratio), { x: left - 18, y: y + 4, 'text-anchor': 'end', class: 'svg-label' });
    });
    if (points.length) {
      const area = `${left},${top + height} ${points.map((point) => point.join(',')).join(' ')} ${left + width},${top + height}`;
      const pointText = points.map((point) => point.join(',')).join(' ');
      svg.appendChild(svgEl('polygon', { points: area, fill: 'url(#centerPulseArea)' }));
      svg.appendChild(svgEl('polyline', { points: pointText, fill: 'none', stroke: '#cfc8ff', 'stroke-width': 14, opacity: 0.26, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      svg.appendChild(svgEl('polyline', { points: pointText, fill: 'none', stroke: 'url(#centerPulseLine)', 'stroke-width': 5.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    }
    points.forEach(([x, y], index) => {
      const label = trendLabels[index] || '';
      const value = trendValues[index] || 0;
      const detail = trendChart?.monthly_details?.[label];
      const topDiagnosis = detail?.top_diagnoses?.[0]?.[0] || '无诊断明细';
      const topOperation = detail?.top_operations?.[0]?.[0] || '无操作明细';
      svg.appendChild(svgEl('line', { x1: x, y1: y, x2: x, y2: top + height, stroke: '#ded8fb', 'stroke-width': 1.1, opacity: 0.58 }));
      svg.appendChild(withTitle(svgEl('circle', { cx: x, cy: y, r: 7, fill: '#ffffff', stroke: '#5f55ad', 'stroke-width': 2.8, class: 'svg-hotspot' }), `${label}：${value}例；高频诊断：${topDiagnosis}；高频操作：${topOperation}`));
      addText(svg, String(label).replace('2025-', ''), { x, y: top + height + 30, 'text-anchor': 'middle', class: 'svg-label' });
    });
    diseaseLabels.forEach((label, index) => {
      const value = diseaseValues[index] || 0;
      const x = left + 128 + index * 128;
      const y = 548;
      const r = 8 + (value / diseaseMax) * 17;
      const color = colors[index % colors.length];
      const node = withTitle(svgEl('circle', { cx: x, cy: y, r, fill: color, opacity: 0.9, class: 'svg-hotspot svg-map-node' }), `${label}：${value}例，点击筛选`);
      if (label !== '其他未归类诊断') node.addEventListener('click', () => setSingleFilter('disease_groups', label));
      svg.appendChild(node);
      addText(svg, shortLabel(label, 7), { x, y: y + 56, 'text-anchor': 'middle', class: 'svg-label' });
      addText(svg, value, { x, y: y + 76, 'text-anchor': 'middle', class: 'svg-value' });
    });
  }

  function renderOverviewDashboard(result) {
    const grid = $('stats-chart-grid');
    if (!grid) return;
    const trend = chartBy(result, 'pulse_line');
    const disease = chartBy(result, 'orbit_bubbles');
    const operation = chartBy(result, 'scan_rank', '操作方式 Top 排名');
    const register = chartBy(result, 'donut');
    const heatmap = chartBy(result, 'heatmap');
    const spectrum = chartBy(result, 'diagnosis_spectrum');
    grid.className = 'stats-chart-grid overview-screen';
    grid.innerHTML = `
      <section class="screen-panel left-top">
        <div class="screen-panel-head"><strong>疾病类目态势</strong></div>
        <div class="screen-mini-canvas" data-chart="orbit"></div>
      </section>
      <section class="screen-panel left-bottom">
        <div class="screen-panel-head"><strong>检查统计</strong></div>
        <ul class="screen-rank-list">${renderRankList(tableBy(result, '操作方式 Top 排名'), 7)}</ul>
      </section>
      <section class="screen-panel center-map">
        <div class="screen-panel-head"><strong>门诊流量</strong></div>
        <div class="screen-map-canvas"></div>
      </section>
      <section class="screen-panel right-top">
        <div class="screen-panel-head"><strong>挂号构成</strong></div>
        <div class="screen-mini-canvas" data-chart="register"></div>
      </section>
      <section class="screen-panel right-middle">
        <div class="screen-panel-head"><strong>口腔问题月度分布</strong></div>
        <div class="screen-mini-canvas" data-chart="heatmap"></div>
      </section>
      <section class="screen-panel right-bottom">
        <div class="screen-panel-head"><strong>高频诊断流</strong></div>
        <div class="screen-spectrum">${renderSpectrumStrip(spectrum)}</div>
      </section>
    `;
    drawTopologyMap(grid.querySelector('.screen-map-canvas'), disease, trend);
    drawChart({ querySelector: () => grid.querySelector('[data-chart="orbit"]') }, disease);
    drawChart({ querySelector: () => grid.querySelector('[data-chart="register"]') }, register);
    drawChart({ querySelector: () => grid.querySelector('[data-chart="heatmap"]') }, heatmap);
  }

  function drawDiagnosisSpectrum(svg, chart) {
    const labels = (chart.labels || []).slice(0, 18);
    const values = (chart.values || []).slice(0, 18);
    const max = Math.max(...values, 1);
    const lanes = [38, 70, 100];
    labels.forEach((label, idx) => {
      const lane = lanes[idx % lanes.length];
      const x = 42 + ((idx * 137) % 760);
      const size = 11 + (values[idx] / max) * 13;
      const opacity = 0.52 + (values[idx] / max) * 0.42;
      const text = addText(svg, shortLabel(label, 9), {
        x,
        y: lane,
        class: 'svg-spectrum-word svg-hotspot',
        'font-size': size.toFixed(1),
        fill: colors[idx % colors.length],
        opacity,
      });
      withTitle(text, `${label}：${values[idx]} 例`);
    });
  }

  function drawChart(panel, chart) {
    if (!panel || !chart) return;
    const slot = panel.querySelector('.stats-chart-canvas');
    if (!slot) return;
    const svg = createSvg(slot, chart.type === 'diagnosis_spectrum' ? '0 0 920 120' : '0 0 920 360');
    if (!svg) return;
    const isFilteredPanel = Boolean(panel.closest?.('.filtered-board'));
    if (chart.type === 'pulse_line') drawPulseLine(svg, chart, panel);
    else if (chart.type === 'orbit_bubbles') drawOrbitBubbles(svg, chart);
    else if (chart.type === 'scan_rank' && isFilteredPanel) drawStraightRankBars(svg, chart);
    else if (chart.type === 'scan_rank') drawScanRank(svg, chart);
    else if (chart.type === 'donut' && isFilteredPanel) drawRegisterBars(svg, chart);
    else if (chart.type === 'donut') drawDonut(svg, chart);
    else if (chart.type === 'heatmap') drawHeatmap(svg, chart);
    else if (chart.type === 'stacked_bar') drawStackedBar(svg, chart);
    else if (chart.type === 'diagnosis_spectrum') drawDiagnosisSpectrum(svg, chart);
    else drawBar(svg, chart);
  }

  function findTableForChart(chart, tables) {
    return (tables || []).find((table) => table.title === chart.title) || null;
  }

  function tableFromChart(chart) {
    if (chart.series?.length) {
      return {
        title: chart.title,
        columns: ['月份', ...chart.series.map((item) => item.name)],
        rows: (chart.labels || []).map((label, index) => [
          label,
          ...chart.series.map((item) => item.values?.[index] || 0),
        ]),
      };
    }
    return { title: chart.title, columns: ['类别', '例数'], rows: (chart.labels || []).map((label, idx) => [label, chart.values?.[idx] || 0]) };
  }

  function renderValueCell(value, maxValue) {
    const numeric = numberValue(value);
    if (numeric === null || maxValue <= 0) return esc(value);
    const width = Math.max(4, (numeric / maxValue) * 100);
    return `<div class="stats-value-cell"><span>${esc(value)}</span><div class="stats-value-track"><div class="stats-value-fill" style="width:${width}%"></div></div></div>`;
  }

  function renderInlineTable(table) {
    const rows = (table?.rows || []).slice(0, 12);
    const columns = table?.columns || [];
    const firstNumericIndex = rows[0]?.findIndex((cell, idx) => idx > 0 && numberValue(cell) !== null) ?? -1;
    const maxValue = firstNumericIndex >= 0 ? Math.max(...rows.map((row) => numberValue(row[firstNumericIndex]) || 0), 1) : 0;
    return `
      <details class="stats-table-card" open>
        <summary><span>数据明细</span><strong>${esc(table?.title || '')}</strong></summary>
        <div class="stats-chart-table-wrap">
          <table class="stats-chart-table">
            <thead><tr><th>#</th>${columns.map((col) => `<th>${esc(col)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map((row, rowIndex) => `<tr><td class="stats-rank-col">${rowIndex + 1}</td>${row.map((cell, cellIndex) => `<td>${cellIndex === firstNumericIndex ? renderValueCell(cell, maxValue) : esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody>
          </table>
        </div>
      </details>
    `;
  }

  function filterLabel(key) {
    return filterDefs.find(([itemKey]) => itemKey === key)?.[1] || key;
  }

  function selectedCount(key) {
    return state.filters[key]?.length || 0;
  }

  function renderFilterControls(options) {
    const grid = $('stats-filter-grid');
    if (!grid) return;
    grid.innerHTML = filterDefs.map(([key, label, optionKey]) => {
      const items = options[optionKey] || [];
      const count = selectedCount(key);
      return `
        <details class="filter-menu" data-key="${esc(key)}">
          <summary>
            <span>${esc(label)}</span>
            <strong>${count ? `${count} 项` : '全部'}</strong>
          </summary>
          <div class="filter-menu-panel">
            <input class="filter-search" type="search" placeholder="搜索${esc(label)}">
            <div class="filter-option-list">
              ${items.map((item) => `
                <label class="filter-option" data-label="${esc(item.label)}">
                  <input type="checkbox" value="${esc(item.value)}" ${state.filters[key].includes(item.value) ? 'checked' : ''}>
                  <span>${esc(item.label)}</span>
                  <em>${esc(item.count)}</em>
                </label>
              `).join('')}
            </div>
          </div>
        </details>
      `;
    }).join('');
  }

  function filterPayload() {
    return { ...state.filters, view: 'dashboard' };
  }

  function renderFilterSummary(result) {
    const scope = $('stats-scope-text');
    const active = $('stats-active-filters');
    if (scope) scope.textContent = `${result.scope_label} · ${result.matched_count} 条记录 · 占同时间范围 ${result.ratio}`;
    const filters = result.active_filters || {};
    const chips = Object.entries(filters).flatMap(([key, values]) => values.map((value) => ({ key, value })));
    if (!active) return;
    if (!chips.length) {
      active.innerHTML = '<span>全量数据</span>';
      return;
    }
    const visible = chips.slice(0, 4);
    const rest = chips.length - visible.length;
    active.innerHTML = [
      `<span class="filter-summary-count">已启用 ${chips.length} 个筛选</span>`,
      ...visible.map((item) => `<span><b>${esc(filterLabel(item.key))}</b>${esc(item.value)}</span>`),
      rest > 0 ? `<span>+${rest}</span>` : '',
    ].join('');
  }

  function renderCharts(result) {
    const grid = $('stats-chart-grid');
    const empty = $('stats-empty');
    if (!grid) return;
    renderFilterSummary(result);
    if (empty) empty.hidden = !result.empty;
    if (result.empty) {
      grid.innerHTML = '';
      return;
    }
    if (!result.is_filtered) {
      renderOverviewDashboard(result);
      return;
    }
    const overviewPriority = ['pulse_line', 'orbit_bubbles', 'scan_rank', 'donut', 'heatmap', 'diagnosis_spectrum'];
    const filteredPriority = ['stacked_bar', 'scan_rank', 'donut'];
    const priority = result.is_filtered ? filteredPriority : overviewPriority;
    const charts = priority
      .map((type) => (result.charts || []).find((chart) => chart.type === type))
      .filter(Boolean);
    grid.className = `stats-chart-grid ${result.is_filtered ? 'filtered-board' : 'overview-board'}`;
    grid.innerHTML = charts.map((chart, idx) => {
      const table = findTableForChart(chart, result.tables) || tableFromChart(chart);
      const featured = idx === 0 ? ' featured' : '';
      return `
        <article class="stats-chart-panel${featured} chart-${esc(chart.type)}">
          <div class="stats-panel-head">
            <span>${result.is_filtered ? '筛选结果' : (idx === 0 ? '中心主视图' : '全量态势')}</span>
            <h3>${esc(chart.title)}</h3>
          </div>
          <div class="stats-chart-body">
            <div class="stats-chart-canvas"></div>
            ${chart.type === 'pulse_line' ? '<aside class="trend-insight"></aside>' : ''}
          </div>
          <div class="stats-panel-table">${renderInlineTable(table)}</div>
        </article>
      `;
    }).join('');
    grid.querySelectorAll('.stats-chart-panel').forEach((panel, idx) => drawChart(panel, charts[idx]));
  }

  async function loadDashboard(payload = filterPayload()) {
    closeFilterMenus();
    const result = await window.oralUi.apiPost('/api/stats/filter', payload);
    state.latest = result;
    renderCharts(result);
  }

  function clearFilters() {
    filterDefs.forEach(([key]) => { state.filters[key] = []; });
    renderFilterControls(state.options);
  }

  function updateFilterSummaries() {
    document.querySelectorAll('.filter-menu').forEach((menu) => {
      const key = menu.dataset.key;
      const strong = menu.querySelector('summary strong');
      const count = selectedCount(key);
      if (strong) strong.textContent = count ? `${count} 项` : '全部';
    });
  }

  function closeFilterMenus(except = null) {
    document.querySelectorAll('.filter-menu[open]').forEach((menu) => {
      if (menu !== except) menu.open = false;
    });
  }

  function bindFilters() {
    const grid = $('stats-filter-grid');
    grid?.addEventListener('click', (event) => {
      const summary = event.target.closest?.('.filter-menu summary');
      if (!summary) return;
      closeFilterMenus(summary.closest('.filter-menu'));
    });
    grid?.addEventListener('change', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || input.type !== 'checkbox') return;
      const menu = input.closest('.filter-menu');
      const key = menu?.dataset.key;
      if (!key) return;
      const values = new Set(state.filters[key] || []);
      if (input.checked) values.add(input.value);
      else values.delete(input.value);
      state.filters[key] = Array.from(values);
      updateFilterSummaries();
    });
    grid?.addEventListener('input', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.classList.contains('filter-search')) return;
      const query = input.value.trim().toLowerCase();
      input.closest('.filter-menu-panel')?.querySelectorAll('.filter-option').forEach((option) => {
        option.hidden = query && !String(option.dataset.label || '').toLowerCase().includes(query);
      });
    });
    document.addEventListener('click', (event) => {
      if (!event.target.closest?.('.filter-menu')) closeFilterMenus();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeFilterMenus();
    });
  }

  async function init() {
    state.options = await fetch('/api/stats/options').then((response) => response.json());
    renderFilterControls(state.options);
    state.historyReady = true;
    if (!window.history.state?.statsFilters) syncHistory('replace');
    await loadDashboard();
  }

  function bind() {
    bindFilters();
    $('stats-chart-grid')?.addEventListener('click', (event) => {
      const panel = event.target.closest?.('.stats-chart-panel');
      if (!panel) return;
      document.querySelectorAll('.stats-chart-panel.is-focus').forEach((item) => {
        if (item !== panel) item.classList.remove('is-focus');
      });
      panel.classList.toggle('is-focus');
    });
    $('stats-apply')?.addEventListener('click', () => {
      syncHistory(hasActiveFilters() ? 'push' : 'replace');
      loadDashboard().catch((err) => {
        setStatus(`统计失败：${err.message || err}`);
      });
    });
    $('stats-reset')?.addEventListener('click', () => {
      clearFilters();
      syncHistory('push');
      loadDashboard().catch((err) => {
        setStatus(`统计失败：${err.message || err}`);
      });
    });
    window.addEventListener('popstate', (event) => {
      state.filters = cloneFilters(event.state?.statsFilters || emptyFilters());
      renderFilterControls(state.options);
      loadDashboard().catch((err) => {
        setStatus(`统计失败：${err.message || err}`);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      bind();
      init().catch((err) => { setStatus(`初始化失败：${err.message || err}`); });
    });
  } else {
    bind();
    init().catch((err) => { setStatus(`初始化失败：${err.message || err}`); });
  }
})();
