# RunWeave load and recovery report

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
