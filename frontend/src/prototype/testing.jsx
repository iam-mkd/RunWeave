import React, { useEffect, useState } from 'react';
import { DataTable, ErrorState, KeyValueList, LineChart, PageHeader, Panel, StatTile, StatusBadge } from '../components/index.js';
import { compactNumber, duration, percent } from '../format.js';
import {
  evalCases, evalDiff, failureCategories, latencySeries, loadSeconds, queueValues, savedReports, shadowGates, shadowPairs, throughputValues,
} from './fixtures.js';

const seconds = (value) => `${value}s`;

function toPoints(values, count) {
  return values.slice(0, count).map((value, index) => ({ x: loadSeconds[index], y: value }));
}

export function LoadTestsScreen() {
  const [ticks, setTicks] = useState(loadSeconds.length);
  const running = ticks < loadSeconds.length;
  useEffect(() => {
    if (!running) return undefined;
    const timer = setTimeout(() => setTicks((value) => value + 1), 400);
    return () => clearTimeout(timer);
  }, [running, ticks]);
  const shown = Math.max(ticks, 2);

  return <>
    <PageHeader eyebrow="Load tests" title="Load tests" description="Run an agent at increasing concurrency with seeded faults. Mock-model results measure RunWeave and your agent code, not provider capacity." />
    <div className="grid sidebar-layout">
      <Panel title="Scenario" id="scenario">
        <form className="stack" onSubmit={(event) => { event.preventDefault(); setTicks(0); }}>
          <div className="field"><label htmlFor="lt-agent">Agent</label>
            <select id="lt-agent" defaultValue="incident-triage"><option>incident-triage</option><option>support-bot</option><option>billing-assistant</option></select></div>
          <div className="field"><label htmlFor="lt-workers">Concurrent runs</label><input id="lt-workers" type="number" min="1" max="512" defaultValue="100" /></div>
          <div className="field"><label htmlFor="lt-total">Total runs</label><input id="lt-total" type="number" min="1" defaultValue="1000" /></div>
          <div className="field"><label htmlFor="lt-faults">Fault profile</label>
            <select id="lt-faults" defaultValue="default"><option value="none">No faults</option><option value="default">faults-default · seed 42</option><option value="chaos">chaos · seed 7</option></select></div>
          <div className="field"><label htmlFor="lt-model">Model</label>
            <select id="lt-model" defaultValue="mock"><option value="mock">Mock model (free, offline)</option><option value="live">Live · Claude (spend cap $5.00)</option></select>
            <span className="field-hint">Live load tests are capped by provider rate limits.</span></div>
          <button className="primary" disabled={running}>{running ? 'Running…' : 'Start load test'}</button>
        </form>
      </Panel>
      <div className="stack">
        <div className="row"><StatusBadge status={running ? 'RUNNING' : 'SUCCEEDED'} live /><span className="muted">
          {running ? `${loadSeconds[ticks]} s elapsed · live` : 'Completed 1,000 runs in 2m 0s'}</span></div>
        <div className="grid four">
          <StatTile label="Throughput" value="90.0 runs/s" />
          <StatTile label="Completion p95" value="926 ms" />
          <StatTile label="Error rate" value="1.2%" />
          <StatTile label="Peak active runs" value="100" />
        </div>
        <Panel title="Latency" id="latency">
          <LineChart title="Completion latency" series={latencySeries.map((line) => ({ name: line.name, points: toPoints(line.values, shown) }))}
            formatX={seconds} formatY={(value) => duration(value)} xLabel="Elapsed" />
        </Panel>
        <div className="grid two">
          <Panel title="Throughput" id="throughput"><LineChart title="Completed runs per second" series={[{ name: 'runs/s', points: toPoints(throughputValues, shown) }]} formatX={seconds} xLabel="Elapsed" height={180} /></Panel>
          <Panel title="Queue depth" id="queue"><LineChart title="Queued runs" series={[{ name: 'queued', points: toPoints(queueValues, shown) }]} formatX={seconds} xLabel="Elapsed" height={180} /></Panel>
        </div>
        <Panel title="Failure categories" id="failures">
          <div className="panel-body flush">
            <DataTable caption="Failure categories" captionHidden rowKey={(row) => row.id} rows={failureCategories} columns={[
              { key: 'category', label: 'Category' },
              { key: 'count', label: 'Count', numeric: true },
              { key: 'outcome', label: 'Outcome', render: (row) => <StatusBadge tone={row.outcome === 'Recovered' ? 'good' : row.outcome === 'Failed' ? 'critical' : row.outcome === 'Timed out' ? 'serious' : 'neutral'} label={row.outcome} /> },
            ]} />
          </div>
        </Panel>
        <Panel title="Saved reports" id="reports">
          <div className="panel-body flush">
            <DataTable caption="Saved load-test reports" captionHidden rowKey={(row) => row.id} rows={savedReports} columns={[
              { key: 'name', label: 'Report', render: (row) => <a href="#/load-tests">{row.name}</a> },
              { key: 'when', label: 'When' },
              { key: 'throughput', label: 'Runs/s', numeric: true },
              { key: 'p95', label: 'p95', numeric: true, render: (row) => duration(row.p95) },
              { key: 'errors', label: 'Errors', numeric: true, render: (row) => percent(row.errors) },
            ]} />
          </div>
        </Panel>
      </div>
    </div>
  </>;
}

