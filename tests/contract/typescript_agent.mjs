// Contract fixture agent (TypeScript SDK). Behavior is specified in tests/contract/README.md.
// Run: node tests/contract/typescript_agent.mjs  ->  prints "RUNWEAVE_AGENT_READY <url>".
import { createAgentHandler, defineAgent, EffectResponseLost, serveAgents } from '../../sdk/typescript/dist/index.js';

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
const events = { cancelled: [], requests: {}, approvals: [], tickets: {} };

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

const approver = defineAgent({
  name: 'contract-approver', version: '1.0.0',
  inputSchema: {
    type: 'object',
    properties: {
      order: { type: 'string', minLength: 1, maxLength: 40 },
      expires_in: { type: 'number', exclusiveMinimum: 0, maximum: 3600, default: 30 },
    },
    required: ['order'],
    additionalProperties: false,
  },
  outputSchema: {
    type: 'object',
    properties: {
      approved: { type: 'boolean' }, status: { type: 'string' }, decided_by: { type: ['string', 'null'] },
      attempt: { type: 'integer' }, remaining_ms: { type: 'number' },
    },
    required: ['approved', 'status', 'decided_by', 'attempt', 'remaining_ms'],
  },
  async run(input, ctx) {
    events.approvals.push({ order: input.order, attempt: ctx.attempt });
    const decision = await ctx.requestApproval('issue_refund', { order: input.order }, { expiresIn: input.expires_in ?? 30 });
    return {
      approved: decision.approved, status: decision.status, decided_by: decision.decidedBy,
      attempt: ctx.attempt, remaining_ms: ctx.remainingMs(),
    };
  },
});

// A downstream that honours idempotency keys.
async function createTicket(order, key) {
  const tickets = (events.tickets[order] ??= []);
  const existing = key === null ? undefined : tickets.find((ticket) => ticket.key === key);
  if (existing) return { id: existing.id };
  const ticket = { id: `${order}-${tickets.length + 1}`, key };
  tickets.push(ticket);
  return { id: ticket.id };
}

const ticketing = defineAgent({
  name: 'contract-ticketing', version: '1.0.0',
  inputSchema: {
    type: 'object',
    properties: {
      order: { type: 'string', minLength: 1, maxLength: 40 },
      use_key: { type: 'boolean', default: true },
    },
    required: ['order'],
    additionalProperties: false,
  },
  outputSchema: {
    type: 'object',
    properties: { ticket: { type: 'string' }, tries: { type: 'integer' } },
    required: ['ticket', 'tries'],
  },
  async run(input, ctx) {
    const key = input.use_key === false ? null : `order-${input.order}`;
    for (let tries = 1; tries <= 5; tries += 1) {
      try {
        const ticket = await ctx.effect('create_ticket', () => createTicket(input.order, key),
          { key, resource: (t) => t.id });
        return { ticket: ticket.id, tries };
      } catch (error) {
        if (!(error instanceof EffectResponseLost)) throw error;
      }
    }
    throw new Error('no ticket after 5 tries');
  },
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

const handler = createAgentHandler([echo, crash, approver, ticketing, ...misbehaving]);

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
