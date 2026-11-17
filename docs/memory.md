# Scoped persistent memory

Milestone 13 stores historical observations in PostgreSQL. Memory is opt-in and
uses exact service/scope filtering, newest-first ordering, and bounded retrieval.
It adds no embeddings, semantic index, provider calls, or automatic model writes.
Both workflows retain their current health/runbook tools and evidence checks.
The later [coordinated workflow](coordination.md) can recall facts at its merge
boundary; its specialists have no memory authority.

## Upgrade and permissions

Stop old workers, apply `alembic upgrade head` including migration 0006, then
start upgraded replicas. This additive migration preserves existing jobs and
grants no new permissions to existing principals. Memory requires PostgreSQL;
in-memory mode rejects recall requests before admission and returns 503 on
memory endpoints. Export retained memory and restore a backup to downgrade.

| Operation | Required authority |
| --- | --- |
| Read private facts | `memory:read`, matching tenant/subject |
| Read project facts | `memory:read`, configured tenant/project membership |
| Save an observation | `memory:write`, `runs:read`, ownership of its successful source run |
| Save to a project | Save authority plus current project membership |
| Delete a fact | `memory:delete`, matching tenant/writer subject |

Run submission still needs `runs:submit`; recall additionally requires
`memory:read` and project membership when selected. Scopes and identity expiry
remain bounded by admission, with current identity revocation rechecked on
execution. Project membership comes exclusively from server configuration:

```dotenv
RUNWEAVE_MEMORY_PROJECT_MEMBERSHIPS='{"example":{"payments":["alice","bob"]}}'
```

The map is tenant → project → subject names. Configure it consistently on all
replicas; at most 1,000 tenants, projects, and total memberships are allowed.
JWT scopes do not grant project membership, and request bodies cannot grant
tenant, subject, permissions, or membership. Default project membership is empty.

## Save, recall, and delete

After a successful run, explicitly save one of its returned evidence IDs:

```http
POST /v1/memory
Content-Type: application/json

{
  "source_run_id": "<successful run UUID>",
  "evidence_id": "<observed health or runbook evidence ID>"
}
```

Add `"project": "payments"` to share within that configured project. Without a
project, the fact belongs exclusively to the writer's subject. A project query
selects that project's facts; it does not implicitly include private facts.

The server copies the exact committed observation, rather than accepting new
content from the caller. Its versioned `observed-evidence-v1` write policy
requires a successful, supported, done checkpoint and a scoped registered
health/runbook call. Health observations are typed and checked against service
and evidence ID. Oversized sources are rejected rather than silently truncated.
Unfinished/foreign/unknown sources, fabricated IDs, and legacy runs without a
supported checkpoint fail with `MEMORY_SOURCE_UNAVAILABLE`. Arbitrary content or
ownership fields are rejected by the request schema.

Facts contain typed kind/service/content, tenant, writer, optional project,
creation/expiry times, and provenance: source run, evidence ID, observation time,
workflow, prompt hash, checkpoint sequence, and source content SHA-256. Content
is limited to 2,048 UTF-8 bytes; its digest is verified on recall. Evidence and
provenance survive ordinary source-run history eviction. A repeated write returns
the same live fact without extending its expiry, including after source eviction.
After expiry or deletion, explicitly saving again requires a retained valid source.

```http
GET /v1/memory?service=payments
GET /v1/memory?service=payments&project=payments
DELETE /v1/memory/<fact UUID>
```

Reads return only the selected authorized scope and exact service. Foreign
private facts are omitted; ungranted projects return 403. Only the writer can
delete a fact, including after leaving a project. Foreign/unknown deletion
returns 404; successful hard deletion returns 204. A project reader cannot
delete another writer's facts. Deletion does not erase separately retained
source runs, evidence, or already generated results.

Opt into recall on a later run:

```json
{
  "service": "payments",
  "description": "Investigate synthetic latency",
  "memory": {}
}
```

Use `"memory": {"project": "payments"}` for a project. Omitting `memory` retains
the existing workflow. Successful triage never writes memory automatically.

## Prompt boundaries and replay

Each inference retrieves live facts again. Recalled facts enter a separate JSON
user-data message named `untrusted_recalled_facts`; a versioned memory instruction
explicitly treats them as historical data without authority. Facts cannot add
tools, modify scopes, change service arguments, or supply valid current evidence
citations. Both fresh tools are still required. Injection fixtures deliberately
propose shell execution, another service, revoked tools, and fabricated evidence;
the existing server-side boundaries reject those proposals.

Recall uses whole facts within a byte allowance, then shares the run's existing
context/input-token/cost budget. Oldest recalled facts are dropped first when
needed to preserve current evidence and protected instructions. IDs and provenance
are kept with retained facts. The reserved input bound includes the serialized
memory envelope and metadata. Dispatch revalidation can only remove facts; it
never adds data after reservation. Groq revalidates after provider concurrency
and rate waits, immediately before building the outgoing payload.

Expiry and hard deletion are checked at database retrieval/dispatch snapshots.
A deletion committed before that snapshot excludes the fact. Already dispatched
requests cannot be recalled from a provider; deletion is not a retroactive erase
of an in-flight request or existing result. Custom model adapters must retain
the dispatch-refresh hook if they add their own waits after budget preparation.

Recalled content is task-local and is not checkpointed or journaled as reusable
tool evidence. Recovery queries the current authorized memory set on its next
inference, so a checkpoint cannot restore a deleted recall. Pending model
decisions already committed before deletion remain ordinary run history.
Memory-enabled checkpoints pin `triage-memory-prompt-v1` and its combined prompt
hash; ordinary checkpoints keep their existing prompt identity.

## Bounds and retention

| Setting | Default | Maximum |
| --- | --- | --- |
| `RUNWEAVE_MEMORY_TTL_SECONDS` | 604800 (7 days) | 2592000 (30 days) |
| `RUNWEAVE_MEMORY_MAX_RECORDS` | 10000 | 100000 |
| `RUNWEAVE_MEMORY_MAX_PER_TENANT` | 1000 | 10000 |
| `RUNWEAVE_MEMORY_MAX_PER_SCOPE` | 100 | 1000 |
| `RUNWEAVE_MEMORY_RECALL_LIMIT` | 5 | 20 |
| `RUNWEAVE_MEMORY_RECALL_BYTES` | 4096 | 16384 |

The scope limit counts all writers within a project, or one private subject.
Writes serialize quota checks across replicas and fail with
`MEMORY_CAPACITY_EXCEEDED` at capacity. Expired facts are immediately excluded
from reads and removed in batches of 128 by supervised maintenance and writes.
API list reads use the row limit; model recall also applies the byte/context
limits. A fact that cannot fit is omitted whole. Read/write/delete audit events
record outcomes without logging memory content, source payloads, or credentials.
Configured credentials are checked on both write and recall.

```powershell
uv run --no-sync pytest tests/test_memory.py -q
.\scripts\verify-postgres.ps1
```

Tests cover process restart, both workflows, provenance, expiry/deletion,
source eviction, private/project/tenant isolation, membership/scope revocation,
concurrent quotas, credential rejection, bounded contexts, dispatch refresh,
adversarial proposals, and checkpoint resume after deletion. These deterministic
fixtures verify execution boundaries; they do not establish live-model semantic
robustness or production capacity.
