import assert from 'node:assert/strict';
import { test } from 'vitest';
import { applyTheme, readThemePreference, resolveTheme, saveThemePreference, themePreferences } from '../src/design/theme.js';

function memoryStorage(initial) {
  let value = initial;
  return { getItem: () => value, setItem: (_, next) => { value = next; }, read: () => value };
}

const blocked = { getItem() { throw new Error('Blocked'); }, setItem() { throw new Error('Blocked'); } };

test('theme preference defaults to system and rejects unknown values', () => {
  assert.deepEqual(themePreferences, ['system', 'dark', 'light']);
  assert.equal(readThemePreference(memoryStorage(null)), 'system');
  assert.equal(readThemePreference(memoryStorage('"><script>')), 'system');
  assert.equal(readThemePreference(memoryStorage('light')), 'light');
  assert.equal(readThemePreference(blocked), 'system');
});

test('saving a preference validates it and tolerates unavailable storage', () => {
  const storage = memoryStorage(null);
  assert.equal(saveThemePreference(storage, 'dark'), 'dark');
  assert.equal(storage.read(), 'dark');
  assert.equal(saveThemePreference(storage, 'neon'), 'system');
  assert.equal(storage.read(), 'system');
  assert.equal(saveThemePreference(blocked, 'light'), 'light');
});

test('system preference follows the operating system, explicit choices win', () => {
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('light', true), 'light');
});

test('applying a theme sets or clears the document data attribute', () => {
  const root = { dataset: {} };
  applyTheme(root, 'light');
  assert.equal(root.dataset.theme, 'light');
  applyTheme(root, 'system');
  assert.equal('theme' in root.dataset, false);
});
