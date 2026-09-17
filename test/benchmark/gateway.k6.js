import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';

function positive(name, fallback) {
  const value = Number(__ENV[name] || fallback);
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer`);
  return value;
}

const target = __ENV.TARGET || 'full';
const targets = {
  mock: { url: 'http://mock:9000/v1/chat/completions' },
  litellm: { url: 'http://litellm:4000/v1/chat/completions' },
  higress: { url: 'http://higress:8080/v1/chat/completions', host: 'mock.benchmark.local' },
  full: { url: 'http://higress:8080/v1/chat/completions' },
};
if (!targets[target]) throw new Error('TARGET must be mock, litellm, higress or full');
const mode = __ENV.LOAD_MODE || 'concurrency';
if (!['concurrency', 'rate'].includes(mode)) throw new Error('LOAD_MODE must be concurrency or rate');
const vus = positive('VUS', 10);
const duration = __ENV.DURATION || '30s';
const stream = __ENV.STREAM === '1';
const responseChars = positive('RESPONSE_CHARS', 512);
const inputChars = positive('INPUT_CHARS', 32);
const timeout = __ENV.TIMEOUT || '30s';
const gracefulStop = __ENV.GRACEFUL_STOP || '35s';
const success = new Rate('valid_responses');
const successful = new Counter('successful_requests');
const maxVUs = positive('MAX_VUS', 200);
if (mode === 'rate' && maxVUs < vus) throw new Error('MAX_VUS must be >= VUS');

export const options = {
  scenarios: { benchmark: mode === 'rate' ? {
    executor: 'constant-arrival-rate', rate: positive('RPS', 50), timeUnit: '1s',
    duration, preAllocatedVUs: vus, maxVUs, gracefulStop,
  } : { executor: 'constant-vus', vus, duration, gracefulStop } },
  tags: { target, stream: String(stream) },
  thresholds: {
    valid_responses: ['rate>=0.99'],
    http_req_failed: ['rate<=0.01'],
    http_req_duration: [`p(95)<${positive('P95_MS', 1000)}`],
    ...(mode === 'rate' ? { dropped_iterations: ['count==0'] } : {}),
  },
};

function answer(body) {
  if (!stream) {
    const data = JSON.parse(body);
    if (data.error) return null;
    return data.choices?.[0]?.message?.content;
  }
  let text = '';
  let done = false;
  let finished = false;
  for (const event of body.split(/\r?\n\r?\n/)) {
    const data = event.split(/\r?\n/).filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n');
    if (!data) continue;
    if (data === '[DONE]') { done = true; break; }
    const chunk = JSON.parse(data);
    if (chunk.error || !Array.isArray(chunk.choices)) return null;
    for (const choice of chunk.choices) {
      if (choice.index !== 0) continue;
      if (typeof choice.delta?.content === 'string') text += choice.delta.content;
      if (choice.finish_reason) finished = true;
    }
  }
  return done && finished ? text : null;
}

const payload = JSON.stringify({ model: 'benchmark-chat', stream,
  messages: [{ role: 'user', content: 'benchmark'.padEnd(inputChars, 'x').slice(0, inputChars) }] });

export default function () {
  const headers = { Authorization: 'Bearer sk-benchmark', 'Content-Type': 'application/json' };
  if (targets[target].host) headers.Host = targets[target].host;
  const response = http.post(targets[target].url, payload, { headers, timeout, redirects: 0 });
  let valid = false;
  try {
    const content = answer(response.body || '');
    const contentType = String(response.headers['Content-Type'] || '').toLowerCase();
    valid = response.status === 200 && typeof content === 'string'
      && content.length === responseChars && content === 'BENCHMARK '.padEnd(responseChars, 'x')
      && contentType.includes(stream ? 'text/event-stream' : 'application/json');
  } catch { valid = false; }
  check(response, { 'HTTP 200 and complete expected content': () => valid });
  success.add(valid);
  if (valid) successful.add(1);
}

export function handleSummary(data) {
  const values = name => data.metrics[name]?.values || {};
  const summary = {
    target, mode, stream, requests: values('http_reqs').count || 0,
    successfulRps: values('successful_requests').rate || 0,
    validRate: values('valid_responses').rate || 0,
    durationMs: values('http_req_duration'),
    blockedMs: values('http_req_blocked'),
    connectingMs: values('http_req_connecting'),
    droppedIterations: values('dropped_iterations').count || 0,
    thresholds: Object.fromEntries(Object.entries(data.metrics)
      .filter(([, metric]) => metric.thresholds).map(([name, metric]) => [name, metric.thresholds])),
  };
  return { stdout: JSON.stringify(summary, null, 2) + '\n',
    [__ENV.REPORT || `/reports/${target}-${stream ? 'stream' : 'json'}-${Date.now()}.json`]:
    JSON.stringify({ target, mode, stream, vus, duration, inputChars, responseChars,
      requestedRps: mode === 'rate' ? positive('RPS', 50) : null, ...data }, null, 2) };
}
