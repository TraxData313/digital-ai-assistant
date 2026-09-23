// Ordinary Codex tasks remain the source of truth; this panel only hands them
// to the assistant or returns control to its owner. DOM textContent keeps task text inert.
(() => {
  const N = (window.ASSISTANT && window.ASSISTANT.name) || 'the assistant';
  const view = document.getElementById('view-codex');
  const indicator = document.getElementById('codex-sessions');
  let refreshing = false;
  let reading = null;
  // Share an in-flight read between the panel and the header. Polling only
  // updates the indicator, so it cannot replace a form somebody is editing.
  function readState() {
    if (!reading) reading = getJson('/api/codex').then(state => {
      renderSessions(state);
      return state;
    }, error => {
      renderSessions(null);
      throw error;
    }).finally(() => { reading = null; });
    return reading;
  }
  function renderSessions(state) {
    const unavailable = !state || state.desktop_error ||
      ((state.controller || state.error) && !state.desktop);
    const seen = new Set();
    const running = [...(state?.desktop?.pinnedThreads || []), ...(state?.desktop?.threads || [])]
      .filter(task => {
        const key = (task.hostId || 'local') + ':' + task.id;
        const status = typeof task.status === 'string' ? task.status : task.status?.type;
        if (task.kind !== 'codex' || seen.has(key) || !['active', 'running', 'inProgress'].includes(status)) return false;
        seen.add(key);
        return true;
      });
    const label = unavailable ? 'sessions unavailable' : !running.length ? 'none'
      : running.length === 1 ? running[0].title || running[0].name || 'Untitled session'
      : `${running.length} sessions running`;
    if (indicator.textContent !== label) indicator.textContent = label;
    indicator.classList.toggle('running', !unavailable && running.length > 0);
    indicator.title = unavailable ? 'Codex sessions unavailable — open task controls to reconnect'
      : running.length ? running.map(task => task.title || task.name || 'Untitled session').join('\n')
      : 'No Codex sessions running — open task controls';
    indicator.setAttribute('aria-label', `Codex sessions: ${label}. Open task controls`);
  }
  function element(tag, text, parent = view) {
    const node = document.createElement(tag);
    if (tag === 'button') node.className = 'small-btn';
    if (text) node.textContent = text;
    parent.appendChild(node);
    return node;
  }
  function button(label, body, parent) {
    const node = element('button', label, parent);
    node.type = 'button';
    node.onclick = async () => {
      node.disabled = true;
      try { await post('/api/codex', body); await refresh(); }
      catch (error) { element('p', error.message, view).setAttribute('role', 'alert'); }
      finally { node.disabled = false; }
    };
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const state = await readState();
      view.replaceChildren();
      element('h2', 'Codex tasks');
      element('p', 'Talk to ' + N + ' about the work you want handled. ' + N + ' can start tasks and join existing conversations; those messages begin “' + N + ':”. Open any task in Codex to read along or step in.');
      if (!state.controller) {
        element('p', 'Connect using the ID of an existing visible Codex task. This supplies the desktop context for ' + N + '’s controls.');
        const label = element('label', 'Controller task ID ');
        const input = element('input', '', label);
        input.type = 'text'; input.placeholder = 'Existing Codex task ID';
        const connect = element('button', 'Connect');
        connect.onclick = async () => {
          connect.disabled = true;
          try { await post('/api/codex', {action: 'connect', controller: input.value.trim()}); await refresh(); }
          catch (error) { element('p', error.message).setAttribute('role', 'alert'); }
          finally { connect.disabled = false; }
        };
        return;
      }
      element('p', state.enabled ? 'Supervision is on.' : 'Supervision is paused. Your tasks remain in Codex.');
      button(state.enabled ? 'Pause ' + N : 'Resume ' + N, {action: state.enabled ? 'pause' : 'resume'});
      if (state.error) element('p', state.error).setAttribute('role', 'alert');
      if (state.desktop_error) element('p', state.desktop_error).setAttribute('role', 'alert');
      const modeLabel = element('label', ' New project tasks: ');
      const mode = element('select', '', modeLabel);
      for (const [value, name] of [['local', 'Saved project folder'], ['worktree', 'Separate working copy']]) {
        const option = element('option', name, mode); option.value = value;
      }
      mode.value = state.project_mode || 'worktree';
      mode.onchange = async () => {
        try { await post('/api/codex', {action: 'project_mode', mode: mode.value}); }
        catch (error) { element('p', error.message).setAttribute('role', 'alert'); }
      };
      const refreshButton = element('button', 'Refresh tasks');
      refreshButton.onclick = refresh;
      const all = [...(state.desktop?.pinnedThreads || []), ...(state.desktop?.threads || [])];
      const seen = new Set();
      for (const task of Object.values(state.tasks || {})) {
        if (!all.some(row => row.id === task.thread && (row.hostId || 'local') === task.host))
          all.push({id: task.thread, hostId: task.host, title: task.title, kind: 'codex', status: 'not in recent list'});
      }
      for (const task of all) {
        if (task.kind !== 'codex') continue;
        const host = task.hostId || 'local', key = host + ':' + task.id;
        if (seen.has(key)) continue;
        seen.add(key);
        const tracked = state.tasks?.[key];
        const row = element('article', ''); row.className = 'codex-task';
        const link = element('a', task.title, row);
        link.href = 'codex://threads/' + encodeURIComponent(task.id);
        element('p', (typeof task.status === 'string' ? task.status : task.status?.type || 'unknown') +
          (tracked?.supervised ? ' · ' + N + ' following' : ' · Yours'), row);
        button(tracked?.supervised ? 'Take over — release ' + N : 'Hand to ' + N,
          {action: tracked?.supervised ? 'release' : 'hand', thread: task.id, host}, row);
      }
      if (state.audit?.length) {
        const details = element('details', '');
        element('summary', 'Recent ' + N + ' dispatches', details);
        for (const item of state.audit.slice().reverse()) {
          element('p', `${item.op}: ${item.status}`, details);
          element('pre', item.prompt || '', details);
          if (item.error) element('p', item.error, details);
        }
      }
    } catch (error) {
      view.replaceChildren();
      element('h2', 'Codex tasks');
      element('p', error.message).setAttribute('role', 'alert');
      const retry = element('button', 'Try again'); retry.onclick = refresh;
    } finally { refreshing = false; }
  }
  window.refreshCodexTasks = refresh;
  indicator.onclick = () => show('codex');
  const pollSessions = () => readState().catch(() => { /* next poll retries */ });
  pollSessions();
  setInterval(pollSessions, 15000);
})();
