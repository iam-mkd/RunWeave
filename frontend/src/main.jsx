import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, approvalBody, clearRecent, isRunId, providerNames, readRecent, rememberRun, terminal } from './api.js';
import './style.css';

const demo = {
  service: 'payments',
  description: 'Payment authorization latency rose after the last deployment. Check service health and the triage runbook.',
};

const browserStorage = {
  getItem: (key) => window.localStorage.getItem(key),
  setItem: (key, value) => window.localStorage.setItem(key, value),
};

function selectedFromUrl() {
  const id = new URLSearchParams(location.search).get('run');
  return isRunId(id) ? id : '';
}

function statusLabel(status) {
  return status.toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function dateLabel(value) {
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

function useRun(id, revision) {
  const [view, setView] = useState({});
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    const { signal } = controller;
    const path = `/v1/runs/${id}`;
    let timer;
    async function poll() {
      let snapshot;
      try {
        snapshot = await api(path, { signal });
        let approval;
        let approvalError;
        if (snapshot.status === 'WAITING_APPROVAL') {
          try {
            approval = await api(`${path}/approval`, { signal });
          } catch (error) {
            approvalError = error.message;
          }
        }
        if (!signal.aborted) setView({ id, snapshot, approval, approvalError });
      } catch (error) {
        if (!signal.aborted) {
          setView((previous) => ({
            ...(previous.id === id && error.status !== 404 ? previous : {}),
            id, error: error.status === 404 ? 'This run expired or is unavailable.' : error.message,
          }));
        }
        if ([401, 403, 404].includes(error.status)) return;
      }
      if (!signal.aborted && !terminal.has(snapshot?.status)) {
        timer = setTimeout(poll, snapshot ? 900 : 2500);
      }
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, revision]);
  return view.id === id ? view : {};
}

function RunDetail({ id, view, onRefresh, onOpen }) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [lookup, setLookup] = useState('');
  const { snapshot, approval } = view;
  async function act(suffix, data) {
    setBusy(true);
    setActionError('');
    try {
      await api(`/v1/runs/${id}/${suffix}`, { data });
    } catch (error) {
      setActionError(error.message);
    } finally {
      onRefresh();
      setBusy(false);
    }
  }

  return <section className="panel detail" aria-labelledby="detail-title">
    <div className="panel-heading">
      <h2 id="detail-title">Run details</h2>
      {!id && <span className="status">No run selected</span>}
      {snapshot && <span role="status" className={`status ${snapshot.status.toLowerCase()}`}>
        {statusLabel(snapshot.status)}
      </span>}
    </div>
    {!id && <div className="empty">
      <div className="empty-mark" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M4 6h7l3 6h6M4 18h7l3-6M4 12h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/><circle cx="3" cy="6" r="1.5" fill="currentColor"/><circle cx="3" cy="18" r="1.5" fill="currentColor"/><circle cx="21" cy="12" r="1.5" fill="currentColor"/></svg></div>
      <h3>No investigation selected.</h3>
      <p>Start a run to investigate a service. Its findings and supporting evidence will appear here.</p>
      <div className="workflow-preview" aria-label="Investigation workflow">
        <div><span>01</span> Check current service health</div>
        <div><span>02</span> Consult the triage runbook</div>
        <div><span>03</span> Review an evidence-backed result</div>
      </div>
      <div className="lookup">
        <label htmlFor="run-lookup">Already have a run ID?</label>
        <div className="lookup-controls">
          <input id="run-lookup" value={lookup} placeholder="Paste a run ID" maxLength={36} onChange={(event) => setLookup(event.target.value.trim())} onKeyDown={(event) => { if (event.key === 'Enter' && isRunId(lookup)) onOpen(lookup); }} />
          <button disabled={!isRunId(lookup)} onClick={() => onOpen(lookup)}>Open</button>
        </div>
      </div>
    </div>}
    {id && <code className="run-id">{id}</code>}
    {id && !snapshot && !view.error && <p role="status" className="loading-state">Loading run…</p>}
    {view.error && <p role="alert" className="error">{view.error}</p>}
    {actionError && <p role="alert" className="error">{actionError}</p>}
    {snapshot && <>
      <dl className="timing">
        <div><dt>Submitted</dt><dd>{dateLabel(snapshot.submitted_at)}</dd></div>
        {snapshot.finished_at && <div><dt>Finished</dt><dd>{dateLabel(snapshot.finished_at)}</dd></div>}
      </dl>
      {snapshot.result && <div className="findings">
        <div className="section-heading"><h3>Findings</h3>
          <span className={`severity ${snapshot.result.severity}`}>{statusLabel(snapshot.result.severity)} severity</span>
        </div>
        <p>{snapshot.result.summary}</p>
        <h3 className="small-title">Evidence</h3>
        <ul className="evidence">{snapshot.result.evidence_ids.map((evidence) =>
          <li key={evidence}><code>{evidence}</code></li>)}</ul>
      </div>}
      {snapshot.error && <p role="alert" className="error">
        {snapshot.error.message} <code>{snapshot.error.code}</code>
      </p>}
      {snapshot.status === 'WAITING_APPROVAL' && <div className="findings">
        <h3>Sandbox ticket approval</h3>
        {view.approvalError && <p role="alert" className="error">{view.approvalError}</p>}
        {approval && <>
          <pre>{approval.call.arguments}</pre>
          <p className="muted">Expires {dateLabel(approval.expires_at)}</p>
          {approval.status === 'pending' && <div className="actions">
            <button className="primary" disabled={busy}
              onClick={() => act('approval', approvalBody(approval, 'approve'))}>Approve ticket</button>
            <button disabled={busy}
              onClick={() => act('approval', approvalBody(approval, 'reject'))}>Reject</button>
          </div>}
        </>}
      </div>}
      {snapshot.usage && <dl className="usage">
        <div><dt>Model requests</dt><dd>{snapshot.usage.requests}</dd></div>
        <div><dt>{snapshot.usage.uncertain_requests ? 'Token estimate (in / out)' : 'Tokens (in / out)'}</dt><dd>{snapshot.usage.input_tokens} / {snapshot.usage.output_tokens}</dd></div>
        <div><dt>Estimated cost</dt><dd>${Number(snapshot.usage.estimated_cost_usd).toFixed(4)}</dd></div>
      </dl>}
      {!terminal.has(snapshot.status) && <button disabled={busy} onClick={() => act('cancel', {})}>
        Cancel run
      </button>}
    </>}
  </section>;
}

