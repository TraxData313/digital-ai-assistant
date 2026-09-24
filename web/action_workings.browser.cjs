// Local Chromium regression with synthetic activity; no room login or model calls.
// Requires playwright-core (available in the bundled Codex Node runtime).
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const extract = (start, end) => {
  const at = source.indexOf(start);
  assert.ok(at >= 0, start);
  const to = source.indexOf(end, at);
  assert.ok(to > at, end);
  return source.slice(at, to);
};
(async () => {
  const browser = await chromium.launch({channel:'chrome', headless:true, ignoreDefaultArgs:['--hide-scrollbars']});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:800}});
    await page.setContent(`<style>
      #main {height:600px;overflow:auto;width:800px}
      .act-body, pre {white-space:pre;overflow:auto;max-height:120px;width:700px}
      summary {height:24px} .hidden {display:none}
      ::-webkit-scrollbar {width:16px;height:16px}
      ::-webkit-scrollbar-thumb {background:#888;min-height:18px}
      ::-webkit-scrollbar-track {background:#ddd}
    </style><main id="main"><div style="height:800px">older messages</div><div id="live"></div></main>`);
    await page.addScriptTag({content:`
      const MAIN = document.getElementById('main'), OPEN = new Set();
      const $ = id => document.getElementById(id);
      const esc = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
      const md = esc, clockOnly = () => '', tidySummary = s => s;
      const LIVE_LABEL = {}, PLAIN_WHAT = {};
      let shown='chat', PLAIN=false, progress={busy:true,turn:123,steps:[],native_active:true};
      let failed=false, state={}, turnStart=Date.now();
      const cancelNativeTools = () => {};
      MAIN.addEventListener('toggle', e => {
        if (!e.target.dataset.k) return;
        if(e.target.open) OPEN.add(e.target.dataset.k); else OPEN.delete(e.target.dataset.k);
      }, true);
      ${extract('function nativeText(', 'function eventDetail(')}
      ${extract('function actHtml(', 'function stepsHtml(')}
      ${extract('function patchHtml(', 'function renderChat(')}
      ${extract('function liveStep(', '// What the assistant is doing,')}
      ${extract('function nativeWorkingLabel(', '// --- the header')}
    `});
    const check = await page.evaluate(async () => {
      const native = detail => ({kind:'native', text:'Native output', detail});
      const output = Array.from({length:150}, (_,i)=>`line ${i} ` + 'x'.repeat(200) + '\n').join('');
      progress.steps = [native({request_row:123}),
        native({id:'a',type:'commandExecution',command:'Get-ChildItem',cwd:'C:/Users/you',status:'inProgress'}),
        ...Array.from({length:100}, (_,i)=>native({itemId:'a',delta:`row ${i}\n`})),
        native({id:'a',type:'commandExecution',status:'completed',exitCode:0,aggregatedOutput:output}),
        native({id:'b',type:'commandExecution',command:'cmd /c exit 7',status:'failed',exitCode:7,aggregatedOutput:'expected failure'})];
      renderLive();
      const count = document.querySelectorAll('#live > details').length;
      const action = document.querySelector('#live > details');
      action.open = true;
      await new Promise(resolve => setTimeout(resolve, 0));
      let pane = action.querySelector('.act-body');
      pane.scrollTop = 360; pane.scrollLeft = 170;
      MAIN.scrollTop = 300;
      for (let i=0;i<8;i++) renderLive();
      pane = document.querySelector('#live > details .act-body');
      const chat = [MAIN.scrollTop,pane.scrollTop,pane.scrollLeft,document.querySelector('#live > details').open];
      const link = document.querySelector('#live a').getAttribute('href');
      const anchorY=document.querySelector('#live > details').getBoundingClientRect().top;
      keepScroll(()=>MAIN.firstElementChild.style.height='900px');
      const anchored=document.querySelector('#live > details').getBoundingClientRect().top === anchorY;
      PLAIN=true; renderLive();
      const hidden = document.querySelectorAll('#live details').length;
      // Exercise panel replacement exactly as Settings refreshes it. Its
      // scroll should stay put even when Settings happens to be at the end.
      shown='dev';
      const panel = `<details data-k="dev:last" open><summary>Last turn</summary><pre>${output}</pre></details>`;
      MAIN.innerHTML=`<div style="height:800px"></div><div id="view-dev">${panel}</div>`;
      MAIN.scrollTop=MAIN.scrollHeight;
      let pre = document.querySelector('pre');
      pre.scrollTop=420; pre.scrollLeft=190;
      const top = MAIN.scrollTop;
      for(let i=0;i<8;i++) keepScroll(()=>{
        patchHtml(document.querySelector('[data-k="dev:last"]'),panel,true);
        if(i===0) document.querySelector('#view-dev').insertAdjacentHTML('beforeend','<div style="height:200px">new status</div>');
      });
      pre=document.querySelector('pre');
      const settings=[MAIN.scrollTop,pre.scrollTop,pre.scrollLeft];
      // Normal bottom-following still works in chat.
      shown='chat'; MAIN.scrollTop=MAIN.scrollHeight;
      keepScroll(()=>MAIN.insertAdjacentHTML('beforeend','<div style="height:100px"></div>'));
      const follows=MAIN.scrollHeight-MAIN.scrollTop-MAIN.clientHeight;
      return {count,chat,link,hidden,settings,top,follows,anchored};
    });
    assert.equal(check.count,2,'100 fragments and setup produce only two top-level actions');
    assert.deepEqual(check.chat,[300,360,170,true],'live polling retains main/internal scroll and open state');
    assert.equal(check.link,'api/native-log?row=123');
    assert.equal(check.anchored,true,'content growth above the reader retains the visible action');
    assert.equal(check.hidden,0,'simple view still hides all native details');
    assert.deepEqual(check.settings,[check.top,420,190],'Settings replacement retains both scroll axes without following');
    assert.equal(check.follows,0,'chat at its end still follows new output');
    const rect = await page.evaluate(() => {
      MAIN.innerHTML='<div id="live"></div>'; MAIN.scrollTop=0;
      PLAIN=false; shown='chat';
      OPEN.add('native:row:777:drag');
      progress.steps=[{kind:'native',detail:{request_row:777}}, {kind:'native',detail:{
        id:'drag',type:'commandExecution',command:'Get-Content fixture.txt',status:'inProgress',
        aggregatedOutput:Array.from({length:100},(_,i)=>`line ${i} `+'x'.repeat(200)+'\n').join('')
      }}];
      renderLive();
      window.testPane=document.querySelector('#live > details .act-body');
      const r=testPane.getBoundingClientRect();
      return {x:r.x,y:r.y,width:r.width,height:r.height};
    });
    // Hold the actual Chromium scrollbar thumb while the real renderer ticks.
    await page.mouse.move(rect.x+rect.width-8,rect.y+9);
    await page.mouse.down();
    await page.mouse.move(rect.x+rect.width-8,rect.y+35);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const firstDrag = await page.evaluate(() => testPane.scrollTop);
    await page.evaluate(() => {for(let i=0;i<4;i++) renderLive();});
    await page.mouse.move(rect.x+rect.width-8,rect.y+65);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const lastDrag = await page.evaluate(() => ({top:testPane.scrollTop,same:testPane===document.querySelector('#live > details .act-body')}));
    await page.mouse.up();
    assert.ok(firstDrag>0 && lastDrag.top>firstDrag,'vertical scrollbar keeps dragging through ticks: '+JSON.stringify({rect,firstDrag,lastDrag}));
    assert.equal(lastDrag.same,true,'scrolling element is never replaced');
    await page.mouse.move(rect.x+12,rect.y+rect.height-8);
    await page.mouse.down();
    await page.mouse.move(rect.x+60,rect.y+rect.height-8);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const firstSide = await page.evaluate(() => testPane.scrollLeft);
    await page.evaluate(() => {for(let i=0;i<4;i++) renderLive();});
    await page.mouse.move(rect.x+110,rect.y+rect.height-8);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const lastSide = await page.evaluate(() => testPane.scrollLeft);
    await page.mouse.up();
    assert.ok(firstSide>0 && lastSide>firstSide,'horizontal scrollbar keeps dragging through ticks');
    const selections = await page.evaluate(async () => {
      const textNode=testPane.firstChild;
      const at=textNode.data.indexOf('line 40');
      const selection=document.getSelection();
      selection.setBaseAndExtent(textNode,at,textNode,at+7);
      const left=testPane.scrollLeft;
      for(let i=0;i<8;i++) {
        progress.steps[1].detail.status='failed';
        progress.steps[1].detail.exitCode=7;
        progress.steps[1].detail.aggregatedOutput+=`new output ${i}\n`;
        renderLive();
        await new Promise(resolve=>setTimeout(resolve,10));
      }
      const live={text:selection.toString(),same:testPane.firstChild===textNode,left:testPane.scrollLeft,previousLeft:left};
      selection.removeAllRanges();
      shown='dev';
      MAIN.innerHTML='<details data-k="dev:last" open><summary>Settings</summary><pre>selected settings passage\n'+'wide '.repeat(300)+'\nline\n'.repeat(200)+'</pre></details>';
      const panel=MAIN.firstChild, pre=panel.querySelector('pre'), text=pre.firstChild;
      pre.scrollTop=150; pre.scrollLeft=200;
      selection.setBaseAndExtent(text,0,text,25);
      const wanted=selection.toString();
      for(let i=0;i<8;i++) keepScroll(()=>patchHtml(panel,
        '<details data-k="dev:last" open><summary>Settings tick '+i+'</summary><pre>'+text.data+'new result\n</pre></details>',true));
      return {live,settings:{text:selection.toString(),wanted,same:pre===panel.querySelector('pre'),top:pre.scrollTop,left:pre.scrollLeft}};
    });
    assert.equal(selections.live.text,'line 40','selection survives status changes and appended output');
    assert.equal(selections.live.same,true,'output text node stays alive');
    assert.equal(selections.live.left,selections.live.previousLeft,'output changes do not move horizontal scroll');
    assert.equal(selections.settings.text,selections.settings.wanted,'Settings selection survives ticks');
    assert.equal(selections.settings.same,true);
    assert.equal(selections.settings.top,150);
    assert.equal(selections.settings.left,200);
    const forms = await page.evaluate(() => {
      document.getSelection().removeAllRanges();
      const html='<div id="settings-test"><input id="field" value="saved"><input id="flag" type="checkbox"><textarea id="note">saved note</textarea><select id="choice"><option value="a" selected>A</option><option value="b">B</option></select></div>';
      patchHtml(MAIN,html);
      $('field').value='draft'; $('flag').checked=true; $('note').value='draft'; $('choice').value='b';
      patchHtml(MAIN,html);
      const reset=[$('field').value,$('flag').checked,$('note').value,$('choice').value];
      $('field').focus(); $('field').value='still typing';
      patchHtml(MAIN,html);
      return {reset,focused:$('field').value};
    });
    assert.deepEqual(forms.reset,['saved',false,'saved note','a'],'settings controls still reflect requested saved values');
    assert.equal(forms.focused,'still typing','focused text input is not overwritten');
    console.log('Chromium passed: grouped actions, live/Settings selection during changed output, real vertical and horizontal scrollbar drags across ticks, retained nodes/folds/scroll, simple view and bottom-follow.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
