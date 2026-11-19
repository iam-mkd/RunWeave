import assert from 'node:assert/strict';
import { test } from 'vitest';
import { href, parseRoute } from '../src/router.js';

const id = '71bb412e-77f9-42a1-a8bf-751d39ecefad';

test('hash routes map to screens with validated parameters', () => {
  assert.deepEqual(parseRoute('', ''), { screen: 'agents', params: {} });
  assert.deepEqual(parseRoute('#/agents', ''), { screen: 'agents', params: {} });
  assert.deepEqual(parseRoute('#/setup', ''), { screen: 'setup', params: {} });
  assert.deepEqual(parseRoute('#/runs?agent=text-report&status=FAILED', ''),
    { screen: 'runs', params: { agent: 'text-report', status: 'FAILED' } });
  assert.deepEqual(parseRoute(`#/runs/${id}`, ''), { screen: 'run', id, params: {} });
  assert.deepEqual(parseRoute('#/runs/new?agent=text-report', ''), { screen: 'new-run', params: { agent: 'text-report' } });
  assert.deepEqual(parseRoute(`#/runs/new?agent=echo&from=${id}`, ''), { screen: 'new-run', params: { agent: 'echo', from: id } });
  assert.deepEqual(parseRoute('#/runs/new?from=../x', ''), { screen: 'new-run', params: {} });
});

test('invalid identifiers and filters are dropped, not trusted', () => {
  assert.deepEqual(parseRoute('#/runs/../../etc', ''), { screen: 'run', id: null, params: {} });
  assert.deepEqual(parseRoute('#/runs?agent=<b>x</b>&status=EVIL', ''), { screen: 'runs', params: {} });
  assert.deepEqual(parseRoute('#/nowhere', ''), { screen: 'agents', params: {} });
});

test('legacy ?run= links still open the run', () => {
  assert.deepEqual(parseRoute('', `?run=${id}`), { screen: 'run', id, params: {} });
  assert.deepEqual(parseRoute('', '?run=not-a-uuid'), { screen: 'agents', params: {} });
});

test('links are built from validated parts', () => {
  assert.equal(href({ screen: 'agents' }), '#/agents');
  assert.equal(href({ screen: 'run', id }), `#/runs/${id}`);
  assert.equal(href({ screen: 'runs', params: { agent: 'text-report', status: 'SUCCEEDED' } }), '#/runs?agent=text-report&status=SUCCEEDED');
  assert.equal(href({ screen: 'new-run', params: { agent: 'text-report' } }), '#/runs/new?agent=text-report');
  assert.equal(href({ screen: 'runs', params: { agent: 'Bad Name' } }), '#/runs');
  assert.throws(() => href({ screen: 'run', id: '../x' }));
});

test('settings has its own route', () => {
  assert.deepEqual(parseRoute('#/settings', ''), { screen: 'settings', params: {} });
  assert.equal(href({ screen: 'settings' }), '#/settings');
});
