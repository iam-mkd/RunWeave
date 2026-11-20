// Screenshots of every workspace screen in both themes, for docs/images.
// Run: npm run build && npm run screenshots [-- --out <folder>]
// With RUNWEAVE_SMOKE_POSTGRES_URL set (a PostgreSQL server), the Approvals screen is
// captured from a durable RunWeave; without it, that screen is skipped.
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = dirname(fileURLToPath(import.meta.url));
const root = resolve(frontend, '..');
const outFlag = process.argv.indexOf('--out');
const out = outFlag > 0 ? resolve(process.argv[outFlag + 1]) : join(root, 'docs', 'images');
mkdirSync(out, { recursive: true });
const python = process.env.RUNWEAVE_TEST_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const VIEWPORT = { width: 1360, height: 900 };

const work = mkdtempSync(join(tmpdir(), 'runweave-shots-'));
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('RUNWEAVE_')));
env.PYTHONPATH = [join(root, 'src'), join(root, 'examples', 'sample-agent')].join(delimiter);
env.RUNWEAVE_KEY_STORE = 'memory';
env.RUNWEAVE_MOCK_RUNBOOK_DELAY_SECONDS = '0.2';

function run(args, options = {}) {
  const result = spawnSync(python, args, { env, encoding: 'utf8', ...options });
  if (result.status !== 0 && !options.allowFailure) throw new Error(`${args.join(' ')} failed: ${result.stderr || result.stdout}`);
  return result;
}

function freePort() {
  return new Promise((resolvePort) => {
    const probe = createServer();
    probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolvePort(port)); });
  });
}

async function until(check, what, ms = 60000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${what}`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
  }
}

async function serve(args, extraEnv = {}) {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const child = spawn(python, ['-m', 'runweave.cli', 'serve', '--port', String(port), '--assets', join(frontend, 'dist'), ...args],
    { cwd: work, env: { ...env, ...extraEnv }, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  child.stdout.on('data', (chunk) => { log = (log + chunk).slice(-4096); });
  child.stderr.on('data', (chunk) => { log = (log + chunk).slice(-4096); });
  await until(async () => {
    if (child.exitCode !== null) throw new Error(`Server exited: ${log}`);
    try { return (await fetch(`${base}/health/ready`)).ok; } catch { return false; }
  }, 'the server');
  return { base, child };
}

async function stop(child) {
  if (child.exitCode === null) {
    const closed = once(child, 'exit');
    child.kill();
    await closed;
  }
}

async function submit(base, agent, input) {
  const response = await fetch(`${base}/v1/runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent, input }) });
  if (response.status !== 202) throw new Error(`Submitting ${agent} failed: ${await response.text()}`);
  return response.json();
}

async function finished(base, id) {
  return until(async () => {
    const run = await (await fetch(`${base}/v1/runs/${id}`)).json();
    return ['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED'].includes(run.status) && run;
  }, `run ${id}`);
}

// A project with real reports: an eval (one case fails, to show failures), a load test with
// two fault profiles and a short soak, and a shadow comparison against a changed version.
const project = join(work, 'demo-agent');
run(['-m', 'runweave.cli', 'init', project]);
run(['-m', 'runweave.cli', 'eval'], { cwd: project });
spawnSync(python, ['-c', `
from pathlib import Path
path = Path("datasets/smoke.jsonl")
path.write_text(path.read_text() + '{"id": "two-words", "input": {"text": "hello world"}, "expect": {"output": {"words": 3}}}\\n')
agent = Path("agent.py").read_text()
Path("agent_v2.py").write_text(agent.replace('version="0.1.0"', 'version="0.2.0"').replace("longest=longest", "longest=words[0] if words else ''"))
`], { cwd: project, env });
run(['-m', 'runweave.cli', 'eval'], { cwd: project, allowFailure: true });
run(['-m', 'runweave.cli', 'bench', '--concurrency', '1', '4', '16', '--runs', '40', '--fault-profile', '', '--fault-profile', 'flaky', '--soak', '4'], { cwd: project });
run(['-m', 'runweave.cli', 'shadow', '--candidate', 'agent_v2:run'], { cwd: project, allowFailure: true });
const report = (kind) => run(['-c', `
import pathlib, sys
names = sorted(p.name for p in pathlib.Path(".runweave/reports").glob("${kind}-*"))
print(names[-1])
`], { cwd: project }).stdout.trim();
const reports = { eval: report('eval'), bench: report('bench'), shadow: report('shadow') };

