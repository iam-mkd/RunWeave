# Examples

Each folder below is a self-contained RunWeave project: a `runweave.toml` in mock mode, its
own exactly pinned dependencies, the agent, a dataset and a `fixtures.json` that scripts
the mock model. All five answer the same question ("What is the weather in Paris?") with
one tool, so you can compare how each framework connects to RunWeave.

| Example | Framework | What it shows |
| --- | --- | --- |
| [examples/plain-python](../examples/plain-python) | `openai` (Python) | An `@agent` with its own Chat Completions tool loop through `ctx.proxy`, validating the model's tool arguments |
| [examples/openai-agents](../examples/openai-agents) | OpenAI Agents SDK | `Agent` and `Runner` with one `@function_tool` on the proxy's Responses endpoint, tracing disabled |
| [examples/claude-agent-sdk](../examples/claude-agent-sdk) | Claude Agent SDK | The SDK's bundled Claude Code CLI on the proxy's Anthropic Messages endpoint, with an in-process MCP tool, built-in tools off, nonessential traffic disabled and a throwaway config folder |
| [examples/langgraph](../examples/langgraph) | LangGraph | `create_react_agent` with `ChatOpenAI` from `langchain-openai` pointed at the proxy |
| [examples/typescript](../examples/typescript) | `runweave-agent` and `openai` (Node) | An HTTP agent that `runweave eval` starts and stops, with a Chat Completions tool loop that passes the run's abort signal |

Every dataset checks three things: the agent called the tool (`tool_called`), the final
answer uses its result, and an empty question is rejected before a run starts
(`status: REJECTED`).

## Run one

Start from a clone of RunWeave (the [quickstart](quickstart.md) has the steps), then go into
the example's folder. The Python examples each get an environment of their own, with
RunWeave installed from the clone:

```bash
uv venv
uv pip install ../.. -r pyproject.toml
uv run --no-sync runweave eval     # the dataset through the mock proxy: offline, no API key
uv run --no-sync runweave bench    # a small concurrency sweep; any failed run fails it
```

`--no-sync` matters: without it, `uv run` installs the example's `pyproject.toml` again and
takes `runweave` from the package registry instead of the clone (before the first release,
it fails with "runweave was not found in the package registry"). The previous line already
installed it from the clone, so the example tests the code you have checked out.

The TypeScript example uses the clone's own environment, so after its `npm` steps (see its
README) it is `uv run runweave eval` and `uv run runweave bench`.

To use a real model instead, switch the proxy to passthrough with your own key and a spend
cap ([LLM proxy](llm-proxy.md#passthrough-to-your-provider)). The agents do not change.

## How CI proves they run offline

[`.github/workflows/examples.yml`](../.github/workflows/examples.yml) runs one job per
example: it installs RunWeave from the checkout and the example's pinned dependencies, then
runs `runweave eval` and `runweave bench` and uploads their reports. During those steps no
provider key is set, and `HTTP_PROXY`/`HTTPS_PROXY` point at a closed local port with
`NO_PROXY=127.0.0.1,localhost` (and `NODE_USE_ENV_PROXY=1`, so Node's `fetch` follows them
too). A model call or any other request that tried to leave the machine would fail the job.
The framework examples are also type-checked with `mypy --strict` in their own environment.

## Limits

- The mock model follows `fixtures.json`, not the prompt: these runs test the wiring, tool
  handling and RunWeave's gates, not answer quality. Bench numbers measure the agent and
  framework overhead against a local mock, not provider capacity.
- The Claude Agent SDK starts one CLI process per run, so it is slower and uses more memory
  than the others.
