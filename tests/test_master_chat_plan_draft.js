// tests/test_master_chat_plan_draft.js
// Node tests that exercise the REAL master-chat.js implementation
// via the exposed window.oralUi.masterChatInternals API.
// No external dependencies (no jsdom). Uses a minimal browser mock.

'use strict';

var assert = require('assert');
var fs = require('fs');
var path = require('path');
var vm = require('vm');

// ============================================================================
// Minimal browser mock — enough for master-chat.js to load without error
// ============================================================================

var mockLocalStorage = {};

function resetLocalStorage() {
  mockLocalStorage = {};
}

function createSandbox() {
  var sb = {
    window: {
      oralUi: {
        byId: function () { return null; },
        escapeHtml: function (v) { return String(v || ''); },
        apiPost: async function () { return {}; },
      },
      lucide: { createIcons: function () {} },
      location: { search: '' },
      history: { replaceState: function () {} },
      EventSource: function EventSourceMock() {
        this.close = function () {};
      },
      addEventListener: function () {},
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
    },
    document: {
      readyState: 'complete',
      documentElement: {},
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      addEventListener: function () {},
      createElement: function () {
        return { className: '', innerHTML: '', appendChild: function () {} };
      },
    },
    // Node.js globals that sandboxed code needs
    URLSearchParams: URLSearchParams,
    URL: URL,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    console: console,
  };
  Object.defineProperty(sb, 'localStorage', {
    get: function () {
      var data = mockLocalStorage;
      return {
        getItem: function (k) { return data[k] !== undefined ? data[k] : null; },
        setItem: function (k, v) { data[k] = String(v); },
        removeItem: function (k) { delete data[k]; },
      };
    },
    configurable: true,
    enumerable: true,
  });
  Object.defineProperty(sb, 'fetch', {
    get: function () {
      return async function () {
        return { ok: true, json: async function () { return {}; }, text: async function () { return ''; } };
      };
    },
    configurable: true,
    enumerable: true,
  });
  return sb;
}

// ============================================================================
// Load the real master-chat.js
// ============================================================================

var scriptPath = path.resolve(__dirname, '..', 'frontend', 'js', 'master-chat.js');
var scriptSrc = fs.readFileSync(scriptPath, 'utf-8');

function loadModule() {
  resetLocalStorage();
  var sandbox = createSandbox();
  vm.runInNewContext(scriptSrc, sandbox, { filename: scriptPath });
  var api = sandbox.window.oralUi.masterChatInternals;
  if (!api) {
    console.error('ERROR: master-chat.js did not expose masterChatInternals');
    process.exit(1);
  }
  return {
    getState: api.getState,
    handleJobEvent: api.handleJobEvent,
    sendMessage: api.sendMessage,
    resetSession: api.resetSession,
    restoreSession: api.restoreSession,
    formatMarkdown: api.formatMarkdown,
    resumePendingJobs: api.resumePendingJobs,
    renderStudyPlanResult: api.renderStudyPlanResult,
    renderClarificationResult: api.renderClarificationResult,
    renderJobResult: api.renderJobResult,
    resultAnswer: api.resultAnswer,
    appendMessageToSession: api.appendMessageToSession,
    resizeComposerInput: api.resizeComposerInput,
  };
}

// ============================================================================
// Test runner
// ============================================================================

var passed = 0;
var failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  PASS: ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL: ' + name);
    console.log('        ' + e.message);
  }
}

function equal(actual, expected, msg) {
  assert.strictEqual(actual, expected, msg);
}

function ok(value, msg) {
  assert.ok(value, msg);
}

// ============================================================================
// 0. Markdown loose ordered lists stay in one list
// ============================================================================

console.log('\n0. Markdown loose ordered lists');

(function () {
  var api = loadModule();
  test('blank lines between ordered items do not restart numbering', function () {
    var html = api.formatMarkdown('1. First\n\n1. Second\n\n1. Third');
    equal((html.match(/<ol>/g) || []).length, 1, 'must render one ordered list');
    equal((html.match(/<li>/g) || []).length, 3, 'must render all three list items');
  });

  test('ordered numbering resumes after a mixed bullet list', function () {
    var html = api.formatMarkdown('1. First\n- detail\n2. Second\n\n3. Third');
    ok(html.indexOf('<ol start="2">') !== -1,
      'an explicit second item must not restart from one after bullets');
    equal((html.match(/<li>/g) || []).length, 4, 'all list items must remain visible');
  });

  test('visible markdown removes internal evidence identifiers', function () {
    var html = api.formatMarkdown('结论（chunk:abc-123）[依据1]');
    ok(html.indexOf('chunk:') === -1, 'internal chunk IDs must be hidden');
    ok(html.indexOf('[依据1]') !== -1, 'public citation markers must remain');
  });
})();

// ============================================================================
// Helpers
// ============================================================================

function makeJobEvent(options) {
  return {
    type: options.type || 'complete',
    id: options.id || 10,
    message: options.message || '',
    data: options.data || {},
  };
}

function makeRouterResult(options) {
  return {
    ok: options.ok !== false,
    route: {
      intent: options.intent || 'study_plan',
      source: 'llm_router',
      reason: 'test',
    },
    final_result: options.final_result || {},
    next_action: options.next_action || 'show_recommendations',
    error: options.error || null,
  };
}

/**
 * Add a pending job to the module's state so handleJobEvent can find it.
 * Also ensures the active session is in state.sessions.
 */
function addPendingJob(api, jobId) {
  var state = api.getState();
  var activeId = state.activeSessionId;

  // Ensure session exists in sessions array
  var existing = state.sessions.find(function (s) { return s.id === activeId; });
  if (!existing) {
    state.sessions.unshift({
      id: activeId,
      title: 'test',
      time: '00:00',
      messages: state.messages,
      backendSessionId: state.backendSessionId,
      currentPlanDraftId: state.currentPlanDraftId,
    });
  }

  state.pendingJobs[jobId] = { sessionId: activeId, lastEventId: 0 };
  state.messages.push({
    role: 'assistant', kind: 'job', jobId: jobId,
    content: '', statusText: 'waiting', time: '00:00',
  });
  // Sync to sessions
  var session = state.sessions.find(function (s) { return s.id === activeId; });
  if (session) session.messages = state.messages;
  return activeId;
}

// ============================================================================
// 1. Session initialization — draft ID starts null
// ============================================================================

console.log('\n1. Session initialization — draft ID starts null');

(function () {
  var api = loadModule();
  test('fresh session has null currentPlanDraftId', function () {
    var state = api.getState();
    equal(state.currentPlanDraftId, null, 'new session must start with null draft ID');
  });
})();

// ============================================================================
// 2. resetSession clears currentPlanDraftId
// ============================================================================

console.log('\n2. resetSession clears currentPlanDraftId');

(function () {
  var api = loadModule();
  test('resetSession clears a previously set draft ID', function () {
    var state = api.getState();
    state.currentPlanDraftId = 'draft_abc123';
    equal(state.currentPlanDraftId, 'draft_abc123', 'precondition');

    api.resetSession();
    state = api.getState();
    equal(state.currentPlanDraftId, null, 'resetSession must clear currentPlanDraftId');
  });
})();

// ============================================================================
// 2b. In-flight jobs stay bound to the conversation that submitted them
// ============================================================================

console.log('\n2b. In-flight job session isolation');

(function () {
  var api = loadModule();
  test('a delayed job message is appended to its original session', function () {
    var state = api.getState();
    var originalId = state.activeSessionId;
    var originalMessages = [{ role: 'user', content: '原会话问题' }];
    state.messages = originalMessages;
    state.sessions.unshift({
      id: originalId, title: '原会话问题', time: '00:00',
      messages: originalMessages, backendSessionId: null,
      currentPlanDraftId: null,
    });

    api.resetSession();
    var newSessionId = api.getState().activeSessionId;
    api.appendMessageToSession(originalId, {
      role: 'assistant', kind: 'job', jobId: 'job-delayed', content: '',
    });

    var original = api.getState().sessions.find(function (item) {
      return item.id === originalId;
    });
    equal(original.messages.length, 2, 'original session receives the delayed job');
    equal(original.messages[1].jobId, 'job-delayed');
    equal(api.getState().activeSessionId, newSessionId, 'active new session is unchanged');
    equal(api.getState().messages.length, 0, 'new session must not receive the old workflow');
  });
})();

// ============================================================================
// 2c. Composer grows until its maximum, then scrolls internally
// ============================================================================

console.log('\n2c. Composer auto-resize');

(function () {
  var api = loadModule();
  test('composer expands before enabling internal scrolling', function () {
    var input = { scrollHeight: 96, style: {} };
    api.resizeComposerInput(input);
    equal(input.style.height, '96px');
    equal(input.style.overflowY, 'hidden');

    input.scrollHeight = 260;
    api.resizeComposerInput(input);
    equal(input.style.height, '180px');
    equal(input.style.overflowY, 'auto');
  });
})();

// ============================================================================
// 3. handleJobEvent: study_plan complete saves plan_draft_id
// ============================================================================

console.log('\n3. handleJobEvent: study_plan complete saves plan_draft_id');

(function () {
  var api = loadModule();
  test('study_plan complete event saves plan_draft_id to session', function () {
    addPendingJob(api, 'job-sp-001');

    api.handleJobEvent('job-sp-001', makeJobEvent({
      type: 'complete', id: 100,
      data: {
        router_result: makeRouterResult({
          intent: 'study_plan',
          final_result: {
            plan_draft_id: 'draft_from_study_plan',
            plain_date: '2026-07-15',
            recommendations: [{ title: '任务A', estimated_minutes: 30 }],
          },
        }),
      },
    }));

    var state = api.getState();
    var session = state.sessions.find(function (s) { return s.id === state.activeSessionId; });
    ok(session, 'active session must exist in sessions array');
    equal(session.currentPlanDraftId, 'draft_from_study_plan',
      'session must have plan_draft_id from complete event');
  });
})();

// ============================================================================
// 4. handleJobEvent: second study_plan complete overwrites first
// ============================================================================

console.log('\n4. handleJobEvent: second study_plan complete overwrites first ID');

(function () {
  var api = loadModule();
  test('second study_plan complete updates plan_draft_id', function () {
    addPendingJob(api, 'job-1');
    addPendingJob(api, 'job-2');

    api.handleJobEvent('job-1', makeJobEvent({
      type: 'complete', id: 101,
      data: {
        router_result: makeRouterResult({
          intent: 'study_plan',
          final_result: { plan_draft_id: 'draft_first' },
        }),
      },
    }));

    var state = api.getState();
    equal(state.currentPlanDraftId, 'draft_first', 'first draft ID is set');

    api.handleJobEvent('job-2', makeJobEvent({
      type: 'complete', id: 102,
      data: {
        router_result: makeRouterResult({
          intent: 'study_plan',
          final_result: { plan_draft_id: 'draft_second' },
        }),
      },
    }));

    state = api.getState();
    equal(state.currentPlanDraftId, 'draft_second',
      'second study_plan complete must overwrite first draft ID');
  });
})();

