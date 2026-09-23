const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context = vm.createContext({
  PLAIN: true, PEOPLE: {sam: 'Sam', lee: 'Leona'}, WHO: {user: 'Sam', assistant: 'Ada'},
  ME: 'assistant', ME_INTERIM: 'assistant_interim', MY_NAME: 'Ada', OWNER: 'sam',
  VIA_SAID: {}, OPEN: new Set(), TO_ANGEL: new Set(), esc, md: esc,
  when: () => '11:57', whoClass: () => '', picsHtml: () => '',
  compactWorkings: events => events, actFromEvent: event => `<aside>${esc(event.summary)}</aside>`,
});
for (const [start, end] of [
  ['function kindClass(', 'const WHO ='],
  ['function msgHtml(', '// Their line, on screen'],
  ['function turnHtml(', "// A night's dream."],
  ['function rowHtml(', 'function prepare('],
]) {
  const at = source.indexOf(start);
  assert.ok(at >= 0 && source.indexOf(end, at) > at);
  vm.runInContext(source.slice(at, source.indexOf(end, at)), context);
}
const text = 'Please handle my latest request. INTERNAL_CONFIG Recent transcript: REPEATED_WORDS';
const handoff = {id: 4431, kind: 'user', text, meta: {who: 'sam', room: 'sam', voice_task: 'session'}};
const original = JSON.stringify(handoff);
const events = [{kind: 'files', summary: 'The memory write failed.'}];
for (const plain of [true, false]) {
  context.PLAIN = plain;
  const html = context.rowHtml(handoff, {4431: events}, {}, false);
  assert.match(html, /Voice request/);
  assert.match(html, /Passed to Ada's tools/);
  assert.match(html, /data-k="row:4431"/);
  assert.match(html, /data-id="4431"/);
  assert.doesNotMatch(html, /Sam|INTERNAL_CONFIG|REPEATED_WORDS|class="msg user/);
  if (!plain) assert.match(html, /The memory write failed./, 'detailed activity still reports tool outcomes');
  const actual = {...handoff, meta: {who: 'sam', room: 'sam', voice_handled: true, voice: {selected: 'sol'}}};
  const userHtml = context.rowHtml(actual, {}, {}, false);
  assert.match(userHtml, /<b>Sam<\/b>/);
  assert.match(userHtml, /INTERNAL_CONFIG/, 'actual user words must not be hidden based on their text');
  assert.doesNotMatch(userHtml, /voice-handoff/);
  const reply = context.rowHtml({...handoff, kind: 'assistant', meta: {}, text: 'Saved your preference.'}, {}, {}, false);
  assert.match(reply, /Saved your preference./);
  assert.match(reply, /<b>Ada<\/b>/);
}
const backend = {id: 4432, kind: 'assistant', text: 'This is the full duplicate backend answer.',
  meta: {room: 'sam', voice_backend: {session: 'session', delivered: true}}};
for (const plain of [true, false]) {
  context.PLAIN = plain;
  const html = context.rowHtml(backend, {4432: events}, {}, false);
  assert.match(html, /Voice result/);
  assert.match(html, /Spoken through Ada's voice/);
  assert.doesNotMatch(html, /full duplicate backend answer|class="msg self/);
  if (!plain) assert.match(html, /The memory write failed./, 'backend tool activity remains available');
}
assert.equal(JSON.stringify(handoff), original, 'rendering must preserve stored request data');
assert.match(context.msgHtml(handoff, null, true), /voice-handoff grey/);
console.log('Voice handoffs and delivered backend copies remain compact while tool results and actual conversation stay intact.');
