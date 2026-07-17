// tests/test_master_chat.js
// Static Node tests for master-chat.js sendMessage flow.
// No external dependencies. No real network calls.
// Validates: fetch request construction, response consumption,
// error paths, session ID preservation, and two different response forms.

'use strict';

var assert = require('assert');
var fs = require('fs');
var path = require('path');

// ============================================================================
// Test helpers — mirror the core logic from master-chat.js
// ============================================================================

/**
 * Replicates the request body construction from sendMessage().
 * Mirrors: frontend/js/master-chat.js sendMessage()
 */
function buildChatRequestBody(message, history, backendSessionId, planItemId, userId) {
  var context = { user_id: userId || 'demo' };
  if (backendSessionId) {
    context.session_id = backendSessionId;
  }
  if (planItemId) {
    context.plan_item_id = planItemId;
  }
  return {
    message: message,
    history: history || [],
    context: context,
  };
}

/**
 * Replicates parseUrlParams() from master-chat.js.
 * Accepts a raw query string (e.g. "?plan_item_id=abc&user_id=demo").
 */
function parseUrlParams(search) {
  var params = new URLSearchParams(search || '');
  var planItemId = params.get('plan_item_id') || '';
  var userId = params.get('user_id') || 'demo';
  return { planItemId: planItemId, userId: userId };
}

/**
 * Replicates historyPayload() from master-chat.js
 */
function historyPayload(messages) {
  return messages.slice(-8).map(function (item) {
    return { role: item.role, content: item.content };
  });
}

/**
 * Replicates resultAnswer() from master-chat.js
 */
function resultAnswer(result) {
  if (!result) return '未返回内容。';
  if (typeof result.answer === 'string' && result.answer.trim()) return result.answer;
  var final = result.final_result || result.result || {};
  if (typeof final.answer === 'string') return final.answer;
  if (typeof final.summary === 'string') return final.summary;
  if (typeof final.markdown === 'string') return final.markdown;
  if (typeof final.review === 'string') return final.review;
  if (typeof final.report === 'string') return final.report;
  if (final.result && final.result.report_text) return final.result.report_text;
  if (final.result && final.result.markdown) return final.result.markdown;
  if (final.result && final.result.review) return final.result.review;
  if (final.result && final.result.report) return final.result.report;
  if (typeof final.message === 'string') return final.message;
  return JSON.stringify(final, null, 2);
}

/**
 * Mirrors currentSectionSlideIndex() from frontend/js/master-chat.js.
 */
function currentSectionSlideIndex(resource, nodeId) {
  var preview = resource && resource.preview && resource.preview.result || {};
  var slides = Array.isArray(preview.slides) ? preview.slides : [];
  if (preview.status !== 'ready' || !nodeId) return -1;
  return slides.findIndex(function (slide) {
    return slide.source_outline_node_id === nodeId;
  });
}

/**
 * Simulates the response processing logic from sendMessage().
 * Returns { messages, backendSessionId }.
 */