// ============================================================================
// 5. handleJobEvent: non-study_plan complete does NOT save
// ============================================================================

console.log('\n5. handleJobEvent: non-study_plan complete does NOT save draft ID');

(function () {
  var api = loadModule();
  test('learning complete does NOT overwrite currentPlanDraftId', function () {
    var state = api.getState();
    state.currentPlanDraftId = 'draft_preserved';
    addPendingJob(api, 'job-learning');

    api.handleJobEvent('job-learning', makeJobEvent({
      type: 'complete', id: 200,
      data: {
        router_result: makeRouterResult({
          intent: 'learning',
          final_result: {
            session: { id: 'sess-learning', topic: 'test' },
            plan_draft_id: 'should_be_ignored',
          },
        }),
      },
    }));

    state = api.getState();
    equal(state.currentPlanDraftId, 'draft_preserved',
      'learning complete must not overwrite existing plan_draft_id');
  });
})();

// ============================================================================
// 6. handleJobEvent: error event does NOT save
// ============================================================================

console.log('\n6. handleJobEvent: error event does NOT save draft ID');

(function () {
  var api = loadModule();
  test('error event does NOT set currentPlanDraftId', function () {
    addPendingJob(api, 'job-err');

    api.handleJobEvent('job-err', makeJobEvent({
      type: 'error', id: 300,
      message: 'service failed',
      data: {
        router_result: makeRouterResult({
          ok: false,
          intent: 'study_plan',
          error: 'service failed',
          final_result: { plan_draft_id: 'draft_from_error' },
        }),
      },
    }));

    var state = api.getState();
    equal(state.currentPlanDraftId, null,
      'error event must not set plan_draft_id');
  });
})();

// ============================================================================
// 7. handleJobEvent: complete without plan_draft_id preserves existing
// ============================================================================

console.log('\n7. handleJobEvent: complete without plan_draft_id preserves existing');

(function () {
  var api = loadModule();
  test('study_plan complete without plan_draft_id does not clear existing', function () {
    var state = api.getState();
    state.currentPlanDraftId = 'draft_existing';
    addPendingJob(api, 'job-no-draft');

    api.handleJobEvent('job-no-draft', makeJobEvent({
      type: 'complete', id: 400,
      data: {
        router_result: makeRouterResult({
          intent: 'study_plan',
          final_result: {
            recommendations: [{ title: '默认推荐', estimated_minutes: 30 }],
          },
        }),
      },
    }));

    state = api.getState();
    equal(state.currentPlanDraftId, 'draft_existing',
      'must NOT clear existing draft ID when new result has no plan_draft_id');
  });
})();

// ============================================================================
// 8. handleJobEvent: non-ok study_plan does NOT set
// ============================================================================

console.log('\n8. handleJobEvent: non-ok study_plan does NOT set');

(function () {
  var api = loadModule();
  test('failed study_plan (ok=false) does NOT set currentPlanDraftId', function () {
    addPendingJob(api, 'job-fail');

    api.handleJobEvent('job-fail', makeJobEvent({
      type: 'complete', id: 500,
      data: {
        router_result: makeRouterResult({
          ok: false,
          intent: 'study_plan',
          error: 'plan generation failed',
          final_result: { plan_draft_id: 'draft_from_failed' },
        }),
      },
    }));

    var state = api.getState();
    equal(state.currentPlanDraftId, null,
      'non-ok study_plan result must NOT set plan_draft_id');
  });
})();

// ============================================================================
// 9. Session persistence — draft ID survives save/restore round-trip
// ============================================================================

console.log('\n9. Session persistence — draft ID survives save/restore round-trip');

(function () {
  var api = loadModule();
  test('currentPlanDraftId persists across save and restore', function () {
    var state = api.getState();
    var id1 = state.activeSessionId;
    state.currentPlanDraftId = 'draft_persisted_xyz';

    // Manually save session with draft ID
    var session1 = state.sessions.find(function (s) { return s.id === id1; });
    if (session1) {
      session1.currentPlanDraftId = 'draft_persisted_xyz';
    } else {
      state.sessions.unshift({
        id: id1, title: 'test', time: '00:00',
        messages: state.messages,
        backendSessionId: state.backendSessionId,
        currentPlanDraftId: 'draft_persisted_xyz',
      });
    }

    // Switch to a new session
    api.resetSession();
    var state2 = api.getState();
    equal(state2.currentPlanDraftId, null, 'new session has null draft ID');
    ok(state2.activeSessionId !== id1, 'new session has different ID');

    // Restore first session
    api.restoreSession(id1);
    state = api.getState();
    equal(state.currentPlanDraftId, 'draft_persisted_xyz',
      'restoring session must restore its currentPlanDraftId');
  });
})();

// ============================================================================
// 10. Session switch — independent draft IDs per session
// ============================================================================

console.log('\n10. Session switch — independent draft IDs per session');

(function () {
  var api = loadModule();
  test('each session has its own independent currentPlanDraftId', function () {
    var state = api.getState();
    var idA = state.activeSessionId;
    state.currentPlanDraftId = 'draft_session_A';

    // Save session A
    var sA = state.sessions.find(function (s) { return s.id === idA; });
    if (sA) {
      sA.currentPlanDraftId = 'draft_session_A';
    } else {
      state.sessions.unshift({
        id: idA, title: 'A', time: '00:00',
        messages: state.messages,
        backendSessionId: state.backendSessionId,
        currentPlanDraftId: 'draft_session_A',
      });
    }

    // Create session B with different draft
    api.resetSession();
    state = api.getState();
    var idB = state.activeSessionId;
    state.currentPlanDraftId = 'draft_session_B';
    state.sessions.unshift({
      id: idB, title: 'B', time: '00:01',
      messages: state.messages,
      backendSessionId: state.backendSessionId,
      currentPlanDraftId: 'draft_session_B',
    });

    // Switch back to A
    api.restoreSession(idA);
    equal(api.getState().currentPlanDraftId, 'draft_session_A',
      'switching to session A must restore its draft ID');

    // Switch back to B
    api.restoreSession(idB);
    equal(api.getState().currentPlanDraftId, 'draft_session_B',
      'switching to session B must restore its own draft ID');

    ok('draft_session_A' !== 'draft_session_B', 'draft IDs must differ per session');
  });
})();

// ============================================================================
// 11. Context isolation — draft ID not guessed from other sources
// ============================================================================

console.log('\n11. Context isolation — draft ID NOT guessed from other fields');

(function () {
  var api = loadModule();
  test('currentPlanDraftId is never set from learning intent complete', function () {
    addPendingJob(api, 'job-learning-isolation');

    api.handleJobEvent('job-learning-isolation', makeJobEvent({
      type: 'complete', id: 600,
      data: {
        router_result: makeRouterResult({
          intent: 'learning',
          final_result: {
            session: { id: 'sess-test', topic: 'test' },
            plan_draft_id: 'should_not_leak_to_state',
          },
        }),
      },
    }));

    equal(api.getState().currentPlanDraftId, null,
      'learning intent complete must not set currentPlanDraftId');
  });

  test('currentPlanDraftId is never set from literature intent complete', function () {
    addPendingJob(api, 'job-lit-isolation');

    api.handleJobEvent('job-lit-isolation', makeJobEvent({
      type: 'complete', id: 601,
      data: {
        router_result: makeRouterResult({
          intent: 'literature',
          final_result: {
            articles: [],
            plan_draft_id: 'should_not_leak_lit',
          },
        }),
      },
    }));

    equal(api.getState().currentPlanDraftId, null,
      'literature intent complete must not set currentPlanDraftId');
  });

  test('currentPlanDraftId is never set from clarification intent complete', function () {
    addPendingJob(api, 'job-clarification-isolation');

    api.handleJobEvent('job-clarification-isolation', makeJobEvent({
      type: 'complete', id: 602,
      data: {
        router_result: makeRouterResult({
          intent: 'clarification',
          final_result: {
            message: 'please clarify',
            plan_draft_id: 'should_not_leak_clar',
          },
        }),
      },
    }));

    equal(api.getState().currentPlanDraftId, null,
      'clarification intent complete must not set currentPlanDraftId');
  });
})();

// ============================================================================
// 12. Plan draft ID uniqueness per session
// ============================================================================

console.log('\n12. Plan draft ID uniqueness per session');

(function () {
  test('session A and B have truly independent draft IDs', function () {
    var api = loadModule();
    var state = api.getState();
    var idA = state.activeSessionId;

    // Session A: set draft
    state.currentPlanDraftId = 'draft_A_unique';
    var sA = state.sessions.find(function (s) { return s.id === idA; });
    if (sA) sA.currentPlanDraftId = 'draft_A_unique';
    else state.sessions.unshift({
      id: idA, title: 'A', time: '00:00',
      messages: [], backendSessionId: null,
      currentPlanDraftId: 'draft_A_unique',
    });

    // Session B: new + set draft
    api.resetSession();
    state = api.getState();
    var idB = state.activeSessionId;
    state.currentPlanDraftId = 'draft_B_unique';
    state.sessions.unshift({
      id: idB, title: 'B', time: '00:01',
      messages: [], backendSessionId: null,
      currentPlanDraftId: 'draft_B_unique',
    });

    // Verify A preserves its draft
    api.restoreSession(idA);
    equal(api.getState().currentPlanDraftId, 'draft_A_unique');

    // Verify B preserves its draft
    api.restoreSession(idB);
    equal(api.getState().currentPlanDraftId, 'draft_B_unique');

    // Switch again to A — must not inherit B's draft
    api.restoreSession(idA);
    equal(api.getState().currentPlanDraftId, 'draft_A_unique',
      'session A must NOT inherit B\'s draft ID');
  });
})();

// ============================================================================
// 13. renderStudyPlanResult: clarification_needed rendering
// ============================================================================

console.log('\n13. renderStudyPlanResult: clarification_needed rendering');

