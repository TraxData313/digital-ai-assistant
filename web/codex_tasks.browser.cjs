// Headless acceptance against the real page, with synthetic APIs only.
const {chromium} = require('playwright-core');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 850}});
    await page.clock.install();
    const errors = [], posts = [];
    page.on('pageerror', error => errors.push(error.message));
    let data = {controller: 'control', enabled: true, project_mode: 'local', audit: [],
      tasks: {'local:a': {thread:'a', host:'local', title:'Check the garden layout', supervised:true}},
      desktop:{threads:[{kind:'codex',id:'a',hostId:'local',title:'Check the garden layout',status:'idle'},
        {kind:'codex',id:'b',hostId:'local',title:'Write the launch notes',status:'active'}]}};
    let failCodex = false;
    // The room fills the page's tokens when it serves it; so does this.
    const identity = {name: 'Ada', app_name: "Ada's Room", slug: 'ada', self: 'assistant',
      owner: 'sam', people: {sam: {called: 'Sam', short: 'Sammy'}}};
    const filled = text => text.replace('{{identity_json}}', JSON.stringify(identity))
      .replace('{{people_options}}', '<option value="sam">Sam</option>')
      .replace('{{person_hidden}}', ' hidden')
      .replace(/\{\{(app_name|title|name)\}\}/g, 'Ada');
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        let result = {};
        if (url.pathname === '/api/codex') {
          if (failCodex) return route.fulfill({status:503, json:{error:'Desktop unavailable'}});
          if (route.request().method() === 'POST') {
            const body = route.request().postDataJSON(); posts.push(body);
            if (body.action === 'release') data.tasks['local:a'].supervised = false;
            if (body.action === 'pause') data.enabled = false;
            if (body.action === 'resume') data.enabled = true;
          }
          result = data;
        } else if (url.pathname === '/api/state') result = {rows:[], events:[], prompt:{self:{}}};
        else if (url.pathname === '/api/progress') result = {busy:false, out:[]};
        else if (url.pathname === '/api/projects') result = {projects:[]};
        else if (url.pathname === '/api/providers') result = {models:[]};
        return route.fulfill({json: result});
      }
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (['index.html','style.css','app.js','codex_tasks.js'].includes(name)) {
        const raw = fs.readFileSync(path.join(__dirname, name));
        return route.fulfill({body: name === 'index.html' ? filled(String(raw)) : raw,
          contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html'});
      }
      return route.fulfill({status:404, body:''});
    });
    await page.goto('http://room.test/');
    const indicator = page.locator('#codex-sessions');
    await page.waitForFunction(() => document.getElementById('codex-sessions').textContent === 'Write the launch notes');
    assert.equal(await page.locator('header #tab-codex').count(), 0);
    // The shortcut works even before Settings has ever rendered its button.
    await indicator.click();
    await page.getByRole('button', {name:'Pause Ada', exact:true}).waitFor();
    assert.equal(await page.locator('#tab-dev').evaluate(el => el.classList.contains('active')), true);
    await page.getByRole('button', {name:'Settings', exact:true}).click();
    const section = page.locator('.dev[data-k="dev:codex"]');
    await section.locator('summary').click();
    await section.getByRole('button', {name:'Codex tasks', exact:true}).click();
    await page.getByRole('button', {name:'Take over — release Ada'}).click();
    await page.getByRole('button', {name:'Pause Ada', exact:true}).click();
    await page.getByRole('button', {name:'Resume Ada', exact:true}).waitFor();
    assert.deepEqual(posts.map(p => p.action), ['release', 'pause']);
    assert.equal(await page.getByRole('button', {name:'Hand to Ada', exact:true}).count(), 2);
    assert.equal(await page.locator('#view-codex a').first().getAttribute('href'), 'codex://threads/a');
    data.desktop.pinnedThreads = [{...data.desktop.threads[1]}];
    data.desktop.threads[0].status = {type:'active'};
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => document.getElementById('codex-sessions').textContent === '2 sessions running');
    data.desktop = {threads:[]};
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => document.getElementById('codex-sessions').textContent === 'none');
    failCodex = true;
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => document.getElementById('codex-sessions').textContent === 'sessions unavailable');
    failCodex = false;
    const longTitle = '<img src=x onerror=alert(1)> ' + 'A very long session title '.repeat(30);
    data.desktop = {threads:[{kind:'codex',id:'long',title:longTitle,status:'active'}]};
    await page.clock.fastForward(15000);
    await page.waitForFunction(title => document.getElementById('codex-sessions').textContent === title, longTitle);
    assert.equal(await indicator.locator('img').count(), 0);
    await indicator.click();
    for (const detailed of [false, true]) {
      await page.evaluate(value => {document.body.className = value ? 'detailed gauges' : 'plain';}, detailed);
      for (const width of [1440,1100,700,390,320]) {
        await page.setViewportSize({width,height:850});
        assert.equal(await indicator.evaluate(el => {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.left >= 0 && rect.right <= innerWidth && el.scrollWidth > el.clientWidth;
        }), true, `long title clips inside header at ${width}px`);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.getByRole('button', {name:'Settings', exact:true}).click();
        await section.getByRole('button', {name:'Codex tasks', exact:true}).waitFor();
        await section.getByRole('button', {name:'Codex tasks', exact:true}).click();
      }
    }
    // Polling must not reset connection text or move keyboard focus.
    data = {controller:null};
    await page.getByRole('button', {name:'Refresh tasks', exact:true}).click();
    const input = page.getByPlaceholder('Existing Codex task ID');
    await input.fill('unfinished-controller');
    await page.clock.fastForward(15000);
    assert.equal(await input.inputValue(), 'unfinished-controller');
    assert.equal(await input.evaluate(el => el === document.activeElement), true);
    if (process.env.ASSISTANT_TEST_SCREENSHOT) {
      await page.setViewportSize({width:1100,height:850});
      await page.getByRole('button', {name:'Settings', exact:true}).click();
      await page.screenshot({path:process.env.ASSISTANT_TEST_SCREENSHOT});
    }
    assert.deepEqual(errors, []);
    console.log('Browser task panel passed: Settings navigation, task controls, live zero/one/many status, deduplication, disconnect/recovery, inert long titles, form preservation, 320–1440px layouts.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