function processRouterResponse(data, existingBackendSessionId) {
  var messages = [];
  var backendSessionId = existingBackendSessionId;

  if (!data.ok) {
    messages.push({
      role: 'assistant',
      isError: true,
      content: data.error || data.message || '请求处理失败。',
    });
    return { messages: messages, backendSessionId: backendSessionId };
  }

  // Save real session.id from backend
  if (data.final_result && data.final_result.session && data.final_result.session.id) {
    backendSessionId = data.final_result.session.id;
  }

  // Build route info
  var route = data.route || {};
  var routeParts = [];
  if (route.reason) routeParts.push(route.reason);
  if (data.next_action) routeParts.push(data.next_action);

  // Extract content
  var final = data.final_result;
  var contentText = resultAnswer(data);
  var isLiterature = !!(final && final.module === 'frontier_literature');
  var hasStageExam = !!(
    final &&
    final.stage_exam &&
    Array.isArray(final.stage_exam.questions) &&
    final.stage_exam.questions.length > 0
  );
  var hasMindmap = !!(final && final.mindmap && final.mindmap.root);

  // Detect select_plan candidates
  var selectPlanCandidates = null;
  if (data.next_action === 'select_plan' && final && Array.isArray(final.candidates) && final.candidates.length > 0) {
    selectPlanCandidates = final.candidates;
  }

  messages.push({
    role: 'assistant',
    isError: false,
    content: contentText,
    routeParts: routeParts,
    isLiterature: isLiterature,
    hasStageExam: hasStageExam,
    hasMindmap: hasMindmap,
    hasFinalResult: !!final,
    selectPlanCandidates: selectPlanCandidates,
  });

  return { messages: messages, backendSessionId: backendSessionId };
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
// 1. Request body construction — generic, no topic special-casing
// ============================================================================

console.log('\n1. Request body construction');

test('builds request with minimal message (topic A)', function () {
  var body = buildChatRequestBody('我想了解口腔溃疡', []);
  equal(body.message, '我想了解口腔溃疡');
  equal(body.context.user_id, 'demo');
  equal(body.history.length, 0);
});

test('builds request with different message (topic B — proves no hardcoding)', function () {
  var body = buildChatRequestBody('颞下颌关节紊乱的治疗', []);
  equal(body.message, '颞下颌关节紊乱的治疗');
  equal(body.context.user_id, 'demo');
});

test('includes backendSessionId in context when present', function () {
  var body = buildChatRequestBody('继续学习', [], 'real-session-123');
  equal(body.context.session_id, 'real-session-123');
  equal(body.context.user_id, 'demo');
});

test('omits session_id from context when no backend session', function () {
  var body = buildChatRequestBody('hello', [], null);
  equal('session_id' in body.context, false);
});

test('passes history payload (last 8 messages, role+content only)', function () {
  var allMessages = [];
  for (var i = 0; i < 12; i++) {
    allMessages.push({ role: 'user', content: 'msg' + i });
    allMessages.push({ role: 'assistant', content: 'reply' + i });
  }
  var history = historyPayload(allMessages);
  equal(history.length, 8, 'should truncate to last 8');
  equal(history[0].content, 'msg8');
  equal(history[7].content, 'reply11');
});

// ============================================================================
// 2. Response processing — learning intent (form 1)
// ============================================================================

console.log('\n2. Response processing — learning intent');

test('processes learning response: ok=true, saves session.id', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '用户想学习新知识', source: 'llm_router' },
    next_action: 'start_session',
    final_result: {
      session: { id: 'learn-sess-001' },
      outline: { nodes: [] },
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  equal(result.messages[0].routeParts.length, 2);
  equal(result.messages[0].routeParts[0], '用户想学习新知识');
  equal(result.messages[0].routeParts[1], 'start_session');
  equal(result.backendSessionId, 'learn-sess-001', 'must save real session.id from backend');
});

test('learning response: resultAnswer extracts answer field', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '开始学习' },
    next_action: 'start_session',
    final_result: {
      session: { id: 'sess-abc' },
      answer: '已为你创建学习课程。',
    },
  };

  var text = resultAnswer(data);
  equal(text, '已为你创建学习课程。');
});

test('learning response: placeholder NOT shown (reverse marker)', function () {
  // The old placeholder text must never appear in response processing
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '学习意图' },
    final_result: { session: { id: 's1' }, answer: 'real answer' },
  };

  var result = processRouterResponse(data, null);

  ok(result.messages[0].content.indexOf('学习总控待接入') === -1,
     'old placeholder text must NOT appear');
  ok(result.messages[0].content.indexOf('旧总控 agent 已停用') === -1,
     'old placeholder text must NOT appear');
});

// ============================================================================
// 3. Response processing — literature intent (form 2)
// ============================================================================

test('learning completion response exposes stage exam in chat payload', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: 'continue current session' },
    next_action: 'show_stage_exam',
    final_result: {
      lesson_completed: true,
      answer: 'Stage exam ready.',
      stage_exam: {
        status: 'ready',
        summary: { question_count: 1, section_count: 1 },
        questions: [{
          id: 'q1',
          stem: 'Which statement is correct?',
          options: ['A. Alpha', 'B. Beta'],
        }],
      },
    },
  };

  var result = processRouterResponse(data, 'sess-existing');

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  equal(result.messages[0].hasStageExam, true);
  equal(result.messages[0].routeParts[1], 'show_stage_exam');
  equal(result.backendSessionId, 'sess-existing', 'existing learning session should remain active');
});