function App() {
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState('');
  const [service, setService] = useState('');
  const [description, setDescription] = useState('');
  const [ticket, setTicket] = useState(false);
  const [recall, setRecall] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [id, setId] = useState(selectedFromUrl);
  const [recent, setRecent] = useState(() => readRecent(browserStorage));
  const [revision, setRevision] = useState(0);
  const view = useRun(id, revision);

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function connect() {
      let connected = false;
      try {
        const [workspace] = await Promise.all([
          api('/v1/workspace', { signal: controller.signal }),
          api('/health/ready', { signal: controller.signal }),
        ]);
        if (!controller.signal.aborted) {
          setConfig(workspace);
          setConfigError('');
          connected = true;
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setConfig(null);
          setConfigError(error.message);
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(connect, connected ? 15000 : 3000);
      }
    }
    connect();
    const onPop = () => setId(selectedFromUrl());
    window.addEventListener('popstate', onPop);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener('popstate', onPop);
    };
  }, []);

  function openRun(runId) {
    if (runId === id) return;
    const url = new URL(location.href);
    url.searchParams.set('run', runId);
    history.pushState(null, '', url);
    setId(runId);
  }

  async function submit(event) {
    event.preventDefault();
    if (!config || submitting || !description.trim()) return;
    setSubmitting(true);
    setSubmitError('');
    try {
      const snapshot = await api('/v1/runs', { data: {
        service: service.trim(), description: description.trim(),
        create_ticket: config.durable && ticket,
        ...(config.durable && recall ? { memory: {} } : {}),
      } });
      setRecent(rememberRun(browserStorage, snapshot.id));
      openRun(snapshot.id);
    } catch (error) {
      setSubmitError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  return <>
    <a className="skip-link" href="#workspace-main">Skip to workspace</a>
    <header className="app-header">
      <div className="shell header-inner">
        <a href="/" className="brand"><span className="mark" aria-hidden="true">R</span>RunWeave</a>
        <span className="header-nav">Workspace</span>
        <div className="connection" role="status"><span aria-hidden="true" className={config ? 'dot connected' : 'dot'} />
          {config ? providerNames[config.provider] : configError ? 'API unavailable' : 'Connecting'}
        </div>
      </div>
    </header>
    <main className="shell" id="workspace-main">
      <div className="page-heading">
        <p className="eyebrow">Local workspace / Incident triage</p>
        <h1>Run an investigation.</h1>
        <p>Check service health and review the supporting evidence.</p>
      </div>
      {configError && <p role="alert" className="error">{configError} <button onClick={() => location.reload()}>Reconnect</button></p>}
      <div className="workspace">
        <div className="sidebar">
          <section className="panel" aria-labelledby="new-title">
            <div className="panel-heading">
              <h2 id="new-title">New run</h2>
              <button type="button" className="text-button" onClick={() => {
                setService(demo.service); setDescription(demo.description);
              }}>Load example</button>
            </div>
            <form onSubmit={submit}>
              <label htmlFor="service">Service</label>
              <input id="service" value={service} placeholder="payments" required
                pattern="[a-z](?:[a-z0-9]|-){0,62}" maxLength={63}
                title="Start with a lowercase letter. Use lowercase letters, digits, and hyphens."
                onChange={(event) => setService(event.target.value)} />
              <div className="section-heading"><label htmlFor="description">Incident description</label>
                <span className="counter">{description.length} / 2000</span>
              </div>
              <textarea id="description" value={description} maxLength={2000} required
                placeholder="What changed? Include symptoms the agent should investigate."
                onChange={(event) => setDescription(event.target.value)} />
              {config?.durable && <div className="options">
                <label><input type="checkbox" checked={recall} onChange={(event) => setRecall(event.target.checked)} /> Recall saved evidence</label>
                <label><input type="checkbox" checked={ticket} onChange={(event) => setTicket(event.target.checked)} /> Propose a sandbox ticket</label>
              </div>}
              {submitError && <p role="alert" className="error">{submitError}</p>}
              <button className="primary submit" disabled={!config || !description.trim() || submitting}>
                {submitting ? 'Starting…' : <>Start run <span aria-hidden="true">→</span></>}
              </button>
              <p className="form-note">{config?.provider === 'fake'
                ? 'Uses synthetic evidence and offline inference.'
                : config ? 'Sends this incident and synthetic evidence to the configured provider.' : 'Waiting for the local API.'}</p>
            </form>
          </section>
          <section className="panel recent" aria-labelledby="recent-title">
            <div className="panel-heading"><h2 id="recent-title">Recent runs</h2>
              {recent.length > 0 && <button className="text-button" aria-label="Clear recent run IDs from this browser" onClick={() => setRecent(clearRecent(browserStorage))}>Clear</button>}
            </div>
            {recent.length ? <ul>{recent.map((value) => <li key={value}>
              <button aria-label={`Open run ${value}`} aria-current={value === id ? 'true' : undefined}
                title={value} onClick={() => openRun(value)}><span className="dot" aria-hidden="true"/><code>{value.slice(0, 8)}</code><span aria-hidden="true">↗</span></button>
            </li>)}</ul> : <p className="muted">Runs started in this browser will appear here.</p>}
          </section>
        </div>
        <RunDetail key={id} id={id} view={view} onOpen={openRun} onRefresh={() => setRevision((previous) => previous + 1)} />
      </div>
      <footer className="footer">
        {config && <span>{config.model} · {config.workflow} · {config.durable ? 'Persistent runs' : 'Session only — resets with server'}</span>}
        <a href="/docs" target="_blank" rel="noreferrer">API reference ↗</a>
      </footer>
    </main>
  </>;
}

createRoot(document.getElementById('root')).render(<App />);
