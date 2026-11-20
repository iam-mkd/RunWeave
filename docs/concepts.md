# Concepts

RunWeave runs your agents many at a time, with limits, and tells you how they behaved:
whether they gave the right answers, how fast they were under load, and how they coped
with a failing model provider. This page names the parts. The [quickstart](quickstart.md)
shows them working.

## Runs and steps

A **run** is one execution of an agent with one input. It is tracked on its own, from
`QUEUED` through `RUNNING` to exactly one end state: `SUCCEEDED`, `FAILED`, `TIMED_OUT` or
`CANCELLED`. An end state is never overwritten, even by a late result.

A **step** is one thing a run did: a model call, a tool call, or a block you named with
`ctx.step("...")`. Steps make up the run's timeline in the workspace and in `GET
/v1/runs/{id}`.

A run is not a process. One RunWeave process runs many runs concurrently on one asyncio
event loop; the limits below keep that bounded.

## Limits

RunWeave admits a run only if there is room. With the queue full, `POST /v1/runs` answers
`503 QUEUE_FULL` with `Retry-After` instead of accepting work it cannot hold. Each run has
a deadline that includes its time in the queue, a step limit, and budgets for model calls,
tokens and estimated cost. Concurrency, queue depth, connections, payload sizes and
retained history are all capped. See [configuration](configuration.md) for every limit.

## Agents and how they connect

An **agent** is your code. RunWeave reaches it in one of three ways:

| Boundary | What you write | Guide |
| --- | --- | --- |
| Python | An `async def` decorated with `@runweave.sdk.agent`, loaded into RunWeave | [Python agents](agents.md) |
| HTTP | Any program, in any language, serving the `runweave.agent/v1` contract | [HTTP agents](http-agents.md) |
| LLM proxy | Your model SDK pointed at `ctx.proxy` instead of the provider | [LLM proxy](llm-proxy.md) |

The first two say how RunWeave calls your agent; the third is how your agent calls its
model. Most agents use the proxy with one of the other two. Your agent is trusted local
code: RunWeave bounds and observes it, but does not sandbox it.

## The LLM proxy and mock mode

Every run gets its own proxy token. Model calls made with it are checked against the
run's budget, recorded as steps, and answered. In **mock mode** (the default) the answer is
deterministic and offline: the same request gives the same reply, and a fixtures file can
script replies and tool calls. In **passthrough mode** calls go to OpenAI or Anthropic
with your key, which stays inside RunWeave. **Fault profiles** make the proxy answer some
calls with rate limits, errors, timeouts or broken tool calls, from a seed, so a failure
can be reproduced. See the [fault catalog](faults.md).

## Projects, datasets and gates

A **project** is a folder with `runweave.toml`: which agent to test, its limits, proxy
settings, datasets and gates. `runweave init` creates one.

A **dataset** is a list of **cases**: an input and what to expect, such as an exact output,
a field that contains a value, a tool that must be called, or a latency limit.

A **gate** turns results into pass or fail: a minimum pass rate, no regressions against a
saved **baseline**, a maximum failure rate or p95 latency under load, or the promotion
rules of a shadow comparison. A failed gate makes the command exit with code 1, which is
what fails a CI job.

## The three testing commands

| Command | Question it answers | Report |
| --- | --- | --- |
| `runweave eval` | Does the agent give the right answers? | `eval-*` |
| `runweave bench` | How does it behave under load and injected faults? | `bench-*` |
| `runweave shadow` | Is a new version at least as good as the current one? | `shadow-*` |

Each writes a report folder under `.runweave/reports/` with `report.json`, `report.md` and,
for `eval`, `junit.xml`. See [reading reports](reports.md) and [test your
agent](testing.md).

## The workspace

`runweave serve` starts the API and a local web UI: agents, runs with live timelines,
the testing screens, approvals and settings. It listens only on `127.0.0.1`. See the
[workspace tour](workspace.md#a-tour-of-the-workspace).

## Approvals

Before an action with consequences, an agent can ask a person:
`await ctx.request_approval("issue_refund", {...})`. The run waits, durably, until someone
approves or rejects those exact arguments in the workspace or with `runweave approvals`.
See [approvals for your agents](agent-approvals.md).

## In-memory and durable mode

By default everything is in memory: fast and simple, but runs do not survive a restart.
**Durable mode** stores runs, steps, approvals, checkpoints and memory in PostgreSQL, so
work resumes after a crash. Approvals need durable mode. See
[architecture](architecture.md) and [Docker Compose](../README.md#docker).
