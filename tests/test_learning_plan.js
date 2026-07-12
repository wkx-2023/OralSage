// tests/test_learning_plan.js
// Static Node tests for learning-plan.js "开始学习" entry point.
// No external dependencies. No real network calls.
// Validates: open → has link with real encoded ID, completed → no entry,
// URL does not contain task text/title/KG fields.

'use strict';

var assert = require('assert');

// ============================================================================
// Test helpers — mirror the core logic from learning-plan.js
// ============================================================================

/**
 * Replicates buildStartLearningUrl() from learning-plan.js.
 * Mirrors: frontend/js/learning-plan.js
 */
function buildStartLearningUrl(itemId) {
  return '/pages/master-chat.html?plan_item_id=' + encodeURIComponent(itemId);
}

/**
 * Replicates the startLearningLink logic from renderTodoItem().
 * Only returns a link when item.status !== 'completed'.
 */
function renderStartLearningLink(item) {
  var completed = item.status === 'completed';
  if (completed) return '';
  return '<a class="todo-tool start-learning-link" href="' +
    buildStartLearningUrl(item.id) +
    '" title="开始学习" aria-label="开始学习"><i data-lucide="play-circle"></i></a>';
}

// ============================================================================
// URL construction tests
// ============================================================================

assert.strictEqual(
  buildStartLearningUrl('test-item-123'),
  '/pages/master-chat.html?plan_item_id=test-item-123',
  'Plain ASCII ID should produce clean URL'
);

assert.strictEqual(
  buildStartLearningUrl('plan-abc-def-456'),
  '/pages/master-chat.html?plan_item_id=plan-abc-def-456',
  'Hyphenated ID should produce clean URL'
);

// ============================================================================
// Encoding tests — special characters
// ============================================================================

(function testEncodingSpecialChars() {
  var url = buildStartLearningUrl('item with spaces & special = chars');
  var paramValue = url.split('plan_item_id=')[1];

  // Spaces should be encoded as %20
  assert.ok(paramValue.indexOf('%20') >= 0, 'Spaces should be percent-encoded');

  // & should be encoded as %26
  assert.ok(paramValue.indexOf('%26') >= 0, 'Ampersand should be percent-encoded');

  // = should be encoded as %3D
  assert.ok(paramValue.indexOf('%3D') >= 0, 'Equals should be percent-encoded');

  // Raw special chars should NOT appear
  assert.strictEqual(paramValue.indexOf(' & '), -1, 'Raw ampersand should not appear');
  assert.strictEqual(paramValue.indexOf(' = '), -1, 'Raw equals should not appear');
})();

// ============================================================================
// Encoding tests — Chinese characters
// ============================================================================

(function testEncodingChinese() {
  var id = '计划任务-口腔溃疡-001';
  var url = buildStartLearningUrl(id);
  var paramValue = url.split('plan_item_id=')[1];

  // Chinese characters should be percent-encoded
  assert.ok(paramValue.indexOf('%') >= 0, 'Chinese characters should be percent-encoded');

  // Encoded string should be longer than raw (each CJK char → 9 bytes in UTF-8 percent encoding)
  assert.ok(paramValue.length > id.length, 'Encoded value should be longer than raw ID');

  // URL should start with the correct path
  assert.ok(url.indexOf('/pages/master-chat.html?plan_item_id=') === 0, 'URL should have correct base path');
})();

// ============================================================================
// URL must NOT leak item fields
// ============================================================================

(function testUrlDoesNotLeakFields() {
  var ids = [
    'any-id-1',
    'item-abc-123',
    'test_with_underscores',
    'mixedCaseId-456',
    '中文ID-测试',
  ];

  ids.forEach(function (id) {
    var url = buildStartLearningUrl(id);

    // No title field in URL
    assert.strictEqual(url.indexOf('title='), -1, 'URL should not contain title= for ID: ' + id);

    // No course fields
    assert.strictEqual(url.indexOf('course'), -1, 'URL should not contain course for ID: ' + id);
    assert.strictEqual(url.indexOf('linked_course'), -1, 'URL should not contain linked_course for ID: ' + id);

    // No KG fields
    assert.strictEqual(url.indexOf('kg_node'), -1, 'URL should not contain kg_node for ID: ' + id);

    // No status field
    assert.strictEqual(url.indexOf('status='), -1, 'URL should not contain status= for ID: ' + id);

    // No estimated_minutes
    assert.strictEqual(url.indexOf('estimated_minutes'), -1, 'URL should not contain estimated_minutes for ID: ' + id);

    // No deferred_count
    assert.strictEqual(url.indexOf('deferred_count'), -1, 'URL should not contain deferred_count for ID: ' + id);

    // Only one query parameter
    var queryString = url.split('?')[1] || '';
    var params = queryString.split('&');
    assert.strictEqual(params.length, 1, 'URL should have exactly one query parameter for ID: ' + id);
    assert.ok(params[0].indexOf('plan_item_id=') === 0, 'The only parameter should be plan_item_id for ID: ' + id);
  });
})();

// ============================================================================
// Render logic — open items get link
// ============================================================================

