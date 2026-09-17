import { cp, mkdir } from 'node:fs/promises';

for (const kind of ['ingresses', 'mcpbridges']) {
  await mkdir(`/data/${kind}`, { recursive: true });
  await cp(`/bench/higress/${kind}`, `/data/${kind}`, { recursive: true });
}
await mkdir('/reports', { recursive: true });
console.log('Benchmark routes initialized.');
