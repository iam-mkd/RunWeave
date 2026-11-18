# HTTP agent contract suite

`tests/test_http_contract_suite.py` runs one set of checks through RunWeave against a
fixture agent written in each SDK language. Every fixture implements the same behavior
with that language's SDK, so the suite proves the SDKs and RunWeave's adapter agree on
the `runweave.agent/v1` contract (`contracts/agent-http-v1.openapi.json`).

A fixture starts on a free loopback port, prints one line
`RUNWEAVE_AGENT_READY http://127.0.0.1:<port>` to stdout and serves until killed.

| Agent | Behavior |
| --- | --- |
| `contract-echo` | Input `{text: 1–100 chars, delay_ms: 0–60000 = 0}`, output `{reply, attempt}`. Records one step titled `Echo`. Sleeps `delay_ms` first; if cancelled while sleeping, appends `text` to `cancelled`. |
| `contract-crash` | Raises an unexpected error (the SDK answers 500 `RUN_FAILED`). |
| `contract-malformed` | Run responses are 200 with truncated JSON. |
| `contract-oversized` | Run responses are 200 with about 2 MB of JSON. |
| `contract-redirect` | Run responses are 307 to a link-local address. |
| `contract-unavailable` | Run responses are 503 with `Retry-After: 0`. |
| `contract-server-error` | Run responses are 500 `text/plain` with a fake stack trace. |

The last five share `contract-echo`'s schemas and are answered by the fixture itself,
because a correct SDK never produces those responses.

`GET /fixture/events` returns `{"cancelled": [text, ...], "requests": {agent: count}}`,
where `requests` counts run requests per agent.
