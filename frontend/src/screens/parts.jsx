import React, { useState } from 'react';
import { DataTable, EmptyState, StatusBadge, Timeline, useScrollRegion } from '../components/index.js';
import { duration } from '../format.js';

const stepTones = {
  running: ['info', 'Running'],
  succeeded: ['good', 'Succeeded'],
  failed: ['critical', 'Failed'],
};

// Browsers compile `pattern` with the `v` flag; skip any schema regex it would reject
// (the server still validates) instead of letting the browser drop it with a warning.
export function htmlPattern(pattern) {
  if (!pattern) return undefined;
  const body = pattern.replace(/^\^/, '').replace(/\$$/, '');
  try {
    new RegExp(`^(?:${body})$`, 'v');
    return body;
  } catch {
    return undefined;
  }
}

function hintFor(field) {
  const parts = [field.description];
  const { minLength, maxLength, minimum, maximum, maxItems } = field.constraints;
  if (field.kind === 'list') parts.push(`One per line${maxItems ? `, up to ${maxItems}` : ''}.`);
  if (field.kind === 'json') parts.push('JSON value.');
  if (maxLength !== undefined && field.kind !== 'textarea') parts.push(`${minLength ?? 0}–${maxLength} characters.`);
  if (minimum !== undefined || maximum !== undefined) parts.push(`Between ${minimum ?? '−∞'} and ${maximum ?? '∞'}.`);
  return parts.filter(Boolean).join(' ');
}

function Control({ field, id, value, invalid, describedBy, onChange }) {
  const common = {
    id,
    name: field.name,
    'aria-invalid': invalid ? 'true' : undefined,
    'aria-describedby': describedBy || undefined,
  };
  if (field.kind === 'boolean') {
    return <input {...common} type="checkbox" checked={value === true} onChange={(event) => onChange(event.target.checked)} />;
  }
  if (field.kind === 'select') {
    return <select {...common} value={value} required={field.required} onChange={(event) => onChange(event.target.value)}>
      <option value="">{field.required ? 'Choose…' : 'Not set'}</option>
      {field.constraints.enum.map((option) => <option key={option} value={option}>{option}</option>)}
    </select>;
  }
  if (field.kind === 'textarea' || field.kind === 'list' || field.kind === 'json') {
    return <textarea {...common} value={value} required={field.required && field.kind !== 'list'}
      maxLength={field.kind === 'textarea' ? field.constraints.maxLength : undefined}
      className={field.kind === 'json' ? 'mono' : undefined} spellCheck={field.kind === 'json' ? false : undefined}
      onChange={(event) => onChange(event.target.value)} />;
  }
  const numeric = field.kind === 'integer' || field.kind === 'number';
  return <input {...common} value={value} required={field.required}
    inputMode={numeric ? (field.kind === 'integer' ? 'numeric' : 'decimal') : undefined}
    maxLength={numeric ? undefined : field.constraints.maxLength}
    minLength={numeric ? undefined : field.constraints.minLength}
    pattern={numeric ? undefined : htmlPattern(field.constraints.pattern)}
    onChange={(event) => onChange(event.target.value)} />;
}

export function SchemaForm({ idPrefix, fields, values, errors, onChange }) {
  return <div className="stack schema-form">{fields.map((field) => {
    const id = `${idPrefix}-${field.name}`;
    const hint = hintFor(field);
    const error = errors[field.name];
    const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(' ');
    const control = <Control field={field} id={id} value={values[field.name]} invalid={Boolean(error)}
      describedBy={describedBy} onChange={(value) => onChange(field.name, value)} />;
    return <div className={field.kind === 'boolean' ? 'field checkbox-field' : 'field'} key={field.name}>
      {field.kind === 'boolean'
        ? <label htmlFor={id}>{control} {field.label}</label>
        : <><label htmlFor={id}>{field.label}{field.required && <span className="required" aria-hidden="true"> *</span>}</label>{control}</>}
      {hint && <span className="field-hint" id={`${id}-hint`}>{hint}</span>}
      {error && <span className="field-error" id={`${id}-error`}>{error}</span>}
    </div>;
  })}</div>;
}

