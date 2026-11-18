// Agent-side TypeScript SDK: the same contract behavior as the Python wrapper.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { after, before, describe, test } from 'node:test';
import { AgentError, CONTRACT_VERSION, InputError, defineAgent, serveAgents } from '../dist/index.js';

const inputSchema = {
  type: 'object',
  properties: { text: { type: 'string', minLength: 1, maxLength: 40 }, delay: { type: 'number' } },
  required: ['text'],
};
const outputSchema = { type: 'object', properties: { reply: { type: 'string' } }, required: ['reply'] };
const calls = [];
const cancelled = [];

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
  });
}

const echo = defineAgent({
  name: 'echo', version: '1.0.0', description: 'Echo', maxSteps: 3, inputSchema, outputSchema,
  parseInput(raw) {
    if (typeof raw.text !== 'string' || raw.text.length === 0) {
      throw new InputError([{ loc: ['text'], type: 'string_too_short', msg: 'Text is required' }]);
    }
    return raw;
  },
  async run(input, ctx) {
    calls.push(input.text);
    await ctx.step('Think', async () => {
      try {
        await sleep(input.delay ?? 0, ctx.signal);
      } catch (error) {
        cancelled.push(input.text);
        throw error;
      }
    }, { detail: 'token protected-value' });
    ctx.log('done protected-value');
    return { reply: input.text, attempt: ctx.attempt, remaining: ctx.remainingMs(), runId: ctx.runId };
  },
});
const crash = defineAgent({
  name: 'crash', version: '1', inputSchema, outputSchema,
  async run() { throw new Error('internal /home/dev/secret detail'); },
});
const classified = defineAgent({
  name: 'classified', version: '1', inputSchema, outputSchema,
  async run() { throw new AgentError('AGENT_OUTPUT_INVALID'); },
});
const notObject = defineAgent({
  name: 'not-object', version: '1', inputSchema, outputSchema,
  async run() { return 'text'; },
});
const steps = defineAgent({
  name: 'steps', version: '1', maxSteps: 1, inputSchema, outputSchema,
  async run(input, ctx) {
    await ctx.step('one', async () => {});
    await ctx.step('two', async () => {});
    return { reply: 'never' };
  },
});

let server;
let base;
const logs = [];

before(async () => {
  server = await serveAgents([echo, crash, classified, notObject, steps], {
    port: 0, protectedValues: ['protected-value'], maxBodyBytes: 2048, maxConcurrency: 2,
    logger: (line) => logs.push(line),
  });
  base = server.url;
});
after(() => server.close());

function envelope(overrides = {}) {
  return { run_id: randomUUID(), attempt: 1, input: { text: 'hi' }, deadline_ms: 5000, ...overrides };
}

