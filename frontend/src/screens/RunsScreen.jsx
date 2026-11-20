import React, { useEffect, useState } from 'react';
import { isRunId, listAgents, listRuns, terminal } from '../api.js';
import { DataTable, ErrorState, LoadingState, PageHeader, Pager, Panel, StatusBadge, usePages } from '../components/index.js';
import { compactNumber, duration, sortRows, statusLabel, usd } from '../format.js';
import { href } from '../router.js';

const statuses = ['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED'];
const refreshMs = 3000;

function runDuration(run) {
  if (!run.started_at) return null;
  return Math.max(0, (run.finished_at ? Date.parse(run.finished_at) : Date.now()) - Date.parse(run.started_at));
}

export function timeLabel(value) {
  return new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
}

const sortValues = {
  duration: (run) => runDuration(run) ?? -1,
  tokens: (run) => (run.usage ? run.usage.input_tokens + run.usage.output_tokens : -1),
  cost: (run) => (run.usage ? Number(run.usage.estimated_cost_usd) : -1),
};

/** Sorts the loaded runs by a column; derived columns sort by their number, missing values first. */
export function sortRuns(rows, sort) {
  if (!sort?.key) return rows;
  const value = sortValues[sort.key];
  if (!value) return sortRows(rows, sort.key, sort.direction);
  const keyed = rows.map((run) => ({ run, [sort.key]: value(run) }));
  return sortRows(keyed, sort.key, sort.direction).map((entry) => entry.run);
}

const columns = [
  { key: 'id', label: 'Run', render: (run) => <a className="mono" href={href({ screen: 'run', id: run.id })} aria-label={`Open run ${run.id}`}>{run.id.slice(0, 8)}</a> },
  { key: 'agent', label: 'Agent', sortable: true },
  { key: 'status', label: 'State', sortable: true, render: (run) => <StatusBadge status={run.status} /> },
  { key: 'submitted_at', label: 'Submitted', sortable: true, render: (run) => <span className="mono">{timeLabel(run.submitted_at)}</span> },
  { key: 'duration', label: 'Duration', numeric: true, sortable: true, render: (run) => duration(runDuration(run)) },
  { key: 'tokens', label: 'Tokens', numeric: true, sortable: true, render: (run) => run.usage ? compactNumber(run.usage.input_tokens + run.usage.output_tokens) : '—' },
  { key: 'cost', label: 'Est. cost', numeric: true, sortable: true, render: (run) => run.usage ? usd(run.usage.estimated_cost_usd) : '—' },
];

export function RunsScreen({ params, navigate }) {
  const [agents, setAgents] = useState([]);
  const [runs, setRuns] = useState(null);
  const [more, setMore] = useState([]);
  const [error, setError] = useState('');
  const [lookup, setLookup] = useState('');
  const [sort, setSort] = useState({ key: 'submitted_at', direction: 'descending' });

  useEffect(() => {
    const controller = new AbortController();
    listAgents({ signal: controller.signal }).then((body) => setAgents(body.agents)).catch(() => {});
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    setMore([]);
    async function load() {
      try {
        const page = await listRuns({ agent: params.agent, status: params.status }, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setRuns(page.runs);
        setError('');
        // Refresh only while some listed run is still moving.
        if (page.runs.some((run) => !terminal.has(run.status))) timer = setTimeout(load, refreshMs);
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(failure.message);
          timer = setTimeout(load, refreshMs);
        }
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [params.agent, params.status]);

  async function loadMore() {
    const all = [...(runs ?? []), ...more];
    const last = all.at(-1);
    if (!last) return;
    try {
      const page = await listRuns({ agent: params.agent, status: params.status, before: last.submitted_at });
      setMore((previous) => [...previous, ...page.runs.filter((run) => !all.some((seen) => seen.id === run.id))]);
    } catch (failure) {
      setError(failure.message);
    }
  }

  const filter = (change) => navigate(href({ screen: 'runs', params: { ...params, ...change } }));
  const rows = [...(runs ?? []), ...more];
  const sorted = sortRuns(rows, sort);
  const pages = usePages(sorted, { resetKey: `${params.agent}|${params.status}|${sort.key}|${sort.direction}` });

  return <>
    <PageHeader eyebrow="Runs" title="Runs" description="Your runs across every agent, newest first."
      actions={<a className="button primary" href={href({ screen: 'new-run', params: { agent: params.agent } })}>New run</a>} />
    <div className="toolbar" role="group" aria-label="Filter runs">
      <label className="visually-hidden" htmlFor="filter-agent">Agent</label>
      <select id="filter-agent" value={params.agent ?? ''} onChange={(event) => filter({ agent: event.target.value || undefined })}>
        <option value="">All agents</option>
        {agents.map((agent) => <option key={agent.name} value={agent.name}>{agent.name}</option>)}
      </select>
      <label className="visually-hidden" htmlFor="filter-status">State</label>
      <select id="filter-status" value={params.status ?? ''} onChange={(event) => filter({ status: event.target.value || undefined })}>
        <option value="">All states</option>
        {statuses.map((status) => <option key={status} value={status}>{statusLabel(status)}</option>)}
      </select>
      <form className="row lookup-form" onSubmit={(event) => { event.preventDefault(); if (isRunId(lookup)) navigate(href({ screen: 'run', id: lookup })); }}>
        <label className="visually-hidden" htmlFor="run-lookup">Open a run by ID</label>
        <input id="run-lookup" className="mono" placeholder="Open run by ID" maxLength={36} value={lookup}
          onChange={(event) => setLookup(event.target.value.trim())} />
        <button type="submit" disabled={!isRunId(lookup)}>Open</button>
      </form>
      {runs !== null && <span className="muted runs-count">{rows.length === 1 ? '1 run' : `${rows.length} runs`}</span>}
    </div>
    {error && <ErrorState>{error}</ErrorState>}
    {runs === null && !error && <LoadingState>Loading runs…</LoadingState>}
    {runs !== null && <Panel title="Recent runs" id="runs-table" className="runs-panel">
      <div className="panel-body flush">
        <DataTable caption="Runs" captionHidden columns={columns} rows={pages.visible} rowKey={(run) => run.id}
          sort={sort} onSort={setSort}
          empty={params.agent || params.status ? 'No runs match these filters.' : 'No runs yet. Start one with New run.'} />
        <Pager label="Runs" window={pages.window} onPage={pages.setPage} />
      </div>
      {rows.length >= 50 && <div className="table-footer row">
        <button type="button" onClick={loadMore}>Load older runs</button>
      </div>}
    </Panel>}
  </>;
}