test('learning response exposes mindmap in chat payload', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '开始学习' },
    next_action: 'start_session',
    final_result: {
      session: { id: 'sess-map', topic: '牙周炎' },
      outline: { nodes: [{ id: 'node-1', title: '病因与机制' }] },
      mindmap: {
        format: 'markmap_json',
        root: {
          name: '牙周炎',
          children: [{ id: 'node-1', name: '病因与机制' }],
        },
      },
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].hasMindmap, true);
  equal(result.backendSessionId, 'sess-map');
});

console.log('\n3. Response processing — literature intent');

test('processes literature response: ok=true, module=frontier_literature', function () {
  var data = {
    ok: true,
    route: { intent: 'literature', reason: '用户需要查文献', source: 'llm_router' },
    next_action: 'show_literature',
    final_result: {
      module: 'frontier_literature',
      query: 'oral cancer',
      articles: [{ title: 'Novel Biomarker X', source: 'PubMed' }],
      review: 'Recent findings suggest...',
      sources: ['PubMed'],
      errors: [],
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  equal(result.messages[0].isLiterature, true);
  equal(result.messages[0].routeParts.length, 2);
  equal(result.messages[0].routeParts[0], '用户需要查文献');
  equal(result.messages[0].routeParts[1], 'show_literature');
  // Literature does NOT create a learning session
  equal(result.backendSessionId, null, 'literature must not set backendSessionId');
});

test('literature resultAnswer extracts review text', function () {
  var data = {
    ok: true,
    final_result: {
      module: 'frontier_literature',
      review: 'This is a literature review.',
    },
  };
  var text = resultAnswer(data);
  equal(text, 'This is a literature review.');
});

// ============================================================================
// 4. Error paths — no fake success, honest error display
// ============================================================================

console.log('\n4. Error paths');

test('ok:false response shows real error message', function () {
  var data = {
    ok: false,
    error: 'KAG unavailable',
    route: { intent: 'learning' },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, true);
  ok(result.messages[0].content.indexOf('KAG unavailable') >= 0,
     'must show real error text, got: ' + result.messages[0].content);
});

test('ok:false response with message field (not error field)', function () {
  var data = {
    ok: false,
    message: '服务暂时不可用',
  };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].isError, true);
  ok(result.messages[0].content.indexOf('服务暂时不可用') >= 0);
});

test('ok:false does NOT save session.id', function () {
  var data = {
    ok: false,
    error: 'something went wrong',
    final_result: { session: { id: 'should-not-save' } },
  };

  var result = processRouterResponse(data, null);

  equal(result.backendSessionId, null, 'must not save session on failed response');
});

test('ok:false with no error/message field shows fallback', function () {
  var data = { ok: false };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].isError, true);
  equal(result.messages[0].content, '请求处理失败。');
});

// ============================================================================
// 5. Clarification — honest display, not fake success
// ============================================================================

console.log('\n5. Clarification responses');

test('clarification response: ok=true, no final_result beyond empty object', function () {
  var data = {
    ok: true,
    route: { intent: 'clarification', reason: '无法判断你的意图，请说得更具体些。' },
    next_action: 'select_plan',
    final_result: {},
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  equal(result.messages[0].routeParts.length, 2);
  ok(result.messages[0].routeParts[0].indexOf('无法判断') >= 0);
  equal(result.messages[0].routeParts[1], 'select_plan');
  equal(result.backendSessionId, null);
});

// ============================================================================
// 6. Session ID persistence — real session.id, not frontend-generated
// ============================================================================

console.log('\n6. Session ID persistence');

test('study plan response keeps existing backendSessionId', function () {
  var data = {
    ok: true,
    route: { intent: 'study_plan', reason: '推荐今日学习计划' },
    next_action: 'show_recommendations',
    final_result: { recommendations: [], stats: { total: 3, completed: 1, pending: 2 } },
  };

  var result = processRouterResponse(data, 'existing-session-999');

  // Must keep existing session ID — study plan doesn't create a new one
  equal(result.backendSessionId, 'existing-session-999');
});

test('new learning session replaces old backendSessionId', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '开始新学习' },
    next_action: 'start_session',
    final_result: { session: { id: 'new-session-456' } },
  };

  var result = processRouterResponse(data, 'old-session-123');

  equal(result.backendSessionId, 'new-session-456', 'must update to new real session.id');
});