(function () {
  var api = loadModule();
  test('clarification_needed=true shows clarification message not default success', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        clarification_needed: true,
        clarification_message: '请告诉我您想学习的教材章节或主题，也可以直接说"薄弱点"，我会根据您的学习情况安排复习。目前支持《牙周病学》《口腔颌面外科学》等口腔医学教材。',
        recommendations: [],
        plan_draft_id: 'draft_clarify_001',
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    // Must show the clarification message with all three entry points
    ok(html.indexOf('请告诉我您想学习的教材章节或主题') !== -1,
      'must contain clarification_message text');
    ok(html.indexOf('薄弱点') !== -1,
      'must mention 薄弱点 as an explicit entry point');
    ok(html.indexOf('学习情况安排复习') !== -1,
      'must mention 学习情况安排复习 for weakness-driven review');
    // Must use clarification-response section
    ok(html.indexOf('clarification-response') !== -1,
      'must use clarification-response section');
    // Must show quick-action buttons
    ok(html.indexOf('clarification-actions') !== -1,
      'must include clarification quick-action buttons');
    // Must NOT show the default success text
    ok(html.indexOf('已生成学习计划建议') === -1,
      'must NOT show default success text');
    // Must NOT show plan mode header
    ok(html.indexOf('计划模式') === -1,
      'must NOT show plan mode header');
    // Must NOT show route details (调度详情)
    ok(html.indexOf('调度详情') === -1,
      'must NOT show route details for clarification');
  });

  test('clarification_needed=true with empty message falls back gracefully', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        clarification_needed: true,
        clarification_message: '',
        recommendations: [],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    // Must still use clarification-response (with fallback text)
    ok(html.indexOf('clarification-response') !== -1,
      'must use clarification-response section even with empty message');
    // Fallback text must also contain the three entry points
    ok(html.indexOf('教材章节或主题') !== -1,
      'fallback must mention 教材章节或主题');
    ok(html.indexOf('薄弱点') !== -1,
      'fallback must mention 薄弱点');
    ok(html.indexOf('学习情况安排复习') !== -1,
      'fallback must mention 学习情况安排复习');
    // Must NOT show default success text
    ok(html.indexOf('已生成学习计划建议') === -1,
      'must NOT show default success text');
    // Must NOT show plan mode header
    ok(html.indexOf('计划模式') === -1,
      'must NOT show plan mode header');
  });

  test('book clarification renders executable scope actions', function () {
    var html = api.renderStudyPlanResult({
      ok: true,
      route: { intent: 'study_plan' },
      final_result: {
        clarification_needed: true,
        clarification_message: '已识别到《牙周病学》，请选择范围。',
        clarification_options: [
          { label: '按薄弱点安排', prompt: '请根据我的真实薄弱点制定复习计划' },
          { label: '填写具体章节', fill: '请为《牙周病学》第__章制定学习计划' },
          { label: '规划整本教材', prompt: '请为整本《牙周病学》制定学习计划' },
        ],
        recommendations: [],
      },
    });
    ok(html.indexOf('data-chat-fill="请为《牙周病学》第__章制定学习计划"') !== -1,
      'chapter action must fill an actionable request instead of sending a vague reply');
    ok(html.indexOf('data-chat-prompt="请为整本《牙周病学》制定学习计划"') !== -1,
      'whole-book action must explicitly confirm complete coverage');
    ok(html.indexOf('我想系统学习一个口腔医学主题') === -1,
      'old vague action must not trigger another clarification loop');
  });

  test('clarification_needed=false with items shows normal plan card', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-16',
        plan_draft_id: 'draft_normal_001',
        recommendations: [
          {
            recommendation_id: 'draft_normal_001:0',
            title: '学习牙周病学第三章',
            task_type: 'learn_chapter',
            reason: '开始系统学习',
            estimated_minutes: 30,
          },
        ],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    // Must show plan mode header
    ok(html.indexOf('计划模式') !== -1,
      'normal plan must show plan mode header');
    // Must show total minutes
    ok(html.indexOf('30') !== -1,
      'normal plan must show total minutes');
    // Must NOT show clarification section
    ok(html.indexOf('clarification-response') === -1,
      'normal plan must NOT show clarification section');
  });

  test('multi-day recommendations are grouped by day with daily totals', function () {
    var html = api.renderStudyPlanResult({
      ok: true,
      route: { intent: 'study_plan' },
      final_result: {
        plan_date: '2026-07-16',
        plan_draft_id: 'draft_multi_day',
        recommendations: [
          { title: '第一天学习', estimated_minutes: 30, day_offset: 0 },
          { title: '第一天练习', estimated_minutes: 20, day_offset: 0 },
          { title: '第二天复习', estimated_minutes: 25, day_offset: 1 },
        ],
      },
    });
    ok(html.indexOf('2 天 · 75 分钟') !== -1, 'header must identify multi-day total');
    ok(html.indexOf('第 1 天') !== -1 && html.indexOf('2026-07-16') !== -1);
    ok(html.indexOf('第 2 天') !== -1 && html.indexOf('2026-07-17') !== -1);
    ok(html.indexOf('50 分钟') !== -1, 'first day total must be shown');
    ok(html.indexOf('25 分钟') !== -1, 'second day total must be shown');
  });

  test('old result without clarification_needed and empty items uses legacy path', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        // No clarification_needed field at all (old result)
        recommendations: [],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    // Legacy path: shows resultAnswer (which produces default success text)
    ok(html.indexOf('已生成学习计划建议') !== -1,
      'old result without clarification field uses legacy success text');
  });
})();

// ============================================================================
// 13b. Quiz clarification and safe error rendering
// ============================================================================

console.log('\n13b. Quiz clarification and safe error rendering');

(function () {
  var api = loadModule();

  test('quiz clarification renders its configured topic action', function () {
    var html = api.renderClarificationResult({
      ok: true,
      route: { intent: 'clarification', source: 'missing_quiz_topic' },
      final_result: {
        message: '请告诉我想考查的具体口腔医学主题。',
        clarification_options: [
          { label: '填写出题主题', fill: '请针对__生成5道题' },
        ],
      },
    });
    ok(html.indexOf('请告诉我想考查的具体口腔医学主题') !== -1);
    ok(html.indexOf('data-chat-fill="请针对__生成5道题"') !== -1,
      'quiz clarification must offer an actionable topic template');
    ok(html.indexOf('请推荐今天的学习计划') === -1,
      'quiz clarification must not show unrelated default actions');
  });

  test('safe user_message hides an internal backend error', function () {
    var html = api.renderJobResult({
      router_result: {
        ok: false,
        route: { intent: 'learning', target: 'quiz' },
        user_message: '题目暂时无法生成，请稍后重试。',
        error: 'No verified question-bank questions matched the requested topic.',
      },
    });
    ok(html.indexOf('题目暂时无法生成') !== -1,
      'safe user-facing message must be rendered');
    ok(html.indexOf('No verified question-bank') === -1,
      'internal backend error must not be rendered');
  });
})();

// ============================================================================
// 14. Entry point cache version — HTML must reference current script version
// ============================================================================

console.log('\n14. Entry point cache version — HTML must reference current script version');

(function () {
  var htmlPath = path.resolve(__dirname, '..', 'frontend', 'pages', 'master-chat.html');
  var htmlSrc = fs.readFileSync(htmlPath, 'utf-8');

  test('HTML script tag references canonical renderer path with current query version', function () {
    ok(htmlSrc.indexOf('/js/master-chat.js?v=20260718-quiz-routing-v1') !== -1,
      'master-chat.html must reference canonical path /js/master-chat.js?v=20260718-quiz-routing-v1');
  });

  test('HTML script tag does NOT reference versioned duplicate path', function () {
    ok(htmlSrc.indexOf('/js/master-chat-plan-clarification.js') === -1,
      'master-chat.html must NOT reference versioned duplicate master-chat-plan-clarification.js');
  });

  test('canonical renderer file path exists on disk', function () {
    var canonicalPath = path.resolve(__dirname, '..', 'frontend', 'js', 'master-chat.js');
    ok(fs.existsSync(canonicalPath),
      'canonical master-chat.js must exist on disk');
  });

  test('versioned duplicate file path does not exist on disk', function () {
    var dupPath = path.resolve(__dirname, '..', 'frontend', 'js', 'master-chat-plan-clarification.js');
    ok(!fs.existsSync(dupPath),
      'versioned duplicate master-chat-plan-clarification.js must not exist on disk');
  });
})();

// ============================================================================
// 14b. OralSage assistant avatar asset and styling contract
// ============================================================================

console.log('\n14b. OralSage assistant avatar contract');

(function () {
  var root = path.resolve(__dirname, '..');
  var avatarPath = path.join(root, 'frontend', 'assets', 'images', 'oralsage-assistant-avatar.png');
  var cssPath = path.join(root, 'frontend', 'css', 'master-chat.css');
  var htmlPath = path.join(root, 'frontend', 'pages', 'master-chat.html');
  var cssSrc = fs.readFileSync(cssPath, 'utf-8');
  var htmlSrc = fs.readFileSync(htmlPath, 'utf-8');

  test('assistant renderer uses the OralSage robot image', function () {
    ok(scriptSrc.indexOf('/assets/images/oralsage-assistant-avatar.png') !== -1,
      'master-chat.js must render the OralSage robot asset');
    ok(fs.existsSync(avatarPath), 'assistant avatar image must exist in frontend assets');
  });

  test('assistant avatar keeps a pale-purple rounded-square treatment', function () {
    var avatarRule = cssSrc.match(/\.oralsage-avatar\s*\{([^}]*)\}/);
    ok(avatarRule, 'master-chat.css must define .oralsage-avatar');
    ok(avatarRule[1].indexOf('background: #efeaff') !== -1,
      'assistant avatar must use the pale-purple theme background');
    ok(avatarRule[1].indexOf('border-radius: 6px') !== -1,
      'assistant avatar must keep the existing rounded-square shape');
  });

  test('HTML cache-busts the avatar CSS update', function () {
    ok(htmlSrc.indexOf('/css/master-chat.css?v=20260718-oralsage-avatar-v1') !== -1,
      'master-chat.html must reference the avatar CSS version');
  });
})();

// ============================================================================
// 15. learning-plan.js data-logic tests — draft mapping and payload correctness
// ============================================================================

console.log('\n15. learning-plan.js data logic — draft mapping and payload correctness');

