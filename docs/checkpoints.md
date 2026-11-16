# Durable workflow checkpoints

Milestone 11 keeps the PostgreSQL lease scheduler as the sole execution owner.
Both the baseline loop and Pydantic Graph use the same typed triage boundaries.
PydanticAI's supported [durable execution integrations](https://ai.pydantic.dev/durable_execution/)
were evaluated: introducing another workflow runtime would require transferring
scheduling, retry, and replay ownership. The existing fenced scheduler already
provides those responsibilities, so this milestone adds no execution runtime.

## State and replay

Migration `0004_workflow_checkpoints.sql` adds a JSON checkpoint and monotonic
sequence to each run, plus an operation journal. Admission saves sequence zero.
Schema `triage-checkpoint-v1` stores the incident, next node, pending typed model
decision, evidence history, remaining steps/output tokens, and usage snapshot.
Identity pins workflow kind, `triage-v1`, `triage-prompt-v1`, system-prompt SHA-256,
`triage-tools-v1`, and model. Change these versions when their semantics change.

Each model decision commits the next collect/finish node before execution. Each
collected result commits the next decide node. Validated completion commits done.
A normal two-tool run reaches sequence six. Checkpoint writes lock the run and
compare the expected sequence under the current lease token, deadline, and status.
Cancellation, expired leases, and competing writers cannot overwrite committed state.
The usage snapshot is copied from the authoritative ledger in that transaction.
Replay never resets the ledger: an inference interrupted before its decision is
committed can run again and consume another reservation, including uncertain cost.

Recovery resumes the saved node with its original step/token allowances even if
deployment defaults change. The current workflow/prompt/model must match the
saved identity. Unsupported schemas fail the affected run with
`CHECKPOINT_VERSION_UNSUPPORTED`; malformed or incompatible state fails closed.
Other jobs continue. Supporting a new format requires an explicit decoder or
migration; this release supports v1. Custom executors must implement these hooks
to gain node replay. In-memory execution retains its existing process lifetime.

Tool permission and argument validation runs again before receipt reuse. Restored
history and cached results pass the current credential/content policy before
being forwarded. A read receipt committed before a crash is reused; an incomplete
read may repeat. A final done checkpoint completes without repeating inference.

## External effects

The internal `EffectRunner` provides a boundary for explicitly wired adapters.
Model-proposed triage tools remain scoped reads. Milestone 12 adds an explicitly
requested [sandbox ticket approval gate](approvals.md) after validated triage.
Controlled database fixtures also verify the general effect protocol.

The stable operation key hashes run ID, kind, and tool-call ID. A separate digest
binds the tool name and canonical arguments. Reusing a call ID with different
arguments fails. Intent is committed before execution and evidence after confirmed
completion. A receipt committed before the workflow checkpoint is reused.

| Recovery observation | Action |
| --- | --- |
| Journal receipt committed | Return saved evidence |
| Downstream confirms committed | Commit and return confirmed evidence |
| Downstream confirms absent and supports real idempotency | Retry with the same key |
| Outcome unknown, or absence without idempotency | Mark needs_resolution and fail safely |

Timeouts and ordinary adapter failures trigger reconciliation. Cancellation
propagates and leaves intent available for recovery. Adapters must use the key
as downstream deduplication identity and provide authoritative confirmation.
This does not promise exactly-once effects. Unknown outcomes fail with
`EFFECT_RECONCILIATION_REQUIRED`. Operators must inspect the journal and downstream
receipt before resolving the incident; resubmitting under a new run ID could
duplicate an effect. This release provides no public manual-resolution endpoint.

The journal is capped at 40 operations per run. Evidence sizes are bounded and
entries are deleted with retained run history. Checkpoints and receipts are
internal database data and are not added to public snapshots or telemetry.

## Upgrade and verification

Stop existing workers before applying migration 0004, then start upgraded workers.
An unfinished pre-checkpoint run has no safe node to resume: migration marks it
failed with `CHECKPOINT_UNAVAILABLE` and closes its active attempt. Its budget
remains retained. Existing terminal history is preserved.

```powershell
uv run --no-sync pytest tests/test_checkpoints.py -q
.\scripts\verify-postgres.ps1
```

The native PostgreSQL suite terminates actual worker processes at nine boundaries
for each workflow, including before checkpoint commit, after decisions/evidence,
before an effect, after its downstream commit, after receipt commit, and after
done. It checks supported resume with changed defaults, retained charges,
idempotent effects, stale leases, cancellation fencing, concurrent checkpoint
writes, unknown versions, manual resolution, and legacy migrations. Local fake
models and controlled effects require no provider credentials.
