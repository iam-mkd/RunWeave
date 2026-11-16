# Context, token, and cost budgets

Milestone 10 adds one run ledger shared by the baseline runner and PydanticAI
workflow. Each model attempt reserves a request, input tokens, output tokens,
and estimated spend before invoking the model. Child asyncio tasks inherit the
ledger. Durable attempts use the same ledger after process recovery.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `RUNWEAVE_AGENT_REQUEST_LIMIT` | 20 | Accounted model attempts across the run |
| `RUNWEAVE_AGENT_INPUT_TOKENS` | 100000 | Cumulative input token allowance |
| `RUNWEAVE_AGENT_COMPLETION_TOKENS` | 2048 | Cumulative output token allowance |
| `RUNWEAVE_AGENT_CONTEXT_TOKENS` | 16384 | Input estimate plus output allowance for one request |
| `RUNWEAVE_AGENT_EVIDENCE_BYTES` | 2048 | Maximum bytes per evidence item injected into context |
| `RUNWEAVE_AGENT_MAX_COST_USD` | 0.05 | Maximum estimated model spend in USD |

Each request reserves up to 512 output tokens, further bounded by the existing
completion budget and the ledger's remaining allowance. The current read-only
health/runbook tools have zero model-token cost. Their retry count, execution
steps, response size, and deadline remain independently bounded. Model retries
and resumed attempts consume additional ledger reservations.

Fake mode uses `fake-free-v1`, with zero prices. Its synthetic model reports one
completion token per response. Input tokens use the conservative estimate.

Live Groq mode requires both rates and a price version. Supply the rates for
the configured model and service tier from your agreement, then set a meaningful
version identifying that price configuration.

```dotenv
RUNWEAVE_MODEL_INPUT_USD_PER_MILLION=<your_input_rate>
RUNWEAVE_MODEL_OUTPUT_USD_PER_MILLION=<your_output_rate>
RUNWEAVE_MODEL_PRICE_VERSION=<your_contract_or_price_revision>
```

Rates support three decimal places per million tokens. Accounting uses integer
nanodollars internally and reports USD as a decimal string. Prices and limits
are pinned at admission for durable jobs. A new model ID cannot use an existing
run's ledger. Updated rates apply to newly admitted jobs.

