# RunWeave in GitHub Actions

[`runweave.yml`](runweave.yml) tests the agent in a `runweave init` project on every pull
request: `runweave eval` against the dataset and a committed baseline (JUnit report), then a
short `runweave bench`. It needs no API key; model calls get deterministic mock replies.

Setup, regression gating and the pre-release install line are in
[Test your agent: In CI](../../docs/testing.md#9-in-ci). `tests/test_release.py` runs these
exact commands and checks that a seeded regression fails the job.
