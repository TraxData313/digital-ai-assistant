// Real room renderers with synthetic native activity; no browser or server.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const context = vm.createContext({
  PLAIN: true, TO_ANGEL: new Set(), ME: 'assistant',
  esc: x => String(x ?? '').replaceAll('<', '&lt;'),
  ACT: {native: ['Native Codex activity', 'files']},
  LIVE_LABEL: {native: ['Native Codex activity', 'files']},
  tidySummary: String,
  eventDetail: e => JSON.stringify(e.detail), stepText: s => JSON.stringify(s.detail),
  actHtml: (key, cls, label, text, detail) => `${label} ${text} ${detail}`,
  msgHtml: () => 'fixture request',
});
function extract(start, end) {
  const at = source.indexOf(start);
  assert.ok(at >= 0);
  vm.runInContext(source.slice(at, source.indexOf(end, at)), context);
}
extract('function body(', 'function stepsHtml(');
extract('function nativeText(', 'function eventDetail(');
extract('function turnHtml(', '// A night\'s dream.');
extract('function liveStep(', '// What the assistant is doing,');
const detail = {command: 'powershell -NoProfile assert', cwd: 'C:/fixture', exitCode: 7,
  aggregatedOutput: 'EXPECTED_FAILURE <untrusted>', diff: '-value=1\n+value=2'};
const events = [{id: 1, kind: 'native', summary: 'command failed', detail}];
context.PLAIN = true;
assert.equal(context.turnHtml({id: 10, kind: 'user'}, events, false), 'fixture request');
context.PLAIN = false;
const html = context.turnHtml({id: 10, kind: 'user'}, events, false);
for (const text of ['powershell', 'C:/fixture', 'Exit code: 7', 'EXPECTED_FAILURE', 'value=2']) assert.ok(html.includes(text));
assert.ok(html.includes('&lt;untrusted>'));
assert.match(context.liveStep({kind: 'native', text: 'command failed', detail}, 'live:1', false), /EXPECTED_FAILURE/);
assert.doesNotMatch(source, /busy && latestCommand/);
console.log('Native renderer checks passed: plain hides activity; detailed keeps commands, cwd, output, exit, diffs and failures.');
