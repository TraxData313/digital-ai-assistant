const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');

const context = vm.createContext({});
let at = source.indexOf('function nativeWorkingLabel(');
vm.runInContext(source.slice(at, source.indexOf('function plainWhat(', at)), context);
const step = (command) => [{kind: 'native', detail: {type: 'commandExecution', commandActions: [{command}]}}];
assert.equal(context.nativeWorkingLabel(step('cmd.exe /d /c echo hello')), 'using CMD…');
assert.equal(context.nativeWorkingLabel(step('powershell.exe -Command Get-Content x')), 'using PowerShell…');
assert.equal(context.nativeWorkingLabel(step('rg -n phrase .')), 'searching…');
assert.equal(context.nativeWorkingLabel(step('curl.exe https://example.com')), 'using the web…');
assert.equal(context.nativeWorkingLabel([{kind: 'native', detail: {type: 'fileChange'}}]), 'editing a file…');
assert.equal(context.nativeWorkingLabel([]), 'getting tools ready…');
assert.match(source, /reach: "checking memory…"/);
assert.match(source, /if \(w && w\.mode === "thinking"\) return "thinking…";/);

const scrollContext = vm.createContext({MAIN: {scrollHeight: 1000, scrollTop: 350, clientHeight: 600}});
at = source.indexOf('function keepScroll(');
vm.runInContext(source.slice(at, source.indexOf('function renderChat(', at)), scrollContext);
scrollContext.keepScroll(() => { scrollContext.MAIN.scrollHeight = 1400; });
assert.equal(scrollContext.MAIN.scrollTop, 350, 'poll must preserve a reader 50px above the bottom');
scrollContext.MAIN = {scrollHeight: 1000, scrollTop: 397, clientHeight: 600};
scrollContext.keepScroll(() => { scrollContext.MAIN.scrollHeight = 1400; });
assert.equal(scrollContext.MAIN.scrollTop, 1400, 'a view genuinely at the bottom may follow new activity');

assert.match(source, /if \(PLAIN\) return msgHtml\(r, turn, false\);/);
assert.doesNotMatch(source, /if \(PLAIN\) return msgHtml\(r, turn, false\) \+/);
assert.doesNotMatch(source, /if \(shown === "chat"\) MAIN\.scrollTop = MAIN\.scrollHeight;\n    renderUsage\(\);/);
console.log('Quiet workings checks passed: compact native status, hidden logs, and reader-controlled scrolling.');
