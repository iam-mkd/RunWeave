import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, test, vi } from 'vitest';
import {
  cancelJob, getJob, getProject, getReport, isJobId, isReportId, listJobs, listReports, startJob,
} from '../src/api.js';
import { productionSections } from '../src/navigation.js';
import { href, parseRoute } from '../src/router.js';
import {
  BenchResults, EvalResults, NoProject, ReportList, ShadowResults, levelsText, liveSeries, parseLevels,
} from '../src/screens/testing.jsx';

const render = (element) => renderToStaticMarkup(element);
const hostile = '<img src=x onerror="alert(1)">';
const reportId = 'bench-20261119T103451Z-abe7da';
const jobId = 'job-0123456789ab';
afterEach(() => vi.restoreAllMocks());

describe('jobs and reports API', () => {
  test('calls the jobs API with validated identifiers', async () => {
    const calls = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
      calls.push([path, options.method, options.body]);
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    await getProject();
    await startJob({ kind: 'bench', concurrency: [1, 4], runs: 20 });
    await getJob(jobId, 12);
    await cancelJob(jobId);
    await listJobs();
    await listReports('eval');
    await getReport(reportId);
    assert.deepEqual(calls, [
      ['/v1/jobs/project', 'GET', undefined],
      ['/v1/jobs', 'POST', JSON.stringify({ kind: 'bench', concurrency: [1, 4], runs: 20 })],
      [`/v1/jobs/${jobId}?after=12`, 'GET', undefined],
      [`/v1/jobs/${jobId}/cancel`, 'POST', JSON.stringify({})],
      ['/v1/jobs', 'GET', undefined],
      ['/v1/reports?kind=eval', 'GET', undefined],
      [`/v1/reports/${reportId}`, 'GET', undefined],
    ]);
    await assert.rejects(getJob('../runs'), /Invalid job/);
    await assert.rejects(cancelJob('job-xyz'), /Invalid job/);
    await assert.rejects(getReport('../../secret'), /Invalid report/);
    await assert.rejects(listReports('deploy'), /Unknown report kind/);
  });

  test('recognises job and report identifiers', () => {
    assert.ok(isJobId(jobId) && !isJobId('job-01234') && !isJobId(`${jobId}/x`));
    assert.ok(isReportId(reportId) && isReportId('eval-20261119T000000Z-000000'));
    assert.ok(!isReportId('bench-latest') && !isReportId('run-20261119T000000Z-abcdef'));
  });
});

describe('navigation and routes', () => {
  test('the testing screens are in the shipped workspace', () => {
    assert.deepEqual(productionSections.map((section) => section.id),
      ['agents', 'runs', 'load-tests', 'evals', 'shadow', 'settings']);
  });

  test('testing routes accept only a valid report id', () => {
    assert.deepEqual(parseRoute('#/load-tests', ''), { screen: 'load-tests', params: {} });
    assert.deepEqual(parseRoute(`#/load-tests?report=${reportId}`, ''), { screen: 'load-tests', params: { report: reportId } });
    assert.deepEqual(parseRoute('#/evals?report=../x', ''), { screen: 'evals', params: {} });
    assert.deepEqual(parseRoute('#/shadow', ''), { screen: 'shadow', params: {} });
    assert.equal(href({ screen: 'evals', params: { report: 'eval-20261119T000000Z-abcdef' } }),
      '#/evals?report=eval-20261119T000000Z-abcdef');
    assert.equal(href({ screen: 'shadow', params: { report: '<b>' } }), '#/shadow');
  });
});

describe('load-test helpers', () => {
  test('live series come from tick events, in seconds', () => {
    const series = liveSeries([
      { event: 'level_started', concurrency: 2 },
      { event: 'tick', elapsed_ms: 500, throughput_rps: 10, p95_latency_ms: 12.5, completed: 5 },
      { event: 'level', concurrency: 2 },
      { event: 'tick', elapsed_ms: 1000, throughput_rps: 20, p95_latency_ms: null, completed: 9 },
    ]);
    assert.deepEqual(series.throughput, [{ x: 0.5, y: 10 }, { x: 1, y: 20 }]);
    assert.deepEqual(series.p95, [{ x: 0.5, y: 12.5 }, { x: 1, y: 0 }]);
    assert.deepEqual(liveSeries([]), { throughput: [], p95: [] });
  });

  test('concurrency levels are parsed strictly', () => {
    assert.deepEqual(parseLevels('16 1, 4 4'), [1, 4, 16]);
    assert.equal(parseLevels('1 0'), null);
    assert.equal(parseLevels('1 x'), null);
    assert.equal(parseLevels(''), null);
    assert.equal(parseLevels('1 2 3 4 5 6 7 8 9 10 11 12 13'), null);
    assert.equal(levelsText([1, 4, 16]), '1 4 16');
  });
});

