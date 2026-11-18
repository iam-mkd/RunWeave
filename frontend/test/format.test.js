import assert from 'node:assert/strict';
import { test } from 'vitest';
import { compactNumber, duration, percent, sortRows, statusLabel, statusTone, usd } from '../src/format.js';

test('numbers are compact above ten thousand and grouped below', () => {
  assert.equal(compactNumber(0), '0');
  assert.equal(compactNumber(1284), '1,284');
  assert.equal(compactNumber(12900), '12.9K');
  assert.equal(compactNumber(4200000), '4.2M');
});

test('durations read naturally from milliseconds to minutes', () => {
  assert.equal(duration(850), '850 ms');
  assert.equal(duration(1366), '1.37 s');
  assert.equal(duration(61000), '1m 1s');
  assert.equal(duration(null), '—');
});

test('money and percentages keep useful precision', () => {
  assert.equal(usd(0.0123), '$0.0123');
  assert.equal(usd(1.234), '$1.23');
  assert.equal(usd('0.5'), '$0.5000');
  assert.equal(percent(0.9834), '98.3%');
});

test('run states map to sentence-case labels and fixed tones', () => {
  assert.equal(statusLabel('WAITING_APPROVAL'), 'Waiting for approval');
  assert.equal(statusLabel('TIMED_OUT'), 'Timed out');
  assert.equal(statusLabel('SOMETHING_NEW'), 'Something new');
  assert.equal(statusTone('SUCCEEDED'), 'good');
  assert.equal(statusTone('RUNNING'), 'info');
  assert.equal(statusTone('QUEUED'), 'neutral');
  assert.equal(statusTone('WAITING_APPROVAL'), 'warning');
  assert.equal(statusTone('TIMED_OUT'), 'serious');
  assert.equal(statusTone('FAILED'), 'critical');
  assert.equal(statusTone('CANCELLED'), 'neutral');
  assert.equal(statusTone('unknown'), 'neutral');
});

test('rows sort by column without mutating the input', () => {
  const rows = [{ name: 'b', ms: 20 }, { name: 'a', ms: 300 }, { name: 'c', ms: 5 }];
  assert.deepEqual(sortRows(rows, 'ms', 'ascending').map((row) => row.ms), [5, 20, 300]);
  assert.deepEqual(sortRows(rows, 'name', 'descending').map((row) => row.name), ['c', 'b', 'a']);
  assert.deepEqual(rows.map((row) => row.name), ['b', 'a', 'c']);
  assert.equal(sortRows(rows, null, 'ascending'), rows);
});
