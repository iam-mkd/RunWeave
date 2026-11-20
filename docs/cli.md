# CLI reference

`runweave` is installed with RunWeave; from a checkout, prefix commands with `uv run`. Every
command prints its own help with `--help`, for example `runweave eval --help`.

Exit codes for `eval`, `bench` and `shadow`: `0` every gate passed, `1` a gate failed, `2`
a project, dataset or connection problem (printed as a message, never a traceback).

`eval`, `bench` and `shadow` find `runweave.toml` by searching upward from the current
folder unless `--config` names one. By default they start RunWeave inside themselves
(in memory, offline, mock proxy); `--server` sends the work to a running `runweave serve`
instead. See [test your agent](testing.md).

The tables below are generated from the CLI itself by `scripts/reference-docs.py`.

## Commands

<!-- generated:commands -->
### `runweave serve`

Start the local workspace and API

| Argument | What it does | Default |
| --- | --- | --- |
| `--agent` `MODULE:ATTRIBUTE` | Python agent to load at startup (repeatable), e.g. mypkg.agent:run |  |
| `--http-agent` `URL` | HTTP agent base URL to discover at startup (repeatable), e.g. http://127.0.0.1:9100 |  |
| `--demo` | Offline, in-memory, ignore .env |  |
| `--port` `PORT` | Port on 127.0.0.1 | `8000` |
| `--assets` `ASSETS` | Built workspace UI (default: the packaged UI, else the checkout's frontend/dist) |  |
| `--check` | Load agents, list them and exit |  |
| `--api-only` | Serve the API without the UI |  |
| `--container` | Inside the service image only: listen on the container's interfaces |  |
| `--project` `RUNWEAVE_TOML` | Project for the Load tests, Evals and Shadow screens (default: ./runweave.toml) |  |

### `runweave init`

Create a project with a sample agent and dataset

| Argument | What it does | Default |
| --- | --- | --- |
| `directory` | Folder to create the project in | `.` |
| `--language` `LANGUAGE` | The sample agent's language | `python` |
| `--force` | Overwrite existing files |  |

### `runweave eval`

Run a dataset through the agent and gate on it

| Argument | What it does | Default |
| --- | --- | --- |
| `--config` `CONFIG` | runweave.toml (default: search upward) |  |
| `--dataset` `DATASET` | Dataset name (default: the first one) |  |
| `--server` `SERVER` | Submit to a running RunWeave, e.g. http://127.0.0.1:8000 |  |
| `--progress` | Print progress as JSON lines instead of a summary (used by the workspace) |  |
| `--baseline` `BASELINE` | An earlier report.json to compare with |  |
| `--save-baseline` | Keep this report as the dataset's baseline |  |
| `--junit` `JUNIT` | Also write JUnit XML here |  |

### `runweave bench`

Load-test the agent: concurrency sweeps, fault profiles and soak runs

| Argument | What it does | Default |
| --- | --- | --- |
| `--config` `CONFIG` | runweave.toml (default: search upward) |  |
| `--dataset` `DATASET` | Dataset name (default: the first one) |  |
| `--server` `SERVER` | Submit to a running RunWeave, e.g. http://127.0.0.1:8000 |  |
| `--progress` | Print progress as JSON lines instead of a summary (used by the workspace) |  |
| `--concurrency` `N` | Levels to sweep, e.g. 1 8 32 |  |
| `--runs` `RUNS` | Runs per level |  |
| `--fault-profile` `NAME` | Proxy fault profile (repeatable; "" for none), e.g. flaky |  |
| `--soak` `SECONDS` | Then soak at the top level |  |

### `runweave shadow`

Compare a candidate agent version with the current one on a dataset

| Argument | What it does | Default |
| --- | --- | --- |
| `--config` `CONFIG` | runweave.toml (default: search upward) |  |
| `--dataset` `DATASET` | Dataset name (default: the first one) |  |
| `--server` `SERVER` | Submit to a running RunWeave, e.g. http://127.0.0.1:8000 |  |
| `--progress` | Print progress as JSON lines instead of a summary (used by the workspace) |  |
| `--candidate` `TARGET` | module:function of a Python @agent, or an HTTP agent URL ([shadow] candidate) |  |

### `runweave approvals`

List and decide agents' approval requests (durable RunWeave)

### `runweave approvals list`

Pending requests (--all for decided ones too)

| Argument | What it does | Default |
| --- | --- | --- |
| `--all` | Include decided requests |  |
| `--json` | Print JSON |  |
| `--server` `SERVER` | RunWeave to talk to (http://127.0.0.1:8000) | `http://127.0.0.1:8000` |

### `runweave approvals show`

One request with its exact arguments

| Argument | What it does | Default |
| --- | --- | --- |
| `id` | The approval request id |  |
| `--json` | Print JSON |  |
| `--server` `SERVER` | RunWeave to talk to (http://127.0.0.1:8000) | `http://127.0.0.1:8000` |

### `runweave approvals approve`

Show a request, confirm, then approve it

| Argument | What it does | Default |
| --- | --- | --- |
| `id` | The approval request id |  |
| `--yes` | Decide without a prompt |  |
| `--server` `SERVER` | RunWeave to talk to (http://127.0.0.1:8000) | `http://127.0.0.1:8000` |

### `runweave approvals reject`

Show a request, confirm, then reject it

| Argument | What it does | Default |
| --- | --- | --- |
| `id` | The approval request id |  |
| `--yes` | Decide without a prompt |  |
| `--server` `SERVER` | RunWeave to talk to (http://127.0.0.1:8000) | `http://127.0.0.1:8000` |

### `runweave serve-agent`

Serve Python agents over the RunWeave HTTP agent contract

| Argument | What it does | Default |
| --- | --- | --- |
| `MODULE:ATTRIBUTE` | Python @agent functions to serve |  |
| `--port` `PORT` | 0 picks a free port | `9100` |

<!-- /generated:commands -->
