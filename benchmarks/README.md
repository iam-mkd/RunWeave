# RunWeave load and recovery report

This folder holds RunWeave's own release evidence: the built-in triage workflow under a
mocked health API. To load-test your own agent, use `runweave bench` (see
[Test your agent](../docs/testing.md)); both share the metric parsing in `runweave.bench`.

## Per-run CPU cost and the 90 runs/s ceiling (November 19, 2026)

`runweave bench` was stuck near 90 runs/s at every concurrency level, even when each
run spent 50 ms waiting. The cause was CPU, not waiting. One process and one core did all
the work, at about 11 ms of CPU per run. Most of that cost came from the load driver's
HTTP client, not from RunWeave. Three driver fixes and one server fix roughly doubled
in-process throughput and also helped `--server` runs (see the table below). Raw numbers
are in [bench-cpu-windows.json](bench-cpu-windows.json).

Environment: Windows 11 (10.0.26300), AMD64 with 16 logical CPUs, 15,791 MiB, Python
3.13.9, native Proactor loop; httpx 0.28.1, httpcore 1.0.9, uvicorn 0.54.0 (pure-Python h11,
no httptools), FastAPI 0.162.0. The workload is the `runweave init` word-count agent, either
unchanged (no I/O) or with `await asyncio.sleep(0.05)` in one step (the agent in
`scripts/measure-ui-overhead.py`, milestone 22). Before and after ran back to back on the
same machine. "Before" is commit 1cbb617. Each row shows the median of three rounds (two for
word-count).

| Workload | Before runs/s | After runs/s | p95 ms before / after |
| --- | ---: | ---: | --- |
| 50 ms agent, in-process, concurrency 64, 2,000 runs | 51.3 | 186.6 | 134.8 / 145.5 |
| 50 ms agent, in-process, concurrency 8, 1,000 runs | 86.1 | 125.0 | 66.4 / 58.1 |
| 50 ms agent, `--server`, concurrency 64, 2,000 runs | 127.1 | 305.2 | 83.8 / 80.0 |
| word-count, in-process, concurrency 1, 1,000 runs | 97.7 | 159.8 | 1.8 / 1.3 |
| word-count, in-process, concurrency 16, 1,000 runs | 100.2 | 179.6 | 11.6 / 10.9 |
| word-count, in-process, concurrency 64, 1,000 runs | 87.6 | 178.2 | 20.4 / 43.2 |
| `benchmarks/load.py` soak, 128 workers, 20 s | 143.0 | 147.1 | n/a |

CPU per run with `--server` (separate processes, concurrency 64, measured with psutil
process times) fell from 5.6 to 3.1 ms on the server and from 7.8 to 3.3 ms in the driver.
In-process, a single core runs all three: RunWeave, the agent and the driver. At
concurrency 64 that core is now saturated, so p95 latency and loop lag rise as throughput
doubles: word-count p95 went from 20 to 43 ms, and the loop-lag p99 bucket from 50 to
250 ms.

### What cost CPU per run

Measured with cProfile around `run_bench` (in-process) and in each process separately
(`--server`). cProfile slows small functions more than large ones, so the percentages
below are shares of profiled CPU, not wall time. Idle waiting in
`GetQueuedCompletionStatus` is excluded.

1. **A new TCP connection for every run (driver, fixed).** `Driver.wait` left the event
   stream as soon as it saw the terminal snapshot. httpx closes a response that is not
   read to the end, so the connection was dropped. A run of 1,000 jobs made 1,001
   loopback connects. The server already ends the stream right after the terminal
   snapshot, so the driver now reads it to the end. The same run then made 14 connects.
2. **A keep-alive cap of 20 in `--server` mode (driver, fixed).** The `--server` client
   used httpx's default limits, which keep only 20 idle connections. With 64 runs in
   flight, 2,050 runs made 2,877 connects in the driver and 4,134 accepts on the server.
   Both modes now keep `workers + 8` connections.
3. **The connection-pool scan in httpcore (driver, fixed).** On every request, httpcore
   walks every connection in the pool, and each idle connection's expiry check calls
   `select()`. With one pool of 72 connections, that scan was 39% of the driver's CPU,
   including 19% in `has_expired`. The driver now gives each block of 8 run slots its own
   client (`SHARD_SLOTS`), so each scan covers about 10 connections.
4. **Admission scanned the whole run history (server, fixed).**
   `RunRegistry.unfinished_for_tenant` counted unfinished runs by walking every retained
   run on every submission. That took 1.56 million generator steps for 2,050 runs, 9% of
   server CPU with a history of 1,000, and the cost grew with the history size. A
   per-tenant counter now changes on create and finish. Only `finish` makes a run
   terminal, and pruning removes only terminal runs, so the count stays exact.

After these fixes, the server's remaining cost is mostly the HTTP stack.

