const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const context = vm.createContext({
  PLAIN: true, state: {rows: []}, whoNow: () => 'lee',
  ME: 'assistant', ME_INTERIM: 'assistant_interim', OWNER: 'sam',
  msgHtml: r => r.text, selfToAngelHtml: () => 'folded angel message',
});
for (const [start, end] of [
  ['function roomsOf(', 'function absorbState('],
  ['function rowHtml(', 'function prepare('],
  ['function liveStep(', '// What the assistant is doing'],
]) {
  const at = source.indexOf(start);
  vm.runInContext(source.slice(at, source.indexOf(end, at)), context);
}
const row = {id: 8, kind: 'assistant_interim', text: 'Checking actual memories.', meta: {room: 'lee'}};
const step = {kind: 'interim', detail: {row}};
assert.equal(context.liveStep(step), row.text, 'words remain while the next reply forms');
context.state.rows = [row];
assert.equal(context.liveStep(step), '', 'saved rows replace their live copies without duplication');
for (const plain of [true, false]) {
  context.PLAIN = plain;
  assert.equal(context.rowHtml(row, {}, {}, false), row.text, 'history retains interim words in both views');
}
context.PLAIN = true;
assert.equal(context.rowHtml({...row, meta: {to: 'angel'}}, {}, {}, false), '');
context.state.rows = [];
assert.equal(context.liveStep({...step, detail: {row: {...row, meta: {room: 'sam'}}}}), '');
console.log('Interim replies survive live-to-history transitions and preserve room visibility.');
