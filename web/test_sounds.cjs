// A sound its voice makes where one is written -- "(laugh)" -- is drawn apart
// from the words, and nothing that merely looks like one is.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const context = vm.createContext({});
const at = source.indexOf('function esc(');
vm.runInContext(source.slice(at, source.indexOf('const MONTH_SAID', at)), context);
const md = context.md;
const mark = (s) => `<span class="sfx">${s}</span>`;

assert.equal(md('It kept going, (laugh) like it had not heard me.'),
  `It kept going, ${mark('(laugh)')} like it had not heard me.`);
for (const sound of ['(sigh)', '(cough)', '(clears throat)', '(Laugh)'])
  assert.ok(md(sound + ' Right.').startsWith(mark(sound)), sound);
// Escaped before it is marked, so a sound can never smuggle markup in.
assert.equal(md('<b>(sigh)</b>'), `&lt;b&gt;${mark('(sigh)')}&lt;/b&gt;`);
// A sentence about laughing is a sentence, and an aside is an aside.
for (const plain of ['I laugh at that.', '(see the laugh above)', '(laughing)', 'laugh)'])
  assert.ok(!md(plain).includes('sfx'), plain);
console.log('Sounds written into a reply are drawn apart, and nothing else is.');
