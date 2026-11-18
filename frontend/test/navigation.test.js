import assert from 'node:assert/strict';
import { test } from 'vitest';
import { productionSections, sections } from '../src/navigation.js';

test('every planned section has a unique id, label and milestone', () => {
  assert.deepEqual(sections.map((section) => section.id), ['agents', 'runs', 'load-tests', 'evals', 'shadow', 'approvals', 'settings']);
  assert.equal(new Set(sections.map((section) => section.label)).size, sections.length);
  for (const section of sections) assert.ok(Number.isInteger(section.milestone), section.id);
});

test('production navigation lists only sections that exist', () => {
  assert.deepEqual(productionSections.map((section) => section.id), ['runs']);
});
