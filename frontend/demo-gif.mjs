// Records docs/images/demo.gif: about 12 seconds of the real product, for the README.
// Run: npm run build && npm run demo-gif
//
// Everything shown is real: the terminal scene replays the output of `runweave eval` and
// `runweave bench`, run just before on a copy of examples/plain-python; the workspace
// scenes drive a live `runweave serve` with that agent. Only the captions and the pointer
// are drawn on top. Frames are captured with fixed durations, so the timing does not
// depend on how fast this machine is; scripts/frames-to-gif.py assembles them.
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = dirname(fileURLToPath(import.meta.url));
const root = resolve(frontend, '..');
const python = process.env.RUNWEAVE_TEST_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const framesDir = join(root, 'target', 'demo-gif', 'frames');
const output = join(root, 'docs', 'images', 'demo.gif');
const VIEWPORT = { width: 1280, height: 760 };

const work = mkdtempSync(join(tmpdir(), 'runweave-demo-'));
const project = join(work, 'weather-agent');
cpSync(join(root, 'examples', 'plain-python'), project, { recursive: true, filter: (path) => !path.includes('.runweave') && !path.includes('.venv') });
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('RUNWEAVE_') && !/api_key/i.test(key)));
env.PYTHONPATH = [join(root, 'src'), project].join(delimiter);
env.RUNWEAVE_KEY_STORE = 'memory';
env.PYTHONIOENCODING = 'utf-8';

