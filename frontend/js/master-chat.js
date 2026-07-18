(() => {
  const { byId, escapeHtml, apiPost } = window.oralUi;
  const sanitizeDisplayText = window.oralUi.sanitizeDisplayText || ((value) => String(value || '')
    .replace(/\b(?:chunk(?:_id)?|core|chapter|node|scope(?:_ref)?|document|kg)\s*[.:=]\s*[A-Za-z0-9][A-Za-z0-9._:/-]*/gi, '')
    .replace(/[\(\[（【]\s*[\)\]）】]/g, '')
    .trim());
  const STORAGE_KEY = 'oralsage.history.master-chat';
  const PENDING_KEY = 'oralsage.pending.master-chat';
  const DEFAULT_USER_ID = 'demo';
  const jobStreams = new Map();
  const resourceRefreshes = new Map();
  const learningResources = new Map();
  const pptViewerIndices = new Map();
  const audioQueues = new Map();
  const quizQuestions = new Map();
  const COMPOSER_MIN_HEIGHT = 28;
  const COMPOSER_MAX_HEIGHT = 180;
  let activeAudio = null;
  let activeStorageUserId = DEFAULT_USER_ID;

  const state = {
    messages: [],
    sessions: loadSessions(),
    activeSessionId: createId(),
    backendSessionId: null,
    currentPlanDraftId: null,
    userId: DEFAULT_USER_ID,
    pendingJobs: loadPendingJobs(),
  };

  function normalizeUserId(value) {
    return String(value || DEFAULT_USER_ID).trim() || DEFAULT_USER_ID;
  }

  function accountStorageKey(baseKey, userId = activeStorageUserId) {
    return `${baseKey}.${encodeURIComponent(normalizeUserId(userId))}`;
  }

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
      const userId = normalizeUserId(activeStorageUserId);
      const raw = localStorage.getItem(accountStorageKey(STORAGE_KEY, userId))
        || (userId === DEFAULT_USER_ID ? localStorage.getItem(STORAGE_KEY) : null)
        || '[]';
      const parsed = JSON.parse(raw);
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
    localStorage.setItem(accountStorageKey(STORAGE_KEY), JSON.stringify(state.sessions.slice(0, 30)));
  }

  function loadPendingJobs() {
    try {
      const userId = normalizeUserId(activeStorageUserId);
      const raw = localStorage.getItem(accountStorageKey(PENDING_KEY, userId))
        || (userId === DEFAULT_USER_ID ? localStorage.getItem(PENDING_KEY) : null)
        || '{}';
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function savePendingJobs() {
    localStorage.setItem(accountStorageKey(PENDING_KEY), JSON.stringify(state.pendingJobs));
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
    let value = sanitizeDisplayText(text);
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

    function openList(type, start = 1) {
      if (listType === type) return;
      closeList();
      html.push(type === 'ol' && start > 1 ? `<ol start="${start}">` : `<${type}>`);
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

    function listTypeForLine(line) {
      if (/^\s*[-*+•]\s+/.test(line)) return 'ul';
      if (/^\s*\d+[.)、]\s*/.test(line)) return 'ol';
      return null;
    }

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const line = lines[lineIndex];
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
      const orderedItem = line.match(/^\s*(\d+)[.)、]\s*(.*)$/);
      if (orderedItem) {
        openList('ol', Number(orderedItem[1]) || 1);
        html.push(`<li>${inlineMarkdown(orderedItem[2])}</li>`);
        continue;
      }
      if (!line.trim()) {
        const nextLine = lines.slice(lineIndex + 1).find((candidate) => candidate.trim());
        if (listType && nextLine && listTypeForLine(nextLine) === listType) {
          continue;
        }
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
    if (exam.status === 'submitted' && exam.submission) return renderStageExamSubmission(exam);
    const sessionId = String(exam.session_id || '');
    const summary = exam.summary || {};
    const questionCount = summary.question_count || exam.questions.length;
    const sectionCount = summary.section_count || 0;
    const questions = exam.questions.map((question, index) => {
      const questionId = String(question.id || '');
      const options = Array.isArray(question.options) ? question.options : [];
      const optionHtml = options.map((option, optionIndex) => {
        const letter = String(option || '').trim().charAt(0).toUpperCase() || String.fromCharCode(65 + optionIndex);
        return (
        '<label class="stage-exam-chat-option">' +
          '<input type="radio" name="stage-exam-' + escapeHtml(sessionId) + '-' + escapeHtml(questionId) + '" value="' + escapeHtml(letter) + '">' +
          '<span>' + escapeHtml(option) + '</span>' +
        '</label>'
        );
      }).join('');
      return (
        '<article class="stage-exam-chat-question" data-stage-exam-question-id="' + escapeHtml(questionId) + '">' +
          '<p class="stage-exam-chat-title">' + (index + 1) + '. ' + escapeHtml(question.stem || '阶段卷题目') + '</p>' +
          '<div class="stage-exam-chat-options">' + optionHtml + '</div>' +
        '</article>'
      );
    }).join('');

    return (
      '<section class="stage-exam-chat-result" data-stage-exam-card data-session-id="' + escapeHtml(sessionId) + '">' +
        '<h2>阶段卷</h2>' +
        '<p class="stage-exam-chat-meta">共 ' + escapeHtml(String(questionCount)) +
          ' 题' + (sectionCount ? '，覆盖 ' + escapeHtml(String(sectionCount)) + ' 个小节' : '') + '</p>' +
        '<div class="stage-exam-chat-list">' + questions + '</div>' +
        '<div class="stage-exam-chat-actions"><button type="button" class="learning-action" data-stage-exam-submit>\u63d0\u4ea4\u7b54\u6848</button><p class="stage-exam-chat-feedback" aria-live="polite"></p></div>' +
      '</section>'
    );
  }

  function renderStageExamSubmission(exam) {
    const submission = exam.submission || {};
    const results = Array.isArray(submission.results) ? submission.results : [];
    const score = Math.round(Number(submission.overall_score || 0) * 100);
    const questions = results.map((question, index) => {
      const assessment = question.assessment || {};
      const isCorrect = Boolean(assessment.is_correct);
      const explanation = assessment.correct_answer_explanation || question.explanation || assessment.explanation || '';
      return (
        '<article class="stage-exam-chat-question ' + (isCorrect ? 'is-correct' : 'is-wrong') + '">' +
          '<p class="stage-exam-chat-title">' + (index + 1) + '. ' + escapeHtml(question.stem || '') + '</p>' +
          '<p class="stage-exam-chat-meta">\u4f60\u7684\u7b54\u6848\uff1a' + escapeHtml(question.selected_option || '') + '\uff1b\u6b63\u786e\u7b54\u6848\uff1a' + escapeHtml(question.answer || '') + '</p>' +
          (explanation ? '<p class="stage-exam-chat-explanation">' + escapeHtml(explanation) + '</p>' : '') +
        '</article>'
      );
    }).join('');
    return (
      '<section class="stage-exam-chat-result" data-stage-exam-card>' +
        '<h2>\u7b54\u9898\u7ed3\u679c</h2>' +
        '<p class="stage-exam-chat-meta">\u5f97\u5206 ' + escapeHtml(String(score)) + '%\uff0c\u7b54\u5bf9 ' +
          escapeHtml(String(submission.correct_count || 0)) + '/' + escapeHtml(String(submission.question_count || results.length)) + '</p>' +
        '<div class="stage-exam-chat-list">' + questions + '</div>' +
      '</section>'
    );
  }

  async function submitStageExamFromChat(button) {
    const card = button.closest('[data-stage-exam-card]');
    const sessionId = String(card?.dataset.sessionId || '');
    const questions = Array.from(card?.querySelectorAll('[data-stage-exam-question-id]') || []);
    const feedback = card?.querySelector('.stage-exam-chat-feedback');
    if (!sessionId || !questions.length) return;
    const answers = questions.map((question) => ({
      question_id: question.dataset.stageExamQuestionId || '',
      selected_option: question.querySelector('input[type="radio"]:checked')?.value || '',
    }));
    if (answers.some((answer) => !answer.question_id || !answer.selected_option)) {
      if (feedback) feedback.textContent = '\u8bf7\u5b8c\u6210\u6240\u6709\u9898\u76ee\u540e\u518d\u63d0\u4ea4\u3002';
      return;
    }
    button.disabled = true;
    if (feedback) feedback.textContent = '\u6b63\u5728\u8bc4\u5206...';
    try {
      const response = await fetch(`/api/learning/session/${encodeURIComponent(sessionId)}/stage-exam/submit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ answers }),
      });
      const body = await response.json();
      if (!response.ok || !body?.ok || !body?.result) {
        throw new Error(body?.detail || body?.error || '\u63d0\u4ea4\u5931\u8d25');
      }
      card.outerHTML = renderStageExamHtml(body.result);
      refreshIcons();
    } catch (error) {
      button.disabled = false;
      if (feedback) feedback.textContent = error?.message || '\u63d0\u4ea4\u5931\u8d25';
    }
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

  function renderDirectQaEvidence(finalResult) {
    const evidence = Array.isArray(finalResult?.used_evidence) ? finalResult.used_evidence : [];
    if (!evidence.length) return '';
    return `
      <details class="qa-evidence">
        <summary>相关证据 <span>${evidence.length}</span></summary>
        <div class="qa-evidence-list">
          ${evidence.map((item, index) => `
            <article>
              <strong>${escapeHtml(sanitizeDisplayText(item.title || item.source || `知识库证据 ${index + 1}`))}</strong>
              ${item.text_excerpt ? `<p>${escapeHtml(sanitizeDisplayText(item.text_excerpt))}</p>` : ''}
              ${item.source ? `<small>${escapeHtml(sanitizeDisplayText(item.source))}</small>` : ''}
            </article>
          `).join('')}
        </div>
      </details>
    `;
  }

  function renderHandoutStream(stream) {
    const sections = Array.isArray(stream?.sections) ? stream.sections : [];
    return `
      <section class="handout-stream" aria-live="polite">
        <div class="job-stream-status"><span class="job-spinner" aria-hidden="true"></span><strong>${escapeHtml(stream?.title || '\u8bb2\u4e49\u751f\u6210\u4e2d')}</strong></div>
        ${sections.map((section) => `
          <article class="handout-stream-section ${escapeHtml(section.status || 'pending')}">
            <h3>${escapeHtml(section.title || '\u672a\u547d\u540d\u5c0f\u8282')}</h3>
            <div class="assistant-markdown">${formatMarkdown((section.chunks || []).join('\n\n'))}${section.status === 'streaming' ? '<span class="stream-caret" aria-hidden="true"></span>' : ''}</div>
          </article>
        `).join('')}
      </section>
    `;
  }

  function renderJobProgress(message) {
    const handoutHtml = message.handoutStream ? renderHandoutStream(message.handoutStream) : '';
    const streamText = String(message.streamText || '');
    if (streamText) {
      return `
        ${handoutHtml}
        <section class="job-stream" data-job-id="${escapeHtml(message.jobId || '')}" aria-live="polite">
          <div class="job-stream-status">
            <span class="job-spinner" aria-hidden="true"></span>
            <strong>${escapeHtml(message.statusText || '正在生成本节讲稿')}</strong>
          </div>
          <div class="job-stream-copy assistant-markdown">${formatMarkdown(streamText)}<span class="stream-caret" aria-hidden="true"></span></div>
        </section>
      `;
    }
    if (handoutHtml) return handoutHtml;
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
    if (final.standalone_generation) {
      return renderStandaloneGenerationResult(routerResult);
    }
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

    const resourceKey = `${sessionId}:${nodeId}`;
    return `
      <section class="learning-response" data-learning-session="${escapeHtml(sessionId)}" data-outline-node="${escapeHtml(nodeId)}">
        <header class="learning-section-head">
          <div>
            <span class="mode-label">学习模式</span>
            <h2>${escapeHtml(title)}</h2>
          </div>
          <button type="button" class="audio-command" data-audio-key="${escapeHtml(resourceKey)}" data-audio-ready-label="播放本节讲稿" disabled title="本节语音正在准备">
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
        <section class="slide-resource" data-slide-resource="${escapeHtml(resourceKey)}">
          <div class="resource-heading"><div><span class="mode-label">同步演示</span><h3>本节 PPT</h3></div><span class="resource-state">生成中</span></div>
          <div class="slide-loading"><span></span><p>正在整理本节演示内容</p></div>
        </section>
        ${step?.stage_exam ? renderStageExamHtml(step.stage_exam) : ''}
        ${renderRouteDetails(routerResult)}
      </section>
    `;
  }

  function renderStandaloneHandout(section) {
    if (!section || typeof section !== 'object') return '';
    const content = section.content || {};
    const overview = String(content.overview || '').trim();
    const points = Array.isArray(content.key_points) ? content.key_points : [];
    if (!overview && !points.length) return '';
    const pointHtml = points.map((point) => {
      const heading = String(point?.heading || '').trim();
      const explanation = String(point?.explanation || '').trim();
      return '<li><strong>' + escapeHtml(heading) + '</strong>' +
        (explanation ? '<p>' + escapeHtml(explanation) + '</p>' : '') + '</li>';
    }).join('');
    return '<section class="standalone-handout"><h3>' + escapeHtml(section.title || '\u8bb2\u4e49') + '</h3>' +
      (overview ? '<p>' + escapeHtml(overview) + '</p>' : '') +
      (pointHtml ? '<ul>' + pointHtml + '</ul>' : '') + '</section>';
  }

  function renderStandaloneHandoutSections(sections, fallbackSection) {
    const readySections = Array.isArray(sections) ? sections.filter((section) => section && typeof section === 'object') : [];
    if (readySections.length) {
      return '<div class="standalone-handout-list">' + readySections.map(renderStandaloneHandout).join('') + '</div>';
    }
    return renderStandaloneHandout(fallbackSection);
  }

  function renderStandaloneGenerationResult(routerResult) {
    const final = routerResult?.final_result || {};
    const route = routerResult?.route || {};
    const session = final.session || {};
    const operation = final.operation || {};
    const target = String(route.target || 'resource');
    const targetLabels = {
      handout: '\u8bb2\u4e49',
      slides: '\u5e7b\u706f\u7247',
      ppt: 'PPT',
      lecture_script: '\u8bb2\u7a3f',
      voice: '\u8bed\u97f3',
      quiz: '\u7ec3\u4e60\u9898',
    };
    const label = targetLabels[target] || '\u5b66\u4e60\u8d44\u6e90';
    const sessionId = String(session.id || '');
    const statusLink = target === 'ppt' && sessionId
      ? `<a class="learning-action" href="/api/learning/session/${encodeURIComponent(sessionId)}/ppt-status" target="_blank" rel="noreferrer"><i data-lucide="presentation"></i><span>\u67e5\u770b PPT \u72b6\u6001</span></a>`
      : '';
    const quiz = final.stage_exam || (target === 'quiz' ? operation : null);
    const handout = final.handout_section || operation.handout_section;
    const handoutSections = final.handout_sections || operation.handout_sections;

    return `
      <section class="learning-response standalone-generation-response">
        <div class="learning-section-head">
          <div><span class="mode-label">\u72ec\u7acb\u751f\u6210</span><h2>${escapeHtml(label)}</h2></div>
        </div>
        <div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>
        ${statusLink ? `<div class="learning-actions">${statusLink}</div>` : ''}
        ${target === 'handout' ? renderStandaloneHandoutSections(handoutSections, handout) : ''}
        ${quiz ? renderStageExamHtml(quiz) : ''}
        ${renderRouteDetails(routerResult)}
      </section>
    `;
  }

  function renderWeaknessOverview(overview, routerResult) {
    if (!overview || !overview.has_evidence) {
      var reason = (overview && overview.evidence_unavailable_reason)
        ? overview.evidence_unavailable_reason
        : '系统尚未积累足够的学习数据来形成可靠的薄弱点判断。完成更多学习和练习后，系统将自动识别您的薄弱环节。';
      return (
        '<section class="weakness-overview-response">' +
          '<header class="result-heading">' +
            '<div><span class="mode-label">学习画像</span><h2>薄弱点分析</h2></div>' +
          '</header>' +
          '<div class="assistant-markdown">' +
            '<p>' + escapeHtml(reason) + '</p>' +
          '</div>' +
        '</section>'
      );
    }

    var weaknesses = Array.isArray(overview.weaknesses) ? overview.weaknesses : [];
    var lowMastery = Array.isArray(overview.low_mastery) ? overview.low_mastery : [];

    var weaknessItems = '';
    if (weaknesses.length) {
      weaknessItems += '<h3>已识别的薄弱点</h3><ul class="weakness-list">';
      weaknesses.forEach(function (w) {
        var sourceLabel = w.evidence_source === 'learner_memory_weakness'
          ? '来自学习记录' : '来自学习画像';
        weaknessItems += (
          '<li>' +
            '<strong>' + escapeHtml(w.label || '未命名薄弱点') + '</strong>' +
            '<span class="weakness-source">' + escapeHtml(sourceLabel) + '</span>' +
          '</li>'
        );
      });
      weaknessItems += '</ul>';
    }

    if (lowMastery.length) {
      weaknessItems += '<h3>掌握度较低的知识点</h3><ul class="weakness-list">';
      lowMastery.forEach(function (m) {
        var pct = Math.round((m.mastery_score || 0) * 100);
        weaknessItems += (
          '<li>' +
            '<strong>' + escapeHtml(m.label || '未命名知识点') + '</strong>' +
            '<span class="weakness-source">掌握度约 ' + pct + '%</span>' +
          '</li>'
        );
      });
      weaknessItems += '</ul>';
    }

    var summaryText = '系统根据您的学习记录和测验表现，分析出以下薄弱环节：';
    return (
      '<section class="weakness-overview-response">' +
        '<header class="result-heading">' +
          '<div><span class="mode-label">学习画像</span><h2>薄弱点分析</h2></div>' +
        '</header>' +
        '<div class="assistant-markdown">' +
          '<p>' + escapeHtml(summaryText) + '</p>' +
          weaknessItems +
        '</div>' +
        '<div class="clarification-actions">' +
          '<button type="button" data-chat-prompt="针对我的薄弱点制定复习计划">按薄弱点制定计划</button>' +
          '<button type="button" data-chat-prompt="我想系统学习一个口腔医学主题">学习主题</button>' +
        '</div>' +
      '</section>'
    );
  }

  function renderPlanClarificationActions(options) {
    const configured = Array.isArray(options) ? options.filter((item) => (
      item && typeof item === 'object' && item.label && (item.prompt || item.fill)
    )) : [];
    const actions = configured.length ? configured : [
      { label: '按薄弱点安排', prompt: '请根据我的真实薄弱点制定复习计划' },
      { label: '填写教材章节', fill: '请为《教材名》第__章制定学习计划' },
      { label: '填写整本计划', fill: '请为整本《教材名》制定学习计划' },
    ];
    return actions.map((item) => {
      const attribute = item.prompt
        ? `data-chat-prompt="${escapeHtml(item.prompt)}"`
        : `data-chat-fill="${escapeHtml(item.fill)}"`;
      return `<button type="button" ${attribute}>${escapeHtml(item.label)}</button>`;
    }).join('');
  }

  function planDateAtOffset(baseDate, offset) {
    const match = String(baseDate || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return String(baseDate || '');
    const value = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    value.setDate(value.getDate() + Number(offset || 0));
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, '0');
    const day = String(value.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function renderStudyPlanResult(routerResult) {
    const final = routerResult?.final_result || {};

    // Weakness overview — show structured evidence from real learner memory
    if (final.weakness_overview) {
      return renderWeaknessOverview(final.weakness_overview, routerResult);
    }

    // Explicit clarification from LLM orchestration — show the question,
    // not a false-success plan card or empty route details.
    if (final.clarification_needed === true) {
      const clarificationMsg = String(final.clarification_message || '').trim();
      return `
        <section class="clarification-response">
          <div class="assistant-markdown">${formatMarkdown(clarificationMsg || '请告诉我您想学习的教材章节或主题，也可以直接说"薄弱点"，我会根据您的学习情况安排复习。目前支持《牙周病学》《口腔颌面外科学》等口腔医学教材。')}</div>
          <div class="clarification-actions">
            ${renderPlanClarificationActions(final.clarification_options)}
          </div>
        </section>
      `;
    }

    const items = Array.isArray(final.recommendations) ? final.recommendations : [];
    if (!items.length) return `<div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>${renderRouteDetails(routerResult)}`;
    const totalMinutes = items.reduce((sum, item) => sum + Number(item.estimated_minutes || 0), 0);
    const grouped = new Map();
    items.forEach((item, index) => {
      const rawOffset = Number(item.day_offset);
      const dayOffset = Number.isInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
      if (!grouped.has(dayOffset)) grouped.set(dayOffset, []);
      grouped.get(dayOffset).push({ item, index });
    });
    const dayGroups = [...grouped.entries()].sort((left, right) => left[0] - right[0]);
    const draftId = final.plan_draft_id || '';
    const planPageHref = draftId
      ? `/pages/learning-plan.html?plan_draft_id=${encodeURIComponent(draftId)}&user_id=${encodeURIComponent(state.userId || 'demo')}`
      : '/pages/learning-plan.html';
    return `
      <section class="plan-response">
        <header class="result-heading">
          <div><span class="mode-label">计划模式</span><h2>${escapeHtml(final.plan_date || '学习安排')}</h2></div>
          <strong>${escapeHtml(String(dayGroups.length))} 天 · ${escapeHtml(String(totalMinutes))} 分钟</strong>
        </header>
        <div class="plan-recommendation-list">
          ${dayGroups.map(([dayOffset, entries]) => {
            const groupMinutes = entries.reduce((sum, entry) => (
              sum + Number(entry.item.estimated_minutes || 0)
            ), 0);
            const groupDate = entries[0]?.item?.plan_date || planDateAtOffset(final.plan_date, dayOffset);
            return `
              <section class="plan-day-group">
                <header class="plan-day-heading">
                  <div><span>第 ${dayOffset + 1} 天</span><h3>${escapeHtml(groupDate || '待定日期')}</h3></div>
                  <strong>${escapeHtml(String(groupMinutes))} 分钟</strong>
                </header>
                ${entries.map(({ item, index }) => `
                  <article class="plan-recommendation">
                    <div class="plan-index">${String(index + 1).padStart(2, '0')}</div>
                    <div><h3>${escapeHtml(item.title || '学习任务')}</h3><p>${escapeHtml(item.reason || item.notes || '')}</p></div>
                    <div class="plan-item-actions">
                      <span><i data-lucide="clock-3"></i>${escapeHtml(String(item.estimated_minutes || 0))} 分钟</span>
                      ${draftId ? '' : `<button
                        type="button"
                        data-start-recommendation="${escapeHtml(item.recommendation_id || '')}"
                        data-plan-date="${escapeHtml(groupDate || final.plan_date || '')}"
                        data-recommendation-title="${escapeHtml(item.title || '学习任务')}"
                      >开始学习</button>`}
                    </div>
                  </article>
                `).join('')}
              </section>
            `;
          }).join('')}
        </div>
        <a class="open-plan-link" href="${planPageHref}"><i data-lucide="calendar-days"></i><span>打开学习计划</span></a>
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
    const directQaEvidence = intent === 'direct_qa'
      ? renderDirectQaEvidence(routerResult.final_result || {})
      : '';
    return `<div class="assistant-markdown">${formatMarkdown(resultAnswer(routerResult))}</div>${directQaEvidence}${renderRouteDetails(routerResult)}`;
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

  function resizeComposerInput(input = byId('agent-message')) {
    if (!input) return;
    input.style.height = 'auto';
    const scrollHeight = Math.max(Number(input.scrollHeight) || 0, COMPOSER_MIN_HEIGHT);
    const nextHeight = Math.min(scrollHeight, COMPOSER_MAX_HEIGHT);
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = scrollHeight > COMPOSER_MAX_HEIGHT ? 'auto' : 'hidden';
  }

  function resetComposerInput({ clear = false } = {}) {
    const input = byId('agent-message');
    if (!input) return;
    if (clear) input.value = '';
    input.style.height = `${COMPOSER_MIN_HEIGHT}px`;
    input.style.overflowY = 'hidden';
  }

  function appendMessage(message) {
    state.messages.push(message);
    renderMessage(message);
    setEmptyState();
    updateSessionTitle();
  }

  function appendMessageToSession(sessionId, message) {
    if (sessionId === state.activeSessionId) {
      appendMessage(message);
      return state.sessions.find((item) => item.id === sessionId) || null;
    }

    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return null;
    if (!Array.isArray(session.messages)) session.messages = [];
    session.messages.push(message);
    const firstUser = session.messages.find((item) => item.role === 'user');
    session.title = firstUser?.content?.slice(0, 28) || session.title || '新建对话';
    session.time = sessionTime();
    saveSessions();
    renderSessions();
    return session;
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
      existing.currentPlanDraftId = state.currentPlanDraftId;
    } else {
      state.sessions.unshift({
        id: state.activeSessionId,
        title,
        time: sessionTime(),
        messages: state.messages,
        backendSessionId: state.backendSessionId,
        currentPlanDraftId: state.currentPlanDraftId,
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
    state.currentPlanDraftId = null;
    state.messages = [];
    const log = byId('agent-chat-log');
    if (log) log.innerHTML = '';
    resetComposerInput({ clear: true });
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
    state.currentPlanDraftId = session.currentPlanDraftId || null;
    resetComposerInput({ clear: true });
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
    const userId = normalizeUserId(params.get('user_id'));
    return { planItemId, userId };
  }

  function userIdFromAuthBody(body, fallback = DEFAULT_USER_ID) {
    const user = body?.authenticated ? body.user : null;
    return normalizeUserId(user?.user_id || user?.username || user?.display_name || fallback);
  }

  async function resolveCurrentUserId(fallback = DEFAULT_USER_ID) {
    try {
      const response = await fetch('/api/auth/me', {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return normalizeUserId(fallback);
      return userIdFromAuthBody(await response.json(), fallback);
    } catch (_err) {
      return normalizeUserId(fallback);
    }
  }

  function switchAccountScope(userId, { resetActive = true } = {}) {
    const nextUserId = normalizeUserId(userId);
    const changed = nextUserId !== activeStorageUserId;
    if (changed) {
      jobStreams.forEach((stream) => stream.close());
      jobStreams.clear();
    }
    activeStorageUserId = nextUserId;
    state.userId = nextUserId;
    state.sessions = loadSessions();
    state.pendingJobs = loadPendingJobs();
    if (changed || resetActive) {
      state.activeSessionId = createId();
      state.backendSessionId = null;
      state.messages = [];
      const log = byId('agent-chat-log');
      if (log) log.innerHTML = '';
      setBusy(false);
    }
    setEmptyState();
    renderSessions();
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
      state.currentPlanDraftId = session.currentPlanDraftId || state.currentPlanDraftId;
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
    } else if (event.type === 'handout_outline') {
      found.message.handoutStream = {
        title: event.data?.title || '\u8bb2\u4e49',
        sections: (event.data?.sections || []).map((section) => ({
          id: String(section.id || ''),
          title: String(section.title || ''),
          chunks: [],
          status: 'pending',
        })),
      };
      found.message.statusText = '\u6b63\u5728\u751f\u6210\u8bb2\u4e49';
    } else if (event.type === 'handout_section_started') {
      const section = found.message.handoutStream?.sections?.find((item) => item.id === event.data?.section_id);
      if (section) section.status = 'streaming';
      found.message.statusText = '\u6b63\u5728\u64b0\u5199\u8bb2\u4e49\u5c0f\u8282';
    } else if (event.type === 'handout_chunk') {
      const section = found.message.handoutStream?.sections?.find((item) => item.id === event.data?.section_id);
      if (section && event.data?.text) section.chunks.push(String(event.data.text));
    } else if (event.type === 'handout_section_completed') {
      const section = found.message.handoutStream?.sections?.find((item) => item.id === event.data?.section_id);
      if (section) section.status = 'completed';
    } else if (event.type === 'handout_section_failed') {
      const section = found.message.handoutStream?.sections?.find((item) => item.id === event.data?.section_id);
      if (section) section.status = 'failed';
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
      delete found.message.handoutStream;
      const backendId = event.data?.router_result?.final_result?.session?.id;
      const standaloneGeneration = Boolean(event.data?.router_result?.final_result?.standalone_generation);
      if (backendId && !standaloneGeneration) found.session.backendSessionId = backendId;
      const routerResult = event.data?.router_result;
      if (routerResult?.ok && routerResult?.route?.intent === 'study_plan') {
        const planDraftId = routerResult?.final_result?.plan_draft_id;
        if (planDraftId) {
          found.session.currentPlanDraftId = planDraftId;
          if (found.session.id === state.activeSessionId) {
            state.currentPlanDraftId = planDraftId;
          }
        }
      }
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

  async function startRouterJob(payload, sessionId) {
    const snapshot = await createRouterJob(payload);
    const jobId = snapshot.job_id;
    if (!jobId) throw new Error('后端没有返回任务 ID。');
    const targetSession = appendMessageToSession(sessionId, {
      role: 'assistant',
      kind: 'job',
      jobId,
      content: '',
      statusText: '正在连接总控',
      steps: ['请求已提交'],
      time: nowTime(),
    });
    if (!targetSession) throw new Error('发起请求的对话已不存在。');
    state.pendingJobs[jobId] = {
      sessionId,
      lastEventId: 0,
    };
    savePendingJobs();
    if (sessionId === state.activeSessionId) setConnectionStatus('生成中', true);
    connectJob(jobId);
  }

  async function sendMessage(text = null) {
    const input = byId('agent-message');
    const raw = (text ?? input?.value ?? '').trim();
    if (!raw) return;
    const requestSessionId = state.activeSessionId;
    resetComposerInput({ clear: true });

    const history = historyPayload();
    appendMessage({ role: 'user', content: raw, time: nowTime() });
    setBusy(true);

    try {
      const context = { user_id: state.userId || 'demo' };
      if (state.backendSessionId) {
        context.session_id = state.backendSessionId;
      }
      if (state.currentPlanDraftId) {
        context.parent_plan_draft_id = state.currentPlanDraftId;
      }

      await startRouterJob({
        message: raw,
        history,
        context,
      }, requestSessionId);
    } catch (error) {
      appendMessageToSession(requestSessionId, {
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
    const requestSessionId = state.activeSessionId;
    setBusy(true);
    try {
      appendMessage({ role: 'user', content: '开始选中的学习计划', time: nowTime() });
      await startRouterJob({
        message: '',
        history: [],
        context: { user_id: userId, plan_item_id: planItemId },
      }, requestSessionId);
    } catch (error) {
      appendMessageToSession(requestSessionId, {
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
    const requestSessionId = state.activeSessionId;
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
      }, requestSessionId);
    } catch (error) {
      appendMessageToSession(requestSessionId, {
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

  function currentSectionSlideIndex(resource, nodeId) {
    const preview = resource?.preview?.result || {};
    const slides = Array.isArray(preview.slides) ? preview.slides : [];
    if (preview.status !== 'ready' || !nodeId) return -1;
    return slides.findIndex((slide) => slide.source_outline_node_id === nodeId);
  }

  function currentSectionPreviewReady(key, resource) {
    const [, nodeId] = key.split(':');
    return currentSectionSlideIndex(resource, nodeId) >= 0;
  }

  function renderSlidePreview(key, resource) {
    const [sessionId, nodeId] = key.split(':');
    const preview = resource?.preview?.result || {};
    const slides = Array.isArray(preview.slides) ? preview.slides : [];
    const pptx = resource?.ppt?.result?.pptx || {};
    const readyForDownload = pptx.ok === true && pptx.status === 'ready';
    const sectionIndex = currentSectionSlideIndex(resource, nodeId);
    const previewReady = sectionIndex >= 0;
    const stateText = previewReady
      ? '真实页面'
      : (preview.status === 'failed' ? '预览生成失败' : '同步生成中');

    const hasViewerIndex = pptViewerIndices.has(key);
    if (!hasViewerIndex && sectionIndex >= 0) {
      pptViewerIndices.set(key, sectionIndex);
    }
    const requestedIndex = hasViewerIndex
      ? Number(pptViewerIndices.get(key) || 0)
      : (sectionIndex >= 0 ? sectionIndex : 0);
    const currentIndex = Math.max(0, Math.min(requestedIndex, Math.max(0, slides.length - 1)));
    if (hasViewerIndex || sectionIndex >= 0) {
      pptViewerIndices.set(key, currentIndex);
    }
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
    const manifestUnavailable = !resource?.audio;
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
      if (manifestUnavailable) {
        const canSpeak = targetKey === key && speechTextForKey(key) && 'speechSynthesis' in window;
        button.dataset.audioPending = 'false';
        if (canSpeak) {
          button.disabled = false;
          button.dataset.audioSpeechFallback = 'true';
          button.title = '服务器语音状态暂不可用，使用浏览器朗读本节讲稿';
          button.dataset.audioIdleLabel = '朗读讲稿';
          setAudioButtonIdle(button);
        } else {
          button.disabled = true;
          button.title = '服务器语音状态暂不可用';
          button.innerHTML = '<i data-lucide="volume-x"></i><span>语音暂不可用</span>';
        }
      } else if (queue.length) {
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
    const audioUnavailable = !audio;
    const waitingAudio = !audioUnavailable
      && (
        (!section || !['ready', 'failed', 'skipped'].includes(section.status))
        || hasPendingAudioButtons(key)
      );
    const previewFailed = resource?.preview?.result?.status === 'failed';
    const waitingSlides = !previewFailed && !currentSectionPreviewReady(key, resource);
    if ((waitingAudio || waitingSlides) && attempt < 45) {
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

  function expirePendingJob(jobId) {
    const found = findJobMessage(jobId);
    if (found.message?.kind === 'job') {
      found.message.kind = 'error';
      found.message.content = '此前的生成任务已失效，请重新提交。';
      delete found.message.streamText;
    }
    delete state.pendingJobs[jobId];
    jobStreams.get(jobId)?.close();
    jobStreams.delete(jobId);
    if (found.session) persistJobSession(found.session);
  }

  async function resumePendingJobs() {
    const entries = Object.entries(state.pendingJobs);
    if (!entries.length) return;
    await Promise.all(entries.map(async ([jobId, pending]) => {
      const found = findJobMessage(jobId);
      if (!found.session || !found.message) {
        expirePendingJob(jobId);
        return;
      }
      try {
        let response = await fetch(`/api/router/jobs/${encodeURIComponent(jobId)}?after=${Number(pending.lastEventId || 0)}`, {
          headers: { Accept: 'application/json' },
        });
        if (response.status === 404) {
          expirePendingJob(jobId);
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        let snapshot = await response.json();
        if (snapshot.done && !(snapshot.events || []).length) {
          response = await fetch(`/api/router/jobs/${encodeURIComponent(jobId)}?after=0`, {
            headers: { Accept: 'application/json' },
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          snapshot = await response.json();
          if (state.pendingJobs[jobId] === pending) pending.lastEventId = 0;
        }
        if (state.pendingJobs[jobId] !== pending) return;
        (snapshot.events || []).forEach((event) => handleJobEvent(jobId, event));
        if (state.pendingJobs[jobId]) connectJob(jobId);
      } catch (_error) {
        if (state.pendingJobs[jobId] === pending) connectJob(jobId);
      }
    }));
    savePendingJobs();
    const activePending = Object.values(state.pendingJobs).some((item) => item.sessionId === state.activeSessionId);
    setBusy(activePending);
    setConnectionStatus(activePending ? '恢复生成任务' : '就绪', activePending);
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
    byId('agent-message')?.addEventListener('input', (event) => {
      resizeComposerInput(event.currentTarget);
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
      const stageExamSubmit = event.target.closest('[data-stage-exam-submit]');
      if (stageExamSubmit) {
        void submitStageExamFromChat(stageExamSubmit);
        return;
      }
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
      const fill = event.target.closest('[data-chat-fill]');
      if (fill?.dataset.chatFill) {
        const input = byId('agent-message');
        if (!input) return;
        input.value = fill.dataset.chatFill;
        resizeComposerInput(input);
        input.focus();
        const placeholderIndex = input.value.indexOf('__');
        if (placeholderIndex >= 0 && typeof input.setSelectionRange === 'function') {
          input.setSelectionRange(placeholderIndex, placeholderIndex + 2);
        }
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

  async function bootMasterChat() {
    bindEvents();
    const { planItemId, userId } = parseUrlParams();
    const resolvedUserId = await resolveCurrentUserId(userId);
    switchAccountScope(resolvedUserId, { resetActive: false });
    refreshIcons();
    if (planItemId) {
      autoSubmitPlanItem(planItemId, state.userId);
    } else {
      void resumePendingJobs();
    }
  }

  window.addEventListener('oralsage:auth-changed', async (event) => {
    const detailUserId = event.detail?.authenticated
      ? normalizeUserId(event.detail?.user?.user_id || event.detail?.user?.username || event.detail?.user?.display_name)
      : '';
    const nextUserId = detailUserId || await resolveCurrentUserId(DEFAULT_USER_ID);
    switchAccountScope(nextUserId, { resetActive: true });
    refreshIcons();
    void resumePendingJobs();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      void bootMasterChat();
    });
  } else {
    void bootMasterChat();
  }

  // ---- test API (exposed for Node-side test verification) ----
  if (typeof window !== 'undefined' && window.oralUi) {
    window.oralUi.masterChatInternals = {
      getState: function () { return state; },
      handleJobEvent: handleJobEvent,
      sendMessage: sendMessage,
      resetSession: resetSession,
      restoreSession: restoreSession,
      formatMarkdown: formatMarkdown,
      resumePendingJobs: resumePendingJobs,
      renderStudyPlanResult: renderStudyPlanResult,
      resultAnswer: resultAnswer,
      appendMessageToSession: appendMessageToSession,
      resizeComposerInput: resizeComposerInput,
    };
  }
})();
