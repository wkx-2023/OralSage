(() => {
  const $ = (id) => document.getElementById(id);
  const palette = ['#3C3B8B', '#6656A6', '#917BBD', '#B19CCB', '#E5CBE1'];
  let lastError = '';

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

  function shortLabel(label, size = 10) {
    const text = String(label ?? '');
    return text.length > size ? `${text.slice(0, size)}...` : text;
  }

  function numberValue(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function drawGrid(svg, left, top, width, height, ticks = 4) {
    for (let i = 0; i <= ticks; i += 1) {
      const y = top + (height / ticks) * i;
      svg.appendChild(svgEl('line', { x1: left, y1: y, x2: left + width, y2: y, stroke: '#eceaf2', 'stroke-width': 1 }));
    }
    svg.appendChild(svgEl('line', { x1: left, y1: top, x2: left, y2: top + height, stroke: '#27243d', 'stroke-width': 1.1 }));
    svg.appendChild(svgEl('line', { x1: left, y1: top + height, x2: left + width, y2: top + height, stroke: '#27243d', 'stroke-width': 1.1 }));
  }

  function createSvg(panel) {
    const svg = svgEl('svg', { viewBox: '0 0 920 330', preserveAspectRatio: 'xMidYMid meet', class: 'stats-inline-svg' });
    panel.appendChild(svg);
    return svg;
  }

  function drawBarChart(svg, chart) {
    const labels = (chart.labels || []).slice(0, 10);
    const values = (chart.values || []).slice(0, 10);
    const max = Math.max(...values, 1);
    const left = 72;
    const top = 44;
    const width = 790;
    const height = 218;
    const gap = 14;
    const barWidth = Math.max(24, (width - gap * Math.max(labels.length - 1, 0)) / Math.max(labels.length, 1));
    drawGrid(svg, left, top, width, height);

    labels.forEach((label, idx) => {
      const value = values[idx] || 0;
      const x = left + idx * (barWidth + gap);
      const h = Math.max(2, (value / max) * height);
      const y = top + height - h;
      svg.appendChild(svgEl('rect', { x, y, width: barWidth, height: h, fill: palette[idx % palette.length], opacity: 0.92 }));
      addText(svg, value, { x: x + barWidth / 2, y: y - 8, 'text-anchor': 'middle', fill: '#27243d', 'font-size': 12 });
      addText(svg, shortLabel(label, 7), { x: x + barWidth / 2, y: top + height + 24, 'text-anchor': 'middle', fill: '#4f4a69', 'font-size': 11 });
    });
  }

  function drawLineChart(svg, chart) {
    const labels = chart.labels || [];
    const values = chart.values || [];
    const max = Math.max(...values, 1);
    const left = 72;
    const top = 44;
    const width = 790;
    const height = 218;
    const step = labels.length > 1 ? width / (labels.length - 1) : width;
    const points = values.map((value, idx) => [left + idx * step, top + height - (value / max) * height]);
    drawGrid(svg, left, top, width, height);
    if (points.length) {
      const gradientId = `line-area-${Math.random().toString(16).slice(2)}`;
      const defs = svgEl('defs');
      const gradient = svgEl('linearGradient', {
        id: gradientId,
        x1: 0,
        y1: top,
        x2: 0,
        y2: top + height,
        gradientUnits: 'userSpaceOnUse',
      });
      gradient.appendChild(svgEl('stop', { offset: '0%', 'stop-color': '#917BBD', 'stop-opacity': 0.46 }));
      gradient.appendChild(svgEl('stop', { offset: '48%', 'stop-color': '#B19CCB', 'stop-opacity': 0.18 }));
      gradient.appendChild(svgEl('stop', { offset: '100%', 'stop-color': '#E5CBE1', 'stop-opacity': 0 }));
      defs.appendChild(gradient);
      svg.appendChild(defs);
      const area = `${left},${top + height} ${points.map((point) => point.join(',')).join(' ')} ${left + width},${top + height}`;
      svg.appendChild(svgEl('polygon', { points: area, fill: `url(#${gradientId})` }));
      svg.appendChild(svgEl('polyline', {
        points: points.map((point) => point.join(',')).join(' '),
        fill: 'none',
        stroke: '#3C3B8B',
        'stroke-width': 3.2,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      }));
    }
    points.forEach(([x, y], idx) => {
      svg.appendChild(svgEl('circle', { cx: x, cy: y, r: 4.5, fill: '#ffffff', stroke: '#3C3B8B', 'stroke-width': 2.4 }));
      addText(svg, values[idx], { x, y: y - 11, 'text-anchor': 'middle', fill: '#27243d', 'font-size': 11 });
      addText(svg, String(labels[idx]).replace('2025-', ''), { x, y: top + height + 24, 'text-anchor': 'middle', fill: '#4f4a69', 'font-size': 11 });
    });
  }

  function drawRankLine(svg, chart) {
    const labels = (chart.labels || []).slice(0, 10);
    const values = (chart.values || []).slice(0, 10);
    const max = Math.max(...values, 1);
    const left = 216;
    const top = 52;
    const width = 610;
    const rowHeight = 22;
    labels.forEach((label, idx) => {
      const y = top + idx * rowHeight;
      const x2 = left + (values[idx] / max) * width;
      addText(svg, shortLabel(label, 14), { x: left - 14, y: y + 4, 'text-anchor': 'end', fill: '#4f4a69', 'font-size': 11 });
      svg.appendChild(svgEl('line', { x1: left, y1: y, x2: left + width, y2: y, stroke: '#eceaf2' }));
      svg.appendChild(svgEl('line', { x1: left, y1: y, x2, y2: y, stroke: palette[idx % palette.length], 'stroke-width': 9, 'stroke-linecap': 'butt' }));
      addText(svg, values[idx], { x: x2 + 10, y: y + 4, fill: '#27243d', 'font-size': 11, 'font-weight': 600 });
    });
  }

  function polar(cx, cy, r, angle) {
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  }

  function drawDonut(svg, chart) {
    const labels = (chart.labels || []).slice(0, 5);
    const values = (chart.values || []).slice(0, 5);
    const total = values.reduce((sum, value) => sum + value, 0) || 1;
    const cx = 300;
    const cy = 164;
    const r = 104;
    let start = -Math.PI / 2;
    values.forEach((value, idx) => {
      const angle = (value / total) * Math.PI * 2;
      const end = start + angle;
      const [x1, y1] = polar(cx, cy, r, start);
      const [x2, y2] = polar(cx, cy, r, end);
      const large = angle > Math.PI ? 1 : 0;
      const path = `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
      svg.appendChild(svgEl('path', { d: path, fill: palette[idx % palette.length], opacity: 0.92 }));
      start = end;
    });
    svg.appendChild(svgEl('circle', { cx, cy, r: 60, fill: '#ffffff' }));
    addText(svg, total, { x: cx, y: cy + 4, 'text-anchor': 'middle', fill: '#27243d', 'font-size': 30, 'font-weight': 700 });
    labels.forEach((label, idx) => {
      const y = 88 + idx * 31;
      svg.appendChild(svgEl('rect', { x: 560, y: y - 11, width: 13, height: 13, fill: palette[idx % palette.length] }));
      addText(svg, shortLabel(label, 15), { x: 583, y, fill: '#4f4a69', 'font-size': 12 });
      addText(svg, values[idx], { x: 800, y, 'text-anchor': 'end', fill: '#27243d', 'font-size': 12, 'font-weight': 600 });
    });
  }

  function drawStackedBar(svg, chart) {
    const labels = chart.labels || [];
    const series = (chart.series || []).slice(0, 5);
    const totals = labels.map((_, idx) => series.reduce((sum, item) => sum + (item.values?.[idx] || 0), 0));
    const max = Math.max(...totals, 1);
    const left = 72;
    const top = 44;
    const width = 560;
    const height = 218;
    const gap = 12;
    const barWidth = Math.max(24, (width - gap * Math.max(labels.length - 1, 0)) / Math.max(labels.length, 1));
    drawGrid(svg, left, top, width, height);
    labels.forEach((label, idx) => {
      let yCursor = top + height;
      series.forEach((item, sIdx) => {
        const value = item.values?.[idx] || 0;
        const h = (value / max) * height;
        if (h > 0) {
          yCursor -= h;
          svg.appendChild(svgEl('rect', { x: left + idx * (barWidth + gap), y: yCursor, width: barWidth, height: Math.max(1, h), fill: palette[sIdx % palette.length], opacity: 0.9 }));
        }
      });
      addText(svg, String(label).replace('2025-', ''), { x: left + idx * (barWidth + gap) + barWidth / 2, y: top + height + 24, 'text-anchor': 'middle', fill: '#4f4a69', 'font-size': 11 });
    });
    series.forEach((item, idx) => {
      const y = 80 + idx * 31;
      svg.appendChild(svgEl('rect', { x: 690, y: y - 11, width: 13, height: 13, fill: palette[idx % palette.length] }));
      addText(svg, shortLabel(item.name, 14), { x: 713, y, fill: '#4f4a69', 'font-size': 11 });
    });
  }

  function drawChart(panel, chart) {
    const chartSlot = panel.querySelector('.stats-chart-figure');
    const svg = createSvg(chartSlot);
    if (chart.type === 'line') drawLineChart(svg, chart);
    else if (chart.type === 'rank_line') drawRankLine(svg, chart);
    else if (chart.type === 'donut') drawDonut(svg, chart);
    else if (chart.type === 'stacked_bar') drawStackedBar(svg, chart);
    else drawBarChart(svg, chart);
  }

  function findTableForChart(chart, tables) {
    const candidates = (tables || []).filter((table) => table.title !== '统计口径');
    return candidates.find((table) => table.title === chart.title) || null;
  }

  function tableFromChart(chart) {
    if (chart.series?.length) {
      return {
        title: chart.title,
        columns: ['月份', ...chart.series.map((item) => item.name)],
        rows: (chart.labels || []).map((label, idx) => [
          label,
          ...chart.series.map((item) => item.values?.[idx] || 0),
        ]),
      };
    }
    return {
      title: chart.title,
      columns: ['类别', '例数'],
      rows: (chart.labels || []).map((label, idx) => [label, chart.values?.[idx] || 0]),
    };
  }

  function renderValueCell(value, maxValue) {
    const numeric = numberValue(value);
    if (numeric === null || maxValue <= 0) return esc(value);
    const width = Math.max(4, (numeric / maxValue) * 100);
    return `
      <div class="stats-value-cell">
        <span>${esc(value)}</span>
        <div class="stats-value-track">
          <div class="stats-value-fill" style="width:${width}%"></div>
        </div>
      </div>
    `;
  }

  function renderInlineTable(table) {
    const rows = (table.rows || []).slice(0, 12);
    const columns = table.columns || [];
    const firstNumericIndex = rows[0]?.findIndex((cell, idx) => idx > 0 && numberValue(cell) !== null) ?? -1;
    const maxValue = firstNumericIndex >= 0
      ? Math.max(...rows.map((row) => numberValue(row[firstNumericIndex]) || 0), 1)
      : 0;
    return `
      <div class="stats-table-card">
        <div class="stats-table-caption">
          <span>数据明细</span>
          <strong>${esc(table.title || '')}</strong>
        </div>
        <div class="stats-chart-table-wrap">
          <table class="stats-chart-table">
            <thead>
              <tr>
                <th class="stats-rank-col">#</th>
                ${columns.map((col) => `<th>${esc(col)}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${rows.map((row, rowIndex) => `
                <tr>
                  <td class="stats-rank-col">${rowIndex + 1}</td>
                  ${row.map((cell, cellIndex) => `
                    <td>${cellIndex === firstNumericIndex ? renderValueCell(cell, maxValue) : esc(cell)}</td>
                  `).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderCharts(charts, tables = []) {
    const grid = $('stats-chart-grid');
    const zone = document.querySelector('.stats-visual-zone');
    if (!grid) return;
    const visibleCharts = (charts || []).filter((chart) => !['kpi', 'field_completeness'].includes(chart.type));
    if (!visibleCharts.length) {
      grid.innerHTML = '';
      zone?.classList.remove('has-results');
      return;
    }
    zone?.classList.add('has-results');
    grid.innerHTML = visibleCharts.map((chart, idx) => {
      const table = findTableForChart(chart, tables) || tableFromChart(chart);
      return `
        <article class="stats-chart-panel stats-chart-panel-${idx}">
          <div class="stats-chart-figure">
            <h3>${esc(chart.title)}</h3>
          </div>
          <aside class="stats-chart-data">
            ${renderInlineTable(table)}
          </aside>
        </article>
      `;
    }).join('');
    grid.querySelectorAll('.stats-chart-panel').forEach((panel, idx) => drawChart(panel, visibleCharts[idx]));
  }

  async function runStats() {
    const query = $('stats-query')?.value.trim() || '';
    if (!query) {
      lastError = '请输入统计请求。';
      renderCharts([]);
      return;
    }
    lastError = '正在生成图表...';
    renderCharts([]);
    const result = await window.oralUi.apiPost('/api/stats/query', { query, top_k: 10 });
    if (!result.ok) {
      lastError = result.message || '统计失败';
      renderCharts([]);
      return;
    }
    lastError = '';
    renderCharts(result.charts || [], result.tables || []);
  }

  function bind() {
    $('stats-run')?.addEventListener('click', () => {
      runStats().catch((err) => {
        lastError = `统计失败：${err.message}`;
        renderCharts([]);
      });
    });
    $('stats-query')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runStats().catch((err) => {
          lastError = `统计失败：${err.message}`;
          renderCharts([]);
        });
      }
    });
    renderCharts([]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
