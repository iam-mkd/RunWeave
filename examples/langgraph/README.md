# LangGraph example

A [LangGraph](https://langchain-ai.github.io/langgraph/) ReAct agent built with
`create_react_agent`, using `ChatOpenAI` from `langchain-openai` and one `@tool`.

- `ChatOpenAI` points at RunWeave's LLM proxy (`ctx.proxy.openai_base_url`, with the run's
  `ctx.proxy.token` as the API key).
- The graph's `recursion_limit` bounds it to four model calls.
- LangSmith tracing stays off unless you set `LANGSMITH_TRACING`; RunWeave records the
  run's model calls and tool steps itself.

## Run it

From this folder, with [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. RunWeave is
installed from this repository's checkout:

```bash
uv venv
uv pip install ../.. -r pyproject.toml
uv run --no-sync runweave eval     # datasets/smoke.jsonl through the mock proxy: offline, no API key
uv run --no-sync runweave bench    # a small concurrency sweep; any failed run fails it
```

`--no-sync` keeps `uv run` from reinstalling this folder's `pyproject.toml`, which would look
for `runweave` in the package registry (it is not published there); the line before
installed it from the clone.

Reports are written to `.runweave/reports/`.

## Files

| File | What it is |
| --- | --- |
| `langgraph_weather.py` | The agent |
| `runweave.toml` | The project: agent, limits, mock proxy, dataset, bench sweep |
| `fixtures.json` | The mock model's script: call `get_weather` for a weather question, then answer from the tool result |
| `datasets/smoke.jsonl` | Cases: the tool is called, the answer uses it, an empty question is rejected |
| `pyproject.toml` | Exactly pinned dependencies |