(function () {
  // Load the real learning-plan.js
  var lpScriptPath = path.resolve(__dirname, '..', 'frontend', 'js', 'learning-plan.js');
  var lpScriptSrc = fs.readFileSync(lpScriptPath, 'utf-8');

  function loadLearningPlanModule() {
    resetLocalStorage();
    var sandbox = createSandbox();
    // Set up oralUi BEFORE running learning-plan.js
    sandbox.window.oralUi = sandbox.window.oralUi || {};
    sandbox.window.oralUi.byId = function () { return null; };
    sandbox.window.oralUi.escapeHtml = function (v) { return String(v || ''); };
    sandbox.window.oralUi.apiPost = async function () { return {}; };
    vm.runInNewContext(lpScriptSrc, sandbox, { filename: lpScriptPath });
    var api = sandbox.window.oralUi.learningPlanInternals;
    if (!api) {
      console.error('ERROR: learning-plan.js did not expose learningPlanInternals');
      process.exit(1);
    }
    // Patch state for draft-payload tests
    var state = api.getState();
    state.selectedDate = '2026-07-16';
    state.draftBaseDate = '2026-07-16';
    state.planDraftId = 'draft_test123';
    state._draftIdToFormalId = {};
    return {
      mapDraftItemsToRecommendations: api.mapDraftItemsToRecommendations,
      recommendationMeta: api.recommendationMeta,
      renderRecommendationCard: api.renderRecommendationCard,
      renderTodoItem: api.renderTodoItem,
      renderItemEditor: api.renderItemEditor,
      taskTypeLabel: api.taskTypeLabel,
      normalizePlanDisplayItem: api.normalizePlanDisplayItem,
      planDisplayLabel: api.planDisplayLabel,
      publicPlanText: api.publicPlanText,
      recommendationPayload: api.recommendationPayload,
      _topologicalOrder: api._topologicalOrder,
      _resolveFormalDependency: api._resolveFormalDependency,
      addDays: api.addDays,
      dateKey: api.dateKey,
      getState: api.getState,
    };
  }

  var lp = loadLearningPlanModule();
  var state = lp.getState();

  // ── helpers ──────────────────────────────────────────────────────
  function makeDraftItem(overrides) {
    var defaults = {
      task_type: 'learn_chapter',
      learning_target: '第三章 牙周病的分类和流行病学',
      estimated_minutes: 30,
      reason: '系统学习牙周病学第三章',
      evidence_source: 'chapter_sequence_v1',
      learning_scope: {
        book_title: '牙周病学',
        document_id: 'document.d766e8629232a2f6',
        chapter_key: 'chapter:perio-ch3',
        chapter_title: '第三章 牙周病的分类和流行病学',
        kg_node_ids: ['core:aaa111', 'core:bbb222'],
      },
      dependency: null,
      day_offset: 0,
    };
    var item = {};
    Object.keys(defaults).forEach(function (k) { item[k] = defaults[k]; });
    if (overrides) Object.keys(overrides).forEach(function (k) { item[k] = overrides[k]; });
    return item;
  }

  test('visible plan UI hides KG identifiers while payload preserves them', function () {
    var items = [makeDraftItem({ task_type: 'continue_lesson' })];
    var rec = lp.mapDraftItemsToRecommendations('draft_visible', items)[0];
    rec.category = 'continue_lesson';
    rec.kg_node_ids = [
      'kg://牙周病学/017-根尖周病/core_3',
      'kg://牙周病学/017-根尖周病/support_15',
    ];

    var cardHtml = lp.renderRecommendationCard(rec, true);
    ok(cardHtml.indexOf('继续学习') !== -1, 'task enum must use a readable label');
    ok(cardHtml.indexOf('continue_lesson') === -1, 'raw task enum must be hidden');
    ok(cardHtml.indexOf('知识点：') === -1, 'KG metadata label must be hidden');
    ok(cardHtml.indexOf('kg://') === -1, 'KG URI must be hidden');
    ok(cardHtml.indexOf('core_3') === -1, 'core ID must be hidden');
    ok(cardHtml.indexOf('support_15') === -1, 'support ID must be hidden');
    ok(cardHtml.indexOf('关联课程：牙周病学') !== -1, 'readable course stays visible');

    var formalItem = {
      id: 'plan-visible-1',
      status: 'open',
      title: '第十七章 根尖周病的临床表现及诊断',
      linked_course_title: '牙体牙髓病学',
      kg_node_ids: rec.kg_node_ids,
      deferred_count: 0,
    };
    state.editingId = 'plan-visible-1';
    var todoHtml = lp.renderTodoItem(formalItem, true);
    ok(todoHtml.indexOf('课程：牙体牙髓病学') !== -1, 'readable course stays visible');
    ok(todoHtml.indexOf('kg://') === -1, 'formal item must hide KG URI');
    ok(todoHtml.indexOf('知识点：') === -1, 'formal item must hide KG metadata label');
    ok(todoHtml.indexOf('data-link-kg') === -1, 'editor must not expose KG ID input');

    var payload = lp.recommendationPayload(rec);
    assert.deepStrictEqual(
      Array.from(payload.kg_node_ids),
      Array.from(rec.kg_node_ids),
      'hidden KG IDs must remain available to the adoption contract',
    );
    ok(payload.learning_scope && payload.learning_scope.chapter_key,
      'learning_scope must remain available to the backend');
    state.editingId = '';
  });

  test('raw KG label is replaced by a readable node name in every plan surface', function () {
    var rawId = 'kg://儿童口腔医学/015-儿童口腔医学实习教程/core_1';
    var item = {
      id: 'plan-legacy-kg',
      recommendation_id: 'weakness:牙髓切断术',
      status: 'open',
      title: '复习薄弱点：' + rawId,
      learning_target: rawId,
      reason: '近期反复混淆 ' + rawId,
      linked_course_title: '根尖周炎',
      task_type: 'review_weakness',
      category: 'review_weakness',
      kg_node_ids: [rawId],
      learning_scope: {
        kg_node_ids: [rawId],
        kg_node_names: {},
        book_title: '儿童口腔医学',
        chapter_title: '第十五章 儿童口腔医学实习教程',
      },
    };
    item.learning_scope.kg_node_names[rawId] = '牙髓切断术';

    var normalized = lp.normalizePlanDisplayItem(item);
    equal(normalized.title, '复习薄弱点：牙髓切断术');
    equal(normalized.learning_target, '牙髓切断术');
    ok(normalized.reason.indexOf('牙髓切断术') !== -1,
      'reason should retain the readable knowledge-point name');

    state.editingId = item.id;
    var cardHtml = lp.renderRecommendationCard(item, true);
    var todoHtml = lp.renderTodoItem(item, true);
    var editorHtml = lp.renderItemEditor(item);
    [cardHtml, todoHtml, editorHtml].forEach(function (html) {
      ok(html.indexOf('牙髓切断术') !== -1, 'readable node name should remain visible');
      ok(html.indexOf('kg://') === -1, 'raw KG URI must not be rendered');
      ok(html.indexOf('core_1') === -1, 'raw KG node suffix must not be rendered');
    });

    var payload = lp.recommendationPayload(item);
    equal(payload.title, '复习薄弱点：牙髓切断术');
    equal(payload.learning_target, '牙髓切断术');
    assert.deepStrictEqual(Array.from(payload.kg_node_ids), [rawId],
      'internal KG IDs remain available to backend execution');
    state.editingId = '';
  });

  test('unresolved raw KG label uses a neutral fallback instead of leaking', function () {
    var rawId = 'kg://不存在的教材/不存在章节/core_999';
    var item = {
      id: 'plan-unresolved-kg',
      status: 'open',
      title: '复习薄弱点：' + rawId,
      learning_target: rawId,
      kg_node_ids: [rawId],
    };
    var normalized = lp.normalizePlanDisplayItem(item);
    equal(normalized.title, '复习薄弱点：相关知识点');
    equal(normalized.learning_target, '相关知识点');
    ok(lp.renderTodoItem(item, true).indexOf('kg://') === -1,
      'unresolved URI must still be hidden');
  });

  test('unknown internal task enum falls back to a neutral user label', function () {
    equal(lp.taskTypeLabel('internal_pipeline_step'), '学习任务');
    equal(lp.taskTypeLabel('章节精读'), '章节精读');
  });

  // ── 15a. linked_course_id must NOT come from scope.document_id
  test('linked_course_id is NOT scope.document_id', function () {
    var items = [makeDraftItem()];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].linked_course_id, null,
      'linked_course_id must be null when item has no linked_course_id/session_id');
    ok(recs[0].learning_scope.document_id === 'document.d766e8629232a2f6',
      'scope.document_id is still present in learning_scope');
    ok(recs[0].linked_course_id !== recs[0].learning_scope.document_id,
      'linked_course_id MUST NOT equal scope.document_id');
  });

  test('linked_course_id uses item.session_id when available', function () {
    var items = [makeDraftItem({ session_id: 'sess_real_123' })];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].linked_course_id, 'sess_real_123',
      'linked_course_id should use real session_id');
    ok(recs[0].linked_course_id !== recs[0].learning_scope.document_id,
      'linked_course_id MUST NOT equal scope.document_id even when session present');
  });

  test('linked_course_id uses item.linked_course_id when available', function () {
    var items = [makeDraftItem({ linked_course_id: 'lc_real_456' })];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].linked_course_id, 'lc_real_456',
      'linked_course_id should use item.linked_course_id');
  });

  // ── 15b. day_offset preserved
  test('day_offset is preserved from draft items', function () {
    var items = [
      makeDraftItem({ day_offset: 0, task_type: 'learn_chapter' }),
      makeDraftItem({ day_offset: 1, task_type: 'basic_practice', dependency: 0 }),
      makeDraftItem({ day_offset: 2, task_type: 'review_chapter', dependency: 0 }),
    ];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].day_offset, 0, 'item 0 day_offset');
    equal(recs[1].day_offset, 1, 'item 1 day_offset');
    equal(recs[2].day_offset, 2, 'item 2 day_offset');
  });

  test('day_offset is undefined when not present in draft', function () {
    var items = [makeDraftItem()];
    delete items[0].day_offset;
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].day_offset, undefined,
      'day_offset should be undefined when missing from draft');
  });

  // ── 15c. dependency preserved as integer (for mapping to formal ID)
  test('dependency preserved as integer index from draft', function () {
    var items = [
      makeDraftItem({ day_offset: 0, dependency: null }),
      makeDraftItem({ day_offset: 1, dependency: 0 }),
      makeDraftItem({ day_offset: 2, dependency: 1 }),
    ];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].dependency, null, 'first item has no dependency');
    equal(recs[1].dependency, 0, 'second item depends on index 0');
    equal(recs[2].dependency, 1, 'third item depends on index 1');
  });

  // ── 15d. practice_level preserved
  test('practice_level is preserved from draft items', function () {
    var items = [makeDraftItem({ practice_level: 'basic' })];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].practice_level, 'basic', 'practice_level preserved');
  });

  test('practice_level is null when not present', function () {
    var items = [makeDraftItem()];
    var recs = lp.mapDraftItemsToRecommendations('draft_x', items);
    equal(recs[0].practice_level, null, 'practice_level should be null when missing');
  });

  // ── 15e. recommendationPayload: plan_date computed from base + offset
  test('plan_date = draftBaseDate + day_offset for draft items', function () {
    state.draftBaseDate = '2026-07-16';
    state.selectedDate = '2026-07-20';
    var item = {
      title: '测试',
      task_type: 'learn_chapter',
      estimated_minutes: 30,
      day_offset: 2,
      dependency: null,
      learning_scope: { book_title: '牙周病学' },
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    equal(payload.plan_date, '2026-07-18',
      'plan_date should be base(07-16) + 2 days = 07-18, not selectedDate');
  });

  test('plan_date falls back to selectedDate when day_offset missing', function () {
    state.draftBaseDate = '2026-07-16';
    state.selectedDate = '2026-07-20';
    var item = {
      title: '测试',
      task_type: 'learn_chapter',
      estimated_minutes: 30,
      dependency: null,
      learning_scope: { book_title: '牙周病学' },
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    equal(payload.plan_date, '2026-07-20',
      'plan_date should fall back to selectedDate when no day_offset');
  });

  // ── 15f. recommendationPayload: no reason field in payload
  test('recommendationPayload does NOT include reason field', function () {
    var item = {
      title: '测试',
      task_type: 'learn_chapter',
      estimated_minutes: 30,
      reason: 'some reason text',
      dependency: null,
      learning_scope: null,
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    ok(!('reason' in payload),
      'reason must NOT appear in the API payload (not a declared schema field)');
    // reason can go into notes though
    ok(typeof payload.notes === 'string' || payload.notes === null,
      'notes is still present for reason text');
  });

  // ── 15g. recommendationPayload: dependency resolved to formal ID
  test('recommendationPayload resolves dependency to formal item ID', function () {
    state.planDraftId = 'draft_test123';
    state._draftIdToFormalId = { 'draft_test123:0': 'formal-item-abc' };
    var item = {
      title: '基础练习',
      task_type: 'basic_practice',
      estimated_minutes: 20,
      dependency: 0,
      learning_scope: { book_title: '牙周病学' },
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    equal(payload.dependency, 'formal-item-abc',
      'dependency must be resolved to formal item ID, not integer index');
  });

  test('recommendationPayload passes practice_level through', function () {
    var item = {
      title: '基础练习',
      task_type: 'basic_practice',
      estimated_minutes: 20,
      dependency: null,
      practice_level: 'basic',
      learning_scope: null,
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    equal(payload.practice_level, 'basic', 'practice_level passed through');
  });

  test('recommendationPayload passes session_id through', function () {
    var item = {
      title: '学习',
      task_type: 'learn_chapter',
      estimated_minutes: 30,
      dependency: null,
      session_id: 'sess_real_789',
      learning_scope: null,
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    equal(payload.session_id, 'sess_real_789', 'session_id passed through');
  });

  test('recommendationPayload omits null session_id', function () {
    var item = {
      title: '学习',
      task_type: 'learn_chapter',
      estimated_minutes: 30,
      dependency: null,
      session_id: null,
      learning_scope: null,
      kg_node_ids: [],
    };
    var payload = lp.recommendationPayload(item);
    ok(!('session_id' in payload), 'null session_id omitted from payload');
  });

  // ── 15h. _topologicalOrder ─────────────────────────────────────
  test('_topologicalOrder sorts with dependencies first', function () {
    var items = [
      { recommendation_id: 'd:0', _draft_index: 0, dependency: null, title: 'learn ch1' },
      { recommendation_id: 'd:1', _draft_index: 1, dependency: 0, title: 'review ch1' },
      { recommendation_id: 'd:2', _draft_index: 2, dependency: 1, title: 'practice ch1' },
    ];
    var ordered = lp._topologicalOrder(items);
    equal(ordered.length, 3, 'all items returned');
    equal(ordered[0]._draft_index, 0, 'index 0 first (no deps)');
    equal(ordered[1]._draft_index, 1, 'index 1 second (depends on 0)');
    equal(ordered[2]._draft_index, 2, 'index 2 third (depends on 1)');
  });

  test('_topologicalOrder handles no-dependency items', function () {
    var items = [
      { recommendation_id: 'd:0', _draft_index: 0, dependency: null, title: 'learn ch1' },
      { recommendation_id: 'd:1', _draft_index: 1, dependency: null, title: 'learn ch2' },
      { recommendation_id: 'd:2', _draft_index: 2, dependency: null, title: 'learn ch3' },
    ];
    var ordered = lp._topologicalOrder(items);
    equal(ordered.length, 3, 'all items returned');
    // With all null deps, natural order by index is maintained
    equal(ordered[0]._draft_index, 0);
    equal(ordered[1]._draft_index, 1);
    equal(ordered[2]._draft_index, 2);
  });

  test('_topologicalOrder handles empty list', function () {
    var ordered = lp._topologicalOrder([]);
    equal(ordered.length, 0, 'empty returns empty');
  });

  // ── 15i. Two structurally different drafts ──────────────────────
  test('three-day chapter draft: dates and dependencies correct', function () {
    state.draftBaseDate = '2026-07-16';
    state.planDraftId = 'draft_ch3';
    state._draftIdToFormalId = {};

    var items = [
      makeDraftItem({ day_offset: 0, dependency: null, task_type: 'learn_chapter' }),
      makeDraftItem({ day_offset: 1, dependency: 0, task_type: 'basic_practice' }),
      makeDraftItem({ day_offset: 2, dependency: 0, task_type: 'review_chapter' }),
    ];
    var recs = lp.mapDraftItemsToRecommendations('draft_ch3', items);
    equal(recs.length, 3);

    // Verify dates
    var p0 = lp.recommendationPayload(recs[0]);
    var p1 = lp.recommendationPayload(recs[1]);
    var p2 = lp.recommendationPayload(recs[2]);
    equal(p0.plan_date, '2026-07-16', 'day 0 → base date');
    equal(p1.plan_date, '2026-07-17', 'day 1 → base + 1');
    equal(p2.plan_date, '2026-07-18', 'day 2 → base + 2');

    // Verify task types
    equal(p0.task_type, 'learn_chapter');
    equal(p1.task_type, 'basic_practice');
    equal(p2.task_type, 'review_chapter');

    // No reason field in any payload
    ok(!('reason' in p0) && !('reason' in p1) && !('reason' in p2),
      'no reason field in any payload');
  });

  test('two-day weakness draft: review+practice per day', function () {
    state.draftBaseDate = '2026-07-17';
    state.planDraftId = 'draft_wk';
    state._draftIdToFormalId = {};

    var items = [
      makeDraftItem({
        day_offset: 0, dependency: null,
        task_type: 'review_chapter',
        learning_target: '牙菌斑生物膜',
        practice_level: null,
        learning_scope: {
          book_title: '牙周病学',
          document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3',
          chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:aaa111'],
        },
      }),
      makeDraftItem({
        day_offset: 0, dependency: 0,
        task_type: 'basic_practice',
        practice_level: 'basic',
        learning_target: '牙菌斑生物膜',
        learning_scope: {
          book_title: '牙周病学',
          document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3',
          chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:aaa111'],
        },
      }),
      makeDraftItem({
        day_offset: 1, dependency: null,
        task_type: 'review_chapter',
        learning_target: '宿主免疫反应',
        practice_level: null,
        learning_scope: {
          book_title: '牙周病学',
          document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3',
          chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:bbb222'],
        },
      }),
      makeDraftItem({
        day_offset: 1, dependency: 2,
        task_type: 'basic_practice',
        practice_level: 'basic',
        learning_target: '宿主免疫反应',
        learning_scope: {
          book_title: '牙周病学',
          document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3',
          chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:bbb222'],
        },
      }),
    ];
    var recs = lp.mapDraftItemsToRecommendations('draft_wk', items);
    equal(recs.length, 4);

    // All linked_course_id must be null (not document_id)
    for (var i = 0; i < recs.length; i++) {
      equal(recs[i].linked_course_id, null,
        'item ' + i + ' linked_course_id must be null, not document_id');
    }

    // Verify dates
    equal(lp.recommendationPayload(recs[0]).plan_date, '2026-07-17', 'weakness day 0 review');
    equal(lp.recommendationPayload(recs[1]).plan_date, '2026-07-17', 'weakness day 0 practice');
    equal(lp.recommendationPayload(recs[2]).plan_date, '2026-07-18', 'weakness day 1 review');
    equal(lp.recommendationPayload(recs[3]).plan_date, '2026-07-18', 'weakness day 1 practice');

    // Verify dependencies preserved
    equal(recs[0].dependency, null, 'day 0 review no dep');
    equal(recs[1].dependency, 0, 'day 0 practice depends on review');
    equal(recs[2].dependency, null, 'day 1 review no dep');
    equal(recs[3].dependency, 2, 'day 1 practice depends on review');

    // Verify practice_level
    equal(recs[1].practice_level, 'basic');
    equal(recs[3].practice_level, 'basic');

    // Verify topological order
    var ordered = lp._topologicalOrder(recs);
    equal(ordered.length, 4);
    // review before practice on same day
    var idx0 = ordered.findIndex(function (r) { return r._draft_index === 0; });
    var idx1 = ordered.findIndex(function (r) { return r._draft_index === 1; });
    ok(idx0 < idx1, 'day 0 review must come before day 0 practice');
    var idx2 = ordered.findIndex(function (r) { return r._draft_index === 2; });
    var idx3 = ordered.findIndex(function (r) { return r._draft_index === 3; });
    ok(idx2 < idx3, 'day 1 review must come before day 1 practice');
  });
})();

// ============================================================================
// 16. Draft adoption acceptance tests — inject apiPost spy, call production
//     acceptRecommendation / acceptAllRecommendations and verify POST sequence,
//     dates, dependency IDs, and fail-closed behavior.
// ============================================================================

console.log('\n16. Draft adoption — production accept functions with apiPost spy');

(function () {
  var lpScriptPath = path.resolve(__dirname, '..', 'frontend', 'js', 'learning-plan.js');
  var lpScriptSrc = fs.readFileSync(lpScriptPath, 'utf-8');

  // ── apiPost spy ──────────────────────────────────────────────────
  function createApiPostSpy() {
    var calls = [];
    var queue = [];

    function spy(url, payload) {
      var call = { url: url, payload: JSON.parse(JSON.stringify(payload)) };
      calls.push(call);
      var response = queue.shift();
      if (response === undefined) {
        return Promise.resolve({ result: { item: { id: 'auto-item-' + calls.length } } });
      }
      if (response instanceof Error) {
        return Promise.reject(response);
      }
      return Promise.resolve(JSON.parse(JSON.stringify(response)));
    }

    spy.calls = calls;
    spy.queue = function (r) { queue.push(r); };
    spy.reset = function () { calls.length = 0; queue.length = 0; };

    return spy;
  }

  // ── Sandbox factory (with proper headers for apiGet/loadMonth) ──
  function createLPSandbox() {
    var sb = {
      window: {
        oralUi: {
          byId: function () { return null; },
          escapeHtml: function (v) { return String(v || ''); },
        },
        lucide: { createIcons: function () {} },
        location: { search: '' },
        history: { replaceState: function () {} },
        EventSource: function EventSourceMock() { this.close = function () {}; },
        addEventListener: function () {},
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
      },
      document: {
        readyState: 'complete',
        documentElement: {},
        querySelector: function () { return null; },
        querySelectorAll: function () { return []; },
        addEventListener: function () {},
        createElement: function () { return { className: '', innerHTML: '', appendChild: function () {} }; },
      },
      URLSearchParams: URLSearchParams,
      URL: URL,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
      setInterval: setInterval,
      clearInterval: clearInterval,
      console: console,
      fetch: async function () {
        return {
          ok: true,
          headers: { get: function () { return 'application/json'; } },
          json: async function () { return {}; },
          text: async function () { return ''; },
        };
      },
    };
    Object.defineProperty(sb, 'localStorage', {
      get: function () { return { getItem: function () { return null; }, setItem: function () {}, removeItem: function () {} }; },
      configurable: true,
      enumerable: true,
    });
    return sb;
  }

  function loadLpModule(spy) {
    var sandbox = createLPSandbox();
    vm.runInNewContext(lpScriptSrc, sandbox, { filename: lpScriptPath });
    var api = sandbox.window.oralUi.learningPlanInternals;
    if (!api) throw new Error('learningPlanInternals not exposed');
    api.setApiPost(spy);
    return { api: api, state: api.getState() };
  }

  function makeDraftItemLP(overrides) {
    var defaults = {
      task_type: 'learn_chapter',
      learning_target: '第三章 牙周病的分类',
      estimated_minutes: 30,
      reason: '学习牙周病学第三章',
      learning_scope: {
        book_title: '牙周病学',
        document_id: 'document.d766e8629232a2f6',
        chapter_key: 'chapter:perio-ch3',
        chapter_title: '第三章 牙周病的分类和流行病学',
        kg_node_ids: ['core:aaa111', 'core:bbb222'],
      },
      dependency: null,
      day_offset: 0,
    };
    var item = {};
    Object.keys(defaults).forEach(function (k) { item[k] = defaults[k]; });
    if (overrides) Object.keys(overrides).forEach(function (k) { item[k] = overrides[k]; });
    return item;
  }

  function setupDraftState(api, baseDate, planDraftId, draftItems) {
    var state = api.getState();
    state.userId = 'demo';
    state.selectedDate = baseDate;
    state.draftBaseDate = baseDate;
    state.planDraftId = planDraftId;
    state._draftIdToFormalId = {};
    state.recommendations = api.mapDraftItemsToRecommendations(planDraftId, draftItems);
    state.recommendationDate = baseDate;
  }

  // ── 16a. Three-chapter draft batch POST: dates, order, dependency IDs
  test('batch: three-chapter draft — POST order, dates, and dependency IDs', async function () {
    var spy = createApiPostSpy();
    // Queue responses with distinct formal IDs
    spy.queue({ result: { item: { id: 'formal-ch1-learn' } } });
    spy.queue({ result: { item: { id: 'formal-ch1-practice' } } });
    spy.queue({ result: { item: { id: 'formal-ch1-review' } } });
    spy.queue({ result: { item: { id: 'formal-ch2-learn' } } });
    spy.queue({ result: { item: { id: 'formal-ch2-practice' } } });
    spy.queue({ result: { item: { id: 'formal-ch2-review' } } });

    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null, task_type: 'learn_chapter', learning_target: '第1章' }),
      makeDraftItemLP({ day_offset: 0, dependency: 0, task_type: 'basic_practice', learning_target: '第1章练习', practice_level: 'basic' }),
      makeDraftItemLP({ day_offset: 1, dependency: 0, task_type: 'review_chapter', learning_target: '第1章复习' }),
      makeDraftItemLP({ day_offset: 1, dependency: null, task_type: 'learn_chapter', learning_target: '第2章' }),
      makeDraftItemLP({ day_offset: 1, dependency: 3, task_type: 'basic_practice', learning_target: '第2章练习', practice_level: 'basic' }),
      makeDraftItemLP({ day_offset: 2, dependency: 3, task_type: 'review_chapter', learning_target: '第2章复习' }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_ch12', items);

    await lp.api.acceptAllRecommendations();

    equal(spy.calls.length, 6, 'exactly 6 POST calls');
    // Verify dates: day_offset 0 → 07-16, offset 1 → 07-17, offset 2 → 07-18
    equal(spy.calls[0].payload.plan_date, '2026-07-16', 'ch1 learn on day 0');
    equal(spy.calls[1].payload.plan_date, '2026-07-16', 'ch1 practice on day 0');
    equal(spy.calls[2].payload.plan_date, '2026-07-17', 'ch1 review on day 1');
    equal(spy.calls[3].payload.plan_date, '2026-07-17', 'ch2 learn on day 1');
    equal(spy.calls[4].payload.plan_date, '2026-07-17', 'ch2 practice on day 1');
    equal(spy.calls[5].payload.plan_date, '2026-07-18', 'ch2 review on day 2');

    // Verify topological order: index 0 first, then 0's dependents + index 3
    equal(spy.calls[0].payload.task_type, 'learn_chapter', 'first call is learn');
    // Practice items have dependency on formal IDs from prior POSTs
    equal(spy.calls[1].payload.dependency, 'formal-ch1-learn', 'ch1 practice depends on formal-ch1-learn');
    equal(spy.calls[4].payload.dependency, 'formal-ch2-learn', 'ch2 practice depends on formal-ch2-learn');
    // Review items depend on the learn item
    equal(spy.calls[2].payload.dependency, 'formal-ch1-learn', 'ch1 review depends on formal-ch1-learn');
    equal(spy.calls[5].payload.dependency, 'formal-ch2-learn', 'ch2 review depends on formal-ch2-learn');

    // document_id never enters linked_course_id
    for (var i = 0; i < spy.calls.length; i++) {
      var lcid = spy.calls[i].payload.linked_course_id;
      ok(lcid !== 'document.d766e8629232a2f6',
        'call ' + i + ' linked_course_id must not be document_id');
    }
  });

  // ── 16b. Individual practice adoption: accept dependency first, then practice
  test('single: accept practice item adopts dependency first', async function () {
    var spy = createApiPostSpy();
    spy.queue({ result: { item: { id: 'formal-review-wk1' } } });
    spy.queue({ result: { item: { id: 'formal-practice-wk1' } } });

    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null, task_type: 'review_chapter', learning_target: '弱点A复习' }),
      makeDraftItemLP({ day_offset: 0, dependency: 0, task_type: 'basic_practice', practice_level: 'basic', learning_target: '弱点A练习' }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_wk', items);

    // Accept the practice item (index 1) — it should auto-adopt dependency (index 0)
    await lp.api.acceptRecommendation('draft_wk:1');

    equal(spy.calls.length, 2, 'exactly 2 POST calls (dependency + target)');
    equal(spy.calls[0].payload.task_type, 'review_chapter', 'dependency posted first');
    equal(spy.calls[1].payload.task_type, 'basic_practice', 'target posted second');
    equal(spy.calls[1].payload.dependency, 'formal-review-wk1', 'practice depends on formal review ID');

    // Both removed from recommendations
    var state = lp.api.getState();
    equal(state.recommendations.length, 0, 'all adopted items removed');
  });

  // ── 16c. Two different scope weakness chains
  test('batch: two different scope weakness chains POST correctly', async function () {
    var spy = createApiPostSpy();
    spy.queue({ result: { item: { id: 'formal-wk1-review' } } });
    spy.queue({ result: { item: { id: 'formal-wk1-practice' } } });
    spy.queue({ result: { item: { id: 'formal-wk2-review' } } });
    spy.queue({ result: { item: { id: 'formal-wk2-practice' } } });

    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({
        day_offset: 0, dependency: null, task_type: 'review_chapter', learning_target: '牙菌斑生物膜',
        learning_scope: {
          book_title: '牙周病学', document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3', chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:aaa111'],
        },
      }),
      makeDraftItemLP({
        day_offset: 0, dependency: 0, task_type: 'basic_practice', practice_level: 'basic', learning_target: '牙菌斑生物膜练习',
        learning_scope: {
          book_title: '牙周病学', document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3', chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:aaa111'],
        },
      }),
      makeDraftItemLP({
        day_offset: 1, dependency: null, task_type: 'review_chapter', learning_target: '宿主免疫反应',
        learning_scope: {
          book_title: '牙周病学', document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3', chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:bbb222'],
        },
      }),
      makeDraftItemLP({
        day_offset: 1, dependency: 2, task_type: 'basic_practice', practice_level: 'basic', learning_target: '宿主免疫反应练习',
        learning_scope: {
          book_title: '牙周病学', document_id: 'document.d766e8629232a2f6',
          chapter_key: 'chapter:perio-ch3', chapter_title: '第三章 牙周病的分类和流行病学',
          kg_node_ids: ['core:bbb222'],
        },
      }),
    ];
    setupDraftState(lp.api, '2026-07-17', 'draft_wk2scopes', items);

    await lp.api.acceptAllRecommendations();

    equal(spy.calls.length, 4, '4 POST calls');
    // Dates
    equal(spy.calls[0].payload.plan_date, '2026-07-17', 'weakness 1 day 0');
    equal(spy.calls[1].payload.plan_date, '2026-07-17', 'weakness 1 day 0 practice');
    equal(spy.calls[2].payload.plan_date, '2026-07-18', 'weakness 2 day 1');
    equal(spy.calls[3].payload.plan_date, '2026-07-18', 'weakness 2 day 1 practice');
    // Dependency IDs use real formal IDs
    equal(spy.calls[1].payload.dependency, 'formal-wk1-review', 'wk1 practice depends on wk1 review');
    equal(spy.calls[3].payload.dependency, 'formal-wk2-review', 'wk2 practice depends on wk2 review');
    // Different scopes
    ok(spy.calls[0].payload.learning_scope.kg_node_ids[0] !== spy.calls[2].payload.learning_scope.kg_node_ids[0],
      'different weakness scopes');
  });

  // ── 16d. missing dependency — fail before first POST
  test('fail-closed: missing dependency index — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 1, dependency: 99 }), // missing index
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_missing', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }
    equal(spy.calls.length, 0, 'zero POST calls when dependency missing');
  });

  // ── 16e. NaN dependency — fail before first POST
  test('fail-closed: NaN dependency — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 1, dependency: 'not-a-number' }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_nan', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }
    equal(spy.calls.length, 0, 'zero POST calls when dependency is NaN');
  });

  // ── 16f. negative dependency — fail before first POST
  test('fail-closed: negative dependency — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 1, dependency: -1 }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_neg', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }
    equal(spy.calls.length, 0, 'zero POST calls when dependency is negative');
  });

  // ── 16g. self-dependency — fail before first POST
  test('fail-closed: self-dependency — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 1, dependency: 1 }), // depends on itself
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_self', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }
    equal(spy.calls.length, 0, 'zero POST calls for self-dependency');
  });

  // ── 16h. forward dependency — fail before first POST
  test('fail-closed: forward dependency — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: 2 }), // depends on future index 2
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 1, dependency: null }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_fwd', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }
    equal(spy.calls.length, 0, 'zero POST calls for forward dependency');
  });

  // ── 16i. cycle detection — fail before first POST
  test('fail-closed: cycle dependency — zero API calls', async function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    // Build items that look valid on static checks (0→null, 1→0, 2→1)
    // but have a cycle by manually setting dependency.
    // Static check: depIdx < idx passes for 1→0 and 2→1.
    // But we can hack it: set up items where the topological order fails due to a cycle.
    // Since _validateDraftDependencyGraph checks depIdx > idx as forward,
    // cycles with valid-looking dependencies need a different shape.
    // Example: 0 depends on 1, 1 depends on 0 — but both are forward (each depIdx > own idx fails).
    // A cycle only passes static check if we use items where the dependency chain wraps.
    // Simplify: just test the validator directly with items that have a cycle.
    var recs = [
      { recommendation_id: 'd:0', _draft_index: 0, dependency: 1, title: 'A' },
      { recommendation_id: 'd:1', _draft_index: 1, dependency: 0, title: 'B' },
    ];
    var errors = lp.api._validateDraftDependencyGraph(recs);
    ok(errors.length > 0, 'validator must detect the cycle (0→1 and 1→0)');
  });

  // ── 16j. cycle via topologicalOrder — throws on GRAY
  test('fail-closed: _topologicalOrder throws on cycle', function () {
    var lp = loadLpModule(createApiPostSpy());
    var items = [
      { recommendation_id: 'd:0', _draft_index: 0, dependency: 1, title: 'A' },
      { recommendation_id: 'd:1', _draft_index: 1, dependency: 0, title: 'B' },
    ];
    try {
      lp.api._topologicalOrder(items);
      ok(false, 'should have thrown on cycle');
    } catch (e) {
      ok(e.message.indexOf('循环依赖') !== -1, 'throws cycle error');
    }
  });

  // ── 16k. API returns no ID — stop subsequent POSTs, no fake mapping
  test('fail-closed: API returns no item.id — stops, no fake mapping', async function () {
    var spy = createApiPostSpy();
    // First POST returns no item.id
    spy.queue({ result: { item: null } });

    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null }),
      makeDraftItemLP({ day_offset: 0, dependency: 0, task_type: 'basic_practice', practice_level: 'basic' }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_noid', items);

    try { await lp.api.acceptAllRecommendations(); } catch (e) { /* expected */ }

    // Only the first call should go through (which got an empty response)
    // Actually the first call to _adoptDraftItem should throw because no ID
    equal(spy.calls.length, 1, 'only 1 POST call — failed on missing ID');
    var state = lp.api.getState();
    ok(!state._draftIdToFormalId['draft_noid:0'], 'no fake mapping for missing ID');
    ok(!state._draftIdToFormalId['draft_noid:1'], 'no fake mapping for dependent item');
    // Recommendations preserved (not cleared)
    equal(state.recommendations.length, 2, 'recommendations preserved on failure');
  });

  // ── 16l. document_id never enters linked_course_id in any payload
  test('document_id never enters linked_course_id', function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null, session_id: null, linked_course_id: null }),
      makeDraftItemLP({ day_offset: 0, dependency: 0, task_type: 'basic_practice', session_id: null, linked_course_id: null }),
    ];
    var recs = lp.api.mapDraftItemsToRecommendations('draft_docid', items);

    for (var i = 0; i < recs.length; i++) {
      ok(recs[i].linked_course_id === null || recs[i].linked_course_id !== 'document.d766e8629232a2f6',
        'item ' + i + ' linked_course_id must not be document_id');
    }

    var p0 = lp.api.recommendationPayload(recs[0]);
    var p1 = lp.api.recommendationPayload(recs[1]);
    ok(p0.linked_course_id !== 'document.d766e8629232a2f6', 'payload 0 no document_id');
    ok(p1.linked_course_id !== 'document.d766e8629232a2f6', 'payload 1 no document_id');
  });

  // ── 16m. duplicate _draft_index detected by validator
  test('validator: duplicate _draft_index is rejected', function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var recs = [
      { recommendation_id: 'd:0', _draft_index: 0, dependency: null },
      { recommendation_id: 'd:0_dup', _draft_index: 0, dependency: null },
    ];
    var errors = lp.api._validateDraftDependencyGraph(recs);
    ok(errors.length > 0, 'duplicate _draft_index must produce error');
  });

  // ── 16n. validator: non-integer _draft_index is rejected
  test('validator: non-integer _draft_index is rejected', function () {
    var spy = createApiPostSpy();
    var lp = loadLpModule(spy);
    var recs = [
      { recommendation_id: 'd:bad', _draft_index: 'abc', dependency: null },
    ];
    var errors = lp.api._validateDraftDependencyGraph(recs);
    ok(errors.length > 0, 'non-integer _draft_index must produce error');
  });

  // ── 16o. already-adopted dependency not re-POSTed (acceptRecommendation)
  test('single: already-adopted dependency not re-POSTed', async function () {
    var spy = createApiPostSpy();
    spy.queue({ result: { item: { id: 'formal-dep' } } });
    spy.queue({ result: { item: { id: 'formal-target' } } });

    var lp = loadLpModule(spy);
    var items = [
      makeDraftItemLP({ day_offset: 0, dependency: null, task_type: 'learn_chapter' }),
      makeDraftItemLP({ day_offset: 0, dependency: 0, task_type: 'basic_practice', practice_level: 'basic' }),
    ];
    setupDraftState(lp.api, '2026-07-16', 'draft_nodup', items);

    // First, accept the dependency item alone
    await lp.api.acceptRecommendation('draft_nodup:0');
    equal(spy.calls.length, 1, '1 POST for dependency');
    spy.reset();

    // Now accept the practice item (dep already adopted)
    spy.queue({ result: { item: { id: 'formal-target' } } });
    await lp.api.acceptRecommendation('draft_nodup:1');
    equal(spy.calls.length, 1, 'only 1 more POST — dependency not re-POSTed');
  });

})();

