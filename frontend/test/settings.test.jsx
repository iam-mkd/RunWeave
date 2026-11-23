import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, test, vi } from 'vitest';
import { getSettings, removeProviderKey, saveProviderKey, updateSettings } from '../src/api.js';
import { RunTimeline, runModelSummary } from '../src/screens/parts.jsx';
import { ProviderKeyCard, environmentLines } from '../src/screens/SettingsScreen.jsx';

const render = (element) => renderToStaticMarkup(element);
afterEach(() => vi.restoreAllMocks());

const view = {
  proxy: {
    mode: 'passthrough', models: ['gpt-4.1-mini', 'claude-x'], public_url: 'http://127.0.0.1:8000',
    openai: { base_url: 'https://api.openai.com/v1', key: 'credential_store', concurrency: 4, requests_per_minute: 60 },
    anthropic: { base_url: 'https://api.anthropic.com', key: 'environment', concurrency: 4, requests_per_minute: 60 },
  },
  budgets: { agent_request_limit: 20, agent_input_tokens: 100000, agent_completion_tokens: 2048, agent_max_cost_usd: '0.05',
    model_input_usd_per_million: '1.5', model_output_usd_per_million: '6', model_price_version: 'list-2026-11' },
  faults: { profile: 'flaky', seed: 42, profiles: [] },
  key_store: { available: true, backend: 'WinVaultKeyring' },
};

describe('settings API', () => {
  test('reads, patches and manages keys with validated provider names', async () => {
    const calls = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
      calls.push([path, options.method, options.body]);
      return options.method === 'PUT' || options.method === 'DELETE'
        ? new Response(null, { status: 204 })
        : new Response(JSON.stringify(view), { status: 200 });
    });
    await getSettings();
    await updateSettings({ faults: { profile: 'chaos' } });
    assert.equal(await saveProviderKey('openai', 'sk-typed-by-user-123456'), null);
    assert.equal(await removeProviderKey('anthropic'), null);
    await assert.rejects(saveProviderKey('../runs', 'x'), /Unknown provider/);
    assert.deepEqual(calls, [
      ['/v1/settings', 'GET', undefined],
      ['/v1/settings', 'PATCH', JSON.stringify({ faults: { profile: 'chaos' } })],
      ['/v1/settings/keys/openai', 'PUT', JSON.stringify({ key: 'sk-typed-by-user-123456' })],
      ['/v1/settings/keys/anthropic', 'DELETE', undefined],
    ]);
  });
});

describe('environment lines', () => {
  test('make the current settings permanent without any key', () => {
    const lines = environmentLines(view);
    assert.ok(lines.includes('RUNWEAVE_PROXY_MODE=passthrough'));
    assert.ok(lines.includes('RUNWEAVE_PROXY_MODELS=\'["gpt-4.1-mini","claude-x"]\''));
    assert.ok(lines.includes('RUNWEAVE_PROXY_FAULT_PROFILE=flaky'));
    assert.ok(lines.includes('RUNWEAVE_PROXY_FAULT_SEED=42'));
    assert.ok(lines.includes('RUNWEAVE_AGENT_MAX_COST_USD=0.05'));
    assert.doesNotMatch(lines, /API_KEY/);
  });
});

describe('ProviderKeyCard', () => {
  test('never renders a key value and says where the key comes from', () => {
    const stored = render(<ProviderKeyCard provider="openai" status={view.proxy.openai} storeAvailable onChanged={() => {}} />);
    assert.match(stored, /Saved in the credential store/);
    assert.match(stored, /type="password"/);
    assert.match(stored, /autoComplete="off"/);
    assert.match(stored, /value=""/);
    assert.match(stored, />Remove key</);
    const env = render(<ProviderKeyCard provider="anthropic" status={view.proxy.anthropic} storeAvailable onChanged={() => {}} />);
    assert.match(env, /Set by environment variable/);
    assert.doesNotMatch(env, /type="password"/);
    const none = render(<ProviderKeyCard provider="openai" status={{ ...view.proxy.openai, key: 'none' }} storeAvailable={false} onChanged={() => {}} />);
    assert.match(none, /RUNWEAVE_PROXY_OPENAI_API_KEY/);
  });
});

describe('model calls on the run', () => {
  const steps = [
    { index: 0, attempt: 1, title: 'Model call', kind: 'model_call', fault: null, status: 'succeeded', detail: 'chat.completions · mock · 12 in / 8 out', duration_ms: 3 },
    { index: 1, attempt: 1, title: 'Model call', kind: 'model_call', fault: 'rate_limit', status: 'failed', detail: 'chat.completions · mock · fault: rate_limit (429)', duration_ms: 1 },
    { index: 2, attempt: 1, title: 'Think', kind: 'step', fault: null, status: 'succeeded', detail: null, duration_ms: 2 },
  ];

  test('the timeline marks injected faults', () => {
    const html = render(<RunTimeline steps={steps} />);
    assert.match(html, /Injected fault: rate_limit/);
    assert.equal((html.match(/Injected fault/g) ?? []).length, 1);
  });

  test('the timeline labels effects and a lost response', () => {
    const effect = { index: 3, attempt: 1, title: 'create_ticket', kind: 'effect', fault: 'response_lost',
      status: 'failed', detail: 'write committed · response lost', duration_ms: 4,
      effect: 'create_ticket', operation: 'a'.repeat(32), resource: 'b'.repeat(32) };
    const html = render(<RunTimeline steps={[...steps, effect]} />);
    assert.match(html, /Effect · Injected fault: response_lost/);
    assert.match(html, /write committed · response lost/);
    assert.doesNotMatch(html, /aaaaaaaa/);
  });

  test('the summary counts model calls and faults', () => {
    assert.deepEqual(runModelSummary(steps), { calls: 2, faults: 1, failed: 1 });
  });
});
