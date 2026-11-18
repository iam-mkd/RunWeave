import React, { useEffect, useMemo, useState } from 'react';
import { api, isRunId, listAgents } from '../api.js';
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel } from '../components/index.js';
import { href } from '../router.js';
import { buildInput, formFields, initialValues } from '../schemaForm.js';
import { SchemaForm } from './parts.jsx';

const TRIAGE = 'incident-triage';
const triageExample = {
  service: 'payments',
  description: 'Payment authorization latency rose after the last deployment. Check service health and the triage runbook.',
};
// Approval tickets and recall need durable mode; the dedicated checkboxes below replace them.
const triageDurableOnly = new Set(['create_ticket', 'memory']);

function valuesFrom(fields, input) {
  const values = initialValues(fields);
  for (const field of fields) {
    if (!(field.name in (input ?? {}))) continue;
    const value = input[field.name];
    if (field.kind === 'boolean') values[field.name] = value === true;
    else if (field.kind === 'list') values[field.name] = Array.isArray(value) ? value.join('\n') : '';
    else if (field.kind === 'json') values[field.name] = JSON.stringify(value, null, 2);
    else values[field.name] = value === null || value === undefined ? '' : String(value);
  }
  return values;
}

export function NewRunScreen({ params, config, navigate }) {
  const [agents, setAgents] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [name, setName] = useState(params.agent ?? '');
  const [values, setValues] = useState({});
  const [raw, setRaw] = useState('{}');
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [ticket, setTicket] = useState(false);
  const [recall, setRecall] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    listAgents({ signal: controller.signal })
      .then((body) => setAgents(body.agents))
      .catch((error) => { if (!controller.signal.aborted) setLoadError(error.message); });
    return () => controller.abort();
  }, []);

  const agent = agents?.find((item) => item.name === name) ?? agents?.find((item) => item.default) ?? agents?.[0];
  const form = useMemo(() => {
    if (!agent) return { fields: [], supported: false };
    const derived = formFields(agent.input_schema);
    return agent.name === TRIAGE
      ? { ...derived, fields: derived.fields.filter((field) => !triageDurableOnly.has(field.name)) }
      : derived;
  }, [agent]);

  useEffect(() => {
    if (!agent) return undefined;
    setName(agent.name);
    setErrors({});
    setSubmitError('');
    const controller = new AbortController();
    const reset = (input) => {
      setValues(valuesFrom(form.fields, input));
      setRaw(JSON.stringify(input ?? {}, null, 2));
    };
    reset({});
    if (isRunId(params.from)) {
      api(`/v1/runs/${params.from}`, { signal: controller.signal })
        .then((run) => { if (run.agent === agent.name) reset(run.input); })
        .catch(() => { /* A missing source run leaves the empty form. */ });
    }
    return () => controller.abort();
  }, [agent, form, params.from]);

  async function submit(event) {
    event.preventDefault();
    if (!agent || submitting) return;
    let input;
    if (form.supported) {
      const built = buildInput(form.fields, values);
      if (!built.ok) {
        setErrors(built.errors);
        return;
      }
      input = built.input;
    } else {
      try {
        input = JSON.parse(raw);
      } catch {
        setErrors({ raw: 'Enter a valid JSON object.' });
        return;
      }
    }
    if (agent.name === TRIAGE && config?.durable) {
      input = { ...input, create_ticket: ticket, ...(recall ? { memory: {} } : {}) };
    }
    setSubmitting(true);
    setErrors({});
    setSubmitError('');
    try {
      const snapshot = await api('/v1/runs', { data: { agent: agent.name, input } });
      navigate(href({ screen: 'run', id: snapshot.id }));
    } catch (error) {
      const fieldErrors = {};
      for (const item of error.detail?.errors ?? []) {
        const field = item.loc?.[0];
        if (typeof field === 'string' && form.fields.some((candidate) => candidate.name === field)) {
          fieldErrors[field] = item.msg;
        }
      }
      setErrors(fieldErrors);
      setSubmitError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  if (loadError) return <><PageHeader eyebrow="Runs" title="New run" /><ErrorState>{loadError}</ErrorState></>;
  if (!agents) return <><PageHeader eyebrow="Runs" title="New run" /><LoadingState>Loading agents…</LoadingState></>;
  if (!agent) return <EmptyState title="No agents registered" icon="agents" />;

  return <>
    <PageHeader eyebrow={<><a href={href({ screen: 'runs' })}>Runs</a> / New</>} title="New run"
      description={agent.description || 'The form is generated from the agent’s input schema.'} />
    <Panel title={`Input for ${agent.name}`} id="new-run" actions={agent.name === TRIAGE &&
      <button type="button" className="text-button" onClick={() => setValues(valuesFrom(form.fields, triageExample))}>Load example</button>}>
      <form className="stack narrow" onSubmit={submit} noValidate={!form.supported}>
        <div className="field">
          <label htmlFor="run-agent">Agent</label>
          <select id="run-agent" value={agent.name} onChange={(event) => navigate(href({ screen: 'new-run', params: { agent: event.target.value } }))}>
            {agents.map((item) => <option key={item.name} value={item.name}>{item.name} · {item.version}</option>)}
          </select>
        </div>
        {form.supported
          ? <SchemaForm idPrefix="run" fields={form.fields} values={values} errors={errors}
            onChange={(field, value) => setValues((previous) => ({ ...previous, [field]: value }))} />
          : <div className="field">
            <label htmlFor="run-raw">Input (JSON)</label>
            <textarea id="run-raw" className="mono" value={raw} spellCheck={false} aria-invalid={errors.raw ? 'true' : undefined}
              aria-describedby={errors.raw ? 'run-raw-error' : undefined} onChange={(event) => setRaw(event.target.value)} />
            {errors.raw && <span className="field-error" id="run-raw-error">{errors.raw}</span>}
          </div>}
        {agent.name === TRIAGE && config?.durable && <div className="options">
          <label><input type="checkbox" checked={recall} onChange={(event) => setRecall(event.target.checked)} /> Recall saved evidence</label>
          <label><input type="checkbox" checked={ticket} onChange={(event) => setTicket(event.target.checked)} /> Propose a sandbox ticket</label>
        </div>}
        {submitError && <ErrorState>{submitError}</ErrorState>}
        <div className="row end">
          <a className="button" href={href({ screen: 'runs' })}>Cancel</a>
          <button className="primary" disabled={submitting || !config}>{submitting ? 'Starting…' : 'Start run'}</button>
        </div>
        <p className="field-hint">{config?.provider === 'fake'
          ? 'Runs offline with the mock model.'
          : config ? 'May call the configured model provider.' : 'Waiting for the local API.'}</p>
      </form>
    </Panel>
  </>;
}
