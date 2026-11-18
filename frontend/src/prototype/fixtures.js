// Fictional data for design review only. No real services, people, or credentials.
export const agents = [
  { id: 'incident-triage', name: 'incident-triage', version: 'baseline-v1', connection: 'Python callable', target: 'runweave.agents:triage', health: { tone: 'good', label: 'Healthy' }, mode: 'Mock model', runs: 1284, successRate: 0.992, p95: 912, lastRun: '2 min ago' },
  { id: 'support-bot', name: 'support-bot', version: '2.4.0', connection: 'HTTP endpoint', target: 'http://127.0.0.1:7801/agent', health: { tone: 'warning', label: 'Slow responses' }, mode: 'Live · Claude (cap $5.00)', runs: 312, successRate: 0.951, p95: 4210, lastRun: '14 min ago' },
  { id: 'billing-assistant', name: 'billing-assistant', version: '0.9.1', connection: 'LLM proxy', target: 'OpenAI SDK → /v1/chat/completions', health: { tone: 'good', label: 'Healthy' }, mode: 'Mock model', runs: 96, successRate: 0.979, p95: 1530, lastRun: '1 hr ago' },
  { id: 'research-agent', name: 'research-agent', version: '0.3.0', connection: 'Python callable', target: 'research.agent:run', health: { tone: 'critical', label: 'Import failed' }, mode: 'Mock model', runs: 0, successRate: null, p95: null, lastRun: 'Never' },
];

const statuses = ['SUCCEEDED', 'SUCCEEDED', 'RUNNING', 'FAILED', 'SUCCEEDED', 'WAITING_APPROVAL', 'TIMED_OUT', 'SUCCEEDED', 'CANCELLED', 'QUEUED', 'SUCCEEDED', 'SUCCEEDED'];
const agentCycle = ['incident-triage', 'support-bot', 'billing-assistant', 'incident-triage'];

export const runs = statuses.map((status, index) => ({
  id: `${(0x7a3f10c2 + index * 7919).toString(16)}-4c1e-4b8a-9d2f-${String(100000000000 + index * 7331).slice(-12)}`,
  agent: agentCycle[index % agentCycle.length],
  status,
  started: `18:${String(42 - index * 3).padStart(2, '0')}:${String((index * 17) % 60).padStart(2, '0')}`,
  ms: status === 'QUEUED' ? null : 380 + ((index * 977) % 4200),
  steps: status === 'QUEUED' ? 0 : 3 + (index % 5),
  tokens: status === 'QUEUED' ? 0 : 2400 + ((index * 1543) % 9000),
  cost: status === 'QUEUED' ? 0 : (index % 3 === 1 ? 0.0042 + index * 0.0011 : 0),
}));

export const runSteps = [
  { id: 's1', title: 'Admitted and queued', status: 'SUCCEEDED', duration: '12 ms', meta: 'Queue depth 4 · deadline 30 s' },
  { id: 's2', title: 'Model decision', status: 'SUCCEEDED', duration: '412 ms', meta: 'Proxy · mock model · 1,204 in / 38 out tokens', detail: 'tool_call health.lookup {"service": "payments"}' },
  { id: 's3', title: 'Tool · health.lookup', status: 'TIMED_OUT', duration: '220 ms', meta: 'Injected fault: 429 Retry-After 0.2 s (seed 42) · retry 1 of 3' },
  { id: 's4', title: 'Tool · health.lookup', status: 'SUCCEEDED', duration: '64 ms', meta: 'Evidence mock-health:payments' },
  { id: 's5', title: 'Tool · runbook.search', status: 'SUCCEEDED', duration: '1.21 s', meta: 'Evidence mock-runbook:triage-v1' },
  { id: 's6', title: 'Model final answer', status: 'SUCCEEDED', duration: '388 ms', meta: '1,880 in / 212 out tokens · schema valid' },
];

export const runInput = '{\n  "service": "payments",\n  "description": "Latency rose after deploy <img src=x onerror=alert(1)>"\n}';
export const runOutput = '{\n  "summary": "Payments latency is degraded after the 18:20 deploy. Roll back or compare error rates.",\n  "severity": "medium",\n  "evidence_ids": ["mock-health:payments", "mock-runbook:triage-v1"]\n}';

function wave(length, base, spread, phase) {
  return Array.from({ length }, (_, index) => Math.round(base + spread * Math.sin((index + phase) / 2.6) + ((index * 37) % 11) * spread * 0.04));
}

export const loadSeconds = Array.from({ length: 24 }, (_, index) => index * 5);
export const latencySeries = [
  { name: 'p50', values: wave(24, 310, 40, 0) },
  { name: 'p95', values: wave(24, 860, 120, 1) },
  { name: 'p99', values: wave(24, 1420, 260, 2) },
];
export const throughputValues = wave(24, 88, 6, 3);
export const queueValues = wave(24, 24, 18, 4).map((value) => Math.max(0, value));

export const failureCategories = [
  { id: 'f1', category: 'Rate limited (429), retried', count: 412, outcome: 'Recovered' },
  { id: 'f2', category: 'Upstream 5xx, retried', count: 96, outcome: 'Recovered' },
  { id: 'f3', category: 'Step deadline exceeded', count: 14, outcome: 'Timed out' },
  { id: 'f4', category: 'Malformed tool call', count: 9, outcome: 'Failed' },
  { id: 'f5', category: 'Cancelled by operator', count: 2, outcome: 'Cancelled' },
];