const shots = [];
const { base, child } = await serve(['--demo', '--agent', 'sample_agent:text_report', '--project', join(project, 'runweave.toml')]);
let browser;
try {
  const triage = await submit(base, 'incident-triage', { service: 'payments', description: 'Card authorization latency rose after the 18:20 deploy.' });
  await submit(base, 'text-report', { text: 'RunWeave runs agents. It measures them, too.', keywords: ['runweave', 'agents'] });
  const failing = await submit(base, 'incident-triage', { service: 'checkout', description: 'Checkout returns intermittent 503s.' });
  const report1 = await submit(base, 'text-report', { text: 'A second report with a few more words in it.', keywords: ['report'] });
  await finished(base, triage.id);
  await finished(base, failing.id);
  await finished(base, report1.id);

  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  const screens = [
    ['agents', '#/agents', 'h1'],
    ['runs', '#/runs', 'section[aria-labelledby="runs-table-title"] tbody tr'],
    ['new-run', '#/runs/new?agent=text-report', 'form'],
    ['run-detail', `#/runs/${triage.id}`, '.run-state .status.succeeded'],
    ['load-tests', `#/load-tests?report=${reports.bench}`, '#bench-levels-title'],
    ['evals', `#/evals?report=${reports.eval}`, '#eval-cases-title'],
    ['shadow', `#/shadow?report=${reports.shadow}`, '#shadow-pairs-title'],
    ['settings', '#/settings', '#budgets-title'],
    ['setup', '#/setup', 'h1'],
  ];
  for (const scheme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: scheme, reducedMotion: 'reduce' });
    const page = await context.newPage();
    for (const [name, path, ready] of screens) {
      await page.goto(`${base}/${path}`);
      await page.locator(ready).first().waitFor();
      await page.waitForLoadState('networkidle');
      const file = join(out, `${name}-${scheme}.png`);
      await page.screenshot({ path: file });
      shots.push(file);
    }
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  await stop(child);
}

const adminDsn = process.env.RUNWEAVE_SMOKE_POSTGRES_URL;
if (adminDsn) {
  const made = run([join(root, 'scripts', 'smoke-database.py'), 'create', adminDsn]);
  const dsn = made.stdout.trim();
  const durableEnv = { RUNWEAVE_DATABASE_URL: dsn };
  let durable;
  try {
    run(['-m', 'alembic', '-c', join(root, 'alembic.ini'), 'upgrade', 'head'], { cwd: work, env: { ...env, ...durableEnv } });
    durable = await serve(['--agent', 'sample_agent:refund_request'], durableEnv);
    await submit(durable.base, 'refund-request', { order: 'A-1042', amount: 40, note: 'Customer charged twice for one order.' });
    await submit(durable.base, 'refund-request', { order: 'B-2207', amount: 125, note: 'Late delivery; goodwill refund requested.' });
    await until(async () => (await (await fetch(`${durable.base}/v1/approvals`)).json()).approvals.length === 2, 'two requests');
    browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
    for (const scheme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: scheme, reducedMotion: 'reduce' });
      const page = await context.newPage();
      await page.goto(`${durable.base}/#/approvals`);
      await page.locator('#approval-arguments').waitFor();
      const file = join(out, `approvals-${scheme}.png`);
      await page.screenshot({ path: file });
      shots.push(file);
      await context.close();
    }
    await browser.close();
  } finally {
    if (durable) await stop(durable.child);
    run([join(root, 'scripts', 'smoke-database.py'), 'drop', dsn], { allowFailure: true });
  }
} else {
  console.log('Skipped Approvals: set RUNWEAVE_SMOKE_POSTGRES_URL to capture it.');
}
console.log(`Wrote ${shots.length} screenshots to ${out}`);
