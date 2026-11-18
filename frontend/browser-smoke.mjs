import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { delimiter, dirname, join, resolve } from 'node:path';
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
env.PYTHONPATH = [join(root, 'src'), join(root, 'examples', 'sample-agent')].join(delimiter);
env.RUNWEAVE_MOCK_RUNBOOK_DELAY_SECONDS = '1.2';
const server = spawn(python, ['-m', 'runweave.cli', 'serve', '--demo', '--port', String(port), '--agent', 'sample_agent:text_report'],
  { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
let output = '';
server.stdout.on('data', (chunk) => { output = (output + chunk).slice(-16384); });
server.stderr.on('data', (chunk) => { output = (output + chunk).slice(-16384); });
const base = `http://127.0.0.1:${port}`;
// A real HTTP agent (the contract fixture) for the Connect flow.
const httpAgent = spawn(python, [join(root, 'tests', 'contract', 'python_agent.py')], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
const httpAgentUrl = new Promise((resolveUrl, rejectUrl) => {
  let buffer = '';
  httpAgent.stdout.on('data', (chunk) => {
    buffer += chunk;
    const match = /RUNWEAVE_AGENT_READY (\S+)/.exec(buffer);
    if (match) resolveUrl(match[1]);
  });
  httpAgent.on('exit', () => rejectUrl(new Error('The HTTP fixture agent exited')));
  setTimeout(() => rejectUrl(new Error('The HTTP fixture agent did not start')), 30000);
});
const hostile = '<img src=x onerror="window.__injected=true">';
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

async function newPage(context) {
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error' && !/status of (4|5)\d\d/.test(message.text())) errors.push(message.text()); });
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (event) => window.__cspViolations.push(event.violatedDirective));
  });
  return { page, errors };
}

