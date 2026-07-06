(() => {
  const oralUi = window.oralUi || {};
  const byId = oralUi.byId || ((id) => document.getElementById(id));
  const escapeHtml = oralUi.escapeHtml || ((text) => String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;'));

  const now = new Date();
  const initialDate = dateKey(now.getFullYear(), now.getMonth() + 1, now.getDate());
  const state = {
    userId: new URLSearchParams(window.location.search).get('user_id')
      || localStorage.getItem('oralsage.learningProfile.userId')
      || 'demo',
    viewYear: now.getFullYear(),
    viewMonth: now.getMonth() + 1,
    selectedDate: initialDate,
    monthData: null,
    editingId: '',
    loading: false,
  };

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  function dateKey(year, month, day) {
    return `${year}-${pad(month)}-${pad(day)}`;
  }

  function parseDateKey(key) {
    const [year, month, day] = String(key || '').split('-').map((part) => Number(part));
    return {
      year: Number.isFinite(year) ? year : now.getFullYear(),
      month: Number.isFinite(month) ? month : now.getMonth() + 1,
      day: Number.isFinite(day) ? day : now.getDate(),
    };
  }

  function daysInMonth(year, month) {
    return new Date(year, month, 0).getDate();
  }

  function addDays(key, delta) {
    const { year, month, day } = parseDateKey(key);
    const date = new Date(year, month - 1, day);
    date.setDate(date.getDate() + delta);
    return dateKey(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  function todayKey() {
    const date = new Date();
    return dateKey(date.getFullYear(), date.getMonth() + 1, date.getDate());
  }

  function humanDate(key) {
    const { year, month, day } = parseDateKey(key);
    const date = new Date(year, month - 1, day);
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      weekday: 'short',
    });
  }

  function selectedDateTitle(key) {
    const base = humanDate(key);
    if (key === todayKey()) return `${base}（今天）`;
    if (key === addDays(todayKey(), 1)) return `${base}（明天）`;
    return base;
  }

  function isPastDate(key) {
    return String(key || '') < todayKey();
  }

  function canEditSelectedDate() {
    return !isPastDate(state.selectedDate);
  }

  function compactDate(key) {
    const { month, day } = parseDateKey(key);
    return `${month}/${day}`;
  }

  function kgText(item) {
    return Array.isArray(item.kg_node_ids) ? item.kg_node_ids.join(', ') : '';
  }

  async function apiGet(url) {
    const res = await fetch(url);
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  async function apiDelete(url) {
    const res = await fetch(url, { method: 'DELETE' });
    const contentType = res.headers.get('content-type') || '';
    const body = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok || body?.ok === false) {
      throw new Error(body?.result?.error || body?.message || String(body));
    }
    return body;
  }

  function setStatus(message, tone = 'muted') {
    const node = byId('plan-status');
    if (!node) return;
    node.textContent = message || '';
    node.dataset.tone = tone;
  }

  async function loadMonth({ silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    localStorage.setItem('oralsage.learningProfile.userId', state.userId);
    if (!silent) setStatus('正在同步计划...');
    try {
      const body = await apiGet(
        `/api/learning/plan/${encodeURIComponent(state.userId)}/month?year=${state.viewYear}&month=${state.viewMonth}`
      );
      state.monthData = body.result || {};
      render();
      if (!silent) setStatus('');
    } catch (err) {
      setStatus(err.message || '计划加载失败', 'error');
    } finally {
      state.loading = false;
      if (window.lucide) window.lucide.createIcons();
    }
  }

  function setView(year, month, selectedDate = null) {
    const day = selectedDate ? parseDateKey(selectedDate).day : parseDateKey(state.selectedDate).day;
    const clampedDay = Math.min(day, daysInMonth(year, month));
    state.viewYear = year;
    state.viewMonth = month;
    state.selectedDate = selectedDate || dateKey(year, month, clampedDay);
    state.editingId = '';
    loadMonth();
  }

  function render() {
    renderSelectors();
    renderCalendar();
    renderTodo();
  }

  function renderSelectors() {
    const monthLabel = byId('month-label');
    if (monthLabel) monthLabel.textContent = `${state.viewYear}年${state.viewMonth}月`;
  }

  function renderCalendar() {
    const grid = byId('calendar-grid');
    if (!grid) return;

    const first = new Date(state.viewYear, state.viewMonth - 1, 1);
    const firstDay = first.getDay() || 7;
    const leading = firstDay - 1;
    const totalDays = daysInMonth(state.viewYear, state.viewMonth);
    const prevMonth = state.viewMonth === 1 ? 12 : state.viewMonth - 1;
    const prevYear = state.viewMonth === 1 ? state.viewYear - 1 : state.viewYear;
    const nextMonth = state.viewMonth === 12 ? 1 : state.viewMonth + 1;
    const nextYear = state.viewMonth === 12 ? state.viewYear + 1 : state.viewYear;
    const prevTotalDays = daysInMonth(prevYear, prevMonth);
    const cells = [];

    for (let i = 0; i < leading; i += 1) {
      const day = prevTotalDays - leading + i + 1;
      const key = dateKey(prevYear, prevMonth, day);
      cells.push(renderCalendarCell({
        key,
        day,
        inMonth: false,
      }));
    }

    for (let day = 1; day <= totalDays; day += 1) {
      const key = dateKey(state.viewYear, state.viewMonth, day);
      cells.push(renderCalendarCell({
        key,
        day,
        inMonth: true,
      }));
    }

    const trailing = Math.max(0, 42 - cells.length);
    for (let i = 1; i <= trailing; i += 1) {
      const key = dateKey(nextYear, nextMonth, i);
      cells.push(renderCalendarCell({
        key,
        day: i,
        inMonth: false,
      }));
    }

    grid.innerHTML = cells.join('');
  }

  function renderCalendarCell({ key, day, inMonth }) {
    const dayData = state.monthData?.days?.[key] || { total: 0, completed: 0, pending: 0 };
    const classes = ['calendar-cell', inMonth ? 'in-month' : 'out-month'];
    if (key === todayKey()) classes.push('today');
    if (key === state.selectedDate) classes.push('selected');
    if (inMonth && dayData.total && dayData.completed === dayData.total) classes.push('completed');
    if (inMonth && dayData.pending) classes.push('pending');
    const meta = inMonth && dayData.total ? `${dayData.completed}/${dayData.total}` : '';
    return `
      <button class="${classes.join(' ')}" type="button" data-date="${key}" aria-label="${escapeHtml(humanDate(key))}">
        <span class="date-tile">
          <span class="day-number">${day}</span>
          <span class="day-meta">${escapeHtml(meta)}</span>
        </span>
      </button>
    `;
  }

  function selectedDayItems() {
    const day = state.monthData?.days?.[state.selectedDate];
    return Array.isArray(day?.items) ? day.items.slice() : [];
  }

  function renderTodo() {
    const items = selectedDayItems();
    const completed = items.filter((item) => item.status === 'completed').length;
    const editable = canEditSelectedDate();
    const todoDate = byId('todo-date');
    const todoStats = byId('todo-stats');
    const planForm = byId('plan-form');
    if (todoDate) todoDate.textContent = selectedDateTitle(state.selectedDate);
    if (todoStats) todoStats.textContent = items.length ? `${completed}/${items.length}` : '0/0';
    if (planForm) {
      planForm.hidden = !editable;
      planForm.classList.toggle('disabled', !editable);
    }

    const list = byId('todo-list');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = `<p class="todo-empty">${escapeHtml(compactDate(state.selectedDate))} 还没有计划</p>`;
      setStatus(editable ? '' : '已过去的日期仅支持查看');
      return;
    }

    list.innerHTML = items.map((item) => renderTodoItem(item, editable)).join('');
    setStatus(editable ? '' : '已过去的日期仅支持查看');
  }

  function renderTodoItem(item, editable) {
    const completed = item.status === 'completed';
    const linkMeta = [
      item.linked_course_title ? `课程：${item.linked_course_title}` : '',
      kgText(item) ? `知识点：${kgText(item)}` : '',
      item.deferred_count ? `延期 ${item.deferred_count} 次` : '',
    ].filter(Boolean).join(' ｜ ');
    const isEditing = editable && state.editingId === item.id;
    const checkControl = editable ? `
          <button class="todo-check-button${completed ? ' checked' : ''}" type="button" data-action="complete" data-id="${escapeHtml(item.id)}" data-completed="${completed ? 'false' : 'true'}" aria-label="${completed ? '标记为未完成' : '标记为完成'}">
            ${completed ? '<i data-lucide="check"></i>' : ''}
          </button>
    ` : `
          <span class="todo-check-button${completed ? ' checked' : ''}" aria-hidden="true">
            ${completed ? '<i data-lucide="check"></i>' : ''}
          </span>
    `;
    const tools = editable ? `
        <div class="todo-tools">
          <button class="todo-tool" type="button" data-action="edit" data-id="${escapeHtml(item.id)}" title="修改计划" aria-label="修改计划">
            <i data-lucide="pencil"></i>
          </button>
          <button class="todo-tool danger" type="button" data-action="delete" data-id="${escapeHtml(item.id)}" title="删除" aria-label="删除">
            <i data-lucide="trash-2"></i>
          </button>
        </div>
    ` : '';
    return `
      <article class="todo-item${completed ? ' completed' : ''}" data-id="${escapeHtml(item.id)}">
        <div class="todo-line">
          ${checkControl}
          <div class="todo-copy">
            <div class="todo-title">
              ${escapeHtml(item.title)}
            </div>
            ${linkMeta ? `<div class="todo-meta">${escapeHtml(linkMeta)}</div>` : ''}
          </div>
          ${tools}
        </div>
        ${isEditing ? renderItemEditor(item) : ''}
      </article>
    `;
  }

  function renderItemEditor(item) {
    return `
      <div class="link-editor" data-editor-id="${escapeHtml(item.id)}">
        <input type="text" data-link-title value="${escapeHtml(item.title || '')}" placeholder="计划内容">
        <input type="text" data-link-course value="${escapeHtml(item.linked_course_title || '')}" placeholder="关联课程">
        <input type="text" data-link-kg value="${escapeHtml(kgText(item))}" placeholder="知识点 ID，逗号分隔">
        <div class="link-editor-actions">
          <button class="link-cancel" type="button" data-action="cancel-edit" data-id="${escapeHtml(item.id)}">取消</button>
          <button class="link-save" type="button" data-action="save-edit" data-id="${escapeHtml(item.id)}">保存</button>
        </div>
      </div>
    `;
  }

  async function createItem(title) {
    await apiPost('/api/learning/plan/items', {
      user_id: state.userId,
      plan_date: state.selectedDate,
      title,
    });
    byId('plan-title').value = '';
    setStatus('已添加计划');
    await loadMonth({ silent: true });
  }

  async function completeItem(itemId, completed) {
    const body = await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/complete`, {
      user_id: state.userId,
      completed,
    });
    const memoryUpdated = body.result?.learner_memory_update?.updated;
    setStatus(completed
      ? (memoryUpdated ? '已完成，并写入学习记忆' : '已完成')
      : '已恢复为未完成');
    await loadMonth({ silent: true });
  }

  async function deferItem(itemId) {
    const item = selectedDayItems().find((entry) => entry.id === itemId);
    const toDate = item ? addDays(item.plan_date, 1) : addDays(state.selectedDate, 1);
    await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/defer`, {
      user_id: state.userId,
      to_date: toDate,
    });
    setStatus(`已延期到 ${compactDate(toDate)}`);
    await loadMonth({ silent: true });
  }

  async function deleteItem(itemId) {
    await apiDelete(`/api/learning/plan/items/${encodeURIComponent(itemId)}?user_id=${encodeURIComponent(state.userId)}`);
    setStatus('已删除计划');
    await loadMonth({ silent: true });
  }

  async function saveItem(itemId) {
    const editor = document.querySelector(`[data-editor-id="${CSS.escape(itemId)}"]`);
    if (!editor) return;
    const title = editor.querySelector('[data-link-title]')?.value || '';
    const course = editor.querySelector('[data-link-course]')?.value || '';
    const kgRaw = editor.querySelector('[data-link-kg]')?.value || '';
    const kgIds = kgRaw.split(/[,\n，]/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!title.trim()) {
      setStatus('计划内容不能为空', 'error');
      return;
    }
    await apiPost(`/api/learning/plan/items/${encodeURIComponent(itemId)}/update`, {
      user_id: state.userId,
      title: title.trim(),
      linked_course_title: course.trim() || null,
      kg_node_ids: kgIds,
    });
    state.editingId = '';
    setStatus('已更新计划');
    await loadMonth({ silent: true });
  }

  function bindEvents() {
    byId('prev-month')?.addEventListener('click', () => {
      const month = state.viewMonth === 1 ? 12 : state.viewMonth - 1;
      const year = state.viewMonth === 1 ? state.viewYear - 1 : state.viewYear;
      setView(year, month);
    });
    byId('next-month')?.addEventListener('click', () => {
      const month = state.viewMonth === 12 ? 1 : state.viewMonth + 1;
      const year = state.viewMonth === 12 ? state.viewYear + 1 : state.viewYear;
      setView(year, month);
    });
    byId('today-button')?.addEventListener('click', () => {
      const today = todayKey();
      const { year, month } = parseDateKey(today);
      setView(year, month, today);
    });
    byId('calendar-grid')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-date]');
      if (!button) return;
      const key = button.getAttribute('data-date') || state.selectedDate;
      const { year, month } = parseDateKey(key);
      if (year !== state.viewYear || month !== state.viewMonth) {
        setView(year, month, key);
        return;
      }
      state.selectedDate = key;
      state.editingId = '';
      render();
      if (window.lucide) window.lucide.createIcons();
    });
    byId('plan-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!canEditSelectedDate()) {
        setStatus('已过去的日期仅支持查看', 'error');
        return;
      }
      const input = byId('plan-title');
      const title = String(input?.value || '').trim();
      if (!title) {
        setStatus('请先写下计划内容', 'error');
        return;
      }
      try {
        await createItem(title);
      } catch (err) {
        setStatus(err.message || '添加失败', 'error');
      }
    });
    byId('todo-list')?.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const action = button.getAttribute('data-action');
      const itemId = button.getAttribute('data-id');
      try {
        if (!canEditSelectedDate()) {
          setStatus('已过去的日期仅支持查看', 'error');
          return;
        }
        if (action === 'complete') {
          await completeItem(itemId, button.getAttribute('data-completed') === 'true');
        } else if (action === 'defer') {
          await deferItem(itemId);
        } else if (action === 'delete') {
          if (window.confirm('删除这项计划？')) await deleteItem(itemId);
        } else if (action === 'edit') {
          state.editingId = state.editingId === itemId ? '' : itemId;
          renderTodo();
          if (window.lucide) window.lucide.createIcons();
        } else if (action === 'cancel-edit') {
          state.editingId = '';
          renderTodo();
          if (window.lucide) window.lucide.createIcons();
        } else if (action === 'save-edit') {
          await saveItem(itemId);
        }
      } catch (err) {
        setStatus(err.message || '操作失败', 'error');
      }
    });
  }

  bindEvents();
  loadMonth();
})();
