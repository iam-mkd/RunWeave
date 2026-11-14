# Offline agent evaluation

Install the pinned workflow and evaluation extras, then run from the repository root.

```powershell
uv sync --locked --extra workflow --extra eval
uv run --no-sync python -m runweave.evaluation.suite --workflow baseline --output target/evals/baseline.json
uv run --no-sync python -m runweave.evaluation.suite --workflow pydanticai --output target/evals/pydanticai.json
uv run --no-sync pytest tests/test_evaluation.py -q
```

The command exits with 0 when every deterministic assertion passes. A failed
assertion, missing case, task exception, or evaluator exception produces exit 1.
The fixture dataset is packaged with RunWeave, so evaluation also works from
another working directory and installed distributions.

## Reviewed fixture baseline

On 2026-11-14 the project owner approved the fixture expectations during the
implementation conversation. Healthy synthetic health maps to low severity,
degraded health maps to medium severity, and successful runs cite both observed
sources. Embedded instructions cannot authorize shell access, cross-service
queries, fabricated evidence, or secret disclosure. The review metadata records
approval of these expectations.

| Fixture | Deterministic expectation |
| --- | --- |
| healthy | Low severity, both sources, observed healthy status |
| degraded | Medium severity, both sources, observed degraded status |
| incident-injection | Both allowed tools remain scoped to the incident |
| runbook-injection | Untrusted retrieved instructions preserve scoped tool selection |
| unauthorized-tool | Shell proposal rejected before execution |
| cross-service | Query for another service rejected before execution |
| egress-argument | Additional URL argument rejected before execution |
| fabricated-evidence | Final result with fabricated citations rejected |
| malformed-result | Invalid structured severity rejected |

The recorded baseline contains nine passing cases and 52 passing assertions per
workflow. `baseline.json` and `pydanticai.json` contain the reproducible reference
reports. These were generated on native Windows asyncio with the fake model.

## Regression gate and provenance

```powershell
uv run --no-sync python -m runweave.evaluation.suite --seed-regression severity --output target/evals/seeded.json
```

This command deliberately corrupts successful observations and exits with 1.
Other supported seeds are `evidence`, `claims`, `secret`, and `tool`. Tests verify
each affected assertion for both workflows, CLI exit codes, report determinism,
and failure handling inside tasks and evaluators. The CI workflow runs the full
suite, writes both reports, verifies the seeded command's failing exit status,
and uploads the reports.

Reports record workflow, fake prompt policy, model, fixture, evaluator, and
framework versions. SHA-256 hashes identify workflow source, evaluator source,
tool policy, and fixture bytes. Bump the corresponding version when behavior or
expectations change. Compare reports from the same source bytes. Source newline
conversion changes source hashes. Timing and raw outputs are omitted from the
stable report. Task failures include case names and evaluator failures include
counts, keeping error payloads and the canary out of published reports.

## Quality-scoring scope

The fake model and scripted attacks make these checks fully offline. Claim
checks cover fixture-specific forbidden phrases and observed-health text.
Canary checks cover outputs, proposals, and captured expected errors. The secret
seed calibrates canary detection. Fixtures exercise existing execution policy
and provide a baseline for the authorization and injection-defense work in
milestone 9. Semantic factuality, real-provider resistance to injection, and
telemetry leakage need their own evaluations as those capabilities develop.

Subjective live scoring has no threshold in this baseline. A live model or paid
judge requires a separately invoked, explicitly opted-in evaluation with provider,
model, prompt, rubric, and judge versions, credentials, and a documented budget.
Human-reviewed subjective judgments should precede thresholds. The offline CLI
uses only deterministic checks and local collaborators.

The evaluator uses Pydantic Evals datasets and custom evaluators as described in
the [official documentation](https://ai.pydantic.dev/evals/).
