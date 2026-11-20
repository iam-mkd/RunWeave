# Quickstart: test an agent in five minutes

You will create a small agent, evaluate it, load-test it, and run it in the workspace. It
all runs on your computer, offline, with no API key: model calls get deterministic mock
replies.

## Before you start

- **Git**
- **uv**, the Python package manager: see [Installing uv](https://docs.astral.sh/uv/getting-started/installation/).
  uv downloads a suitable Python (3.11 or newer) for you.
- **Node.js 22.12 or newer (or 24)**, only to build the workspace UI once.

The commands below work the same in bash, zsh, PowerShell and Command Prompt.

## 1. Get RunWeave and build the workspace

<!-- quickstart:run -->
```bash
git clone https://github.com/iam-mkd/RunWeave.git
cd RunWeave
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
```

`uv sync` creates `.venv` with RunWeave and its dependencies; the two `npm` commands build
the workspace UI into `frontend/dist`, which `runweave serve` finds by itself. This step
takes a minute or two the first time.

## 2. Create an agent project

<!-- quickstart:run -->
```bash
uv run runweave init my-agent
cd my-agent
```

`my-agent/` holds `runweave.toml` (the project), `agent.py` (a small word-count agent) and
`datasets/smoke.jsonl` (four test cases). Because it sits inside the clone, `uv run` uses
RunWeave's environment. To create it elsewhere later, see [next steps](#next-steps).

## 3. Evaluate it

<!-- quickstart:run -->
```bash
uv run runweave eval
```

RunWeave starts in the background, runs each case through the agent, checks the
assertions, and prints a summary like:

```text
word-count on smoke: 4/4 cases passed (100%), p95 6.5 ms, 0 tokens, $0
Gates passed.
Report: my-agent/.runweave/reports/eval-20261120T150025Z-4b21a6
```

The exit code is `0` when every gate passes and `1` when one fails, so the same command
gates a pull request. Open `report.md` in the report folder it names: [Reading reports](reports.md) explains
each part.

## 4. Load-test it

<!-- quickstart:run -->
```bash
uv run runweave bench
```

`bench` sweeps concurrency levels 1, 4 and 16 with 32 runs each (about ten seconds) and
reports throughput,
p50/p95/p99 latency, queue wait, API latency, event-loop lag and memory per level. Add
`--fault-profile chaos` to inject provider failures; the [fault catalog](faults.md) lists
them. These numbers describe your machine with a mock model, [not provider
capacity](limits.md).

## 5. Run it in the workspace

<!-- quickstart:background http://127.0.0.1:8000/ -->
```bash
uv run runweave serve --agent agent:run
```

Open <http://127.0.0.1:8000> in your browser:

1. **Agents** lists `word-count`. Choose **New run** on its row.
2. In **text**, type `RunWeave tests agents`, then choose **Start run**.
3. The run detail shows each step on a timeline and the output: `{"words": 3, "longest":
   "RunWeave"}`.
4. **Evals** and **Load tests** start the same commands you just ran and list their saved
   reports.

Press Ctrl+C in the terminal to stop the server.

## Next steps

- **Make it yours:** replace the body of `run` in `agent.py`, then add cases to
  `datasets/smoke.jsonl` ([test your agent](testing.md)).
- **Call a model:** point your OpenAI or Anthropic SDK at `ctx.proxy` and every call is
  metered, budgeted and mocked offline ([LLM proxy](llm-proxy.md)). Switch to your own key
  when you are ready ([bring your own key](budgets.md#bring-your-own-key)).
- **Use a framework or another language:** see the [examples](README.md#examples) and
  [HTTP agents](http-agents.md).
- **A project outside the clone:** install RunWeave into your own environment with
  `uv pip install -e path/to/RunWeave` (or `pip install -e`), then run `runweave init` there.
- **Something went wrong?** See [troubleshooting](troubleshooting.md).
