import { createSseParser } from './sse.js';

export const providerNames = {
  fake: 'Offline demo',
  groq: 'Groq',
  openai: 'OpenAI',
  anthropic: 'Claude',
  xai: 'Grok',
  gemini: 'Gemini',
};

export const terminal = new Set(['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED']);
const runIdPattern = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;

export function isRunId(value) {
  return typeof value === 'string' && runIdPattern.test(value);
}

export function approvalBody(approval, decision) {
  return {
    approval_id: approval.id,
    call_id: approval.call.id,
    argument_digest: approval.argument_digest,
    policy_version: approval.policy_version,
    expires_at: approval.expires_at,
    decision,
  };
}

export async function api(path, { data, signal, method } = {}) {
  const response = await fetch(path, {
    signal,
    headers: data === undefined ? { Accept: 'application/json' } : {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    method: method ?? (data === undefined ? 'GET' : 'POST'),
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: 'no-store',
  });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const error = new Error(detail?.message || detail?.code ||
      (typeof detail === 'string' ? detail : `Request failed (HTTP ${response.status})`));
    error.status = response.status;
    error.detail = typeof detail === 'object' && detail !== null ? detail : undefined;
    throw error;
  }
  if (!body) throw new Error('RunWeave returned an unreadable response.');
  return body;
}

const agentNamePattern = /^[a-z][a-z0-9-]{0,62}$/;
const runStatuses = new Set(['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED']);

export function listAgents(options) {
  return api('/v1/agents', options);
}

// The server validates and normalizes agent URLs (loopback only by default); the browser
// only forwards what the developer typed.
export function checkHttpAgent(url, options) {
  return api('/v1/agents/http/check', { ...options, data: { url } });
}

export function registerHttpAgent(url, options) {
  return api('/v1/agents/http', { ...options, data: { url } });
}

export async function removeAgent(name, options) {
  if (!agentNamePattern.test(name ?? '')) throw new Error('Invalid agent name');
  return api(`/v1/agents/${name}`, { ...options, method: 'DELETE' });
}

const providers = new Set(['openai', 'anthropic']);

export function getSettings(options) {
  return api('/v1/settings', options);
}

export function updateSettings(patch, options) {
  return api('/v1/settings', { ...options, data: patch, method: 'PATCH' });
}

// Keys are write-only: they go to the server once and are never read back.
export async function saveProviderKey(provider, key, options) {
  if (!providers.has(provider)) throw new Error('Unknown provider');
  return api(`/v1/settings/keys/${provider}`, { ...options, data: { key }, method: 'PUT' });
}

const jobIdPattern = /^job-[0-9a-f]{12}$/;
const reportIdPattern = /^(?:eval|bench|shadow)-\d{8}T\d{6}Z-[0-9a-f]{6}$/;
const reportKinds = new Set(['eval', 'bench', 'shadow']);

export function isJobId(value) {
  return typeof value === 'string' && jobIdPattern.test(value);
}

export function isReportId(value) {
  return typeof value === 'string' && reportIdPattern.test(value);
}

const approvalIdPattern = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;
const approvalStatuses = new Set(['pending', 'all']);
const decisions = new Set(['approve', 'reject']);

export function isApprovalId(value) {
  return typeof value === 'string' && approvalIdPattern.test(value);
}

// Agent approvals (ctx.request_approval): the inbox and decisions bound to the exact request.
export function listApprovals(status = 'pending', options) {
  const which = approvalStatuses.has(status) ? status : 'pending';
  return api(`/v1/approvals?status=${which}`, options);
}

export async function getApproval(id, options) {
  if (!isApprovalId(id)) throw new Error('Invalid approval ID');
  return api(`/v1/approvals/${id}`, options);
}

export async function decideApproval(approval, decision, options) {
  if (!isApprovalId(approval?.id)) throw new Error('Invalid approval ID');
  if (!decisions.has(decision)) throw new Error('Unknown decision');
  return api(`/v1/approvals/${approval.id}`, {
    ...options,
    data: {
      decision,
      argument_digest: approval.argument_digest,
      policy_version: approval.policy_version,
      expires_at: approval.expires_at,
    },
  });
}

// Testing jobs: the workspace starts the same `runweave eval|bench|shadow` as the CLI.
export function getProject(options) {
  return api('/v1/jobs/project', options);
}

export function startJob(request, options) {
  return api('/v1/jobs', { ...options, data: request });
}

export function listJobs(options) {
  return api('/v1/jobs', options);
}

export async function getJob(id, after = 0, options) {
  if (!isJobId(id)) throw new Error('Invalid job ID');
  const cursor = Math.max(0, Math.floor(Number(after) || 0));
  return api(`/v1/jobs/${id}?after=${cursor}`, options);
}

export async function cancelJob(id, options) {
  if (!isJobId(id)) throw new Error('Invalid job ID');
  return api(`/v1/jobs/${id}/cancel`, { ...options, data: {} });
}

export async function listReports(kind, options) {
  if (!reportKinds.has(kind)) throw new Error('Unknown report kind');
  return api(`/v1/reports?kind=${kind}`, options);
}

export async function getReport(id, options) {
  if (!isReportId(id)) throw new Error('Invalid report ID');
  return api(`/v1/reports/${id}`, options);
}

export async function removeProviderKey(provider, options) {
  if (!providers.has(provider)) throw new Error('Unknown provider');
  return api(`/v1/settings/keys/${provider}`, { ...options, method: 'DELETE' });
}

export function listRuns({ agent, status, limit = 50, before } = {}, options) {
  const query = new URLSearchParams();
  if (agentNamePattern.test(agent ?? '')) query.set('agent', agent);
  if (runStatuses.has(status)) query.set('status', status);
  query.set('limit', String(Math.min(100, Math.max(1, Number(limit) || 50))));
  if (before && !Number.isNaN(Date.parse(before))) query.set('before', before);
  return api(`/v1/runs?${query}`, options);
}

// Resolves 'closed' when the run finished or the server ended the stream, 'reconnect' when the
// server asks the client to reconnect. Rejects on HTTP errors so callers can fall back to polling.
export async function streamRun(id, { onSnapshot, signal } = {}) {
  if (!isRunId(id)) throw new Error('Invalid run ID');
  const response = await fetch(`/v1/runs/${id}/events`, {
    signal, cache: 'no-store', headers: { Accept: 'text/event-stream' },
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    const error = new Error(body?.detail?.message || `Live updates unavailable (HTTP ${response.status})`);
    error.status = response.status;
    throw error;
  }
  let outcome = 'closed';
  const feed = createSseParser(({ event, data }) => {
    if (event === 'reconnect') outcome = 'reconnect';
    if (event === 'snapshot') onSnapshot(JSON.parse(data));
  });
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return outcome;
    feed(value);
  }
}