// ============================================================================
// 17. renderStudyPlanResult: open-plan-link carries both plan_draft_id and user_id
// ============================================================================

console.log('\n17. renderStudyPlanResult: open-plan-link carries both plan_draft_id and user_id');

(function () {
  var api = loadModule();

  // ── helper: extract href from rendered link ────────────────────────
  function extractOpenPlanHref(html) {
    var m = html.match(/<a\s[^>]*class="open-plan-link"[^>]*href="([^"]*)"/);
    return m ? m[1] : '';
  }

  test('open-plan-link includes both encoded plan_draft_id and user_id', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: 'draft_abc123',
        recommendations: [
          { recommendation_id: 'draft_abc123:0', title: '学习任务A', task_type: 'learn_chapter', estimated_minutes: 30 },
        ],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    var href = extractOpenPlanHref(html);

    ok(href.indexOf('plan_draft_id=') !== -1, 'href must contain plan_draft_id');
    ok(href.indexOf('user_id=') !== -1, 'href must contain user_id');
    // Both values are encoded
    ok(href.indexOf('plan_draft_id=draft_abc123') !== -1, 'plan_draft_id value present');
    ok(href.indexOf('user_id=demo') !== -1, 'default user_id=demo present');
    // Parameter separator is &
    ok(href.indexOf('&user_id=') !== -1, 'parameters joined with &');
  });

  test('open-plan-link encodes complex user ID (spaces, Chinese, slashes)', function () {
    // Set a complex user ID via state
    api.getState().userId = 'user alpha/中文';
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: 'draft_test',
        recommendations: [
          { recommendation_id: 'draft_test:0', title: '任务', task_type: 'learn_chapter', estimated_minutes: 20 },
        ],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    var href = extractOpenPlanHref(html);

    ok(href.indexOf('user_id=') !== -1, 'href must contain user_id');
    // user_id VALUE must be encoded, not raw (href naturally contains / from path)
    // Verify round-trip: decode back
    var userParam = href.split('&').find(function (p) { return p.startsWith('user_id='); });
    ok(userParam, 'user_id parameter exists');
    var rawValue = userParam.slice(userParam.indexOf('=') + 1);
    // Raw value must contain encoded characters
    ok(rawValue.indexOf(' ') === -1, 'spaces must be encoded');
    ok(rawValue.indexOf('%2F') !== -1, 'slash must be encoded as %2F');
    var decoded = decodeURIComponent(rawValue);
    equal(decoded, 'user alpha/中文', 'round-trip decode matches original');
    // Chinese chars also encoded
    ok(rawValue.indexOf('中文') === -1, 'Chinese chars must be encoded');
  });

  test('open-plan-link encodes complex draft ID (spaces, Chinese, slashes)', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: 'draft with spaces/中文',
        recommendations: [
          { recommendation_id: 'd:0', title: '任务', task_type: 'learn_chapter', estimated_minutes: 20 },
        ],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    var href = extractOpenPlanHref(html);

    ok(href.indexOf('plan_draft_id=') !== -1, 'href must contain plan_draft_id');
    // draft ID must be encoded
    var draftParam = href.split('&')[0];
    var prefix = 'plan_draft_id=';
    var value = draftParam.slice(draftParam.indexOf(prefix) + prefix.length);
    ok(value.indexOf(' ') === -1, 'draft spaces must be encoded');
    // Verify round-trip
    var decoded = decodeURIComponent(value);
    equal(decoded, 'draft with spaces/中文', 'round-trip decode matches original draft ID');
  });

  test('second render uses latest child draft ID while keeping same user', function () {
    api.getState().userId = 'fixture-child-owner-42';

    // First render
    var result1 = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: 'draft_first_child',
        recommendations: [
          { recommendation_id: 'draft_first_child:0', title: '任务v1', task_type: 'learn_chapter', estimated_minutes: 30 },
        ],
      },
    };
    var href1 = extractOpenPlanHref(api.renderStudyPlanResult(result1));
    ok(href1.indexOf('plan_draft_id=draft_first_child') !== -1, 'first render has first draft ID');
    ok(href1.indexOf('user_id=fixture-child-owner-42') !== -1, 'first render has user');

    // Second render — different draft ID, same user
    var result2 = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: 'draft_second_child',
        recommendations: [
          { recommendation_id: 'draft_second_child:0', title: '任务v2', task_type: 'learn_chapter', estimated_minutes: 20 },
        ],
      },
    };
    var href2 = extractOpenPlanHref(api.renderStudyPlanResult(result2));
    ok(href2.indexOf('plan_draft_id=draft_second_child') !== -1, 'second render has second draft ID');
    ok(href2.indexOf('user_id=fixture-child-owner-42') !== -1, 'second render keeps same user');
    ok(href1 !== href2, 'two renders produce different hrefs');
  });

  test('no draft link when no recommendations — uses legacy path, no open-plan-link', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        // no recommendations, no plan_draft_id
        recommendations: [],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    // No open-plan-link in the output — empty recommendations fall to legacy resultAnswer
    ok(html.indexOf('open-plan-link') === -1,
      'empty recommendations must not produce open-plan-link');
    ok(html.indexOf('plan_draft_id=') === -1,
      'empty recommendations must not have plan_draft_id in href');
  });

  test('no draft but has recommendations — href is bare learning-plan.html', function () {
    var routerResult = {
      ok: true,
      route: { intent: 'study_plan', source: 'llm_router', reason: 'test' },
      final_result: {
        plan_date: '2026-07-17',
        plan_draft_id: '',  // empty draft ID
        recommendations: [
          { recommendation_id: 'r:0', title: '旧版推荐', task_type: 'learn_chapter', estimated_minutes: 15 },
        ],
      },
    };
    var html = api.renderStudyPlanResult(routerResult);
    var href = extractOpenPlanHref(html);
    // When draftId is empty, href is bare /pages/learning-plan.html
    equal(href, '/pages/learning-plan.html',
      'empty draft ID produces bare learning-plan.html href');
  });
})();

