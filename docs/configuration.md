# Configuration reference

RunWeave has two kinds of configuration:

- **`runweave.toml`**, the project file that `runweave eval`, `bench` and `shadow` read
  (and `runweave serve` for its testing screens). It lives in your agent's folder and is
  meant to be committed.
- **`RUNWEAVE_*` settings** for the service itself: environment variables, or lines in a
  `.env` file in the folder where you start it. An unknown name in `.env` is an error, so a
  typo there never goes unnoticed (unknown environment variables are ignored). Lists and objects are written as JSON, for example
  `RUNWEAVE_AGENTS='["mypkg.agent:run"]'`. Secrets (`secret` below) are never logged or
  returned by the API.

The workspace's **Settings** screen changes the proxy, budgets and fault settings while the
server runs, and shows the environment lines that make a change permanent (see [LLM
proxy](llm-proxy.md#settings-screen)).

The tables are generated from the code by `scripts/reference-docs.py`.

## `runweave.toml`

Paths in the file are relative to it and must stay inside the project folder. Unknown keys
are an error.

```toml
[agent]
name = "word-count"
python = "agent:run"           # or http = "http://127.0.0.1:9200", and/or command = [...]

[limits]
run_timeout_seconds = 30
concurrency = 4

[proxy]
mode = "mock"
fault_profile = ""             # for example "flaky"; see faults.md
mock_fixtures = "fixtures.json"

[[datasets]]
name = "smoke"
path = "datasets/smoke.jsonl"

[eval]
min_pass_rate = 1.0

[bench]
concurrency = [1, 4, 16]
runs = 32

[shadow]
candidate = { python = "agent_v2:run" }
```

<!-- generated:project -->
### `[agent]`

The agent to test. Set `python`, or `http` and/or `command`.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `python` | string | unset | length ≤ 200 |
| `http` | string | unset | length ≤ 2048 |
| `command` | list | unset | length ≥ 1, length ≤ 32 |
| `name` | string | required |  |

### `[limits]`

Per-run deadline and cases in flight.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `run_timeout_seconds` | number | `30` | > 0, ≤ 3600 |
| `concurrency` | integer | `4` | ≥ 1, ≤ 256 |

### `[proxy]`

The LLM proxy for this project's runs.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `mode` | `"mock"` | `mock` |  |
| `fault_profile` | string | empty | length ≤ 40 |
| `fault_seed` | integer | `0` | ≥ 0 |
| `mock_fixtures` | string | unset |  |

### `[[datasets]]`

One table per dataset (`.jsonl`, `.yaml` or `.yml`).

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `name` | string | required |  |
| `path` | string | required | length ≥ 1, length ≤ 500 |

### `[eval]`

`runweave eval` gates and report folder.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `min_pass_rate` | number | `1.0` | ≥ 0, ≤ 1 |
| `baseline` | string | unset |  |
| `reports` | string | `.runweave/reports` |  |

### `[bench]`

`runweave bench` sweep and gates.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `concurrency` | list | `[1, 4, 16]` | length ≥ 1, length ≤ 12 |
| `runs` | integer | `32` | ≥ 1, ≤ 10000 |
| `fault_profiles` | list | unset | length ≥ 1, length ≤ 12 |
| `soak_seconds` | number | `0` | ≥ 0, ≤ 3600 |
| `max_failure_rate` | number | unset | ≥ 0, ≤ 1 |
| `max_p95_latency_ms` | number | unset | > 0 |
| `max_duplicate_writes` | integer | unset | ≥ 0 |
| `max_tool_calls` | object | unset | length ≤ 50 |

### `[shadow]`

`runweave shadow`: the candidate (an agent table) and promotion thresholds.

| Key | Type | Default | Allowed |
| --- | --- | --- | --- |
| `candidate` | object | unset |  |
| `max_cost_ratio` | number | `1.05` | ≥ 1, ≤ 100 |
| `max_latency_ratio` | number | `2.0` | ≥ 1, ≤ 100 |
| `latency_slack_ms` | number | `50` | ≥ 0, ≤ 60000 |
| `require_output_parity` | boolean | `false` |  |

<!-- /generated:project -->
## Service settings (`RUNWEAVE_*`)

<!-- generated:settings -->
### General

The service's display name.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_APP_NAME` | string | `RunWeave` | length ≥ 1, length ≤ 100 |

### Authentication and tenants

Who may call the API and how much each tenant may queue. See [Authentication and execution policy](security.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_AUTH_MODE` | `"local"` \| `"test"` \| `"jwt"` | `local` |  |
| `RUNWEAVE_AUTH_TEST_IDENTITIES` | object | empty |  |
| `RUNWEAVE_AUTH_REVOKED_SUBJECTS` | list | empty | length ≤ 1000 |
| `RUNWEAVE_AUTH_ISSUER` | string | empty | length ≤ 500 |
| `RUNWEAVE_AUTH_AUDIENCE` | string | empty | length ≤ 200 |
| `RUNWEAVE_AUTH_SIGNING_KEY` | secret | empty |  |
| `RUNWEAVE_TENANT_MAX_UNFINISHED` | integer | `1000` | ≥ 1, ≤ 10000 |
| `RUNWEAVE_PROTECTED_VALUES` | list | empty | length ≤ 32 |

### Admission, scheduling and limits

Bounded queue, workers, deadlines, retained history and payload sizes. See [Architecture](architecture.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_HTTP_TIMEOUT_SECONDS` | number | `5.0` | > 0, ≤ 60 |
| `RUNWEAVE_HTTP_MAX_CONNECTIONS` | integer | `128` | ≥ 1, ≤ 1024 |
| `RUNWEAVE_WORKER_COUNT` | integer | `128` | ≥ 1, ≤ 1024 |
| `RUNWEAVE_QUEUE_CAPACITY` | integer | `1000` | ≥ 1, ≤ 10000 |
| `RUNWEAVE_HISTORY_MAX_RECORDS` | integer | `1000` | ≥ 1, ≤ 10000 |
| `RUNWEAVE_HISTORY_TTL_SECONDS` | number | `3600` | > 0, ≤ 86400 |
| `RUNWEAVE_HISTORY_CLEANUP_INTERVAL_SECONDS` | number | `30` | > 0, ≤ 3600 |
| `RUNWEAVE_RUN_TIMEOUT_SECONDS` | number | `30` | > 0, ≤ 3600 |
| `RUNWEAVE_STEP_TIMEOUT_SECONDS` | number | `5` | > 0, ≤ 3600 |
| `RUNWEAVE_SHUTDOWN_GRACE_SECONDS` | number | `5` | ≥ 0, ≤ 60 |
| `RUNWEAVE_RUN_REQUEST_MAX_BYTES` | integer | `65536` | ≥ 1024, ≤ 1048576 |
| `RUNWEAVE_AGENT_OUTPUT_MAX_BYTES` | integer | `65536` | ≥ 1024, ≤ 1048576 |

### Tool retries

Retries for classified transient tool failures, within the run's deadline.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_TOOL_RETRY_ATTEMPTS` | integer | `3` | ≥ 1, ≤ 10 |
| `RUNWEAVE_TOOL_RETRY_BASE_DELAY_SECONDS` | number | `0.1` | > 0, ≤ 60 |
| `RUNWEAVE_TOOL_RETRY_MAX_DELAY_SECONDS` | number | `2` | > 0, ≤ 60 |

### Agent budgets

Step, token, context and cost budgets per run. See [Budgets](budgets.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_AGENT_MAX_STEPS` | integer | `7` | ≥ 1, ≤ 20 |
| `RUNWEAVE_AGENT_COMPLETION_TOKENS` | integer | `2048` | ≥ 1, ≤ 8192 |
| `RUNWEAVE_AGENT_REQUEST_LIMIT` | integer | `20` | ≥ 1, ≤ 1000 |
| `RUNWEAVE_AGENT_INPUT_TOKENS` | integer | `100000` | ≥ 1, ≤ 10000000 |
| `RUNWEAVE_AGENT_CONTEXT_TOKENS` | integer | `16384` | ≥ 1, ≤ 131072 |
| `RUNWEAVE_AGENT_EVIDENCE_BYTES` | integer | `2048` | ≥ 128, ≤ 16384 |
| `RUNWEAVE_AGENT_MAX_COST_USD` | decimal | `0.05` | ≥ 0, ≤ 100 |
| `RUNWEAVE_MODEL_INPUT_USD_PER_MILLION` | decimal | unset | ≥ 0, ≤ 1000 |
| `RUNWEAVE_MODEL_OUTPUT_USD_PER_MILLION` | decimal | unset | ≥ 0, ≤ 1000 |
| `RUNWEAVE_MODEL_PRICE_VERSION` | string | empty | length ≤ 100 |

### Agents and projects

Python agents loaded at startup, the project the workspace's test screens use, and the interpreter their jobs run with. See [Run your own Python agent](agents.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_AGENT_WORKFLOW` | `"baseline"` \| `"pydanticai"` \| `"coordinated"` | `baseline` |  |
| `RUNWEAVE_AGENTS` | list | empty | length ≤ 32 |
| `RUNWEAVE_PROJECT_FILE` | path | unset |  |
| `RUNWEAVE_JOB_PYTHON` | path | unset |  |

### HTTP agents

Agents in their own process, any language. See [HTTP agents](http-agents.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_HTTP_AGENTS` | list | empty | length ≤ 16 |
| `RUNWEAVE_HTTP_AGENT_ALLOWED_HOSTS` | list | empty | length ≤ 16 |
| `RUNWEAVE_HTTP_AGENT_MAX_REGISTERED` | integer | `32` | ≥ 1, ≤ 128 |
| `RUNWEAVE_HTTP_AGENT_RETRY_ATTEMPTS` | integer | `3` | ≥ 1, ≤ 5 |
| `RUNWEAVE_HTTP_AGENT_RETRY_BASE_DELAY_SECONDS` | number | `0.2` | > 0, ≤ 5 |
| `RUNWEAVE_HTTP_AGENT_RETRY_MAX_DELAY_SECONDS` | number | `2.0` | > 0, ≤ 30 |
| `RUNWEAVE_HTTP_AGENT_RESPONSE_MAX_BYTES` | integer | `1048576` | ≥ 4096, ≤ 8388608 |
| `RUNWEAVE_HTTP_AGENT_CONNECT_TIMEOUT_SECONDS` | number | `2.0` | > 0, ≤ 30 |

### LLM proxy

Mock or passthrough mode, provider keys and limits, faults. See [LLM proxy](llm-proxy.md) and the [fault catalog](faults.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_PROXY_PUBLIC_URL` | string | `http://127.0.0.1:8000` | length ≤ 200 |
| `RUNWEAVE_PROXY_MOCK_FIXTURES` | path | unset |  |
| `RUNWEAVE_PROXY_REQUEST_MAX_BYTES` | integer | `1048576` | ≥ 4096, ≤ 16777216 |
| `RUNWEAVE_PROXY_DEFAULT_MAX_TOKENS` | integer | `1024` | ≥ 1, ≤ 100000 |
| `RUNWEAVE_PROXY_MODE` | `"mock"` \| `"passthrough"` | `mock` |  |
| `RUNWEAVE_PROXY_MODELS` | list | empty | length ≤ 64 |
| `RUNWEAVE_PROXY_OPENAI_BASE_URL` | string | `https://api.openai.com/v1` | length ≤ 200 |
| `RUNWEAVE_PROXY_OPENAI_API_KEY` | secret | empty |  |
| `RUNWEAVE_PROXY_OPENAI_CONCURRENCY` | integer | `4` | ≥ 1, ≤ 64 |
| `RUNWEAVE_PROXY_OPENAI_REQUESTS_PER_MINUTE` | integer | `60` | ≥ 1, ≤ 6000 |
| `RUNWEAVE_PROXY_ANTHROPIC_BASE_URL` | string | `https://api.anthropic.com` | length ≤ 200 |
| `RUNWEAVE_PROXY_ANTHROPIC_API_KEY` | secret | empty |  |
| `RUNWEAVE_PROXY_ANTHROPIC_CONCURRENCY` | integer | `4` | ≥ 1, ≤ 64 |
| `RUNWEAVE_PROXY_ANTHROPIC_REQUESTS_PER_MINUTE` | integer | `60` | ≥ 1, ≤ 6000 |
| `RUNWEAVE_KEY_STORE` | `"keyring"` \| `"memory"` \| `"none"` | `keyring` |  |
| `RUNWEAVE_PROXY_UPSTREAM_MAX_BYTES` | integer | `8388608` | ≥ 65536, ≤ 67108864 |
| `RUNWEAVE_PROXY_FAULT_PROFILE` | string | empty | length ≤ 40 |
| `RUNWEAVE_PROXY_FAULT_SEED` | integer | `0` | ≥ 0, ≤ 9223372036854775807 |
| `RUNWEAVE_PROXY_FAULT_PROFILES_FILE` | path | unset |  |
| `RUNWEAVE_PROXY_FAULT_INLINE` | list | empty | length ≤ 16 |

### Run events

The live `/v1/runs/{id}/events` stream.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_EVENT_STREAM_MAX_SUBSCRIBERS` | integer | `64` | ≥ 1, ≤ 1024 |
| `RUNWEAVE_EVENT_STREAM_MAX_SECONDS` | number | `60` | ≥ 1, ≤ 600 |
| `RUNWEAVE_EVENT_STREAM_HEARTBEAT_SECONDS` | number | `15` | ≥ 0.1, ≤ 60 |

### Multi-agent coordination

See [Typed multi-agent coordination](coordination.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_COORDINATION_CONCURRENCY` | `"1"` \| `"2"` | `2` |  |

### Isolated tools

Disposable Python and browser containers. See [Isolation](isolation.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_ISOLATED_TOOLS_ENABLED` | boolean | `false` |  |
| `RUNWEAVE_ISOLATED_TOOLS_CONCURRENCY` | integer | `2` | ≥ 1, ≤ 8 |
| `RUNWEAVE_SANDBOX_PYTHON_IMAGE` | string | empty |  |
| `RUNWEAVE_SANDBOX_BROWSER_IMAGE` | string | empty |  |
| `RUNWEAVE_BROWSER_ALLOWED_ORIGINS` | list | empty |  |
| `RUNWEAVE_SANDBOX_FIXTURE_CREDENTIAL` | secret | empty |  |

### Built-in model provider

The built-in demo agent's live model (opt-in, your key). Your own agents use the [LLM proxy](llm-proxy.md) instead.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_MODEL_PROVIDER` | `"fake"` \| `"groq"` \| `"openai"` \| `"anthropic"` \| `"xai"` \| `"gemini"` | `fake` |  |
| `RUNWEAVE_MODEL_API_KEY` | secret | empty |  |
| `RUNWEAVE_MODEL_NAME` | string | empty | length ≤ 100 |
| `RUNWEAVE_MODEL_CONCURRENCY` | integer | `2` | ≥ 1, ≤ 32 |
| `RUNWEAVE_MODEL_REQUESTS_PER_MINUTE` | integer | `30` | ≥ 1, ≤ 600 |
| `RUNWEAVE_GROQ_API_KEY` | secret | empty |  |
| `RUNWEAVE_GROQ_MODEL` | string | `llama-3.3-70b-versatile` | length ≥ 1, length ≤ 100 |
| `RUNWEAVE_GROQ_CONCURRENCY` | integer | `2` | ≥ 1, ≤ 32 |
| `RUNWEAVE_GROQ_REQUESTS_PER_MINUTE` | integer | `30` | ≥ 1, ≤ 600 |

### Observability

Structured logs and console traces.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_JSON_LOGS` | boolean | `false` |  |
| `RUNWEAVE_CONSOLE_TRACES` | boolean | `false` |  |

### Demo tools

The built-in demo agent's mock tools.

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_MOCK_RUNBOOK_DELAY_SECONDS` | number | `0` | ≥ 0, ≤ 5 |

### Durable mode (PostgreSQL)

Crash recovery with PostgreSQL. See [Architecture](architecture.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_DATABASE_URL` | secret | unset |  |
| `RUNWEAVE_DB_POOL_SIZE` | integer | `8` | ≥ 1, ≤ 64 |
| `RUNWEAVE_LEASE_SECONDS` | number | `6` | ≥ 1, ≤ 60 |
| `RUNWEAVE_DB_POLL_SECONDS` | number | `0.25` | ≥ 0.05, ≤ 5 |
| `RUNWEAVE_DURABLE_MAX_ATTEMPTS` | integer | `3` | ≥ 1, ≤ 10 |
| `RUNWEAVE_DURABLE_RETRY_DELAY_SECONDS` | number | `0.5` | ≥ 0.05, ≤ 30 |

### Approvals

See [Human approvals](approvals.md) and [Approvals for your agents](agent-approvals.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_APPROVAL_TTL_SECONDS` | number | `3600` | ≥ 1, ≤ 86400 |
| `RUNWEAVE_APPROVAL_MAX_PENDING` | integer | `100` | ≥ 1, ≤ 10000 |

### Memory

See [Scoped persistent memory](memory.md).

| Variable | Type | Default | Allowed |
| --- | --- | --- | --- |
| `RUNWEAVE_MEMORY_TTL_SECONDS` | number | `604800` | ≥ 1, ≤ 2592000 |
| `RUNWEAVE_MEMORY_MAX_RECORDS` | integer | `10000` | ≥ 1, ≤ 100000 |
| `RUNWEAVE_MEMORY_MAX_PER_TENANT` | integer | `1000` | ≥ 1, ≤ 10000 |
| `RUNWEAVE_MEMORY_MAX_PER_SCOPE` | integer | `100` | ≥ 1, ≤ 1000 |
| `RUNWEAVE_MEMORY_RECALL_LIMIT` | integer | `5` | ≥ 1, ≤ 20 |
| `RUNWEAVE_MEMORY_RECALL_BYTES` | integer | `4096` | ≥ 128, ≤ 16384 |
| `RUNWEAVE_MEMORY_PROJECT_MEMBERSHIPS` | object | empty |  |

<!-- /generated:settings -->
