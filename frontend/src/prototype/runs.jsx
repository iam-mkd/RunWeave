import React, { useState } from 'react';
import { DataTable, KeyValueList, PageHeader, Panel, StatTile, StatusBadge, Timeline } from '../components/index.js';
import { compactNumber, duration, sortRows, statusLabel, usd } from '../format.js';
import { runInput, runOutput, runs, runSteps } from './fixtures.js';

const columns = [
  { key: 'id', label: 'Run', render: (run) => <a className="mono" href={`#/runs/${run.id}`}>{run.id.slice(0, 8)}</a> },
  { key: 'agent', label: 'Agent', sortable: true },
  { key: 'status', label: 'State', sortable: true, render: (run) => <StatusBadge status={run.status} /> },
  { key: 'started', label: 'Started', sortable: true, render: (run) => <span className="mono">{run.started}</span> },
  { key: 'ms', label: 'Duration', numeric: true, sortable: true, render: (run) => duration(run.ms) },
  { key: 'steps', label: 'Steps', numeric: true, sortable: true },
  { key: 'tokens', label: 'Tokens', numeric: true, sortable: true, render: (run) => compactNumber(run.tokens) },
  { key: 'cost', label: 'Est. cost', numeric: true, sortable: true, render: (run) => usd(run.cost) },
];

export function RunsScreen() {
  const [sort, setSort] = useState({ key: 'started', direction: 'descending' });
  const [agent, setAgent] = useState('all');
  const [state, setState] = useState('all');
  const filtered = runs.filter((run) => (agent === 'all' || run.agent === agent) && (state === 'all' || run.status === state));
  return <>
    <PageHeader eyebrow="Runs" title="Runs" description="Every run across connected agents. Live updates stream from the server."
      actions={<a className="button primary" href="#/runs/new">New run</a>} />
    <div className="toolbar" role="group" aria-label="Filter runs">
      <label className="visually-hidden" htmlFor="filter-agent">Agent</label>
      <select id="filter-agent" value={agent} onChange={(event) => setAgent(event.target.value)}>
        <option value="all">All agents</option>{[...new Set(runs.map((run) => run.agent))].map((name) => <option key={name}>{name}</option>)}
      </select>
      <label className="visually-hidden" htmlFor="filter-state">State</label>
      <select id="filter-state" value={state} onChange={(event) => setState(event.target.value)}>
        <option value="all">All states</option>{[...new Set(runs.map((run) => run.status))].map((value) => <option key={value} value={value}>{statusLabel(value)}</option>)}
      </select>
      <label className="visually-hidden" htmlFor="filter-id">Run ID</label>
      <input id="filter-id" className="mono" placeholder="Filter by run ID" />
      <span className="muted toolbar-count">{filtered.length} of {runs.length} runs</span>
    </div>
    <Panel title="Recent runs" id="runs-table">
      <div className="panel-body flush">
        <DataTable caption="Runs" captionHidden columns={columns} rows={sortRows(filtered, sort.key, sort.direction)} rowKey={(run) => run.id}
          sort={sort} onSort={setSort} empty="No runs match these filters." />
      </div>
    </Panel>
  </>;
}

const endings = {
  FAILED: { title: 'Model final answer', status: 'FAILED', duration: '96 ms', meta: 'Output failed schema validation', detail: 'severity: expected one of low, medium, high; got "urgent"' },
  TIMED_OUT: { title: 'Tool · runbook.search', status: 'TIMED_OUT', duration: '5.00 s', meta: 'Step deadline exceeded (5 s)' },
  CANCELLED: { title: 'Cancelled by operator', status: 'CANCELLED', duration: '—', meta: 'Cleanup finished; worker slot released' },
  WAITING_APPROVAL: { title: 'Approval requested · create_ticket', status: 'WAITING_APPROVAL', duration: '—', meta: 'Expires in 54 min · worker slot released' },
  RUNNING: { title: 'Tool · runbook.search', status: 'RUNNING', duration: '0.8 s', meta: 'In progress' },
  QUEUED: { title: 'Waiting for a worker', status: 'QUEUED', duration: '—', meta: 'Position 3 in queue' },
};

function stepsFor(run) {
  if (run.status === 'SUCCEEDED') return runSteps;
  if (run.status === 'QUEUED') return [{ id: 'end', ...endings.QUEUED }];
  return [...runSteps.slice(0, 4), { id: 'end', ...endings[run.status] }];
}

const terminalStates = new Set(['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED']);

export function NewRunScreen() {
  return <>
    <PageHeader eyebrow={<><a href="#/runs">Runs</a> / New</>} title="New run" description="The form is generated from the agent’s declared input schema." />
    <Panel title="Input for incident-triage" id="new-run">
      <form className="stack narrow" onSubmit={(event) => { event.preventDefault(); location.hash = `#/runs/${runs[2].id}`; }}>
        <div className="field"><label htmlFor="run-agent">Agent</label>
          <select id="run-agent" defaultValue="incident-triage"><option>incident-triage</option><option>support-bot</option><option>billing-assistant</option></select></div>
        <div className="field"><label htmlFor="run-service">service <span className="muted">string · required</span></label>
          <input id="run-service" className="mono" defaultValue="payments" /></div>
        <div className="field"><label htmlFor="run-description">description <span className="muted">string · 1–2000 chars</span></label>
          <textarea id="run-description" defaultValue="Latency rose after the last deploy." /></div>
        <div className="field"><label htmlFor="run-faults">Fault profile</label>
          <select id="run-faults" defaultValue="none"><option value="none">No faults</option><option>faults-default</option><option>chaos</option></select></div>
        <div className="row end"><a className="button" href="#/runs">Cancel</a><button className="primary">Start run</button></div>
      </form>
    </Panel>
  </>;
}

export function RunDetailScreen({ id }) {
  const run = runs.find((candidate) => candidate.id === id) ?? runs[0];
  const steps = stepsFor(run);
  return <>
    <PageHeader eyebrow={<><a href="#/runs">Runs</a> / {run.agent}</>} title={<span className="mono title-id">{run.id}</span>}
      actions={<><StatusBadge status={run.status} live /><button type="button">Re-run</button>
        <button type="button" disabled={terminalStates.has(run.status)}>Cancel</button></>} />
    <div className="grid four">
      <StatTile label="Duration" value={duration(run.ms)} />
      <StatTile label="Steps" value={`${steps.length} of 7`} />
      <StatTile label="Tokens" value={compactNumber(run.tokens)} />
      <StatTile label="Estimated cost" value={usd(run.cost)} />
    </div>
    <div className="grid detail-layout spaced-top">
      <Panel title="Timeline" id="timeline"><Timeline steps={steps} /></Panel>
      <div className="stack">
        <Panel title="Input" id="input"><pre>{runInput}</pre></Panel>
        <Panel title="Output" id="output">{run.status === 'SUCCEEDED' ? <pre>{runOutput}</pre>
          : <p className="muted">No validated output — the run {statusLabel(run.status).toLowerCase()}.</p>}</Panel>
        <Panel title="Budget and limits" id="budget">
          <KeyValueList items={[
            { label: 'Deadline', value: '2.31 s of 30 s' },
            { label: 'Model requests', value: '2 of 20' },
            { label: 'Spend cap', value: '$0.0000 of $0.0500' },
            { label: 'Retries', value: '1 (rate limited)' },
            { label: 'Fault profile', value: 'faults-default · seed 42' },
            { label: 'Trace', value: <code>4bf92f35…a3ce</code> },
          ]} />
        </Panel>
      </div>
    </div>
  </>;
}
