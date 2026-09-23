// Adapter to the installed Codex desktop's own app-tools pipe.
// No app-server is launched, no rollouts are edited, and no credentials are read.
import net from 'node:net';
import fs from 'node:fs';
import crypto from 'node:crypto';

const allowed = new Set(['list_threads', 'list_projects', 'read_thread',
  'wait_threads', 'create_thread', 'send_message_to_thread', 'capabilities']);
const maxFrame = 8 * 1024 * 1024;
// Several desktop integrations use this pipe prefix. Discover by their read-only
// tool catalogs; a filename alone does not identify the app-controls service.
async function isAppToolsPipe(path) {
  return new Promise(resolve => {
    const probe = net.createConnection(path);
    let data = Buffer.alloc(0), settled = false;
    const finish = value => {
      if (settled) return;
      settled = true; clearTimeout(timeout); probe.destroy(); resolve(value);
    };
    const timeout = setTimeout(() => finish(false), 1500);
    probe.once('error', () => finish(false));
    probe.once('close', () => finish(false));
    probe.once('connect', () => {
      const body = Buffer.from(JSON.stringify({jsonrpc:'2.0', id:1, method:'tools/list', params:{threadStartKind:'all'}}));
      const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
      probe.write(Buffer.concat([header, body]));
    });
    probe.on('data', chunk => {
      data = Buffer.concat([data, chunk]);
      if (data.length < 4) return;
      const length = data.readUInt32LE(0);
      if (length > maxFrame) return finish(false);
      if (data.length < length + 4) return;
      try {
        const result = JSON.parse(data.subarray(4, length + 4).toString('utf8'));
        finish(Boolean(result.result?.tools?.some(tool => tool.name === 'list_threads')));
      } catch { finish(false); }
    });
  });
}
let socket;
const timer = setTimeout(() => {
  process.stderr.write('Codex desktop did not respond before the timeout.\n');
  socket?.destroy();
  process.exit(1);
}, 55000);

try {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
    if (input.length > maxFrame) throw new Error('Request too large.');
  }
  const request = JSON.parse(input);
  if (!allowed.has(request.tool)) throw new Error('Unsupported desktop operation.');
  let pipe = process.env.CODEX_APP_TOOLS_PIPE_PATH;
  if (pipe && !await isAppToolsPipe(pipe)) pipe = null;
  if (!pipe) {
    const candidates = fs.readdirSync('\\\\.\\pipe\\')
      .filter(name => name.startsWith('codex-browser-use-'));
    const checks = await Promise.all(candidates.map(async name => {
      const path = '\\\\.\\pipe\\' + name;
      return await isAppToolsPipe(path) ? path : null;
    }));
    const matches = checks.filter(Boolean);
    if (matches.length !== 1)
      throw new Error('Could not identify one Codex app-controls connection. Open Codex and try again.');
    pipe = matches[0];
  }
  socket = net.createConnection(pipe);
  let buffer = Buffer.alloc(0), nextId = 0;
  const pending = new Map();
  socket.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const size = buffer.readUInt32LE(0);
      if (size > maxFrame) { socket.destroy(new Error('Desktop response too large.')); return; }
      if (buffer.length < size + 4) return;
      try {
        const result = JSON.parse(buffer.subarray(4, size + 4).toString('utf8'));
        buffer = buffer.subarray(size + 4);
        const entry = pending.get(result.id);
        pending.delete(result.id);
        if (entry) result.error ? entry.reject(new Error(result.error.message)) : entry.resolve(result.result);
      } catch (error) { socket.destroy(error); return; }
    }
  });
  socket.on('error', error => { for (const entry of pending.values()) entry.reject(error); });
  socket.on('close', () => { for (const entry of pending.values()) entry.reject(new Error('Codex desktop disconnected.')); });
  await new Promise((resolve, reject) => { socket.once('connect', resolve); socket.once('error', reject); });
  const rpc = (method, params) => new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, {resolve, reject});
    const body = Buffer.from(JSON.stringify({jsonrpc: '2.0', id, method, params}));
    const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
    socket.write(Buffer.concat([header, body]));
  });
  const catalog = await rpc('tools/list', {threadStartKind: 'all'});
  if (request.tool === 'capabilities') {
    process.stdout.write(JSON.stringify(catalog.tools.filter(tool =>
      ['create_thread', 'send_message_to_thread'].includes(tool.name))));
  } else {
  const tool = catalog.tools.find(tool => tool.name === request.tool);
  if (!tool) throw new Error('This Codex version does not expose ' + request.tool);
  if (!request.thread) throw new Error('A visible controller task must be selected.');
  const result = await rpc('tools/call', {
    tool: tool.name, namespace: tool.namespace, arguments: request.args || {},
    threadId: request.thread, callId: 'assistant-' + crypto.randomUUID(),
    turnId: 'assistant-' + crypto.randomUUID(),
  });
  if (!result.success) throw new Error(result.contentItems?.map(item => item.text || '').join('\n') || 'Desktop operation failed.');
  const texts = result.contentItems.filter(item => item.type === 'inputText').map(item => item.text);
  if (texts.length !== 1) throw new Error('Unexpected desktop response.');
  process.stdout.write(texts[0]);
  }
} catch (error) {
  process.stderr.write(String(error.message) + '\n');
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  socket?.destroy();
}
