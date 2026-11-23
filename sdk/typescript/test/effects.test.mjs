// ctx.effect: a side-effecting call routed through RunWeave, and a lost response after the write.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { EffectResponseLost } from '../dist/index.js';
import { createContext, Redactor } from '../dist/agent.js';
import { effectLost, operationDigest, parseEffectFaults, resourceDigest } from '../dist/effects.js';

const LOST_WRITES = { seed: 11, faults: [{ kind: 'response_lost', probability: 0.5 }] };

// Pinned from the Python implementation (runweave.proxy.faults / runweave.agent_effects), so
// an agent draws the same faults and digests whichever SDK it uses.
const PYTHON_LOST_POSITIONS = [1, 4, 7, 10, 11, 12, 13, 14, 15, 17, 20, 21, 26, 27, 28, 29, 30, 31, 34, 35, 36];

test('the effect draw matches the Python implementation', () => {
  const faults = parseEffectFaults(LOST_WRITES);
  const lost = [...Array(40).keys()].filter((n) => effectLost(faults, n, 'create_ticket'));
  assert.deepEqual(lost, PYTHON_LOST_POSITIONS);
});

test('digests match the Python implementation', () => {
  assert.equal(operationDigest('create_ticket', 'order-17'), '15e813368ba9621beb82958e6144d335');
  assert.equal(operationDigest('create_ticket', null), '2f919fd72b2353ada327afd85792f5dd');
  assert.equal(resourceDigest('T-1'), 'fcc5fcd048d1b5abb744a1c37167c3d6');
  assert.equal(resourceDigest({ b: 1, a: ['é', 2.5, null, true] }), 'b3d0c9ef1fa0d1d7c663d5f46e7d86b3');
});

test('an effects filter limits which effects lose responses', () => {
  const faults = parseEffectFaults({ seed: 0, faults: [{ kind: 'response_lost', probability: 1, effects: ['create_ticket'] }] });
  assert.equal(effectLost(faults, 0, 'create_ticket'), true);
  assert.equal(effectLost(faults, 0, 'send_email'), false);
});

test('invalid effect fault plans are rejected', () => {
  for (const bad of [
    { seed: -1, faults: [{ kind: 'response_lost', probability: 0.5 }] },
    { seed: 1, faults: [] },
    { seed: 1, faults: [{ kind: 'rate_limit', probability: 0.5 }] },
    { seed: 1, faults: [{ kind: 'response_lost', probability: 2 }] },
    'nope',
  ]) assert.equal(parseEffectFaults(bad), undefined, JSON.stringify(bad));
  assert.equal(parseEffectFaults(undefined), null);
  assert.equal(parseEffectFaults(null), null);
});

function context(effectFaults) {
  return createContext({
    runId: '00000000-0000-4000-8000-000000000001', agent: 'test', attempt: 1, maxSteps: 10,
    clock: { remainingMs: () => 10_000, pause() {}, resume() {} },
    signal: new AbortController().signal, redactor: new Redactor([]), logger: () => {},
    proxy: null, approvalsUrl: null, effectFaults,
  });
}

function service() {
  const tickets = [];
  return {
    tickets,
    async create(key) {
      const found = key === null ? undefined : tickets.find((t) => t.key === key);
      if (found) return { id: found.id };
      const ticket = { id: `T-${tickets.length + 1}`, key, title: 'secret title' };
      tickets.push(ticket);
      return { id: ticket.id, title: ticket.title };
    },
  };
}

const LOSE_FIRST = { seed: 0, faults: [{ kind: 'response_lost', probability: 1 }] };

test('a lost response comes after the write, and a keyed retry writes once', async () => {
  const downstream = service();
  // probability 1 loses every call; the agent gives up after the first try here.
  const { context: ctx, steps } = context(parseEffectFaults(LOSE_FIRST));
  await assert.rejects(
    ctx.effect('create_ticket', () => downstream.create('key-9'), { key: 'key-9', resource: (t) => t.id }),
    (error) => error instanceof EffectResponseLost && error.code === 'EFFECT_RESPONSE_LOST',
  );
  assert.equal(downstream.tickets.length, 1);
  const [step] = steps;
  assert.equal(step.kind, 'effect');
  assert.equal(step.effect, 'create_ticket');
  assert.equal(step.status, 'failed');
  assert.equal(step.fault, 'response_lost');
  assert.match(step.operation, /^[0-9a-f]{32}$/);
  assert.match(step.resource, /^[0-9a-f]{32}$/);
  const text = JSON.stringify(steps);
  assert.ok(!text.includes('key-9') && !text.includes('T-1') && !text.includes('secret title'));
});

test('without effect faults an effect just runs and is recorded', async () => {
  const downstream = service();
  const { context: ctx, steps } = context(null);
  const ticket = await ctx.effect('create_ticket', () => downstream.create(null));
  assert.equal(ticket.id, 'T-1');
  assert.equal(steps[0].status, 'succeeded');
  assert.equal(steps[0].fault, null);
});

test('a failing call records no resource', async () => {
  const { context: ctx, steps } = context(null);
  await assert.rejects(ctx.effect('create_ticket', async () => { throw new Error('refused'); }), /refused/);
  assert.equal(steps[0].status, 'failed');
  assert.equal(steps[0].resource, null);
});

test('effect names are validated', async () => {
  const { context: ctx } = context(null);
  await assert.rejects(ctx.effect('Create Ticket!', async () => 1), /Effect name/);
});
