// The Sessions tab: every Codex task and Claude Code session on this machine,
// which of them the assistant follows, and what it has done with them. The
// sessions themselves live in Codex and in Claude Code; this page only shows
// them and hands them over. Session text goes in through textContent only.
(() => {
  const N = (window.ASSISTANT && window.ASSISTANT.name) || 'the assistant';
  const view = document.getElementById('view-sessions');
  const indicator = document.getElementById('sessions-indicator');
  const opened = new Set();      // cards whose conversation is showing
  const talk = {};               // session id -> its fetched messages
  let data = null, reading = null, visible = false, timer = null;

  // -- reading ----------------------------------------------------------------
  function load() {
    if (!reading) reading = getJson('api/sessions').then(got => {
      data = got;
      paintIndicator();
      return got;
    }).finally(() => { reading = null; });
    return reading;
  }

  const WORKING = ['active', 'running', 'inProgress', 'working'];
  function codexStatus(task) {
    const raw = task.status, type = typeof raw === 'string' ? raw : raw?.type;
    const flags = (raw && raw.activeFlags) || [];
    if (flags.some(f => f === 'waitingOnApproval' || f === 'waitingOnUserInput')) return 'waiting';
    if (WORKING.includes(type)) return 'working';
    if (type === 'systemError') return 'ended';
    return 'idle';
  }

  // Both kinds as one list of cards, newest first.
  function cards() {
    const out = [];
    const claude = (data && data.claude) || {};
    for (const s of claude.sessions || []) out.push({
      kind: 'claude', id: s.session, short: s.short, title: s.title || 'Untitled session',
      cwd: s.cwd, status: s.status, following: s.following, by: s.started_by,
      model: s.model, effort: s.effort, mode: s.permission_mode, detail: s.detail,
      last: s.last, when: s.last ? s.last.at : s.started, interactive: s.kind === 'interactive'});
    const codex = (data && data.codex) || {};
    const tracked = codex.tasks || {};
    const seen = new Set();
    const threads = [...(codex.desktop?.pinnedThreads || []), ...(codex.desktop?.threads || [])];
    for (const t of Object.values(tracked))
      if (!threads.some(r => r.id === t.thread)) threads.push({id: t.thread, hostId: t.host, title: t.title, kind: 'codex', status: 'unknown'});
    for (const t of threads) {
      const host = t.hostId || 'local', key = host + ':' + t.id;
      if (t.kind !== 'codex' || seen.has(key)) continue;
      seen.add(key);
      const mine = tracked[key];
      out.push({kind: 'codex', id: t.id, host, title: t.title || 'Untitled task', cwd: t.cwd,
        status: codexStatus(t), following: !!(mine && mine.supervised),
        last: t.summary ? {role: 'summary', text: t.summary} : null, when: t.updatedAt});
    }
    return out.sort((a, b) => stamp(b.when) - stamp(a.when));
  }

  function stamp(v) {
    if (v == null) return 0;
    if (typeof v === 'number') return v < 1e12 ? v * 1000 : v;
    const t = Date.parse(v);
    return isNaN(t) ? 0 : t;
  }
  function ago(v) {
    const t = stamp(v);
    if (!t) return '';
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function paintIndicator() {
    const all = data ? cards() : [];
    const working = all.filter(c => c.status === 'working');
    const waiting = all.filter(c => c.status === 'waiting');
    let label = !data ? 'sessions unavailable' : !working.length ? 'none'
      : working.length === 1 ? working[0].title : working.length + ' sessions working';
    if (waiting.length) label = waiting.length + ' waiting for you · ' + label;
    if (indicator.textContent !== label) indicator.textContent = label;
    indicator.classList.toggle('running', working.length > 0 || waiting.length > 0);
    indicator.title = [...waiting, ...working].map(c => c.title).join('\n') || 'Sessions — open the Sessions tab';
    indicator.setAttribute('aria-label', 'Sessions: ' + label + '. Open the Sessions tab');
  }

  // -- drawing ----------------------------------------------------------------
  function el(tag, cls, text, parent) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null && text !== '') node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function act(label, parent, run, cls = 'small-btn') {
    const b = el('button', cls, label, parent);
    b.type = 'button';
    b.onclick = async () => {
      b.disabled = true;
      try { await run(); await refresh(); }
      catch (error) { alertLine(error.message); }
      finally { b.disabled = false; }
    };
    return b;
  }
  let lastAlert = '';
  function alertLine(text) { lastAlert = text || ''; draw(); }

  const STATUS = {working: 'working', idle: 'between turns', waiting: 'waiting for you',
                  ended: 'ended', done: 'done'};
  function short(path) {
    if (!path) return '';
    const parts = String(path).split(/[\\/]/).filter(Boolean);
    return parts.slice(-2).join('/');
  }

  function card(c, parent) {
    const box = el('article', 'sess ' + c.kind + ' ' + c.status + (c.following ? ' followed' : ''), '', parent);
    const head = el('div', 'sess-head', '', box);
    el('span', 'sess-kind', c.kind === 'claude' ? 'Claude Code' : 'Codex', head);
    el('b', 'sess-title', c.title, head);
    el('span', 'sess-status', STATUS[c.status] || c.status, head);
    const meta = [];
    meta.push(c.following ? (c.by ? N + ' started it · ' + N + ' follows it' : N + ' follows it')
                          : (c.by ? 'started by ' + c.by : 'yours'));
    if (c.model || c.effort) meta.push([c.model, c.effort].filter(Boolean).join(' · '));
    if (c.cwd) meta.push(short(c.cwd));
    if (c.when) meta.push(ago(c.when));
    el('div', 'sess-meta', meta.join('  ·  '), box);
    if (c.status === 'waiting' && c.detail) el('div', 'sess-detail', c.detail, box);
    if (c.last && c.last.text) {
      const line = el('div', 'sess-last', '', box);
      el('span', 'who', c.last.role === 'claude' ? 'Claude: ' : c.last.role === 'you' ? 'Said to it: ' : '', line);
      el('span', '', c.last.text, line);
    }
    const row = el('div', 'sess-actions', '', box);
    if (c.kind === 'claude') {
      const key = 'claude:' + c.id;
      act(opened.has(key) ? 'Hide conversation' : 'Read conversation', row, async () => {
        if (opened.has(key)) { opened.delete(key); return; }
        opened.add(key);
        await fetchTalk(c.id);
      });
      if (c.short && !['ended', 'done'].includes(c.status) && !c.interactive) {
        act('Open on this PC', row, () => post('api/claude', {action: 'open', short: c.short}));
        el('code', 'sess-cmd', 'claude attach ' + c.short, row);
      }
      if (c.following) {
        act('Take over', row, () => post('api/claude', {action: 'release', session: c.id}));
        if (c.short && !c.interactive && !['ended', 'done'].includes(c.status))
          act('Stop', row, () => post('api/claude', {action: 'stop', session: c.id}));
      } else if (data.claude.enabled && !['ended'].includes(c.status)) {
        act('Hand to ' + N, row, () => post('api/claude', {action: 'hand', session: c.id}));
      }
      if (opened.has(key)) conversation(c.id, box);
    } else {
      const link = el('a', 'small-btn', 'Open in Codex', row);
      link.href = 'codex://threads/' + encodeURIComponent(c.id);
      if (c.following)
        act('Take over', row, () => post('api/codex', {action: 'release', thread: c.id, host: c.host}));
      else if (data.codex.enabled && data.codex.controller)
        act('Hand to ' + N, row, () => post('api/codex', {action: 'hand', thread: c.id, host: c.host}));
    }
  }

  async function fetchTalk(id) {
    talk[id] = await post('api/claude', {action: 'read', session: id});
  }
  function conversation(id, parent) {
    const got = talk[id];
    const box = el('div', 'sess-talk', '', parent);
    if (!got) { el('p', 'quiet', 'reading…', box); return; }
    if (got.older != null) el('p', 'quiet', (got.messages_total - got.messages.length) + ' earlier messages are in the session itself.', box);
    for (const m of got.messages || []) {
      const line = el('div', 'msg ' + m.role, '', box);
      el('span', 'who', (m.role === 'claude' ? 'Claude' : 'Said to it') + (m.at ? ' · ' + ago(m.at) : ''), line);
      if (m.text) el('div', 'text', m.text, line);
      for (const t of m.tools || []) el('div', 'tool', t, line);
    }
  }

  function switches(parent) {
    const box = el('div', 'sess-switches', '', parent);
    const codex = data.codex || {}, claude = data.claude || {};
    const one = (label, sub, on, change) => {
      const row = el('label', 'sess-switch', '', box);
      const input = el('input', '', '', row);
      input.type = 'checkbox'; input.checked = !!on;
      input.onchange = async () => {
        input.disabled = true;
        try { await change(input.checked); await refresh(); }
        catch (error) { input.checked = !input.checked; alertLine(error.message); }
        finally { input.disabled = false; }
      };
      const words = el('span', '', '', row);
      el('b', '', label, words);
      el('span', 'quiet', sub, words);
    };
    one('Codex tasks', N + ' may start, follow and talk to Codex tasks. Off, the Codex tools and instructions are gone from ' + N + '’s turns.',
        codex.enabled && codex.controller, async on => {
          if (on && !codex.controller) { connecting = true; draw(); throw new Error(''); }
          await post('api/codex', {action: on ? 'resume' : 'pause'});
        });
    if (connecting || (codex.enabled && !codex.controller)) {
      const row = el('div', 'sess-connect', '', box);
      el('span', 'quiet', 'Codex needs one existing task of yours to work through. Paste its ID: ', row);
      const input = el('input', '', '', row);
      input.type = 'text'; input.placeholder = 'Codex task ID';
      act('Connect', row, async () => {
        await post('api/codex', {action: 'connect', controller: input.value.trim()});
        connecting = false;
      });
    }
    one('Claude Code sessions', N + ' may start, follow and talk to Claude Code sessions. Off, the Claude tools and instructions are gone from ' + N + '’s turns.',
        claude.enabled, on => post('api/claude', {action: on ? 'resume' : 'pause'}));
    if (claude.signed_in === false)
      el('p', 'sess-warn', 'Claude Code is not signed in outside the desktop app, so no Claude session can start. Run “claude auth login” in a terminal once.', box);
    if (claude.error) el('p', 'sess-warn', 'Claude Code: ' + claude.error, box);
    if (codex.desktop_error || codex.error) el('p', 'sess-warn', 'Codex: ' + (codex.desktop_error || codex.error), box);
  }
  let connecting = false;

  function activity(parent) {
    const items = [];
    for (const a of (data.claude && data.claude.audit) || []) items.push({at: a.at, kind: 'Claude Code',
      line: (a.op === 'start' ? 'started “' + (a.title || '') + '”' : 'wrote to “' + (a.title || '') + '”')
        + ([a.model, a.effort].filter(Boolean).length ? ' (' + [a.model, a.effort].filter(Boolean).join(', ') + ')' : '')
        + (a.status === 'failed' ? ' — it failed: ' + (a.error || '') : ''),
      body: a.prompt});
    for (const e of (data.claude && data.claude.handled) || []) items.push({at: e.handled_at || e.at, kind: 'Claude Code',
      line: 'was told: “' + ((e.detail && e.detail.title) || '') + '” ' + ({session_reply: 'finished a turn',
        session_waiting: 'is waiting for you', session_ended: 'ended', handed_to_assistant: 'was handed over'}[e.kind] || e.kind),
      body: e.detail && e.detail.reply});
    for (const a of (data.codex && data.codex.audit) || []) items.push({at: a.at, kind: 'Codex',
      line: (a.op === 'start' ? 'started a task' : a.op === 'send' ? 'wrote to a task' : a.op) + ' — ' + a.status,
      body: a.prompt});
    items.sort((a, b) => stamp(b.at) - stamp(a.at));
    const box = el('details', 'sess-activity', '', parent);
    box.open = activityOpen;
    box.ontoggle = () => { activityOpen = box.open; };
    el('summary', '', 'What ' + N + ' did lately', box);
    if (!items.length) el('p', 'quiet', 'Nothing yet.', box);
    for (const it of items.slice(0, 20)) {
      const row = el('details', 'sess-act', '', box);
      const sum = el('summary', '', '', row);
      el('span', 'when', ago(it.at), sum);
      el('span', 'sess-kind', it.kind, sum);
      el('span', '', it.line, sum);
      if (it.body) el('pre', '', it.body, row);
    }
  }
  let activityOpen = false;

  function settings(parent) {
    const box = el('details', 'sess-settings', '', parent);
    box.open = settingsOpen;
    box.ontoggle = () => { settingsOpen = box.open; };
    el('summary', '', 'New sessions', box);
    const claude = (data.claude && data.claude.settings) || {};
    const row = el('label', '', 'Claude Code starts in ', box);
    const mode = el('select', '', '', row);
    for (const [v, t] of [['bypassPermissions', 'no questions (as you)'], ['auto', 'auto mode'], ['acceptEdits', 'accept edits'], ['plan', 'plan mode'], ['manual', 'asking for each permission']]) {
      const o = el('option', '', t, mode); o.value = v;
    }
    mode.value = claude.permission_mode || 'bypassPermissions';
    mode.onchange = () => post('api/claude', {action: 'settings', permission_mode: mode.value}).catch(e => alertLine(e.message));
    const wt = el('label', '', '', box);
    const check = el('input', '', '', wt); check.type = 'checkbox'; check.checked = !!claude.worktree;
    el('span', '', ' in a git worktree of its own', wt);
    check.onchange = () => post('api/claude', {action: 'settings', worktree: check.checked}).catch(e => alertLine(e.message));
    const rc = el('label', '', '', box);
    const rcCheck = el('input', '', '', rc); rcCheck.type = 'checkbox'; rcCheck.checked = claude.remote_control !== false;
    el('span', '', ' with Remote Control, so it shows in your Claude apps and on claude.ai/code', rc);
    rcCheck.onchange = () => post('api/claude', {action: 'settings', remote_control: rcCheck.checked}).catch(e => alertLine(e.message));
    const cx = el('label', '', 'Codex project tasks use ', box);
    const pm = el('select', '', '', cx);
    for (const [v, t] of [['local', 'the saved project folder'], ['worktree', 'a separate working copy']]) {
      const o = el('option', '', t, pm); o.value = v;
    }
    pm.value = (data.codex && data.codex.project_mode) || 'worktree';
    pm.onchange = () => post('api/codex', {action: 'project_mode', mode: pm.value}).catch(e => alertLine(e.message));
  }
  let settingsOpen = false;

  function group(title, list, parent, empty) {
    if (!list.length && !empty) return;
    el('h3', 'sess-group', title, parent);
    if (!list.length) el('p', 'quiet', empty, parent);
    for (const c of list) card(c, parent);
  }

  function draw() {
    const focus = document.activeElement;
    if (focus && view.contains(focus) && (/^(SELECT|TEXTAREA)$/.test(focus.tagName)
        || (focus.tagName === 'INPUT' && focus.type === 'text'))) return;
    const scroll = view.parentNode ? view.parentNode.scrollTop : 0;
    view.replaceChildren();
    el('h2', '', 'Sessions', view);
    if (!data) { el('p', 'quiet', 'looking…', view); return; }
    if (lastAlert) el('p', 'sess-warn', lastAlert, view).setAttribute('role', 'alert');
    switches(view);
    const all = cards();
    const waiting = all.filter(c => c.status === 'waiting');
    const followed = all.filter(c => c.following && c.status !== 'waiting');
    const working = all.filter(c => !c.following && c.status === 'working');
    const rest = all.filter(c => !c.following && !['waiting', 'working'].includes(c.status));
    const sum = el('p', 'sess-summary', '', view);
    sum.textContent = N + ' follows ' + all.filter(c => c.following).length + ' · '
      + all.filter(c => c.status === 'working').length + ' working now · '
      + waiting.length + ' waiting for you';
    group('Waiting for you', waiting, view);
    group(N + ' is following', followed, view, 'Nothing right now.');
    group('Working now', working, view);
    const others = el('details', 'sess-others', '', view);
    others.open = othersOpen;
    others.ontoggle = () => { othersOpen = others.open; };
    el('summary', '', 'Everything else on this machine (' + rest.length + ')', others);
    for (const c of rest.slice(0, 40)) card(c, others);
    activity(view);
    settings(view);
    if (view.parentNode) view.parentNode.scrollTop = scroll;
  }
  let othersOpen = false;

  async function refresh() {
    try {
      await load();
      await Promise.all([...opened].map(key => fetchTalk(key.slice(7)).catch(() => {})));
    } catch (error) { data = null; lastAlert = error.message; }
    if (visible) draw();
  }

  window.showSessions = on => {
    visible = on;
    clearInterval(timer);
    if (on) { draw(); refresh(); timer = setInterval(refresh, 5000); }
  };
  indicator.onclick = () => show('sessions');
  load().catch(() => paintIndicator());
  setInterval(() => { if (!visible) load().catch(() => {}); }, 15000);
})();
