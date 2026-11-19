# word-count agent (TypeScript)

A RunWeave project created by `runweave init --language typescript`.

```bash
npm install                      # installs the bundled @runweave/agent SDK
runweave eval                    # starts agent.mjs, runs datasets/smoke.jsonl, stops it
runweave eval --save-baseline    # keep this result to catch regressions later
```

`vendor/runweave-agent` is the RunWeave TypeScript SDK, copied here because it is not
published to npm yet. Edit `agent.mjs` and `datasets/smoke.jsonl`; settings live in
`runweave.toml`.
