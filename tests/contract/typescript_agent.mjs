// Contract fixture agent (TypeScript SDK). Behavior is specified in tests/contract/README.md.
// Run: node tests/contract/typescript_agent.mjs  ->  prints "RUNWEAVE_AGENT_READY <url>".
import { createAgentHandler, defineAgent, serveAgents } from '../../sdk/typescript/dist/index.js';

const inputSchema = {
  type: 'object',
  properties: {
    text: { type: 'string', minLength: 1, maxLength: 100 },
    delay_ms: { type: 'integer', minimum: 0, maximum: 60000, default: 0 },
  },
  required: ['text'],
  additionalProperties: false,
};
const outputSchema = {
  type: 'object',
  properties: { reply: { type: 'string' }, attempt: { type: 'integer' } },
  required: ['reply', 'attempt'],
};
const events = { cancelled: [], requests: {} };

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
  });
}

const echo = defineAgent({
  name: 'contract-echo', version: '1.0.0', maxSteps: 5, inputSchema, outputSchema,
  async run(input, ctx) {
    await ctx.step('Echo', async () => {
      try {
        await sleep(input.delay_ms ?? 0, ctx.signal);
      } catch (error) {
        events.cancelled.push(input.text);
        throw error;
      }
    }, { detail: `${input.text.length} characters` });
    return { reply: input.text, attempt: ctx.attempt };
  },
});
const crash = defineAgent({
  name: 'contract-crash', version: '1.0.0', inputSchema, outputSchema,
  async run() { throw new Error('fixture crash with /internal/path'); },
});

const raw = {
  'contract-malformed': [200, { 'Content-Type': 'application/json' }, '{"output": '],
  'contract-oversized': [200, { 'Content-Type': 'application/json' },
    JSON.stringify({ output: { reply: 'x'.repeat(2_000_000), attempt: 1 } })],
  'contract-redirect': [307, { Location: 'http://169.254.169.254/latest/meta-data' }, ''],
  'contract-unavailable': [503, { 'Retry-After': '0' }, ''],
  'contract-server-error': [500, { 'Content-Type': 'text/plain' }, 'Traceback /secret/path'],
};
const misbehaving = Object.keys(raw).map((name) => defineAgent({
  name, version: '1.0.0', inputSchema, outputSchema,
  async run() { throw new Error('the fixture answers this agent itself'); },
}));

const handler = createAgentHandler([echo, crash, ...misbehaving]);

function fixture(request, response) {
  const path = new URL(request.url, 'http://fixture').pathname;
  if (path === '/fixture/events') {
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(events));
    return;
  }
  const parts = path.split('/').filter(Boolean);
  if (parts.length === 5 && parts.slice(0, 3).join('/') === 'runweave/v1/agents' && parts[4] === 'runs') {
    events.requests[parts[3]] = (events.requests[parts[3]] ?? 0) + 1;
    const answer = raw[parts[3]];
    if (answer) {
      const [status, headers, body] = answer;
      request.resume();
      response.writeHead(status, { ...headers, 'Content-Length': Buffer.byteLength(body) }).end(body);
      return;
    }
  }
  handler(request, response);
}

const server = await serveAgents([], { port: 0, handler: fixture });
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
