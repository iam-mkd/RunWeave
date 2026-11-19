# Human approvals and resumable waiting

This page covers the built-in triage agent's ticket approval. For your own agents, see
[Approvals for your agents](agent-approvals.md) (`ctx.request_approval`).

Milestone 12 adds one concrete gated action: creating a synthetic ticket in the
local PostgreSQL sandbox table. It uses PydanticAI's
[deferred tool approval](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)
with `requires_approval=True`, serialized message history, and
`DeferredToolResults` on resume. RunWeave remains the only scheduler/replay owner.
Both triage workflows use this boundary. No ticket-provider credentials or live
model calls are needed for approval handling: its FunctionModel makes one fixed
proposal and one fixed acknowledgement, with no model-selected argument changes.

## Enable and request a ticket

Install `requirements-durable.txt` and the pinned `workflow` extra, stop old
workers, and apply `alembic upgrade head` (including migration 0005). Deploy the
same version and authorization configuration on all replicas. Ordinary runs
retain their read-only triage behavior. Ticket requests require durable mode;
memory mode rejects them before admission with `DURABLE_APPROVAL_REQUIRED`.
Migration 0005 preserves existing jobs and scopes. Downgrade requires resolving
approval runs and restoring a backup; its Alembic downgrade fails explicitly.

```json
{
  "service": "payments",
  "description": "Synthetic latency incident",
  "create_ticket": true
}
```

Submit this incident to `POST /v1/runs` with `runs:submit` and
`tools:create_ticket`. Normal health/runbook scopes are still required. After
validated triage, the proposed ticket contains the incident's exact service,
the first 200 characters of the validated result summary, and its severity.
This deterministic policy is versioned as `sandbox-ticket-v1`.

The run enters `WAITING_APPROVAL`. The tool call, exact arguments, canonical
argument digest, policy, tenant, expiry, and PydanticAI message history are
persisted in the same transaction that closes the attempt and releases the lease.
No worker/task is retained while waiting, and restarts preserve the request.
The final triage decision remains checkpointed so resume needs no new inference.

## Review and decide

`GET /v1/runs/{run_id}/approval` returns the pending request to its tenant/subject
owner with `runs:read`. Review `call.arguments` before approving. Send the
returned bindings unchanged to `POST /v1/runs/{run_id}/approval`:

```json
{
  "approval_id": "<id from approval>",
  "call_id": "sandbox-ticket-v1",
  "argument_digest": "<argument_digest from approval>",
  "policy_version": "sandbox-ticket-v1",
  "expires_at": "<expires_at from approval>",
  "decision": "approve"
}
```

Use `reject` to deny. Decision requires `approvals:decide`, `runs:read`, and the
same tenant/subject owner. This release supports owner approval; cross-subject
reviewer delegation is outside its authorization model. Unknown/foreign requests
return 404, missing scopes 403, conflicting/stale bindings 409, and additional
fields (including argument overrides) 422. A decision is bound to the run path,
tenant, approval ID, tool-call ID, argument digest, policy version, and expiry.

Identical decisions are idempotent, including after completion. Opposing
decisions conflict. Run-row locks serialize decisions with cancellation and
effect execution. Approval requeues the saved run; rejection ends it as FAILED
with `APPROVAL_REJECTED`. Normal cancellation also works while waiting or queued
after approval. Expiry ends unfinished pending/approved runs as TIMED_OUT with
`APPROVAL_EXPIRED`. A repeated approval never revives a cancelled or expired run.

On execution, the gate rechecks current identity/expiry, tool scope, policy, and
exact arguments. It verifies the saved deferred messages before granting the
PydanticAI approval; no argument override is supplied. The sandbox insert itself
is conditional on the current lease, active deadline, approval status, and expiry.
The insert and approval's executed status commit atomically. Its stable operation
identity and downstream receipt support crash reconciliation without duplicate
tickets. `GET .../approval` then exposes status `executed` and its `ticket_id`.
Cancellation cannot undo an effect that already committed; the transaction
determines which operation won. The effect journal retains committed evidence.

## Limits, time, and authorization

| Setting | Default | Bound |
| --- | --- | --- |
| `RUNWEAVE_APPROVAL_TTL_SECONDS` | 3600 | 1–86400 seconds |
| `RUNWEAVE_APPROVAL_MAX_PENDING` | 100 | 1–10000 requests |
| `RUNWEAVE_AUTH_REVOKED_SUBJECTS` | `[]` | 1000 tenant/subject pairs |

Capacity counts pending and approved unfinished requests across replicas under
a database advisory lock. Exhaustion fails the affected run with
`APPROVAL_CAPACITY_EXCEEDED` before creating a request. Existing queue/history and
tenant limits also count waiting runs, bounding retained state. Each run has one
approval and one sandbox ticket; message history is limited to 64 KiB and arguments
are typed/bounded. Approval and ticket rows expire with retained run history.

Approval wait pauses the run's original active deadline. The decision adds exactly
that waiting interval to the saved deadline; queue wait and recovery otherwise
continue consuming it. Model request/token/cost reservations are never reset.
`approval_wait_started_at` and accumulated `approval_wait_seconds` are exposed
separately on run snapshots. The approval's absolute expiry also gates execution
after approval, so a long post-approval queue cannot authorize a late effect.
Deferral grants one additional bounded lease acquisition without consuming a
recovery allowance; the attempt records retain the WAITING_APPROVAL boundary.

JWT permissions are pinned to admitted scopes and token expiry. Revocation before
token expiry uses the server-owned denylist, for example
`RUNWEAVE_AUTH_REVOKED_SUBJECTS='[["example","alice"]]'`. Apply it to every replica
before resuming affected effects. There is no external identity introspection.
Test mode additionally intersects admitted scopes with all current token-map
entries for that tenant/subject; removing the identity or scope prevents execution.
Local mode uses the developer principal. Granting later scopes never expands a
run's original authority. Legacy principals retain their original scopes on upgrade.

The approval row stores the chosen decision, decider, and decision time; sandbox
receipts store exact arguments and commit time. Sanitized API/worker audit events
record decisions and waiting without publishing argument or message payloads.

## Verify

```powershell
uv run --no-sync pytest tests/test_approvals.py -q
.\scripts\verify-postgres.ps1
```

Tests cover deferred tool execution, exact bindings, process exit while waiting,
process exit after the real sandbox commit, resume without more inference,
duplicate/opposing decisions, owner/scope boundaries, expiry, cancellation,
changed approval state, current authorization, capacity, and separate wait time.
An API test verifies another run completes on the sole worker while approval waits.
