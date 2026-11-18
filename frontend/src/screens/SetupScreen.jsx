import React, { useEffect, useRef, useState } from 'react';
import { listAgents } from '../api.js';
import { PageHeader, Panel, StatusBadge } from '../components/index.js';
import { href } from '../router.js';

const targetPattern = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$/;
const pollMs = 2000;

const kinds = [
  { id: 'python', title: 'Python callable', text: 'An async function in your project, decorated with @runweave.sdk.agent.', available: true },
  { id: 'http', title: 'HTTP endpoint', text: 'Any language, called over HTTP. Arrives in milestone 20.', available: false },
  { id: 'proxy', title: 'LLM proxy only', text: 'Point your SDK’s base URL at RunWeave. Arrives in milestone 21.', available: false },
];

function CopyCommand({ id, command }) {
  const [copied, setCopied] = useState('');
  const ref = useRef(null);
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied('Copied');
    } catch {
      const range = document.createRange();
      range.selectNodeContents(ref.current);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      setCopied('Selected — press Ctrl+C to copy');
    }
  }
  return <div className="command">
    <pre id={id} ref={ref}>{command}</pre>
    <div className="row"><button type="button" onClick={copy} aria-describedby={id}>Copy</button>
      <span className="muted" role="status">{copied}</span></div>
  </div>;
}

export function SetupScreen() {
  const [step, setStep] = useState(1);
  const [target, setTarget] = useState('');
  const [found, setFound] = useState(null);
  const [checking, setChecking] = useState('');
  const valid = targetPattern.test(target);

  useEffect(() => {
    if (step !== 3 || found) return undefined;
    const controller = new AbortController();
    let timer;
    async function check() {
      try {
        const { agents } = await listAgents({ signal: controller.signal });
        const match = agents.find((agent) => agent.target === target);
        if (match) {
          setFound(match);
          return;
        }
        setChecking(`Not registered yet. Checking every ${pollMs / 1000} seconds…`);
      } catch {
        if (!controller.signal.aborted) setChecking('Waiting for RunWeave to come back after the restart…');
      }
      if (!controller.signal.aborted) timer = setTimeout(check, pollMs);
    }
    check();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [step, target, found]);

  return <>
    <PageHeader eyebrow={<><a href={href({ screen: 'agents' })}>Agents</a> / Connect</>} title="Connect an agent"
      description="Agents load when RunWeave starts, never from the browser. Everything runs offline with the mock model unless you add an API key." />
    <ol className="stepper" aria-label="Setup progress">
      {['Connection', 'Start RunWeave', 'Smoke test'].map((label, index) => <li key={label}
        aria-current={step === index + 1 ? 'step' : undefined} data-done={step > index + 1 ? 'true' : undefined}>
        <span className="step-number">{index + 1}</span>{label}</li>)}
    </ol>
    {step === 1 && <Panel title="How does RunWeave reach your agent?" id="kind">
      <fieldset className="choice-grid">
        <legend className="visually-hidden">Connection type</legend>
        {kinds.map((kind) => <label key={kind.id} className="choice" data-disabled={kind.available ? undefined : 'true'}>
          <input type="radio" name="kind" value={kind.id} defaultChecked={kind.id === 'python'} disabled={!kind.available} />
          <span className="choice-title">{kind.title}</span>
          <span className="choice-text">{kind.text}</span>
        </label>)}
      </fieldset>
      <div className="row end"><button type="button" className="primary" onClick={() => setStep(2)}>Continue</button></div>
    </Panel>}
    {step === 2 && <Panel title="Start RunWeave with your agent" id="configure">
      <form className="stack narrow" onSubmit={(event) => { event.preventDefault(); if (valid) setStep(3); }}>
        <div className="field">
          <label htmlFor="agent-target">Agent function (module:attribute)</label>
          <input id="agent-target" className="mono" placeholder="mypkg.agent:run" value={target} maxLength={200}
            aria-invalid={target && !valid ? 'true' : undefined} aria-describedby="agent-target-hint"
            onChange={(event) => setTarget(event.target.value.trim())} />
          <span className="field-hint" id="agent-target-hint">The module must be importable where RunWeave runs, for example installed with <code>pip install -e .</code></span>
        </div>
        {valid && <>
          <p>Stop this server, then start it again with your agent:</p>
          <CopyCommand id="serve-command" command={`runweave serve --agent ${target}`} />
          <p className="muted">To check the import first without starting the server:</p>
          <CopyCommand id="check-command" command={`runweave serve --check --agent ${target}`} />
        </>}
        <div className="row end"><button type="button" onClick={() => setStep(1)}>Back</button>
          <button className="primary" disabled={!valid}>I restarted RunWeave</button></div>
      </form>
    </Panel>}
    {step === 3 && <Panel title="Smoke test" id="smoke">
      {found ? <div className="stack">
        <div className="row"><StatusBadge tone="good" label="Registered" /><span><strong>{found.name}</strong> {found.version} is ready.</span></div>
        <p className="muted">Run one input through it with the mock model and the default 30 second deadline.</p>
        <div className="row end"><a className="button" href={href({ screen: 'agents' })}>Done</a>
          <a className="button primary" href={href({ screen: 'new-run', params: { agent: found.name } })}>Run a smoke test</a></div>
      </div> : <div className="stack">
        <div className="row"><StatusBadge tone="info" label="Waiting" live /><span>Looking for <code>{target}</code>.</span></div>
        <p className="muted">{checking || 'Checking…'}</p>
        <div className="row end"><button type="button" onClick={() => setStep(2)}>Back</button></div>
      </div>}
    </Panel>}
  </>;
}