async function noOverflow(page, label) {
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${label} overflows at 390px`);
  await page.setViewportSize({ width: 1360, height: 980 });
}

try {
  await waitForServer();
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  const context = await browser.newContext({ viewport: { width: 1360, height: 980 }, colorScheme: 'light' });
  const { page, errors } = await newPage(context);
  let admissions = 0;
  page.on('response', (response) => { if (response.request().method() === 'POST' && response.url() === `${base}/v1/runs` && response.status() === 202) admissions++; });

  // Agents: arriving from another site by link, both agents listed.
  await page.route('http://onboarding.invalid/**', (route) => route.fulfill({ contentType: 'text/html', body: `<a href="${base}/">Open workspace</a>` }));
  await page.goto('http://onboarding.invalid/');
  await page.getByRole('link', { name: 'Open workspace', exact: true }).click();
  await page.getByRole('heading', { name: 'Agents', level: 1 }).waitFor();
  await page.locator('.connection').getByText('Offline demo').waitFor();
  await page.getByRole('link', { name: 'text-report', exact: true }).waitFor();
  await page.getByText('sample_agent:text_report', { exact: true }).waitFor();
  await page.screenshot({ path: join(artifacts, 'agents-light.png'), fullPage: true });
  await noOverflow(page, 'agents');

  // Built-in triage: server-side validation errors land on the field, no run is created.
  await page.getByRole('link', { name: 'New run for incident-triage' }).click();
  await page.getByRole('heading', { name: 'New run', level: 1 }).waitFor();
  assert.equal(await page.getByText('Recall saved evidence', { exact: true }).count(), 0);
  await page.getByRole('button', { name: 'Load example' }).click();
  await page.getByRole('textbox', { name: 'Service', exact: true }).fill('INVALID');
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.locator('#run-service-error').waitFor();
  assert.equal(await page.getByRole('textbox', { name: 'Service', exact: true }).getAttribute('aria-invalid'), 'true');
  assert.equal(admissions, 0);

  // A real run: live updates, findings and evidence, untrusted text stays text.
  await page.getByRole('textbox', { name: 'Service', exact: true }).fill('payments');
  await page.getByRole('textbox', { name: 'Description', exact: true }).fill(`${hostile} Synthetic latency spike.`);
  const live = page.waitForRequest((request) => request.url().endsWith('/events'));
  await page.getByRole('button', { name: 'Start run' }).click();
  await live;
  await page.locator('.run-state .status.succeeded').waitFor({ timeout: 15000 });
  await page.getByText('mock-health:payments', { exact: true }).waitFor();
  await page.getByText('mock-runbook:triage-v1', { exact: true }).waitFor();
  for (const step of ['Model decision', 'Tool call', 'Validate result']) await page.getByText(step, { exact: true }).first().waitFor();
  assert.equal(await page.locator('.findings img, pre img').count(), 0);
  assert.equal(await page.evaluate(() => window.__injected), undefined);
  assert.equal(admissions, 1);
  const runUrl = page.url();
  const runId = runUrl.split('/').at(-1);
  await page.screenshot({ path: join(artifacts, 'run-result-desktop.png'), fullPage: true });
  await page.reload();
  await page.locator('.run-state .status.succeeded').waitFor();
  await noOverflow(page, 'run detail');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: join(artifacts, 'run-result-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1360, height: 980 });

  // Runs list and old ?run= links.
  await page.goto(`${base}/#/runs`);
  await page.getByRole('link', { name: `Open run ${runId}` }).waitFor();
  await noOverflow(page, 'runs');
  await page.goto(`${base}/?run=${runId}`);
  await page.locator('.run-state .status.succeeded').waitFor();
  assert.ok(page.url().endsWith(`/#/runs/${runId}`), page.url());

  // Cancellation of a running run.
  await page.goto(`${base}/#/runs/new?agent=incident-triage`);
  await page.getByRole('textbox', { name: 'Service', exact: true }).fill('payments');
  await page.getByRole('textbox', { name: 'Description', exact: true }).fill('Cancel this synthetic investigation.');
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.locator('.run-state .status.cancelled').waitFor();

  // Admission failure and expired runs.
  await page.goto(`${base}/#/runs/new?agent=incident-triage`);
  await page.getByRole('button', { name: 'Load example' }).click();
  await page.route('**/v1/runs', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: { code: 'QUEUE_FULL', message: 'Queue is full. Try again shortly.' } } })
    : route.continue());
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.getByRole('alert').filter({ hasText: 'Queue is full' }).waitFor();
  await page.unroute('**/v1/runs');
  await page.goto(`${base}/#/runs/00000000-0000-4000-8000-000000000000`);
  await page.getByText('This run expired or is unavailable.', { exact: true }).waitFor();

  // An SDK agent: generated form, step timeline, JSON output and re-run.
  await page.goto(`${base}/#/runs/new?agent=text-report`);
  await page.getByRole('textbox', { name: 'Text', exact: true }).fill(`Hello there. ${hostile} Bye.`);
  await page.getByRole('textbox', { name: 'Keywords', exact: true }).fill('hello\nbye');
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.locator('.run-state .status.succeeded').waitFor({ timeout: 15000 });
  await page.getByText('Tokenize', { exact: true }).waitFor();
  await page.getByText('Count keywords', { exact: true }).waitFor();
  await page.locator('#output-title').waitFor();
  assert.match(await page.locator('section[aria-labelledby="output-title"] .json-block').innerText(), /"keyword_hits"/);
  assert.equal(await page.locator('pre img').count(), 0);
  await page.screenshot({ path: join(artifacts, 'sdk-run-light.png'), fullPage: true });
  await page.getByRole('link', { name: 'Re-run' }).click();
  await page.getByRole('textbox', { name: 'Text', exact: true }).waitFor();
  assert.equal(await page.getByRole('textbox', { name: 'Text', exact: true }).inputValue(), `Hello there. ${hostile} Bye.`);

  // Setup: startup-only registration, then detection of the already loaded agent.
  await page.goto(`${base}/#/setup`);
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel('Agent function (module:attribute)').fill('sample_agent:text_report');
  await page.getByText('runweave serve --agent sample_agent:text_report', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'I restarted RunWeave' }).click();
  await page.getByRole('link', { name: 'Run a smoke test' }).waitFor();

  // Connect an HTTP agent from the UI, run it, then remove it.
  const agentUrl = await httpAgentUrl;
  await page.goto(`${base}/#/setup`);
  await page.getByRole('radio', { name: /HTTP endpoint/ }).check();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('textbox', { name: 'Agent URL', exact: true }).fill('http://169.254.169.254/latest');
  await page.getByRole('button', { name: 'Test connection' }).click();
  await page.getByRole('alert').filter({ hasText: 'loopback' }).waitFor();
  await page.getByRole('textbox', { name: 'Agent URL', exact: true }).fill(`${agentUrl}/`);
  await page.getByRole('button', { name: 'Test connection' }).click();
  await page.getByRole('cell', { name: 'contract-echo', exact: true }).waitFor();
  await page.getByText(`runweave serve --http-agent ${agentUrl}`, { exact: true }).waitFor();
  await noOverflow(page, 'connect http');
  await page.getByRole('button', { name: 'Register agents' }).click();
  await page.getByRole('link', { name: 'Run contract-echo' }).click();
  await page.getByRole('textbox', { name: 'Text', exact: true }).fill(`remote ${hostile}`);
  await page.getByRole('button', { name: 'Start run' }).click();
  await page.locator('.run-state .status.succeeded').waitFor();
  await page.getByText('Echo', { exact: true }).first().waitFor();
  await page.locator('section[aria-labelledby="output-title"] .json-block').filter({ hasText: 'remote' }).waitFor();
  assert.equal(await page.evaluate(() => window.__injected), undefined);
  await page.goto(`${base}/#/agents`);
  const row = page.getByRole('row').filter({ has: page.getByRole('link', { name: 'contract-echo', exact: true }) });
  await row.getByText('HTTP endpoint').waitFor();
  await page.screenshot({ path: join(artifacts, 'agents-http.png'), fullPage: true });
  await row.getByRole('button', { name: 'Remove' }).click();
  await row.getByRole('button', { name: 'Confirm remove' }).click();
  await page.getByRole('link', { name: 'contract-echo', exact: true }).waitFor({ state: 'detached' });

  // Durable approval bindings against an intercepted fixture.
  const fixtureId = '11111111-1111-4111-8111-111111111111';
  const approval = {
    id: 'approval-fixture', call: { id: 'sandbox-ticket-v1', arguments: '{"service":"payments","summary":"<b>Untrusted ticket text</b>"}' },
    argument_digest: 'bound-digest', policy_version: 'sandbox-ticket-v1', expires_at: '2099-01-01T00:00:00Z', status: 'pending',
  };
  await page.route('**/v1/workspace', (route) => route.fulfill({ json: { provider: 'fake', model: 'fake-v1', workflow: 'baseline', durable: true } }));
  await page.route(`**/v1/runs/${fixtureId}/events`, (route) => route.fulfill({ status: 503, json: { detail: { code: 'EVENT_STREAMS_BUSY', message: 'busy' } } }));
  let decision;
  let expectedDecision = 'approve';
  await page.route(`**/v1/runs/${fixtureId}`, (route) => route.fulfill({ json: {
    id: fixtureId, agent: 'incident-triage', agent_version: 'baseline', status_url: `/v1/runs/${fixtureId}`,
    status: decision === 'reject' ? 'FAILED' : decision ? 'SUCCEEDED' : 'WAITING_APPROVAL',
    submitted_at: '2026-11-18T00:00:00Z', started_at: '2026-11-18T00:00:01Z', finished_at: decision ? '2026-11-18T00:00:02Z' : null,
    input: { service: 'payments', description: 'Fixture', create_ticket: true }, steps: [],
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
  await page.goto(`${base}/#/runs/${fixtureId}`);
  await page.getByRole('button', { name: 'Approve ticket', exact: true }).waitFor();
  assert.equal(await page.locator('pre b').count(), 0);
  await page.getByText('Refreshing every second', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Approve ticket', exact: true }).click();
  await page.locator('.run-state .status.succeeded').waitFor();
  assert.equal(decision, 'approve');
  decision = undefined;
  expectedDecision = 'reject';
  await page.reload();
  await page.getByRole('button', { name: 'Reject', exact: true }).click();
  await page.locator('.run-state .status.failed').waitFor();
  await page.getByRole('alert').filter({ hasText: 'Approval rejected.' }).waitFor();
  assert.equal(decision, 'reject');
  await page.goto(`${base}/#/runs/new?agent=incident-triage`);
  await page.getByText('Recall saved evidence', { exact: true }).waitFor();
  assert.deepEqual(await page.evaluate(() => window.__cspViolations), []);
  assert.deepEqual(errors, []);
  await context.close();

  // Dark theme renders the same screens.
  const dark = await browser.newContext({ viewport: { width: 1360, height: 980 }, colorScheme: 'dark' });
  const darkPage = await newPage(dark);
  await darkPage.page.goto(`${base}/#/agents`);
  await darkPage.page.getByRole('heading', { name: 'Agents', level: 1 }).waitFor();
  await darkPage.page.screenshot({ path: join(artifacts, 'agents-dark.png'), fullPage: true });
  await darkPage.page.goto(`${base}/#/runs/${runId}`);
  await darkPage.page.locator('.run-state .status.succeeded').waitFor();
  await darkPage.page.screenshot({ path: join(artifacts, 'run-result-dark.png'), fullPage: true });
  assert.deepEqual(await darkPage.page.evaluate(() => window.__cspViolations), []);
  assert.deepEqual(darkPage.errors, []);
  await dark.close();

  console.log('Browser checks passed: agents, generated forms, server validation, live run updates, findings, SDK agent timeline and re-run, runs list, legacy links, cancellation, queue errors, expired IDs, setup detection, HTTP agent connect/run/remove, approval bindings, mobile overflow, safe text, both themes.');
  console.log(`Screenshots: ${artifacts}`);
} finally {
  if (browser) await browser.close();
  if (httpAgent.exitCode === null) httpAgent.kill();
  if (server.exitCode === null) {
    const closed = once(server, 'exit');
    server.kill();
    await closed;
  }
}
