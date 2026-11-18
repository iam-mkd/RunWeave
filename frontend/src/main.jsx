import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, approvalBody, clearRecent, isRunId, providerNames, readRecent, rememberRun, terminal } from './api.js';
import { browserStorage } from './browserStorage.js';
import {
  AppShell, EmptyState, ErrorState, Icon, KeyValueList, LoadingState, PageHeader, Panel, StatusBadge,
} from './components/index.js';
import { useThemePreference } from './design/useThemePreference.js';
import { usd } from './format.js';
import { productionSections } from './navigation.js';
import './design/tokens.css';
import './design/base.css';
import './components/components.css';
import './workspace.css';

const demo = {
  service: 'payments',
  description: 'Payment authorization latency rose after the last deployment. Check service health and the triage runbook.',
};

function selectedFromUrl() {
  const id = new URLSearchParams(location.search).get('run');
  return isRunId(id) ? id : '';
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

function RunLookup({ onOpen }) {
  const [lookup, setLookup] = useState('');
  return <div className="lookup">
    <label htmlFor="run-lookup">Already have a run ID?</label>
    <div className="row">
      <input id="run-lookup" className="mono" value={lookup} placeholder="Paste a run ID" maxLength={36}
        onChange={(event) => setLookup(event.target.value.trim())}
        onKeyDown={(event) => { if (event.key === 'Enter' && isRunId(lookup)) onOpen(lookup); }} />
      <button type="button" disabled={!isRunId(lookup)} onClick={() => onOpen(lookup)}>Open</button>
    </div>
  </div>;
}

function RunDetail({ id, view, onRefresh, onOpen }) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
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

  const badge = snapshot
    ? <StatusBadge status={snapshot.status} live />
    : !id && <StatusBadge tone="neutral" label="No run selected" />;

  return <Panel title="Run details" id="detail" className="detail" actions={badge}>
    {!id && <EmptyState title="No investigation selected." icon="runs">
      <p>Start a run to investigate a service. Its findings and supporting evidence will appear here.</p>
      <ol className="workflow-preview" aria-label="Investigation workflow">
        <li>Check current service health</li>
        <li>Consult the triage runbook</li>
        <li>Review an evidence-backed result</li>
      </ol>
      <RunLookup onOpen={onOpen} />
    </EmptyState>}
    {id && <code className="run-id">{id}</code>}
    {id && !snapshot && !view.error && <LoadingState>Loading run…</LoadingState>}
    {view.error && <ErrorState>{view.error}</ErrorState>}
    {actionError && <ErrorState>{actionError}</ErrorState>}
    {snapshot && <div className="stack">
      <KeyValueList items={[
        { label: 'Submitted', value: dateLabel(snapshot.submitted_at) },
        ...(snapshot.finished_at ? [{ label: 'Finished', value: dateLabel(snapshot.finished_at) }] : []),
      ]} />
      {snapshot.result && <section className="findings" aria-labelledby="findings-title">
        <div className="row spaced">
          <h3 id="findings-title">Findings</h3>
          <StatusBadge tone={{ low: 'good', medium: 'warning', high: 'critical' }[snapshot.result.severity] ?? 'neutral'}
            label={`${snapshot.result.severity[0].toUpperCase()}${snapshot.result.severity.slice(1)} severity`} />
        </div>
        <p className="summary">{snapshot.result.summary}</p>
        <h4>Evidence</h4>
        <ul className="evidence">{snapshot.result.evidence_ids.map((evidence) =>
          <li key={evidence}><code>{evidence}</code></li>)}</ul>
      </section>}
      {snapshot.error && <ErrorState>{snapshot.error.message} <code>{snapshot.error.code}</code></ErrorState>}
      {snapshot.status === 'WAITING_APPROVAL' && <section className="findings" aria-labelledby="approval-title">
        <h3 id="approval-title">Sandbox ticket approval</h3>
        {view.approvalError && <ErrorState>{view.approvalError}</ErrorState>}
        {approval && <>
          <pre>{approval.call.arguments}</pre>
          <p className="muted">Expires {dateLabel(approval.expires_at)}</p>
          {approval.status === 'pending' && <div className="row">
            <button type="button" className="primary" disabled={busy}
              onClick={() => act('approval', approvalBody(approval, 'approve'))}>Approve ticket</button>
            <button type="button" disabled={busy}
              onClick={() => act('approval', approvalBody(approval, 'reject'))}>Reject</button>
          </div>}
        </>}
      </section>}
      {snapshot.usage && <KeyValueList className="kv usage" items={[
        { label: 'Model requests', value: snapshot.usage.requests },
        { label: snapshot.usage.uncertain_requests ? 'Token estimate (in / out)' : 'Tokens (in / out)', value: `${snapshot.usage.input_tokens} / ${snapshot.usage.output_tokens}` },
        { label: 'Estimated cost', value: usd(snapshot.usage.estimated_cost_usd) },
      ]} />}
      {!terminal.has(snapshot.status) && <div>
        <button type="button" disabled={busy} onClick={() => act('cancel', {})}>Cancel run</button>
      </div>}
    </div>}
  </Panel>;
}