async function post(name, body, key) {
  const headers = { 'Content-Type': 'application/json' };
  if (key) headers['Idempotency-Key'] = key;
  const response = await fetch(`${base}/runweave/v1/agents/${name}/runs`, {
    method: 'POST', headers, body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  return { status: response.status, headers: response.headers, body: await response.json() };
}

describe('definition', () => {
  test('rejects invalid names, versions, limits and non-async agents', () => {
    assert.throws(() => defineAgent({ name: 'Bad Name', version: '1', inputSchema, outputSchema, run: async () => ({}) }), /name/);
    assert.throws(() => defineAgent({ name: 'ok', version: '', inputSchema, outputSchema, run: async () => ({}) }), /version/);
    assert.throws(() => defineAgent({ name: 'ok', version: '1', maxSteps: 0, inputSchema, outputSchema, run: async () => ({}) }), /maxSteps/);
    assert.throws(() => defineAgent({ name: 'ok', version: '1', inputSchema: { type: 'array' }, outputSchema, run: async () => ({}) }), /object/);
    assert.throws(() => defineAgent({ name: 'ok', version: '1', inputSchema, outputSchema }), /run/);
  });

  test('serves only unique names on loopback', async () => {
    await assert.rejects(serveAgents([echo, echo], { port: 0 }), /more than once/);
    await assert.rejects(serveAgents([], { port: 0 }), /at least one/);
    await assert.rejects(serveAgents([echo], { port: 0, host: '0.0.0.0' }), /loopback/);
  });
});

describe('contract', () => {
  test('manifest lists the agents and their schemas', async () => {
    const response = await fetch(`${base}/runweave/v1/agents`);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type'), /^application\/json/);
    const body = await response.json();
    assert.equal(body.contract, CONTRACT_VERSION);
    const entry = body.agents.find((a) => a.name === 'echo');
    assert.deepEqual(entry, {
      name: 'echo', version: '1.0.0', description: 'Echo', input_schema: inputSchema,
      output_schema: outputSchema, max_steps: 3,
    });
  });

  test('a run returns output and redacted steps with the context fields', async () => {
    const body = envelope({ attempt: 2, deadline_ms: 4000 });
    const { status, body: result } = await post('echo', body);
    assert.equal(status, 200);
    assert.equal(result.output.reply, 'hi');
    assert.equal(result.output.attempt, 2);
    assert.equal(result.output.runId, body.run_id);
    assert.ok(result.output.remaining > 0 && result.output.remaining <= 4000);
    assert.equal(result.steps.length, 1);
    assert.equal(result.steps[0].title, 'Think');
    assert.equal(result.steps[0].status, 'succeeded');
    assert.ok(result.steps[0].duration_ms >= 0);
    assert.ok(!result.steps[0].detail.includes('protected-value'));
    assert.ok(logs.some((line) => line.includes('done') && !line.includes('protected-value')));
  });

  for (const [name, body, status, code] of [
    ['nobody', envelope(), 404, 'UNKNOWN_AGENT'],
    ['echo', { input: {} }, 400, 'INVALID_REQUEST'],
    ['echo', envelope({ deadline_ms: 0 }), 400, 'INVALID_REQUEST'],
    ['echo', envelope({ run_id: 'not-a-uuid' }), 400, 'INVALID_REQUEST'],
    ['echo', envelope({ input: ['x'] }), 400, 'INVALID_REQUEST'],
    ['echo', '{nope', 400, 'INVALID_REQUEST'],
    ['echo', envelope({ input: { text: '' } }), 422, 'INVALID_INPUT'],
    ['crash', envelope(), 500, 'RUN_FAILED'],
    ['classified', envelope(), 500, 'AGENT_OUTPUT_INVALID'],
    ['not-object', envelope(), 500, 'AGENT_OUTPUT_INVALID'],
    ['steps', envelope(), 500, 'AGENT_STEP_LIMIT'],
    ['echo', envelope({ input: { text: 'x'.repeat(4000) } }), 413, 'INPUT_TOO_LARGE'],
  ]) {
    test(`${name} answers ${status} ${code}`, async () => {
      const response = await post(name, body);
      assert.equal(response.status, status);
      assert.equal(response.body.error.code, code);
      assert.ok(!JSON.stringify(response.body).includes('secret'));
    });
  }

  test('invalid input errors are returned without values', async () => {
    const { body } = await post('echo', envelope({ input: { text: '' } }));
    assert.deepEqual(body.error.errors, [{ loc: ['text'], type: 'string_too_short', msg: 'Text is required' }]);
  });

  test('the agent deadline has its own code', async () => {
    const { status, body } = await post('echo', envelope({ input: { text: 'slow', delay: 5000 }, deadline_ms: 100 }));
    assert.equal(status, 500);
    assert.equal(body.error.code, 'AGENT_DEADLINE_EXCEEDED');
  });

  test('a repeated idempotency key runs once', async () => {
    const body = envelope({ input: { text: 'once', delay: 200 } });
    const [first, second] = await Promise.all([post('echo', body, body.run_id), post('echo', body, body.run_id)]);
    const third = await post('echo', body, body.run_id);
    assert.deepEqual([first.status, second.status, third.status], [200, 200, 200]);
    assert.deepEqual(first.body, second.body);
    assert.deepEqual(first.body, third.body);
    assert.equal(calls.filter((text) => text === 'once').length, 1);
  });

  test('reusing a key for another run is a conflict', async () => {
    await post('echo', envelope({ input: { text: 'a' } }), 'shared-key');
    const { status, body } = await post('echo', envelope({ input: { text: 'b' } }), 'shared-key');
    assert.equal(status, 409);
    assert.equal(body.error.code, 'IDEMPOTENCY_CONFLICT');
  });

  test('the cancel call aborts the running agent', async () => {
    const body = envelope({ input: { text: 'cancel-me', delay: 30000 } });
    const running = post('echo', body, body.run_id);
    while (!calls.includes('cancel-me')) await new Promise((r) => setTimeout(r, 10));
    const response = await fetch(`${base}/runweave/v1/agents/echo/runs/${body.run_id}/cancel`, { method: 'POST' });
    assert.equal(response.status, 202);
    const result = await running;
    assert.equal(result.status, 409);
    assert.equal(result.body.error.code, 'RUN_CANCELLED');
    assert.ok(cancelled.includes('cancel-me'));
  });

  test('closing the request aborts the agent', async () => {
    const body = envelope({ input: { text: 'disconnect', delay: 30000 } });
    const controller = new AbortController();
    const running = fetch(`${base}/runweave/v1/agents/echo/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal,
    }).catch(() => null);
    while (!calls.includes('disconnect')) await new Promise((r) => setTimeout(r, 10));
    controller.abort();
    await running;
    for (let i = 0; i < 100 && !cancelled.includes('disconnect'); i += 1) await new Promise((r) => setTimeout(r, 10));
    assert.ok(cancelled.includes('disconnect'));
  });

  test('cancel for unknown runs is not found', async () => {
    const response = await fetch(`${base}/runweave/v1/agents/echo/runs/${randomUUID()}/cancel`, { method: 'POST' });
    assert.equal(response.status, 404);
  });

  test('capacity is bounded with a retryable status', async () => {
    const slow = [post('echo', envelope({ input: { text: 'busy-1', delay: 300 } })),
      post('echo', envelope({ input: { text: 'busy-2', delay: 300 } }))];
    while (!calls.includes('busy-2')) await new Promise((r) => setTimeout(r, 10));
    const busy = await post('echo', envelope());
    await Promise.all(slow);
    assert.equal(busy.status, 503);
    assert.equal(busy.headers.get('retry-after'), '1');
    assert.equal(busy.body.error.code, 'AGENT_BUSY');
  });

  test('unknown paths and methods are not found', async () => {
    assert.equal((await fetch(`${base}/elsewhere`)).status, 404);
    assert.equal((await fetch(`${base}/runweave/v1/agents`, { method: 'DELETE' })).status, 404);
  });
});
