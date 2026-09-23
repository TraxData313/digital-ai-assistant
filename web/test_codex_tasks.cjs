const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.attrs = {}; this.classList = {toggle() {}}; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attrs[key] = value; }
  set innerHTML(_) { throw new Error('Task text must never become HTML'); }
}
const view = new Element('section'), indicator = new Element('button');
const timers = [];
const nodes = node => [node, ...node.children.flatMap(nodes)];
const posts = [];
let state = {controller: 'controller', enabled: true, followup_limit: 12,
  tasks: {'local:task': {thread: 'task', host: 'local', supervised: true, followups: 2}},
  desktop: {pinnedThreads: [], threads: [{id: 'task', hostId: 'local', kind: 'codex',
    title: '<img src=x onerror=alert(1)>', status: 'idle'}]}, audit: []};
const context = vm.createContext({document: {
  createElement: tag => new Element(tag), getElementById: id => id === 'codex-sessions' ? indicator : view,
}, window: {ASSISTANT: {name: 'Ada'}}, getJson: async () => state,
  setInterval: (callback, ms) => timers.push({callback, ms}),
  post: async (url, body) => { posts.push({url, body}); }, show: () => {}});
vm.runInContext(fs.readFileSync(`${__dirname}/codex_tasks.js`, 'utf8'), context);
(async () => {
  await context.window.refreshCodexTasks();
  assert.equal(indicator.textContent, 'none');
  const task = state.desktop.threads[0];
  state.desktop = {pinnedThreads: [{...task, status: {type: 'active'}}],
    threads: [{...task, status: 'active'}, {...task, id: 'chat', kind: 'chatgpt', status: 'active'}]};
  await timers[0].callback();
  assert.equal(indicator.textContent, task.title, 'pinned duplicates and ChatGPT chats are not counted');
  assert.equal(timers[0].ms, 15000);
  state.desktop.threads.push({...task, hostId: 'remote', status: 'running'});
  await timers[0].callback();
  assert.equal(indicator.textContent, '2 sessions running', 'the same ID on different hosts is distinct');
  state.desktop = {threads: [task]};
  await timers[0].callback();
  assert.equal(indicator.textContent, 'none', 'completion clears live activity');
  assert.equal(nodes(view).find(n => n.tag === 'a').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(nodes(view).find(n => n.tag === 'a').href, 'codex://threads/task');
  await nodes(view).find(n => n.textContent === 'Take over — release Ada').onclick();
  assert.equal(posts[0].body.action, 'release');
  assert.equal(posts[0].body.thread, 'task');
  await nodes(view).find(n => n.textContent === 'Pause Ada').onclick();
  assert.equal(posts[1].body.action, 'pause');
  state = {...state, desktop: undefined, desktop_error: 'Desktop disconnected'};
  await context.window.refreshCodexTasks();
  assert.equal(indicator.textContent, 'sessions unavailable');
  assert.ok(nodes(view).some(n => n.textContent === 'Pause Ada'));
  assert.ok(nodes(view).some(n => n.textContent === 'Desktop disconnected' && n.attrs.role === 'alert'));
  state = {controller: null};
  await context.window.refreshCodexTasks();
  nodes(view).find(n => n.tag === 'input').value = 'visible-controller';
  await nodes(view).find(n => n.textContent === 'Connect').onclick();
  assert.equal(posts[2].body.controller, 'visible-controller');
  console.log('Codex task panel checks passed: connection, takeover, pause, disconnect controls, inert task text.');
})().catch(error => { console.error(error); process.exitCode = 1; });