function App() {
  const [theme, setTheme] = useThemePreference();
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

  const connection = config
    ? { state: 'connected', label: providerNames[config.provider] }
    : { state: configError ? 'error' : 'connecting', label: configError ? 'API unavailable' : 'Connecting' };
  const context = config && `${config.model} · ${config.workflow} · ${config.durable ? 'Persistent runs' : 'Session only — resets with server'}`;

  return <AppShell sections={productionSections} active="runs" connection={connection} theme={theme} onThemeChange={setTheme}
    context={context} footer={<a className="sidebar-link" href="/docs" target="_blank" rel="noreferrer">API reference <Icon name="external" size={12} /></a>}>
    <PageHeader eyebrow="Runs / Incident triage" title="Run an investigation." description="Check service health and review the supporting evidence." />
    {configError && <ErrorState action={<button type="button" onClick={() => location.reload()}>Reconnect</button>}>{configError}</ErrorState>}
    <div className="grid sidebar-layout workspace">
      <div className="stack">
        <Panel title="New run" id="new" actions={<button type="button" className="text-button" onClick={() => {
          setService(demo.service); setDescription(demo.description);
        }}>Load example</button>}>
          <form className="stack" onSubmit={submit}>
            <div className="field">
              <label htmlFor="service">Service</label>
              <input id="service" value={service} placeholder="payments" required
                pattern="[a-z](?:[a-z0-9]|-){0,62}" maxLength={63}
                title="Start with a lowercase letter. Use lowercase letters, digits, and hyphens."
                onChange={(event) => setService(event.target.value)} />
            </div>
            <div className="field">
              <div className="row spaced"><label htmlFor="description">Incident description</label>
                <span className="counter">{description.length} / 2000</span>
              </div>
              <textarea id="description" value={description} maxLength={2000} required
                placeholder="What changed? Include symptoms the agent should investigate."
                onChange={(event) => setDescription(event.target.value)} />
            </div>
            {config?.durable && <div className="options">
              <label><input type="checkbox" checked={recall} onChange={(event) => setRecall(event.target.checked)} /> Recall saved evidence</label>
              <label><input type="checkbox" checked={ticket} onChange={(event) => setTicket(event.target.checked)} /> Propose a sandbox ticket</label>
            </div>}
            {submitError && <ErrorState>{submitError}</ErrorState>}
            <button className="primary submit" disabled={!config || !description.trim() || submitting}>
              {submitting ? 'Starting…' : <>Start run <span aria-hidden="true">→</span></>}
            </button>
            <p className="field-hint form-note">{config?.provider === 'fake'
              ? 'Uses synthetic evidence and offline inference.'
              : config ? 'Sends this incident and synthetic evidence to the configured provider.' : 'Waiting for the local API.'}</p>
          </form>
        </Panel>
        <Panel title="Recent runs" id="recent" className="recent" actions={recent.length > 0 &&
          <button type="button" className="text-button" aria-label="Clear recent run IDs from this browser" onClick={() => setRecent(clearRecent(browserStorage))}>Clear</button>}>
          {recent.length ? <ul>{recent.map((value) => <li key={value}>
            <button type="button" aria-label={`Open run ${value}`} aria-current={value === id ? 'true' : undefined}
              title={value} onClick={() => openRun(value)}><code>{value.slice(0, 8)}</code><span aria-hidden="true">↗</span></button>
          </li>)}</ul> : <p className="muted">Runs started in this browser will appear here.</p>}
        </Panel>
      </div>
      <RunDetail key={id} id={id} view={view} onOpen={openRun} onRefresh={() => setRevision((previous) => previous + 1)} />
    </div>
  </AppShell>;
}

createRoot(document.getElementById('root')).render(<App />);
