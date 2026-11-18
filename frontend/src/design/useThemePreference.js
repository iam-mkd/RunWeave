import { useEffect, useState } from 'react';
import { browserStorage } from '../browserStorage.js';
import { applyTheme, readThemePreference, saveThemePreference } from './theme.js';

export function useThemePreference() {
  const [theme, setTheme] = useState(() => readThemePreference(browserStorage));
  useEffect(() => applyTheme(document.documentElement, theme), [theme]);
  return [theme, (preference) => setTheme(saveThemePreference(browserStorage, preference))];
}
