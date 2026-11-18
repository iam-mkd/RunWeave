import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = dirname(fileURLToPath(import.meta.url));
const root = resolve(frontend, '..');
const artifacts = join(frontend, 'test-results');
await mkdir(artifacts, { recursive: true });
const socket = createServer();
socket.listen(0, '127.0.0.1');
await once(socket, 'listening');
const port = socket.address().port;
await new Promise((done) => socket.close(done));
const python = process.env.RUNWEAVE_TEST_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('RUNWEAVE_')));
env.PYTHONPATH = join(root, 'src');
env.RUNWEAVE_MOCK_RUNBOOK_DELAY_SECONDS = '1.2';
const server = spawn(python, ['-m', 'runweave.workspace', '--demo', '--port', String(port)], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
let output = '';
server.stdout.on('data', (chunk) => { output = (output + chunk).slice(-16384); });
server.stderr.on('data', (chunk) => { output = (output + chunk).slice(-16384); });
const base = `http://127.0.0.1:${port}`;
let browser;

async function waitForServer() {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(`Server exited: ${output}`);
    try {
      if ((await fetch(`${base}/health/ready`)).ok) return;
    } catch { /* The child is still binding its socket. */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error(`Server startup timed out: ${output}`);
}

try {
  await waitForServer();
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  const context = await browser.newContext({ viewport: { width: 1360, height: 980 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (event) => window.__cspViolations.push(event.violatedDirective));
  });
  await page.route('http://onboarding.invalid/**', (route) => route.fulfill({ contentType: 'text/html', body: `<a href="${base}/">Open workspace</a>` }));
  await page.goto('http://onboarding.invalid/');
  await page.getByRole('link', { name: 'Open workspace', exact: true }).click();
  await page.getByRole('heading', { name: 'Run an investigation.' }).waitFor();
  await page.locator('.connection').getByText('Offline demo').waitFor();
  assert.equal(await page.getByText('Recall saved evidence', { exact: true }).count(), 0);
  await page.screenshot({ path: join(artifacts, 'workspace-desktop.png'), fullPage: true });

  await page.getByRole('button', { name: 'Load example' }).click();
  await page.getByLabel('Service', { exact: true }).fill('INVALID');
  let admissions = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && request.url() === `${base}/v1/runs`) admissions++; });
  await page.getByRole('button', { name: 'Start run' }).click();
  assert.equal(await page.getByLabel('Service', { exact: true }).evaluate((input) => input.validity.patternMismatch), true);
  assert.equal(admissions, 0);
  await page.getByLabel('Service', { exact: true }).fill('payments-api');
  assert.equal(await page.getByLabel('Service', { exact: true }).evaluate((input) => input.checkValidity()), true);
  await page.getByLabel('Service', { exact: true }).fill('payments');
  await page.getByLabel('Incident description', { exact: true }).fill('<img src=x onerror="window.__injected=true"> Synthetic latency spike.');
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.locator('.status.succeeded').waitFor({ timeout: 15000 });
  await page.getByText('mock-health:payments', { exact: true }).waitFor();
  await page.getByText('mock-runbook:triage-v1', { exact: true }).waitFor();
  assert.equal(await page.locator('.findings img').count(), 0);
  assert.equal(await page.evaluate(() => window.__injected), undefined);
  assert.equal(admissions, 1);
  const successUrl = page.url();
  await page.screenshot({ path: join(artifacts, 'run-result-desktop.png'), fullPage: true });
  await page.reload();
  await page.locator('.status.succeeded').waitFor();
  assert.equal(await page.locator('.recent li').count(), 1);

  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: join(artifacts, 'run-result-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1360, height: 980 });
  await page.getByLabel('Service', { exact: true }).fill('payments');
  await page.getByLabel('Incident description', { exact: true }).fill('Cancel this synthetic investigation.');
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.locator('.status.cancelled').waitFor();

  await page.route('**/v1/runs', (route) => route.fulfill({ status: 503, json: { detail: { code: 'QUEUE_FULL', message: 'Queue is full. Try again shortly.' } } }));
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.getByRole('alert').filter({ hasText: 'Queue is full' }).waitFor();
  await page.unroute('**/v1/runs');
  await page.goto(`${base}/?run=00000000-0000-4000-8000-000000000000`);
  await page.getByText('This run expired or is unavailable.', { exact: true }).waitFor();

  const fixtureId = '11111111-1111-4111-8111-111111111111';
  const approval = {
    id: 'approval-fixture', call: { id: 'sandbox-ticket-v1', arguments: '{"service":"payments","summary":"<b>Untrusted ticket text</b>"}' },
    argument_digest: 'bound-digest', policy_version: 'sandbox-ticket-v1', expires_at: '2099-01-01T00:00:00Z', status: 'pending',
  };
  await page.route('**/v1/workspace', (route) => route.fulfill({ json: { provider: 'fake', model: 'fake-v1', workflow: 'baseline', durable: true } }));
  let decision;
  let expectedDecision = 'approve';
  await page.route(`**/v1/runs/${fixtureId}`, (route) => route.fulfill({ json: {
    id: fixtureId, status: decision === 'reject' ? 'FAILED' : decision ? 'SUCCEEDED' : 'WAITING_APPROVAL', submitted_at: '2026-11-18T00:00:00Z', finished_at: decision ? '2026-11-18T00:00:02Z' : null,
    result: decision === 'approve' ? { summary: 'Controlled approval fixture.', severity: 'low', evidence_ids: ['fixture-health', 'fixture-runbook'] } : null,
    error: decision === 'reject' ? { code: 'APPROVAL_REJECTED', message: 'Approval rejected.' } : null,
  } }));
  await page.route(`**/v1/runs/${fixtureId}/approval`, async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      assert.deepEqual(body, { approval_id: approval.id, call_id: approval.call.id, argument_digest: approval.argument_digest, policy_version: approval.policy_version, expires_at: approval.expires_at, decision: expectedDecision });
      decision = expectedDecision;
      await route.fulfill({ json: { status: decision === 'approve' ? 'approved' : 'rejected' } });
    } else await route.fulfill({ json: approval });
  });
  await page.goto(`${base}/?run=${fixtureId}`);
  await page.getByRole('button', { name: 'Approve ticket', exact: true }).waitFor();
  assert.equal(await page.locator('.findings b').count(), 0);
  await page.screenshot({ path: join(artifacts, 'approval-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'Approve ticket', exact: true }).click();
  await page.locator('.status.succeeded').waitFor();
  assert.equal(decision, 'approve');
  decision = undefined;
  expectedDecision = 'reject';
  await page.reload();
  await page.getByRole('button', { name: 'Reject', exact: true }).click();
  await page.locator('.status.failed').waitFor();
  await page.getByRole('alert').filter({ hasText: 'Approval rejected.' }).waitFor();
  assert.equal(decision, 'reject');
  assert.deepEqual(await page.evaluate(() => window.__cspViolations), []);
  assert.deepEqual(errors, []);
  await context.close();
  console.log('Browser checks passed: real API submission, results, cancellation, refresh, mobile, safe text, queue errors, expired IDs; approval binding contract verified with an intercepted fixture.');
  console.log(`Screenshots: ${artifacts}`);
  console.log(`Real run: ${successUrl}`);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    const closed = once(server, 'exit');
    server.kill();
    await closed;
  }
}
