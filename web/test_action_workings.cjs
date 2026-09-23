const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const context = vm.createContext({});
const at = source.indexOf('function compactWorkings(');
vm.runInContext(source.slice(at, source.indexOf('function nativeActionHtml(', at)), context);
const native = (detail, summary = '') => ({kind: 'native', detail, summary});
const events = [native({request_row: 123}, 'Tools enabled'),
  native({command: 'cmd /c exit 0'}, 'Checking native command startup'),
  native({command: 'cmd /c exit 0', exitCode: 0}, 'Native command startup result'),
  {kind: 'recall', detail: {text: 'remembered'}, summary: 'Memory'},
  native({id: 'a', type: 'commandExecution', command: 'dir', cwd: 'C:/Users/you', status: 'inProgress'}),
  native({id: 'b', type: 'commandExecution', command: 'echo failed', status: 'inProgress'})];
for (let i = 0; i < 100; i++) events.push(native({itemId: 'a', delta: `file-${i}\n`}));
events.push(native({itemId: 'b', delta: 'failure output'}));
const original = JSON.stringify(events);
let result = context.compactWorkings(events, 'turn:1');
assert.equal(result.length, 3, 'two commands and shared recall, regardless of output chunk count');
assert.equal(JSON.stringify(events), original, 'audit events never mutated');
let actions = result.filter(e => e.kind === 'native');
assert.equal(actions[0].detail.aggregatedOutput, Array.from({length:100}, (_,i)=>`file-${i}\n`).join(''));
assert.equal(actions[1].detail.aggregatedOutput, 'failure output');
const stableKey = actions[0].detail._key;
events.push(native({id: 'a', type: 'commandExecution', status: 'completed', exitCode: 0, aggregatedOutput: 'complete output'}));
events.push(native({id: 'b', type: 'commandExecution', status: 'failed', exitCode: 7, stderr: 'stderr retained'}));
events.push(native({diff: '-one\n+two'}, 'Turn diff'));
events.push(native({status: 'failed', error: 'Malformed final JSON'}, 'Turn failed'));
result = context.compactWorkings(events, 'turn:1');
actions = result.filter(e => e.kind === 'native');
assert.equal(actions.length, 2);
assert.equal(actions[0].detail._key, stableKey);
assert.equal(actions[0].detail.command, 'dir');
assert.equal(actions[0].detail.aggregatedOutput, 'complete output', 'completion replaces chunks without doubling output');
assert.equal(actions[0].detail.request_row, 123);
assert.equal(actions[0].detail._runFailed, true);
assert.ok(actions[0].detail._notes.some(e => e.detail.diff));
assert.equal(actions[1].detail.exitCode, 7);
assert.equal(actions[1].detail.stderr, 'stderr retained');
assert.equal(actions[1].detail.aggregatedOutput, 'failure output');
const patch = context.compactWorkings([
  native({id:'edit', type:'fileChange', status:'inProgress'}),
  native({itemId:'edit', delta:'patch output'}),
  native({id:'edit', type:'fileChange', status:'completed', changes:[{path:'test.txt', diff:'-1\n+2'}]})]);
assert.equal(patch.length, 1);
assert.equal(patch[0].detail.changes[0].diff, '-1\n+2');
assert.equal(patch[0].detail.aggregatedOutput, 'patch output');
const stopped = context.compactWorkings([native({status:'cancelled', error:'stopped'})]);
assert.equal(stopped.length, 1);
assert.equal(stopped[0].detail._runFailed, true);
assert.equal(context.compactWorkings([native({itemId:'orphan', delta:'partial'})])[0].detail.aggregatedOutput, 'partial');
assert.equal(context.compactWorkings(events, 'history')[1].detail._key, stableKey, 'live and saved actions keep the same disclosure and scroll handle');
assert.notEqual(context.compactWorkings([native({request_row:124}), ...events.slice(1)], 'turn:2')[1].detail._key, stableKey, 'different turns have independent disclosure state');
console.log('Action grouping passed: interleaved commands, 100 chunks, edits, recall, failures, cancellation, malformed final, logs and immutable audit events.');

context.OPEN = new Set();
context.esc = s => String(s ?? '').replaceAll('<', '&lt;');
for (const [start, end] of [['function nativeText(', 'function eventDetail('], ['function actHtml(', 'function compactWorkings('], ['function nativeActionHtml(', 'function actFromEvent(']]) {
  const a = source.indexOf(start);
  vm.runInContext(source.slice(a, source.indexOf(end, a)), context);
}
const draw = events => context.compactWorkings(events, 'test').map(e => context.nativeActionHtml(e)).join('');
const routine = [native({request_row:1,status:'started'}),
  native({command:'cmd /c exit 0',exitCode:0},'Native command startup result'), native({status:'completed'})];
assert.equal(draw(routine),'','successful housekeeping has no chat entry');
assert.equal(draw(routine.slice(0,1)),'','startup does not flash a notes entry');
const actual = draw([...routine,native({id:'cmd',type:'commandExecution',command:'echo hello',status:'completed',exitCode:0,aggregatedOutput:'hello'})]);
assert.match(actual,/echo hello/);
assert.match(actual,/Download run log/);
assert.doesNotMatch(actual,/Run notes|startup result/);
for (const status of ['failed','interrupted','cancelling','unknown','denied','declined']) {
  const html=draw([...routine,native({status})]);
  assert.match(html,/Run notes/);
  assert.equal((html.match(/class="lbl">Run notes/g)||[]).length,1,'no nested notes-only heading');
}
assert.match(draw([...routine,native({id:'cmd',type:'commandExecution',command:'exit 7',exitCode:7})]),/Run notes/);
const diffOnly=draw([...routine,native({diff:'-old\n+new'})]);
assert.match(diffOnly,/Changes/);
assert.match(diffOnly,/\+new/);
assert.doesNotMatch(diffOnly,/Run notes/);
console.log('Quiet run notes passed: successful setup hidden, actions/diffs retained, failures and interruptions visible.');
