import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdir, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const frontend = dirname(fileURLToPath(import.meta.url));
const root = resolve(frontend, 'dist-prototype');
const artifacts = join(frontend, 'test-results', 'prototype');
await mkdir(artifacts, { recursive: true });
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
// Same policy the workspace serves, so the prototype proves the components comply.
const policy = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";

const missing = [];
const server = createServer(async (request, response) => {
  const path = normalize(join(root, decodeURIComponent(new URL(request.url, 'http://x').pathname)));
  if (!path.startsWith(root + sep)) { response.writeHead(403).end(); return; }
  try {
    const body = await readFile(path);
    response.writeHead(200, { 'Content-Type': types[extname(path)] ?? 'application/octet-stream', 'Content-Security-Policy': policy }).end(body);
  } catch {
    missing.push(request.url);
    response.writeHead(404).end();
  }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const base = `http://127.0.0.1:${server.address().port}/prototype.html`;
const screens = ['agents', 'setup', 'runs', `runs/7a3f10c2-4c1e-4b8a-9d2f-100000000000`, 'runs/new', 'load-tests', 'evals', 'shadow', 'approvals', 'settings'];
let browser;

try {
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL || process.platform === 'win32' ? { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' } : {}) });
  for (const scheme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, colorScheme: scheme });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    await page.addInitScript(() => {
      window.__cspViolations = [];
      document.addEventListener('securitypolicyviolation', (event) => window.__cspViolations.push(event.violatedDirective));
    });
    for (const screen of screens) {
      await page.goto(`${base}#/${screen}`);
      await page.locator('main h1').first().waitFor();
      assert.equal(await page.locator('nav[aria-label="Primary"] [aria-current="page"]').count(), 1, screen);
      await page.screenshot({ path: join(artifacts, `${screen.replaceAll('/', '-').slice(0, 16)}-${scheme}.png`), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${screen} overflows at 390px`);
      if (screen === 'agents' || screen === 'load-tests') await page.screenshot({ path: join(artifacts, `${screen}-${scheme}-mobile.png`), fullPage: true });
      await page.setViewportSize({ width: 1360, height: 900 });
    }

    await page.goto(`${base}#/load-tests`);
    const chart = page.getByRole('img', { name: /^Completion latency/ });
    const box = await chart.boundingBox();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
    await page.locator('.chart-tooltip').first().waitFor();
    await chart.focus();
    await page.keyboard.press('ArrowRight');
    await page.getByRole('button', { name: 'Start load test' }).click();
    await page.locator('.status.running').waitFor();

    await page.goto(`${base}#/approvals`);
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    await page.getByRole('status').filter({ hasText: 'Approved' }).waitFor();
    assert.equal(await page.locator('pre b').count(), 0);

    await page.goto(`${base}#/setup`);
    await page.getByLabel('HTTP endpoint').check();
    await page.getByRole('button', { name: 'Continue' }).click();
    await page.getByLabel('Endpoint URL').waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}#/runs`);
    await page.getByRole('button', { name: 'Menu' }).click();
    await page.getByRole('link', { name: 'Evals' }).click();
    await page.getByRole('heading', { name: 'Evals', level: 1 }).waitFor();
    assert.equal(await page.locator('#primary-navigation').getAttribute('data-open'), 'false');
    // Route changes move focus to main; a fresh load must start at the skip link.
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'main');

    await page.reload();
    await page.locator('main h1').waitFor();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Skip to content');
    assert.equal(await page.evaluate(() => window.__injected), undefined);
    assert.deepEqual(await page.evaluate(() => window.__cspViolations), []);
    assert.deepEqual(errors, [], `page errors; missing files: ${missing.join(', ')}`);
    await context.close();
  }
  console.log(`Prototype checks passed for ${screens.length} screens in dark and light themes at 1360px and 390px.`);
  console.log(`Screenshots: ${artifacts}`);
} finally {
  if (browser) await browser.close();
  server.close();
}
