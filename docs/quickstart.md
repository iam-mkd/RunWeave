# Quickstart: test an agent in five minutes

You will create a small agent, evaluate it, load-test it, and run it in the workspace. It
all runs on your computer, offline, with no API key: model calls get deterministic mock
replies.

## Before you start

- **uv**, the Python package manager: see [Installing uv](https://docs.astral.sh/uv/getting-started/installation/).
  uv uses a Python 3.11 or newer that it finds on your computer, or downloads one. CI tests
  3.11, 3.12 and 3.13.

That is all: no Node.js, no clone and no API key. The commands below work the same in bash,
zsh, PowerShell and Command Prompt.

## 1. Install RunWeave

<!-- quickstart:run -->
```bash
uv tool install runweave
runweave --version
```

`uv tool install` puts the `runweave` command in its own environment, with the workspace
UI already built in. The second line prints the installed version, such as
`runweave 0.1.0`. If your terminal says `runweave` is not found, run `uv tool update-shell`,
then open a new terminal.

Without uv, install it with [pipx](https://pipx.pypa.io/) (`pipx install runweave`), or
with `pip install runweave` inside a virtual environment of your own. To upgrade later:
`uv tool upgrade runweave` (or `pipx upgrade runweave`, or `pip install -U runweave`).

## 2. Create an agent project

<!-- quickstart:run -->
```bash
runweave init my-agent
cd my-agent
```

`my-agent/` holds `runweave.toml` (the project), `agent.py` (a small word-count agent),
`datasets/smoke.jsonl` (four test cases), a `README.md` and a `.gitignore` for the reports.
Create it wherever you keep your code.

## 3. Evaluate it

<!-- quickstart:run -->
```bash
runweave eval
```

RunWeave starts in the background, runs each case through the agent, checks the
assertions, and prints a summary like:

```text
word-count on smoke: 4/4 cases passed (100%), p95 6.5 ms, 0 tokens, $0
Gates passed.
Report: C:\...\my-agent\.runweave\reports\eval-20261120T150025Z-4b21a6
```

The exit code is `0` when every gate passes and `1` when one fails, so the same command
gates a pull request. The report folder is printed with its full path; open `report.md` in it: [Reading reports](reports.md) explains
each part.

## 4. Load-test it

<!-- quickstart:run -->
```bash
runweave bench
```

`bench` sweeps concurrency levels 1, 4 and 16 with 32 runs each, in about ten seconds. The
terminal shows throughput, p95 latency and failures per level; the `report.md` it names adds
p50/p99 latency, queue wait, API latency, event-loop lag and memory. Add
`--fault-profile chaos` to inject provider failures; the [fault catalog](faults.md) lists
them. These numbers describe your machine with a mock model, [not provider
capacity](limits.md).

## 5. Run it in the workspace

<!-- quickstart:background http://127.0.0.1:8000/ -->
```bash
runweave serve --agent agent:run
```

Open <http://127.0.0.1:8000> in your browser:

1. **Agents** lists `word-count`, next to the built-in example agent `incident-triage`.
   Choose **New run** on `word-count`'s row.
2. In **text**, type `RunWeave tests agents`, then choose **Start run**.
3. The run detail shows each step on a timeline and the output: `{"words": 3, "longest":
   "RunWeave"}`.
4. **Evals** and **Load tests** start the same commands you just ran and list their saved
   reports.

Press Ctrl+C in the terminal to stop the server.

Without a browser, the same steps go through the API the workspace uses: `GET /v1/agents`,
then `POST /v1/runs` with `{"agent": "word-count", "input": {"text": "RunWeave tests
agents"}}` and `GET /v1/runs/{id}` ([submit and follow runs](agents.md#3-submit-and-follow-runs)).

## Next steps

- **Make it yours:** replace the body of `run` in `agent.py`, then add cases to
  `datasets/smoke.jsonl` ([test your agent](testing.md)).
- **Call a model:** point your OpenAI or Anthropic SDK at `ctx.proxy` and every call is
  metered, budgeted and mocked offline ([LLM proxy](llm-proxy.md)). Switch to your own key
  when you are ready ([bring your own key](budgets.md#bring-your-own-key)).
- **Use a framework or another language:** see the [examples](README.md#examples) and
  [HTTP agents](http-agents.md).
- **Your agent's own dependencies:** `uv tool install` gives RunWeave an environment of its
  own, so an agent that imports, say, `openai` needs it there too:
  `uv tool install runweave --with openai`. Or install `runweave` into your project's own
  virtual environment with `pip install runweave` (or `uv add --dev runweave`) and run it
  from there.
- **Something went wrong?** See [troubleshooting](troubleshooting.md).

## From source

To work on RunWeave itself, or to try unreleased changes, run it from a clone. You also need
Git and Node.js 22.12 or newer (or 24) to build the workspace UI once:

```bash
git clone https://github.com/iam-mkd/RunWeave.git
cd RunWeave
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
uv run runweave init my-agent
```

`uv sync` creates `.venv` with RunWeave and its dependencies; the two `npm` commands build
the workspace UI into `frontend/dist`, which `runweave serve` finds by itself. Recent npm
versions may warn that an install script for `esbuild` was blocked; the build does not need
it. Inside the clone, `runweave` is not on your PATH: prefix each command above with
`uv run`, as in `uv run runweave eval`. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the
checks a pull request runs.