| Server share of profiled CPU (concurrency 64) | % |
| --- | ---: |
| ASGI app: FastAPI, Starlette and RunWeave handlers, including the next three rows | 44 |
| FastAPI dependency resolution | 8 |
| Pydantic snapshot JSON (`to_json`, 12.6k calls) | 2.8 |
| `registry.get` copying the snapshot to refresh usage | 3.5 |
| Run execution (`_execute`: agent, step records, budget) | 10 |
| uvicorn h11 event handling | 10 |
| Rest: asyncio Proactor scheduling, `WSASend`/`AcceptEx`, socket objects | about 35 |
| Audit records (`telemetry.audit`) | 0.1 |
| Content-policy checks (10.5k checks on inputs and stream frames) | 0.3 |
| Run telemetry (`telemetry.finished`) | 0.2 |

Audit, the content policy and telemetry are cheap, so none of them was changed. The
driver's remaining 3.3 ms per run goes to httpx, httpcore and anyio, for one submission
and one event stream per run.

### Not changed: recommendations

- **Separate processes for more throughput.** In-process, all per-run CPU (about 6.4 ms)
  runs on one core, so the ceiling is roughly 155–190 runs/s. `--server` splits it across
  two processes. There, the server's 3.1 ms per run sets the ceiling at about 320 runs/s,
  and the measured 305 runs/s is close to it. A driver that spreads load over several
  processes would help only once the server is no longer the limit.
- **Less HTTP work on the server.** Three options, none measured here: installing
  httptools, which uvicorn uses instead of h11 when present (a dependency decision);
  fewer requests per run, such as submitting and streaming in one request or one event
  stream for many runs (an API change); and skipping the snapshot copy in `registry.get`
  when usage has not changed (about 3.5%).
- **More than one Uvicorn process needs shared state.** The in-memory MVP keeps one
  process (see AGENTS.md), so multiple processes would need the durable backend.
- `benchmarks/load.py` has its own polling driver, and its sweep is bounded by its waves
  rather than by CPU: 128 runs with 100 workers and at least 0.4 s of mocked tool waits
  each make two waves of about 0.6 s. Its milestone 10 figure of 95 runs/s therefore
  does not show a per-run CPU ceiling. Its soak gained only from fix 4 (143 to 147 runs/s).

These are loopback measurements with mocked or simulated I/O on one Windows machine. They
show RunWeave's and the driver's own overhead, not live-provider capacity. To reproduce in
a `runweave init` project with the 50 ms agent:

```powershell
runweave bench --concurrency 64 --runs 2000
python -m cProfile -o bench.prof -m runweave.cli bench --concurrency 64 --runs 2000
python -m runweave.cli serve --api-only --port 8000 --agent agent:run   # second terminal
runweave bench --server http://127.0.0.1:8000 --concurrency 64 --runs 2000
```

## Milestone 16 verification on November 18, 2026

The latest native Windows sweep and ten-minute soak are recorded in
[sweep-m16-windows.json](sweep-m16-windows.json) and
[soak-m16-windows.json](soak-m16-windows.json). The 100-worker case completed 128
runs in 1.366 s, versus 55.604 s sequentially, and reached 100 overlapping runs
and tool steps. Status p95 was 2.679 ms; loop-lag p99 fell in the bucket ending
at 250 ms. The soak completed 56,960 runs in 601.333 s, retained 128 records,
and ended at 83.45 MiB RSS (78.22 start, 84.54 peak). Status p95 was 2.239 ms;
the lag p99 bucket ended at 1,000 ms. The strict sub-50 ms target remains unmet.
These are co-resident ASGI-driver/mock-I/O results, with other local verification
activity during the soak; they do not establish live-network or provider capacity.
Native Uvicorn TCP and main-container startup/shutdown checks now pass separately.
See the [release guide](../docs/release.md) for commands, environment and evidence.

## Milestone 10 verification on November 15, 2026

Both workflows completed all 128 synthetic runs with 100 workers, reaching 100
overlapping runs and 100 tool steps. These were sequentially measured cases on
native Windows asyncio, Python 3.13.9, Windows 11, 16 logical CPUs, and 15,791 MiB
of memory, with one application process, queue capacity 128, and HTTP limit 128.
Both used the same fake model and two 200 ms synthetic tool waits. Reports now
record the effective budget policy. It allowed 20 model attempts, 100,000 input
tokens, 2,048 output tokens, 16,384 context tokens, 2,048 evidence bytes per item,
and USD 0.05 estimated spend. Fake prices were zero.

