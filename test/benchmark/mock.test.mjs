import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

let child;
let base;
const headers = { Authorization: 'Bearer sk-benchmark', 'Content-Type': 'application/json' };
const payload = { model: 'benchmark-chat', messages: [{ role: 'user', content: 'test' }] };

before(async () => {
  child = spawn(process.execPath, [fileURLToPath(new URL('./mock.mjs', import.meta.url))], {
    env: { ...process.env, MOCK_PORT: '0', MOCK_RESPONSE_CHARS: '128', MOCK_DELAY_MS: '10',
      MOCK_STREAM_CHUNKS: '4', MOCK_STREAM_INTERVAL_MS: '15' },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  const lines = createInterface({ input: child.stdout });
  const [line] = await once(lines, 'line');
  base = `http://127.0.0.1:${JSON.parse(line).port}`;
  lines.close();
}, { timeout: 5000 });

after(async () => {
  if (child && child.exitCode === null) {
    child.kill();
    await once(child, 'exit');
  }
});

test('auth, model and body validation', async () => {
  assert.equal((await fetch(base + '/health')).status, 200);
  assert.equal((await fetch(base + '/v1/models')).status, 401);
  const models = await (await fetch(base + '/v1/models', { headers })).json();
  assert.equal(models.data[0].id, 'benchmark-chat');
  for (const body of ('{', JSON.stringify({ ...payload, model: 'real-model' }))) {
    assert.equal((await fetch(base + '/v1/chat/completions', { method: 'POST', headers, body })).status, 400);
  }
});

test('fixed JSON content', async () => {
  const response = await fetch(base + '/v1/chat/completions', {
    method: 'POST', headers, body: JSON.stringify(payload),
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.choices[0].message.content, 'BENCHMARK '.padEnd(128, 'x'));
  assert.equal(body.choices[0].finish_reason, 'stop');
});

test('complete SSE content and end markers', async () => {
  const response = await fetch(base + '/v1/chat/completions', {
    method: 'POST', headers, body: JSON.stringify({ ...payload, stream: true }),
  });
  assert.match(response.headers.get('content-type'), /text\/event-stream/);
  const events = (await response.text()).trim().split('\n\n').map(line => line.slice(6));
  assert.equal(events.pop(), '[DONE]');
  const chunks = events.map(JSON.parse);
  assert.equal(chunks.at(-1).choices[0].finish_reason, 'stop');
  assert.equal(chunks.map(chunk => chunk.choices[0].delta.content || '').join(''), 'BENCHMARK '.padEnd(128, 'x'));
});

test('client cancellation releases active request', async () => {
  const response = await fetch(base + '/v1/chat/completions', {
    method: 'POST', headers, body: JSON.stringify({ ...payload, stream: true }),
  });
  await response.body.cancel();
  let stats;
  for (let attempt = 0; attempt < 50; attempt++) {
    stats = await (await fetch(base + '/metrics', { headers })).json();
    if (stats.active === 0) break;
    await delay(10);
  }
  assert.equal(stats.active, 0);
  assert.equal(stats.aborted, 1);
  assert.equal(stats.completed, 2);
  assert.equal(stats.started, 3);
});
