// Large-data checks for the workspace: thousands of runs, a 200-step timeline, 5,000-case
// eval and shadow reports, and a 2,000-tick live load-test chart. Each screen must render
// quickly, stay interactive (no long main-thread tasks), and keep the DOM bounded.
// Run: npm run test:perf   (results in test-results/perf-report.json)
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = dirname(fileURLToPath(import.meta.url));
const root = resolve(frontend, '..');
const artifacts = join(frontend, 'test-results');
mkdirSync(artifacts, { recursive: true });
const python = process.env.RUNWEAVE_TEST_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const RUNS = Number(process.env.RUNWEAVE_PERF_RUNS || 3000);
const CASES = 5000;
const BUDGET = { renderMs: 1500, interactMs: 250, longTaskMs: 200, rows: 100 };

const work = mkdtempSync(join(tmpdir(), 'runweave-perf-'));
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('RUNWEAVE_')));
env.PYTHONPATH = [join(root, 'src'), join(root, 'examples', 'sample-agent'), work].join(delimiter);
env.RUNWEAVE_HISTORY_MAX_RECORDS = String(RUNS + 500);
env.RUNWEAVE_KEY_STORE = 'memory';

// An agent with the maximum timeline, and a fast one for thousands of runs.
writeFileSync(join(work, 'perf_agents.py'), `
from pydantic import BaseModel
from runweave.sdk import RunContext, agent


class Empty(BaseModel):
    pass


class Done(BaseModel):
    steps: int


@agent(name="long-timeline", version="1", input=Empty, output=Done, max_steps=200)
async def long_timeline(input: Empty, ctx: RunContext) -> Done:
    for index in range(200):
        async with ctx.step(f"Step {index + 1}", detail=f"Detail for step {index + 1}"):
            pass
    return Done(steps=200)


@agent(name="quick", version="1", input=Empty, output=Done, max_steps=1)
async def quick(input: Empty, ctx: RunContext) -> Done:
    return Done(steps=0)
`);
const projectDir = join(work, 'project');
const made = spawnSync(python, ['-m', 'runweave.cli', 'init', projectDir], { env, encoding: 'utf8' });
if (made.status !== 0) throw new Error(`runweave init failed: ${made.stderr}`);

// Seeded reports with the same shape the CLI writes.
const reports = join(projectDir, '.runweave', 'reports');
const evalId = 'eval-20261119T000000Z-aaaaaa';
const shadowId = 'shadow-20261119T000000Z-bbbbbb';
const caseRecord = (i, passed) => ({
  id: `case-${String(i).padStart(5, '0')}`, passed, status: 'SUCCEEDED', run_id: null, latency_ms: 1 + (i % 17), tokens: 0,
  cost_usd: '0', model_calls: 0, tags: [], assertions: [{ name: 'output', passed, message: passed ? '' : 'words: expected 3, got 2' }],
});
const evalCases = Array.from({ length: CASES }, (_, i) => caseRecord(i, i % 50 !== 0));
mkdirSync(join(reports, evalId), { recursive: true });
writeFileSync(join(reports, evalId, 'report.json'), JSON.stringify({
  kind: 'eval', version: 1, id: evalId, created_at: '2026-11-19T00:00:00+00:00',
  dataset: { name: 'large', path: 'datasets/large.jsonl', sha256: '0'.repeat(64), cases: CASES },
  agent: { name: 'word-count', version: '0.1.0', kind: 'python' },
  environment: { mode: 'in-process', runweave: 'source', python: '3.13', platform: 'perf', machine: 'perf', cpu_count: 1 },
  settings: {}, summary: { total: CASES, passed: CASES - 100, failed: 100, pass_rate: (CASES - 100) / CASES, duration_ms: 1, p50_latency_ms: 8, p95_latency_ms: 16, tokens: 0, cost_usd: '0' },
  cases: evalCases, baseline: null, gates: { min_pass_rate: 0.9, passed: true, reasons: [] },
}));
const pairs = evalCases.map((record, i) => ({
  id: record.id, primary: { ...record, passed: true }, candidate: record,
  differences: { status_equal: true, output_equal: record.passed, latency_ms: 0.5, tokens: 0, cost_usd: '0', model_calls: 0,
    newly_failing: record.passed ? [] : ['output'], regressed: !record.passed, fixed: false },
}));
mkdirSync(join(reports, shadowId), { recursive: true });
writeFileSync(join(reports, shadowId, 'report.json'), JSON.stringify({
  kind: 'shadow', version: 1, id: shadowId, created_at: '2026-11-19T00:00:00+00:00', policy: 'perf',
  dataset: { name: 'large', path: 'datasets/large.jsonl', sha256: '0'.repeat(64), cases: CASES }, environment: { mode: 'in-process' },
  settings: { min_pass_rate: 0.9, max_cost_ratio: 1.05, max_latency_ratio: 2, latency_slack_ms: 50, require_output_parity: false },
  primary: { agent: { name: 'word-count', version: '0.1.0', kind: 'python' }, passed: CASES, pass_rate: 1, p50_latency_ms: 8, p95_latency_ms: 16, tokens: 0, cost_usd: '0' },
  candidate: { agent: { name: 'word-count', version: '0.2.0', kind: 'python' }, passed: CASES - 100, pass_rate: 0.98, p50_latency_ms: 8, p95_latency_ms: 16, tokens: 0, cost_usd: '0' },
  summary: { pairs: CASES, status_equal: CASES, output_equal: CASES - 100, duration_ms: 1 },
  regressions: pairs.filter((p) => p.differences.regressed).map((p) => p.id), fixed: [],
  gates: { candidate_pass_rate_met: true, no_paired_regressions: false, outcome_parity: true, candidate_cost_within_limit: true, candidate_latency_within_limit: true },
  promotion_eligible: false, pairs, limitations: 'Seeded for the large-data check.',
}));