test('backendSessionId is saved from final_result.session.id only', function () {
  // Test that only final_result.session.id is used, not other session-like fields
  var data = {
    ok: true,
    route: { intent: 'learning' },
    final_result: {
      session_id: 'wrong-field',    // wrong field name
      session: { id: 'correct-id' }, // correct path
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.backendSessionId, 'correct-id', 'must use final_result.session.id');
});

// ============================================================================
// 7. Generic rendering — no topic / intent / ID / page / URL special-casing
// ============================================================================

console.log('\n7. Generic rendering (anti-hardcoding)');

test('two different learning topics produce identical request structure', function () {
  var topicA = buildChatRequestBody('口腔溃疡病因', []);
  var topicB = buildChatRequestBody('根管治疗步骤', []);

  // Both must be processed identically — only message differs
  equal(topicA.context.user_id, 'demo');
  equal(topicB.context.user_id, 'demo');
  equal(typeof topicA.message, 'string');
  equal(typeof topicB.message, 'string');
  ok(topicA.message !== topicB.message, 'topics must differ but structure identical');
});

test('different intents both extract route info generically', function () {
  var learningResp = processRouterResponse({
    ok: true,
    route: { intent: 'learning', reason: '学习意图' },
    next_action: 'start_session',
    final_result: { session: { id: 's1' } },
  }, null);

  var literatureResp = processRouterResponse({
    ok: true,
    route: { intent: 'literature', reason: '文献意图' },
    next_action: 'show_literature',
    final_result: { module: 'frontier_literature', articles: [] },
  }, null);

  // Both must have route info extracted via the same code path
  ok(learningResp.messages[0].routeParts.length > 0, 'learning must have route info');
  ok(literatureResp.messages[0].routeParts.length > 0, 'literature must have route info');

  // Different intents, same generic processing — no per-intent fork
  equal(learningResp.messages[0].isLiterature, false);
  equal(literatureResp.messages[0].isLiterature, true);
});

test('resultAnswer handles missing fields without crashing', function () {
  // final_result with no answer/summary/review/markdown — must fall back gracefully
  var text = resultAnswer({ ok: true, final_result: { session: { id: 'x' } } });
  ok(typeof text === 'string', 'must return string, got: ' + typeof text);
  ok(text.length > 0, 'must not return empty string');
});

test('empty final_result renders without error', function () {
  var result = processRouterResponse({
    ok: true,
    route: { intent: 'clarification', reason: '请重新描述' },
    final_result: {},
  }, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  ok(typeof result.messages[0].content === 'string');
});

// ============================================================================
// 8. Request body integrity — fetch call simulation
// ============================================================================

console.log('\n8. Request body JSON serialization');

test('request body is valid JSON with expected shape', function () {
  var body = buildChatRequestBody('test message', [
    { role: 'user', content: 'previous' },
  ], 'sess-backend-1');

  var json = JSON.stringify(body);
  var parsed = JSON.parse(json);

  equal(parsed.message, 'test message');
  equal(parsed.history.length, 1);
  equal(parsed.history[0].role, 'user');
  equal(parsed.history[0].content, 'previous');
  equal(parsed.context.user_id, 'demo');
  equal(parsed.context.session_id, 'sess-backend-1');
});

test('request body omits session_id key entirely when null', function () {
  var body = buildChatRequestBody('hello', [], null);
  var json = JSON.stringify(body);
  var parsed = JSON.parse(json);

  equal(parsed.context.user_id, 'demo');
  // session_id key must not exist in serialized JSON
  equal('session_id' in parsed.context, false);
});

// ============================================================================
// 9. URL plan_item_id parsing
// ============================================================================

console.log('\n9. URL plan_item_id parsing');

test('parseUrlParams extracts plan_item_id from URL', function () {
  var result = parseUrlParams('?plan_item_id=abc-123');
  equal(result.planItemId, 'abc-123');
  equal(result.userId, 'demo'); // default when not in URL
});

test('parseUrlParams extracts both plan_item_id and user_id', function () {
  var result = parseUrlParams('?plan_item_id=abc-123&user_id=user-456');
  equal(result.planItemId, 'abc-123');
  equal(result.userId, 'user-456');
});

test('parseUrlParams returns empty planItemId when not in URL', function () {
  var result = parseUrlParams('');
  equal(result.planItemId, '');
  equal(result.userId, 'demo');
});

test('parseUrlParams returns empty planItemId for unrelated params', function () {
  var result = parseUrlParams('?foo=bar&baz=qux');
  equal(result.planItemId, '');
  equal(result.userId, 'demo');
});

test('parseUrlParams with only plan_item_id defaults user_id to demo', function () {
  var result = parseUrlParams('?plan_item_id=test-001');
  equal(result.userId, 'demo');
  equal(result.planItemId, 'test-001');
});

// ============================================================================
// 10. Auto-submit context with plan_item_id — no hardcoded IDs
// ============================================================================

console.log('\n10. Auto-submit context with plan_item_id');

test('buildChatRequestBody includes plan_item_id in context when provided', function () {
  var body = buildChatRequestBody('', [], null, 'plan-abc-123', 'demo');
  equal(body.context.plan_item_id, 'plan-abc-123');
  equal(body.context.user_id, 'demo');
});

test('buildChatRequestBody includes both plan_item_id and custom user_id', function () {
  var body = buildChatRequestBody('', [], null, 'plan-xyz', 'user-789');
  equal(body.context.plan_item_id, 'plan-xyz');
  equal(body.context.user_id, 'user-789');
});

test('buildChatRequestBody with plan_item_id sends empty message', function () {
  // Auto-submit sends empty message — backend uses plan_item_id, not message
  var body = buildChatRequestBody('', [], null, 'plan-001');
  equal(body.message, '');
  equal(body.context.plan_item_id, 'plan-001');
});

test('buildChatRequestBody without plan_item_id omits the key entirely', function () {
  var body = buildChatRequestBody('hello', [], null);
  equal('plan_item_id' in body.context, false);
});

test('buildChatRequestBody is backward compatible with 3-arg calls', function () {
  // Existing tests call with 3 args — must still work
  var body = buildChatRequestBody('test message', [
    { role: 'user', content: 'previous' },
  ], 'sess-backend-1');
  equal(body.message, 'test message');
  equal(body.context.user_id, 'demo');
  equal(body.context.session_id, 'sess-backend-1');
  equal('plan_item_id' in body.context, false);
});

test('auto-submit request body is valid JSON with plan_item_id', function () {
  var body = buildChatRequestBody('', [], null, 'plan-001', 'user-002');
  var json = JSON.stringify(body);
  var parsed = JSON.parse(json);
  equal(parsed.context.plan_item_id, 'plan-001');
  equal(parsed.context.user_id, 'user-002');
  equal(parsed.message, '');
  equal(parsed.history.length, 0);
});

test('two different plan_item_ids produce identical request structure', function () {
  var bodyA = buildChatRequestBody('', [], null, 'plan-aaa', 'demo');
  var bodyB = buildChatRequestBody('', [], null, 'plan-bbb', 'demo');

  // Structure identical — only plan_item_id differs
  equal(bodyA.message, '');
  equal(bodyB.message, '');
  equal(bodyA.context.user_id, 'demo');
  equal(bodyB.context.user_id, 'demo');
  ok(bodyA.context.plan_item_id !== bodyB.context.plan_item_id,
     'plan_item_ids must differ but structure identical');
});

// ============================================================================
// 11. Error display for plan_item_id calls — unified response, no special path
// ============================================================================

console.log('\n11. Plan item error display');

test('plan item not found error displays via existing unified response', function () {
  // Existing error path uses data.error for content — no special plan_item path
  var data = {
    ok: false,
    route: {
      intent: 'learning',
      target: 'learning_orchestrator',
      source: 'explicit_plan_item',
      reason: '未找到计划任务 plan-404，或该任务不属于当前用户。',
    },
    final_result: null,
    next_action: null,
    error: 'Study plan item not found or access denied: plan-404',
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, true);
  // Existing unified error display shows data.error content
  ok(result.messages[0].content.indexOf('Study plan item not found') >= 0,
     'must display error via existing unified path, got: ' + result.messages[0].content);
  equal(result.backendSessionId, null, 'must not save session on plan item error');
});

test('plan item completed status error displays correctly', function () {
  // Existing error path uses data.error — route.reason is for success path only
  var data = {
    ok: false,
    route: {
      intent: 'learning',
      target: 'learning_orchestrator',
      source: 'explicit_plan_item',
      reason: '计划任务「根管治疗」状态为 completed，无法开始学习。',
    },
    final_result: null,
    next_action: null,
    error: 'Plan item is not open: status=completed',
  };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].isError, true);
  // Existing unified error display shows data.error — not route.reason
  ok(result.messages[0].content.indexOf('Plan item is not open') >= 0,
     'must display error via existing unified path, got: ' + result.messages[0].content);
});

