const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const start = source.indexOf("// The automatic memory's knobs:");
const end = source.indexOf('function devVoice()', start);
assert.ok(start >= 0 && end > start, 'the automatic memory Settings helpers are present');

// The room's side, as server/recall.py keeps it: each knob held to its
// bounds, a count to a whole number, and `reset` puts back the defaults.
const BOUNDS = {
  timeout_s: {min: 1, max: 60, default: 60, float: true},
  lines: {min: 1, max: 12, default: 5},
  review_lines: {min: 1, max: 12, default: 5},
  top: {min: 1, max: 10, default: 3},
  keyword_top: {min: 0, max: 5, default: 2},
  read_chars: {min: 200, max: 6000, default: 1500},
};
const DEFAULTS = Object.fromEntries(Object.entries(BOUNDS).map(([k, b]) => [k, b.default]));
let room = {...DEFAULTS};
const status = (extra = {}) => ({
  model: 'google/gemma-4-e4b', phase: 'loaded', models: [], detail: '', last: null,
  knobs: {...room},
  knob_bounds: Object.fromEntries(Object.entries(BOUNDS).map(([k, b]) => [k, {min: b.min, max: b.max, default: b.default}])),
  ...extra,
});

// One fake box per knob, standing in for the page's own.
const boxes = Object.keys(BOUNDS).map(knob => {
  const box = {
    value: String(DEFAULTS[knob]), dataset: {knob, saved: String(DEFAULTS[knob])},
    classes: new Set(['knob']),
    blur() { if (context.document.activeElement === box) context.document.activeElement = null; },
  };
  box.classList = {toggle: (name, on) => { if (on) box.classes.add(name); else box.classes.delete(name); }};
  return box;
});
const box = knob => boxes.find(b => b.dataset.knob === knob);
const buttons = {
  'recall-knobs-set': {hidden: true}, 'recall-knobs-reset': {},
  'recall-try': {disabled: false}, 'recall-try-note': {textContent: ''},
};

const posted = [];
let failNext = false;
const context = vm.createContext({
  progress: {recall: status()},
  MY_NAME: 'Ada',
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  recallLine: () => ({cls: 'on', text: 'automatic memory: loaded'}),
  gb: () => '', recallHtml: () => '', fold: () => '', facts: () => '',
  keepScroll: fn => fn(), patchHtml: () => {},
  renderRecall: () => {}, pollProgress: async () => {},
  alert: message => { throw new Error('alert: ' + message); },
  RECALL_MORE: new Set(),
  document: {activeElement: null},
  MAIN: {querySelectorAll: selector => selector === 'input.knob' ? boxes : []},
  // The live row is left out, so a repaint is this test's own `paint`.
  $: id => buttons[id] || null,
  setTimeout: () => 0, Date,
  post: async (url, body) => {
    posted.push([url, JSON.parse(JSON.stringify(body ?? null))]);
    if (failNext) { failNext = false; throw new Error('the room did not answer'); }
    if (url === 'api/recall/settings') {
      if (body.reset) room = {...DEFAULTS};
      for (const [k, v] of Object.entries(body)) {
        const b = BOUNDS[k];
        if (b) room[k] = Math.min(Math.max(b.float ? Number(v) : Math.trunc(Number(v)), b.min), b.max);
      }
      return status();
    }
    return {};
  },
});
vm.runInContext(source.slice(start, end), context);
const settled = () => vm.runInContext('KNOB_SAVE', context);

// A redraw of the section, done the way patchHtml does it: every attribute
// is taken from the new HTML, and the value too -- except in the box that
// has the focus.
const paint = () => {
  const html = context.devRecall();
  for (const b of boxes) {
    const m = html.match(new RegExp(`<input class="([^"]*)" data-knob="${b.dataset.knob}" type="text"[^>]*` +
                                    ` data-saved="([^"]*)" value="([^"]*)">`));
    assert.ok(m, `a text box for ${b.dataset.knob}`);
    b.classes = new Set(m[1].split(' '));
    b.dataset.saved = m[2];
    if (b !== context.document.activeElement) b.value = m[3];
  }
  return html;
};

// The room as it stands: the numbers it uses, nothing to save.
let html = paint();
assert.doesNotMatch(html, /type="number"/, 'no number boxes, so a mouse wheel cannot move a knob');
assert.equal(box('lines').value, '5', 'the writer box shows the number the room uses');
assert.match(html, /id="recall-knobs-set"[^>]* hidden/, 'with nothing typed there is no save to press');

// The regression: 3 typed into both line knobs and not saved, then a turn
// redraws the section. The boxes must still say 3, and say it is unsaved.
box('lines').value = '3';
box('review_lines').value = '3';
context.progress.recall = status({phase: 'working'});
html = paint();
assert.equal(box('lines').value, '3', 'a typed number survives the redraw a turn makes');
assert.equal(box('review_lines').value, '3', 'both of them');
assert.ok(box('lines').classes.has('draft'), 'and wears the unsaved look');
assert.doesNotMatch(html, /id="recall-knobs-set"[^>]* hidden/, 'with its save button showing');

