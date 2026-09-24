const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const start = source.indexOf('// --- its notebook');
const end = source.indexOf('function renderDev()', start);
assert.ok(start >= 0 && end > start, 'the notebook Settings helpers are present');
// The redraw every few seconds must never pass the notebook by: a skipped
// section is a table that stops answering the room.
const loop = source.slice(end, source.indexOf('\nasync function takeBackup', end));
assert.doesNotMatch(loop, /key === "dev:notebook"[^\n]*continue/, 'Settings never skips redrawing the notebook');

const book = (cap, notes) => ({
  cap, min_cap: 100, max_cap: 100000, grace_pct: 5, locked: false,
  used: notes.filter(n => !n.gone).reduce((s, n) => s + n.tokens, 0), pct: 0,
  count: notes.filter(n => !n.gone).length, removed: notes.filter(n => n.gone).length, notes,
});
const note = (id, text, extra = {}) => ({
  id, text, dt: '2026-09-24T08:45:00+00:00', tokens: Math.round(text.length / 4),
  up: 0, down: 0, turns: 0, gone: false, gone_dt: null, ...extra,
});

// One fake box for the cap, one for its button, and a stand-in for the section.
const box = {value: '5000', dataset: {saved: '5000'}, classList: {toggle() {}}, blur() {}};
const button = {hidden: true};
const posted = [];
let answer = null;
const context = vm.createContext({
  state: {who: 'sam', notebook: book(5000, [])},
  OWNER: 'sam', PEOPLE: {sam: 'Sam', lee: 'Leona'}, MY_NAME: 'Ada',
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  shortWhen: () => '24 Sep 10:45',
  dev: (key, title, sub, inner) => inner,
  keepScroll: fn => fn(), patchHtml: () => {},
  document: {getSelection: () => ''},
  setTimeout: () => 0, Date,
  $: id => ({'nb-cap': box, 'nb-cap-save': button,
             'view-dev': {querySelector: () => ({querySelectorAll: () => []})}})[id] || null,
  post: async (url, body) => {
    posted.push([url, body]);
    if (answer instanceof Error) throw answer;
    return answer;
  },
});
vm.runInContext(source.slice(start, end), context);

// Empty, then the note arrives: the table answers the room.
let html = context.devNotebook();
assert.match(html, /No notes yet/, 'an empty book says so');
assert.match(html, /value="5000"/, 'the cap box shows the cap the room uses');
assert.match(html, /id="nb-cap-save"[^>]* hidden/, 'with nothing typed there is no save to press');

// The regression: a number typed and not saved must not freeze the table.
box.value = '1000';
context.state.notebook = book(5000, [note(1, 'The first thing worth keeping in sight this morning, and why.')]);
html = context.devNotebook();
assert.match(html, /The first thing worth keeping/, 'a new note shows even while the cap box holds a draft');
assert.match(html, /value="1000"/, 'the typed number survives the redraw');
assert.match(html, /class="draft"/, 'and wears the unsaved look');
assert.doesNotMatch(html, /id="nb-cap-save"[^>]* hidden/, 'and its save button is showing');
assert.match(html, /~15<\/b> of 5,000 tokens/, 'while the meter still tells the truth about the cap in force');

(async () => {
  // Enter saves it, once; the box's own change after that does not send it again.
  answer = book(1000, context.state.notebook.notes);
  context.wireNotebook();
  box.onkeydown({key: 'Enter', preventDefault() {}});
  await new Promise(r => setImmediate(r));
  await box.onchange();
  assert.equal(JSON.stringify(posted), JSON.stringify([['api/notebook', {cap: '1000'}]]),
               'one save, with the typed number');
  assert.equal(context.state.notebook.cap, 1000, 'the state takes the room\'s answer');
  assert.equal(box.dataset.saved, '1000', 'and the box knows it is saved');
  html = context.devNotebook();
  assert.match(html, /Saved — the cap is now 1,000 tokens\./, 'saying so where the rule usually stands');
  assert.match(html, /id="nb-cap-save"[^>]* hidden/, 'with the button put away again');

  // A refusal stays said, and the same wrong number is not sent twice.
  box.value = '50';
  answer = new Error('the cap is between 100 and 100,000 tokens');
  box.onkeydown({key: 'Enter', preventDefault() {}});
  await new Promise(r => setImmediate(r));
  await box.onchange();
  assert.equal(posted.length, 2, 'a refused number is tried once, not again on the way out of the box');
  html = context.devNotebook();
  assert.match(html, /class="nb-rule bad"[^>]*>the cap is between 100 and 100,000 tokens/, 'the refusal is said in words');

  // Esc puts it back and the refusal goes with it.
  box.onkeydown({key: 'Escape', preventDefault() {}});
  html = context.devNotebook();
  assert.match(html, /value="1000"/, 'Esc returns the saved cap');
  assert.doesNotMatch(html, /nb-rule bad/, 'and the refusal is gone with the draft');

  // Somebody who is not the owner reads it and cannot move it.
  context.state.who = 'lee';
  html = context.devNotebook();
  assert.match(html, /id="nb-cap"[^>]* disabled/, 'the cap box is shut to anyone but the owner');
  assert.doesNotMatch(html, /nb-cap-save/, 'and has no save at all');
  context.state.who = 'sam';

  // Sorted by votes, most first; removed notes only on the switch.
  context.state.notebook = book(1000, [
    note(1, 'one', {up: 1}), note(2, 'two', {up: 3}), note(3, 'gone', {gone: true, gone_dt: '2026-09-24T09:00:00+00:00'}),
  ]);
  vm.runInContext('NB_SORT = {key: "up", dir: -1}', context);
  html = context.devNotebook();
  assert.ok(html.indexOf('>two<') < html.indexOf('>one<'), 'most voted first when sorted by votes');
  assert.doesNotMatch(html, />gone</, 'a removed note stays out of the table by default');
  assert.match(html, /show removed \(1\)/, 'with a switch that counts it');
  vm.runInContext('NB_GONE = true', context);
  assert.match(context.devNotebook(), /nb-tag">removed<\/span>gone/, 'and shows it, marked, when asked');

  console.log('Notebook Settings keeps a typed cap without freezing the table, saves it once, refuses in words, and sorts and filters the notes.');
})().catch(err => { console.error(err); process.exit(1); });
