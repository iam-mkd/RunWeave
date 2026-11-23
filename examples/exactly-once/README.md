# Exactly once: a lost response after the write

The failure most fault injection misses. The downstream **processed the write**, but its
response never arrived: a timeout, a dropped connection, a crashed proxy. The agent retries.
Did it just create the ticket twice?

`tickets.py` has two agents that create a support ticket through `ctx.effect`, in an
in-memory ticket API that deduplicates by idempotency key, as most real APIs do:

| Agent | On a timeout | Under `lost-writes` |
| --- | --- | --- |
| `naive_agent` | Retries without an idempotency key | Writes twice; `exactly_once` fails |
| `safe_agent` | Retries with one key per order | The API returns the same ticket; passes |

The project uses the `lost-writes` fault profile with seed 0, which loses the response of each
run's first write: the write commits, then the agent gets `EffectResponseLost` (a
`TimeoutError`). No model is involved, so no proxy fixtures are needed.

## Run it

From this folder, with [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. RunWeave is
installed from this repository's checkout:

```bash
uv venv
uv pip install ../.. -r pyproject.toml
uv run --no-sync runweave eval                       # the safe agent: 4/4 passed
uv run --no-sync runweave eval --config naive.toml   # the naive agent: fails, on purpose
uv run --no-sync runweave bench --config naive.toml  # "0/12 failed, 12 wrote twice"
```

The naive agent's eval reports `exactly_once: wrote twice after a retry: create_ticket` for
each order. Its load test shows the trap: no run *failed*, yet every run wrote twice; the
`max_duplicate_writes = 0` gate is what fails it.

`--no-sync` keeps `uv run` from reinstalling this folder's `pyproject.toml` from the package
registry; the line before installed RunWeave from the clone.

## Files

| File | What it is |
| --- | --- |
| `tickets.py` | The ticket API and both agents |
| `runweave.toml` | The safe agent's project: `lost-writes` faults, the dataset, a bench gate |
| `naive.toml` | The same project for the naive agent |
| `datasets/orders.jsonl` | Three orders that must each create exactly one ticket, and a rejected input |
| `pyproject.toml` | Exactly pinned dependencies |

See [Effects and exactly-once writes](../../docs/agents.md#effects-and-exactly-once-writes).