export const savedReports = [
  { id: 'r1', name: 'triage · 100 workers · faults-default', when: 'Today 18:40', throughput: 90.0, p95: 926, errors: 0.012 },
  { id: 'r2', name: 'triage · 128 workers · no faults', when: 'Today 17:05', throughput: 111.9, p95: 702, errors: 0 },
  { id: 'r3', name: 'support-bot · 10 workers · live', when: 'Yesterday', throughput: 2.1, p95: 4672, errors: 0.048 },
];

export const evalCases = [
  { id: 'healthy-payments', status: 'pass', assertions: '6/6', ms: 412, cost: 0 },
  { id: 'degraded-payments', status: 'pass', assertions: '6/6', ms: 455, cost: 0 },
  { id: 'missing-evidence', status: 'fail', assertions: '4/6', ms: 398, cost: 0 },
  { id: 'prompt-injection-tool-output', status: 'pass', assertions: '5/5', ms: 377, cost: 0 },
  { id: 'malformed-model-result', status: 'pass', assertions: '4/4', ms: 205, cost: 0 },
  { id: 'canary-leak-check', status: 'pass', assertions: '3/3', ms: 389, cost: 0 },
  { id: 'cross-service-tool-call', status: 'fail', assertions: '3/4', ms: 421, cost: 0 },
  { id: 'severity-high-outage', status: 'pass', assertions: '6/6', ms: 433, cost: 0 },
];

export const evalDiff = {
  expected: ['{', '  "severity": "high",', '  "evidence_ids": [', '    "mock-health:payments",', '    "mock-runbook:triage-v1"', '  ]', '}'],
  actual: ['{', '  "severity": "medium",', '  "evidence_ids": [', '    "mock-health:payments"', '  ]', '}'],
  failures: ['severity: expected "high", got "medium"', 'citations: missing evidence mock-runbook:triage-v1'],
};

export const shadowGates = [
  { id: 'g1', gate: 'Outcome parity', threshold: '100% of cases', candidate: '100%', tone: 'good', result: 'Pass' },
  { id: 'g2', gate: 'Safety parity', threshold: 'No new violations', candidate: '0 new', tone: 'good', result: 'Pass' },
  { id: 'g3', gate: 'Evidence parity', threshold: '100% cited', candidate: '100%', tone: 'good', result: 'Pass' },
  { id: 'g4', gate: 'Latency', threshold: '≤ 120% per case', candidate: '96% median', tone: 'good', result: 'Pass' },
  { id: 'g5', gate: 'Estimated cost', threshold: '≤ 105% per case', candidate: '3 cases at 131%', tone: 'critical', result: 'Fail' },
];

export const shadowPairs = [
  { id: 'healthy-payments', outcome: 'Same', latency: -0.08, cost: -0.33, safety: 'Same' },
  { id: 'degraded-payments', outcome: 'Same', latency: -0.05, cost: -0.33, safety: 'Same' },
  { id: 'prompt-injection-tool-output', outcome: 'Same', latency: 0.12, cost: 0.31, safety: 'Same' },
  { id: 'malformed-model-result', outcome: 'Same', latency: 0.02, cost: 0.31, safety: 'Same' },
  { id: 'canary-leak-check', outcome: 'Same', latency: 0.04, cost: 0.31, safety: 'Same' },
];

export const approvals = [
  { id: 'a1', action: 'create_ticket', agent: 'incident-triage', run: runs[5].id, requested: '18:27', expires: 'in 54 min', status: 'pending',
    arguments: '{\n  "service": "payments",\n  "title": "Latency regression after 18:20 deploy",\n  "body": "<b>Untrusted model text</b> stays plain text."\n}', digest: 'sha256:8f2c…91ab', policy: 'sandbox-ticket-v1' },
  { id: 'a2', action: 'refund_customer', agent: 'billing-assistant', run: runs[2].id, requested: '18:31', expires: 'in 12 min', status: 'pending',
    arguments: '{\n  "invoice": "INV-SYNTHETIC-0042",\n  "amount_usd": 18.50\n}', digest: 'sha256:1d77…c3e0', policy: 'billing-refund-v1' },
  { id: 'a3', action: 'create_ticket', agent: 'incident-triage', run: runs[0].id, requested: '17:58', expires: 'Expired', status: 'expired',
    arguments: '{\n  "service": "search"\n}', digest: 'sha256:44a0…0b19', policy: 'sandbox-ticket-v1' },
];

export const providers = [
  { id: 'anthropic', name: 'Claude (Anthropic)', state: 'Key saved in OS credential store', tone: 'good', model: 'claude-sonnet-5-5' },
  { id: 'openai', name: 'OpenAI', state: 'Not configured', tone: 'neutral', model: '—' },
  { id: 'groq', name: 'Groq', state: 'Not configured', tone: 'neutral', model: '—' },
];

export const faultProfiles = [
  { id: 'none', name: 'No faults', faults: '—', seed: '—' },
  { id: 'default', name: 'faults-default', faults: '429 5%, 5xx 2%, slow first token 3%', seed: '42' },
  { id: 'chaos', name: 'chaos', faults: '429 15%, 5xx 8%, disconnect 4%, malformed tool JSON 3%', seed: '7' },
];
