// Exercise the real renderers with fixtures, without a browser, server or network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const usage = {innerHTML: ''};
const context = vm.createContext({
  keepScroll: fn => fn(),
  patchHtml: (el, html) => { el.innerHTML = html; },
  state: {prompt: {self: {}, plan: {plan: 'Claude Max', limits: [{window: 'old-Claude'}]}}},
  progress: {provider: {codex_only: true, service: 'codex', label: 'Sol',
    service_label: 'Codex subscription', limits: [
      {window: 'codex_primary', label: '5-hour limit', used_fraction: .58},
      {window: 'codex_secondary', label: 'Weekly', used_fraction: .09}],
    credits: {unlimited: false, balance: '217.8642000000'}}},
  PROV: {codex_only: true, chosen: 'codex/sol', dream_model: 'claude_code/opus',
    dream_follows_chat: false, dream_paused: 'Paused', services: {codex: {label: 'Codex subscription'}},
    models: [{key: 'codex/sol', service: 'codex', label: 'Sol', ready: true, price: {}}],
    paused: [{capability: 'Dreams', reason: 'Saved night model requires Claude'}]},
  $: () => usage, esc: x => String(x ?? ''), k: String, roomy: String,
  money: String, priceLine: () => '', perTurn: () => '', provMoneyLine: () => '',
  winHtml: l => `${l.label}: ${Math.round(l.used_fraction * 100)}%`,
  facts: x => JSON.stringify(x), NO_NUMBER: 'Claude login not read',
});
for (const [start, end] of [
  ['function creditsHtml(', 'function renderUsage()'],
  ['function renderUsage()', '// --- the developer tab'],
  ['function devPlan()', '// The automatic memory'],
  ['function modelOptions(', 'function wireProviders()'],
]) {
  const at = source.indexOf(start);
  vm.runInContext(source.slice(at, source.indexOf(end, at)), context);
}
context.renderUsage();
assert.match(usage.innerHTML, /5-hour limit: 58%/);
assert.match(usage.innerHTML, /Weekly: 9%/);
assert.match(usage.innerHTML, /Credits/);
assert.match(usage.innerHTML, />217</);
assert.match(usage.innerHTML, /217\.8642000000 credits remaining, read live from Codex/);
assert.ok(usage.innerHTML.indexOf('Weekly') < usage.innerHTML.indexOf('Credits'));
assert.doesNotMatch(usage.innerHTML, /Claude Max|old-Claude|Claude login/);
context.state.prompt.self = {prompt_tokens_est: 23500, measured_input_tokens_last_turn: 229932, budget_target_tokens: 30000};
context.renderUsage();
assert.match(usage.innerHTML, /23500/);
assert.match(usage.innerHTML, /target 30000/);
assert.doesNotMatch(usage.innerHTML, /229932 of/);
const html = context.devProviders();
assert.match(html, /id="prov-codex-only" checked/);
assert.match(html, /Saved selection paused/);
assert.match(html, /paused — saved night model requires Claude/);
assert.doesNotMatch(html, /Claude Code has no bill/);
// The night's blank option names the chat it would follow, and is offered in
// Codex-only mode too -- following a chat already forced off Claude is safe.
assert.match(html, /the same model as the chat — Sol/);
assert.doesNotMatch(html, /always dreamt as|on its pin/);
assert.doesNotMatch(context.devPlan(), /Claude Max|old-Claude/);
const savedProgress = context.progress;
context.progress = null;
context.state.prompt.paused_capabilities = [];
context.state.prompt.self = {model: 'codex/sol', model_name: 'Sol'};
context.state.prompt.plan = {plan: 'ChatGPT subscription', limits: savedProgress.provider.limits};
context.renderUsage();
assert.match(usage.innerHTML, /5-hour limit: 58%/);
assert.doesNotMatch(usage.innerHTML, /Claude login/);
context.progress = savedProgress;
context.progress.provider = {codex_only: true, service: 'claude_code', label: 'Chat paused'};
context.renderUsage();
assert.doesNotMatch(usage.innerHTML, /old-Claude|Claude Max|Claude login|not read yet/);
context.PROV.codex_only = false;
context.PROV.dream_paused = null;
const legacy = context.devProviders();
assert.match(legacy, /Claude Code has no bill/);
assert.match(legacy, /the same model as the chat — Sol/);
assert.match(legacy, /set apart from the chat, on purpose/);
context.PROV.dream_follows_chat = true;
assert.match(context.devProviders(), /following the chat/);
assert.doesNotMatch(legacy, /id="prov-codex-only" checked/);
console.log('UI render checks passed: toggle, paused selection, Codex gauges, legacy mode.');
