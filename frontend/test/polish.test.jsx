import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, test } from 'vitest';
import { sortRuns, timeLabel } from '../src/screens/RunsScreen.jsx';
import { SetupScreen } from '../src/screens/SetupScreen.jsx';
import { EvalResults, EvalsScreen, ShadowResults, benchSummary, gateMeasured } from '../src/screens/testing.jsx';

const render = (element) => renderToStaticMarkup(element);

const run = (id, submitted, startedMs, finishedMs, tokens, cost) => ({
  id, agent: 'a', status: 'SUCCEEDED', submitted_at: submitted,
  started_at: new Date(startedMs).toISOString(), finished_at: new Date(finishedMs).toISOString(),
  usage: tokens === null ? null : { input_tokens: tokens, output_tokens: 1, estimated_cost_usd: cost },
});

describe('Runs table', () => {
  const rows = [
    run('r1', '2026-11-20T10:00:00Z', 0, 300, 5, '0.0002'),
    run('r2', '2026-11-20T11:00:00Z', 0, 100, null, null),
    run('r3', '2026-11-20T09:00:00Z', 0, 200, 50, '0.01'),
  ];
  const ids = (sorted) => sorted.map((row) => row.id);

  test('sorts loaded runs by derived duration, tokens and cost', () => {
    assert.deepEqual(ids(sortRuns(rows, { key: 'duration', direction: 'ascending' })), ['r2', 'r3', 'r1']);
    assert.deepEqual(ids(sortRuns(rows, { key: 'tokens', direction: 'descending' })), ['r3', 'r1', 'r2']);
    assert.deepEqual(ids(sortRuns(rows, { key: 'cost', direction: 'ascending' })), ['r2', 'r1', 'r3']);
    assert.deepEqual(ids(sortRuns(rows, { key: 'submitted_at', direction: 'descending' })), ['r2', 'r1', 'r3']);
    assert.deepEqual(ids(sortRuns(rows, null)), ['r1', 'r2', 'r3']);
  });

  test('times are 24-hour', () => {
    assert.match(timeLabel('2026-11-20T23:05:09Z'), /^\d{2}:\d{2}:\d{2}$/);
  });
});

const shadowReport = {
  kind: 'shadow', id: 'shadow-20261120T000000Z-abcdef', promotion_eligible: false,
  dataset: { name: 'smoke' }, environment: { mode: 'in-process' },
  primary: { agent: { name: 'a', version: '1' }, pass_rate: 1, p95_latency_ms: 7.5, cost_usd: '0.002', tokens: 10 },
  candidate: { agent: { name: 'a', version: '2' }, pass_rate: 0.75, p95_latency_ms: 6.8, cost_usd: '0.003', tokens: 12 },
  gates: { candidate_pass_rate_met: false, no_paired_regressions: false, outcome_parity: true,
    candidate_cost_within_limit: false, candidate_latency_within_limit: true },
  settings: { min_pass_rate: 1, max_cost_ratio: 1.05, max_latency_ratio: 2, latency_slack_ms: 50 },
  regressions: ['b'], fixed: [],
  pairs: ['a', 'b', 'c', 'd'].map((id) => ({ id, primary: { status: 'SUCCEEDED' }, candidate: { status: id === 'd' ? 'FAILED' : 'SUCCEEDED' },
    differences: { status_equal: id !== 'd', output_equal: id === 'a', latency_ms: 0, tokens: 0, cost_usd: '0', model_calls: 0,
      newly_failing: [], regressed: id === 'b', fixed: false } })),
  limitations: '',
};

describe('Shadow gates', () => {
  test('each gate shows what the candidate measured', () => {
    assert.equal(gateMeasured('candidate_pass_rate_met', shadowReport), '75.0%');
    assert.equal(gateMeasured('no_paired_regressions', shadowReport), '1 regressed');
    assert.equal(gateMeasured('outcome_parity', shadowReport), '3 of 4');
    assert.equal(gateMeasured('outcome_parity', { ...shadowReport, settings: { require_output_parity: true } }), '1 of 4');
    assert.equal(gateMeasured('candidate_cost_within_limit', shadowReport), '150%');
    assert.equal(gateMeasured('candidate_cost_within_limit', { ...shadowReport, primary: { ...shadowReport.primary, cost_usd: '0' } }), '$0.0030');
    assert.equal(gateMeasured('candidate_latency_within_limit', shadowReport), '6.8 ms');
    const html = render(<ShadowResults report={shadowReport} />);
    assert.match(html, /<th[^>]*>Candidate<\/th>[\s\S]*1 regressed/);
  });
});

describe('Load test summary', () => {
  test('a saved report is summarised by its best throughput, highest level, failures and levels', () => {
    const report = { cases: [
      { fault_profile: '', concurrency: 1, throughput_rps: 93.1, latency_ms: { p95: 2.1 }, failures: {} },
      { fault_profile: '', concurrency: 16, throughput_rps: 120.4, latency_ms: { p95: 20.2 }, failures: { 'FAILED:x': 2 } },
      { fault_profile: 'flaky', concurrency: 16, throughput_rps: 75.8, latency_ms: { p95: 25.8 }, failures: { 'FAILED:y': 8 } },
    ] };
    assert.deepEqual(benchSummary(report), [
      { label: 'Best throughput', value: '120.4 runs/s' },
      { label: 'p95 at concurrency 16', value: '25.8 ms' },
      { label: 'Failed runs', value: '10' },
      { label: 'Levels', value: '3' },
    ]);
  });
});

describe('copy and states', () => {
  test('evals describe the CLI as code and show the run status as a badge', () => {
    const header = render(<EvalsScreen navigate={() => {}} />);
    assert.doesNotMatch(header, /`/);
    assert.match(header, /<code>runweave eval<\/code>/);
    const report = {
      kind: 'eval', id: 'eval-20261120T000000Z-abcdef', agent: { name: 'a', version: '1' }, dataset: { name: 's', cases: 1 },
      environment: { mode: 'in-process' }, baseline: null, gates: { passed: true, reasons: [] },
      summary: { total: 1, passed: 1, failed: 0, pass_rate: 1, p95_latency_ms: 1, tokens: 0, cost_usd: '0' },
      cases: [{ id: 'one', passed: true, status: 'TIMED_OUT', latency_ms: 1, tokens: 0, cost_usd: '0', assertions: [] }],
    };
    const html = render(<EvalResults report={report} selected="one" onSelect={() => {}} />);
    assert.doesNotMatch(html, />TIMED_OUT</);
  });

  test('setup no longer promises a past milestone', () => {
    const html = render(<SetupScreen />);
    assert.doesNotMatch(html, /milestone/i);
    assert.match(html, /LLM proxy/);
  });
});