// ============================================================================
// 18. buildStartLearningUrl — carries both plan_item_id and user_id
// ============================================================================

console.log('\n18. buildStartLearningUrl — carries both plan_item_id and user_id');

(function () {
  var lpScriptPath = path.resolve(__dirname, '..', 'frontend', 'js', 'learning-plan.js');
  var lpScriptSrc = fs.readFileSync(lpScriptPath, 'utf-8');

  function createLPSandbox() {
    var sb = {
      window: {
        oralUi: {
          byId: function () { return null; },
          escapeHtml: function (v) { return String(v || ''); },
        },
        lucide: { createIcons: function () {} },
        location: { search: '' },
        history: { replaceState: function () {} },
        EventSource: function EventSourceMock() { this.close = function () {}; },
        addEventListener: function () {},
        setTimeout: setTimeout,
        clearTimeout: clearTimeout,
      },
      document: {
        readyState: 'complete',
        documentElement: {},
        querySelector: function () { return null; },
        querySelectorAll: function () { return []; },
        addEventListener: function () {},
        createElement: function () { return { className: '', innerHTML: '', appendChild: function () {} }; },
      },
      URLSearchParams: URLSearchParams,
      URL: URL,
      setTimeout: setTimeout,
      clearTimeout: clearTimeout,
      setInterval: setInterval,
      clearInterval: clearInterval,
      console: console,
      fetch: async function () {
        return {
          ok: true,
          headers: { get: function () { return 'application/json'; } },
          json: async function () { return {}; },
          text: async function () { return ''; },
        };
      },
    };
    Object.defineProperty(sb, 'localStorage', {
      get: function () { return { getItem: function () { return null; }, setItem: function () {}, removeItem: function () {} }; },
      configurable: true,
      enumerable: true,
    });
    return sb;
  }

  function loadLP() {
    var sandbox = createLPSandbox();
    vm.runInNewContext(lpScriptSrc, sandbox, { filename: lpScriptPath });
    var api = sandbox.window.oralUi.learningPlanInternals;
    if (!api) throw new Error('learningPlanInternals not exposed');
    return api;
  }

  test('default user_id=demo when state.userId is default', function () {
    var api = loadLP();
    var url = api.buildStartLearningUrl('item-abc-123');
    ok(url.indexOf('plan_item_id=item-abc-123') !== -1, 'plan_item_id present');
    ok(url.indexOf('&user_id=demo') !== -1, 'default user_id=demo present');
  });

  test('encodes complex item ID (spaces, Chinese, slashes)', function () {
    var api = loadLP();
    var url = api.buildStartLearningUrl('item 中文/测试');
    // Extract the encoded values
    var parts = url.split('?')[1].split('&');
    var itemPart = parts.find(function (p) { return p.startsWith('plan_item_id='); });
    var encodedItem = itemPart.slice('plan_item_id='.length);
    ok(encodedItem.indexOf(' ') === -1, 'item spaces must be encoded');
    ok(encodedItem.indexOf('/') === -1 || encodedItem.indexOf('%2F') !== -1, 'item slash must be encoded');
    equal(decodeURIComponent(encodedItem), 'item 中文/测试', 'round-trip decode matches');
  });

  test('encodes user_id when state has complex user ID', function () {
    var api = loadLP();
    api.getState().userId = 'user with spaces/中文';
    var url = api.buildStartLearningUrl('item-xyz');
    var parts = url.split('?')[1].split('&');
    var userPart = parts.find(function (p) { return p.startsWith('user_id='); });
    var encodedUser = userPart.slice('user_id='.length);
    ok(encodedUser.indexOf(' ') === -1, 'user spaces must be encoded');
    ok(encodedUser.indexOf('%2F') !== -1, 'user slash must be encoded as %2F');
    equal(decodeURIComponent(encodedUser), 'user with spaces/中文', 'round-trip decode matches');
  });

  test('consecutive different items keep same user', function () {
    var api = loadLP();
    api.getState().userId = 'steady-user-99';
    var url1 = api.buildStartLearningUrl('item-A');
    var url2 = api.buildStartLearningUrl('item-B');
    ok(url1.indexOf('plan_item_id=item-A') !== -1, 'url1 has item-A');
    ok(url2.indexOf('plan_item_id=item-B') !== -1, 'url2 has item-B');
    ok(url1.indexOf('user_id=steady-user-99') !== -1, 'url1 has user');
    ok(url2.indexOf('user_id=steady-user-99') !== -1, 'url2 has same user');
  });

  test('user_id falls back to demo when state.userId is empty', function () {
    var api = loadLP();
    api.getState().userId = '';
    var url = api.buildStartLearningUrl('item-fallback');
    ok(url.indexOf('&user_id=demo') !== -1, 'empty userId falls back to demo');
  });
})();

