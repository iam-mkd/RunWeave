import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, providerNames } from './api.js';
import { AppShell, ErrorState, Icon } from './components/index.js';
import { useThemePreference } from './design/useThemePreference.js';
import { productionSections } from './navigation.js';
import { href, parseRoute } from './router.js';
import { AgentsScreen } from './screens/AgentsScreen.jsx';
import { ApprovalsScreen } from './screens/ApprovalsScreen.jsx';
import { NewRunScreen } from './screens/NewRunScreen.jsx';
import { RunDetailScreen } from './screens/RunDetailScreen.jsx';
import { RunsScreen } from './screens/RunsScreen.jsx';
import { SettingsScreen } from './screens/SettingsScreen.jsx';
import { SetupScreen } from './screens/SetupScreen.jsx';
import { EvalsScreen, LoadTestsScreen, ShadowScreen } from './screens/testing.jsx';
import './design/tokens.css';
import './design/base.css';
import './components/components.css';
import './screens/screens.css';

function currentRoute() {
  return parseRoute(location.hash, location.search);
}

function useConnection() {
  const [config, setConfig] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function connect() {
      let connected = false;
      try {
        const [workspace] = await Promise.all([
          api('/v1/workspace', { signal: controller.signal }),
          api('/health/ready', { signal: controller.signal }),
        ]);
        if (!controller.signal.aborted) {
          setConfig(workspace);
          setError('');
          connected = true;
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setConfig(null);
          setError(failure.message);
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(connect, connected ? 15000 : 3000);
      }
    }
    connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  return [config, error];
}

function Screen({ route, config, navigate }) {
  if (route.screen === 'setup') return <SetupScreen />;
  if (route.screen === 'settings') return <SettingsScreen />;
  if (route.screen === 'approvals') return <ApprovalsScreen run={route.params.run} />;
  if (route.screen === 'load-tests') return <LoadTestsScreen report={route.params.report} navigate={navigate} />;
  if (route.screen === 'evals') return <EvalsScreen report={route.params.report} navigate={navigate} />;
  if (route.screen === 'shadow') return <ShadowScreen report={route.params.report} navigate={navigate} />;
  if (route.screen === 'runs') return <RunsScreen params={route.params} navigate={navigate} />;
  if (route.screen === 'run') return <RunDetailScreen id={route.id} />;
  if (route.screen === 'new-run') return <NewRunScreen params={route.params} config={config} navigate={navigate} />;
  return <AgentsScreen />;
}

function App() {
  const [theme, setTheme] = useThemePreference();
  const [config, configError] = useConnection();
  const [route, setRoute] = useState(currentRoute);

  useEffect(() => {
    // Old ?run=<id> links keep working; rewrite them to the hash route once.
    if (new URLSearchParams(location.search).has('run')) {
      const target = route.screen === 'run' ? href({ screen: 'run', id: route.id }) : href({ screen: 'agents' });
      history.replaceState(null, '', `${location.pathname}${target}`);
    }
    const onChange = () => {
      setRoute(currentRoute());
      document.getElementById('main')?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    };
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const navigate = (target) => { if (location.hash !== target) location.hash = target; };
  const active = route.screen === 'agents' || route.screen === 'setup' ? 'agents'
    : ['settings', 'load-tests', 'evals', 'shadow', 'approvals'].includes(route.screen) ? route.screen : 'runs';
  const connection = config
    ? { state: 'connected', label: providerNames[config.provider] ?? config.provider }
    : { state: configError ? 'error' : 'connecting', label: configError ? 'API unavailable' : 'Connecting' };
  const context = config && `${config.model} · ${config.durable ? 'Persistent runs' : 'Session only — resets with server'}`;

  return <AppShell sections={productionSections} active={active} brandHref="#/agents" connection={connection}
    theme={theme} onThemeChange={setTheme} context={context}
    footer={<a className="sidebar-link" href="/docs" target="_blank" rel="noreferrer">API reference <Icon name="external" size={12} /></a>}>
    {configError && <ErrorState action={<button type="button" onClick={() => location.reload()}>Reconnect</button>}>{configError}</ErrorState>}
    <Screen key={location.hash || route.screen} route={route} config={config} navigate={navigate} />
  </AppShell>;
}

createRoot(document.getElementById('root')).render(<App />);