const port = await new Promise((resolvePort) => {
  const probe = createServer();
  probe.listen(0, '127.0.0.1', () => { const { port: free } = probe.address(); probe.close(() => resolvePort(free)); });
});
const base = `http://127.0.0.1:${port}`;
const server = spawn(python, ['-m', 'runweave.cli', 'serve', '--demo', '--port', String(port), '--assets', join(frontend, 'dist'),
  '--agent', 'perf_agents:long_timeline', '--agent', 'perf_agents:quick', '--project', join(projectDir, 'runweave.toml')],
{ cwd: work, env, stdio: ['ignore', 'pipe', 'pipe'] });
let log = '';
server.stdout.on('data', (chunk) => { log = (log + chunk).slice(-8192); });
server.stderr.on('data', (chunk) => { log = (log + chunk).slice(-8192); });

async function until(check, what, ms = 120000) {
  const deadline = Date.now() + ms;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`Timed out waiting for ${what}`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 200));
  }
}

async function post(path, body) {
  const response = await fetch(`${base}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (response.status === 503) return null;
  assert.equal(response.status, 202, await response.text());
  return response.json();
}

const results = {};
let browser;
try {
  await until(async () => {
    if (server.exitCode !== null) throw new Error(`Server exited: ${log}`);
    try { return (await fetch(`${base}/health/ready`)).ok; } catch { return false; }
  }, 'the server');

  // Thousands of runs: submit in waves that respect the queue.
  const started = Date.now();
  let submitted = 0;
  while (submitted < RUNS) {
    const wave = await Promise.all(Array.from({ length: Math.min(200, RUNS - submitted) }, () => post('/v1/runs', { agent: 'quick', input: {} })));
    submitted += wave.filter(Boolean).length;
    if (wave.some((item) => !item)) await new Promise((resolveWait) => setTimeout(resolveWait, 300));
  }
  const long = await post('/v1/runs', { agent: 'long-timeline', input: {} });
  await until(async () => (await (await fetch(`${base}/v1/runs/${long.id}`)).json()).status === 'SUCCEEDED', 'the long run');
  results.seed = { runs: RUNS + 1, seconds: Math.round((Date.now() - started) / 100) / 10 };

  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  const context = await browser.newContext({ viewport: { width: 1360, height: 980 }, colorScheme: 'light' });
  await context.addInitScript(() => {
    window.__longTasks = [];
    new PerformanceObserver((list) => { for (const entry of list.getEntries()) window.__longTasks.push(entry.duration); })
      .observe({ type: 'longtask', buffered: true });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));

  const timed = async (action, ready) => {
    const begin = Date.now();
    await action();
    await ready();
    return Date.now() - begin;
  };
  const longest = async () => Math.max(0, ...(await page.evaluate(() => window.__longTasks.splice(0))));
  const rows = (selector) => page.locator(`${selector} tbody tr`).count();

  // Runs: first page, then older pages; the table keeps one page in the DOM.
  results.runs = {};
  results.runs.firstRenderMs = await timed(() => page.goto(`${base}/#/runs`), () => page.locator('section[aria-labelledby="runs-table-title"] tbody tr').first().waitFor());
  const older = page.getByRole('button', { name: 'Load older runs' });
  const loadTimes = [];
  for (let i = 0; i < 12; i += 1) {
    const before = await page.getByText(/Rows \d+–\d+ of [\d,]+|runs loaded/).first().textContent().catch(() => '');
    loadTimes.push(await timed(() => older.click(), () => until(async () => (await page.getByText(/Rows \d+–\d+ of [\d,]+|runs loaded/).first().textContent().catch(() => '')) !== before, 'older runs', 10000)));
  }
  results.runs.loadOlderMs = Math.max(...loadTimes);
  results.runs.domRows = await rows('section[aria-labelledby="runs-table-title"]');
  results.runs.longTaskMs = await longest();

  // A 200-step timeline.
  results.timeline = {};
  results.timeline.renderMs = await timed(() => page.goto(`${base}/#/runs/${long.id}`), () => page.getByText('Step 200', { exact: true }).waitFor());
  results.timeline.longTaskMs = await longest();

  // A 5,000-case eval report: failures first, then paging and search.
  results.evals = {};
  results.evals.renderMs = await timed(() => page.goto(`${base}/#/evals?report=${evalId}`), () => page.locator('section[aria-labelledby="eval-cases-title"] tbody tr').first().waitFor());
  results.evals.domRows = await rows('section[aria-labelledby="eval-cases-title"]');
  await page.getByRole('checkbox', { name: 'Failed only' }).uncheck();
  await page.getByText(/Rows 1–100 of 5,000/).waitFor();
  results.evals.nextPageMs = await timed(() => page.getByRole('button', { name: 'Next' }).first().click(), () => page.getByText(/Rows 101–200 of 5,000/).waitFor());
  results.evals.searchMs = await timed(() => page.getByLabel('Find a case').fill('case-0420'), () => page.getByText(/Rows 1–10 of 10|case-04209/).first().waitFor());
  results.evals.longTaskMs = await longest();

  // A 5,000-pair shadow report.
  results.shadow = {};
  results.shadow.renderMs = await timed(() => page.goto(`${base}/#/shadow?report=${shadowId}`), () => page.locator('section[aria-labelledby="shadow-pairs-title"] tbody tr').first().waitFor());
  await page.getByRole('checkbox', { name: 'Changed only' }).uncheck();
  results.shadow.nextPageMs = await timed(() => page.getByRole('button', { name: 'Next' }).first().click(), () => page.getByText(/Rows 101–200 of 5,000/).waitFor());
  results.shadow.domRows = await rows('section[aria-labelledby="shadow-pairs-title"]');
  results.shadow.longTaskMs = await longest();

  // A live load test with 2,000 ticks (the job API is intercepted).
  const jobId = 'job-0123456789ab';
  const ticks = Array.from({ length: 2000 }, (_, i) => ({ seq: i + 1, event: 'tick', fault_profile: '', concurrency: 8, elapsed_ms: (i + 1) * 500,
    completed: i * 10, runs: null, throughput_rps: 80 + (i % 40), p95_latency_ms: 10 + (i % 25), failures: 0 }));
  const job = { id: jobId, kind: 'bench', options: {}, status: 'RUNNING', created_at: '2026-11-19T00:00:00+00:00', finished_at: null, report_id: null, passed: null, error: null, last_seq: 2000 };
  await page.route('**/v1/jobs', (route) => (route.request().method() === 'GET' ? route.fulfill({ json: { jobs: [job] } }) : route.continue()));
  let served = false;
  await page.route(`**/v1/jobs/${jobId}?**`, (route) => {
    const events = served ? [] : ticks;
    served = true;
    return route.fulfill({ json: { job, events } });
  });
  results.liveChart = {};
  results.liveChart.renderMs = await timed(() => page.goto(`${base}/#/load-tests`), () => page.locator('#live-throughput-title').waitFor());
  results.liveChart.pathPoints = await page.locator('section[aria-labelledby="live-throughput-title"] path.series-line').evaluate((path) => path.getAttribute('d').split(/[ML]/).filter(Boolean).length);
  results.liveChart.longTaskMs = await longest();
  assert.deepEqual(errors, []);
  await context.close();

  writeFileSync(join(artifacts, 'perf-report.json'), `${JSON.stringify({ budget: BUDGET, results }, null, 2)}\n`);
  const fail = [];
  for (const [screen, values] of Object.entries(results)) {
    for (const [key, value] of Object.entries(values)) {
      if (/render|First/i.test(key) && value > BUDGET.renderMs) fail.push(`${screen}.${key} ${value} ms > ${BUDGET.renderMs}`);
      if (/nextPage|search|loadOlder/i.test(key) && value > Math.max(BUDGET.interactMs, key === 'loadOlderMs' ? 1000 : 0)) fail.push(`${screen}.${key} ${value} ms`);
      if (key === 'longTaskMs' && value > BUDGET.longTaskMs) fail.push(`${screen}.longTask ${value} ms > ${BUDGET.longTaskMs}`);
      if (key === 'domRows' && value > BUDGET.rows) fail.push(`${screen} renders ${value} rows > ${BUDGET.rows}`);
    }
  }
  if (results.liveChart.pathPoints > 400) fail.push(`live chart draws ${results.liveChart.pathPoints} points > 400`);
  console.log(JSON.stringify(results, null, 2));
  assert.deepEqual(fail, [], `Large-data budgets missed:\n${fail.join('\n')}`);
  console.log('Large-data checks passed.');
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    const closed = once(server, 'exit');
    server.kill();
    await closed;
  }
}
