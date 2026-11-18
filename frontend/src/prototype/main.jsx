import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AppShell } from '../components/index.js';
import { useThemePreference } from '../design/useThemePreference.js';
import { sections } from '../navigation.js';
import { AgentsScreen, SetupScreen } from './agents.jsx';
import { ApprovalsScreen, SettingsScreen } from './admin.jsx';
import { NewRunScreen, RunDetailScreen, RunsScreen } from './runs.jsx';
import { EvalsScreen, LoadTestsScreen, ShadowScreen } from './testing.jsx';
import '../design/tokens.css';
import '../design/base.css';
import '../components/components.css';
import './prototype.css';

const prototypeSections = sections.map((section) => ({ ...section, href: `#/${section.id}` }));

function parseRoute() {
  const [section = 'agents', detail] = location.hash.replace(/^#\/?/, '').split('?')[0].split('/');
  return { section, detail };
}

function Screen({ route, theme, onThemeChange }) {
  if (route.section === 'setup') return <SetupScreen />;
  if (route.section === 'runs' && route.detail === 'new') return <NewRunScreen />;
  if (route.section === 'runs' && route.detail) return <RunDetailScreen id={route.detail} />;
  if (route.section === 'runs') return <RunsScreen />;
  if (route.section === 'load-tests') return <LoadTestsScreen />;
  if (route.section === 'evals') return <EvalsScreen />;
  if (route.section === 'shadow') return <ShadowScreen />;
  if (route.section === 'approvals') return <ApprovalsScreen />;
  if (route.section === 'settings') return <SettingsScreen theme={theme} onThemeChange={onThemeChange} />;
  return <AgentsScreen />;
}

function Prototype() {
  const [theme, setTheme] = useThemePreference();
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute());
      window.scrollTo(0, 0);
      document.getElementById('main')?.focus({ preventScroll: true });
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const active = route.section === 'setup' ? 'agents' : route.section;
  return <AppShell sections={prototypeSections} active={active} brandHref="#/agents" theme={theme} onThemeChange={setTheme}
    connection={{ state: 'connected', label: 'Prototype · synthetic data' }}
    context={<span className="prototype-note">Design prototype for milestone 18 review. Data is fictional; nothing is sent anywhere.</span>}>
    <Screen key={`${route.section}/${route.detail ?? ''}`} route={route} theme={theme} onThemeChange={setTheme} />
  </AppShell>;
}

createRoot(document.getElementById('root')).render(<Prototype />);