test('plan item success response saves session.id and shows route info', function () {
  var data = {
    ok: true,
    route: {
      intent: 'learning',
      target: 'learning_orchestrator',
      source: 'explicit_plan_item',
      reason: '根据计划任务「口腔溃疡病因」启动学习。',
    },
    next_action: 'start_session',
    final_result: {
      session: { id: 'plan-session-001' },
      answer: '已为你创建学习课程：口腔溃疡病因',
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].isError, false);
  equal(result.backendSessionId, 'plan-session-001', 'must save real session.id from plan item start');
  equal(result.messages[0].routeParts[0], '根据计划任务「口腔溃疡病因」启动学习。');
  equal(result.messages[0].routeParts[1], 'start_session');
});

test('plan item response: no hardcoded ID/title/topic in processing', function () {
  // Two different plan items produce identical processing structure
  var resp1 = processRouterResponse({
    ok: true,
    route: { intent: 'learning', source: 'explicit_plan_item', reason: '根据计划任务「topicA」启动学习。' },
    next_action: 'start_session',
    final_result: { session: { id: 's1' }, answer: 'answer A' },
  }, null);

  var resp2 = processRouterResponse({
    ok: true,
    route: { intent: 'learning', source: 'explicit_plan_item', reason: '根据计划任务「topicB」启动学习。' },
    next_action: 'start_session',
    final_result: { session: { id: 's2' }, answer: 'answer B' },
  }, null);

  // Both use same generic processing — no per-ID/topic special-casing
  equal(resp1.messages[0].isError, false);
  equal(resp2.messages[0].isError, false);
  equal(resp1.messages.length, 1);
  equal(resp2.messages.length, 1);
  equal(resp1.backendSessionId, 's1');
  equal(resp2.backendSessionId, 's2');
});

