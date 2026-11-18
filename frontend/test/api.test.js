import assert from 'node:assert/strict';
import { afterEach, test, vi } from 'vitest';
import { api, approvalBody, clearRecent, isRunId, readRecent, rememberRun, terminal } from '../src/api.js';

const id = '71bb412e-77f9-42a1-a8bf-751d39ecefad';

afterEach(() => vi.restoreAllMocks());

test('recent IDs are bounded and invalid or unavailable browser storage is harmless', () => {
  let saved = JSON.stringify([id, id, '<script>', null, 123]);
  const storage = { getItem: () => saved, setItem: (_, value) => { saved = value; } };
  assert.deepEqual(readRecent(storage), [id]);
  assert.deepEqual(rememberRun(storage, 'invalid'), [id]);
  assert.deepEqual(rememberRun(storage, id), [id]);
  for (let i = 0; i < 20; i++) rememberRun(storage, `71bb412e-77f9-42a1-a8bf-${String(i).padStart(12, '0')}`);
  assert.equal(readRecent(storage).length, 12);
  assert.deepEqual(clearRecent(storage), []);
  assert.deepEqual(readRecent(storage), []);
  const blocked = { getItem() { throw new Error('Blocked'); }, setItem() { throw new Error('Blocked'); } };
  assert.deepEqual(readRecent(blocked), []);
  assert.deepEqual(rememberRun(blocked, id), [id]);
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
