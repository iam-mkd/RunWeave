# Dependency license audit

RunWeave is licensed under Apache-2.0. This audit lists the licenses of its
dependencies so the project and later binary distributions (milestone 25) stay
compatible. It was produced on 2026-11-18 from package metadata in a clean
Python 3.11 environment (`uv sync --locked --extra workflow --extra eval`, which
includes the dev group), `requirements-durable.txt`,
`sandbox/requirements-browser.txt`, and `frontend/package-lock.json`.

Refresh it whenever dependencies change and before every release.

## Summary

- No GPL, LGPL, or AGPL dependencies were found.
- All dependencies are permissive (MIT, BSD, ISC, Apache-2.0, PSF-2.0) except
  two under MPL-2.0: `certifi` (runtime, through HTTPX) and `pathspec`
  (development only, through mypy). MPL-2.0 is file-level copyleft; unmodified
  use is compatible with Apache-2.0. Binary distributions must include the
  MPL-2.0 license text and point to the source of those files.
- Binary distributions (wheels with bundled UI, the desktop app, container
  images) must ship the third-party license texts of everything they bundle.

## Python runtime dependencies

| Package | License |
| --- | --- |
| fastapi, starlette | MIT; BSD-3-Clause |
| uvicorn, h11, click, colorama | BSD-3-Clause; MIT; BSD-3-Clause; BSD |
| httpx, httpcore, anyio, idna, certifi | BSD-3-Clause; BSD-3-Clause; MIT; BSD-3-Clause; MPL-2.0 |
| pydantic, pydantic-core, pydantic-settings, annotated-types, typing-inspection, python-dotenv | MIT (python-dotenv: BSD-3-Clause) |
| opentelemetry-api, opentelemetry-sdk, opentelemetry-semantic-conventions | Apache-2.0 |
| pyjwt | MIT |
| jsonschema, jsonschema-specifications, referencing, rpds-py, attrs | MIT |
| pyyaml | MIT |
| psutil | BSD-3-Clause |
| keyring, jaraco.classes, jaraco.context, jaraco.functools, more-itertools | MIT |
| keyring on Windows: pywin32-ctypes | BSD-3-Clause |
| keyring on Linux: SecretStorage, jeepney, cryptography, cffi, pycparser | BSD-3-Clause; MIT; Apache-2.0 OR BSD-3-Clause; MIT-0; BSD-3-Clause |
| keyring on older Pythons: importlib-metadata, zipp, backports.tarfile | Apache-2.0; MIT; MIT |
| sqlalchemy, greenlet | MIT; MIT AND PSF-2.0 |
| typing-extensions | PSF-2.0 |

## Optional extras

| Extra | Packages | License |
| --- | --- | --- |
| `workflow` | pydantic-ai-slim, pydantic-graph, genai-prices, logfire-api, griffelib | MIT (griffelib: ISC) |
| `eval` | pydantic-evals, rich, markdown-it-py, mdurl, pygments, pyyaml | MIT (pygments: BSD-2-Clause) |
| durable (`requirements-durable.txt`) | asyncpg, alembic | Apache-2.0; MIT |
| browser sandbox image | playwright, pyee, greenlet | Apache-2.0; MIT; MIT AND PSF-2.0 |

## Development tooling (not distributed)

pytest, pytest-asyncio, pluggy, iniconfig, packaging, mypy, mypy-extensions,
librt, pathspec, ruff, truststore, types-jsonschema, types-pyyaml, and the official model
SDKs used by the proxy compatibility tests: openai and anthropic (Apache-2.0), with
jiter, docstring-parser and sniffio (MIT; sniffio MIT OR Apache-2.0). Licenses: MIT, Apache-2.0,
BSD-3-Clause, and MPL-2.0 (pathspec).

## Frontend (`frontend/package-lock.json`)

| Package | Scope | License |
| --- | --- | --- |
| react, react-dom, scheduler | Bundled into the built UI | MIT |
| vite, rollup, esbuild, postcss, nanoid, picocolors, fdir, picomatch, tinyglobby, source-map-js, @types/estree | Build tooling | MIT (picocolors: ISC; source-map-js: BSD-3-Clause) |
| playwright, playwright-core | Browser smoke tests | Apache-2.0 |
| axe-core | Accessibility audit in the browser smoke test (never shipped) | MPL-2.0 |

## TypeScript SDK (`sdk/typescript/package-lock.json`)

The SDK has no runtime dependencies. Its build tooling is not distributed.

| Package | Scope | License |
| --- | --- | --- |
| typescript (and its platform binaries) | Build tooling | Apache-2.0 |
| @types/node, undici-types | Type declarations | MIT |

## Proxy compatibility tests (`tests/proxy_compat/package-lock.json`)

Test-only; not distributed.

| Package | License |
| --- | --- |
| openai, @anthropic-ai/sdk, @opentelemetry/api | Apache-2.0 |
| @babel/runtime, json-schema-to-ts, ts-algebra, standardwebhooks, @stablelib/base64 | MIT |
| fast-sha256 | Unlicense |

## Container base images

| Image | Used by | Notes |
| --- | --- | --- |
| `python:3.13-slim` | Service image and Python sandbox | Python is PSF-2.0; Debian packages carry their own licenses |
| `mcr.microsoft.com/playwright/python` | Browser sandbox | Playwright Apache-2.0 (`sandbox/PLAYWRIGHT-LICENSE`); Chromium BSD-3-Clause plus bundled third-party licenses; Ubuntu packages carry their own licenses |
| `ghcr.io/astral-sh/uv` | Build stage only | uv is MIT OR Apache-2.0; not present in the final image |

The desktop app in milestone 25 will bundle PostgreSQL (PostgreSQL License,
permissive) and a frozen Python runtime (PSF-2.0); add them here when it does.
