// WCAG 2.1 AA checks for the browser smoke test: axe-core on rendered screens, plus the
// keyboard and motion checks axe cannot do. Findings are collected across the whole run
// and reported together, so one run lists everything that needs fixing.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
// Evaluated through the DevTools protocol, which the page's CSP (script-src 'self') does
// not govern, so the smoke test's own CSP checks still mean something.
const axeSource = readFileSync(require.resolve('axe-core'), 'utf8');
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];

// Rule results we accept, each with the reason (also listed in docs/design-system.md).
export const ACCEPTED = [];

const findings = [];
let audited = 0;

function accepted(violation, node) {
  return ACCEPTED.some((rule) => rule.id === violation.id && (!rule.target || node.target.join(' ').includes(rule.target)));
}

export async function audit(page, label) {
  if (!(await page.evaluate(() => typeof window.axe === 'object'))) await page.evaluate(axeSource);
  const result = await page.evaluate(async (tags) => {
    const outcome = await window.axe.run(document, { runOnly: { type: 'tag', values: tags }, resultTypes: ['violations'] });
    return outcome.violations.map((violation) => ({
      id: violation.id, impact: violation.impact, help: violation.help, helpUrl: violation.helpUrl,
      nodes: violation.nodes.map((node) => ({ target: node.target.map(String), summary: node.failureSummary })),
    }));
  }, TAGS);
  audited += 1;
  for (const violation of result) {
    for (const node of violation.nodes) {
      if (accepted(violation, node)) continue;
      findings.push({ screen: label, url: page.url(), rule: violation.id, impact: violation.impact, help: violation.help,
        helpUrl: violation.helpUrl, target: node.target.join(' '), summary: node.summary });
    }
  }
}

// Lets resize observers and the re-render they cause run before measuring.
function settle(page) {
  return page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

/** Audits a screen at desktop width and at 390 px, and checks it does not scroll sideways. */
export async function checkScreen(page, label) {
  await audit(page, `${label} (desktop)`);
  await page.setViewportSize({ width: 390, height: 844 });
  await settle(page);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${label} overflows at 390px`);
  await audit(page, `${label} (390 px)`);
  await page.setViewportSize({ width: 1360, height: 980 });
  await settle(page);
}

export function report(path) {
  const unique = new Map();
  for (const finding of findings) unique.set(`${finding.screen}|${finding.rule}|${finding.target}`, finding);
  const list = [...unique.values()];
  writeFileSync(path, `${JSON.stringify({ audited, violations: list }, null, 2)}\n`);
  const lines = list.map((f) => `- [${f.impact}] ${f.rule} on ${f.screen}: ${f.target} (${f.help})`);
  assert.equal(list.length, 0, `Accessibility violations (${list.length}):\n${lines.join('\n')}\nDetails: ${path}`);
  return audited;
}

/** The element that has focus, described for assertions. */
export function focused(page) {
  return page.evaluate(() => {
    const element = document.activeElement;
    if (!element || element === document.body) return null;
    return {
      tag: element.tagName.toLowerCase(), id: element.id || null, text: (element.textContent || '').trim().slice(0, 60),
      label: element.getAttribute('aria-label'), href: element.getAttribute('href'), current: element.getAttribute('aria-current'),
    };
  });
}

/** With reduced motion requested, nothing on the page animates or transitions. */
export async function assertNoMotion(page, label) {
  const moving = await page.evaluate(() => [...document.querySelectorAll('*')].flatMap((element) => {
    const style = getComputedStyle(element);
    const seconds = (value) => Math.max(...value.split(',').map((part) => (part.trim().endsWith('ms') ? parseFloat(part) / 1000 : parseFloat(part) || 0)));
    const animated = style.animationName !== 'none' && seconds(style.animationDuration) > 0.01;
    const transitioned = seconds(style.transitionDuration) > 0.01;
    return animated || transitioned ? [`${element.tagName.toLowerCase()}.${[...element.classList].join('.')}`] : [];
  }));
  assert.deepEqual(moving, [], `${label}: elements still animate with reduced motion: ${moving.slice(0, 10).join(', ')}`);
}