(function testOpenItemHasLink() {
  var item = {
    id: 'plan-abc-123',
    title: '学习口腔溃疡诊断',
    status: 'open',
    linked_course_title: '口腔医学基础',
    kg_node_ids: ['kg-001', 'kg-002'],
  };

  var link = renderStartLearningLink(item);

  // Link should not be empty
  assert.ok(link.length > 0, 'Open item should have start learning link');

  // Link should contain the correct plan_item_id
  assert.ok(link.indexOf('plan_item_id=plan-abc-123') >= 0, 'Link should contain the item ID');

  // Link href should NOT contain the title in raw form
  assert.strictEqual(link.indexOf('口腔溃疡'), -1, 'Link should not contain raw task title');

  // Link href should NOT contain the course title
  assert.strictEqual(link.indexOf('口腔医学基础'), -1, 'Link should not contain raw course title');

  // Link href should NOT contain KG node IDs
  assert.strictEqual(link.indexOf('kg-001'), -1, 'Link should not contain kg node ID');
  assert.strictEqual(link.indexOf('kg-002'), -1, 'Link should not contain kg node ID');

  // Link href should NOT contain kg_node_ids as a field name
  assert.strictEqual(link.indexOf('kg_node'), -1, 'Link should not contain kg_node field name');

  // Link should be an anchor tag
  assert.ok(link.indexOf('<a ') === 0 || link.indexOf('<a\n') === 0, 'Should be an anchor tag');

  // Link should have the todo-tool class
  assert.ok(link.indexOf('todo-tool') >= 0, 'Should have todo-tool class');

  // Link should NOT be inside the URL itself (href attribute only)
  var hrefStart = link.indexOf('href="') + 6;
  var hrefEnd = link.indexOf('"', hrefStart);
  var hrefValue = link.substring(hrefStart, hrefEnd);

  // The href should only contain plan_item_id
  assert.ok(hrefValue.indexOf('plan_item_id=') >= 0, 'href should contain plan_item_id');
  assert.strictEqual(hrefValue.indexOf('&title='), -1, 'href should not have title param');
  assert.strictEqual(hrefValue.indexOf('&course'), -1, 'href should not have course param');
})();

// ============================================================================
// Render logic — completed items get NO link
// ============================================================================

(function testCompletedItemHasNoLink() {
  var item = {
    id: 'plan-xyz-789',
    title: '已完成的学习计划',
    status: 'completed',
    linked_course_title: '测试课程',
    kg_node_ids: ['kg-003'],
  };

  var link = renderStartLearningLink(item);

  // Link should be empty string
  assert.strictEqual(link, '', 'Completed item should NOT have start learning link');
})();

// ============================================================================
// Render logic — various non-completed statuses
// ============================================================================

(function testNonCompletedStatuses() {
  var statuses = ['open', 'in_progress', 'pending', 'active', ''];

  statuses.forEach(function (status) {
    var item = { id: 'item-' + status, title: '任务', status: status };
    var link = renderStartLearningLink(item);

    if (status === 'completed') {
      assert.strictEqual(link, '', 'completed status should have no link');
    } else {
      assert.ok(link.length > 0, 'Non-completed status "' + status + '" should have start learning link');
      assert.ok(link.indexOf('plan_item_id=item-' + status) >= 0,
        'Link should contain correct encoded ID for status "' + status + '"');
    }
  });
})();

// ============================================================================
// Render logic — link exists regardless of editable flag
// ============================================================================

(function testLinkIndependentOfEditable() {
  // The start learning link depends only on status, not on editable
  var openItem = { id: 'open-past', title: '过去的开放任务', status: 'open' };
  var link = renderStartLearningLink(openItem);

  // Even past (non-editable) items get the link if still open
  assert.ok(link.length > 0, 'Non-editable open items should still have start learning link');
  assert.ok(link.indexOf('plan_item_id=open-past') >= 0, 'Link should contain correct ID');
})();

// ============================================================================
// Edge cases
// ============================================================================

(function testEmptyId() {
  var item = { id: '', title: '空ID任务', status: 'open' };
  var link = renderStartLearningLink(item);

  // Should still produce a link, just with empty plan_item_id
  assert.ok(link.indexOf('plan_item_id=') >= 0, 'Even empty ID should produce plan_item_id param');
})();

(function testNullFields() {
  var item = {
    id: 'safe-id',
    title: '安全任务',
    status: 'open',
    linked_course_title: null,
    kg_node_ids: null,
  };

  var link = renderStartLearningLink(item);

  assert.ok(link.indexOf('plan_item_id=safe-id') >= 0, 'Should encode safe ID');
  assert.strictEqual(link.indexOf('null'), -1, 'Should not contain null literal');
  assert.strictEqual(link.indexOf('undefined'), -1, 'Should not contain undefined literal');
})();

(function testVariousIdFormats() {
  var ids = [
    'simple',
    'with-hyphens',
    'with_underscores',
    'camelCase123',
    'UPPERCASE',
    'mixed.Case-123_test',
    '中文ID',
    '日本語ID',
    '한국어ID',
  ];

  ids.forEach(function (id) {
    var item = { id: id, title: '测试', status: 'open' };
    var link = renderStartLearningLink(item);

    assert.ok(link.indexOf(encodeURIComponent(id)) >= 0,
      'Link should contain encoded ID for: ' + id);
  });
})();

(function testUrlHasOnlyOneQuestionMark() {
  var item = { id: 'single-qmark', title: '测试', status: 'open' };
  var link = renderStartLearningLink(item);

  // Extract href value from the link
  var hrefMatch = link.match(/href="([^"]+)"/);
  assert.ok(hrefMatch, 'Should have href attribute');
  var href = hrefMatch[1];

  var questionMarkCount = (href.match(/\?/g) || []).length;
  assert.strictEqual(questionMarkCount, 1, 'URL should have exactly one question mark');
})();

console.log('All learning-plan tests passed.');
