import React, { useEffect, useState } from 'react';
import { getSettings, removeProviderKey, saveProviderKey, updateSettings } from '../api.js';
import { ErrorState, KeyValueList, LoadingState, PageHeader, Panel, StatusBadge } from '../components/index.js';
import { CopyBlock } from './parts.jsx';

const providerNames = { openai: 'OpenAI', anthropic: 'Anthropic' };
const keySources = {
  credential_store: ['good', 'Saved in the credential store'],
  environment: ['info', 'Set by environment variable'],
  none: ['neutral', 'Not set'],
};
const budgetFields = [
  ['agent_request_limit', 'Model calls per run', 'number'],
  ['agent_input_tokens', 'Input tokens per run', 'number'],
  ['agent_completion_tokens', 'Output tokens per run', 'number'],
  ['agent_max_cost_usd', 'Spend cap per run (USD)', 'decimal'],
  ['model_input_usd_per_million', 'Input price per million tokens (USD)', 'decimal'],
  ['model_output_usd_per_million', 'Output price per million tokens (USD)', 'decimal'],
  ['model_price_version', 'Price list version', 'text'],
];

function shellValue(value) {
  const text = String(value);
  return /^[\w.:+@/-]+$/.test(text) ? text : `'${text.replaceAll("'", "'\\''")}'`;
}

// Environment lines that make the current settings permanent. Keys are never included.
export function environmentLines(view) {
  const lines = [`RUNWEAVE_PROXY_MODE=${view.proxy.mode}`];
  if (view.proxy.models.length) lines.push(`RUNWEAVE_PROXY_MODELS='${JSON.stringify(view.proxy.models)}'`);
  const budgets = view.budgets;
  lines.push(
    `RUNWEAVE_AGENT_REQUEST_LIMIT=${budgets.agent_request_limit}`,
    `RUNWEAVE_AGENT_INPUT_TOKENS=${budgets.agent_input_tokens}`,
    `RUNWEAVE_AGENT_COMPLETION_TOKENS=${budgets.agent_completion_tokens}`,
    `RUNWEAVE_AGENT_MAX_COST_USD=${budgets.agent_max_cost_usd}`,
  );
  if (budgets.model_input_usd_per_million !== null) lines.push(`RUNWEAVE_MODEL_INPUT_USD_PER_MILLION=${budgets.model_input_usd_per_million}`);
  if (budgets.model_output_usd_per_million !== null) lines.push(`RUNWEAVE_MODEL_OUTPUT_USD_PER_MILLION=${budgets.model_output_usd_per_million}`);
  if (budgets.model_price_version) lines.push(`RUNWEAVE_MODEL_PRICE_VERSION=${shellValue(budgets.model_price_version)}`);
  if (view.faults.profile) {
    lines.push(`RUNWEAVE_PROXY_FAULT_PROFILE=${shellValue(view.faults.profile)}`, `RUNWEAVE_PROXY_FAULT_SEED=${view.faults.seed}`);
  }
  return lines.join('\n');
}

function problem(error) {
  const details = error.detail?.errors?.map((item) => item.msg).filter(Boolean) ?? [];
  return details.length ? `${error.message}: ${details.join('; ')}` : error.message;
}

function useSaver(onSaved) {
  const [state, setState] = useState({ busy: false, error: '', done: '' });
  async function run(work, done) {
    setState({ busy: true, error: '', done: '' });
    try {
      onSaved(await work());
      setState({ busy: false, error: '', done });
    } catch (error) {
      setState({ busy: false, error: problem(error), done: '' });
    }
  }
  return [state, run];
}

function Outcome({ state }) {
  return <>
    {state.error && <ErrorState>{state.error}</ErrorState>}
    <span className="muted" role="status">{state.done}</span>
  </>;
}

