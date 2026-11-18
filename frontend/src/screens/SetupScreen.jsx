import React, { useEffect, useRef, useState } from 'react';
import { checkHttpAgent, listAgents, registerHttpAgent } from '../api.js';
import { ErrorState, PageHeader, Panel, StatusBadge } from '../components/index.js';
import { href } from '../router.js';
import { HttpAgentCheck } from './parts.jsx';

const targetPattern = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$/;
const pollMs = 2000;

const kinds = [
  { id: 'python', title: 'Python callable', text: 'An async function in your project, decorated with @runweave.sdk.agent.', available: true },
  { id: 'http', title: 'HTTP endpoint', text: 'A service in any language that speaks the runweave.agent/v1 contract, on this machine.', available: true },
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

function HttpSetup({ onBack }) {
  const [url, setUrl] = useState('');
  const [result, setResult] = useState(null);
  const [registered, setRegistered] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function act(work) {
    setBusy(true);
    setError('');
    try {
      await work();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  const check = (event) => {
    event.preventDefault();
    setResult(null);
    act(async () => setResult(await checkHttpAgent(url)));
  };
  const register = () => act(async () => setRegistered((await registerHttpAgent(result.url)).agents));
  const ready = result?.agents.some((agent) => !agent.conflict);

  if (registered) {
    return <Panel title="Smoke test" id="smoke">
      <div className="stack">
        <div className="row"><StatusBadge tone="good" label="Registered" />
          <span>{registered.length === 1 ? '1 agent is' : `${registered.length} agents are`} ready until this server stops.</span></div>
        <p className="muted">To register them every time RunWeave starts, use the startup command from the previous step.</p>
        <ul className="link-list">{registered.map((agent) => <li key={agent.name}>
          <a className="button" href={href({ screen: 'new-run', params: { agent: agent.name } })}>Run {agent.name}</a></li>)}</ul>
        <div className="row end"><a className="button" href={href({ screen: 'agents' })}>Done</a></div>
      </div>
    </Panel>;
  }
  return <Panel title="Check your HTTP agent" id="configure">
    <div className="stack">
      <p>Start your agent so it listens on this machine, for example:</p>
      <CopyCommand id="serve-agent-command" command="runweave serve-agent mypkg.agent:run --port 9100" />
      <p className="muted">TypeScript: <code>serveAgents([agent], {'{'} port: 9100 {'}'})</code> from <code>@runweave/agent</code>.</p>
      <form className="stack narrow" onSubmit={check}>
        <div className="field">
          <label htmlFor="agent-url">Agent URL</label>
          <input id="agent-url" className="mono" placeholder="http://127.0.0.1:9100" value={url} maxLength={2048}
            inputMode="url" autoComplete="off" spellCheck="false" aria-describedby="agent-url-hint"
            onChange={(event) => { setUrl(event.target.value.trim()); setResult(null); }} />
          <span className="field-hint" id="agent-url-hint">Loopback addresses only (127.0.0.1, ::1 or localhost), unless the server allows other hosts.</span>
        </div>
        <div className="row end"><button type="button" onClick={onBack}>Back</button>
          <button disabled={!url || busy}>Test connection</button></div>
      </form>
      {error && <ErrorState>{error}</ErrorState>}
      {result && <>
        <HttpAgentCheck result={result} />
        <p className="muted">To register these agents every time RunWeave starts instead:</p>
        <CopyCommand id="http-serve-command" command={`runweave serve --http-agent ${result.url}`} />
        <div className="row end"><button type="button" className="primary" disabled={!ready || busy} onClick={register}>Register agents</button></div>
      </>}
    </div>
  </Panel>;
}

export function SetupScreen() {
  const [kind, setKind] = useState('python');
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
      description="Python agents load when RunWeave starts, never from the browser. HTTP agents are only called, never imported. Everything runs offline with the mock model unless you add an API key." />
    <ol className="stepper" aria-label="Setup progress">
      {['Connection', kind === 'http' ? 'Check the endpoint' : 'Start RunWeave', 'Smoke test'].map((label, index) => <li key={label}
        aria-current={step === index + 1 ? 'step' : undefined} data-done={step > index + 1 ? 'true' : undefined}>
        <span className="step-number">{index + 1}</span>{label}</li>)}
    </ol>
    {step === 1 && <Panel title="How does RunWeave reach your agent?" id="kind">
      <fieldset className="choice-grid">
        <legend className="visually-hidden">Connection type</legend>
        {kinds.map((option) => <label key={option.id} className="choice" data-disabled={option.available ? undefined : 'true'}>
          <input type="radio" name="kind" value={option.id} checked={kind === option.id} disabled={!option.available}
            onChange={() => setKind(option.id)} />
          <span className="choice-title">{option.title}</span>
          <span className="choice-text">{option.text}</span>
        </label>)}
      </fieldset>
      <div className="row end"><button type="button" className="primary" onClick={() => setStep(2)}>Continue</button></div>
    </Panel>}
    {step === 2 && kind === 'http' && <HttpSetup onBack={() => setStep(1)} />}
    {step === 2 && kind === 'python' && <Panel title="Start RunWeave with your agent" id="configure">
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
    {step === 3 && kind === 'python' && <Panel title="Smoke test" id="smoke">
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
