// ctx.requestApproval against a stand-in for RunWeave's approval callback.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { after, before, describe, test } from 'node:test';
import { ApprovalExpired, defineAgent, serveAgents } from '../dist/index.js';

const token = `rwp_${'t'.repeat(40)}`;
const inputSchema = { type: 'object', properties: { order: { type: 'string' } }, required: ['order'] };
const outputSchema = { type: 'object' };
const received = [];
let script = [];

const approver = defineAgent({
  name: 'approver', version: '2.0.0', inputSchema, outputSchema,
  async run(input, ctx) {
    const decision = await ctx.requestApproval('issue_refund', { order: input.order }, { expiresIn: 60 });
    return { approved: decision.approved, status: decision.status, decidedBy: decision.decidedBy, remaining: ctx.remainingMs() };
  },
});

let agents;
let callback;

before(async () => {
  // Answers each POST with the next scripted status (after an optional delay).
  callback = createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', async () => {
      received.push({ auth: request.headers.authorization, body: JSON.parse(body) });
      const [status, delay = 0, http = 200] = script.shift() ?? ['pending', 50];
      await new Promise((resolve) => setTimeout(resolve, delay));
      const payload = http === 200
        ? { approval: { id: randomUUID(), status, decided_by: status === 'pending' ? null : 'reviewer', decided_at: null, expires_at: '2099-01-01T00:00:00Z' }, deadline_ms: 1000 }
        : { error: { code: status } };
      response.writeHead(http, { 'Content-Type': 'application/json' }).end(JSON.stringify(payload));
    });
  });
  await new Promise((resolve) => callback.listen(0, '127.0.0.1', resolve));
  agents = await serveAgents([approver], { port: 0 });
});

after(async () => {
  await agents.close();
  await new Promise((resolve) => callback.close(resolve));
});

function run(deadlineMs, approvals = true) {
  const { port } = callback.address();
  return fetch(`${agents.url}/runweave/v1/agents/approver/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': randomUUID() },
    body: JSON.stringify({
      run_id: randomUUID(), attempt: 1, input: { order: 'A-17' }, deadline_ms: deadlineMs,
      proxy: { token, openai_base_url: 'http://127.0.0.1:1/llm/v1', anthropic_base_url: 'http://127.0.0.1:1/llm' },
      approvals_url: approvals ? `http://127.0.0.1:${port}/agent/v1/approvals` : null,
    }),
  }).then(async (response) => ({ status: response.status, body: await response.json() }));
}

describe('ctx.requestApproval', () => {
  test('waits through pending answers, then returns the decision', async () => {
    received.length = 0;
    script = [['pending', 50], ['pending', 50], ['approved', 0]];
    const { status, body } = await run(5000);
    assert.equal(status, 200, JSON.stringify(body));
    assert.equal(body.output.approved, true);
    assert.equal(body.output.decidedBy, 'reviewer');
    assert.equal(received.length, 3);
    assert.equal(received[0].auth, `Bearer ${token}`);
    assert.deepEqual(received[0].body.arguments, { order: 'A-17' });
    assert.equal(received[0].body.action, 'issue_refund');
    assert.equal(received[0].body.expires_in, 60);
    assert.ok(received.every((r) => JSON.stringify(r.body) === JSON.stringify(received[0].body)));
  });

  test('a rejection is returned, not thrown', async () => {
    script = [['rejected', 0]];
    const { status, body } = await run(5000);
    assert.equal(status, 200);
    assert.deepEqual([body.output.approved, body.output.status], [false, 'rejected']);
  });

  test('waiting does not use up the run deadline', async () => {
    script = [['pending', 400], ['pending', 400], ['approved', 0]];
    const { status, body } = await run(300);
    assert.equal(status, 200, JSON.stringify(body));
    assert.ok(body.output.remaining > 0 && body.output.remaining <= 300);
  });

  test('expiry fails the run with APPROVAL_EXPIRED', async () => {
    script = [['expired', 0]];
    const { status, body } = await run(5000);
    assert.equal(status, 500);
    assert.equal(body.error.code, 'APPROVAL_EXPIRED');
    assert.equal(new ApprovalExpired().code, 'APPROVAL_EXPIRED');
  });

  test('a refused request reports its code', async () => {
    script = [['APPROVAL_CAPACITY_EXCEEDED', 0, 409]];
    const { status, body } = await run(5000);
    assert.equal(status, 500);
    assert.equal(body.error.code, 'APPROVAL_CAPACITY_EXCEEDED');
  });

  test('without durable RunWeave the call is refused', async () => {
    const { status, body } = await run(5000, false);
    assert.equal(status, 500);
    assert.equal(body.error.code, 'DURABLE_APPROVAL_REQUIRED');
  });
});