export function ProviderKeyCard({ provider, status, storeAvailable, onChanged }) {
  const name = providerNames[provider];
  const [key, setKey] = useState('');
  const [state, run] = useSaver(() => {});
  const [tone, label] = keySources[status.key] ?? keySources.none;
  const id = `${provider}-key`;
  const variable = `RUNWEAVE_PROXY_${provider.toUpperCase()}_API_KEY`;
  async function save(event) {
    event.preventDefault();
    const typed = key;
    setKey('');
    await run(async () => { await saveProviderKey(provider, typed); await onChanged(); }, `${name} key saved`);
  }
  return <Panel title={name} id={id}>
    <div className="stack">
      <div className="row"><StatusBadge tone={tone} label={label} /></div>
      <KeyValueList items={[
        { label: 'Base URL', value: <code>{status.base_url}</code> },
        { label: 'Limits', value: `${status.concurrency} at once, ${status.requests_per_minute} per minute` },
      ]} />
      {status.key === 'environment' && <p className="muted"><code>{variable}</code> sets this key. Change it there.</p>}
      {status.key !== 'environment' && !storeAvailable && <p className="muted">
        No OS credential store is available here. Set <code>{variable}</code> before starting RunWeave instead.</p>}
      {status.key !== 'environment' && storeAvailable && <form className="stack narrow" onSubmit={save}>
        <div className="field">
          <label htmlFor={`${id}-input`}>{name} API key</label>
          <input id={`${id}-input`} type="password" className="mono" value={key} autoComplete="off" spellCheck="false"
            maxLength={512} aria-describedby={`${id}-hint`} onChange={(event) => setKey(event.target.value.trim())} />
          <span className="field-hint" id={`${id}-hint`}>Stored in your OS credential store and never shown again. Agents only ever see their run token.</span>
        </div>
        <div className="row end">
          {status.key === 'credential_store' && <button type="button" disabled={state.busy}
            onClick={() => run(async () => { await removeProviderKey(provider); await onChanged(); }, `${name} key removed`)}>Remove key</button>}
          <button className="primary" disabled={!key || state.busy}>Save key</button>
        </div>
      </form>}
      <Outcome state={state} />
    </div>
  </Panel>;
}

function ModelAccess({ view, onSaved }) {
  const [mode, setMode] = useState(view.proxy.mode);
  const [models, setModels] = useState(view.proxy.models.join('\n'));
  const [state, run] = useSaver(onSaved);
  const list = models.split('\n').map((line) => line.trim()).filter(Boolean);
  return <Panel title="Model access" id="model-access">
    <form className="stack" onSubmit={(event) => { event.preventDefault(); run(() => updateSettings({ proxy: { mode, models: list } }), 'Model access saved'); }}>
      <fieldset className="choice-grid two">
        <legend className="visually-hidden">Proxy mode</legend>
        {[['mock', 'Mock', 'Deterministic replies, offline and free.'],
          ['passthrough', 'Passthrough', 'Forward to your providers with your keys. Needs allowed models and prices.']]
          .map(([value, title, text]) => <label key={value} className="choice">
            <input type="radio" name="proxy-mode" value={value} checked={mode === value} onChange={() => setMode(value)} />
            <span className="choice-title">{title}</span><span className="choice-text">{text}</span>
          </label>)}
      </fieldset>
      <div className="field narrow">
        <label htmlFor="proxy-models">Allowed models</label>
        <textarea id="proxy-models" className="mono" rows={3} value={models} aria-describedby="proxy-models-hint"
          onChange={(event) => setModels(event.target.value)} />
        <span className="field-hint" id="proxy-models-hint">One per line. Passthrough forwards only these; the prices below apply to all of them.</span>
      </div>
      <div className="row end"><Outcome state={state} /><button className="primary" disabled={state.busy}>Save model access</button></div>
    </form>
  </Panel>;
}

function Budgets({ view, onSaved }) {
  const [values, setValues] = useState(() => Object.fromEntries(budgetFields.map(([name]) => [name, view.budgets[name] ?? ''])));
  const [state, run] = useSaver(onSaved);
  function patch() {
    return Object.fromEntries(budgetFields.map(([name, , kind]) => {
      const raw = String(values[name]).trim();
      if (kind === 'number') return [name, raw === '' ? null : Number(raw)];
      if (kind === 'decimal') return [name, raw === '' ? null : raw];
      return [name, raw];
    }).filter(([name, value]) => !(value === null && name === 'agent_max_cost_usd')));
  }
  return <Panel title="Budgets per run" id="budgets">
    <form className="stack" onSubmit={(event) => { event.preventDefault(); run(() => updateSettings({ budgets: patch() }), 'Budgets saved'); }}>
      <p className="muted">Applied to runs started after you save. Calls are reserved against these limits before they are answered.</p>
      <div className="form-grid">
        {budgetFields.map(([name, label, kind]) => <div className="field" key={name}>
          <label htmlFor={`budget-${name}`}>{label}</label>
          <input id={`budget-${name}`} className={kind === 'text' ? undefined : 'mono'} value={values[name]}
            inputMode={kind === 'number' ? 'numeric' : kind === 'decimal' ? 'decimal' : undefined}
            onChange={(event) => setValues({ ...values, [name]: event.target.value })} />
        </div>)}
      </div>
      <div className="row end"><Outcome state={state} /><button className="primary" disabled={state.busy}>Save budgets</button></div>
    </form>
  </Panel>;
}

