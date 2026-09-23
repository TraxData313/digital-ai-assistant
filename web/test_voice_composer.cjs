const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const sent = [], errors = [], pending = [];
const context = vm.createContext({
  BOX: {value: ''}, waiting: [], resetBox: () => {},
  window: {AssistantVoice: {
    active: () => true,
    sendText: text => { sent.push(text); return new Promise((resolve,reject) => pending.push({resolve,reject})); },
    message: text => errors.push(text),
  }},
});
const at = source.indexOf('let voiceSending = false;');
vm.runInContext(source.slice(at, source.indexOf('// --- clicks and folds', at)), context);
(async () => {
  context.BOX.value = '  First message  ';
  const first = context.send();
  await context.send();
  assert.deepEqual(sent, ['First message'], 'a second click or Enter must not resend the same draft');
  context.BOX.value = 'My next message';
  pending.shift().resolve(); await first;
  assert.equal(context.BOX.value, 'My next message', 'an acknowledgement must preserve a newer draft');
  const second = context.send(); pending.shift().resolve(); await second;
  assert.deepEqual(sent, ['First message','My next message']);
  assert.equal(context.BOX.value, '', 'only the acknowledged draft is cleared');
  context.BOX.value = 'Keep this if sending fails';
  const failed = context.send(); pending.shift().reject(Error('Connection interrupted')); await failed;
  assert.equal(context.BOX.value, 'Keep this if sending fails');
  assert.deepEqual(errors, ['Connection interrupted']);
  const retry = context.send(); pending.shift().resolve(); await retry;
  assert.equal(sent.length, 4, 'failure releases the send guard for an explicit retry');
  context.BOX.value = '  '; await context.send(); assert.equal(sent.length, 4);
  context.waiting.push({name:'picture.png'});context.BOX.value='With a picture';
  await context.send();assert.equal(sent.length, 4);
  assert.equal(context.BOX.value, 'With a picture');
  assert.match(errors.at(-1), /End voice before sending a picture/);
  console.log('Voice composer preserves newer drafts, prevents duplicate sends, and permits retry after failure.');
})().catch(error => {console.error(error);process.exitCode=1;});
