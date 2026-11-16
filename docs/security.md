# Authentication and execution policy

Milestone 9 adds run ownership, API and tool scopes, tenant admission limits,
and credential checks at execution boundaries. The default local mode assigns
every request to tenant `local`, subject `developer`, with access to the two
registered synthetic read tools and the opt-in approved sandbox ticket action.
Select JWT mode for authenticated deployments.

## Identity configuration

| Mode | Identity source | Intended use |
| --- | --- | --- |
| `local` | Fixed local developer principal | Existing offline demo and benchmarks |
| `test` | Configured bearer-token map | Local tests with multiple principals |
| `jwt` | Signed bearer token from a configured issuer | Authenticated deployments |

Test mode requires `RUNWEAVE_AUTH_TEST_IDENTITIES`, a JSON map from synthetic
bearer tokens to principals. For example, this entry gives Alice access to her
own synthetic runs. Tokens must be 16 to 512 characters and the map has a
32-entry limit.

```json
{
  "synthetic-alice-token": {
    "tenant": "example",
    "subject": "alice",
    "scopes": [
      "runs:submit", "runs:read", "runs:cancel",
      "tools:check_health", "tools:lookup_runbook"
    ]
  }
}
```

JWT mode requires these settings. Configure a random shared signing key of at
least 32 bytes through the deployment's secret mechanism.

```dotenv
RUNWEAVE_AUTH_MODE=jwt
RUNWEAVE_AUTH_ISSUER=https://your-issuer.example
RUNWEAVE_AUTH_AUDIENCE=runweave
RUNWEAVE_AUTH_SIGNING_KEY=replace_with_at_least_32_random_bytes
```

The configured issuer must issue HS256 tokens containing `iss`, `aud`, `sub`,
`tenant`, `scope`, `iat`, and `exp`. `scope` is a space-delimited string. `iat`
and `exp` are integer Unix timestamps. PyJWT verifies the signature, issuer,
audience, expiry, and issued-at time. The verifier fixes HS256 in code. Tenant
and subject names have a bounded identifier grammar and at most 32 scopes are
accepted. Missing JWT configuration fails startup. JWT verification uses the
configured key and performs no discovery or key-fetching requests.

API callers send `Authorization: Bearer <token>`. Missing or invalid credentials
return 401 with `WWW-Authenticate: Bearer`. A valid principal missing a required
scope receives 403. Requests carrying tenant, subject, or scope headers retain
the identity established by the selected provider.

## Ownership and admission

| Operation | Required scopes |
| --- | --- |
| Submit a run | `runs:submit` |
| Read a run | `runs:read` and matching tenant and subject |
| Cancel and return a run snapshot | `runs:cancel`, `runs:read`, and matching tenant and subject |
| Execute health lookup | `tools:check_health` |
| Execute runbook lookup | `tools:lookup_runbook` |
| Request/execute a sandbox ticket | `tools:create_ticket` |
| Read an approval | `runs:read` and matching tenant/subject |
| Decide an approval | `approvals:decide`, `runs:read`, and matching tenant/subject |

Foreign and unknown run IDs both return 404. The request body uses the incident
schema, which forbids ownership and permission fields. Internal snapshots carry
the admitted principal, and API serialization excludes that field. Scopes and
expiry are preserved through queued execution and durable recovery. Each tool
dispatch checks its scope and identity expiry again. A run whose identity has
expired before execution fails with a sanitized run error.

Milestone 12 rechecks current identity before approved effects. The server-owned
`RUNWEAVE_AUTH_REVOKED_SUBJECTS` denylist blocks tenant/subject pairs on API calls
and recovered execution. Apply revocation to every replica. JWT authority remains
bounded by admitted scopes and token expiry; there is no external introspection.
Test mode also checks the current identity map. See [approval policy](approvals.md).