const benchReport = {
  kind: 'bench', id: reportId, created_at: '2026-11-19T10:34:51+00:00',
  agent: { name: hostile, version: '0.1.0', kind: 'python' },
  dataset: { name: 'smoke', inputs: 3 },
  environment: { mode: 'in-process', logical_cpus: 16, memory_mib: 15791, python: '3.13.9', platform: 'Windows-11' },
  settings: { concurrency: [1, 16], runs: 128, fault_profiles: ['', 'chaos'], soak_seconds: 0 },
  cases: [
    { fault_profile: '', concurrency: 1, runs: 128, duration_ms: 1375, throughput_rps: 93.1,
      latency_ms: { p50: 1.2, p95: 2.1, p99: 3 }, queue_wait_ms: { p50: 0.5, p95: 1.2, p99: 1.5 },
      status_api_ms: { p50: 3, p95: 13, p99: 20 }, status_api_samples: 20, loop_lag_p99_bucket_ms: 25,
      memory: { rss_start_mib: 100, rss_peak_mib: 102.14, rss_end_mib: 101 }, peak_active_runs: 1,
      outcomes: { SUCCEEDED: 128 }, failures: {}, tokens: 0, cost_usd: '0', model_calls: 0, admission_retries: 0 },
    { fault_profile: 'chaos', concurrency: 16, runs: 128, duration_ms: 1400, throughput_rps: 75.8,
      latency_ms: { p50: 12.4, p95: 25.8, p99: 28.9 }, queue_wait_ms: { p50: 3, p95: 11, p99: 12 },
      status_api_ms: { p50: 100, p95: 873.7, p99: 900 }, status_api_samples: 18, loop_lag_p99_bucket_ms: 50,
      memory: null, peak_active_runs: 16, outcomes: { SUCCEEDED: 120, FAILED: 8 },
      failures: { [`FAILED:${hostile}`]: 8 }, tokens: 1200, cost_usd: '0.0012', model_calls: 128, admission_retries: 0 },
  ],
  soak: null,
  gates: { passed: false, reasons: [`failure rate 6% at concurrency 16 ${hostile}`] },
  limitations: 'Shared event loop.',
};

describe('result views', () => {
  test('bench results list every level, failure categories and gates, escaping agent text', () => {
    const html = render(<BenchResults report={benchReport} />);
    assert.match(html, /93\.1/);
    assert.match(html, /chaos/);
    assert.match(html, /No faults/);
    assert.match(html, /25\.8/);
    assert.match(html, /FAILED:/);
    assert.match(html, /Gates failed/);
    assert.match(html, /16 logical CPUs/);
    assert.match(html, /Shared event loop\./);
    assert.doesNotMatch(html, /<img/);
  });

  test('eval results show cases and why a selected case failed', () => {
    const report = {
      kind: 'eval', id: 'eval-20261119T000000Z-abcdef', created_at: '2026-11-19T10:00:00+00:00',
      agent: { name: 'word-count', version: '0.1.0', kind: 'python' }, dataset: { name: 'smoke', cases: 2 },
      environment: { mode: 'in-process' },
      summary: { total: 2, passed: 1, failed: 1, pass_rate: 0.5, p95_latency_ms: 7, tokens: 0, cost_usd: '0' },
      gates: { passed: false, reasons: ['pass rate 50% is below the minimum 100%'] },
      baseline: { regressions: ['wrong'], fixed: [], new: [], report_id: 'eval-x' },
      cases: [
        { id: 'ok', passed: true, status: 'SUCCEEDED', latency_ms: 5, tokens: 0, cost_usd: '0', assertions: [{ name: 'output', passed: true, message: '' }] },
        { id: hostile, passed: false, status: 'SUCCEEDED', latency_ms: 7, tokens: 0, cost_usd: '0',
          assertions: [{ name: 'output', passed: false, message: `words: expected 3, got ${hostile}` }] },
      ],
    };
    const html = render(<EvalResults report={report} selected={hostile} onSelect={() => {}} />);
    assert.match(html, /1 \/ 2/);
    assert.match(html, /below the minimum/);
    assert.match(html, /Regressed against the baseline/);
    assert.match(html, /words: expected 3/);
    assert.doesNotMatch(html, /<img/);
  });

  test('shadow results show both versions, every gate and paired differences', () => {
    const report = {
      kind: 'shadow', id: 'shadow-20261119T000000Z-abcdef', promotion_eligible: false,
      dataset: { name: 'smoke', cases: 1 }, environment: { mode: 'in-process' },
      primary: { agent: { name: 'word-count', version: '0.1.0' }, pass_rate: 1, p95_latency_ms: 7.5, cost_usd: '0', tokens: 0 },
      candidate: { agent: { name: 'word-count', version: hostile }, pass_rate: 0.75, p95_latency_ms: 6.8, cost_usd: '0', tokens: 0 },
      gates: { candidate_pass_rate_met: false, no_paired_regressions: false, outcome_parity: true,
        candidate_cost_within_limit: true, candidate_latency_within_limit: true },
      settings: { max_cost_ratio: 1.05, max_latency_ratio: 2, latency_slack_ms: 50, min_pass_rate: 1 },
      regressions: ['finds-longest'], fixed: [],
      pairs: [{ id: 'finds-longest', primary: { status: 'SUCCEEDED', passed: true }, candidate: { status: 'SUCCEEDED', passed: false },
        differences: { status_equal: true, output_equal: false, latency_ms: -0.7, tokens: 0, cost_usd: '0', model_calls: 0,
          newly_failing: ['contains'], regressed: true, fixed: false } }],
      limitations: 'traffic is not switched to the candidate.',
    };
    const html = render(<ShadowResults report={report} />);
    assert.match(html, /Not eligible/);
    assert.match(html, /No paired regressions/);
    assert.match(html, /finds-longest/);
    assert.match(html, /Regressed/);
    assert.match(html, /contains/);
    assert.match(html, /not switched/);
    assert.doesNotMatch(html, /<img/);
  });

  test('saved reports link to their screen and a missing project explains what to do', () => {
    const html = render(<ReportList screen="load-tests" reports={[
      { id: reportId, kind: 'bench', created_at: '2026-11-19T10:34:51+00:00', agent: { name: hostile, version: '1' },
        dataset: 'smoke', passed: true, headline: { levels: 2, throughput_rps: 93.1, p95_latency_ms: 25.8, failures: 8 } },
    ]} />);
    assert.match(html, new RegExp(`href="#/load-tests\\?report=${reportId}"`));
    assert.doesNotMatch(html, /<img/);
    const missing = render(<NoProject />);
    assert.match(missing, /runweave init/);
    assert.match(missing, /runweave serve/);
  });
});
