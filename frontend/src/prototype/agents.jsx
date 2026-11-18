import React, { useState } from 'react';
import { DataTable, EmptyState, ErrorState, PageHeader, Panel, StatTile, StatusBadge, Timeline } from '../components/index.js';
import { compactNumber, duration, percent } from '../format.js';
import { agents } from './fixtures.js';

const agentColumns = [
  { key: 'name', label: 'Agent', sortable: true, render: (agent) => <a href={`#/runs?agent=${agent.id}`}>{agent.name}</a> },
  { key: 'connection', label: 'Connection', render: (agent) => <div><div>{agent.connection}</div><code className="muted">{agent.target}</code></div> },
  { key: 'health', label: 'Health', render: (agent) => <StatusBadge tone={agent.health.tone} label={agent.health.label} /> },
  { key: 'mode', label: 'Mode' },
  { key: 'runs', label: 'Runs', numeric: true, sortable: true, render: (agent) => compactNumber(agent.runs) },
  { key: 'successRate', label: 'Success', numeric: true, render: (agent) => agent.successRate === null ? '—' : percent(agent.successRate) },
  { key: 'p95', label: 'p95', numeric: true, render: (agent) => duration(agent.p95) },
  { key: 'lastRun', label: 'Last run' },
];

export function AgentsScreen() {
  const [sort, setSort] = useState({ key: 'runs', direction: 'descending' });
  const sorted = [...agents].sort((a, b) => (sort.key === 'name' ? a.name.localeCompare(b.name) : a[sort.key] - b[sort.key]) * (sort.direction === 'ascending' ? 1 : -1));
  return <>
    <PageHeader eyebrow="Agents" title="Agents" description="Agents connected to this workspace and how they are being tested."
      actions={<a className="button primary" href="#/setup">Connect agent</a>} />
    <div className="grid four">
      <StatTile label="Connected agents" value="4" />
      <StatTile label="Runs today" value="1,692" trend={[12, 18, 15, 30, 42, 38, 51, 47, 60, 72, 66, 81]} />
      <StatTile label="Success rate" value="98.4%" delta={{ direction: 'up', good: true, text: '0.6 pts vs yesterday' }} />
      <StatTile label="Spend today" value="$1.82" delta={{ direction: 'up', good: false, text: '$0.40 vs yesterday' }} />
    </div>
    <Panel title="All agents" id="agents" className="spaced-top">
      <div className="panel-body flush">
        <DataTable caption="Connected agents" captionHidden columns={agentColumns} rows={sorted} rowKey={(agent) => agent.id} sort={sort} onSort={setSort} />
      </div>
    </Panel>
    <div className="spaced-top">
      <ErrorState action={<a className="button" href="#/setup">Fix connection</a>}>
        <strong>research-agent</strong> could not be imported: <code>ModuleNotFoundError: research</code>. Check the selected virtual environment.
      </ErrorState>
    </div>
  </>;
}

const kinds = [
  { id: 'python', title: 'Python callable', text: 'RunWeave imports an async function from your project, for example mypkg.agent:run.' },
  { id: 'http', title: 'HTTP endpoint', text: 'RunWeave calls your agent server over HTTP. Works with any language or framework.' },
  { id: 'proxy', title: 'LLM proxy only', text: 'Point your agent’s model base URL at RunWeave. No code changes; faults and budgets apply to model calls.' },
];

export function SetupScreen() {
  const [step, setStep] = useState(1);
  const [kind, setKind] = useState('python');
  const [tested, setTested] = useState(false);
  return <>
    <PageHeader eyebrow="Agents / Connect" title="Connect an agent" description="Three steps. Everything runs offline with the mock model until you add an API key." />
    <ol className="stepper" aria-label="Setup progress">
      {['Connection', 'Configure', 'Smoke test'].map((label, index) => <li key={label} aria-current={step === index + 1 ? 'step' : undefined}
        data-done={step > index + 1 ? 'true' : undefined}><span className="step-number">{index + 1}</span>{label}</li>)}
    </ol>
    {step === 1 && <Panel title="How does RunWeave reach your agent?" id="kind">
      <fieldset className="choice-grid">
        <legend className="visually-hidden">Connection type</legend>
        {kinds.map((option) => <label key={option.id} className="choice">
          <input type="radio" name="kind" value={option.id} checked={kind === option.id} onChange={() => setKind(option.id)} />
          <span className="choice-title">{option.title}</span>
          <span className="choice-text">{option.text}</span>
        </label>)}
      </fieldset>
      <div className="row end"><button type="button" className="primary" onClick={() => setStep(2)}>Continue</button></div>
    </Panel>}
    {step === 2 && <Panel title="Configure" id="configure">
      <form className="stack narrow" onSubmit={(event) => { event.preventDefault(); setStep(3); }}>
        <div className="field"><label htmlFor="agent-name">Name</label><input id="agent-name" defaultValue="my-agent" /></div>
        {kind === 'python' && <>
          <div className="field"><label htmlFor="target">Callable</label><input id="target" className="mono" defaultValue="mypkg.agent:run" />
            <span className="field-hint">An <code>async def run(input, ctx)</code> function.</span></div>
          <div className="field"><label htmlFor="interpreter">Python environment</label>
            <select id="interpreter" defaultValue="venv"><option value="venv">.venv (Python 3.12.13)</option><option value="system">System Python 3.13.9</option></select></div>
        </>}
        {kind === 'http' && <div className="field"><label htmlFor="url">Endpoint URL</label><input id="url" className="mono" defaultValue="http://127.0.0.1:7801/agent" />
          <span className="field-hint">Loopback addresses only by default. Redirects are not followed.</span></div>}
        {kind === 'proxy' && <div className="field"><label htmlFor="base">Set your SDK base URL to</label>
          <pre>OPENAI_BASE_URL=http://127.0.0.1:8000/v1{'\n'}ANTHROPIC_BASE_URL=http://127.0.0.1:8000</pre>
          <span className="field-hint">Each run receives its own proxy token; your provider key never reaches the agent.</span></div>}
        <div className="row end"><button type="button" onClick={() => setStep(1)}>Back</button><button className="primary">Continue</button></div>
      </form>
    </Panel>}
    {step === 3 && <Panel title="Smoke test" id="smoke">
      {!tested ? <EmptyState title="Run one test case" icon="evals" action={<button type="button" className="primary" onClick={() => setTested(true)}>Run smoke test</button>}>
        <p>Sends a sample input through your agent with the mock model, a 30 s deadline, and no injected faults.</p>
      </EmptyState> : <div className="stack">
        <div className="row"><StatusBadge status="SUCCEEDED" /><span className="muted">Completed in 1.42 s · 3 steps · $0.0000</span></div>
        <Timeline steps={[
          { id: 'a', title: 'Input validated against schema', status: 'SUCCEEDED', duration: '2 ms' },
          { id: 'b', title: 'Model call through proxy (mock)', status: 'SUCCEEDED', duration: '310 ms' },
          { id: 'c', title: 'Output validated against schema', status: 'SUCCEEDED', duration: '1 ms' },
        ]} />
        <div className="row end"><a className="button" href="#/load-tests">Run a load test</a><a className="button primary" href="#/agents">Done</a></div>
      </div>}
    </Panel>}
  </>;
}
