# @runweave/agent

Serve Node agents over the RunWeave HTTP agent contract (`runweave.agent/v1`), so RunWeave
can run, time-limit, cancel and test them. No runtime dependencies. Node 22.12+ or 24+.

```js
import { defineAgent, serveAgents } from '@runweave/agent';

const echo = defineAgent({
  name: 'echo',
  version: '1.0.0',
  inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] },
  outputSchema: { type: 'object', properties: { reply: { type: 'string' } }, required: ['reply'] },
  async run(input, ctx) {
    const reply = await ctx.step('Answer', () => callMyModel(input.text, { signal: ctx.signal }));
    return { reply };
  },
});

const server = await serveAgents([echo], { port: 9100 }); // loopback only
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
```

Then `runweave serve --http-agent http://127.0.0.1:9100`, or connect it from the
workspace (Agents → Connect agent → HTTP endpoint).

## The context

| Member | Meaning |
| --- | --- |
| `ctx.runId`, `ctx.agent`, `ctx.attempt` | Identity of this run; `attempt` is 2+ when durable mode re-runs it |
| `ctx.signal` | Aborted when RunWeave cancels the run or its deadline passes. Pass it to `fetch` and timers |
| `ctx.remainingMs()`, `ctx.remainingSeconds()` | Time left in the run deadline |
| `await ctx.step(title, fn, { detail })` | Runs `fn` as one timeline step; more than `maxSteps` fails the run with `AGENT_STEP_LIMIT` |
| `ctx.log(message)` | JSON log line with `protectedValues` redacted (1,000 characters max) |
| `ctx.usage()` | `null` until the RunWeave LLM proxy meters model calls |

RunWeave validates input against `inputSchema` before calling you and output against
`outputSchema` afterwards. For local validation (for example with zod or ajv), pass
`parseInput` (throw `InputError` with `{loc, type, msg}` entries, never the values) and
`parseOutput`. Throw `AgentError(code)` for a classified failure; any other error is
logged in your process and reported as `RUN_FAILED` without details.

JavaScript cannot stop a promise from outside. When a run is cancelled or times out, the
SDK answers RunWeave at once and discards the result; your code stops early only if it
watches `ctx.signal`.

## Server options

`serveAgents(agents, options)` and `createAgentHandler(agents, options)` (a Node request
listener for your own server) accept `maxConcurrency` (32; beyond it 503 `AGENT_BUSY`,
which RunWeave retries), `maxBodyBytes` (1 MiB), `protectedValues`, `idempotencyCacheSize`
(256) and `idempotencyTtlMs` (10 minutes), and `logger`. A repeated `Idempotency-Key`
runs once; closing the request or calling the cancel endpoint aborts `ctx.signal`.

See `docs/http-agents.md` in the RunWeave repository for the full contract.