`RUNWEAVE_TENANT_MAX_UNFINISHED` defaults to 1,000. It bounds queued and running
and approval-waiting jobs across all subjects in one tenant. Terminal transitions release quota.
Memory mode checks quota and enqueues without yielding. PostgreSQL checks quota
and commits admission under the existing transaction-level admission lock, so
competing replicas share one limit. A tenant quota rejection returns 429 with
`Retry-After`. Global queue saturation continues to return 503.

JWT permissions are captured at submission and bounded by token expiry. An
issuer's later revocation or scope change becomes effective through new tokens
and expiry. Immediate revocation requires a separately configured revocation
mechanism. Use short-lived tokens that cover the intended run duration.

Health probes and aggregate `/metrics` remain accessible to the deployment's
monitoring network. Scope checks cover the run API. Restrict monitoring endpoints
at the deployment boundary when exposing an authenticated service.

## Prompt and tool boundaries

The model proposes a typed call. RunWeave validates the registered name, scope,
strict arguments, incident service, and resource limits before dispatch. Model
responses, incident instructions, and retrieved text cannot modify the principal.
The tools have fixed destinations and schema-constrained service paths. HTTP
adapters explicitly disable redirects, and response reads and retained evidence
have size limits. Both workflow implementations use these checks.

The model receives incident and tool content as untrusted data. The fake model
can repeat that text in its synthetic summary. Authorization relies on the
execution checks. The final evidence validator still requires both observed
citations.

Configured Groq keys, signing keys, test bearer tokens, and
`RUNWEAVE_PROTECTED_VALUES` are checked at input, model-decision, evidence, and
final-output boundaries. Retained API responses are checked again when read.
A match rejects the operation with a generic error.
JSON-escaped credential forms are also checked. Configure protected fixture
values as a JSON array, such as `["RW_PRIVATE_CANARY_73f09"]`. This is exact-value
filtering. Arbitrary secret discovery, transformed encodings, and semantic
factuality require additional controls and evaluations for future capabilities.

Request validation errors omit submitted values. Run errors omit exception
messages. RunWeave logs and traces use allowlisted identifiers, status, timing,
and policy decisions. PydanticAI content instrumentation is explicitly disabled.
With `RUNWEAVE_JSON_LOGS=true`, policy decisions produce `policy.decision` events
with a hash of the principal's tenant and subject. Metrics count decisions using
bounded action and reason labels. The bounded logging queue can drop events
under sustained overload, which is reflected in the dropped-event metric.

## PostgreSQL upgrade

Apply migrations before starting the updated durable service. Milestone 10 also
adds migration 0003 and handles unfinished pre-budget jobs as described in the
[budget upgrade guide](budgets.md).

```powershell
uv pip install -r requirements-durable.txt
uv run --no-sync alembic upgrade head
```

Migration `0002_run_principals` backfills existing jobs to the fixed local
developer owner and persists principals for new jobs. An authenticated caller
needs that exact tenant and subject to access those legacy rows. The migration
adds an index for unfinished tenant counts. PostgreSQL cancellation includes
the owner predicate in the update transaction, including repeated cancellation
and terminal-run reads.

## Verification

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync pytest tests/test_security.py tests/test_evaluation.py -q
uv run --no-sync python -m runweave.evaluation.suite --workflow baseline --output target/evals/baseline.json
uv run --no-sync python -m runweave.evaluation.suite --workflow pydanticai --output target/evals/pydanticai.json
.\scripts\verify-postgres.ps1
```

The PostgreSQL script creates and removes a disposable local cluster and
synchronizes workflow/evaluation extras and installs durable test requirements.
Use `uv run --no-sync` for subsequent checks. Security tests use synthetic identities and
credentials, local transports, and mocked model decisions. They cover owner
isolation, scope denial, concurrent quotas, JWT failure cases, injection attempts,
credential disclosure through outputs and telemetry, redirects, and response
limits. The PostgreSQL integration also verifies legacy migration, durable owner
isolation, quota races, and ownership retained through claiming and recovery.

The JWT implementation follows the fixed-algorithm and claim-validation APIs
in the [PyJWT documentation](https://pyjwt.readthedocs.io/en/stable/api.html).
