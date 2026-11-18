import assert from 'node:assert/strict';
import { afterEach, test, vi } from 'vitest';
import { api, approvalBody, checkHttpAgent, isRunId, registerHttpAgent, removeAgent, terminal } from '../src/api.js';

const id = '71bb412e-77f9-42a1-a8bf-751d39ecefad';

afterEach(() => vi.restoreAllMocks());

test('run IDs are validated before they reach a URL', () => {
  assert.equal(isRunId(id), true);
  assert.equal(isRunId('../other'), false);
  assert.equal(terminal.has('WAITING_APPROVAL'), false);
});

test('approval posts preserve the server bindings without argument overrides', () => {
  const approval = { id, call: { id: 'call-1', arguments: 'untrusted text' }, argument_digest: 'digest', policy_version: 'policy', expires_at: 'date' };
  assert.deepEqual(approvalBody(approval, 'approve'), {
    approval_id: id, call_id: 'call-1', argument_digest: 'digest', policy_version: 'policy', expires_at: 'date', decision: 'approve',
  });
});

test('API distinguishes mutations, propagates aborts and preserves admission errors', async () => {
  const controller = new AbortController();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
    assert.equal(path, '/v1/runs');
    assert.equal(options.method, 'POST');
    assert.equal(options.signal, controller.signal);
    assert.deepEqual(JSON.parse(options.body), { service: 'payments' });
    return new Response(JSON.stringify({ detail: { code: 'QUEUE_FULL', message: 'Queue is full' } }), { status: 503 });
  });
  await assert.rejects(api('/v1/runs', { data: { service: 'payments' }, signal: controller.signal }), (error) => error.status === 503 && error.message === 'Queue is full');
});

test('HTTP agent registration posts the URL and removal validates the agent name', async () => {
  const calls = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
    calls.push([path, options.method, options.body]);
    if (options.method === 'DELETE') return new Response(null, { status: 204 });
    return new Response(JSON.stringify({ url: 'http://127.0.0.1:9100', agents: [] }), { status: 200 });
  });
  await checkHttpAgent('http://127.0.0.1:9100/');
  await registerHttpAgent('http://127.0.0.1:9100');
  assert.equal(await removeAgent('remote-echo'), null);
  await assert.rejects(removeAgent('../runs'), /Invalid agent name/);
  assert.deepEqual(calls, [
    ['/v1/agents/http/check', 'POST', JSON.stringify({ url: 'http://127.0.0.1:9100/' })],
    ['/v1/agents/http', 'POST', JSON.stringify({ url: 'http://127.0.0.1:9100' })],
    ['/v1/agents/remote-echo', 'DELETE', undefined],
  ]);
});
