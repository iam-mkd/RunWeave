# word-count agent

A RunWeave project created by `runweave init`.

```bash
runweave eval                    # run datasets/smoke.jsonl, offline, no API key
runweave eval --save-baseline    # keep this result to catch regressions later
runweave serve --agent agent:run # open the workspace at http://127.0.0.1:8000
```

Edit `agent.py`, add cases to `datasets/smoke.jsonl`, and set limits, proxy mode and
fault profiles in `runweave.toml`. Reports are written to `.runweave/reports/`.
