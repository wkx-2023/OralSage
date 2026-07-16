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
// Summary
// ============================================================================

console.log('\n' + '='.repeat(60));
console.log('Results: ' + passed + ' passed, ' + failed + ' failed, ' + (passed + failed) + ' total');
console.log('='.repeat(60));

if (failed > 0) {
  process.exit(1);
}