function Faults({ view, onSaved }) {
  const [profile, setProfile] = useState(view.faults.profile);
  const [seed, setSeed] = useState(String(view.faults.seed));
  const [state, run] = useSaver(onSaved);
  const chosen = view.faults.profiles.find((item) => item.name === profile);
  return <Panel title="Fault injection" id="faults">
    <form className="stack" onSubmit={(event) => {
      event.preventDefault();
      run(() => updateSettings({ faults: { profile, seed: Number(seed) } }), 'Fault settings saved');
    }}>
      <p className="muted">The same seed and the same calls reproduce the same faults. Applies to proxied calls immediately.</p>
      <div className="form-grid">
        <div className="field">
          <label htmlFor="fault-profile">Fault profile</label>
          <select id="fault-profile" value={profile} onChange={(event) => setProfile(event.target.value)}>
            <option value="">None</option>
            {view.faults.profiles.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
            {profile === 'custom' && <option value="custom">custom</option>}
          </select>
        </div>
        <div className="field">
          <label htmlFor="fault-seed">Seed</label>
          <input id="fault-seed" className="mono" inputMode="numeric" value={seed} onChange={(event) => setSeed(event.target.value.trim())} />
        </div>
      </div>
      {chosen && <div className="stack">
        {chosen.description && <p>{chosen.description}</p>}
        <ul className="fault-list">{chosen.faults.map((fault, index) => <li key={index}>
          <code>{fault.kind}</code> <span className="muted">{Math.round(fault.probability * 1000) / 10}% of calls</span></li>)}</ul>
      </div>}
      <div className="row end"><Outcome state={state} /><button className="primary" disabled={state.busy}>Save fault settings</button></div>
    </form>
  </Panel>;
}

export function SettingsScreen() {
  const [view, setView] = useState(null);
  const [error, setError] = useState('');
  async function load() {
    try {
      setView(await getSettings());
      setError('');
    } catch (failure) {
      setError(failure.message);
    }
  }
  useEffect(() => { load(); }, []);

  const header = <PageHeader eyebrow="Settings" title="Settings"
    description="Changes apply while this server runs. Keys go to your OS credential store; copy the lines at the end to keep everything else." />;
  if (error) return <>{header}<ErrorState>{error}</ErrorState></>;
  if (!view) return <>{header}<LoadingState>Loading settings…</LoadingState></>;
  return <>
    {header}
    <ModelAccess view={view} onSaved={setView} />
    <div className="grid two spaced-top">
      {Object.keys(providerNames).map((provider) => <ProviderKeyCard key={provider} provider={provider}
        status={view.proxy[provider]} storeAvailable={view.key_store.available} onChanged={load} />)}
    </div>
    <p className="muted spaced-top">Credential store: <code>{view.key_store.available ? view.key_store.backend : 'not available'}</code>.
      Proxy base URL for agents: <code>{view.proxy.public_url}/llm/v1</code>.</p>
    <div className="spaced-top"><Budgets view={view} onSaved={setView} /></div>
    <div className="spaced-top"><Faults view={view} onSaved={setView} /></div>
    <div className="spaced-top"><Panel title="Keep these settings" id="environment">
      <div className="stack">
        <p className="muted">Add these to your environment or <code>.env</code> to keep the current settings after a restart. Keys are not included.</p>
        <CopyBlock id="environment-lines" text={environmentLines(view)} />
      </div>
    </Panel></div>
  </>;
}