(async () => {
  // A click anywhere else saves them -- both, in one go, since neither is
  // being typed in -- and the next run uses them.
  context.wireRecall();
  await box('lines').onchange();
  await box('review_lines').onchange();
  assert.deepEqual(posted, [['api/recall/settings', {lines: 3, review_lines: 3}]], 'one save, with both numbers');
  assert.equal(room.lines, 3, 'the room keeps the writer at 3');
  assert.equal(room.review_lines, 3, 'and the reviewer at 3');
  context.progress.recall = status({phase: 'loaded'});
  html = paint();
  assert.equal(box('lines').value, '3', 'after the turn the box still says 3');
  assert.ok(!box('lines').classes.has('draft'), 'no longer as a draft');
  assert.match(html, /class="knob-says good">saved</, 'and the row says it is saved');

  // Enter saves it, once; the box's own change on the way out does not send it again.
  posted.length = 0;
  box('top').value = '4';
  context.document.activeElement = box('top');
  box('top').onkeydown({key: 'Enter', preventDefault() {}});
  await settled();
  await box('top').onchange();
  assert.deepEqual(posted, [['api/recall/settings', {top: 4}]], 'Enter saves it, once');

  // The box somebody is still typing in is not saved from under them.
  posted.length = 0;
  box('read_chars').value = '2';                   // on its way to 2500
  context.document.activeElement = box('read_chars');
  box('timeout_s').value = '15';
  await box('timeout_s').onchange();
  assert.deepEqual(posted, [['api/recall/settings', {timeout_s: 15}]], 'only the box that was left is saved');
  assert.equal(box('read_chars').value, '2', 'the one being typed in is left as it is');
  box('read_chars').value = '2500';
  box('read_chars').blur();
  await box('read_chars').onchange();
  assert.deepEqual(posted[1], ['api/recall/settings', {read_chars: 2500}], 'and saved when it is left');

  // Past a bound: the room holds it there, and the row says so.
  box('lines').value = '50';
  await box('lines').onchange();
  assert.equal(box('lines').value, '12', 'the box shows what the room kept');
  html = paint();
  assert.match(html, /saved — writer reads, lines: 12, the most it takes/, 'and says why it is not 50');

  // Not a number: nothing is sent, the refusal is said in words, and Esc
  // puts the saved number back.
  posted.length = 0;
  box('top').value = 'abc';
  await box('top').onchange();
  assert.equal(posted.length, 0, 'a word is never sent as a knob');
  html = paint();
  assert.match(html, /class="knob-says bad">top by likeness: “abc” is not a number/, 'the refusal is said in words');
  assert.equal(box('top').value, 'abc', 'and what was typed stays to be fixed');
  box('top').onkeydown({key: 'Escape', preventDefault() {}});
  html = paint();
  assert.equal(box('top').value, '4', 'Esc puts the saved number back');
  assert.doesNotMatch(html, /knob-says/, 'and the refusal goes with it');

  // Emptied and left: back as it was, nothing sent.
  box('keyword_top').value = '';
  await box('keyword_top').onchange();
  assert.equal(posted.length, 0, 'an emptied box sends nothing');
  assert.equal(box('keyword_top').value, '2', 'and shows the saved number again');

  // A save that fails says so and keeps the number; the button tries again.
  failNext = true;
  box('keyword_top').value = '1';
  await box('keyword_top').onchange();
  html = paint();
  assert.match(html, /class="knob-says bad">the room did not answer/, 'a failed save is said');
  assert.equal(box('keyword_top').value, '1', 'the typed number is kept');
  assert.doesNotMatch(html, /id="recall-knobs-set"[^>]* hidden/, 'with the save button there to try again');
  await buttons['recall-knobs-set'].onclick();
  assert.equal(room.keyword_top, 1, 'which saves it');

  // "ask it now" runs with the number typed just before it.
  posted.length = 0;
  box('lines').value = '6';
  await buttons['recall-try'].onclick();
  assert.deepEqual(posted.map(p => p[0]), ['api/recall/settings', 'api/recall/try'],
                   'the knob is saved before the run is asked for');

  // Defaults: every knob back, a number still being fixed with them.
  box('top').value = 'x';
  await buttons['recall-knobs-reset'].onclick();
  assert.deepEqual(posted.at(-1), ['api/recall/settings', {reset: true}], 'defaults asks the room to reset');
  for (const b of boxes) assert.equal(b.value, String(DEFAULTS[b.dataset.knob]), `${b.dataset.knob} is back to its default`);
  html = paint();
  assert.match(html, /class="knob-says good">back to the defaults/, 'and the row says so');

  console.log('The automatic memory keeps a typed knob through a turn, saves it on Enter or a click away, once, and says what the room kept.');
})().catch(err => { console.error(err); process.exit(1); });