Groq's model listing currently labels `llama-3.3-70b-versatile` pricing as
“Contact Sales.” The fixture rates of 0.59 input and 0.79 output in tests are
synthetic test configuration. Actual billing depends on the supplied rates,
provider reporting, and the applicable agreement. The cost field is an estimate.
[Groq model listing](https://console.groq.com/docs/models)

## Context preparation

The canonical request builder is shared with the Groq adapter. Preflight includes
system instructions, the incident, tool schemas, the final-result schema, tool
call arguments and IDs, evidence envelopes, and output allowance.

The pinned estimator is `utf8-bytes-v1`. It counts one token per byte of the
ASCII-escaped serialized payload, plus 1,024 tokens of framing slack. The project
has no pinned Llama tokenizer assets, so the estimator deliberately overcounts
ordinary text. Set the configured context limit within the selected model's
actual window. Groq documents a 131,072-token window for the existing Llama model.
[Groq model specification](https://console.groq.com/docs/model/llama-3.3-70b-versatile)

Preparation bounds free-text runbook evidence with a deterministic UTF-8 prefix
and truncation marker. If the request still exceeds the context limit, older
runbook text is replaced with a compact marker first. Evidence IDs, structured
health observations, tool names, arguments, complete call/result pairs, system
instructions, and the incident remain available. Structured evidence that cannot
fit its byte limit causes preflight failure. The original observed history is
kept for final evidence validation. Credential filtering checks full evidence
before any context truncation.

The current workflow injects health and runbook evidence. Persistent memory is
scheduled for milestone 13 and must enter through this bounded preparation path.

## Reservations and reconciliation

Memory transactions execute without yielding while changing the ledger.
PostgreSQL transactions lock the run row, verify the current live lease, apply
the reservation, and commit before model invocation. A competing task sees the
already reserved amounts. The database transaction ends before provider I/O.

When Groq returns valid usage, the adapter reconciles prompt and completion
counts before validating the proposed action. A malformed result therefore
retains any known usage. If one usage field is missing, that field retains its
full reservation. Missing responses, invalid usage, transport failures, timeouts,
cancellation, and process death leave the unresolved reservation charged in full.
The count can include a cancelled attempt that was still waiting for a provider
slot. This conservative treatment also covers ambiguous database acknowledgments.

There is no cancellation-time refund await that could replace `CancelledError`.
Stale or terminal leases cannot refund a durable reservation. Reported usage
above the reserved bound is recorded and blocks further reservations, even if
the aggregate allowance still has room. Other in-flight calls can settle their
known usage while the ledger stays blocked.

Recovery resumes the next committed workflow node within the original deadline.
Its requests share the authoritative existing accounting, including uncertain
reservations. Checkpoints retain the original allowances and atomically snapshot
usage; they never reset the ledger. A child task shares its parent's ledger and
pinned model policy. See [workflow replay](checkpoints.md); typed delegation is
planned for milestone 14.

For an opt-in sandbox ticket, [approval waiting](approvals.md) pauses only the
active run deadline. Waiting has its own absolute expiry; request/token/cost
allowances and uncertain reservations are unchanged. Run snapshots expose the
waiting timestamp and accumulated waiting seconds separately.

## API and telemetry

Run snapshots add a `usage` object containing `model`, `price_version`,
`estimator`, `requests`, `input_tokens`, `output_tokens`, `estimated_cost_usd`,
`uncertain_requests`, and `blocked`. Token counts include conservative reservations when
usage is unknown. `uncertain_requests` also includes currently pending attempts
and fake-model attempts with estimated input usage.

Budget or context rejection becomes a failed run with a sanitized error code.

| Code | Meaning |
| --- | --- |
| `RUN_BUDGET_EXCEEDED` | Request, cumulative tokens, or estimated spend cannot be reserved |
| `CONTEXT_LIMIT_EXCEEDED` | Protected context or structured evidence cannot fit |
| `BUDGET_POLICY_MISMATCH` | Execution model differs from the pinned ledger model |
| `BUDGET_LEASE_LOST` | An expired, cancelled, or stale owner attempted an accounting update |
| `PROVIDER_USAGE_EXCEEDED_RESERVATION` | Provider usage exceeded the estimated reserved bound |
| `PROVIDER_USAGE_INVALID` | A usage value was invalid |

Policy metrics record `budget` decisions using bounded reasons such as `reserved`,
`compacted`, `denied`, and `uncertain`. Optional JSON completion events contain
aggregate usage numbers. The ledger, API usage object, logs, and traces contain
accounting metadata and omit prompt and evidence text.

## PostgreSQL upgrade

Stop existing workers, install the durable requirements, and apply migration 0003
before starting the updated service.

```powershell
uv pip install -r requirements-durable.txt
uv run --no-sync alembic upgrade head
```

Migration 0003 adds the ledger column. Existing terminal jobs remain readable
with `usage` set to null. Existing unfinished jobs have unknown prior usage and
become failed with `BUDGET_ACCOUNTING_UNAVAILABLE`. Their open attempts are closed.
Review those outcomes and resubmit work under a new explicit budget if needed.
New jobs persist their policy and reservation journal with admission. The journal
is bounded by the configured request limit and is deleted with retained job data.

## Verification

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv lock --check --offline
.\scripts\verify-postgres.ps1
```

Tests cover preflight before the HTTP provider transport, both workflows through
the run API, shared concurrent reservations, cost arithmetic, reconciliation,
missing usage, invalid results, cancellation, retries, deterministic compaction,
and invalid persisted policies. The PostgreSQL test kills a worker after a
reservation, verifies retained charges after recovery, races reservations, and
rejects stale and terminal-lease refunds. It also checks the legacy upgrade.

The uv build cache keys include Python source and packaged fixture files. This
ensures non-editable verification installs rebuild when those files change.

Offline evaluation and 100-worker load checks use the fake model. Live inference
requires the existing explicit opt-in and configured prices.
