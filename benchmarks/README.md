# RunWeave load and recovery report

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
