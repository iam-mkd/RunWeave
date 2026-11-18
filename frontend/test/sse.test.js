import assert from 'node:assert/strict';
import { afterEach, test, vi } from 'vitest';
import { listAgents, listRuns, streamRun } from '../src/api.js';
import { createSseParser } from '../src/sse.js';

afterEach(() => vi.restoreAllMocks());

const id = '71bb412e-77f9-42a1-a8bf-751d39ecefad';

test('the parser assembles events across chunk boundaries and ignores comments', () => {
  const events = [];
  const feed = createSseParser((event) => events.push(event));
  feed('event: snapshot\ndata: {"a"');
  feed(':1}\n\n: keep-alive\n\nevent: reconnect\r\ndata: {}\r\n\r\n');
  feed('data: line one\ndata: line two\n\n');
  assert.deepEqual(events, [
    { event: 'snapshot', data: '{"a":1}' },
    { event: 'reconnect', data: '{}' },
    { event: 'message', data: 'line one\nline two' },
  ]);
});

function streamResponse(chunks) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

test('streamRun delivers each snapshot and reports how the stream ended', async () => {
  const seen = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path, options) => {
    assert.equal(path, `/v1/runs/${id}/events`);
    assert.equal(options.headers.Accept, 'text/event-stream');
    return streamResponse([
      `event: snapshot\ndata: {"id":"${id}","status":"RUNNING"}\n\n`,
      `event: snapshot\ndata: {"id":"${id}","status":"SUCCEEDED"}\n\n`,
    ]);
  });
  const outcome = await streamRun(id, { onSnapshot: (snapshot) => seen.push(snapshot.status) });
  assert.deepEqual(seen, ['RUNNING', 'SUCCEEDED']);
  assert.equal(outcome, 'closed');
});

test('streamRun reports reconnect requests and refuses invalid identifiers', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamResponse(['event: reconnect\ndata: {}\n\n']));
  assert.equal(await streamRun(id, { onSnapshot: () => {} }), 'reconnect');
  await assert.rejects(streamRun('../x', { onSnapshot: () => {} }));
});

test('streamRun surfaces HTTP errors with their status for the caller to fall back', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'EVENT_STREAMS_BUSY', message: 'Too many open event streams' } }), { status: 503 }));
  await assert.rejects(streamRun(id, { onSnapshot: () => {} }), (error) => error.status === 503);
});

test('list helpers build bounded, validated queries', async () => {
  const calls = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (path) => {
    calls.push(path);
    return new Response(JSON.stringify(path.startsWith('/v1/agents') ? { agents: [] } : { runs: [] }));
  });
  await listAgents();
  await listRuns({ agent: 'text-report', status: 'FAILED', limit: 500, before: '2026-11-18T10:00:00Z' });
  await listRuns({ agent: 'Bad Name', status: 'nope' });
  assert.deepEqual(calls, [
    '/v1/agents',
    '/v1/runs?agent=text-report&status=FAILED&limit=100&before=2026-11-18T10%3A00%3A00Z',
    '/v1/runs?limit=50',
  ]);
});