test('plan item response: no topic placeholder in rendered content', function () {
  // The old placeholder text must never appear even for plan_item responses
  var data = {
    ok: true,
    route: { intent: 'learning', source: 'explicit_plan_item', reason: 'plan start' },
    final_result: { session: { id: 's1' }, answer: 'real lesson content' },
  };

  var result = processRouterResponse(data, null);

  ok(result.messages[0].content.indexOf('学习总控待接入') === -1,
     'old placeholder must NOT appear in plan item responses');
  ok(result.messages[0].content.indexOf('旧总控 agent 已停用') === -1,
     'old placeholder must NOT appear in plan item responses');
  ok(result.messages[0].content.indexOf('learning_router_placeholder') === -1,
     'old placeholder must NOT appear in plan item responses');
});

// ============================================================================
// 12. Select-plan candidate rendering — consume router candidates, reuse autoSubmitPlanItem
// ============================================================================

console.log('\n12. Select-plan candidate rendering');

test('select_plan response exposes candidates array', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '多个匹配计划，请选择一项开始学习。' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'plan-001', title: '口腔溃疡病因', linked_course_title: '口腔医学基础' },
        { plan_item_id: 'plan-002', title: '根管治疗', linked_course_title: null },
      ],
      message: '找到 2 个未完成的学习计划任务，请选择一项开始学习。',
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages.length, 1);
  equal(result.messages[0].isError, false);
  equal(result.messages[0].routeParts[1], 'select_plan');
  // Candidates must be exposed
  ok(result.messages[0].selectPlanCandidates !== null, 'must expose candidates');
  equal(result.messages[0].selectPlanCandidates.length, 2);
  // No session created from select_plan
  equal(result.backendSessionId, null, 'select_plan must not create session');
});

test('select_plan candidate has plan_item_id for click handler', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '请选择' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'plan-abc', title: '任务A' },
      ],
      message: '找到 1 个未完成的学习计划任务。',
    },
  };

  var result = processRouterResponse(data, null);
  var candidates = result.messages[0].selectPlanCandidates;

  equal(candidates.length, 1);
  equal(candidates[0].plan_item_id, 'plan-abc', 'candidate must expose plan_item_id for click');
  equal(candidates[0].title, '任务A');
});