function runweave(...args) {
  const result = spawnSync(python, ['-m', 'runweave.cli', ...args], { cwd: project, env, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(`runweave ${args.join(' ')} failed: ${result.stderr || result.stdout}`);
  return result.stdout.trim().split(/\r?\n/).filter((line) => !line.startsWith('Report:'));
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

// The real output the terminal scene replays.
const evalLines = runweave('eval');
const BENCH = ['bench', '--fault-profile', '', '--fault-profile', 'flaky'];
const benchLines = runweave(...BENCH);
const benchReport = spawnSync(python, ['-c', 'import pathlib; print(sorted(p.name for p in pathlib.Path(".runweave/reports").glob("bench-*"))[-1])'], { cwd: project, env, encoding: 'utf8' }).stdout.trim();

const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const server = spawn(python, ['-m', 'runweave.cli', 'serve', '--port', String(port), '--assets', join(frontend, 'dist'),
  '--agent', 'openai_weather:run', '--project', join(project, 'runweave.toml')],
{ cwd: project, env: { ...env, RUNWEAVE_PROXY_MOCK_FIXTURES: join(project, 'fixtures.json') }, stdio: ['ignore', 'pipe', 'pipe'] });
let log = '';
server.stdout.on('data', (chunk) => { log = (log + chunk).slice(-4096); });
server.stderr.on('data', (chunk) => { log = (log + chunk).slice(-4096); });

const frames = [];
let page;
async function frame(ms) {
  const index = frames.length;
  const file = join(framesDir, `${String(index).padStart(3, '0')}.png`);
  await page.screenshot({ path: file });
  frames.push({ file, ms });
}

// Overlays: a caption bar and a pointer, drawn over the page for the recording only.
async function caption(text) {
  await page.evaluate((value) => {
    let bar = document.getElementById('demo-caption');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'demo-caption';
      bar.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:99999;'
        + 'background:rgba(12,14,20,.92);color:#fff;font:600 21px/1.3 system-ui,sans-serif;padding:10px 20px;'
        + 'border-radius:10px;border:1px solid rgba(255,255,255,.18);box-shadow:0 8px 30px rgba(0,0,0,.45);white-space:nowrap';
      document.body.appendChild(bar);
    }
    bar.textContent = value;
  }, text);
}

async function pointAt(locator) {
  const box = await locator.boundingBox();
  await page.evaluate(({ x, y }) => {
    let dot = document.getElementById('demo-pointer');
    if (!dot) {
      dot = document.createElement('div');
      dot.id = 'demo-pointer';
      dot.style.cssText = 'position:fixed;z-index:99999;width:26px;height:26px;margin:-13px 0 0 -13px;border-radius:50%;'
        + 'background:rgba(255,214,10,.55);border:2px solid #ffd60a;pointer-events:none';
      document.body.appendChild(dot);
    }
    dot.style.left = `${x}px`;
    dot.style.top = `${y}px`;
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
}

const TERMINAL = `<!doctype html><html><body style="margin:0;background:#0d1117;color:#e6edf3;font:19px/1.55 'Cascadia Mono',Consolas,'SF Mono',monospace">
<div style="margin:36px 44px;border:1px solid #30363d;border-radius:10px;overflow:hidden">
<div style="background:#161b22;padding:9px 14px;color:#8b949e;font-size:14px">weather-agent: examples/plain-python</div>
<pre id="screen" style="margin:0;padding:18px 22px;min-height:470px;white-space:pre-wrap"></pre></div></body></html>`;

function escape(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;');
}

async function terminal(lines) {
  await page.evaluate((html) => { document.getElementById('screen').innerHTML = html; }, lines.join('\n'));
}

async function typeCommand(done, command, ms) {
  for (let i = 1; i <= command.length; i += 3) {
    await terminal([...done, `<span style="color:#7ee787">$</span> ${escape(command.slice(0, i))}<span style="background:#e6edf3"> </span>`]);
    await frame(ms);
  }
  return [...done, `<span style="color:#7ee787">$</span> ${escape(command)}`];
}

function colour(line) {
  const text = escape(line);
  if (/Gates passed/.test(line)) return `<span style="color:#7ee787">${text}</span>`;
  if (/cases passed|runs\/s/.test(line)) return `<span style="color:#e6edf3">${text}</span>`;
  return `<span style="color:#8b949e">${text}</span>`;
}

let browser;
try {
  await until(async () => {
    if (server.exitCode !== null) throw new Error(`Server exited: ${log}`);
    try { return (await fetch(`${base}/health/ready`)).ok; } catch { return false; }
  }, 'the server');
  rmSync(framesDir, { recursive: true, force: true });
  mkdirSync(framesDir, { recursive: true });

  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: 'dark', reducedMotion: 'reduce' });
  page = await context.newPage();

  // 1. The CLI: eval and bench, offline, no key (about 3.6 s).
  await page.setContent(TERMINAL);
  await caption('Test your agent from the CLI: offline, no API key');
  let shown = await typeCommand([], 'runweave eval', 70);
  shown = [...shown, ...evalLines.map(colour)];
  await terminal(shown);
  await frame(1200);
  shown = await typeCommand([...shown, ''], 'runweave bench --fault-profile "" --fault-profile flaky', 70);
  shown = [...shown, ...benchLines.map(colour)];
  await terminal(shown);
  await frame(1700);

  // 2. The workspace: start a run (about 3 s).
  await page.goto(`${base}/#/agents`);
  await page.locator('h1').first().waitFor();
  await page.waitForLoadState('networkidle');
  await caption('Run it in the local workspace');
  const newRun = page.getByRole('link', { name: 'New run for plain-python-weather' });
  await pointAt(newRun);
  await frame(900);
  await newRun.click();
  const field = page.locator('form textarea, form input[type="text"]').first();
  await field.waitFor();
  await page.waitForLoadState('networkidle');
  await caption('Run it in the local workspace');
  await pointAt(field);
  const question = 'What is the weather in Paris?';
  for (let i = 4; i <= question.length; i += 5) {
    await field.fill(question.slice(0, i));
    await frame(60);
  }
  await field.fill(question);
  const start = page.getByRole('button', { name: 'Start run' });
  await pointAt(start);
  await frame(600);
  await start.click();

  // 3. The run: every model call and tool call on the timeline (about 2.4 s).
  await page.locator('.run-state .status.succeeded').first().waitFor({ timeout: 30000 });
  await page.waitForLoadState('networkidle');
  await caption('See every model call and tool call, with tokens');
  await page.evaluate(() => document.getElementById('demo-pointer')?.remove());
  await frame(2400);

  // 4. Load tests: throughput, latency percentiles and failures per level (about 2.4 s).
  await page.goto(`${base}/#/load-tests?report=${benchReport}`);
  await page.locator('#bench-levels-title').waitFor();
  await page.waitForLoadState('networkidle');
  await caption('Load-test it, with and without injected provider faults');
  await frame(900);
  await page.locator('#bench-levels-title').evaluate((title) => title.scrollIntoView({ block: 'start' }));
  await page.evaluate(() => window.scrollBy(0, -24));
  await frame(2100);
  await context.close();
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    const closed = once(server, 'exit');
    server.kill();
    await closed;
  }
  rmSync(work, { recursive: true, force: true });
}

writeFileSync(join(framesDir, 'frames.json'), JSON.stringify(frames, null, 1));
const total = frames.reduce((sum, item) => sum + item.ms, 0);
console.log(`Captured ${frames.length} frames, ${(total / 1000).toFixed(1)} s. Assemble with:`);
console.log(`  uv run --no-project --with pillow python scripts/frames-to-gif.py "${framesDir}" "${output}"`);
