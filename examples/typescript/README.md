# TypeScript example

A Node agent on the RunWeave TypeScript SDK (`@runweave/agent`, from
[`sdk/typescript`](../../sdk/typescript)) that uses the `openai` npm client with Chat
Completions and a small tool loop. RunWeave starts it (`command` in `runweave.toml`),
calls it over the HTTP agent contract and stops it afterwards.

- The client points at RunWeave's LLM proxy (`ctx.proxy.openaiBaseUrl`, `ctx.proxy.token`)
  and passes `ctx.signal`, so a cancelled or timed-out run aborts its model call.
- Tool arguments from the model are validated before the tool runs as a timeline step.

## Run it

With Node 22.12 or newer, and RunWeave installed in a Python environment (see the
[quickstart](../../README.md)):

```bash
npm --prefix ../../sdk/typescript ci
npm --prefix ../../sdk/typescript run build
npm ci
runweave eval     # datasets/smoke.jsonl through the mock proxy: offline, no API key
runweave bench    # a small concurrency sweep; any failed run fails it
```

To serve it yourself instead, run `npm start` (it prints `RUNWEAVE_AGENT_READY <url>`) and
connect it with `runweave serve --http-agent <url>`.

## Files

| File | What it is |
| --- | --- |
| `agent.mjs` | The agent and its HTTP server |
| `runweave.toml` | The project: agent command, limits, mock proxy, dataset, bench sweep |
| `fixtures.json` | The mock model's script: call `get_weather` for a weather question, then answer from the tool result |
| `datasets/smoke.jsonl` | Cases: the tool is called, the answer uses it, a direct answer, an empty question is rejected |
| `package.json`, `package-lock.json` | Exactly pinned dependencies |