| Metric | Baseline | PydanticAI |
| --- | ---: | ---: |
| Duration (s) | 1.342 | 4.697 |
| Throughput (runs/s) | 95.41 | 27.25 |
| Completion p50 / p95 / p99 (ms) | 634.81 / 939.41 / 978.26 | 2536.81 / 3392.45 / 3399.70 |
| Queue wait p50 / p95 / p99 (ms) | 141.17 / 496.49 / 506.24 | 145.25 / 2423.23 / 2430.67 |
| Status API p50 / p95 / p99 (ms) | 1.68 / 1.94 / 1.94 | 1.56 / 2.04 / 2.11 |
| Status probes | 15 | 45 |
| Event-loop lag p99 bucket upper bound (ms) | 100 | 250 |
| RSS start / peak / end (MiB) | 78.00 / 81.70 / 81.75 | 77.76 / 119.14 / 116.95 |

Each case retained 128 records. Both failure matrices matched the expected
rate-limit, transient, disconnect, forbidden, malformed, slow, and cancellation
outcomes, with three health-tool retries. Both cases missed the strict sub-50 ms
lag target. Workflow performance parity remains open. This verification used mocked I/O.

The generated reports are `target/evals/m10-baseline-load.json` and
`target/evals/m10-pydanticai-load.json`. Reproduce each case separately with the
appropriate workflow selected.

```powershell
$env:RUNWEAVE_AGENT_WORKFLOW = 'baseline'
uv run --no-sync python -m benchmarks.load --workers 100 --output target/evals/m10-baseline-load.json
$env:RUNWEAVE_AGENT_WORKFLOW = 'pydanticai'
uv run --no-sync python -m benchmarks.load --workers 100 --output target/evals/m10-pydanticai-load.json
```

## Earlier sandbox measurements on November 13, 2026

Measured on November 13, 2026 with Python 3.13.9 on Windows 11, 16 logical CPUs,
and 15,791 MiB of memory. One process ran the driver and ASGI app. The sandbox
hung creating the native Windows wakeup socket pair, so the measurement used
the Proactor loop with wakeup sockets disabled in a temporary harness. The
benchmark uses in-process HTTPX transports, which measure application behavior
without TCP, Uvicorn, or a live model provider. Console traces and JSON logs
were disabled for the measurements.

Run the same workload locally with the default asyncio loop:

```powershell
uv sync --locked
uv run --locked python -m benchmarks.load --output sweep.json
uv run --locked python -m benchmarks.load --workers 128 --soak-seconds 600 --output soak.json
```

The sweep submits 128 identical incidents per case. Each run makes three fake
model decisions and two tool calls with approximately 200 ms of asynchronous
delay each. Queue capacity and terminal history are both 128. The configured
HTTP connection limit is 128, though mock transports bypass sockets and the
connection pool. Throughput includes submission, work, and result collection.
The p99 lag column is the upper bound of the observed histogram bucket.

| Workers | Peak runs / tools | Elapsed (s) | Runs/s | Queue p95 (ms) | Completion p95 (ms) | Status p95 (ms) | Loop lag p99 upper (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 / 1 | 55.13 | 2.32 | 51,674 | 52,092 | 2.93 | 25 |
| 10 | 10 / 10 | 6.01 | 21.31 | 5,104 | 5,532 | 2.97 | 25 |
| 50 | 50 / 50 | 1.90 | 67.45 | 935 | 1,367 | 3.60 | 50 |
| 100 | 100 / 100 | 1.43 | 89.34 | 484 | 917 | 2.83 | 100 |
| 128 | 128 / 128 | 1.04 | 123.14 | 254 | 689 | 2.88 | 100 |

Every run succeeded. The high-concurrency cases reached 100 and 128 overlapping runs
and stayed within their configured limits. The status p95 stayed below the
100 ms target, with only nine probes in the 128-worker case. The 100 ms loop-lag
bucket at 100 and 128 workers leaves the strict sub-50 ms target unverified.
Peak RSS across the sweep was 61.11 MiB. Full p50/p95/p99 and sample counts are
in [results-windows-asgi.json](results-windows-asgi.json).

The 128-worker soak completed **76,928 runs in 600.823 seconds** at 128.04 runs
per second. RSS moved from 57.74 MiB to 63.02 MiB, with samples leveling off
around 63 MiB. The registry retained 128 records after 601 batches. Status p95
was 2.73 ms across 5,373 probes, and completion p95 was 666.37 ms across the
last 8,192 runs. The soak's loop-lag p99 fell in the 0.5–1 second bucket,
so it missed the sub-50 ms target in this harness. Full measurements and the
30-second RSS samples are in [soak-windows-asgi.json](soak-windows-asgi.json).

| Injected fault | Observed outcome |
| --- | --- |
| HTTP 429, HTTP 503, or simulated read disconnect | Success after one retry each |
| HTTP 403 | Failure without retry |
| Malformed health response | Validation failure without retry |
| Slow health response | Step timeout |
| Cancel during a health lookup | Cancelled |

The test suite also exercises malformed model tool requests, queue saturation,
deadline handling, cancellation races, and shutdown with active work. The native
Windows socket smoke test and container build/run remain open. The offline Python
wheel built successfully. The Docker daemon was unavailable in the sandbox.
