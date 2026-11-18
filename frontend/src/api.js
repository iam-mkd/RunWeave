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
const recentKey = 'runweave.recent.v1';

export function isRunId(value) {
  return typeof value === 'string' && runIdPattern.test(value);
}

export function readRecent(storage) {
  try {
    const ids = JSON.parse(storage.getItem(recentKey) ?? '[]');
    return Array.isArray(ids) ? [...new Set(ids.filter(isRunId))].slice(0, 12) : [];
  } catch {
    return [];
  }
}

export function rememberRun(storage, id) {
  if (!isRunId(id)) return readRecent(storage);
  const ids = [id, ...readRecent(storage).filter((value) => value !== id)].slice(0, 12);
  try {
    storage.setItem(recentKey, JSON.stringify(ids));
  } catch {
    // The current page still works if browser storage is unavailable.
  }
  return ids;
}

export function clearRecent(storage) {
  try {
    storage.setItem(recentKey, '[]');
  } catch { /* Clearing the current view still works without browser storage. */ }
  return [];
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
    throw error;
  }
  if (!body) throw new Error('RunWeave returned an unreadable response.');
  return body;
}