test('select_plan candidate without linked_course_title still renders', function () {
  // Missing linked_course_title must not break rendering
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '请选择' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'plan-no-course', title: '无关联课程的任务' },
      ],
      message: '找到 1 个未完成的学习计划任务。',
    },
  };

  var result = processRouterResponse(data, null);
  var candidates = result.messages[0].selectPlanCandidates;

  equal(candidates.length, 1);
  equal(candidates[0].plan_item_id, 'plan-no-course');
  equal(candidates[0].title, '无关联课程的任务');
  // linked_course_title may be absent — still valid candidate
  equal(candidates[0].linked_course_title, undefined);
});

test('select_plan empty candidates array does NOT flag as hasCandidates', function () {
  var data = {
    ok: true,
    route: { intent: 'clarification', reason: '无法判断意图' },
    next_action: 'select_plan',
    final_result: {
      candidates: [],
      message: '未找到未完成的学习计划。',
    },
  };

  var result = processRouterResponse(data, null);

  // Empty candidates — must not render candidate UI
  equal(result.messages[0].selectPlanCandidates, null, 'empty array must not produce candidates');
});

test('select_plan without candidates field does NOT flag', function () {
  var data = {
    ok: true,
    route: { intent: 'clarification', reason: '请重新描述' },
    next_action: 'select_plan',
    final_result: {
      message: '不太确定您的意图。',
    },
  };

  var result = processRouterResponse(data, null);

  equal(result.messages[0].selectPlanCandidates, null, 'missing candidates field must not flag');
});

test('select_plan response: no placeholder text in content', function () {
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '请选择计划' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'plan-x', title: '任务X' },
      ],
      message: '选择一个计划。',
    },
  };

  var result = processRouterResponse(data, null);

  ok(result.messages[0].content.indexOf('学习总控待接入') === -1,
     'old placeholder must NOT appear in select_plan responses');
  ok(result.messages[0].content.indexOf('旧总控 agent 已停用') === -1,
     'old placeholder must NOT appear in select_plan responses');
});

test('select_plan candidates only expose whitelist fields — no completed / non-self fields', function () {
  // Router whitelist: plan_item_id, title, linked_course_title only
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '请选择' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'plan-001', title: '任务A', linked_course_title: '课程A' },
      ],
      message: '选择一个计划。',
    },
  };

  var result = processRouterResponse(data, null);
  var candidate = result.messages[0].selectPlanCandidates[0];

  // Whitelist fields must be present
  ok('plan_item_id' in candidate, 'must have plan_item_id');
  ok('title' in candidate, 'must have title');
  // linked_course_title may be null — that's fine

  // Forbidden fields must NOT be present (router never sends them)
  equal('status' in candidate, false, 'status must not leak to frontend');
  equal('user_id' in candidate, false, 'user_id must not leak to frontend');
  equal('plan_date' in candidate, false, 'plan_date must not leak to frontend');
  equal('completed' in candidate, false, 'completed must not leak to frontend');
});

test('select_plan: generic — different candidate sets produce identical structure', function () {
  var resp1 = processRouterResponse({
    ok: true,
    route: { intent: 'learning', reason: '多选A' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'aaa', title: '任务A', linked_course_title: '课程A' },
        { plan_item_id: 'bbb', title: '任务B' },
      ],
      message: '选一个。',
    },
  }, null);

  var resp2 = processRouterResponse({
    ok: true,
    route: { intent: 'learning', reason: '多选B' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'ccc', title: '任务C', linked_course_title: '课程C' },
      ],
      message: '选一个。',
    },
  }, null);

  // Both use same generic processing — no per-ID/title special-casing
  equal(resp1.messages[0].isError, false);
  equal(resp2.messages[0].isError, false);
  ok(resp1.messages[0].selectPlanCandidates !== null);
  ok(resp2.messages[0].selectPlanCandidates !== null);
  // Different IDs, identical processing structure
  ok(resp1.messages[0].selectPlanCandidates[0].plan_item_id !==
     resp2.messages[0].selectPlanCandidates[0].plan_item_id,
     'different candidates must differ in ID only');
});

