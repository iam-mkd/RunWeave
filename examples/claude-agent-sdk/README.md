# Claude Agent SDK example

A [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/python) agent with an
in-process MCP tool. The SDK starts the Claude Code CLI bundled in its wheel; RunWeave's
LLM proxy stands in for Anthropic's API.

- The CLI gets `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` from `ctx.proxy.env()`: the
  proxy's Anthropic Messages endpoint and the run's proxy token.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off the CLI's telemetry, error
  reporting and update checks.
- `CLAUDE_CONFIG_DIR` is a throwaway folder per run and `setting_sources=[]`, so the CLI
  never reads or writes your own Claude settings, sessions or credentials.
- Built-in tools are off (`tools=[]`): no shell, file or web access. The only tool is
  `get_weather`, served by an in-process MCP server (`create_sdk_mcp_server`) and allowed
  explicitly. The model sees it as `mcp__weather__get_weather`.

Each run starts a CLI process, so runs take about two seconds and use more memory than
the other examples; the bench sweep is kept small (concurrency 1 and 2).

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
| `claude_weather.py` | The agent |
| `runweave.toml` | The project: agent, limits, mock proxy, dataset, bench sweep |
| `fixtures.json` | The mock model's script: call `mcp__weather__get_weather` for a weather question, then answer from the tool result |
| `datasets/smoke.jsonl` | Cases: the tool is called, the answer uses it, an empty question is rejected |
| `pyproject.toml` | Exactly pinned dependencies |
