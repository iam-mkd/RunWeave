# Limits: what RunWeave does and does not tell you

RunWeave measures your agent honestly within these limits. Read them before you quote a
number.

## A mock benchmark is not provider capacity

`runweave bench` with the mock model measures **your agent and RunWeave on your machine**:
how they schedule, queue and recover when the model answers instantly and deterministically.
It is not provider capacity. Real providers cap requests and tokens per minute, per
account and per model, often far below what the mock sustains, and their latency varies with
load. 100 runs a second against the mock does not mean 100 runs a second against a
provider.

- **Accepted is not running.** RunWeave accepting 100 runs (HTTP 202) does not mean 100 were
  executing at once; the queue holds what the workers cannot take yet. Bench reports queue
  wait separately for that reason.
- **In-process numbers share one process.** By default RunWeave, a Python agent and the load
  driver share one process and event loop, so a CPU-heavy agent slows the driver too.
  `--server` moves the work to a separate `runweave serve`; see [test your
  agent](testing.md#6-load-test-runweave-bench).
- **Faults are a model of failure, not a forecast.** Fault profiles show how your agent
  reacts to a given rate of errors; they do not predict how often a provider fails.
- **Live numbers need live calls.** In passthrough mode bench measures real provider
  traffic and spends real money within your budgets. Keep those results apart from mock
  results, and record the provider, model and date with them.

## Your agent is trusted code

RunWeave bounds and observes your agent but does not sandbox it. A Python agent runs in the
RunWeave process with its permissions; an HTTP agent runs with its own. Run only code you
trust. Disposable containers exist for specific tools, not for agents ([isolation](isolation.md)).

## In-memory mode forgets; durable mode repeats

- **In memory (the default),** runs and their timelines are lost when the server stops.
  Report files under `.runweave/reports` stay.
- **In durable mode** (PostgreSQL), a run whose worker dies is run again from the start after
  its lease expires: at least once, never exactly once. Make side effects idempotent, or use
  approvals and checkpoints around them ([agents](agents.md#limits-and-guarantees)).

## Local first

RunWeave listens only on `127.0.0.1` and is verified for local development and CI. It is not
a hosted service: before exposing it to a network you need real authentication, TLS, rate
limits at the edge and more. See the [public deployment
gate](architecture.md#public-deployment-gate).

## Other boundaries

- **Mock replies ignore the model name** and estimate tokens at four bytes each; costs in mock
  mode are zero ([LLM proxy](llm-proxy.md#limits)).
- **Costs are estimates** from the prices you configure, not your provider's bill
  ([budgets](budgets.md)).
- **HTTP agents' timelines arrive at the end** of a run, not live, in this contract version
  ([HTTP agents](http-agents.md#limits-and-guarantees)).
- **The desktop app is unsigned** and built for Apple silicon, x64 Windows and x64 Linux
  ([desktop app](desktop.md#limits)).
