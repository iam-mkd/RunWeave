import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
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
// Keys saved in the smoke test stay in memory; never touch the developer's credential store.
env.RUNWEAVE_KEY_STORE = 'memory';
const smokeKey = 'sk-smoke-test-only-7c1e9a2b4d';
// A fresh `runweave init` project for the Load tests, Evals and Shadow screens.
const projectDir = join(mkdtempSync(join(tmpdir(), 'runweave-smoke-')), 'project');
const created = spawnSync(python, ['-m', 'runweave.cli', 'init', projectDir], { env, encoding: 'utf8' });
if (created.status !== 0) throw new Error(`runweave init failed: ${created.stderr}`);
const server = spawn(python, ['-m', 'runweave.cli', 'serve', '--demo', '--port', String(port), '--agent', 'sample_agent:text_report',
  '--project', join(projectDir, 'runweave.toml')], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] });
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

function freePort() {
  return new Promise((resolvePort) => {
    const probe = createServer();
    probe.listen(0, '127.0.0.1', () => {
      const { port: free } = probe.address();
      probe.close(() => resolvePort(free));
    });
  });
}

async function until(check, what, ms = 30000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${what}`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
}

// Agent approvals need durable mode: a throwaway database, migrated, and a durable RunWeave
// serving the sample refund agent. Approve one request and reject another from the screen.
async function approvalChecks(adminDsn) {
  const made = spawnSync(python, [join(root, 'scripts', 'smoke-database.py'), 'create', adminDsn], { env, encoding: 'utf8' });
  if (made.status !== 0) throw new Error(`Could not create the smoke database: ${made.stderr}`);
  const dsn = made.stdout.trim();
  const durableEnv = { ...env, RUNWEAVE_DATABASE_URL: dsn };
  let durable;
  try {
    const migrated = spawnSync(python, ['-m', 'alembic', 'upgrade', 'head'], { cwd: root, env: durableEnv, encoding: 'utf8' });
    if (migrated.status !== 0) throw new Error(`Migrations failed: ${migrated.stderr}`);
    const durablePort = await freePort();
    const durableBase = `http://127.0.0.1:${durablePort}`;
    let log = '';
    durable = spawn(python, ['-m', 'runweave.cli', 'serve', '--port', String(durablePort), '--agent', 'sample_agent:refund_request'],
      { cwd: root, env: durableEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    durable.stdout.on('data', (chunk) => { log = (log + chunk).slice(-8192); });
    durable.stderr.on('data', (chunk) => { log = (log + chunk).slice(-8192); });
    await until(async () => {
      if (durable.exitCode !== null) throw new Error(`Durable server exited: ${log}`);
      try { return (await fetch(`${durableBase}/health/ready`)).ok; } catch { return false; }
    }, 'the durable server');
    const submit = async (input) => {
      const response = await fetch(`${durableBase}/v1/runs`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent: 'refund-request', input }),
      });
      assert.equal(response.status, 202, await response.text());
      return (await response.json()).status_url;
    };
    const runStatus = async (url) => (await (await fetch(`${durableBase}${url}`)).json());
    const first = await submit({ order: 'A-17', amount: 40, note: hostile });
    await until(async () => (await runStatus(first)).status === 'WAITING_APPROVAL', 'the first request');
    const second = await submit({ order: 'B-2', amount: 5, note: 'second request' });
    await until(async () => (await runStatus(second)).status === 'WAITING_APPROVAL', 'the second request');

    const context = await browser.newContext({ viewport: { width: 1360, height: 980 }, colorScheme: 'light' });
    const { page, errors } = await newPage(context);
    // A waiting run points to the inbox.
    await page.goto(`${durableBase}/#/runs/${first.split('/').at(-1)}`);
    await page.getByRole('link', { name: 'Open Approvals' }).click();
    await page.getByRole('heading', { name: 'Approvals', level: 1 }).waitFor();
    const inboxRows = page.locator('section[aria-labelledby="inbox-title"] tbody tr');
    await until(async () => (await inboxRows.count()) === 2, 'two pending requests');
    await page.locator('#approval-arguments').filter({ hasText: '"order": "A-17"' }).waitFor();
    assert.equal(await page.evaluate(() => window.__injected), undefined);
    await page.screenshot({ path: join(artifacts, 'approvals-light.png'), fullPage: true });
    await noOverflow(page, 'approvals');
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    await page.getByRole('button', { name: 'Confirm approve' }).click();
    await page.locator('#approval-detail-title').locator('..').getByText('Approved', { exact: true }).waitFor();
    const approved = await until(async () => {
      const run = await runStatus(first);
      return run.status === 'SUCCEEDED' && run;
    }, 'the approved run');
    assert.equal(approved.result.approved, true);

    await until(async () => (await inboxRows.count()) === 1, 'one pending request');
    await inboxRows.first().click();
    await page.locator('#approval-arguments').filter({ hasText: '"order": "B-2"' }).waitFor();
    await page.getByRole('button', { name: 'Reject', exact: true }).click();
    await page.getByRole('button', { name: 'Confirm reject' }).click();
    const rejected = await until(async () => {
      const run = await runStatus(second);
      return run.status === 'SUCCEEDED' && run;
    }, 'the rejected run');
    assert.equal(rejected.result.approved, false);
    await page.getByText('No pending requests', { exact: false }).first().waitFor();
    assert.deepEqual(await page.evaluate(() => window.__cspViolations), []);
    assert.deepEqual(errors, []);
    await context.close();

    const dark = await browser.newContext({ viewport: { width: 1360, height: 980 }, colorScheme: 'dark' });
    const darkPage = await newPage(dark);
    await darkPage.page.goto(`${durableBase}/#/approvals`);
    await darkPage.page.getByRole('checkbox', { name: 'Show decided' }).check();
    await darkPage.page.locator('section[aria-labelledby="inbox-title"] tbody tr').nth(1).waitFor();
    await darkPage.page.screenshot({ path: join(artifacts, 'approvals-dark.png'), fullPage: true });
    assert.deepEqual(darkPage.errors, []);
    await dark.close();
  } finally {
    if (durable && durable.exitCode === null) {
      const closed = once(durable, 'exit');
      durable.kill();
      await closed;
    }
    spawnSync(python, [join(root, 'scripts', 'smoke-database.py'), 'drop', dsn], { env, encoding: 'utf8' });
  }
}

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
  await page.goto(`${base}/#/agents`);
  await page.getByRole('link', { name: 'Connect agent', exact: true }).click();
  await page.getByRole('radio', { name: /HTTP endpoint/ }).check();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('textbox', { name: 'Agent URL', exact: true }).fill('http://169.254.169.254/latest');
  await page.getByRole('button', { name: 'Test connection' }).click();
  await page.getByRole('alert').filter({ hasText: 'loopback' }).waitFor();
  await page.getByRole('textbox', { name: 'Agent URL', exact: true }).fill(`${agentUrl}/`);
  await page.getByRole('button', { name: 'Test connection' }).click();
  await page.getByRole('cell', { name: 'contract-echo', exact: true }).waitFor();
  await page.getByText(`runweave serve --http-agent ${agentUrl}`, { exact: true }).waitFor();
  await page.screenshot({ path: join(artifacts, 'connect-http.png'), fullPage: true });
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

  // Settings: a saved key never reaches the browser; settings survive a reload.
  const bodies = [];
  const collect = async (response) => {
    if (response.url().startsWith(`${base}/v1/`)) bodies.push(await response.text().catch(() => ''));
  };
  page.on('response', collect);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('heading', { name: 'Settings', level: 1 }).waitFor();
  const openai = page.getByRole('region', { name: 'OpenAI' });
  await openai.getByLabel('OpenAI API key').fill(smokeKey);
  await openai.getByRole('button', { name: 'Save key' }).click();
  await openai.getByText('Saved in the credential store').waitFor();
  assert.equal(await openai.getByLabel('OpenAI API key').inputValue(), '');
  await page.getByLabel('Fault profile').selectOption('outage');
  await page.getByLabel('Seed').fill('7');
  await page.getByRole('button', { name: 'Save fault settings' }).click();
  await page.getByRole('status').filter({ hasText: 'Fault settings saved' }).waitFor();
  await page.getByLabel('Model calls per run').fill('5');
  await page.getByRole('button', { name: 'Save budgets' }).click();
  await page.getByRole('status').filter({ hasText: 'Budgets saved' }).waitFor();
  await page.screenshot({ path: join(artifacts, 'settings-light.png'), fullPage: true });
  await noOverflow(page, 'settings');
  await page.reload();
  await page.getByRole('heading', { name: 'Settings', level: 1 }).waitFor();
  assert.equal(await page.getByLabel('Fault profile').inputValue(), 'outage');
  assert.equal(await page.getByLabel('Model calls per run').inputValue(), '5');
  await page.getByText('RUNWEAVE_PROXY_FAULT_PROFILE=outage').waitFor();
  await page.getByRole('region', { name: 'OpenAI' }).getByRole('button', { name: 'Remove key' }).click();
  await page.getByRole('region', { name: 'OpenAI' }).getByText('Not set').waitFor();
  await page.getByLabel('Fault profile').selectOption('');
  await page.getByRole('button', { name: 'Save fault settings' }).click();
  await page.getByRole('status').filter({ hasText: 'Fault settings saved' }).waitFor();
  page.off('response', collect);
  assert.ok(bodies.length > 3);
  assert.ok(bodies.every((body) => !body.includes(smokeKey)), 'A saved key reached the browser');
  assert.ok(!(await page.content()).includes(smokeKey));

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

  // Testing screens start the same CLI jobs and read back the saved reports.
  await page.goto(`${base}/#/evals`);
  await page.getByRole('heading', { name: 'Evals', level: 1 }).waitFor();
  await page.getByRole('button', { name: 'Run eval' }).click();
  await page.getByText('Gates passed', { exact: true }).first().waitFor({ timeout: 60000 });
  assert.match(page.url(), /#\/evals\?report=eval-/);
  await page.locator('.stat-value').filter({ hasText: /^4 \/ 4$/ }).waitFor();
  await noOverflow(page, 'evals');
  await page.goto(`${base}/#/load-tests`);
  await page.getByLabel('Concurrency levels').fill('1 2');
  await page.getByLabel('Runs per level').fill('6');
  await page.getByRole('button', { name: 'Start load test' }).click();
  await page.getByRole('heading', { name: 'Results by level' }).waitFor({ timeout: 60000 });
  assert.match(page.url(), /#\/load-tests\?report=bench-/);
  assert.equal(await page.locator('section[aria-labelledby="bench-levels-title"] tbody tr').count(), 2);
  await page.screenshot({ path: join(artifacts, 'load-tests-light.png'), fullPage: true });
  await noOverflow(page, 'load tests');
  await page.goto(`${base}/#/shadow`);
  await page.getByLabel('Candidate').fill('agent:run');
  await page.getByRole('button', { name: 'Compare' }).click();
  await page.getByText('Eligible for promotion', { exact: true }).waitFor({ timeout: 60000 });
  await page.screenshot({ path: join(artifacts, 'shadow-light.png'), fullPage: true });
  await noOverflow(page, 'shadow');
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
  await darkPage.page.goto(`${base}/#/settings`);
  await darkPage.page.getByRole('heading', { name: 'Settings', level: 1 }).waitFor();
  await darkPage.page.screenshot({ path: join(artifacts, 'settings-dark.png'), fullPage: true });
  await darkPage.page.goto(`${base}/#/load-tests`);
  await darkPage.page.getByRole('link', { name: /^bench-/ }).first().click();
  await darkPage.page.getByRole('heading', { name: 'Results by level' }).waitFor();
  await darkPage.page.screenshot({ path: join(artifacts, 'load-tests-dark.png'), fullPage: true });
  assert.deepEqual(await darkPage.page.evaluate(() => window.__cspViolations), []);
  assert.deepEqual(darkPage.errors, []);
  await dark.close();

  const adminDsn = process.env.RUNWEAVE_SMOKE_POSTGRES_URL;
  if (adminDsn) await approvalChecks(adminDsn);
  else if (process.env.CI) throw new Error('Set RUNWEAVE_SMOKE_POSTGRES_URL: CI runs the approvals checks on PostgreSQL');
  else console.log('Skipped the approvals checks: set RUNWEAVE_SMOKE_POSTGRES_URL to a PostgreSQL server to run them.');

  console.log('Browser checks passed: agents, generated forms, server validation, live run updates, findings, SDK agent timeline and re-run, runs list, legacy links, cancellation, queue errors, expired IDs, setup detection, HTTP agent connect/run/remove, settings and key secrecy, approval bindings, eval/load-test/shadow jobs and reports, approving and rejecting agent approvals (with PostgreSQL), mobile overflow, safe text, both themes.');
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