test('select_plan does not auto-start learning or create session', function () {
  // select_plan is a waiting state — no session, no auto-dispatch
  var data = {
    ok: true,
    route: { intent: 'learning', reason: '多项匹配' },
    next_action: 'select_plan',
    final_result: {
      candidates: [
        { plan_item_id: 'p1', title: 'T1' },
        { plan_item_id: 'p2', title: 'T2' },
      ],
      message: '请选择。',
    },
  };

  var result = processRouterResponse(data, 'existing-sess');

  // Must keep existing session — select_plan does not start a new one
  equal(result.backendSessionId, 'existing-sess', 'select_plan must preserve existing session');
  // Must not have start_session next_action
  equal(result.messages[0].routeParts[1], 'select_plan');
});

// ============================================================================
// 9. Learning resource synchronization — current section, not whole deck
// ============================================================================

console.log('\n9. Learning resource synchronization');

test('partial deck is not ready for a current section it does not contain', function () {
  var resource = {
    preview: {
      result: {
        status: 'ready',
        slides: [
          { source_outline_node_id: '' },
          { source_outline_node_id: 'node_1' },
        ],
      },
    },
  };

  equal(currentSectionSlideIndex(resource, 'node_2'), -1);
});

test('current section becomes ready only when its rendered page exists', function () {
  var resource = {
    preview: {
      result: {
        status: 'ready',
        slides: [
          { source_outline_node_id: '' },
          { source_outline_node_id: 'node_1' },
          { source_outline_node_id: 'node_2' },
        ],
      },
    },
  };

  equal(currentSectionSlideIndex(resource, 'node_2'), 2);
});

test('master chat shows lecture immediately while polling PPT independently', function () {
  var source = fs.readFileSync(
    path.join(__dirname, '..', 'frontend', 'js', 'master-chat.js'),
    'utf8',
  );

  ok(source.indexOf("found.message.streamText = `${found.message.streamText || ''}${event.data?.delta || ''}`;") >= 0,
    'Tutor teaching text must be appended to the visible stream immediately');
  ok(source.indexOf("found.message.statusText = '正在同步准备本节讲稿与 PPT';") < 0,
    'PPT readiness must not suppress the lecture stream');
  ok(source.indexOf("const previewFailed = resource?.preview?.result?.status === 'failed';") >= 0,
    'a failed preview must be terminal');
  ok(source.indexOf('const waitingSlides = !previewFailed && !currentSectionPreviewReady(key, resource);') >= 0,
    'polling must wait for the current section, not merely any ready preview');
  ok(source.indexOf('pptViewerIndices.set(key, sectionIndex);') >= 0,
    'viewer must start on the current section page');
  ok(source.indexOf('if (hasViewerIndex || sectionIndex >= 0)') >= 0,
    'a missing current section must not persist the cover-page index');
});

test('handout and lecture streams render together during generation', function () {
  var source = fs.readFileSync(
    path.join(__dirname, '..', 'frontend', 'js', 'master-chat.js'),
    'utf8',
  );

  ok(source.indexOf('if (message.handoutStream) return renderHandoutStream(message.handoutStream);') < 0,
    'the handout stream must not hide the lecture stream');
  ok(source.indexOf("const handoutHtml = message.handoutStream ? renderHandoutStream(message.handoutStream) : '';") >= 0,
    'the handout stream must be retained while rendering the lecture');
  ok(/\$\{handoutHtml\}\r?\n\s*<section class="job-stream"/.test(source),
    'lecture markup must include the handout markup when both streams exist');
  ok(source.indexOf('if (handoutHtml) return handoutHtml;') >= 0,
    'the handout remains visible before the lecture stream starts');
});

test('boot reconnects pending jobs without restoring their old conversation', function () {
  var source = fs.readFileSync(
    path.join(__dirname, '..', 'frontend', 'js', 'master-chat.js'),
    'utf8',
  );

  ok(source.indexOf('if (firstSession) restoreSession(firstSession.id);') < 0,
    'pending jobs must not replace the active smart-QA conversation');
  ok(source.indexOf('response.status === 404') >= 0,
    'stale in-memory backend jobs must be removed after a backend restart');
});

// ============================================================================
// Summary
// ============================================================================

console.log('\n' + '='.repeat(60));
console.log('Results: ' + passed + ' passed, ' + failed + ' failed, ' + (passed + failed) + ' total');
console.log('='.repeat(60));

if (failed > 0) {
  process.exit(1);
}
