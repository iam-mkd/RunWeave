import React, { useEffect, useState } from 'react';
import { listAgents, listRuns, removeAgent } from '../api.js';
import { DataTable, ErrorState, LoadingState, PageHeader, Panel, StatTile } from '../components/index.js';
import { percent } from '../format.js';
import { href } from '../router.js';

const connectionLabels = { 'built-in': 'Built-in example', python: 'Python callable', http: 'HTTP endpoint' };

function stats(runs) {
  const byAgent = {};
  for (const run of runs) {
    const entry = byAgent[run.agent] ?? { total: 0, finished: 0, succeeded: 0, last: null };
    entry.total += 1;
    if (run.finished_at) entry.finished += 1;
    if (run.status === 'SUCCEEDED') entry.succeeded += 1;
    if (!entry.last || run.submitted_at > entry.last) entry.last = run.submitted_at;
    byAgent[run.agent] = entry;
  }
  return byAgent;
}

function relative(value) {
  if (!value) return 'Never';
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(value)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// Runtime-registered HTTP agents can be removed; a second click confirms.
function AgentActions({ agent, onRemoved }) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState('');
  async function remove() {
    try {
      await removeAgent(agent.name);
      onRemoved(agent.name);
    } catch (failure) {
      setError(failure.message);
      setConfirming(false);
    }
  }
  return <div className="row">
    <a className="button" href={href({ screen: 'new-run', params: { agent: agent.name } })} aria-label={`New run for ${agent.name}`}>New run</a>
    {agent.removable && (confirming
      ? <><button type="button" className="danger" onClick={remove}>Confirm remove</button>
        <button type="button" onClick={() => setConfirming(false)}>Keep</button></>
      : <button type="button" onClick={() => setConfirming(true)} aria-label={`Remove ${agent.name}`}>Remove</button>)}
    {error && <span role="alert" className="field-error">{error}</span>}
  </div>;
}

export function AgentsScreen() {
  const [agents, setAgents] = useState(null);
  const [recent, setRecent] = useState([]);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([listAgents({ signal: controller.signal }), listRuns({ limit: 100 }, { signal: controller.signal })])
      .then(([catalog, page]) => { setAgents(catalog.agents); setRecent(page.runs); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, []);

  if (error) return <><PageHeader eyebrow="Agents" title="Agents" /><ErrorState>{error}</ErrorState></>;
  if (!agents) return <><PageHeader eyebrow="Agents" title="Agents" /><LoadingState>Loading agents…</LoadingState></>;
  const byAgent = stats(recent);
  const finished = recent.filter((run) => run.finished_at);
  const succeeded = finished.filter((run) => run.status === 'SUCCEEDED').length;
  const custom = agents.filter((agent) => agent.kind !== 'built-in');

  const columns = [
    { key: 'name', label: 'Agent', render: (agent) => <div><a href={href({ screen: 'runs', params: { agent: agent.name } })}>{agent.name}</a>
      {agent.description && <div className="muted cell-note">{agent.description}</div>}</div> },
    { key: 'kind', label: 'Connection', render: (agent) => <div>{connectionLabels[agent.kind] ?? agent.kind}
      {agent.target && <div><code className="muted">{agent.target}</code></div>}</div> },
    { key: 'version', label: 'Version', render: (agent) => <span className="mono">{agent.version}</span> },
    { key: 'runs', label: 'Recent runs', numeric: true, render: (agent) => byAgent[agent.name]?.total ?? 0 },
    { key: 'success', label: 'Success', numeric: true, render: (agent) => {
      const entry = byAgent[agent.name];
      return entry?.finished ? percent(entry.succeeded / entry.finished) : '—';
    } },
    { key: 'last', label: 'Last run', render: (agent) => relative(byAgent[agent.name]?.last) },
    { key: 'action', label: 'Action', render: (agent) => <AgentActions agent={agent}
      onRemoved={(name) => setAgents((current) => current.filter((item) => item.name !== name))} /> },
  ];

  return <>
    <PageHeader eyebrow="Agents" title="Agents" description="Agents registered with this RunWeave server."
      actions={<a className="button primary" href={href({ screen: 'setup' })}>Connect agent</a>} />
    <div className="grid four">
      <StatTile label="Registered agents" value={String(agents.length)} />
      <StatTile label="Recent runs" value={String(recent.length)} />
      <StatTile label="Success rate" value={finished.length ? percent(succeeded / finished.length) : '—'} />
      <StatTile label="Active now" value={String(recent.filter((run) => !run.finished_at).length)} />
    </div>
    {!custom.length && <div className="spaced-top callout">
      <p><strong>Only the built-in example agent is registered.</strong> Connect your own agent, in Python or over HTTP, to test it here.</p>
      <a className="button" href={href({ screen: 'setup' })}>Connect your agent</a>
    </div>}
    <Panel title="All agents" id="agents" className="spaced-top">
      <div className="panel-body flush">
        <DataTable caption="Registered agents" captionHidden columns={columns} rows={agents} rowKey={(agent) => agent.name} />
      </div>
    </Panel>
    <p className="muted spaced-top">Statistics cover your 100 most recent runs.</p>
  </>;
}
