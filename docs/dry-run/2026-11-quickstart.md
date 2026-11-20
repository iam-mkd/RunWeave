# Recorded dry run: a newcomer follows the quickstart (2026-11-20)

Milestone 26 is done when "a recorded dry run shows a new developer completing the
quickstart from the docs alone". This is that recording.

**How it was run.** A fresh Claude agent with no knowledge of the repository played the
new developer. It got only a copy of `docs/` (at commit `a00a262`) and an empty folder. It
was told not to read the maintainers' checkout, to get RunWeave only as the quickstart says
(a clone of the public GitHub repository, at `e6edbfd`), and to record every stumble. It had
no API key. It worked through a terminal, so it could not click in a browser; step 5 says
what it did instead.

The report below is the agent's, unchanged except that local folder paths, which contain the
machine's user name, are shortened to `<work>` and `<scratch>`. The [follow-up](#follow-up)
at the end lists what was changed because of each stumble.

---

## Machine
- OS: Windows 11 Home Single Language, 10.0.26300, 16 logical CPUs, about 15.8 GiB RAM.
- Git 2.51.0.windows.1; uv 0.11.17; Node.js v24.22.1 (npm 12.2.1).
- Python that uv used: CPython 3.13.9 (an existing Anaconda install) for the clone's
  `uv sync`, and CPython 3.15.2 (an existing system install) for the example's `uv venv`.
- Shell: Git Bash. Port 8000 was free before starting.

## Steps

| # | Command (folder) | Time | Exit | Key output |
|---|---|---|---|---|
| 1a | `git clone https://github.com/iam-mkd/RunWeave.git` (work) | a few seconds (not timed) | 0 | Clone HEAD `e6edbfd` |
| 1b | `uv sync` (RunWeave) | 10.9 s | 0 | Used CPython 3.13.9, created `.venv`, built runweave, installed 89 packages |
| 1c | `npm --prefix frontend ci` | 4.1 s | 0 | 48 packages, 0 vulnerabilities. Warning: an `esbuild` install script was blocked (not covered by `allowScripts`) |
| 1d | `npm --prefix frontend run build` | 2.9 s | 0 | Vite built `frontend/dist`, despite the blocked script |
| 2 | `uv run runweave init my-agent` (RunWeave) | 2.9 s | 0 | "Created a python project in my-agent (5 files). Next: cd my-agent / runweave eval" |
| 3 | `uv run runweave eval` (my-agent) | 8.3 s | 0 | "word-count on smoke: 4/4 cases passed (100%), p95 3.9 ms, 0 tokens, $0", "Gates passed.", report folder with `junit.xml`, `report.json`, `report.md` |
| 4 | `uv run runweave bench` (my-agent) | 5.0 s | 0 | Concurrency 1/4/16: 189 / 195 / 199 runs/s, p95 1.3 / 2.5 / 6.7 ms, 0/32 failed each, "Gates passed." |
| 5 | `uv run runweave serve --agent agent:run` (my-agent, background) | `GET /` answered after about 4.7 s | stopped | "Uvicorn running on http://127.0.0.1:8000"; `GET /` served the RunWeave Workspace page |

Command time for steps 1 to 5 was about 40 s. The whole quickstart, reading included, took
about three minutes.

## Workspace
The agent could not click in a browser, so it did step 5 through the documented HTTP API
(found in `agents.md` and `testing.md`; the quickstart did not mention it):

- `GET /` returned 200 with `<title>RunWeave Workspace</title>`; the JavaScript bundle was
  served too. The page was not seen rendering.
- `GET /v1/agents` listed `word-count` (Python, 0.1.0, input `text`: 1 to 5000 characters)
  and the built-in `incident-triage` agent.
- `POST /v1/runs` with `{"agent": "word-count", "input": {"text": "RunWeave tests agents"}}`
  returned 202. The run SUCCEEDED with `{"words": 3, "longest": "RunWeave"}`, exactly as the
  page says, with the steps "Split words" and "Find the longest word".
