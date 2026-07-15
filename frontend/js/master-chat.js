(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;
  const STORAGE_KEY = 'oralsage.history.master-chat';
  const PENDING_KEY = 'oralsage.pending.master-chat';
  const jobStreams = new Map();
  const resourceRefreshes = new Map();
  const learningResources = new Map();
  const pptViewerIndices = new Map();
  const audioQueues = new Map();
  const quizQuestions = new Map();
  let activeAudio = null;

  const state = {
    messages: [],
    sessions: loadSessions(),
    activeSessionId: createId(),
    backendSessionId: null,
    userId: 'demo',
    pendingJobs: loadPendingJobs(),
  };

  function createId() {
    return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function nowTime() {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  function sessionTime() {
    return new Date().toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function loadSessions() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.map(migrateStoredSession) : [];
    } catch (_err) {
      return [];
    }
  }

  function migrateStoredSession(session) {
    if (!session || typeof session !== 'object') return session;
    const messages = Array.isArray(session.messages) ? session.messages.map((message) => {
      if (!message || typeof message !== 'object') return message;
      if (typeof message.html === 'string' && /<h3>(横向思维导图|轻量树状图)<\/h3>/.test(message.html)) {
        return {
          ...message,
          html: message.html.replace(/<h3>(横向思维导图|轻量树状图)<\/h3>/g, '<h3>思维导图</h3>'),
        };
      }
      return message;
    }) : [];
    return { ...session, messages };
  }

  function saveSessions() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions.slice(0, 30)));
  }

  function loadPendingJobs() {
    try {
      const parsed = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}');
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function savePendingJobs() {
    localStorage.setItem(PENDING_KEY, JSON.stringify(state.pendingJobs));
  }

  function refreshIcons() {
    if (window.lucide) window.lucide.createIcons();
  }

  function setConnectionStatus(label, active = false) {
    const node = byId('chat-connection-status');
    if (!node) return;
    node.classList.toggle('active', active);
    const text = node.querySelector('span:last-child');
    if (text) text.textContent = label;
  }

  function normalizeMarkdownText(text) {
    let value = String(text || '').trim();
    if (/^```(?:markdown|md)?\s*[\s\S]*```$/i.test(value)) {
      value = value.replace(/^```(?:markdown|md)?\s*/i, '').replace(/\s*```$/i, '').trim();
    }
    const escapedNewlineCount = (value.match(/\\n/g) || []).length;
    if (!/\n/.test(value) && escapedNewlineCount >= 2) {
      value = value.replace(/\\n/g, '\n');
    }
    return value;
  }

  function formatMarkdown(text) {
    const lines = normalizeMarkdownText(text).split(/\r?\n/);
    const html = [];
    let listType = null;
    let inCode = false;
    let codeLines = [];
    let tableLines = [];

    function inlineMarkdown(value) {
      const escaped = escapeHtml(value || '');
      const placeholders = [];
      const keep = (htmlValue) => {
        const token = `@@MD${placeholders.length}@@`;
        placeholders.push(htmlValue);
        return token;
      };
      const protectedCode = escaped.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${code}</code>`));
      const protectedLinks = protectedCode.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => (
        keep(`<a href="${url}" target="_blank" rel="noreferrer">${label}</a>`)
      ));
      const withLinks = protectedLinks.replace(
        /(https?:\/\/[^\s<]+[^\s<.,;:!?，。；：！？）\]])/g,
        (url) => keep(`<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`),
      );
      return withLinks
        .replace(/\*\*\*([\s\S]+?)\*\*\*/g, '<strong><em>$1</em></strong>')
        .replace(/___([\s\S]+?)___/g, '<strong><em>$1</em></strong>')
        .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
        .replace(/__([\s\S]+?)__/g, '<strong>$1</strong>')
        .replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
        .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>')
        .replace(/~~([\s\S]+?)~~/g, '<del>$1</del>')
        .replace(/@@MD(\d+)@@/g, (_, index) => placeholders[Number(index)] || '');
    }

    function closeList() {
      if (listType) {
        html.push(`</${listType}>`);
        listType = null;
      }
    }

    function openList(type) {
      if (listType === type) return;
      closeList();
      html.push(`<${type}>`);
      listType = type;
    }

    function flushCode() {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        codeLines = [];
        inCode = false;
      }
    }

    function isTableLine(line) {
      const trimmed = line.trim();
      return trimmed.includes('|') && trimmed.split('|').length >= 3;
    }

    function isTableDivider(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }

    function splitTableRow(line) {
      return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
    }

    function flushTable() {
      if (!tableLines.length) return;
      const rowsOnly = tableLines.filter((line) => line.trim() && !isTableDivider(line));
      if (rowsOnly.length >= 2) {
        const headers = splitTableRow(rowsOnly[0]);
        const rows = rowsOnly.slice(1).map(splitTableRow);
        const colCount = Math.max(headers.length, ...rows.map((row) => row.length));
        html.push(
          `<div class="markdown-table-wrap"><table><thead><tr>${Array.from({ length: colCount }, (_, index) => `<th>${inlineMarkdown(headers[index] || '')}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${Array.from({ length: colCount }, (_, index) => `<td>${inlineMarkdown(row[index] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`,
        );
      } else {
        tableLines.forEach((line) => html.push(`<p>${inlineMarkdown(line)}</p>`));
      }
      tableLines = [];
    }

    for (const line of lines) {
      if (line.trim().startsWith('```')) {
        if (inCode) {
          flushCode();
        } else {
          closeList();
          flushTable();
          inCode = true;
          codeLines = [];
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      if (!line.trim() && tableLines.length) {
        continue;
      }
      if (isTableLine(line)) {
        closeList();
        tableLines.push(line);
        continue;
      }
      flushTable();
      if (/^\s*---+\s*$/.test(line)) {
        closeList();
        html.push('<hr>');
        continue;
      }
      const heading = line.match(/^\s*(#{1,6})\s*(.+?)\s*#*\s*$/);
      if (heading) {
        closeList();
        const level = Math.min(6, Math.max(2, heading[1].length + 1));
        html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s*>\s*/.test(line)) {
        closeList();
        html.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s*/, ''))}</blockquote>`);
        continue;
      }
      if (/^\s*[-*+•]\s+/.test(line)) {
        openList('ul');
        html.push(`<li>${inlineMarkdown(line.replace(/^\s*[-*+•]\s+/, ''))}</li>`);
        continue;
      }
      if (/^\s*\d+[.)、]\s*/.test(line)) {
        openList('ol');
        html.push(`<li>${inlineMarkdown(line.replace(/^\s*\d+[.)、]\s*/, ''))}</li>`);
        continue;
      }
      if (!line.trim()) {
        closeList();
        flushTable();
        html.push('');
        continue;
      }
      closeList();
      html.push(`<p>${inlineMarkdown(line)}</p>`);
    }
    flushCode();
    flushTable();
    closeList();
    return html.join('');
  }

  function resultAnswer(result) {
    if (!result) return '未返回内容。';
    if (typeof result.answer === 'string' && result.answer.trim()) return result.answer;
    const final = result.final_result || result.result || {};
    if (typeof final.answer === 'string') return final.answer;
    if (typeof final.summary === 'string') return final.summary;
    if (typeof final.markdown === 'string') return final.markdown;
    if (typeof final.review === 'string') return final.review;
    if (typeof final.report === 'string') return final.report;
    if (final.result?.report_text) return final.result.report_text;
    if (final.result?.markdown) return final.result.markdown;
    if (final.result?.review) return final.result.review;
    if (final.result?.report) return final.result.report;
    if (typeof final.message === 'string') return final.message;
    if (Array.isArray(final.recommendations)) return '已生成学习计划建议。';
    if (final.session?.topic) return `已为“${final.session.topic}”建立学习课程。`;
    return '任务已完成。';
  }

  function authorsText(article) {
    const authors = Array.isArray(article?.authors) ? article.authors.filter(Boolean) : [];
    if (!authors.length) return '未提供作者';
    return authors.slice(0, 4).join('、') + (authors.length > 4 ? ' 等' : '');
  }

  function translatedValue(article, key) {
    const translation = article?.translation || {};
    return translation.source === 'llm' ? String(translation[key] || '').replace(/\s+/g, ' ').trim() : '';
  }

  function compactText(text, fallback = '暂无内容') {
    return String(text || fallback).replace(/\s+/g, ' ').trim();
  }

  function literatureResultHtml(result) {
    const final = result?.final_result || result?.result || result || {};
    if (final.module !== 'frontier_literature') return '';

    const terms = final.terms || {};
    const articles = Array.isArray(final.chat_articles) ? final.chat_articles : (Array.isArray(final.articles) ? final.articles : []);
    const sources = Array.isArray(final.sources) && final.sources.length ? final.sources.join(' + ') : '未获得可用来源';
    const query = final.query || '';
    const months = terms.recommended_months || '-';
    const keywords = terms.recommended_keywords || query || '-';

    const articleHtml = articles.length ? articles.map((article, index) => {
      const title = compactText(translatedValue(article, 'title_zh') || article.title || 'No title');
      const englishTitle = compactText(article.title || 'No title');
      const summaryZh = compactText(translatedValue(article, 'summary_zh'), '暂未生成中文摘要翻译。');
      const summaryEn = compactText(article.summary, 'Abstract not available');
      const journal = compactText(article.journal, 'Unknown journal');
      const date = compactText(article.publish_date || article.year, 'Unknown date');
      const source = compactText(article.source, 'Unknown source');
      const score = Number(article.relevance_percent);
      const link = compactText(article.link, '');
      const doi = compactText(article.doi, '');

      return `
        <article class="literature-chat-card">
          <div class="literature-chat-rank">${index + 1}</div>
          <div class="literature-chat-body">
            <h3>${escapeHtml(title)}</h3>
            <p class="literature-chat-title-en">${escapeHtml(englishTitle)}</p>
            <div class="literature-chat-meta">
              <span>${escapeHtml(source)}</span>
              <span>${escapeHtml(journal)}</span>
              <span>${escapeHtml(date)}</span>
              ${Number.isFinite(score) && score > 0 ? `<span>相关度 ${escapeHtml(String(Math.round(score)))}%</span>` : ''}
            </div>
            <p><strong>作者：</strong>${escapeHtml(authorsText(article))}</p>
            <p><strong>中文摘要：</strong>${escapeHtml(summaryZh)}</p>
            <details class="literature-chat-abstract">
              <summary>英文摘要</summary>
              <p>${escapeHtml(summaryEn)}</p>
            </details>
            <div class="literature-chat-links">
              ${doi ? `<span>DOI：${escapeHtml(doi)}</span>` : ''}
              ${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer">打开原文</a>` : ''}
            </div>
          </div>
        </article>
      `;
    }).join('') : '<p class="literature-chat-empty">未检索到可展示的真实文献结果。</p>';

    const review = String(final.review || final.report || '').trim();
    const reviewHtml = review
      ? `<section class="literature-chat-review"><h3>综合解读</h3>${formatMarkdown(review)}</section>`
      : '';

    return `
      <section class="literature-chat-result">
        <h2>文献检索结果</h2>
        <p class="literature-chat-brief">已按"${escapeHtml(query)}"检索近 ${escapeHtml(String(months))} 个月文献，当前获得 ${escapeHtml(String(articles.length))} 篇。</p>
        <div class="literature-chat-search-meta">
          <span>关键词：${escapeHtml(keywords)}</span>
          <span>数据来源：${escapeHtml(sources)}</span>
        </div>
        <div class="literature-chat-list">${articleHtml}</div>
        ${reviewHtml}
        <p class="literature-chat-disclaimer">内容仅用于口腔医学学习与科研参考，不构成临床诊疗建议。</p>
      </section>
    `;
  }

  function renderStageExamHtml(exam) {
    if (!exam || !Array.isArray(exam.questions) || exam.questions.length === 0) return '';
    const summary = exam.summary || {};
    const questionCount = summary.question_count || exam.questions.length;
    const sectionCount = summary.section_count || 0;
    const questions = exam.questions.map((question, index) => {
      const options = Array.isArray(question.options) ? question.options : [];
      const optionHtml = options.map((option) => (
        '<label class="stage-exam-chat-option">' +
          '<input type="radio" disabled>' +
          '<span>' + escapeHtml(option) + '</span>' +
        '</label>'
      )).join('');
      return (
        '<article class="stage-exam-chat-question">' +
          '<p class="stage-exam-chat-title">' + (index + 1) + '. ' + escapeHtml(question.stem || '阶段卷题目') + '</p>' +
          '<div class="stage-exam-chat-options">' + optionHtml + '</div>' +
        '</article>'
      );
    }).join('');

    return (
      '<section class="stage-exam-chat-result">' +
        '<h2>阶段卷</h2>' +
        '<p class="stage-exam-chat-meta">共 ' + escapeHtml(String(questionCount)) +
          ' 题' + (sectionCount ? '，覆盖 ' + escapeHtml(String(sectionCount)) + ' 个小节' : '') + '</p>' +
        '<div class="stage-exam-chat-list">' + questions + '</div>' +
      '</section>'
    );
  }

  function renderSelectPlanCandidates(candidates) {
    if (!Array.isArray(candidates) || candidates.length === 0) return '';

    const items = candidates.map(function (c) {
      const title = escapeHtml(c.title || '未命名任务');
      const courseHtml = c.linked_course_title
        ? ' <span class="plan-candidate-course">' + escapeHtml(c.linked_course_title) + '</span>'
        : '';
      return (
        '<button type="button" class="plan-candidate-btn" data-plan-item-id="' + escapeHtml(c.plan_item_id || '') + '">' +
        '<span class="plan-candidate-title">' + title + '</span>' + courseHtml +
        '</button>'
      );
    }).join('');

    return '<div class="plan-candidates"><div class="plan-candidates-list">' + items + '</div></div>';
  }

  function renderRouteDetails(routerResult) {
    const route = routerResult?.route || {};
    if (!route.intent && !route.reason) return '';
    return `
      <details class="route-card">
        <summary>调度详情</summary>
        <div class="route-detail-grid">
          <span>${escapeHtml(route.intent || 'unknown')}</span>
          <p>${escapeHtml(route.reason || '未提供调度理由')}</p>
        </div>
      </details>
    `;
  }

  function renderJobProgress(message) {
    const streamText = String(message.streamText || '');
    if (streamText) {
      return `
        <section class="job-stream" data-job-id="${escapeHtml(message.jobId || '')}" aria-live="polite">
          <div class="job-stream-status">
            <span class="job-spinner" aria-hidden="true"></span>
            <strong>${escapeHtml(message.statusText || '正在生成本节讲稿')}</strong>
          </div>
          <div class="job-stream-copy assistant-markdown">${formatMarkdown(streamText)}<span class="stream-caret" aria-hidden="true"></span></div>
        </section>
      `;
    }
    const steps = Array.isArray(message.steps) ? message.steps.slice(-4) : [];
    return `
      <section class="job-progress" data-job-id="${escapeHtml(message.jobId || '')}">
        <div class="job-progress-head">
          <span class="job-spinner" aria-hidden="true"></span>
          <strong>${escapeHtml(message.statusText || '正在处理')}</strong>
        </div>
        <div class="job-progress-track"><span></span></div>
        <ol>${steps.map((step, index) => `<li class="${index === steps.length - 1 ? 'current' : ''}">${escapeHtml(step)}</li>`).join('')}</ol>
        <p>任务会在后台继续运行</p>
      </section>
    `;
  }

  function optionLetter(index) {
    return String.fromCharCode(65 + index);
  }

  function quizQuestionKey(sessionId, nodeId, index, question) {
    return [sessionId || 'session', nodeId || 'node', question?.id || `q${index + 1}`].join(':');
  }

  function renderTutorQuestions(questions, sessionId = '', nodeId = '') {
    if (!Array.isArray(questions) || !questions.length) return '';
    return `
      <details class="section-quiz">
        <summary>本节自测 · ${questions.length} 题</summary>
        <div class="section-quiz-list">
          ${questions.map((question, index) => {
            const key = quizQuestionKey(sessionId, nodeId, index, question);
            quizQuestions.set(key, {
              sessionId,
              nodeId,
              question,
              index,
            });
            const options = Array.isArray(question.options) ? question.options : [];
            const optionHtml = options.length
              ? options.map((option, optionIndex) => {
                const letter = optionLetter(optionIndex);
                return `
                  <button type="button" class="quiz-option" data-quiz-option="${letter}">
                    <span class="quiz-option-letter">${letter}</span>
                    <span>${escapeHtml(option)}</span>
                  </button>
                `;
              }).join('')
              : '<p class="section-quiz-empty">本题暂未提供选项</p>';
            return `
            <article class="section-quiz-item" data-quiz-key="${escapeHtml(key)}">
              <h4>${index + 1}. ${escapeHtml(question.stem || '自测题')}</h4>
              <div class="section-quiz-choice">
                <button type="button" class="quiz-mode-btn primary" data-quiz-start>开始作答</button>
                <button type="button" class="quiz-mode-btn" data-quiz-skip>稍后再答</button>
              </div>
              <div class="section-quiz-options" hidden>
                ${optionHtml}
                <button type="button" class="quiz-submit" data-quiz-submit disabled>提交答案</button>
              </div>
              <div class="quiz-feedback" aria-live="polite"></div>
            </article>
          `;
          }).join('')}
        </div>
      </details>
    `;
  }

  function renderLectureCopy(teachingText, sessionId, nodeId) {
    const baseKey = `${sessionId}:${nodeId}`;
    return `
      <article class="lecture-copy assistant-markdown" data-lecture-key="${escapeHtml(baseKey)}">
        ${formatMarkdown(teachingText)}
      </article>
    `;
  }

  function mindmapNodeTitle(node) {
    return String(node?.name || node?.title || node?.label || '').trim();
  }

  function renderMindmapTree(node, currentNodeId, depth = 0) {
    if (!node || depth > 3) return '';
    const title = mindmapNodeTitle(node);
    const children = Array.isArray(node.children) ? node.children.filter(Boolean) : [];
    if (!title && !children.length) return '';
    const nodeId = String(node.id || node.node_id || '');
    const active = currentNodeId && nodeId === currentNodeId;
    const classes = [
      depth === 0 ? 'mindmap-node-root' : '',
      `mindmap-depth-${depth}`,
      children.length ? 'has-children' : '',
      active ? 'active' : '',
    ].filter(Boolean).join(' ');
    return `
      <li class="${classes}">
        ${title ? `<span>${escapeHtml(title)}</span>` : ''}
        ${children.length ? `<ol>${children.map((child) => renderMindmapTree(child, currentNodeId, depth + 1)).join('')}</ol>` : ''}
      </li>
    `;
  }

  function renderMindmap(final, currentNodeId) {
    const root = final?.mindmap?.root || null;
    if (!root) return '';
    const tree = renderMindmapTree(root, currentNodeId);
    if (!tree) return '';
    return `
      <section class="mindmap-resource">
        <div class="resource-heading"><div><span class="mode-label">学习地图</span><h3>思维导图</h3></div><span class="resource-state">已同步</span></div>
        <ol class="mindmap-tree">${tree}</ol>
      </section>
    `;
  }

  function renderLearningResult(routerResult, step) {
    const final = routerResult?.final_result || {};
    const session = final.session || {};
    const sessionId = session.id || step?.session_id || '';
    const nodeId = step?.outline_node_id || final.current_outline_node_id || '';
    const nodes = final.outline?.nodes || [];
    const currentNode = nodes.find((node) => node.id === nodeId) || nodes[0] || {};
    const title = currentNode.title || session.topic || '本节讲解';
    const teachingText = String(step?.teaching_text || '').trim();
    if (!teachingText) {
      const base = step?.lesson_completed
        ? '本阶段内容已经完成。阶段卷已准备好，可以开始作答。'
        : resultAnswer(routerResult);
      return `
        <section class="learning-response">
          <div class="learning-section-head"><div><span class="mode-label">学习模式</span><h2>${escapeHtml(title)}</h2></div></div>
          ${renderMindmap(final, nodeId)}
          <div class="assistant-markdown">${formatMarkdown(base)}</div>
          ${step?.stage_exam ? renderStageExamHtml(step.stage_exam) : ''}
          ${renderRouteDetails(routerResult)}
        </section>
      `;
    }

    return `
      <section class="learning-response" data-learning-session="${escapeHtml(sessionId)}" data-outline-node="${escapeHtml(nodeId)}">
        <header class="learning-section-head">
          <div>
            <span class="mode-label">学习模式</span>
            <h2>${escapeHtml(title)}</h2>
          </div>
          <button type="button" class="audio-command" data-audio-key="${escapeHtml(`${sessionId}:${nodeId}`)}" data-audio-ready-label="播放本节讲稿" disabled title="本节语音正在准备">
            <i data-lucide="loader-circle"></i><span>语音准备中</span>
          </button>
        </header>
        ${renderMindmap(final, nodeId)}
        ${renderLectureCopy(teachingText, sessionId, nodeId)}
        <div class="learning-actions">
          <button type="button" class="learning-action primary" data-chat-prompt="继续">
            <i data-lucide="arrow-right"></i><span>继续下一节</span>
          </button>
          <button type="button" class="learning-action" data-focus-composer>
            <i data-lucide="message-circle-question"></i><span>针对本节提问</span>
          </button>
        </div>
        ${renderTutorQuestions(step?.questions, sessionId, nodeId)}
        <section class="slide-resource" data-slide-resource="${escapeHtml(`${sessionId}:${nodeId}`)}">
          <div class="resource-heading"><div><span class="mode-label">同步演示</span><h3>本节 PPT</h3></div><span class="resource-state">生成中</span></div>
          <div class="slide-loading"><span></span><p>正在整理本节演示内容</p></div>
        </section>
        ${step?.stage_exam ? renderStageExamHtml(step.stage_exam) : ''}
        ${renderRouteDetails(routerResult)}
      </section>
    `;
  }

  function renderStudyPlanResult(routerResult) {
    const final = routerResult?.final_result || {};
    const items = Array.isArray(final.recommendations) ? final.recommendations : [];
    if (!items.length) return `<div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>${renderRouteDetails(routerResult)}`;
    const totalMinutes = items.reduce((sum, item) => sum + Number(item.estimated_minutes || 0), 0);
    return `
      <section class="plan-response">
        <header class="result-heading">
          <div><span class="mode-label">计划模式</span><h2>${escapeHtml(final.plan_date || '学习安排')}</h2></div>
          <strong>${escapeHtml(String(totalMinutes))} 分钟</strong>
        </header>
        <div class="plan-recommendation-list">
          ${items.map((item, index) => `
            <article class="plan-recommendation">
              <div class="plan-index">${String(index + 1).padStart(2, '0')}</div>
              <div><h3>${escapeHtml(item.title || '学习任务')}</h3><p>${escapeHtml(item.reason || item.notes || '')}</p></div>
              <div class="plan-item-actions">
                <span><i data-lucide="clock-3"></i>${escapeHtml(String(item.estimated_minutes || 0))} 分钟</span>
                <button
                  type="button"
                  data-start-recommendation="${escapeHtml(item.recommendation_id || '')}"
                  data-plan-date="${escapeHtml(final.plan_date || '')}"
                  data-recommendation-title="${escapeHtml(item.title || '学习任务')}"
                >开始学习</button>
              </div>
            </article>
          `).join('')}
        </div>
        <a class="open-plan-link" href="/pages/learning-plan.html"><i data-lucide="calendar-days"></i><span>打开学习计划</span></a>
        ${renderRouteDetails(routerResult)}
      </section>
    `;
  }

  function renderClarificationResult(routerResult) {
    return `
      <section class="clarification-response">
        <div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>
        <div class="clarification-actions">
          <button type="button" data-chat-prompt="我想系统学习一个口腔医学主题">学习主题</button>
          <button type="button" data-chat-prompt="请推荐今天的学习计划">学习计划</button>
          <button type="button" data-chat-prompt="我想检索一个口腔医学主题的最新文献">文献检索</button>
        </div>
        ${renderRouteDetails(routerResult)}
      </section>
    `;
  }

  function renderJobResult(payload) {
    const routerResult = payload?.router_result || {};
    const intent = routerResult.route?.intent;
    if (!routerResult.ok) {
      return `<section class="error-response"><h3>处理失败</h3><p>${escapeHtml(routerResult.error || '请求处理失败')}</p></section>`;
    }
    if (routerResult.next_action === 'select_plan' && Array.isArray(routerResult.final_result?.candidates)) {
      return `
        <section class="plan-select-response">
          <div class="assistant-markdown">${formatMarkdown(routerResult.final_result.message || '请选择要继续的学习计划。')}</div>
          ${renderSelectPlanCandidates(routerResult.final_result.candidates)}
          ${renderRouteDetails(routerResult)}
        </section>
      `;
    }
    if (intent === 'learning') return renderLearningResult(routerResult, payload?.learning_step);
    if (intent === 'study_plan') return renderStudyPlanResult(routerResult);
    if (intent === 'literature') {
      return literatureResultHtml(routerResult) || `<div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>`;
    }
    if (intent === 'clarification') return renderClarificationResult(routerResult);
    return `<div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>${renderRouteDetails(routerResult)}`;
  }

  function isNearBottom(log) {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  }

  function scrollToBottom(log) {
    log.scrollTop = log.scrollHeight;
  }

  function renderMessage(message, options = {}) {
    const log = byId('agent-chat-log');
    if (!log) return;
    const row = document.createElement('article');
    row.className = `chat-message ${message.role}`;
    const roleText = message.role === 'user' ? '你' : 'OralSage';
    const avatar = message.role === 'user' ? 'ME' : 'OS';
    let content = message.html || (message.role === 'assistant' ? formatMarkdown(message.content) : escapeHtml(message.content));
    if (message.kind === 'job') content = renderJobProgress(message);
    if (message.kind === 'result') content = renderJobResult(message.payload);
    row.innerHTML = `
      <div class="message-avatar">${avatar}</div>
      <div class="message-stack">
        <div class="message-role">${roleText} · ${escapeHtml(message.time || nowTime())}</div>
        <div class="chat-bubble assistant-markdown">${content}</div>
        ${message.routeHtml || ''}
      </div>
    `;
    log.appendChild(row);
    if (options.follow !== false) scrollToBottom(log);
    refreshIcons();
  }

  function renderMessages() {
    const log = byId('agent-chat-log');
    if (!log) return;
    const shouldFollow = isNearBottom(log);
    const scrollTop = log.scrollTop;
    log.innerHTML = '';
    state.messages.forEach((message) => renderMessage(message, { follow: false }));
    setEmptyState();
    hydrateLearningResources();
    refreshIcons();
    if (shouldFollow) {
      scrollToBottom(log);
    } else {
      log.scrollTop = scrollTop;
    }
  }

  function setEmptyState() {
    const stage = document.querySelector('.chat-stage');
    if (!stage) return;
    stage.classList.toggle('is-empty', state.messages.length === 0);
  }

  function setBusy(isBusy) {
    const send = byId('agent-send');
    const input = byId('agent-message');
    if (send) {
      send.disabled = isBusy;
      send.innerHTML = isBusy
        ? '<span>已提交</span><i data-lucide="loader-circle"></i>'
        : '<span>发送</span><i data-lucide="arrow-up"></i>';
    }
    if (input) input.disabled = false;
    refreshIcons();
  }

  function appendMessage(message) {
    state.messages.push(message);
    renderMessage(message);
    setEmptyState();
    updateSessionTitle();
  }

  function optionText(question, letter) {
    const index = letter.charCodeAt(0) - 65;
    const options = Array.isArray(question?.options) ? question.options : [];
    return options[index] || letter;
  }

  function assessmentFeedbackHtml(result) {
    const assessment = result?.assessment || {};
    const correct = assessment.is_correct === true;
    const title = correct ? '回答正确' : '答案需要修正';
    const parts = [assessment.explanation];
    if (assessment.option_error_analysis) parts.push(assessment.option_error_analysis);
    if (assessment.correct_answer_explanation) parts.push(assessment.correct_answer_explanation);
    const suggestion = assessment.recommendation?.remedial_suggestion;
    if (suggestion) parts.push(suggestion);
    const detail = parts.filter(Boolean).join('\n\n') || (correct ? '这道题已经掌握。' : '请回到本节讲稿再复习一次。');
    return `
      <section class="quiz-feedback-card ${correct ? 'correct' : 'incorrect'}">
        <h3>${title}</h3>
        <p>你的选择：${escapeHtml(assessment.selected_option || '')}${assessment.correct_option ? `，正确答案：${escapeHtml(assessment.correct_option)}` : ''}</p>
        <div class="assistant-markdown">${formatMarkdown(detail)}</div>
      </section>
    `;
  }

  async function submitSectionQuizAnswer(article, button) {
    const key = article?.dataset.quizKey || '';
    const record = quizQuestions.get(key);
    const selected = article?.dataset.selectedOption || '';
    const feedback = article?.querySelector('.quiz-feedback');
    if (!record || !selected) return;
    const sessionId = record.sessionId || state.backendSessionId;
    if (!sessionId) return;

    button.disabled = true;
    if (feedback) {
      feedback.className = 'quiz-feedback loading';
      feedback.textContent = '正在评估答案...';
    }
    appendMessage({
      role: 'user',
      content: `本节自测第 ${record.index + 1} 题选择 ${selected}：${optionText(record.question, selected)}`,
      time: nowTime(),
    });

    try {
      const body = await apiPost('/api/learning/answer', {
        session_id: sessionId,
        question: record.question,
        answer: selected,
        use_llm_policy: false,
      });
      const execution = body?.result?.execution || {};
      const result = execution.result || {};
      if (execution.error) throw new Error(execution.error);
      const html = assessmentFeedbackHtml(result);
      if (feedback) {
        feedback.className = 'quiz-feedback done';
        feedback.innerHTML = html;
      }
      article.querySelectorAll('button').forEach((node) => { node.disabled = true; });
      appendMessage({ role: 'assistant', html, time: nowTime() });
    } catch (error) {
      button.disabled = false;
      if (feedback) {
        feedback.className = 'quiz-feedback error';
        feedback.textContent = `评估失败：${error.message || error}`;
      }
    }
  }

  function updateSessionTitle() {
    const firstUser = state.messages.find((item) => item.role === 'user');
    const title = firstUser ? firstUser.content.slice(0, 28) : '新建对话';
    const existing = state.sessions.find((item) => item.id === state.activeSessionId);
    if (existing) {
      existing.title = title;
      existing.time = sessionTime();
      existing.messages = state.messages;
      existing.backendSessionId = state.backendSessionId;
    } else {
      state.sessions.unshift({
        id: state.activeSessionId,
        title,
        time: sessionTime(),
        messages: state.messages,
        backendSessionId: state.backendSessionId,
      });
    }
    saveSessions();
    renderSessions();
  }

  function renderSessions() {
    const list = byId('history-list');
    if (!list) return;
    list.innerHTML = state.sessions.map((session) => {
      const active = session.id === state.activeSessionId ? ' active' : '';
      return `
        <button type="button" class="history-item${active}" data-session-id="${escapeHtml(session.id)}">
          <div class="history-title">${escapeHtml(session.title)}</div>
          <div class="history-time">${escapeHtml(session.time)}</div>
        </button>
      `;
    }).join('');
  }

  function resetSession() {
    state.activeSessionId = createId();
    state.backendSessionId = null;
    state.messages = [];
    const log = byId('agent-chat-log');
    if (log) log.innerHTML = '';
    const input = byId('agent-message');
    if (input) input.value = '';
    setEmptyState();
    renderSessions();
    setBusy(false);
  }

  function restoreSession(sessionId) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    state.activeSessionId = session.id;
    state.messages = Array.isArray(session.messages) ? session.messages : [];
    state.backendSessionId = session.backendSessionId || null;
    renderMessages();
    renderSessions();
    const pending = Object.values(state.pendingJobs).some((item) => item.sessionId === session.id);
    setBusy(pending);
    setConnectionStatus(pending ? '生成中' : '就绪', pending);
  }

  function historyPayload() {
    return state.messages
      .filter((item) => typeof item.content === 'string' && item.content.trim())
      .slice(-8)
      .map((item) => ({ role: item.role, content: item.content }));
  }

  function parseUrlParams(search) {
    const params = new URLSearchParams(search || window.location.search);
    const planItemId = params.get('plan_item_id') || '';
    const userId = params.get('user_id') || 'demo';
    return { planItemId, userId };
  }

  function cleanUrlParams() {
    const url = new URL(window.location.href);
    url.searchParams.delete('plan_item_id');
    url.searchParams.delete('user_id');
    window.history.replaceState({}, '', url.toString());
  }

  async function createRouterJob(payload) {
    const response = await fetch('/api/router/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      let detail = '';
      try { detail = await response.text(); } catch (_e) { /* ignore */ }
      throw new Error(`HTTP ${response.status}${detail ? ': ' + detail : ''}`);
    }

    return response.json();
  }

  function findJobMessage(jobId) {
    const pending = state.pendingJobs[jobId];
    const session = state.sessions.find((item) => item.id === pending?.sessionId);
    const messages = session?.messages || (pending?.sessionId === state.activeSessionId ? state.messages : []);
    return { pending, session, messages, message: messages.find((item) => item.jobId === jobId) };
  }

  function persistJobSession(session) {
    if (session?.id === state.activeSessionId) {
      state.messages = session.messages;
      state.backendSessionId = session.backendSessionId || state.backendSessionId;
      renderMessages();
    }
    saveSessions();
    renderSessions();
  }

  function handleJobEvent(jobId, event) {
    const found = findJobMessage(jobId);
    if (!found.pending || !found.message) return;
    const eventId = Number(event.id || 0);
    if (eventId <= Number(found.pending.lastEventId || 0)) return;
    found.pending.lastEventId = eventId;

    if (event.type === 'route_ready') {
      found.message.statusText = event.message || '工作流已确认';
      found.message.route = event.data?.route || null;
      if (event.data?.session_id) {
        found.session.backendSessionId = event.data.session_id;
      }
    } else if (event.type === 'content_delta') {
      found.message.streamText = `${found.message.streamText || ''}${event.data?.delta || ''}`;
      found.message.statusText = event.message || '正在生成本节讲稿';
    } else if (event.type === 'content_replace') {
      found.message.streamText = String(event.data?.text || '');
      found.message.statusText = event.message || '正在整理本节讲稿';
    } else if (event.type === 'complete') {
      found.message.kind = 'result';
      found.message.payload = event.data || {};
      found.message.content = resultAnswer(event.data?.router_result || {});
      found.message.statusText = '已完成';
      delete found.message.streamText;
      const backendId = event.data?.router_result?.final_result?.session?.id;
      if (backendId) found.session.backendSessionId = backendId;
      delete state.pendingJobs[jobId];
      jobStreams.get(jobId)?.close();
      jobStreams.delete(jobId);
    } else if (event.type === 'error') {
      found.message.kind = 'error';
      found.message.content = event.data?.error || event.message || '请求处理失败。';
      delete state.pendingJobs[jobId];
      jobStreams.get(jobId)?.close();
      jobStreams.delete(jobId);
    } else {
      found.message.statusText = event.message || found.message.statusText;
      found.message.steps = [...(found.message.steps || []), event.message].filter(Boolean).slice(-5);
    }

    savePendingJobs();
    persistJobSession(found.session);
    const activePending = Object.values(state.pendingJobs).some((item) => item.sessionId === state.activeSessionId);
    setBusy(activePending);
    setConnectionStatus(activePending ? '生成中' : '就绪', activePending);
  }

  function connectJob(jobId) {
    if (!jobId || jobStreams.has(jobId) || !state.pendingJobs[jobId]) return;
    const after = Number(state.pendingJobs[jobId].lastEventId || 0);
    const source = new EventSource(`/api/router/jobs/${encodeURIComponent(jobId)}/stream?after=${after}`);
    jobStreams.set(jobId, source);
    source.addEventListener('router_job', (rawEvent) => {
      try {
        handleJobEvent(jobId, JSON.parse(rawEvent.data));
      } catch (_error) {
        // A malformed progress event should not terminate the underlying job.
      }
    });
    source.onerror = () => {
      source.close();
      jobStreams.delete(jobId);
      if (state.pendingJobs[jobId]) {
        setTimeout(() => connectJob(jobId), 1200);
      }
    };
  }

  async function startRouterJob(payload) {
    const snapshot = await createRouterJob(payload);
    const jobId = snapshot.job_id;
    if (!jobId) throw new Error('后端没有返回任务 ID。');
    appendMessage({
      role: 'assistant',
      kind: 'job',
      jobId,
      content: '',
      statusText: '正在连接总控',
      steps: ['请求已提交'],
      time: nowTime(),
    });
    state.pendingJobs[jobId] = {
      sessionId: state.activeSessionId,
      lastEventId: 0,
    };
    savePendingJobs();
    updateSessionTitle();
    setConnectionStatus('生成中', true);
    connectJob(jobId);
  }

  async function sendMessage(text = null) {
    const input = byId('agent-message');
    const raw = (text ?? input?.value ?? '').trim();
    if (!raw) return;
    if (input) input.value = '';

    const history = historyPayload();
    appendMessage({ role: 'user', content: raw, time: nowTime() });
    setBusy(true);

    try {
      const context = { user_id: state.userId || 'demo' };
      if (state.backendSessionId) {
        context.session_id = state.backendSessionId;
      }

      await startRouterJob({
        message: raw,
        history,
        context,
      });
    } catch (error) {
      appendMessage({
        role: 'assistant',
        content: '请求失败：\n\n' + (error.message || error),
        time: nowTime(),
      });
    } finally {
      const pending = Object.values(state.pendingJobs).some((item) => item.sessionId === state.activeSessionId);
      setBusy(pending);
      input?.focus();
    }
  }

  async function autoSubmitPlanItem(planItemId, userId) {
    setBusy(true);
    try {
      appendMessage({ role: 'user', content: '开始选中的学习计划', time: nowTime() });
      await startRouterJob({
        message: '',
        history: [],
        context: { user_id: userId, plan_item_id: planItemId },
      });
    } catch (error) {
      appendMessage({
        role: 'assistant',
        content: '请求失败：\n\n' + (error.message || error),
        time: nowTime(),
      });
    } finally {
      const pending = Object.values(state.pendingJobs).some((item) => item.sessionId === state.activeSessionId);
      setBusy(pending);
      cleanUrlParams();
      const input = byId('agent-message');
      input?.focus();
    }
  }

  async function autoSubmitRecommendation(recommendationId, planDate, title) {
    if (!recommendationId) return;
    setBusy(true);
    try {
      appendMessage({ role: 'user', content: title || '开始选中的学习推荐', time: nowTime() });
      await startRouterJob({
        message: '',
        history: [],
        context: {
          user_id: state.userId || 'demo',
          recommendation_id: recommendationId,
          plan_date: planDate || '',
        },
      });
    } catch (error) {
      appendMessage({
        role: 'assistant',
        content: '请求失败：\n\n' + (error.message || error),
        time: nowTime(),
      });
    } finally {
      const pending = Object.values(state.pendingJobs).some((item) => item.sessionId === state.activeSessionId);
      setBusy(pending);
      byId('agent-message')?.focus();
    }
  }

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!response.ok) return null;
    return response.json();
  }

  function pptStateLabel(payload) {
    const result = payload?.result || payload || {};
    const value = result.pptx_status?.status || result.pptx?.status || result.status || 'pending';
    return String(value || 'pending');
  }

  function renderSlidePreview(key, resource) {
    const [sessionId, nodeId] = key.split(':');
    const preview = resource?.preview?.result || {};
    const slides = Array.isArray(preview.slides) ? preview.slides : [];
    const pptState = pptStateLabel(resource?.ppt);
    const readyForDownload = ['partial', 'ready'].includes(pptState);
    const previewReady = preview.status === 'ready' && slides.length > 0;
    const stateText = previewReady
      ? '真实页面'
      : (preview.status === 'failed' ? '预览生成失败' : '同步生成中');

    if (!pptViewerIndices.has(key) && slides.length) {
      const sectionIndex = slides.findIndex((slide) => slide.source_outline_node_id === nodeId);
      pptViewerIndices.set(key, sectionIndex >= 0 ? sectionIndex : 0);
    }
    const requestedIndex = Number(pptViewerIndices.get(key) || 0);
    const currentIndex = Math.max(0, Math.min(requestedIndex, Math.max(0, slides.length - 1)));
    pptViewerIndices.set(key, currentIndex);
    const current = slides[currentIndex] || null;

    const viewerHtml = current ? `
      <div class="ppt-real-viewer" data-ppt-viewer="${escapeHtml(key)}" tabindex="0" aria-label="PPT 页面查看器">
        <div class="ppt-viewer-toolbar">
          <button type="button" data-ppt-nav="prev" ${currentIndex === 0 ? 'disabled' : ''} title="上一页" aria-label="上一页"><i data-lucide="chevron-left"></i></button>
          <div><strong>${currentIndex + 1} / ${slides.length}</strong><span>${escapeHtml(current.title || `第 ${currentIndex + 1} 页`)}</span></div>
          <button type="button" data-ppt-nav="next" ${currentIndex >= slides.length - 1 ? 'disabled' : ''} title="下一页" aria-label="下一页"><i data-lucide="chevron-right"></i></button>
        </div>
        <div class="ppt-real-stage">
          <img src="${escapeHtml(current.image_url || '')}" alt="${escapeHtml(current.title || `PPT 第 ${currentIndex + 1} 页`)}" width="1920" height="1080">
        </div>
        <div class="ppt-thumbnail-strip" role="tablist" aria-label="PPT 页面">
          ${slides.map((slide, index) => `
            <button type="button" class="ppt-thumbnail${index === currentIndex ? ' active' : ''}" data-ppt-page="${index}" role="tab" aria-selected="${index === currentIndex ? 'true' : 'false'}" title="${escapeHtml(slide.title || `第 ${index + 1} 页`)}">
              <img src="${escapeHtml(slide.image_url || '')}" alt="" loading="lazy" width="1920" height="1080">
              <span>${index + 1}</span>
            </button>
          `).join('')}
        </div>
      </div>
    ` : `
      <div class="slide-loading"><span></span><p>${preview.status === 'failed' ? '真实 PPT 页面暂时无法生成' : '正在通过标准模板渲染真实页面'}</p></div>
    `;

    document.querySelectorAll('[data-slide-resource]').forEach((container) => {
      if (container.dataset.slideResource !== key) return;
      container.innerHTML = `
        <div class="resource-heading">
          <div><span class="mode-label">同步演示</span><h3>本节 PPT</h3></div>
          <div class="resource-tools">
            <span class="resource-state">${escapeHtml(stateText)}</span>
            ${readyForDownload ? `<a href="/api/learning/session/${encodeURIComponent(sessionId)}/pptx" title="下载 PPTX"><i data-lucide="download"></i></a>` : ''}
          </div>
        </div>
        ${viewerHtml}
      `;
    });
  }

  function changePptPage(key, nextIndex) {
    const resource = learningResources.get(key);
    const slides = resource?.preview?.result?.slides || [];
    if (!slides.length) return;
    const index = Math.max(0, Math.min(Number(nextIndex), slides.length - 1));
    pptViewerIndices.set(key, index);
    renderSlidePreview(key, resource);
    refreshIcons();
  }

  function audioUrl(sessionId, nodeId, audioFile) {
    const filename = String(audioFile || '').replace(/\\/g, '/').split('/').pop();
    return `/api/learning/session/${encodeURIComponent(sessionId)}/audio/${encodeURIComponent(nodeId)}/${encodeURIComponent(filename)}`;
  }

  function isTutorAudioSlide(slide) {
    return slide?.kind === 'tutor_teaching_text'
      || slide?.source_path === 'tutor_step.teaching_text';
  }

  function setAudioButtonIdle(button) {
    const label = button.dataset.audioIdleLabel || button.dataset.audioReadyLabel || '播放讲稿';
    button.classList.remove('is-playing');
    button.innerHTML = `<i data-lucide="play"></i><span>${escapeHtml(label)}</span>`;
  }

  function renderAudioState(key, resource) {
    const [sessionId, nodeId] = key.split(':');
    const manifest = resource?.audio?.result || {};
    const section = (manifest.sections || []).find((item) => item.outline_node_id === nodeId);
    const tutorSlides = (section?.slides || []).filter(isTutorAudioSlide);
    const readySlides = tutorSlides.filter((slide) => slide.status === 'ready' && slide.audio_file);
    const sectionAudioReady = tutorSlides.length > 0 && readySlides.length === tutorSlides.length;
    Array.from(audioQueues.keys()).forEach((queueKey) => {
      if (queueKey === key || queueKey.startsWith(`${key}:`)) audioQueues.delete(queueKey);
    });
    const readyBySlideId = new Map();
    readySlides.forEach((slide) => {
      if (slide.slide_id) readyBySlideId.set(String(slide.slide_id), slide);
    });
    const sectionQueue = sectionAudioReady
      ? readySlides.map((slide) => audioUrl(sessionId, nodeId, slide.audio_file))
      : [];
    audioQueues.set(key, sectionQueue);

    const queueForButton = (targetKey, button) => {
      if (targetKey === key) return sectionQueue;
      if (!targetKey.startsWith(`${key}:`)) return [];
      const slideId = targetKey.slice(key.length + 1);
      const slide = readyBySlideId.get(slideId);
      if (!slide) return [];
      const expectedHash = String(button.dataset.audioTextHash || '');
      const actualHash = String(slide.text_hash || slide.script_hash || '');
      if (expectedHash && actualHash !== expectedHash) return [];
      return [audioUrl(sessionId, nodeId, slide.audio_file)];
    };

    document.querySelectorAll('[data-audio-key]').forEach((button) => {
      const targetKey = button.dataset.audioKey || '';
      if (targetKey !== key && !targetKey.startsWith(`${key}:`)) return;
      if (activeAudio?.button === button) return;
      delete button.dataset.audioSpeechFallback;
      const queue = queueForButton(targetKey, button);
      audioQueues.set(targetKey, queue);
      const expectedHash = String(button.dataset.audioTextHash || '');
      const targetSlideId = targetKey.startsWith(`${key}:`) ? targetKey.slice(key.length + 1) : '';
      const hasStaleReadySlide = Boolean(expectedHash && targetSlideId && readyBySlideId.has(targetSlideId));
      const hasStaleSectionAudio = targetKey === key
        && tutorSlides.length > 0
        && tutorSlides.every((slide) => slide.status === 'ready')
        && !sectionQueue.length;
      if (queue.length) {
        button.dataset.audioPending = 'false';
        button.disabled = false;
        button.title = targetKey === key ? '播放本节完整讲稿' : '播放本段讲稿语音';
        button.dataset.audioIdleLabel = button.dataset.audioReadyLabel || (targetKey === key ? '播放本节讲稿' : '播放本段');
        setAudioButtonIdle(button);
      } else if (hasStaleReadySlide || hasStaleSectionAudio) {
        button.dataset.audioPending = 'true';
        button.disabled = true;
        button.title = '语音缓存与当前讲稿不一致，正在重新生成';
        button.innerHTML = '<i data-lucide="loader-circle"></i><span>等待语音</span>';
      } else if (['failed', 'skipped'].includes(section?.status)) {
        const canSpeak = targetKey === key && speechTextForKey(key) && 'speechSynthesis' in window;
        button.dataset.audioPending = 'false';
        if (canSpeak) {
          button.disabled = false;
          button.dataset.audioSpeechFallback = 'true';
          button.title = '服务器语音暂不可用，使用浏览器朗读本节讲稿';
          button.dataset.audioIdleLabel = '朗读讲稿';
          setAudioButtonIdle(button);
        } else {
          button.disabled = true;
          button.title = targetKey === key ? '本节语音生成失败，可稍后重试资源生成' : '本段语音暂不可用';
          button.innerHTML = '<i data-lucide="volume-x"></i><span>语音暂不可用</span>';
        }
      } else {
        button.dataset.audioPending = 'true';
        button.disabled = true;
        button.title = targetKey === key ? '本节语音正在准备' : '本段语音正在准备';
        button.innerHTML = '<i data-lucide="loader-circle"></i><span>等待语音</span>';
      }
    });
  }

  function hasPendingAudioButtons(key) {
    return Array.from(document.querySelectorAll('[data-audio-key]')).some((button) => {
      const targetKey = button.dataset.audioKey || '';
      return (targetKey === key || targetKey.startsWith(`${key}:`))
        && button.dataset.audioPending === 'true';
    });
  }

  function renderLearningResource(key, resource) {
    renderSlidePreview(key, resource);
    renderAudioState(key, resource);
    refreshIcons();
  }

  async function refreshLearningResource(key, attempt = 0) {
    const [sessionId] = key.split(':');
    if (!sessionId) return;
    resourceRefreshes.set(key, true);
    const base = `/api/learning/session/${encodeURIComponent(sessionId)}`;
    const [audio, preview, ppt] = await Promise.all([
      fetchJson(`${base}/audio-manifest`),
      fetchJson(`${base}/ppt-preview`),
      fetchJson(`${base}/ppt-status`),
    ]);
    const resource = { audio, preview, ppt, attempt };
    learningResources.set(key, resource);
    renderLearningResource(key, resource);

    const section = (audio?.result?.sections || []).find((item) => key.endsWith(`:${item.outline_node_id}`));
    const waitingAudio = (!section || !['ready', 'failed', 'skipped'].includes(section.status))
      || hasPendingAudioButtons(key);
    const waitingSlides = preview?.result?.status !== 'ready';
    const waitingPpt = !['ready', 'partial', 'failed'].includes(pptStateLabel(ppt));
    if ((waitingAudio || waitingSlides || waitingPpt) && attempt < 45) {
      const delay = attempt < 12 ? 1000 : 2500;
      const timer = setTimeout(() => refreshLearningResource(key, attempt + 1), delay);
      resourceRefreshes.set(key, timer);
    } else {
      resourceRefreshes.delete(key);
    }
  }

  function hydrateLearningResources() {
    const keys = new Set();
    document.querySelectorAll('[data-learning-session]').forEach((node) => {
      const key = `${node.dataset.learningSession || ''}:${node.dataset.outlineNode || ''}`;
      if (key !== ':') keys.add(key);
    });
    keys.forEach((key) => {
      if (learningResources.has(key)) {
        renderLearningResource(key, learningResources.get(key));
      }
      if (!resourceRefreshes.has(key)) {
        refreshLearningResource(key);
      }
    });
  }

  function stopActiveAudio() {
    if (!activeAudio) return;
    if (activeAudio.speech) {
      window.speechSynthesis?.cancel();
    } else if (activeAudio.audio) {
      activeAudio.audio.pause();
      activeAudio.audio.src = '';
    }
    setAudioButtonIdle(activeAudio.button);
    activeAudio = null;
    refreshIcons();
  }

  function speechTextForKey(key) {
    const node = Array.from(document.querySelectorAll('[data-lecture-key]')).find((item) => item.dataset.lectureKey === key);
    return String(node?.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function playSpeechFallback(key, button) {
    const text = speechTextForKey(key);
    if (!text || !('speechSynthesis' in window)) return false;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    utterance.rate = 0.95;
    utterance.onend = () => {
      if (activeAudio?.utterance === utterance) stopActiveAudio();
    };
    utterance.onerror = () => {
      if (activeAudio?.utterance === utterance) stopActiveAudio();
    };
    activeAudio = { key, button, speech: true, utterance };
    button.classList.add('is-playing');
    button.innerHTML = '<i data-lucide="square"></i><span>停止</span>';
    refreshIcons();
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
    return true;
  }

  function playAudioQueue(key, button) {
    if (activeAudio?.key === key) {
      stopActiveAudio();
      return;
    }
    stopActiveAudio();
    const queue = audioQueues.get(key) || [];
    if (!queue.length) {
      playSpeechFallback(key, button);
      return;
    }
    let index = 0;
    const audio = new Audio();
    activeAudio = { key, audio, button };
    button.classList.add('is-playing');
    const playNext = () => {
      if (!activeAudio || activeAudio.audio !== audio) return;
      if (index >= queue.length) {
        stopActiveAudio();
        return;
      }
      button.innerHTML = queue.length > 1
        ? `<i data-lucide="square"></i><span>${index + 1}/${queue.length}</span>`
        : '<i data-lucide="square"></i><span>停止</span>';
      refreshIcons();
      audio.src = queue[index];
      index += 1;
      audio.play().catch(() => stopActiveAudio());
    };
    audio.addEventListener('ended', playNext);
    audio.addEventListener('error', stopActiveAudio, { once: true });
    playNext();
  }

  function resumePendingJobs() {
    const entries = Object.entries(state.pendingJobs);
    if (!entries.length) return;
    const firstSession = state.sessions.find((session) => session.id === entries[0][1].sessionId);
    if (firstSession) restoreSession(firstSession.id);
    entries.forEach(([jobId]) => connectJob(jobId));
    setBusy(true);
    setConnectionStatus('恢复生成任务', true);
  }

  function bindEvents() {
    byId('agent-send')?.addEventListener('click', () => sendMessage());
    byId('agent-clear')?.addEventListener('click', resetSession);
    byId('new-session')?.addEventListener('click', resetSession);
    byId('history-list')?.addEventListener('click', (event) => {
      const item = event.target.closest('[data-session-id]');
      if (!item) return;
      restoreSession(item.dataset.sessionId || '');
    });
    byId('agent-message')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    document.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', () => sendMessage(chip.dataset.prompt || chip.textContent || ''));
    });
    byId('agent-chat-log')?.addEventListener('click', (event) => {
      const candidate = event.target.closest('.plan-candidate-btn');
      if (candidate?.dataset.planItemId) {
        autoSubmitPlanItem(candidate.dataset.planItemId, state.userId);
        return;
      }
      const recommendation = event.target.closest('[data-start-recommendation]');
      if (recommendation?.dataset.startRecommendation) {
        autoSubmitRecommendation(
          recommendation.dataset.startRecommendation,
          recommendation.dataset.planDate,
          recommendation.dataset.recommendationTitle,
        );
        return;
      }
      const quizStart = event.target.closest('[data-quiz-start]');
      if (quizStart) {
        const item = quizStart.closest('.section-quiz-item');
        const options = item?.querySelector('.section-quiz-options');
        if (options) options.hidden = false;
        item?.querySelector('.quiz-feedback')?.replaceChildren();
        return;
      }
      const quizSkip = event.target.closest('[data-quiz-skip]');
      if (quizSkip) {
        const item = quizSkip.closest('.section-quiz-item');
        const options = item?.querySelector('.section-quiz-options');
        if (options) options.hidden = true;
        const feedback = item?.querySelector('.quiz-feedback');
        if (feedback) {
          feedback.className = 'quiz-feedback muted';
          feedback.textContent = '已跳过本题。';
        }
        return;
      }
      const quizOption = event.target.closest('[data-quiz-option]');
      if (quizOption) {
        const item = quizOption.closest('.section-quiz-item');
        if (!item) return;
        item.dataset.selectedOption = quizOption.dataset.quizOption || '';
        item.querySelectorAll('.quiz-option').forEach((node) => node.classList.toggle('selected', node === quizOption));
        const submit = item.querySelector('[data-quiz-submit]');
        if (submit) submit.disabled = !item.dataset.selectedOption;
        return;
      }
      const quizSubmit = event.target.closest('[data-quiz-submit]');
      if (quizSubmit) {
        submitSectionQuizAnswer(quizSubmit.closest('.section-quiz-item'), quizSubmit);
        return;
      }
      const prompt = event.target.closest('[data-chat-prompt]');
      if (prompt?.dataset.chatPrompt) {
        sendMessage(prompt.dataset.chatPrompt);
        return;
      }
      const audioButton = event.target.closest('[data-audio-key]');
      if (audioButton && !audioButton.disabled) {
        playAudioQueue(audioButton.dataset.audioKey, audioButton);
        return;
      }
      const viewer = event.target.closest('[data-ppt-viewer]');
      const pageButton = event.target.closest('[data-ppt-page]');
      if (viewer && pageButton) {
        changePptPage(viewer.dataset.pptViewer, Number(pageButton.dataset.pptPage));
        return;
      }
      const navButton = event.target.closest('[data-ppt-nav]');
      if (viewer && navButton && !navButton.disabled) {
        const key = viewer.dataset.pptViewer;
        const delta = navButton.dataset.pptNav === 'prev' ? -1 : 1;
        changePptPage(key, Number(pptViewerIndices.get(key) || 0) + delta);
        return;
      }
      if (event.target.closest('[data-focus-composer]')) {
        byId('agent-message')?.focus();
      }
    });
    byId('agent-chat-log')?.addEventListener('keydown', (event) => {
      const viewer = event.target.closest('[data-ppt-viewer]');
      if (!viewer || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const key = viewer.dataset.pptViewer;
      const delta = event.key === 'ArrowLeft' ? -1 : 1;
      changePptPage(key, Number(pptViewerIndices.get(key) || 0) + delta);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      bindEvents();
      setEmptyState();
      renderSessions();
      refreshIcons();
      const { planItemId, userId } = parseUrlParams();
      state.userId = userId;
      if (planItemId) {
        autoSubmitPlanItem(planItemId, userId);
      } else {
        resumePendingJobs();
      }
    });
  } else {
    bindEvents();
    setEmptyState();
    renderSessions();
    refreshIcons();
    const { planItemId, userId } = parseUrlParams();
    state.userId = userId;
    if (planItemId) {
      autoSubmitPlanItem(planItemId, userId);
    } else {
      resumePendingJobs();
    }
  }
})();
