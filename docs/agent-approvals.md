# Approvals for your agents: `ctx.request_approval`

Before an agent does something that matters (issue a refund, send an email, delete a
record), it can ask a person to approve that exact action and wait for the answer:

```python
from runweave.sdk import RunContext, agent


@agent(name="refunder", version="1.0.0", input=Refund, output=Outcome)
async def refunder(input: Refund, ctx: RunContext) -> Outcome:
    decision = await ctx.request_approval(
        "issue_refund", {"order": input.order, "amount": input.amount}, expires_in=900
    )
    if not decision.approved:
        return Outcome(refunded=False, reason=f"rejected by {decision.decided_by}")
    receipt = await payments.refund(input.order, input.amount, idempotency_key=str(ctx.run_id))
    return Outcome(refunded=True, receipt=receipt)
```

The run shows `WAITING_APPROVAL` until someone decides in the workspace's **Approvals**
screen, with `runweave approvals`, or through the API below. The agent's
call then returns and the agent continues where it was. Agents served over HTTP have the
same call (`ctx.request_approval` with `runweave.sdk_http`, `ctx.requestApproval` in the
TypeScript SDK); see [below](#http-agents-and-the-typescript-sdk).

## What you get back

| Outcome | What `request_approval` does |
| --- | --- |
| Approved | Returns `Approval(status="approved", approved=True, decided_by=..., decided_at=...)` |
| Rejected | Returns `approved=False`; your agent decides what to do next |
| Nobody decided in time | Raises `ApprovalExpired`; uncaught, the run ends `TIMED_OUT` with `APPROVAL_EXPIRED` |
| The run was cancelled | The wait stops like any other await (the request becomes `cancelled`) |
| Too many requests pending | Raises with `APPROVAL_CAPACITY_EXCEEDED` (the run fails unless you catch it) |
| Memory mode (no database) | Raises `ApprovalUnavailable` (`DURABLE_APPROVAL_REQUIRED`) |

`action` must match `^[a-z][a-z0-9_.:-]{0,99}$`. `arguments` is any JSON value up to 64 KiB;
it passes the same content policy as run input. `expires_in` (seconds) is capped by
`RUNWEAVE_APPROVAL_TTL_SECONDS` (default one hour), which is also the default.

## Requirements and guarantees

- **Durable mode.** Requests are stored in PostgreSQL (`RUNWEAVE_DATABASE_URL`, migration
  `0008_action_approvals`: `alembic upgrade head`). Memory mode has nothing to survive a
  restart with, so it refuses the call instead of pretending.
- **Bound to the exact request.** A request is bound to the tenant, run, action, the
  SHA-256 of the canonical JSON arguments, a policy version (`agent:<name>@<version>`) and
  its expiry. A decision must name the digest, policy version and expiry it is for; a
  mismatch is refused (`APPROVAL_BINDING_MISMATCH`).
- **Decisions are idempotent.** The same decision again is accepted; the opposite decision
  is refused (`APPROVAL_DECISION_CONFLICT`); deciding after expiry or cancellation is
  refused (`APPROVAL_NOT_PENDING`). Concurrent decisions resolve to exactly one.
- **Waiting does not use up the run deadline.** The run's deadline is paused while it
  waits and resumes with the time that was left; the wait has its own hard expiry instead.
  `ctx.remaining_seconds()` and the run's proxy token follow the pause.
- **A waiting agent keeps its worker slot.** RunWeave cannot checkpoint your code, so the
  call waits in place and the run keeps its lease. Nothing is re-run and no model calls
  are repeated. To keep waiting runs from starving the pool, at most
  `min(RUNWEAVE_APPROVAL_MAX_PENDING, worker_count / 2)` requests can be pending at once.
- **Restarts do not duplicate requests.** If RunWeave stops while an agent waits, the run
  is picked up again after its lease expires and the agent runs from the start. When it
  repeats the same request (same action and arguments), it gets the existing request, or
  the decision that was made in the meantime, not a new one.

## What stays your responsibility

RunWeave approves the request; it does not perform the action. If your agent restarts
after the action happened but before the run finished, it will run again and, with the
same arguments, get the same approval again. **Make the approved effect itself
idempotent**: pass an idempotency key to the downstream API (the run id is a good one), or
check whether the action already happened before doing it again. Also keep the code before
`request_approval` deterministic enough that a re-run asks with the same arguments; a
different request is a new request.

## HTTP agents and the TypeScript SDK

```ts
const decision = await ctx.requestApproval('issue_refund', { order: input.order }, { expiresIn: 900 });
if (!decision.approved) return { refunded: false, reason: `rejected by ${decision.decidedBy}` };
```

Rejections return `approved: false`; expiry throws `ApprovalExpired` (`APPROVAL_EXPIRED`,
the run times out); without a durable RunWeave the call throws `ApprovalUnavailable`. The
agent's own deadline (`ctx.remainingMs()`, `ctx.signal`) is paused while it waits, like the
run's.

Under the hood, in durable mode each run request carries an `approvals_url`. The SDK posts
`{"action", "arguments", "expires_in", "wait_seconds"}` there with the run attempt's proxy
token as a bearer token; RunWeave records the request (or finds the one it already has),
waits up to `wait_seconds` (at most 25) for a decision and answers
`{"approval": {"id", "status", "decided_by", "decided_at", "expires_at"}}`. While the
status is `pending` the SDK repeats the same call, which is safe because requests are
deduplicated by run, action and arguments. If RunWeave is unreachable (restarting, say) the
SDK keeps trying; RunWeave ends the run's request, aborting the agent, if it gives up.
Errors use the contract shape (`401 APPROVAL_TOKEN_INVALID`, `409` with
`APPROVAL_CAPACITY_EXCEEDED` and similar, `422 APPROVAL_REQUEST_INVALID`). Agents in other
languages can implement the same loop.

## Deciding

**In the workspace:** the Approvals screen lists pending requests (tick *Show decided* for
the rest) with the agent, run and time left. Selecting one shows the exact arguments, the
argument digest, the policy version and the expiry; Approve and Reject each ask for
confirmation and apply only to that request. A run page waiting on an agent's request
links to it.

**In a terminal:**

```bash
runweave approvals list                      # pending requests (--all for decided ones)
runweave approvals show <id>                 # the exact arguments (--json for scripts)
runweave approvals approve <id>              # shows the request, asks, then approves
runweave approvals reject <id> --yes         # no prompt, for scripts
```

Commands talk to `--server` (default `http://127.0.0.1:8000`); set `RUNWEAVE_API_TOKEN` for
a server with authentication. A decision is sent with the binding just read, so it applies
only to the request that was shown. Exit codes: `0` done, `1` the server refused the
decision (for example, the opposite decision was already made), `2` a usage, not-found or
connection problem.

Try it with the sample agent (`examples/sample-agent`), which asks before issuing a refund:

```bash
RUNWEAVE_DATABASE_URL=postgresql+asyncpg://... runweave serve --agent sample_agent:refund_request
```

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/approvals` (`?status=all`) | Pending requests (or all) for your runs, with exact arguments; `durable: false` in memory mode |
| `GET /v1/approvals/{id}` | One request and its status |
| `POST /v1/approvals/{id}` | `{"decision": "approve" \\| "reject", "argument_digest", "policy_version", "expires_at"}` |

Reading needs `runs:read`; deciding needs `approvals:decide`. Requests are visible only to
the owner of the run, as for runs themselves. The ticket approvals of the built-in
triage agent ([milestone 12](approvals.md)) keep their own `/v1/runs/{id}/approval` routes.
