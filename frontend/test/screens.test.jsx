import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, test } from 'vitest';
import { HttpAgentCheck, RunOutput, RunTimeline, SchemaForm } from '../src/screens/parts.jsx';
import { SetupScreen } from '../src/screens/SetupScreen.jsx';
import { formFields } from '../src/schemaForm.js';

const render = (element) => renderToStaticMarkup(element);
const hostile = '<img src=x onerror="alert(1)">';

describe('SchemaForm', () => {
  const { fields } = formFields({
    type: 'object', required: ['text'],
    properties: {
      text: { type: 'string', maxLength: 500, title: 'Text', description: 'Words to analyse.' },
      mode: { enum: ['fast', 'slow'], type: 'string', title: 'Mode' },
      strict: { type: 'boolean', default: false, title: 'Strict' },
    },
  });

  test('renders labelled, described inputs with schema constraints', () => {
    const html = render(<SchemaForm idPrefix="run" fields={fields} values={{ text: '', mode: '', strict: false }} errors={{ text: 'Required' }} onChange={() => {}} />);
    assert.match(html, /<label for="run-text"[^>]*>Text/);
    assert.match(html, /<textarea[^>]*id="run-text"[^>]*maxLength="500"|<textarea[^>]*maxLength="500"[^>]*id="run-text"/);
    assert.match(html, /aria-invalid="true"/);
    assert.match(html, /aria-describedby="run-text-hint run-text-error"/);
    assert.match(html, /Words to analyse\./);
    assert.match(html, /<select[^>]*id="run-mode"/);
    assert.match(html, /type="checkbox"[^>]*id="run-strict"|id="run-strict"[^>]*type="checkbox"/);
  });
});

describe('RunTimeline', () => {
  test('maps step states to status badges and escapes agent text', () => {
    const html = render(<RunTimeline steps={[
      { index: 0, attempt: 1, title: 'Tokenize', status: 'succeeded', detail: hostile, duration_ms: 51.2, started_at: '2026-11-18T10:00:00Z' },
      { index: 1, attempt: 2, title: hostile, status: 'running', detail: null, duration_ms: null, started_at: '2026-11-18T10:00:01Z' },
      { index: 2, attempt: 2, title: 'Count', status: 'failed', detail: null, duration_ms: 3, started_at: '2026-11-18T10:00:02Z' },
    ]} />);
    assert.match(html, />Succeeded</);
    assert.match(html, />Running</);
    assert.match(html, />Failed</);
    assert.match(html, /51 ms/);
    assert.match(html, /Attempt 2/);
    assert.doesNotMatch(html, /<img/);
  });

  test('explains an empty timeline', () => {
    assert.match(render(<RunTimeline steps={[]} />), /No steps recorded/);
  });
});

describe('RunOutput', () => {
  test('renders triage findings with evidence, and other agents as JSON', () => {
    const triage = render(<RunOutput agent="incident-triage" result={{ summary: hostile, severity: 'high', evidence_ids: ['mock-health:payments'] }} />);
    assert.match(triage, /class="findings"/);
    assert.match(triage, />High severity</);
    assert.match(triage, /mock-health:payments/);
    assert.doesNotMatch(triage, /<img/);
    const generic = render(<RunOutput agent="text-report" result={{ words: 3, summary: hostile }} />);
    assert.match(generic, /<pre[^>]*>\{/);
    assert.match(generic, /&quot;words&quot;: 3/);
    assert.doesNotMatch(generic, /<img/);
  });
});

describe('HttpAgentCheck', () => {
  test('lists discovered agents, flags conflicts and escapes agent text', () => {
    const html = render(<HttpAgentCheck result={{
      url: 'http://127.0.0.1:9100',
      agents: [
        { name: 'remote-echo', version: '1.0.0', description: hostile, max_steps: 5, conflict: false },
        { name: 'echo', version: '2', description: '', max_steps: 50, conflict: true },
      ],
    }} />);
    assert.match(html, /remote-echo/);
    assert.match(html, /1\.0\.0/);
    assert.match(html, /Name already registered/);
    assert.ok(!html.includes('<img'));
    assert.match(html, /&lt;img/);
  });
});

describe('SetupScreen', () => {
  test('offers HTTP endpoints alongside Python callables', () => {
    const html = render(<SetupScreen />);
    const input = (value) => html.match(new RegExp(`<input[^>]*value="${value}"[^>]*>`))?.[0] ?? '';
    assert.ok(input('http') && !input('http').includes('disabled'));
    assert.ok(input('proxy').includes('disabled'));
  });
});
