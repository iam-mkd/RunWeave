import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, test, vi } from 'vitest';
import { decideApproval, getApproval, isApprovalId, listApprovals } from '../src/api.js';
import { productionSections } from '../src/navigation.js';
import { href, parseRoute } from '../src/router.js';
import { ApprovalDetail, ApprovalInbox, NotDurable, expiresText } from '../src/screens/ApprovalsScreen.jsx';
import { AgentApprovalNotice } from '../src/screens/RunDetailScreen.jsx';

const render = (element) => renderToStaticMarkup(element);
const hostile = '<img src=x onerror="alert(1)">';
const id = '6f1d2c3b-4a59-4e8f-9d0a-1b2c3d4e5f60';
const runId = '71bb412e-77f9-42a1-a8bf-751d39ecefad';
const approval = {
  id, run_id: runId, tenant: 'local', agent: 'refund-request', action: 'issue_refund',
  arguments: { order: 'A-17', note: hostile, amount: 40 },
  argument_digest: 'a'.repeat(64), policy_version: 'agent:refund-request@1.0.0', status: 'pending',
  created_at: '2026-11-19T10:00:00+00:00', expires_at: '2026-11-19T11:00:00+00:00', decided_by: null, decided_at: null,
};
afterEach(() => vi.restoreAllMocks());

describe('approvals API', () => {
  test('lists, reads and decides with the exact binding', async () => {
    const calls = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
      calls.push([path, options.method, options.body]);
      return new Response(JSON.stringify({ approvals: [], durable: true }), { status: 200 });
    });
    await listApprovals();
    await listApprovals('all');
    await getApproval(id);
    await decideApproval(approval, 'approve');
    assert.deepEqual(calls, [
      ['/v1/approvals?status=pending', 'GET', undefined],
      ['/v1/approvals?status=all', 'GET', undefined],
      [`/v1/approvals/${id}`, 'GET', undefined],
      [`/v1/approvals/${id}`, 'POST', JSON.stringify({
        decision: 'approve', argument_digest: approval.argument_digest,
        policy_version: approval.policy_version, expires_at: approval.expires_at,
      })],
    ]);
    await assert.rejects(getApproval('../runs'), /Invalid approval/);
    await assert.rejects(decideApproval({ ...approval, id: 'x' }, 'approve'), /Invalid approval/);
    await assert.rejects(decideApproval(approval, 'maybe'), /Unknown decision/);
    assert.ok(isApprovalId(id) && !isApprovalId(`${id}/x`));
  });
});

describe('navigation and route', () => {
  test('Approvals is a shipped section with an optional run filter', () => {
    assert.ok(productionSections.some((section) => section.id === 'approvals' && section.href === '#/approvals'));
    assert.deepEqual(parseRoute('#/approvals', ''), { screen: 'approvals', params: {} });
    assert.deepEqual(parseRoute(`#/approvals?run=${runId}`, ''), { screen: 'approvals', params: { run: runId } });
    assert.deepEqual(parseRoute('#/approvals?run=../x', ''), { screen: 'approvals', params: {} });
    assert.equal(href({ screen: 'approvals', params: { run: runId } }), `#/approvals?run=${runId}`);
    assert.equal(href({ screen: 'approvals' }), '#/approvals');
  });
});

describe('approval views', () => {
  test('the detail shows the exact arguments, binding and expiry, escaping agent text', () => {
    const html = render(<ApprovalDetail approval={approval} now={Date.parse('2026-11-19T10:30:00Z')} onDecide={() => {}} />);
    assert.match(html, /issue_refund/);
    assert.match(html, /&quot;order&quot;: &quot;A-17&quot;/);
    assert.match(html, /aaaaaaaa/);
    assert.match(html, /agent:refund-request@1\.0\.0/);
    assert.match(html, /in 30 min/);
    assert.match(html, />Approve</);
    assert.match(html, />Reject</);
    assert.match(html, /idempotent/);
    assert.match(html, new RegExp(`href="#/runs/${runId}"`));
    assert.doesNotMatch(html, /<img/);
  });

  test('a decided request offers no decision', () => {
    const html = render(<ApprovalDetail approval={{ ...approval, status: 'approved', decided_by: 'developer', decided_at: '2026-11-19T10:31:00+00:00' }}
      now={Date.parse('2026-11-19T10:40:00Z')} onDecide={() => {}} />);
    assert.match(html, /Approved/);
    assert.match(html, /developer/);
    assert.doesNotMatch(html, />Approve</);
  });

  test('a decision asks for confirmation first', () => {
    const html = render(<ApprovalDetail approval={approval} now={Date.parse('2026-11-19T10:30:00Z')} confirming="approve" onDecide={() => {}} />);
    assert.match(html, /Confirm approve/);
    assert.match(html, />Cancel</);
  });

  test('the inbox lists requests and the memory-mode notice explains durable mode', () => {
    const html = render(<ApprovalInbox approvals={[approval, { ...approval, id: 'b'.repeat(8), action: hostile }]} selected={id}
      onSelect={() => {}} now={Date.parse('2026-11-19T10:30:00Z')} />);
    assert.match(html, /refund-request/);
    assert.match(html, /2 pending/);
    assert.doesNotMatch(html, /<img/);
    assert.match(render(<NotDurable />), /RUNWEAVE_DATABASE_URL/);
  });

  test('expiry reads naturally', () => {
    const now = Date.parse('2026-11-19T10:00:00Z');
    assert.equal(expiresText('2026-11-19T10:00:30Z', now), 'in 30 s');
    assert.equal(expiresText('2026-11-19T10:45:00Z', now), 'in 45 min');
    assert.equal(expiresText('2026-11-19T12:30:00Z', now), 'in 2 h 30 min');
    assert.equal(expiresText('2026-11-19T09:59:00Z', now), 'Expired');
  });

  test('a run waiting on an agent approval links to the inbox', () => {
    const html = render(<AgentApprovalNotice runId={runId} />);
    assert.match(html, new RegExp(`href="#/approvals\\?run=${runId}"`));
  });
});
