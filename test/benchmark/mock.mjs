import http from 'node:http';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { randomUUID } from 'node:crypto';

function integer(name, fallback, min, max) {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer in [${min}, ${max}]`);
  }
  return value;
}

const settings = {
  delayMs: integer('MOCK_DELAY_MS', 0, 0, 60000),
  responseChars: integer('MOCK_RESPONSE_CHARS', 512, 16, 1048576),
  streamChunks: integer('MOCK_STREAM_CHUNKS', 10, 1, 1000),
  streamIntervalMs: integer('MOCK_STREAM_INTERVAL_MS', 50, 0, 60000),
};
const content = 'BENCHMARK '.padEnd(settings.responseChars, 'x');
const stats = { started: 0, completed: 0, aborted: 0, active: 0, peak: 0 };

function json(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

async function serve(req, res) {
  if (req.method === 'GET' && req.url === '/health') {
    return json(res, 200, { status: 'ok' });
  }
  if (req.headers.authorization !== 'Bearer sk-benchmark') {
    return json(res, 401, { error: { message: 'Invalid benchmark key' } });
  }
  if (req.method === 'GET' && req.url === '/metrics') {
    return json(res, 200, { ...stats, settings });
  }
  if (req.method === 'GET' && req.url === '/v1/models') {
    return json(res, 200, { object: 'list', data: [{ id: 'benchmark-chat', object: 'model' }] });
  }
  if (req.method !== 'POST' || req.url !== '/v1/chat/completions') {
    return json(res, 404, { error: { message: 'Unknown endpoint' } });
  }
  const buffers = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 1048576) return json(res, 413, { error: { message: 'Body too large' } });
    buffers.push(chunk);
  }
  let body;
  try {
    body = JSON.parse(Buffer.concat(buffers).toString('utf8'));
  } catch {
    return json(res, 400, { error: { message: 'Invalid JSON' } });
  }
  if (!body || body.model !== 'benchmark-chat' || !Array.isArray(body.messages) || !body.messages.length) {
    return json(res, 400, { error: { message: 'Expected benchmark-chat and messages' } });
  }
  stats.started++;
  stats.active++;
  stats.peak = Math.max(stats.peak, stats.active);
  const abort = new AbortController();
  let finished = false;
  res.once('finish', () => { finished = true; stats.completed++; });
  res.once('close', () => {
    stats.active--;
    if (!finished) stats.aborted++;
    abort.abort();
  });
  const id = `chatcmpl-bench-${randomUUID()}`;
  const common = { id, created: Math.floor(Date.now() / 1000), model: 'benchmark-chat' };
  if (settings.delayMs) await delay(settings.delayMs, undefined, { signal: abort.signal });
  if (!body.stream) {
    return json(res, 200, {
      ...common, object: 'chat.completion',
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    });
  }
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
  res.flushHeaders();
  async function event(value) {
    if (!res.write(`data: ${JSON.stringify(value)}\n\n`)) {
      await once(res, 'drain', { signal: abort.signal });
    }
  }
  for (let index = 0; index < settings.streamChunks; index++) {
    if (index && settings.streamIntervalMs) {
      await delay(settings.streamIntervalMs, undefined, { signal: abort.signal });
    }
    const start = Math.floor(index * content.length / settings.streamChunks);
    const end = Math.floor((index + 1) * content.length / settings.streamChunks);
    await event({ ...common, object: 'chat.completion.chunk',
      choices: [{ index: 0, delta: { content: content.slice(start, end) }, finish_reason: null }] });
  }
  await event({ ...common, object: 'chat.completion.chunk',
    choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
  res.end('data: [DONE]\n\n');
}

const server = http.createServer((req, res) => {
  serve(req, res).catch(error => {
    if (error.name === 'AbortError' || res.destroyed) return;
    if (!res.headersSent) json(res, 500, { error: { message: 'Mock internal error' } });
    else res.destroy();
    console.error(error.message);
  });
});
server.keepAliveTimeout = 65000;
server.headersTimeout = 66000;
server.listen(integer('MOCK_PORT', 9000, 0, 65535), '0.0.0.0', () => {
  console.log(JSON.stringify({ port: server.address().port, ...settings }));
});
