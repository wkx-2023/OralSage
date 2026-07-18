'use strict';

var assert = require('assert');
var fs = require('fs');
var path = require('path');
var vm = require('vm');

function loadInternals(fileName, key) {
  var sandbox = {
    window: {
      oralUi: {
        byId: function () { return null; },
        escapeHtml: function (value) {
          return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
        },
        apiPost: async function () { return {}; },
      },
    },
    document: {
      readyState: 'loading',
      addEventListener: function () {},
    },
    console: console,
  };
  var scriptPath = path.resolve(__dirname, '..', 'frontend', 'js', fileName);
  vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), sandbox, { filename: scriptPath });
  return sandbox.window.oralUi[key];
}

function verifyRenderer(fileName, key) {
  var api = loadInternals(fileName, key);
  assert.ok(api && typeof api.renderMarkdown === 'function');

  var loose = api.renderMarkdown('1. 第一项\n\n1. 第二项\n\n1. 第三项');
  assert.strictEqual((loose.match(/<ol>/g) || []).length, 1);
  assert.strictEqual((loose.match(/<li>/g) || []).length, 3);

  var mixed = api.renderMarkdown('1. 第一项\n- 补充\n2. 第二项');
  assert.ok(mixed.indexOf('<ol start="2">') !== -1);

  var sanitized = api.renderMarkdown('结论（chunk:test-1）[依据1]');
  assert.strictEqual(sanitized.indexOf('chunk:'), -1);
  assert.ok(sanitized.indexOf('[依据1]') !== -1);
}

verifyRenderer('pathology.js', 'pathologyInternals');
verifyRenderer('decision.js', 'decisionInternals');
console.log('PASS: pathology and decision markdown renderers preserve numbering and hide internal IDs');