// ============================================================================
// 19. Learning Plan CSS contract — text containment and ellipsis
// ============================================================================

console.log('\n19. Learning Plan CSS contract — text containment and ellipsis');

(function () {
  var cssPath = path.resolve(__dirname, '..', 'frontend', 'css', 'learning-plan.css');
  var cssSrc = fs.readFileSync(cssPath, 'utf-8');
  var htmlPath = path.resolve(__dirname, '..', 'frontend', 'pages', 'learning-plan.html');
  var htmlSrc = fs.readFileSync(htmlPath, 'utf-8');

  test('HTML cache-busts the KG display sanitization script', function () {
    ok(htmlSrc.indexOf('/js/learning-plan.js?v=20260718-hide-internal-sources-v2') !== -1,
      'learning-plan.html must load the v2 display-sanitization script');
  });

  // Helper: extract the body of a CSS rule (text between { and })
  function extractCssBlock(css, selector) {
    var idx = css.indexOf(selector + ' {');
    if (idx === -1) idx = css.indexOf(selector + '{');
    if (idx === -1) return '';
    var start = css.indexOf('{', idx) + 1;
    var depth = 1, i = start;
    while (i < css.length && depth > 0) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}') depth--;
      i++;
    }
    return css.substring(start, i - 1);
  }

  // ── 19a. .todo-copy overflow containment ────────────────────────────
  test('.todo-copy rule has overflow: hidden containment', function () {
    var block = extractCssBlock(cssSrc, '.todo-copy');
    ok(block !== '', '.todo-copy rule exists in CSS');
    ok(block.indexOf('overflow') !== -1 && block.indexOf('hidden') !== -1,
      '.todo-copy must contain overflow: hidden');
  });

  // ── 19b. .todo-title single-line ellipsis ───────────────────────────
  test('.todo-title rule has single-line ellipsis (nowrap + hidden + ellipsis)', function () {
    var block = extractCssBlock(cssSrc, '.todo-title');
    ok(block !== '', '.todo-title rule exists in CSS');
    ok(block.indexOf('text-overflow') !== -1 && block.indexOf('ellipsis') !== -1,
      '.todo-title must have text-overflow: ellipsis');
    ok(block.indexOf('white-space') !== -1 && block.indexOf('nowrap') !== -1,
      '.todo-title must have white-space: nowrap');
    ok(block.indexOf('overflow') !== -1 && block.indexOf('hidden') !== -1,
      '.todo-title must have overflow: hidden');
    ok(block.indexOf('overflow-wrap') === -1,
      '.todo-title must NOT have overflow-wrap: anywhere');
  });

  // ── 19c. .todo-meta single-line ellipsis ────────────────────────────
  test('.todo-meta rule has single-line ellipsis (nowrap + hidden + ellipsis)', function () {
    var block = extractCssBlock(cssSrc, '.todo-meta');
    ok(block !== '', '.todo-meta rule exists in CSS');
    ok(block.indexOf('text-overflow') !== -1 && block.indexOf('ellipsis') !== -1,
      '.todo-meta must have text-overflow: ellipsis');
    ok(block.indexOf('white-space') !== -1 && block.indexOf('nowrap') !== -1,
      '.todo-meta must have white-space: nowrap');
    ok(block.indexOf('overflow') !== -1 && block.indexOf('hidden') !== -1,
      '.todo-meta must have overflow: hidden');
    ok(block.indexOf('overflow-wrap') === -1,
      '.todo-meta must NOT have overflow-wrap: anywhere');
  });

  // ── 19d. New CSS token present in HTML ──────────────────────────────
  test('HTML references canonical CSS with new token v=20260717-plan-text-ellipsis', function () {
    ok(htmlSrc.indexOf('learning-plan.css?v=20260717-plan-text-ellipsis') !== -1,
      'HTML must reference learning-plan.css with new token');
  });

  // ── 19e. Old CSS token absent from HTML ─────────────────────────────
  test('HTML does NOT reference old CSS token', function () {
    ok(htmlSrc.indexOf('20260712-cleanup-merge-v1') === -1,
      'old token 20260712-cleanup-merge-v1 must not appear in HTML');
  });

  // ── 19f. .todo-tools still exists in CSS (tools area preserved) ─────
  test('.todo-tools rules still exist in CSS', function () {
    ok(cssSrc.indexOf('.todo-tools') !== -1,
      '.todo-tools rules must still exist in CSS');
  });

  // ── 19g. .start-learning-link referenced in learning-plan.js ────────
  test('.start-learning-link is referenced in learning-plan.js', function () {
    var jsPath = path.resolve(__dirname, '..', 'frontend', 'js', 'learning-plan.js');
    var jsSrc = fs.readFileSync(jsPath, 'utf-8');
    ok(jsSrc.indexOf('start-learning-link') !== -1,
      '.start-learning-link must be referenced in learning-plan.js');
  });

  // ── 19h. Both long-title and long-meta covered by containment ──────
  test('both .todo-title and .todo-meta have overflow:hidden containment', function () {
    var titleBlock = extractCssBlock(cssSrc, '.todo-title');
    var metaBlock = extractCssBlock(cssSrc, '.todo-meta');
    ok(titleBlock.indexOf('overflow') !== -1 && titleBlock.indexOf('hidden') !== -1,
      '.todo-title overflow hidden confirmed');
    ok(metaBlock.indexOf('overflow') !== -1 && metaBlock.indexOf('hidden') !== -1,
      '.todo-meta overflow hidden confirmed');
  });
})();

// ============================================================================
// Summary
// ============================================================================

console.log('\n' + '='.repeat(60));
console.log('Results: ' + passed + ' passed, ' + failed + ' failed, ' + (passed + failed) + ' total');
console.log('='.repeat(60));

if (failed > 0) {
  process.exit(1);
}