export function RunTimeline({ steps }) {
  if (!steps.length) {
    return <EmptyState title="No steps recorded yet" icon="runs">
      <p>Steps appear here as the agent calls <code>ctx.step()</code>, or as the built-in agent works.</p>
    </EmptyState>;
  }
  return <Timeline steps={steps.map((step) => {
    const [tone, label] = stepTones[step.status] ?? ['neutral', step.status];
    return {
      id: `${step.attempt}-${step.index}`,
      title: step.title,
      tone,
      label,
      duration: step.duration_ms === null || step.duration_ms === undefined ? undefined : duration(step.duration_ms),
      meta: [step.attempt > 1 ? `Attempt ${step.attempt}` : null,
        step.fault ? `Injected fault: ${step.fault}` : null].filter(Boolean).join(' · ') || undefined,
      detail: step.detail || undefined,
    };
  })} />;
}

const severityTones = { low: 'good', medium: 'warning', high: 'critical' };

/** Preformatted text that scrolls in its box and stays reachable by keyboard. */
export function ScrollPre({ id, label, className, children }) {
  const region = useScrollRegion(label);
  return <pre id={id} className={className} {...region}>{children}</pre>;
}

export function JsonBlock({ value, label = 'JSON', id }) {
  return <ScrollPre id={id} label={label} className="json-block">{JSON.stringify(value, null, 2)}</ScrollPre>;
}

export function RunOutput({ agent, result }) {
  const triage = agent === 'incident-triage' && typeof result?.summary === 'string' && Array.isArray(result?.evidence_ids);
  if (!triage) return <JsonBlock value={result} />;
  const severity = String(result.severity);
  return <section className="findings" aria-labelledby="findings-title">
    <div className="row spaced">
      <h3 id="findings-title">Findings</h3>
      <StatusBadge tone={severityTones[severity] ?? 'neutral'} label={`${severity.charAt(0).toUpperCase()}${severity.slice(1)} severity`} />
    </div>
    <p className="summary">{result.summary}</p>
    <h4>Evidence</h4>
    <ul className="evidence">{result.evidence_ids.map((evidence) => <li key={evidence}><code>{evidence}</code></li>)}</ul>
  </section>;
}

// What a Connect check found at an HTTP agent URL. Agent text is rendered as plain text.
export function HttpAgentCheck({ result }) {
  return <DataTable caption={`Agents served at ${result.url}`} columns={[
    { key: 'name', label: 'Agent', render: (agent) => <span className="mono">{agent.name}</span> },
    { key: 'version', label: 'Version', render: (agent) => <span className="mono">{agent.version}</span> },
    { key: 'description', label: 'Description', render: (agent) => agent.description || <span className="muted">—</span> },
    { key: 'status', label: 'Status', render: (agent) => agent.conflict
      ? <StatusBadge tone="warning" label="Name already registered" />
      : <StatusBadge tone="good" label="Ready to register" /> },
  ]} rows={result.agents} rowKey={(agent) => agent.name} />;
}

// Proxied model calls and injected faults on a run, from its timeline.
export function runModelSummary(steps) {
  const calls = steps.filter((step) => step.kind === 'model_call');
  return {
    calls: calls.length,
    faults: calls.filter((step) => step.fault).length,
    failed: calls.filter((step) => step.status === 'failed').length,
  };
}

export function CopyBlock({ id, text, label = 'Command' }) {
  const [copied, setCopied] = useState('');
  const region = useScrollRegion(label);
  const { ref } = region;
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
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
    <pre id={id} {...region}>{text}</pre>
    <div className="row"><button type="button" onClick={copy} aria-describedby={id}>Copy</button>
      <span className="muted" role="status">{copied}</span></div>
  </div>;
}
