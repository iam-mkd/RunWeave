import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, test } from 'vitest';
import { Pager, downsample, pageWindow } from '../src/components/index.js';
import { EvalResults, ShadowResults, visibleCases } from '../src/screens/testing.jsx';

const render = (element) => renderToStaticMarkup(element);

describe('pageWindow', () => {
  test('pages of a fixed size, clamped to the rows that exist', () => {
    assert.deepEqual(pageWindow(0, 0, 100), { page: 0, pages: 1, start: 0, end: 0, total: 0 });
    assert.deepEqual(pageWindow(40, 0, 100), { page: 0, pages: 1, start: 0, end: 40, total: 40 });
    assert.deepEqual(pageWindow(250, 2, 100), { page: 2, pages: 3, start: 200, end: 250, total: 250 });
    assert.deepEqual(pageWindow(250, 9, 100), { page: 2, pages: 3, start: 200, end: 250, total: 250 });
    assert.deepEqual(pageWindow(250, -3, 100), { page: 0, pages: 3, start: 0, end: 100, total: 250 });
  });
});

describe('Pager', () => {
  test('announces the rows shown and disables buttons at the ends', () => {
    const html = render(<Pager label="Cases" window={pageWindow(5000, 1, 100)} onPage={() => {}} />);
    assert.match(html, /<nav[^>]*aria-label="Cases pages"/);
    assert.match(html, /aria-live="polite"/);
    assert.match(html, /Rows 101–200 of 5,000/);
    assert.doesNotMatch(html, /disabled=""[^>]*>Previous/);
    const first = render(<Pager label="Cases" window={pageWindow(5000, 0, 100)} onPage={() => {}} />);
    assert.match(first, /<button[^>]*disabled=""[^>]*>Previous/);
    const last = render(<Pager label="Cases" window={pageWindow(5000, 49, 100)} onPage={() => {}} />);
    assert.match(last, /<button[^>]*disabled=""[^>]*>Next/);
  });

  test('a single page needs no pager', () => {
    assert.equal(render(<Pager label="Cases" window={pageWindow(40, 0, 100)} onPage={() => {}} />), '');
  });
});

describe('downsample', () => {
  test('keeps short series and bounds long ones while keeping spikes', () => {
    const short = [{ x: 0, y: 1 }, { x: 1, y: 2 }];
    assert.deepEqual(downsample([{ name: 'a', points: short }], 400)[0].points, short);
    const points = Array.from({ length: 2000 }, (_, i) => ({ x: i, y: i === 1234 ? 999 : i % 7 }));
    const [line] = downsample([{ name: 'a', points }], 400);
    assert.ok(line.points.length <= 400, `${line.points.length} points`);
    assert.ok(line.points.some((p) => p.y === 999), 'the spike survives');
    assert.deepEqual(line.points.at(0), points.at(0));
    assert.deepEqual(line.points.at(-1), points.at(-1));
    const xs = line.points.map((p) => p.x);
    assert.deepEqual(xs, [...xs].sort((a, b) => a - b));
  });

  test('several series stay aligned on the same x values', () => {
    const a = Array.from({ length: 1000 }, (_, i) => ({ x: i, y: Math.sin(i) }));
    const b = a.map((p) => ({ x: p.x, y: p.y * 2 }));
    const [first, second] = downsample([{ name: 'a', points: a }, { name: 'b', points: b }], 300);
    assert.deepEqual(first.points.map((p) => p.x), second.points.map((p) => p.x));
  });
});

function evalReport(total, failing) {
  return {
    kind: 'eval', id: 'eval-20261119T000000Z-abcdef', created_at: '2026-11-19T10:00:00+00:00',
    agent: { name: 'word-count', version: '1', kind: 'python' }, dataset: { name: 'big', cases: total },
    environment: { mode: 'in-process' },
    summary: { total, passed: total - failing, failed: failing, pass_rate: (total - failing) / total, p95_latency_ms: 3, tokens: 0, cost_usd: '0' },
    gates: { passed: failing === 0, reasons: [] }, baseline: null,
    cases: Array.from({ length: total }, (_, i) => ({
      id: `case-${String(i).padStart(4, '0')}`, passed: i >= failing, status: 'SUCCEEDED', latency_ms: 1, tokens: 0, cost_usd: '0',
      assertions: [{ name: 'output', passed: i >= failing, message: i >= failing ? '' : 'expected 3' }],
    })),
  };
}

describe('large reports', () => {
  test('eval cases render one page at a time, failures first by default', () => {
    const html = render(<EvalResults report={evalReport(5000, 0)} selected={null} onSelect={() => {}} />);
    assert.equal((html.match(/<tr /g) ?? []).length, 100);
    assert.match(html, /Rows 1–100 of 5,000/);
    const report = evalReport(5000, 30);
    const failing = render(<EvalResults report={report} selected={null} onSelect={() => {}} />);
    assert.match(failing, /Failed only/);
    assert.equal((failing.match(/<tr /g) ?? []).length, 30);
  });

  test('case filters: failed only and a case id search', () => {
    const { cases } = evalReport(300, 12);
    assert.equal(visibleCases(cases, { failedOnly: true, search: '' }).length, 12);
    assert.equal(visibleCases(cases, { failedOnly: false, search: 'case-02' }).length, 100);
    assert.equal(visibleCases(cases, { failedOnly: true, search: 'case-0011' }).length, 1);
  });

  test('shadow pairs page and can show only changed cases', () => {
    const pairs = Array.from({ length: 1200 }, (_, i) => ({
      id: `pair-${i}`, primary: { status: 'SUCCEEDED', passed: true }, candidate: { status: 'SUCCEEDED', passed: i % 100 !== 0 },
      differences: { status_equal: true, output_equal: true, latency_ms: 0, tokens: 0, cost_usd: '0', model_calls: 0,
        newly_failing: i % 100 === 0 ? ['output'] : [], regressed: i % 100 === 0, fixed: false },
    }));
    const report = {
      kind: 'shadow', id: 'shadow-20261119T000000Z-abcdef', promotion_eligible: false, dataset: { name: 'big' }, environment: { mode: 'in-process' },
      primary: { agent: { name: 'a', version: '1' }, pass_rate: 1, p95_latency_ms: 1, cost_usd: '0', tokens: 0 },
      candidate: { agent: { name: 'a', version: '2' }, pass_rate: 0.99, p95_latency_ms: 1, cost_usd: '0', tokens: 0 },
      gates: { candidate_pass_rate_met: false, no_paired_regressions: false, outcome_parity: true, candidate_cost_within_limit: true, candidate_latency_within_limit: true },
      settings: {}, regressions: [], fixed: [], pairs, limitations: '',
    };
    const html = render(<ShadowResults report={report} />);
    assert.match(html, /Changed only/);
    assert.match(html, /12 changed of 1,200 pairs/);
  });
});
