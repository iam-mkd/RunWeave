# Test your agent: `runweave init` and `runweave eval`

`runweave eval` runs a dataset of cases through your agent and decides pass or fail, offline
and with no API key: model calls through the [LLM proxy](llm-proxy.md) get deterministic
mock replies unless you switch to passthrough. It writes JSON, JUnit XML and a readable
report, and exits non-zero when a gate fails, so it fits straight into CI.

## 1. Create a project

```bash
runweave init my-agent                         # Python
runweave init my-agent --language typescript   # TypeScript (Node 22.12+ or 24+)
cd my-agent
runweave eval
```

| File | What it is |
| --- | --- |
| `runweave.toml` | The project: agent, limits, proxy and fault settings, datasets, gates |
| `agent.py` / `agent.mjs` | A small word-count agent to replace with yours |
| `datasets/smoke.jsonl` | Four sample cases, including an expected rejection |
| `vendor/runweave-agent` | TypeScript only: the RunWeave SDK (not on npm yet); run `npm install` once |

`init` refuses to overwrite existing files unless you pass `--force`.

## 2. `runweave.toml`

```toml
[agent]
name = "word-count"            # the agent to test
python = "agent:run"           # a Python @agent, imported from this folder
# http = "http://127.0.0.1:9200"   # or an HTTP agent that is already running
# command = ["node", "agent.mjs"]  # or one RunWeave starts and stops around each command

[limits]
run_timeout_seconds = 30
concurrency = 4                # cases in flight at once

[proxy]
mode = "mock"
fault_profile = ""             # e.g. "flaky" or "chaos" (see docs/llm-proxy.md)
fault_seed = 0
# mock_fixtures = "fixtures.json"

[[datasets]]
name = "smoke"
path = "datasets/smoke.jsonl"  # .jsonl, .yaml or .yml

[eval]
min_pass_rate = 1.0
# baseline = ".runweave/baselines/smoke.json"
# reports = ".runweave/reports"
```

Commands find `runweave.toml` in the current folder or a parent (or take `--config`). All
paths stay inside the project folder. A `command` agent must print
`RUNWEAVE_AGENT_READY <url>` once it listens, as `serveAgents` and `runweave serve-agent`
do.

## 3. Cases

One JSON object per line (`.jsonl`), or a YAML list:

```json
{"id": "finds-longest", "input": {"text": "a wonderful day"}, "expect": {"contains": {"longest": "wonderful"}}, "tags": ["smoke"]}
```

`id` defaults to `case-<line>`; `input` must match your agent's input schema (or expect
`"status": "REJECTED"`). Assertions under `expect`:

| Assertion | Passes when |
| --- | --- |
| `status` | The run ends `SUCCEEDED` (default), `FAILED`, `TIMED_OUT`, `CANCELLED`, or is `REJECTED` at admission |
| `error_code` | The run failed with this code (implies `status: FAILED`) |
| `schema` | The output matches the agent's output schema; on by default for successful runs |
| `output` | The output contains these keys and values (nested objects match as subsets) |
| `output_equals` | The output equals this value exactly |
| `contains` | `{"path.to.field": "text"}`: the field contains the text |
| `matches` | `{"path.to.field": "regex"}`: the field matches the regular expression |
| `tool_called` | Each name is a timeline step title (record tool use with `ctx.step("lookup_weather")`) |
| `not_contains` | None of these strings (canaries) appear in the output or timeline |
| `max_latency_ms`, `max_cost_usd`, `max_tokens`, `max_model_calls` | The run stays within the ceiling |
| `python` | `["checks:my_check"]`: your function `(case, result)` returns `True`, `(False, "why")`, or raises |
| `pydantic_evals` | `[{"evaluator": "EqualsExpected"}]`: a Pydantic Evals evaluator (built-in name or `module:Class`, with `args`); needs `runweave[eval]` |

Custom checks are imported from the project folder; they are your trusted code.

## 4. Reports, gates and baselines

Each run writes `.runweave/reports/eval-<time>-<id>/` with `report.json` (the full result,
read back by the workspace), `junit.xml` and `report.md`. `--junit path` writes another
JUnit copy where CI expects it. The JSON records the agent and version, dataset digest,
machine and Python version, settings, per-case assertions, latency (p50/p95), tokens and
estimated cost.

Gates:

- the pass rate must reach `min_pass_rate`;
- with a baseline (`--baseline report.json`, or `[eval] baseline`), no case that passed in
  the baseline may fail now. `runweave eval --save-baseline` stores the current report as
  the dataset's baseline.

Exit codes: `0` gates passed, `1` a gate failed, `2` a project, dataset or connection
problem (with a message, never a traceback).

## 5. In-process or against a server

By default the command starts RunWeave inside itself (in memory, offline, mock proxy), so
CI needs nothing else. `--server http://127.0.0.1:8000` submits the cases to a running
`runweave serve` instead; the agent must be registered there (an `http`/`command` agent is
registered for you). Both paths use the same API, so results match. `runweave serve
--api-only` serves the API without the workspace UI.
