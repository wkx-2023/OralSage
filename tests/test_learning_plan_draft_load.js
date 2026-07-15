// tests/test_learning_plan_draft_load.js
// Static Node tests for learning-plan.js draft loading behavior.
// No external dependencies. No real network calls.
// Validates: plan_draft_id URL extraction, draft→recommendations mapping,
// successful load, error no-fallback, no-ID compatibility,
// requestRecommendations blocked when draft ID present.

'use strict';

var assert = require('assert');

// ============================================================================
// Test helpers — mirrors logic from frontend/js/learning-plan.js
// ============================================================================

/**
 * Extract plan_draft_id from URL search params.
 * Mirrors: learning-plan.js state.planDraftId initialization
 */
function extractPlanDraftId(queryString) {
  // In the real code this uses URLSearchParams(window.location.search)
  // We simulate various query string inputs here.
  var qs = queryString || '';
  // Simple parsing that mirrors URLSearchParams.get('plan_draft_id') || ''
  if (!qs) return '';
  var match = qs.match(/[?&]plan_draft_id=([^&]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

/**
 * Map draft items to recommendation card shape.
 * Mirrors: learning-plan.js mapDraftItemsToRecommendations()
 */
function mapDraftItemsToRecommendations(draftId, items) {
  return items.map(function (item, i) {
    var scope = item.learning_scope || {};
    return {
      recommendation_id: draftId + ':' + i,
      title: item.learning_target || item.title || '',
      task_type: item.task_type || 'topic_learning',
      learning_target: item.learning_target || '',
      reason: item.reason || '',
      estimated_minutes: item.estimated_minutes || 0,
      kg_node_ids: Array.isArray(scope.kg_node_ids) ? scope.kg_node_ids : [],
      learning_scope: item.learning_scope || null,
      linked_course_title: scope.book_title || scope.chapter_title || null,
      linked_course_id: scope.document_id || null,
      category: item.task_type || 'recommend',
      evidence_source: item.evidence_source || null,
    };
  });
}

/**
 * Simulate loadPlanDraft success path.
 * Returns { success, recommendations, draftLoaded }.
 */
function simulateDraftLoadSuccess(draftId, draftItems) {
  var items = Array.isArray(draftItems) ? draftItems : [];
  if (!items.length) {
    return { success: true, recommendations: [], draftLoaded: true, hasItems: false };
  }
  var recommendations = mapDraftItemsToRecommendations(draftId, items);
  return { success: true, recommendations: recommendations, draftLoaded: true, hasItems: true };
}

/**
 * Simulate loadPlanDraft error path.
 * Returns { success, error }.
 */
function simulateDraftLoadError(errMessage) {
  return { success: false, error: errMessage || '计划草案加载失败', draftLoaded: false, recommendations: [] };
}

/**
 * Simulate requestRecommendations guard.
 * Returns { blocked: true, reason } if planDraftId is non-empty.
 * Returns { blocked: false } otherwise.
 */
function shouldBlockRequestRecommendations(planDraftId) {
  if (planDraftId) {
    return { blocked: true, reason: '当前页面已加载聊天计划草案，如需系统推荐请重新打开计划页' };
  }
  return { blocked: false };
}

/**
 * Extract items from a real API draft record as returned by
 * GET /api/learning/plan/drafts/{plan_draft_id}.
 *
 * Contract (verified by tests/test_learning_plan_api.py):
 *   body.result.payload.items  — the ONLY valid path to draft items.
 *   body.result.items          — does NOT exist; must never be treated as success.
 *
 * Mirrors the corrected loadPlanDraft extraction.
 */
function extractItemsFromDraftRecord(record) {
  if (!record || typeof record !== 'object') {
    return { items: [], error: '无效的记录结构' };
  }
  var payload = record.payload;
  if (!payload || typeof payload !== 'object') {
    return { items: [], error: '记录缺少 payload 字段' };
  }
  var items = payload.items;
  if (!Array.isArray(items)) {
    return { items: [], error: 'payload 中缺少 items 数组' };
  }
  return { items: items, error: null };
}

/**
 * Build a real API draft record in the shape verified by
 * tests/test_learning_plan_api.py.
 *
 *   {
 *     plan_draft_id, user_id, created_at, expires_at,
 *     payload: { request: {...}, items: [...] }
 *   }
 */
function makeDraftRecord(draftId, userId, draftItems) {
  return {
    plan_draft_id: draftId,
    user_id: userId,
    created_at: 1752595200,
    expires_at: 1752598800,
    payload: {
      request: {
        intent: 'review_chapter',
        days: 3,
        daily_minutes: 30,
        focus_text: null,
        book_title: null,
        document_id: null,
        chapter_key: null,
      },
      items: draftItems || [],
    },
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

function deepEqual(actual, expected, msg) {
  assert.deepStrictEqual(actual, expected, msg);
}

function ok(value, msg) {
  assert.ok(value, msg);
}

// ============================================================================
// 1. plan_draft_id URL extraction
// ============================================================================

console.log('\n1. plan_draft_id URL extraction');

test('extracts plan_draft_id from query string', function () {
  var id = extractPlanDraftId('?plan_draft_id=draft_abc123');
  equal(id, 'draft_abc123');
});

test('extracts plan_draft_id alongside other params', function () {
  var id = extractPlanDraftId('?user_id=demo&plan_draft_id=draft_xyz789&other=value');
  equal(id, 'draft_xyz789');
});

test('returns empty string when plan_draft_id is absent', function () {
  var id = extractPlanDraftId('?user_id=demo');
  equal(id, '');
});

test('returns empty string for query string without plan_draft_id', function () {
  var id = extractPlanDraftId('');
  equal(id, '');
});

test('decodes percent-encoded draft IDs', function () {
  var id = extractPlanDraftId('?plan_draft_id=draft_hello%20world');
  equal(id, 'draft_hello world');
});

test('decodes draft IDs with special characters', function () {
  var id = extractPlanDraftId('?plan_draft_id=draft_test%26key%3Dvalue');
  equal(id, 'draft_test&key=value');
});

// ============================================================================
// 2. mapDraftItemsToRecommendations — successful mapping
// ============================================================================

console.log('\n2. mapDraftItemsToRecommendations — successful mapping');

test('maps single draft item with learning_scope', function () {
  var items = [{
    learning_target: '复习牙周病学第三章',
    task_type: 'review_chapter',
    reason: '用户指定第三章复习',
    estimated_minutes: 30,
    evidence_source: 'user_request + knowledge_graph',
    learning_scope: {
      book_title: '牙周病学',
      document_id: 'doc-001',
      chapter_key: 'chapter:3',
      chapter_title: '第三章 炎症机制',
      kg_node_ids: ['kg-001', 'kg-002'],
    },
  }];
  var result = mapDraftItemsToRecommendations('draft_001', items);
  equal(result.length, 1);
  equal(result[0].recommendation_id, 'draft_001:0');
  equal(result[0].title, '复习牙周病学第三章');
  equal(result[0].task_type, 'review_chapter');
  equal(result[0].reason, '用户指定第三章复习');
  equal(result[0].estimated_minutes, 30);
  equal(result[0].evidence_source, 'user_request + knowledge_graph');
  equal(result[0].linked_course_title, '牙周病学');
  equal(result[0].linked_course_id, 'doc-001');
  deepEqual(result[0].kg_node_ids, ['kg-001', 'kg-002']);
  ok(result[0].learning_scope !== null, 'learning_scope must be preserved');
});

test('maps multiple draft items with sequential indices', function () {
  var items = [
    { learning_target: '任务A', task_type: 'review', reason: '理由A', estimated_minutes: 20, learning_scope: {} },
    { learning_target: '任务B', task_type: 'practice', reason: '理由B', estimated_minutes: 30, learning_scope: {} },
    { learning_target: '任务C', task_type: 'topic_learning', reason: '理由C', estimated_minutes: 25, learning_scope: {} },
  ];
  var result = mapDraftItemsToRecommendations('draft_multi', items);
  equal(result.length, 3);
  equal(result[0].recommendation_id, 'draft_multi:0');
  equal(result[1].recommendation_id, 'draft_multi:1');
  equal(result[2].recommendation_id, 'draft_multi:2');
  equal(result[0].title, '任务A');
  equal(result[1].title, '任务B');
  equal(result[2].title, '任务C');
});

test('handles item with missing learning_scope gracefully', function () {
  var items = [{
    learning_target: '自由学习',
    task_type: 'topic_learning',
    reason: '通用推荐',
    estimated_minutes: 15,
  }];
  var result = mapDraftItemsToRecommendations('draft_noscope', items);
  equal(result[0].kg_node_ids.length, 0);
  equal(result[0].linked_course_title, null);
  equal(result[0].linked_course_id, null);
  equal(result[0].learning_scope, null);
  equal(result[0].category, 'topic_learning');
});

test('falls back to item.title when learning_target is missing', function () {
  var items = [{
    title: '备用标题',
    reason: '理由',
    estimated_minutes: 10,
  }];
  var result = mapDraftItemsToRecommendations('draft_title_fallback', items);
  equal(result[0].title, '备用标题');
  equal(result[0].learning_target, '');
});

test('uses chapter_title as linked_course_title when book_title absent', function () {
  var items = [{
    learning_target: '学习某章',
    reason: 'r',
    estimated_minutes: 20,
    learning_scope: {
      chapter_title: '第三章 牙体牙髓病',
      kg_node_ids: ['kg-003'],
    },
  }];
  var result = mapDraftItemsToRecommendations('draft_ch_only', items);
  equal(result[0].linked_course_title, '第三章 牙体牙髓病');
  deepEqual(result[0].kg_node_ids, ['kg-003']);
});

test('empty items array returns empty result', function () {
  var result = mapDraftItemsToRecommendations('draft_empty', []);
  equal(result.length, 0);
});

// ============================================================================
// 3. Successful draft load simulation
// ============================================================================

console.log('\n3. Successful draft load simulation');

test('successful load populates recommendations and sets draftLoaded', function () {
  var items = [
    { learning_target: '复习第三章', task_type: 'review_chapter', reason: '用户指定', estimated_minutes: 30, learning_scope: {} },
  ];
  var result = simulateDraftLoadSuccess('draft_ok', items);
  ok(result.success, 'must be successful');
  ok(result.draftLoaded, '_draftLoaded must be true');
  ok(result.hasItems, 'hasItems must be true');
  equal(result.recommendations.length, 1);
  equal(result.recommendations[0].recommendation_id, 'draft_ok:0');
});

test('successful load with empty items still sets draftLoaded', function () {
  var result = simulateDraftLoadSuccess('draft_noitems', []);
  ok(result.success);
  ok(result.draftLoaded, '_draftLoaded must be true even with no items');
  ok(!result.hasItems, 'hasItems must be false');
  equal(result.recommendations.length, 0);
});

test('draft items preserve learning_scope for adoption payload', function () {
  var scope = {
    book_title: '口腔颌面外科学',
    document_id: 'doc-002',
    chapter_key: 'chapter:5',
    chapter_title: '第五章 颌骨骨折',
    kg_node_ids: ['kg-010', 'kg-011'],
  };
  var items = [{
    learning_target: '复习颌骨骨折',
    task_type: 'review_chapter',
    reason: '用户指定第五章',
    estimated_minutes: 45,
    evidence_source: 'user_request + knowledge_graph',
    learning_scope: scope,
  }];
  var result = simulateDraftLoadSuccess('draft_scope_full', items);
  var rec = result.recommendations[0];
  equal(rec.linked_course_title, '口腔颌面外科学');
  equal(rec.linked_course_id, 'doc-002');
  deepEqual(rec.kg_node_ids, ['kg-010', 'kg-011']);
  ok(rec.learning_scope !== null);
  equal(rec.learning_scope.chapter_key, 'chapter:5');
  equal(rec.evidence_source, 'user_request + knowledge_graph');
});

// ============================================================================
// 3b. Real API record shape → payload.items extraction (handoff contract)
// ============================================================================
// The API returns: { ok, result: { plan_draft_id, user_id, payload: { request, items } } }
// The frontend MUST read result.payload.items — NOT result.items (doesn't exist).
// Shape is verified by tests/test_learning_plan_api.py.

console.log('\n3b. Real API record shape → payload.items extraction');

test('extracts items from a real record with payload.items containing learning_scope', function () {
  var scope = {
    book_title: '牙周病学',
    document_id: 'document.d766e8629232a2f6',
    chapter_key: 'chapter:perio-ch3',
    chapter_title: '第三章 牙周病的发病机制',
    kg_node_ids: ['core:aaa111', 'core:bbb222', 'core:ccc333'],
  };
  var items = [{
    learning_target: '牙周病的发病机制（1/3）',
    task_type: 'learn_chapter',
    reason: '学习《牙周病学》牙周病的发病机制',
    estimated_minutes: 30,
    evidence_source: 'kg_chapter',
    learning_scope: scope,
  }];
  var record = makeDraftRecord('draft_real_001', 'u1', items);
  var extracted = extractItemsFromDraftRecord(record);

  equal(extracted.error, null, 'must have no error');
  equal(extracted.items.length, 1, 'must extract one item');
  equal(extracted.items[0].learning_target, '牙周病的发病机制（1/3）');
  ok(extracted.items[0].learning_scope !== null, 'learning_scope must be preserved');
  equal(extracted.items[0].learning_scope.book_title, '牙周病学');
  equal(extracted.items[0].learning_scope.document_id, 'document.d766e8629232a2f6');
  deepEqual(extracted.items[0].learning_scope.kg_node_ids, ['core:aaa111', 'core:bbb222', 'core:ccc333']);
});

test('extracts multiple items from real record (matching test_learning_plan_api.py item count)', function () {
  var items = [
    { learning_target: '任务1', task_type: 'learn_chapter', reason: 'r1', estimated_minutes: 30, evidence_source: 'kg_chapter', learning_scope: { book_title: '牙周病学', kg_node_ids: ['kg-1'] } },
    { learning_target: '任务2', task_type: 'learn_chapter', reason: 'r2', estimated_minutes: 30, evidence_source: 'kg_chapter', learning_scope: { book_title: '牙周病学', kg_node_ids: ['kg-2'] } },
    { learning_target: '任务3', task_type: 'learn_chapter', reason: 'r3', estimated_minutes: 30, evidence_source: 'kg_chapter', learning_scope: { book_title: '牙周病学', kg_node_ids: ['kg-3'] } },
  ];
  var record = makeDraftRecord('draft_real_003', 'u1', items);
  var extracted = extractItemsFromDraftRecord(record);

  equal(extracted.error, null);
  equal(extracted.items.length, 3, 'must match the 3-item contract from test_learning_plan_api.py');
});

test('extracted items can be mapped to recommendation cards via mapDraftItemsToRecommendations', function () {
  var items = [{
    learning_target: '复习第三章',
    task_type: 'review_chapter',
    reason: '用户指定',
    estimated_minutes: 45,
    evidence_source: 'user_request + knowledge_graph',
    learning_scope: {
      book_title: '口腔颌面外科学',
      document_id: 'doc-omfs',
      chapter_key: 'chapter:omfs-5',
      chapter_title: '第五章 颌骨骨折',
      kg_node_ids: ['kg-010', 'kg-011'],
    },
  }];
  var record = makeDraftRecord('draft_chain_001', 'u1', items);
  var extracted = extractItemsFromDraftRecord(record);

  equal(extracted.error, null);
  var recs = mapDraftItemsToRecommendations('draft_chain_001', extracted.items);
  equal(recs.length, 1);
  equal(recs[0].title, '复习第三章');
  equal(recs[0].linked_course_title, '口腔颌面外科学');
  equal(recs[0].linked_course_id, 'doc-omfs');
  deepEqual(recs[0].kg_node_ids, ['kg-010', 'kg-011']);
  equal(recs[0].evidence_source, 'user_request + knowledge_graph');
});

// ============================================================================
// 3c. Reject top-level items proxy / missing payload — MUST NOT fake success
// ============================================================================

console.log('\n3c. Reject top-level items proxy / missing payload');

test('record with top-level items but NO payload → error (proxy rejected)', function () {
  // A record with items at the top level is NOT the real API shape.
  // The corrected loadPlanDraft reads payload.items; a top-level items
  // must never be treated as success.
  var badRecord = {
    plan_draft_id: 'draft_bad_proxy',
    user_id: 'u1',
    items: [{ learning_target: '伪造任务', reason: '不应该出现', estimated_minutes: 10 }],
  };
  var extracted = extractItemsFromDraftRecord(badRecord);

  ok(extracted.error !== null, 'must produce an error for missing payload');
  equal(extracted.items.length, 0, 'must return empty items on error');
  ok(extracted.error.indexOf('payload') > -1, 'error must mention missing payload');
});

test('record with payload but NO items field → error', function () {
  var badRecord = {
    plan_draft_id: 'draft_no_items_field',
    user_id: 'u1',
    payload: { request: { intent: 'custom', days: 7, daily_minutes: 30 } },
  };
  var extracted = extractItemsFromDraftRecord(badRecord);

  ok(extracted.error !== null, 'must produce an error for missing items');
  equal(extracted.items.length, 0, 'must return empty items');
  ok(extracted.error.indexOf('items') > -1, 'error must mention missing items');
});

test('record with payload.items as non-array → error', function () {
  var badRecord = {
    plan_draft_id: 'draft_items_not_array',
    user_id: 'u1',
    payload: { request: {}, items: 'not-an-array' },
  };
  var extracted = extractItemsFromDraftRecord(badRecord);

  ok(extracted.error !== null, 'must produce an error');
  equal(extracted.items.length, 0);
});

test('record with null payload → error', function () {
  var badRecord = {
    plan_draft_id: 'draft_null_payload',
    user_id: 'u1',
    payload: null,
  };
  var extracted = extractItemsFromDraftRecord(badRecord);

  ok(extracted.error !== null, 'must produce an error');
  equal(extracted.items.length, 0);
});

test('record with empty payload object but no items → error', function () {
  var badRecord = {
    plan_draft_id: 'draft_empty_payload',
    user_id: 'u1',
    payload: {},
  };
  var extracted = extractItemsFromDraftRecord(badRecord);

  ok(extracted.error !== null, 'must produce an error');
  equal(extracted.items.length, 0);
});

test('null record → error', function () {
  var extracted = extractItemsFromDraftRecord(null);
  ok(extracted.error !== null);
  equal(extracted.items.length, 0);
});

test('undefined record → error', function () {
  var extracted = extractItemsFromDraftRecord(undefined);
  ok(extracted.error !== null);
  equal(extracted.items.length, 0);
});

test('non-object record → error', function () {
  var extracted = extractItemsFromDraftRecord('not-an-object');
  ok(extracted.error !== null);
  equal(extracted.items.length, 0);
});

test('record with payload and empty items array → success with zero items', function () {
  // Empty items array is valid — payload.items exists and is an array.
  var record = makeDraftRecord('draft_empty_items', 'u1', []);
  var extracted = extractItemsFromDraftRecord(record);

  equal(extracted.error, null, 'empty items array is valid, not an error');
  equal(extracted.items.length, 0);
});

// ============================================================================
// 4. Error path — no fallback to default recommendations
// ============================================================================

console.log('\n4. Error path — no fallback to default recommendations');

test('error: draftLoaded is false, recommendations remain empty', function () {
  var result = simulateDraftLoadError('草案已过期');
  ok(!result.success, 'must indicate failure');
  ok(!result.draftLoaded, '_draftLoaded must remain false');
  equal(result.recommendations.length, 0, 'no recommendations on error');
  ok(result.error.indexOf('草案') > -1, 'error message must describe the problem');
});

test('error: 404 unknown draft', function () {
  var result = simulateDraftLoadError('Unknown plan draft');
  ok(!result.success);
  ok(!result.draftLoaded);
  equal(result.recommendations.length, 0);
});

test('error: 403 cross-user access', function () {
  var result = simulateDraftLoadError('Draft does not belong to user');
  ok(!result.success);
  ok(!result.draftLoaded);
  equal(result.recommendations.length, 0);
});

test('error: 410 expired draft', function () {
  var result = simulateDraftLoadError('Draft已过期');
  ok(!result.success);
  ok(!result.draftLoaded);
  equal(result.recommendations.length, 0);
});

test('error path never populates recommendations', function () {
  // Any error scenario must not produce recommendations
  var errors = ['过期', '不存在', '无权访问', 'Network Error', '500 Internal Server Error'];
  errors.forEach(function (msg) {
    var result = simulateDraftLoadError(msg);
    ok(!result.success, 'error must not be success: ' + msg);
    equal(result.recommendations.length, 0, 'recommendations must be empty on error: ' + msg);
  });
});

// ============================================================================
// 5. No-ID compatibility — requestRecommendations NOT blocked
// ============================================================================

console.log('\n5. No-ID compatibility — requestRecommendations NOT blocked');

test('empty planDraftId does not block requestRecommendations', function () {
  var result = shouldBlockRequestRecommendations('');
  ok(!result.blocked, 'empty draft ID must not block');
});

test('undefined planDraftId does not block (falsy check)', function () {
  var result = shouldBlockRequestRecommendations(undefined);
  ok(!result.blocked, 'undefined draft ID must not block');
});

test('null planDraftId does not block (falsy check)', function () {
  var result = shouldBlockRequestRecommendations(null);
  ok(!result.blocked, 'null draft ID must not block');
});

// ============================================================================
// 6. Draft ID present — requestRecommendations BLOCKED
// ============================================================================

console.log('\n6. Draft ID present — requestRecommendations BLOCKED');

test('non-empty planDraftId blocks requestRecommendations', function () {
  var result = shouldBlockRequestRecommendations('draft_abc123');
  ok(result.blocked, 'non-empty draft ID must block');
  ok(result.reason.indexOf('聊天计划草案') > -1, 'reason must reference draft');
});

test('any non-empty draft ID blocks', function () {
  var ids = ['draft_001', 'draft_test', 'draft_xyz', 'any_draft_id'];
  ids.forEach(function (id) {
    var result = shouldBlockRequestRecommendations(id);
    ok(result.blocked, 'must block for draft ID: ' + id);
  });
});

test('blocked with non-empty draft ID regardless of draft load success', function () {
  // Even if draft failed to load, the presence of plan_draft_id in URL
  // must still block requestRecommendations (no silent fallback)
  var result = shouldBlockRequestRecommendations('draft_expired_but_still_in_url');
  ok(result.blocked, 'must block even if draft might be expired/failed');
});

test('block reason does not contain technical error codes', function () {
  var result = shouldBlockRequestRecommendations('draft_123');
  ok(result.reason.indexOf('404') === -1, 'reason must not leak HTTP codes');
  ok(result.reason.indexOf('500') === -1, 'reason must not leak HTTP codes');
  ok(result.reason.indexOf('Error') === -1, 'reason must not leak generic errors');
});

// ============================================================================
// 7. Structural validation — draft vs system recommendations
// ============================================================================

console.log('\n7. Structural validation — draft vs system recommendations');

test('draft-loaded recommendations match card render shape', function () {
  // The renderRecommendationCard function expects: title, reason,
  // recommendation_id, category, linked_course_title, kg_node_ids, estimated_minutes
  var items = [{
    learning_target: '测试任务',
    task_type: 'review_chapter',
    reason: '测试依据',
    estimated_minutes: 30,
    evidence_source: 'test',
    learning_scope: {
      book_title: '测试教材',
      document_id: 'doc-test',
      kg_node_ids: ['kg-1', 'kg-2'],
    },
  }];
  var recs = mapDraftItemsToRecommendations('draft_card_shape', items);
  var card = recs[0];

  // All fields that renderRecommendationCard reads must be present
  ok(typeof card.title === 'string' && card.title.length > 0, 'title must be non-empty string');
  ok(typeof card.reason === 'string', 'reason must be string');
  ok(typeof card.recommendation_id === 'string' && card.recommendation_id.length > 0, 'recommendation_id must be non-empty string');
  ok(typeof card.category === 'string', 'category must be string');
  ok(typeof card.estimated_minutes === 'number', 'estimated_minutes must be number');
  ok(Array.isArray(card.kg_node_ids), 'kg_node_ids must be array');
  // linked_course_title is used by recommendationMeta for display
  ok(card.linked_course_title !== undefined, 'linked_course_title must be present');
});

test('draft recommendations include fields needed by recommendationPayload for adoption', function () {
  var items = [{
    learning_target: '采纳测试',
    task_type: 'review_chapter',
    reason: '采纳理由',
    estimated_minutes: 25,
    learning_scope: {
      book_title: '教材名',
      document_id: 'doc-adopt',
      kg_node_ids: ['kg-adopt-1'],
    },
  }];
  var recs = mapDraftItemsToRecommendations('draft_adopt', items);
  var item = recs[0];

  // recommendationPayload uses: title, notes/reason, linked_course_id,
  // linked_course_title, task_type, learning_target, kg_node_ids
  ok(typeof item.title === 'string' && item.title.length > 0, 'title for adoption');
  ok(typeof item.reason === 'string', 'reason → notes for adoption');
  ok(typeof item.task_type === 'string', 'task_type for adoption');
  ok(typeof item.learning_target === 'string', 'learning_target for adoption');
  ok(item.linked_course_id !== undefined, 'linked_course_id for adoption');
  ok(item.linked_course_title !== undefined, 'linked_course_title for adoption');
  ok(Array.isArray(item.kg_node_ids), 'kg_node_ids for adoption');
  ok(item.learning_scope !== undefined, 'learning_scope for adoption');
});

// ============================================================================
// 8. recommendation_id uniqueness and stability
// ============================================================================

console.log('\n8. recommendation_id uniqueness and stability');

test('same draft ID and items produce same recommendation_ids', function () {
  var items = [
    { learning_target: '任务1', reason: 'r1', estimated_minutes: 10, learning_scope: {} },
    { learning_target: '任务2', reason: 'r2', estimated_minutes: 20, learning_scope: {} },
  ];
  var result1 = mapDraftItemsToRecommendations('draft_stable', items);
  var result2 = mapDraftItemsToRecommendations('draft_stable', items);
  deepEqual(result1[0].recommendation_id, result2[0].recommendation_id);
  deepEqual(result1[1].recommendation_id, result2[1].recommendation_id);
});

test('different draft IDs produce different recommendation_id prefixes', function () {
  var items = [{ learning_target: '任务', reason: 'r', estimated_minutes: 10, learning_scope: {} }];
  var resultA = mapDraftItemsToRecommendations('draft_aaa', items);
  var resultB = mapDraftItemsToRecommendations('draft_bbb', items);
  ok(resultA[0].recommendation_id !== resultB[0].recommendation_id,
     'different draft IDs must produce different recommendation IDs');
  ok(resultA[0].recommendation_id.indexOf('draft_aaa') === 0, 'ID must be prefixed with draft ID');
  ok(resultB[0].recommendation_id.indexOf('draft_bbb') === 0, 'ID must be prefixed with draft ID');
});

// ============================================================================
// 9. Edge cases
// ============================================================================

console.log('\n9. Edge cases');

test('draft items with kg_node_ids as non-array are handled gracefully', function () {
  var items = [{
    learning_target: '任务',
    reason: 'r',
    estimated_minutes: 10,
    learning_scope: { kg_node_ids: 'not-an-array' },
  }];
  var result = mapDraftItemsToRecommendations('draft_bad_kg', items);
  deepEqual(result[0].kg_node_ids, [], 'non-array kg_node_ids must become empty array');
});

test('draft items with null learning_scope are handled', function () {
  var items = [{
    learning_target: '任务',
    reason: 'r',
    estimated_minutes: 10,
    learning_scope: null,
  }];
  var result = mapDraftItemsToRecommendations('draft_null_scope', items);
  deepEqual(result[0].kg_node_ids, []);
  equal(result[0].linked_course_title, null);
  equal(result[0].learning_scope, null);
});

test('draft items with missing estimated_minutes default to 0', function () {
  var items = [{
    learning_target: '无时长任务',
    reason: 'r',
  }];
  var result = mapDraftItemsToRecommendations('draft_no_mins', items);
  equal(result[0].estimated_minutes, 0);
});

test('multiple draft items maintain correct index ordering', function () {
  var items = [];
  for (var i = 0; i < 10; i++) {
    items.push({ learning_target: '任务' + i, reason: 'r' + i, estimated_minutes: 10, learning_scope: {} });
  }
  var result = mapDraftItemsToRecommendations('draft_10', items);
  equal(result.length, 10);
  for (var j = 0; j < 10; j++) {
    equal(result[j].recommendation_id, 'draft_10:' + j, 'index ' + j + ' must match');
  }
});

// ============================================================================
// 10. No hardcoded business content
// ============================================================================

console.log('\n10. Generic — no hardcoded business content');

test('map function does not hardcode specific book or chapter content', function () {
  var items = [
    { learning_target: '任意任务标题', task_type: '任意类型', reason: '任意理由', estimated_minutes: 99, learning_scope: {} },
  ];
  var result = mapDraftItemsToRecommendations('draft_generic_test', items);
  var forbidden = ['牙周病学', '口腔颌面外科学', '第三章', '炎症机制', '龋病'];
  var json = JSON.stringify(result);
  forbidden.forEach(function (term) {
    ok(json.indexOf(term) === -1, 'must not hardcode "' + term + '"');
  });
});

test('block reason is generic and not tied to specific draft content', function () {
  var result = shouldBlockRequestRecommendations('draft_any');
  var forbidden = ['牙周病学', '第三章', 'demo', 'test_user'];
  forbidden.forEach(function (term) {
    ok(result.reason.indexOf(term) === -1, 'block reason must not contain "' + term + '"');
  });
});

// ============================================================================
// 11. recommendationPayload — learning_scope / task_type / learning_target / reason
// ============================================================================
// Mirrors: learning-plan.js recommendationPayload()
// Requirement: when adopting a draft recommendation, the create payload MUST
// transmit learning_scope, task_type, learning_target, and reason.
// When scope is absent (system recommendation), the payload MUST NOT include
// the learning_scope key (backward compatible).

console.log('\n11. recommendationPayload — scope + draft metadata in create payload');

/**
 * Build the POST /api/learning/plan/items payload from a recommendation item.
 * Mirrors: learning-plan.js recommendationPayload()
 */
function buildRecommendationPayload(item, userId, planDate) {
  var payload = {
    user_id: userId,
    plan_date: planDate,
    title: String(item.title || '').trim(),
    notes: item.notes || item.reason || null,
    linked_course_id: item.linked_course_id || null,
    linked_course_title: item.linked_course_title || null,
    task_type: item.task_type || item.category || 'topic_learning',
    learning_target: item.learning_target || null,
    kg_node_ids: Array.isArray(item.kg_node_ids) ? item.kg_node_ids : [],
  };
  if (item.learning_scope) {
    payload.learning_scope = item.learning_scope;
  }
  if (item.reason) {
    payload.reason = item.reason;
  }
  return payload;
}

test('payload includes learning_scope when recommendation has it (draft adoption)', function () {
  var scope = {
    book_title: '口腔颌面外科学',
    document_id: 'doc-omfs',
    chapter_key: 'chapter:omfs-5',
    chapter_title: '第五章 颌骨骨折',
    kg_node_ids: ['kg-010', 'kg-011'],
  };
  var recs = mapDraftItemsToRecommendations('draft_payload_scope', [{
    learning_target: '复习颌骨骨折',
    task_type: 'review_chapter',
    reason: '用户指定第五章',
    estimated_minutes: 45,
    evidence_source: 'user_request + knowledge_graph',
    learning_scope: scope,
  }]);
  var payload = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');

  ok(payload.learning_scope !== undefined, 'learning_scope must be in payload');
  equal(payload.learning_scope.book_title, '口腔颌面外科学');
  equal(payload.learning_scope.document_id, 'doc-omfs');
  equal(payload.learning_scope.chapter_key, 'chapter:omfs-5');
  deepEqual(payload.learning_scope.kg_node_ids, ['kg-010', 'kg-011']);
});

test('payload includes reason when recommendation has it', function () {
  var recs = mapDraftItemsToRecommendations('draft_payload_reason', [{
    learning_target: '学习牙周病学',
    task_type: 'learn_chapter',
    reason: '基于知识图谱推荐第三章学习',
    estimated_minutes: 30,
    learning_scope: { book_title: '牙周病学', kg_node_ids: ['kg-1'] },
  }]);
  var payload = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');

  ok(payload.reason !== undefined, 'reason must be in payload');
  equal(payload.reason, '基于知识图谱推荐第三章学习');
});

test('payload always includes task_type', function () {
  var recs = mapDraftItemsToRecommendations('draft_payload_task_type', [{
    learning_target: '任务',
    task_type: 'review_chapter',
    reason: 'r',
    estimated_minutes: 20,
    learning_scope: {},
  }]);
  var payload = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');

  equal(payload.task_type, 'review_chapter');
});

test('payload always includes learning_target', function () {
  var recs = mapDraftItemsToRecommendations('draft_payload_lt', [{
    learning_target: '牙周病学的发病机制（1/3）',
    task_type: 'learn_chapter',
    reason: 'r',
    estimated_minutes: 30,
    learning_scope: { book_title: '牙周病学' },
  }]);
  var payload = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');

  equal(payload.learning_target, '牙周病学的发病机制（1/3）');
});

test('payload does NOT include learning_scope key when absent (system recommendation compat)', function () {
  // System recommendations (no draft) don't have learning_scope.
  // The payload must NOT gain a learning_scope key in that case.
  var systemRec = {
    recommendation_id: 'sys:rec:001',
    title: '系统推荐任务',
    task_type: 'topic_learning',
    category: 'recommend',
    reason: '基于学习历史推荐',
    estimated_minutes: 25,
    kg_node_ids: [],
    linked_course_title: null,
    linked_course_id: null,
    learning_target: '系统推荐学习',
    // No learning_scope field
  };
  var payload = buildRecommendationPayload(systemRec, 'u1', '2026-07-15');

  ok(!('learning_scope' in payload), 'learning_scope must NOT be in payload when absent');
});

test('payload does NOT include reason key when absent', function () {
  var noReasonRec = {
    recommendation_id: 'sys:rec:002',
    title: '无理由推荐',
    task_type: 'topic_learning',
    category: 'recommend',
    estimated_minutes: 15,
    kg_node_ids: [],
    linked_course_title: null,
    linked_course_id: null,
    learning_target: '学习任务',
    // No reason field
  };
  var payload = buildRecommendationPayload(noReasonRec, 'u1', '2026-07-15');

  ok(!('reason' in payload), 'reason must NOT be in payload when absent');
});

test('payload with learning_scope=null does NOT include learning_scope key', function () {
  var nullScopeRec = {
    recommendation_id: 'draft:nullscope',
    title: '任务',
    task_type: 'topic_learning',
    reason: 'r',
    estimated_minutes: 10,
    kg_node_ids: [],
    linked_course_title: null,
    linked_course_id: null,
    learning_target: '学习',
    learning_scope: null,
  };
  var payload = buildRecommendationPayload(nullScopeRec, 'u1', '2026-07-15');

  ok(!('learning_scope' in payload), 'null learning_scope must NOT add the key');
});

test('payload with reason=empty-string does NOT include reason key', function () {
  var emptyReasonRec = {
    recommendation_id: 'draft:emptyreason',
    title: '任务',
    task_type: 'topic_learning',
    reason: '',
    estimated_minutes: 10,
    kg_node_ids: [],
    linked_course_title: null,
    linked_course_id: null,
    learning_target: '学习',
    learning_scope: {},
  };
  var payload = buildRecommendationPayload(emptyReasonRec, 'u1', '2026-07-15');

  ok(!('reason' in payload), 'empty-string reason must NOT add the key');
});

test('full draft→recommendation→payload chain preserves all four fields', function () {
  var scope = {
    book_title: '牙周病学',
    document_id: 'doc-perio',
    chapter_key: 'chapter:perio-ch3',
    chapter_title: '第三章 牙周病的发病机制',
    kg_node_ids: ['core:aaa111', 'core:bbb222'],
  };
  var items = [{
    learning_target: '牙周病的发病机制（1/3）',
    task_type: 'learn_chapter',
    reason: '学习《牙周病学》牙周病的发病机制',
    estimated_minutes: 30,
    evidence_source: 'kg_chapter',
    learning_scope: scope,
  }];
  var recs = mapDraftItemsToRecommendations('draft_full_chain', items);
  var payload = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');

  // All four required fields must be present
  ok(payload.learning_scope !== undefined, 'learning_scope in payload');
  deepEqual(payload.learning_scope, scope);
  equal(payload.task_type, 'learn_chapter');
  equal(payload.learning_target, '牙周病的发病机制（1/3）');
  equal(payload.reason, '学习《牙周病学》牙周病的发病机制');

  // Existing fields must not be lost
  equal(payload.title, '牙周病的发病机制（1/3）');
  equal(payload.user_id, 'u1');
  equal(payload.plan_date, '2026-07-15');
  deepEqual(payload.kg_node_ids, ['core:aaa111', 'core:bbb222']);
  equal(payload.linked_course_title, '牙周病学');
  equal(payload.linked_course_id, 'doc-perio');
});

test('accept all reuse: multiple items all get correct payloads', function () {
  var items = [
    { learning_target: '任务A', task_type: 'review', reason: '理由A', estimated_minutes: 20, learning_scope: { book_title: '书A' } },
    { learning_target: '任务B', task_type: 'practice', reason: '理由B', estimated_minutes: 30, learning_scope: { book_title: '书B' } },
  ];
  var recs = mapDraftItemsToRecommendations('draft_accept_all', items);
  var payloadA = buildRecommendationPayload(recs[0], 'u1', '2026-07-15');
  var payloadB = buildRecommendationPayload(recs[1], 'u1', '2026-07-15');

  equal(payloadA.learning_scope.book_title, '书A');
  equal(payloadA.task_type, 'review');
  equal(payloadA.learning_target, '任务A');
  equal(payloadA.reason, '理由A');

  equal(payloadB.learning_scope.book_title, '书B');
  equal(payloadB.task_type, 'practice');
  equal(payloadB.learning_target, '任务B');
  equal(payloadB.reason, '理由B');
});

test('existing fields are not lost when learning_scope and reason are added', function () {
  var scope = { book_title: '测试书', kg_node_ids: ['kg-x'] };
  var recs = mapDraftItemsToRecommendations('draft_no_loss', [{
    learning_target: '测试任务',
    task_type: 'review_chapter',
    reason: '测试理由',
    estimated_minutes: 40,
    evidence_source: 'test_source',
    learning_scope: scope,
  }]);
  var payload = buildRecommendationPayload(recs[0], 'demo_user', '2026-07-20');

  // Verify new fields are present
  ok(payload.learning_scope !== undefined);
  equal(payload.reason, '测试理由');

  // Verify no existing fields were lost
  equal(payload.user_id, 'demo_user');
  equal(payload.plan_date, '2026-07-20');
  equal(payload.title, '测试任务');
  equal(payload.notes, '测试理由');
  equal(payload.task_type, 'review_chapter');
  equal(payload.learning_target, '测试任务');
  deepEqual(payload.kg_node_ids, ['kg-x']);
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
