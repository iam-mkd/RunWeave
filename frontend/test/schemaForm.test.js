import assert from 'node:assert/strict';
import { describe, test } from 'vitest';
import { buildInput, formFields, initialValues } from '../src/schemaForm.js';

// Shapes produced by Pydantic v2 model_json_schema().
const textReport = {
  title: 'TextInput', type: 'object', required: ['text'],
  properties: {
    text: { type: 'string', minLength: 1, maxLength: 5000, title: 'Text', description: 'Text to analyse.' },
    keywords: { type: 'array', items: { type: 'string' }, maxItems: 20, title: 'Keywords' },
  },
};

const incident = {
  title: 'IncidentRequest', type: 'object', required: ['service', 'description'],
  $defs: { MemorySelection: { type: 'object', properties: { project: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null } } } },
  properties: {
    service: { type: 'string', pattern: '^[a-z][a-z0-9-]{0,62}$', title: 'Service' },
    create_ticket: { type: 'boolean', default: false, title: 'Create Ticket' },
    memory: { anyOf: [{ $ref: '#/$defs/MemorySelection' }, { type: 'null' }], default: null },
    description: { type: 'string', minLength: 1, maxLength: 2000, title: 'Description' },
  },
};

const mixed = {
  type: 'object', required: ['count', 'mode'],
  properties: {
    count: { type: 'integer', minimum: 1, maximum: 10, title: 'Count' },
    ratio: { anyOf: [{ type: 'number' }, { type: 'null' }], default: null },
    mode: { enum: ['fast', 'slow'], type: 'string', title: 'Mode' },
    config: { type: 'object', additionalProperties: true, title: 'Config' },
  },
};

describe('formFields', () => {
  test('derives typed fields, labels and constraints from a Pydantic schema', () => {
    const { fields, supported } = formFields(textReport);
    assert.equal(supported, true);
    assert.deepEqual(fields.map((field) => [field.name, field.kind, field.required]), [
      ['text', 'textarea', true],
      ['keywords', 'list', false],
    ]);
    assert.equal(fields[0].label, 'Text');
    assert.equal(fields[0].description, 'Text to analyse.');
    assert.equal(fields[0].constraints.maxLength, 5000);
  });

  test('handles optional values, references, enums, numbers and nested objects', () => {
    const fields = Object.fromEntries(formFields(mixed).fields.map((field) => [field.name, field]));
    assert.equal(fields.count.kind, 'integer');
    assert.deepEqual([fields.count.constraints.minimum, fields.count.constraints.maximum], [1, 10]);
    assert.equal(fields.ratio.kind, 'number');
    assert.equal(fields.ratio.required, false);
    assert.equal(fields.mode.kind, 'select');
    assert.deepEqual(fields.mode.constraints.enum, ['fast', 'slow']);
    assert.equal(fields.config.kind, 'json');
    const triage = Object.fromEntries(formFields(incident).fields.map((field) => [field.name, field]));
    assert.equal(triage.memory.kind, 'json');
    assert.equal(triage.create_ticket.kind, 'boolean');
    assert.equal(triage.service.constraints.pattern, '^[a-z][a-z0-9-]{0,62}$');
    assert.equal(triage.description.kind, 'textarea');
  });

  test('a schema that is not an object of properties falls back to raw JSON', () => {
    assert.equal(formFields({ type: 'array' }).supported, false);
    assert.equal(formFields(null).supported, false);
  });
});

describe('buildInput', () => {
  test('coerces values, omits empty optional fields and reports missing ones', () => {
    const { fields } = formFields(mixed);
    const values = { ...initialValues(fields), count: '3', mode: 'fast', config: '{"a": 1}' };
    assert.deepEqual(buildInput(fields, values), { ok: true, input: { count: 3, mode: 'fast', config: { a: 1 } } });
    const missing = buildInput(fields, initialValues(fields));
    assert.equal(missing.ok, false);
    assert.deepEqual(Object.keys(missing.errors).sort(), ['count', 'mode']);
  });

  test('rejects bad numbers and bad JSON without throwing', () => {
    const { fields } = formFields(mixed);
    const result = buildInput(fields, { count: '2.5', mode: 'slow', ratio: 'abc', config: '{not json' });
    assert.equal(result.ok, false);
    assert.deepEqual(Object.keys(result.errors).sort(), ['config', 'count', 'ratio']);
  });

  test('lists split by line and booleans keep their defaults', () => {
    const report = formFields(textReport).fields;
    assert.deepEqual(buildInput(report, { text: 'Hi', keywords: 'alpha\n\n beta \n' }),
      { ok: true, input: { text: 'Hi', keywords: ['alpha', 'beta'] } });
    const triage = formFields(incident).fields;
    const values = { ...initialValues(triage), service: 'payments', description: 'Latency' };
    assert.deepEqual(buildInput(triage, values), { ok: true, input: { service: 'payments', create_ticket: false, description: 'Latency' } });
  });
});
