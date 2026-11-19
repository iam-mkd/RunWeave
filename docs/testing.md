# Test your agent: `runweave init`, `eval`, `bench` and `shadow`

`runweave eval` runs a dataset of cases through your agent and decides pass or fail, offline
and with no API key: model calls through the [LLM proxy](llm-proxy.md) get deterministic
mock replies unless you switch to passthrough. It writes JSON, JUnit XML and a readable
report, and exits non-zero when a gate fails, so it fits straight into CI. `runweave bench`
load-tests the same agent, and `runweave shadow` compares a new version with it.

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

[bench]                        # runweave bench (all optional)
concurrency = [1, 4, 16]       # levels to sweep
runs = 32                      # runs per level
# fault_profiles = ["", "flaky"]   # default: [proxy] fault_profile
# soak_seconds = 600
# max_failure_rate = 0.01      # gates; exit 1 when exceeded
# max_p95_latency_ms = 2000

[shadow]                       # runweave shadow
# candidate = { python = "agent_v2:run" }   # or { command = [...] } / { http = "..." }
max_cost_ratio = 1.05
max_latency_ratio = 2.0
latency_slack_ms = 50
require_output_parity = false
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

By default the command starts RunWeave inside itself (in memory, offline, mock proxy,
listening on a free loopback port so your agent reaches the proxy over a real socket), so
CI needs nothing else. `--server http://127.0.0.1:8000` submits the cases to a running
`runweave serve` instead; the agent must be registered there (an `http`/`command` agent is
registered for you). Both paths use the same API, so results match. `runweave serve
--api-only` serves the API without the workspace UI.

## 6. Load-test: `runweave bench`

```bash
runweave bench                                     # the [bench] sweep
runweave bench --concurrency 1 8 32 --runs 200     # a custom sweep
runweave bench --fault-profile "" --fault-profile chaos   # with and without faults
runweave bench --concurrency 32 --soak 600         # then ten minutes at the top level
```

Each fault profile gets its own RunWeave. Within it, each concurrency level submits `runs`
runs with at most that many in flight, using the dataset's inputs in turn (cases that
expect a rejection are left out). Fault profiles act on model calls through the
[LLM proxy](llm-proxy.md), so they matter only for agents that call a model.

`.runweave/reports/bench-<time>-<id>/report.json` and `report.md` record, per profile and
level:

- the machine (CPUs, memory, platform, Python), the agent and version, and the settings;
- duration and throughput, and run latency and queue wait at p50/p95/p99, from the
  server's own timestamps (the driver's polling does not inflate them);
- status-API latency, probed every 50 ms while the load runs, and the event-loop lag
  histogram bucket holding the window's p99;
- memory (RSS at the start, peak and end), peak active runs, and outcomes;
- failure categories such as `FAILED:AGENT_UNAVAILABLE` or `TIMED_OUT`, admission retries,
  tokens, estimated cost and model calls.

A soak adds windows over time (runs, throughput, p95, failures, RSS), which is where leaks
and slowdowns show. Set `max_failure_rate` or `max_p95_latency_ms` to make `bench` exit 1
in CI when a level exceeds them.

In-process, RunWeave, your agent (if it is Python) and the load driver share one process
and event loop, so a CPU-bound agent and the driver compete; the numbers describe this
machine with the mock proxy, not provider capacity. `--server` loads a running RunWeave
instead (memory is then not measured, and the server's own fault settings apply).

## 7. Compare versions: `runweave shadow`

```bash
runweave shadow --candidate agent_v2:run      # or [shadow] candidate in runweave.toml
```

The current `[agent]` and the candidate get the same cases at the same time, each in its own
RunWeave with its own budgets. For every case the report records both verdicts and the
paired differences (status, whether outputs are equal, latency, tokens, cost, model calls,
and assertions that newly fail). Outputs are compared, never stored.

The candidate is eligible for promotion only when every gate passes:

| Gate | Passes when |
| --- | --- |
| `candidate_pass_rate_met` | The candidate reaches `[eval] min_pass_rate` |
| `no_paired_regressions` | No case the current version passes fails on the candidate |
| `outcome_parity` | Every case ends in the same status (and the same output with `require_output_parity`) |
| `candidate_cost_within_limit` | Each case costs at most `max_cost_ratio` (105%) of the current one |
| `candidate_latency_within_limit` | Candidate p95 is at most `max_latency_ratio` (2x) the current p95 plus `latency_slack_ms` (50 ms) |

These are the milestone 16 shadow gates applied to your agent. `shadow` exits 0 when the
candidate is eligible and 1 when not; nothing is switched automatically. With `--server`,
the candidate must be an HTTP agent registered under its own `name`.
