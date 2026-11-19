import { isReportId, isRunId } from './api.js';

const agentPattern = /^[a-z][a-z0-9-]{0,62}$/;
const testingScreens = new Set(['load-tests', 'evals', 'shadow']);
const statuses = new Set(['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED']);

function cleanParams(query) {
  const raw = new URLSearchParams(query);
  const params = {};
  if (agentPattern.test(raw.get('agent') ?? '')) params.agent = raw.get('agent');
  if (statuses.has(raw.get('status'))) params.status = raw.get('status');
  if (isRunId(raw.get('from'))) params.from = raw.get('from');
  if (isReportId(raw.get('report'))) params.report = raw.get('report');
  return params;
}

export function parseRoute(hash, search) {
  const [path, query = ''] = String(hash).replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean);
  const params = cleanParams(query);
  if (!parts.length) {
    const legacy = new URLSearchParams(search).get('run');
    return isRunId(legacy) ? { screen: 'run', id: legacy, params: {} } : { screen: 'agents', params: {} };
  }
  if (parts[0] === 'setup') return { screen: 'setup', params: {} };
  if (parts[0] === 'settings') return { screen: 'settings', params: {} };
  if (testingScreens.has(parts[0]) && parts.length === 1) {
    return { screen: parts[0], params: params.report ? { report: params.report } : {} };
  }
  if (parts[0] === 'runs' && parts[1] === 'new') return { screen: 'new-run', params };
  if (parts[0] === 'runs' && parts.length > 1) {
    return { screen: 'run', id: parts.length === 2 && isRunId(parts[1]) ? parts[1] : null, params: {} };
  }
  if (parts[0] === 'runs') return { screen: 'runs', params };
  return { screen: 'agents', params: {} };
}

function query(params = {}) {
  const clean = cleanParams(new URLSearchParams(Object.entries(params).filter(([, value]) => value)));
  const text = new URLSearchParams(clean).toString();
  return text ? `?${text}` : '';
}

export function href({ screen, id, params }) {
  if (screen === 'run') {
    if (!isRunId(id)) throw new Error('Invalid run ID');
    return `#/runs/${id}`;
  }
  if (screen === 'new-run') return `#/runs/new${query(params)}`;
  if (screen === 'runs') return `#/runs${query(params)}`;
  if (screen === 'setup') return '#/setup';
  if (screen === 'settings') return '#/settings';
  if (testingScreens.has(screen)) {
    return `#/${screen}${query(params?.report ? { report: params.report } : {})}`;
  }
  return '#/agents';
}
