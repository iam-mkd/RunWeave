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

export async function api(path, { data, signal } = {}) {
  const response = await fetch(path, {
    signal,
    headers: data === undefined ? { Accept: 'application/json' } : {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    method: data === undefined ? 'GET' : 'POST',
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: 'no-store',
  });
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
