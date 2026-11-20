# RunWeave documentation

RunWeave runs and tests your AI agents on your own computer: correctness with datasets and
gates, behavior under load, recovery from a failing model provider, and human approvals.
It works offline with no API key.

## Start here

1. [Quickstart](quickstart.md): create, evaluate, load-test and run an agent in five minutes.
2. [Concepts](concepts.md): runs, steps, agents, the proxy, datasets, gates and reports.
3. [Test your agent](testing.md): `runweave init`, `eval`, `bench` and `shadow` in depth.

## Connect your agent

- [Python agents](agents.md): the `@agent` decorator, steps and the run context.
- [HTTP agents](http-agents.md): the `runweave.agent/v1` contract for agents in any language,
  and the TypeScript SDK.
- [LLM proxy](llm-proxy.md): point your OpenAI or Anthropic SDK at RunWeave for metering,
  budgets, mock replies and faults.
- [Approvals for your agents](agent-approvals.md): `ctx.request_approval` before an action
  that matters.

## Examples

Each example is a complete project you can copy, tested offline in CI.

| Example | What it shows |
| --- | --- |
| [`examples/sample-agent`](../examples/sample-agent) | Plain Python agents with the RunWeave SDK |
| [`examples/http-agent-typescript`](../examples/http-agent-typescript) | A Node agent over the HTTP contract |
| [`examples/github-actions`](../examples/github-actions) | A CI job that fails a pull request when the agent regresses |

## Use RunWeave

- [Workspace](workspace.md): a tour of the local web UI, with screenshots.
- [Reading reports](reports.md): what eval, bench and shadow reports tell you.
- [Fault catalog](faults.md): the provider failures RunWeave can inject, and how to handle them.
- [Budgets](budgets.md): token and cost limits per run, and [bring your own
  key](budgets.md#bring-your-own-key).
- [Human approvals](approvals.md): approvals, waiting and resuming in durable mode.
- [Desktop app](desktop.md): RunWeave as an app for Windows, macOS and Linux.
- [Limits](limits.md): what the numbers mean, and what they don't.
- [Troubleshooting](troubleshooting.md): messages and their fixes.

## Reference

- [CLI](cli.md): every command and option.
- [Configuration](configuration.md): `runweave.toml` and every `RUNWEAVE_*` setting.

## How RunWeave works

- [Architecture](architecture.md): components, trust boundaries, durable mode and operations.
- [Security](security.md): authentication and execution policy.
- [Checkpoints](checkpoints.md), [memory](memory.md), [coordination](coordination.md) and
  [isolation](isolation.md): the durable workflow features.
- [Design system](design-system.md): the workspace's visual language.
- [Releases](release.md) and [licenses](licenses.md): how releases are verified and the
  dependency license audit.
