# OpenAI Agents SDK example

An [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) agent with one
`@function_tool`, running on the Responses API.

- The SDK's model is `OpenAIResponsesModel` with an `AsyncOpenAI` client pointed at
  RunWeave's LLM proxy (`ctx.proxy.openai_base_url`, `ctx.proxy.token`).
- Tracing is disabled for the run (`RunConfig(tracing_disabled=True)`), so nothing is sent
  to OpenAI's trace collector. RunWeave records the run's model calls and tool steps itself.
- The tool records itself as a `get_weather` timeline step, which the dataset asserts.

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
| `agents_sdk_weather.py` | The agent |
| `runweave.toml` | The project: agent, limits, mock proxy, dataset, bench sweep |
| `fixtures.json` | The mock model's script: call `get_weather` for a weather question, then answer from the tool result |
| `datasets/smoke.jsonl` | Cases: the tool is called, the answer uses it, an empty question is rejected |
| `pyproject.toml` | Exactly pinned dependencies |
