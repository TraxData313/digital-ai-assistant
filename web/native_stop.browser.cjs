// Synthetic activity only: no room connection, model call, or real cancellation.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const extract = (start, end) => {
  const at = source.indexOf(start), to = source.indexOf(end, at);
  assert.ok(at >= 0 && to > at, start);
  return source.slice(at, to);
};
// Use the actual Settings row markup, including its classes and button order.
const rowExpression = source.match(/patchHtml\(view, (`<p class="workings-row">`[\s\S]*?`[^`]*<\/p>`)/)[1];
const row = vm.runInNewContext(rowExpression);
assert.match(extract('function renderDev()', 'async function takeBackup()'), /applyView\(\);\s+renderNativeStop\(\);/);
assert.doesNotMatch(extract('function renderLive()', '// --- the header'), /html \+=.*native-stop-live/);

(async () => {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 850}});
    const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8')
      .replace(/<script[\s\S]*?<\/script>/g, '')
      .replace(/<link[^>]*>/g, '')
      .replace('<section id="view-dev" class="view hidden"></section>', `<section id="view-dev" class="view">${row}</section>`)
      .replace('<section id="view-chat" class="view"></section>', '<section id="view-chat" class="view hidden"><div id="live"></div></section>');
    await page.setContent(html);
    await page.addStyleTag({content: fs.readFileSync(`${__dirname}/style.css`, 'utf8')});
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      let progress = null, failed = false, state = {}, PLAIN = true, turnStart = Date.now();
      const keepScroll = f => f(), patchHtml = (el, html) => {el.innerHTML = html;};
      const compactWorkings = () => [], plainWhat = () => 'using tools';
      const esc = s => String(s), md = esc, clockOnly = () => '';
      window.posts = []; window.errors = []; window.rejectCancel = false;
      async function post(url, body) {
        window.posts.push({url, body});
        if (window.rejectCancel) throw new Error('Synthetic cancellation failure');
      }
      const alert = message => window.errors.push(message);
      ${extract('async function cancelNativeTools()', 'function wireProviders()')}
      ${extract('function renderNativeStop()', '// --- the header')}
      $('workings').textContent = 'hide workings';
      $('gauges').textContent = 'hide gauges';
      $('tab-chat').classList.remove('active');
      $('tab-dev').classList.add('active');
      window.setProgress = value => {progress = value; renderLive();};
    `});
    const stop = page.getByRole('button', {name: 'Stop tools', exact: true});
    for (const state of [null, {}, {busy: false, native_active: true}, {busy: true, native_active: false}, {busy: false, error: 'failed'}]) {
      await page.evaluate(value => setProgress(value), state);
      assert.equal(await stop.isVisible(), false, `hidden for ${JSON.stringify(state)}`);
    }
    await page.evaluate(() => setProgress({busy: true, native_active: true, steps: []}));
    assert.equal(await stop.isVisible(), true);
    assert.equal(await stop.isEnabled(), true);
    assert.equal(await page.locator('#live #native-stop-live').count(), 0);
    assert.deepEqual(await page.locator('.workings-row button').evaluateAll(buttons => buttons.map(b => b.id)), ['workings', 'gauges', 'native-stop-live']);
    await stop.focus();
    assert.equal(await page.evaluate(() => {
      const original = $('native-stop-live');
      for (let i = 0; i < 5; i++) renderLive();
      return original === $('native-stop-live') && document.activeElement === original;
    }), true, 'polling preserves the button and keyboard focus');
    await page.keyboard.press('Enter');
    assert.deepEqual(await page.evaluate(() => window.posts), [{url: 'api/native-tools/cancel', body: {}}]);
    await page.evaluate(() => {window.rejectCancel = true;});
    await stop.click();
    assert.deepEqual(await page.evaluate(() => window.errors), ['Synthetic cancellation failure']);
    for (const detailed of [false, true]) {
      await page.evaluate(value => {PLAIN = !value; document.body.className = value ? 'detailed gauges' : 'plain'; renderLive();}, detailed);
      for (const width of [1440, 1100, 700, 390, 320]) {
        await page.setViewportSize({width, height: 850});
        const fits = await page.locator('.workings-row button').evaluateAll(buttons => {
          const rects = buttons.map(b => b.getBoundingClientRect());
          return rects.every((r, i) => r.width > 0 && r.left >= 0 && r.right <= innerWidth &&
            rects.slice(i + 1).every(s => r.right <= s.left || s.right <= r.left || r.bottom <= s.top || s.bottom <= r.top));
        });
        assert.equal(fits, true, `visible controls do not overlap or overflow at ${width}px, detailed=${detailed}`);
        assert.equal(await stop.isVisible(), true);
      }
    }
    if (process.env.ASSISTANT_TEST_SCREENSHOT) await page.screenshot({path: process.env.ASSISTANT_TEST_SCREENSHOT});
    // Turn completion and absent live markup must still clear the upper control.
    await page.evaluate(() => {$('live').remove(); setProgress({busy: false, native_active: true});});
    assert.equal(await stop.isVisible(), false);
    await page.evaluate(() => setProgress({busy: true, native_active: true}));
    assert.equal(await stop.isVisible(), true);
    console.log('Stop tools browser checks passed: Settings placement, activity visibility, enabled state, cancellation/error handling, stable focus, both views and 320–1440px wrapping.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
