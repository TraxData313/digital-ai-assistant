// An automatic-memory card opened while the turn is live stays open once the
// reply lands, and the next turn's card does not open by itself. Wants Node.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const context = vm.createContext({});
const at = source.indexOf('function recallKey(');
vm.runInContext(source.slice(at, source.indexOf('function actFromEvent(', at)), context);

const block = {asked: {essence: 'a walk by the river'}, line_ids: [41, 42], results: []};
const live = context.recallKey(block, 'live:0');
const saved = context.recallKey(dict(block, {after: {pulled: {}, picked: []}}), 'ev:90');
assert.equal(live, saved, 'the live card and the saved one share a key');
assert.ok(!live.startsWith('live:'), 'the key is not the card\'s place in the turn');

const next = context.recallKey({asked: {essence: 'a song about rain'}, line_ids: [43, 44]}, 'live:0');
assert.notEqual(next, live, 'the next turn\'s card in the same place has its own key');

assert.equal(context.recallKey({asked: {essence: ''}}, 'ev:1'), 'ev:1', 'a missed run keeps its own key');
assert.equal(context.recallKey(null, 'live:2'), 'live:2');

function dict(a, b) { return Object.assign({}, a, b); }
console.log('Memory cards keep their open state from live to saved, and the next turn starts closed.');
