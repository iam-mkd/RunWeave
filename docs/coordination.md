# Typed multi-agent coordination

Milestone 14 adds an opt-in `coordinated` workflow. It has a fixed coordinator,
one health specialist, and one runbook specialist. Each specialist makes one
bounded model proposal, executes its single scoped read, and returns a typed
analysis linked to the observed evidence. The coordinator makes one merge
request and must cite both current evidence IDs. It cannot dispatch another tool.
The baseline and Pydantic Graph workflows remain available for comparison.

```dotenv
RUNWEAVE_AGENT_WORKFLOW=coordinated
RUNWEAVE_COORDINATION_CONCURRENCY=2
```

Run submission is unchanged. The admitted principal needs `agents:delegate`,
`tools:check_health`, and `tools:lookup_runbook`, in addition to ordinary run API
scopes. Missing authority fails before any model/tool handoff. The local developer
principal includes these scopes; existing persisted principals and JWTs are not
granted new authority automatically. No additional dependencies or database
migration are required beyond the current schema head, migration 0006.
Stop older workers and upgrade all replicas before enabling this workflow.
Older binaries cannot decode its branch state or workflow identity.

## Contracts and merge policy

`HandoffRequest` binds run/role correlation ID, exact incident service, one of
two closed roles, and a two-step maximum. `HandoffOutput` includes that request,
the exact typed tool exchange, and a typed health observation or bounded runbook
excerpt. Argument service, tool name, evidence size/identity, and derived analysis
are validated before committing results. A specialist receives only its own tool
definition and the corresponding permission intersected with the parent's scopes.
It receives no delegation, approval, ticket, or memory authority. Parent memory
selection and ticket flags are removed from specialist inputs.

The actor prompts are fixed and versioned. Incident and retrieved text cannot
change roles, permissions, service arguments, or the number of handoffs. Returning
another role's tool, a shell call, a recursive delegate, or an early final answer
fails the branch. Nested coordinator execution is also rejected. The coordinator
receives both validated exchanges in health/runbook order and only the finish
schema. Final evidence validation remains at the coordinator before success.

Both branches are required. A classified branch failure retains the sibling's
successful checkpoint, records a sanitized failure code, and fails the job without
inventing a partial incident result. The sibling can finish its bounded read;
there is no repeated delegation after failure. Cancellation and unexpected
checkpoint failures cancel/await owned tasks through `asyncio.TaskGroup`.
Model/tool exceptions are sanitized in API snapshots and stored branch failures.

## Budgets, deadlines, and traces

The specialists inherit the parent's budget ledger, deadline, and trace context;
they do not create independent token/cost allowances or worker jobs. A successful
run needs at least five execution steps: two proposals, two reads, and one merge.
It normally uses three model requests, matching the baseline's request count.
Provider calls use the existing shared client and provider concurrency/rate limits.

With concurrency two, independent branches overlap when the remaining output,
input, request, and estimated-cost allowances permit their reservations. Otherwise
the workflow uses a sequential plan, allowing known unused reservations to settle
before the next proposal. Setting concurrency to one always selects sequential
handoffs. Atomic ledger updates enforce the aggregate bound even under concurrent
execution; a later reservation can still fail if actual usage consumes the budget.
The overall deadline includes queue wait, every branch, retry, and merge. Each
model/read also has a step deadline. Parent cancellation propagates to both
specialists and awaits cleanup. Unknown inference usage retains its original charge.

There are at most two specialists per active run. They are tasks within the
existing worker job, so a deployment with N active jobs can have up to 2N specialist
tasks. Each specialist has one tool interaction and cannot spawn more agents.
Spans `agent.specialist.health`, `agent.specialist.runbook`, and `agent.coordinator`
inherit the run trace and carry its run ID. Audit events record dispatch plan and
branch outcome without raw prompts or payloads.

## Durable replay and feature integration

Each proposal is checkpointed before its read, and each validated specialist
output is committed independently. Branch correlation, pending call, stage,
analysis/evidence, and sanitized failures live in the versioned run checkpoint.
Two bounded writers use the existing lease-fenced compare-and-swap sequence and
retry only real sequence conflicts, up to five attempts. No application state lock
is held across a model/tool/network call. Parent usage snapshots remain atomic
with checkpoint writes. Read receipts cover a crash between tool result commit
and branch checkpoint commit.

Recovery validates run/role correlation, current authority, workflow identity,
and content policy. It reuses committed proposals and results, executes remaining
work, and merges once. A committed final decision resumes without more inference;
completed runs do not repeat effects. A crash before a model decision is committed
can repeat inference and cost; this is not an exactly-once provider guarantee.
Unsupported or inconsistent checkpoint state fails safely.

The identity is `coordinated-v1`, with pinned coordinator/specialist prompt hashes
and a separate memory-enabled prompt identity. Default changes do not reset saved
step/token allowances. Ordinary baseline/Pydantic checkpoints remain supported.
New branch fields are additive to the current checkpoint decoder.

Optional scoped recall is available only to the coordinator's merge request,
through the existing bounded, dispatch-revalidated memory path. Specialists cannot
read or write memory. Optional sandbox ticket creation still uses the existing
durable approval gate after final evidence validation. Approval resumes from the
saved final decision and does not repeat branches or merge inference. Successful
coordinated evidence can be explicitly saved through the memory API with its
workflow provenance.

## Verification and comparison

```powershell
uv run --no-sync pytest tests/test_coordination.py -q
.\scripts\verify-postgres.ps1
uv run --no-sync python -m runweave.evaluation.suite --workflow coordinated --output target/evals/coordinated.json
uv run --no-sync python -m runweave.evaluation.coordination --output target/evals/coordination-comparison.json
```

The unit suite covers actual overlap, closed handoff contracts, partial failure,
shared budget exhaustion, small-budget fallback, parent deadline/cancellation,
trace parentage, invalid correlation, and unauthorized/recursive delegation. The
PostgreSQL suite terminates actual worker processes at committed proposals,
each specialist completion, merge decision, and done. It verifies retained charges,
reuse across changed defaults, atomic branch writes, stale/cancelled fencing, and
the complete memory → coordination → ticket approval flow.

The coordinated evaluator uses the same 13 reviewed fixtures and 68 assertions as
the single-agent baseline. Allowed executions are compared in canonical tool order
because concurrent completion order is unspecified. The paired comparison records
healthy/degraded quality checks, usage, estimated cost, host/configuration, source
hashes, and illustrative latency with the same 20 ms mock-tool delay.

The checked-in comparison uses synthetic $1/$2-per-million input/output rates and
a conservative UTF-8-byte input estimate, not provider billing. Both paths make
three fake requests and preserve the expected severity/evidence. Coordinated input
reservations are smaller because specialists see one schema and the coordinator
sees only finish. Timing has only two samples per path and includes Python overhead;
it establishes neither live-model quality nor production capacity. See
`evaluations/coordination-comparison.json` for the recorded values and limits.