export function EvalsScreen() {
  const [selected, setSelected] = useState('missing-evidence');
  const current = evalCases.find((row) => row.id === selected);
  return <>
    <PageHeader eyebrow="Evals" title="Evals" description="Run your dataset against an agent. Deterministic assertions gate CI; the run exits non-zero on failure."
      actions={<><select aria-label="Dataset" defaultValue="triage-v2"><option value="triage-v2">triage-v2 · 13 cases</option><option>triage-v1 · 9 cases</option></select>
        <button type="button" className="primary">Run eval</button></>} />
    <div className="grid four">
      <StatTile label="Cases passed" value="11 / 13" />
      <StatTile label="Assertions passed" value="64 / 68" />
      <StatTile label="vs baseline" value="−2 cases" delta={{ direction: 'down', good: false, text: 'Regression: exit code 1' }} />
      <StatTile label="Estimated cost" value="$0.0000" />
    </div>
    <div className="grid two spaced-top">
      <Panel title="Cases" id="cases">
        <div className="panel-body flush">
          <DataTable caption="Evaluation cases" captionHidden rowKey={(row) => row.id} rows={evalCases} selectedKey={selected}
            onRowClick={(row) => setSelected(row.id)} columns={[
              { key: 'id', label: 'Case', render: (row) => <button type="button" className="text-button mono" onClick={() => setSelected(row.id)}>{row.id}</button> },
              { key: 'status', label: 'Result', render: (row) => <StatusBadge tone={row.status === 'pass' ? 'good' : 'critical'} label={row.status === 'pass' ? 'Pass' : 'Fail'} /> },
              { key: 'assertions', label: 'Assertions', numeric: true },
              { key: 'ms', label: 'Latency', numeric: true, render: (row) => duration(row.ms) },
            ]} />
        </div>
      </Panel>
      <Panel title={current.id} id="case-detail" actions={<StatusBadge tone={current.status === 'pass' ? 'good' : 'critical'} label={current.status === 'pass' ? 'Pass' : 'Fail'} />}>
        {current.status === 'fail' ? <div className="stack">
          <ErrorState><ul className="plain">{evalDiff.failures.map((failure) => <li key={failure}>{failure}</li>)}</ul></ErrorState>
          <div className="grid two diff">
            <div><h3 className="diff-title">Expected</h3><pre className="diff-block">{evalDiff.expected.map((line, index) =>
              <span key={index} className={evalDiff.actual.includes(line) ? 'diff-line' : 'diff-line removed'}>{evalDiff.actual.includes(line) ? '  ' : '− '}{line}{'\n'}</span>)}</pre></div>
            <div><h3 className="diff-title">Actual</h3><pre className="diff-block">{evalDiff.actual.map((line, index) =>
              <span key={index} className={evalDiff.expected.includes(line) ? 'diff-line' : 'diff-line added'}>{evalDiff.expected.includes(line) ? '  ' : '+ '}{line}{'\n'}</span>)}</pre></div>
          </div>
        </div> : <KeyValueList items={[
          { label: 'Assertions', value: current.assertions },
          { label: 'Latency', value: duration(current.ms) },
          { label: 'Checks', value: 'schema, severity, citations, canary absence' },
        ]} />}
      </Panel>
    </div>
  </>;
}

const signed = (value) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(Math.round(value * 100))}%`;

export function ShadowScreen() {
  return <>
    <PageHeader eyebrow="Shadow" title="Shadow comparison" description="The candidate replays the same sanitized inputs in a separate queue and budget. Effects are simulated; only the primary result counts." />
    <div className="grid two">
      <div className="stat-tile"><div className="stat-label">Primary</div><div className="stat-value mono">incident-triage · baseline-v1</div></div>
      <div className="stat-tile"><div className="stat-label">Candidate</div><div className="stat-value mono">incident-triage · coordinated-v1</div></div>
    </div>
    <div className="spaced-top">
      <ErrorState action={<><button type="button" disabled>Promote candidate</button><button type="button">Keep primary</button></>}>
        <strong>Promotion blocked.</strong> The estimated-cost gate failed on 3 cases (131% of primary). All result, safety, and evidence gates passed.
      </ErrorState>
    </div>
    <div className="grid two spaced-top">
      <Panel title="Promotion gates" id="gates">
        <div className="panel-body flush">
          <DataTable caption="Promotion gates" captionHidden rowKey={(row) => row.id} rows={shadowGates} columns={[
            { key: 'gate', label: 'Gate' },
            { key: 'threshold', label: 'Threshold' },
            { key: 'candidate', label: 'Candidate' },
            { key: 'result', label: 'Result', render: (row) => <StatusBadge tone={row.tone} label={row.result} /> },
          ]} />
        </div>
      </Panel>
      <Panel title="Paired cases" id="pairs">
        <div className="panel-body flush">
          <DataTable caption="Paired case differences" captionHidden rowKey={(row) => row.id} rows={shadowPairs} columns={[
            { key: 'id', label: 'Case', render: (row) => <span className="mono">{row.id}</span> },
            { key: 'outcome', label: 'Outcome' },
            { key: 'latency', label: 'Δ latency', numeric: true, render: (row) => signed(row.latency) },
            { key: 'cost', label: 'Δ cost', numeric: true, render: (row) => <span className={row.cost > 0.05 ? 'over-threshold' : undefined}>{signed(row.cost)}</span> },
            { key: 'safety', label: 'Safety' },
          ]} />
        </div>
      </Panel>
    </div>
    <p className="muted spaced-top">Replays: {compactNumber(11)} sanitized cases · 2 excluded (unsanitized credential fixtures) · no external effects.</p>
  </>;
}
