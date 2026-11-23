# Reading reports

`runweave eval`, `bench` and `shadow` each write a folder under `.runweave/reports/` (or
`[eval] reports`), named `<kind>-<UTC time>-<id>`:

| File | For |
| --- | --- |
| `report.md` | Reading: a summary table and what failed |
| `report.json` | Everything, for tools and the workspace, which reads it back |
| `junit.xml` | `eval` only: one test case per dataset case, for CI test views |

The workspace's **Evals**, **Load tests** and **Shadow** screens list every saved report and
open it with charts. Order reports by `created_at` in `report.json` rather than by folder
name: two reports made in the same second sort by their random id.

## Eval

```text
word-count on smoke: 4/4 cases passed (100%), p95 2.8 ms, 0 tokens, $0
Gates passed.
```

`report.md` opens with the verdict, then the agent and version, whether RunWeave ran
in-process or against `--server`, p50/p95 latency, tokens and estimated cost. Below that, each
failed case lists the assertions that failed and why, for example `output: words is 4,
expected 3`.

In `report.json`:

- `summary`: totals, `pass_rate`, latency percentiles, tokens and cost.
- `cases`: one entry per case with `passed`, the run's `status` and `run_id` (open it in the
  workspace at `#/runs/<run_id>`), `latency_ms`, `tokens`, `cost_usd`, `model_calls` and every
  assertion with its `message`.
- `gates`: `passed`, and the `reasons` when not. The pass rate must reach `min_pass_rate`;
  with a baseline, any case that passed in the baseline and fails now fails the gates too,
  even when the pass rate is still above the minimum.
- `baseline`: the baseline compared with, and the case ids that `regressions`, `fixed`,
  `new` and `removed` list.
- `dataset` (with a SHA-256 digest of the file), `agent`, `environment` (machine, OS,
  Python) and `settings`: enough to tell whether two reports are comparable.

Exit codes: `0` gates passed, `1` a gate failed, `2` the project, dataset or connection is
wrong.

## Bench

```text
word-count 0.1.0 (in-process)
  faults none, concurrency 1: 164.5 runs/s, p95 1.2 ms, 0/32 failed
  faults none, concurrency 4: 173.85 runs/s, p95 2.9 ms, 0/32 failed
  faults none, concurrency 16: 151.12 runs/s, p95 9.9 ms, 0/32 failed
```

`report.md` has one row per fault profile and concurrency level:

| Column | Meaning |
| --- | --- |
| Runs/s | Completed runs per second over the level |
| p50 / p95 / p99 ms | Run latency from admission to the end state, from the server's own timestamps |
| Queue wait p95 ms | Time from admission to a worker picking the run up. Rising queue wait means the level is above what the agent or RunWeave can serve at once |
| Status API p95 ms | Latency of `GET /v1/runs/{id}`, probed every 50 ms during the load: does the API stay responsive? |
| Loop lag p99 ms | How late the event loop ran scheduled work. Tens of milliseconds means something is blocking it: often synchronous code in an agent |
| Peak RSS MiB | The process's peak memory (in-process only) |
| Failures | Failure categories, for example `FAILED:AGENT_UNAVAILABLE` or `TIMED_OUT` |
| Duplicate writes | Runs where a retry after a lost response wrote twice (see [effects](agents.md#effects-and-exactly-once-writes)). These runs often *succeed*, so this column, not Failures, is where they show. The workspace calls it **Wrote twice** |
| Tokens | Tokens through the proxy |

Read across a profile's rows. Throughput that stops rising while latency and queue wait
climb is the level where your agent saturates. Compare a fault profile's rows with the `none`
rows to see what the faults cost: more latency is expected; failures mean the agent did not
recover. A soak adds a table over time (runs, throughput, p95, failures, RSS); memory that
keeps growing there is a leak.

The note under the table says what the numbers describe. In-process, RunWeave, your agent
and the load driver share one process, with the mock model. That measures your agent and
RunWeave on this machine, [not provider capacity](limits.md).

## Shadow

`report.md` shows each gate with its result, then each case paired: both statuses, whether
outputs were equal, and the latency, token and cost differences. Assertions that pass on
the current version and fail on the candidate are listed first. The candidate is eligible
only if every gate passes; nothing is switched for you. See [compare
versions](testing.md#7-compare-versions-runweave-shadow) for the gates.
