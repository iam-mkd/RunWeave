# Plain Python example

An `@agent` that uses the official `openai` client with Chat Completions and a small tool
loop of its own: the model asks for `get_weather`, the agent validates the arguments, runs
the tool as a timeline step and sends the result back, for at most four turns.

The client points at RunWeave's LLM proxy (`ctx.proxy.openai_base_url`, with the run's
`ctx.proxy.token` as the API key), so every model call is metered and shown on the run. In
mock mode the proxy answers from `fixtures.json`.

## Run it

From this folder, with [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. RunWeave is
installed from this repository's checkout:

```bash
uv venv
uv pip install ../.. -r pyproject.toml
uv run --no-sync runweave eval     # datasets/smoke.jsonl through the mock proxy: offline, no API key
uv run --no-sync runweave bench    # a small concurrency sweep; any failed run fails it
```

Reports are written to `.runweave/reports/`.

## Files

| File | What it is |
| --- | --- |
| `openai_weather.py` | The agent |
| `runweave.toml` | The project: agent, limits, mock proxy, dataset, bench sweep |
| `fixtures.json` | The mock model's script: call `get_weather` for a weather question, then answer from the tool result |
| `datasets/smoke.jsonl` | Cases: the tool is called, the answer uses it, an empty question is rejected |
| `pyproject.toml` | Exactly pinned dependencies |

To try a real model, switch RunWeave's proxy to passthrough with your own key and a spend
cap (see [docs/llm-proxy.md](../../docs/llm-proxy.md)); the agent code does not change.