- The Evals and Load tests screens' API: `POST /v1/jobs` with `eval`, then with `bench`,
  both completed and passed, and `GET /v1/reports` listed all four reports.
- The server was stopped by ending the process rather than with Ctrl+C, so graceful shutdown
  was not observed.

## Example
`examples/plain-python`, following its README:

| Command | Time | Exit | Output |
|---|---|---|---|
| `uv venv` | 0.1 s | 0 | CPython 3.15.2 |
| `uv pip install ../.. -r pyproject.toml` | 11.0 s | 0 | 44 packages, RunWeave from the clone, `openai==3.57.0` |
| `uv run --no-sync runweave eval` | 11.6 s | 0 | "plain-python-weather on smoke: 4/4 cases passed (100%), p95 678.1 ms, 703 tokens, $0", "Gates passed." |

What a newcomer might type instead: the examples page's bare `runweave eval` failed with
"command not found" (exit 127), and `uv run` without `--no-sync` failed with "runweave was
not found in the package registry".

## Stumbles
1. **The examples page gave a command that does not work as written:** "from its folder:
   `runweave eval`" fails with "command not found"; the README's `uv run --no-sync runweave
   eval` works.
2. **`runweave init` printed a bare `runweave eval`** as the next command, while the
   quickstart uses `uv run runweave eval`.
3. **`--no-sync` was unexplained,** and leaving it out gives a confusing resolver error.
4. **An unexplained npm warning** that an `esbuild` install script was blocked, although
   the build succeeded.
5. **uv picked different Pythons silently** (an existing 3.13 and an existing 3.15), not a
   download, while the docs say "uv downloads a suitable Python". Which versions are tested
   was not stated.
6. **`init` created five files;** the quickstart listed three.
7. **The sample report path** was relative; the real one is a long absolute path.
8. **Bench's console output** shows only runs/s, p95 and failures; the other metrics the
   page lists are in `report.md`, which that section did not mention.
9. **The Agents list also has `incident-triage`,** which the quickstart did not mention.
10. **The workspace API was not linked from the quickstart,** and `testing.md` did not show
   that `GET /v1/jobs/{id}` wraps its answer as `{"job": ..., "events": [...]}`.
11. **Ctrl+C could not be tried** from a terminal tool.
12. Covered by 1.

## Result
- **Quickstart completed from the docs alone: yes.** Steps 1 to 4 ran exactly as written,
  every command exited 0, and the output matched the page. In step 5 the server served the
  workspace, and a run, an eval job and a bench job succeeded through the API the workspace
  uses. Clicking through the UI was not possible.
- **Example:** `plain-python` installed and passed 4/4 with its README's commands.
- **Time:** about 40 s of commands for the quickstart, about three minutes in all.
- **Nothing left running:** the server's process was gone and port 8000 was free.

---

## Follow-up

Changed in the same pull request as this recording:

| Stumble | Change |
| --- | --- |
| 1 | [`examples.md`](../examples.md) gives the README's exact commands for the Python examples and `uv run runweave ...` for TypeScript; the TypeScript README uses `uv run` too. A test fails if the examples page or an example README gives a bare `runweave` command |
| 2 | `runweave init` prints `uv run runweave eval` as the next step when it runs under `uv run` (tested) |
| 3 | `examples.md` and each Python example's README explain `--no-sync` and the error you get without it |
| 4 | The quickstart says the `esbuild` warning is harmless |
| 5 | The quickstart says uv uses a Python 3.11+ it finds or downloads, that CI tests 3.11 to 3.13, and how to pick one |
| 6, 7, 8, 9 | The quickstart lists all five files, shows a real report path, says where bench's other metrics are, and mentions `incident-triage` |
| 10 | The quickstart shows step 5 through the API, linking [submit and follow runs](../agents.md#3-submit-and-follow-runs); `testing.md` shows the job response's shape |
| 11 | Not a docs problem. Ctrl+C sends SIGINT, which the runner in `scripts/check-docs.py` sends on Linux and macOS in CI |
