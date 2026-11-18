export const themePreferences = ['system', 'dark', 'light'];
const storageKey = 'runweave.theme.v1';

function valid(value) {
  return themePreferences.includes(value) ? value : 'system';
}

export function readThemePreference(storage) {
  try {
    return valid(storage.getItem(storageKey));
  } catch {
    return 'system';
  }
}

export function saveThemePreference(storage, preference) {
  const value = valid(preference);
  try {
    storage.setItem(storageKey, value);
  } catch { /* The choice still applies to this page without browser storage. */ }
  return value;
}

export function resolveTheme(preference, prefersDark) {
  if (preference === 'dark' || preference === 'light') return preference;
  return prefersDark ? 'dark' : 'light';
}

export function applyTheme(root, preference) {
  if (preference === 'dark' || preference === 'light') root.dataset.theme = preference;
  else delete root.dataset.theme;
}
