# Dependency license audit

RunWeave is licensed under Apache-2.0. This audit lists the licenses of its
dependencies so the project and later binary distributions (milestone 25) stay
compatible. It was produced on 2026-11-18 from package metadata in a clean
Python 3.11 environment (`uv sync --locked --extra workflow --extra eval`, which
includes the dev group), `requirements-durable.txt`,
`sandbox/requirements-browser.txt`, and `frontend/package-lock.json`.

Refresh it whenever dependencies change and before every release.

## Summary

- No GPL, LGPL, or AGPL Python or npm dependencies were found. The desktop app's
  bundled PostgreSQL for Windows includes two LGPL-2.1 DLLs (libiconv, libintl), used
  unmodified and dynamically linked, and the PyInstaller bootloader is GPL-2.0 with an
  exception for frozen programs; see [Desktop service](#desktop-service-milestone-25).
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
| `durable` (also `requirements-durable.txt`) | asyncpg, alembic, mako, markupsafe | Apache-2.0; MIT; MIT; BSD-3-Clause |
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
| `node:24-slim` | Build stage only (the workspace UI) | Node.js is MIT; not present in the final image |
| `postgres:18-alpine` | `compose.yaml` | PostgreSQL License (permissive); Alpine packages carry their own licenses |

## Desktop service (milestone 25)

| Component | License | Notes |
| --- | --- | --- |
| PostgreSQL 18.7 binaries (zonky `embedded-postgres-binaries`) | PostgreSQL License; packaging Apache-2.0 | Bundled as an archive; the Windows build also carries ICU (Unicode License), OpenSSL (Apache-2.0), libxml2/libxslt (MIT), zlib, zstd, lz4 (BSD/zlib), libiconv/libintl (LGPL-2.1, dynamically linked DLLs, unmodified) and wxWidgets (wxWindows Library Licence) DLLs |
| PyInstaller 6.22.3 bootloader | GPL-2.0 with the bootloader exception | The exception allows distributing frozen programs under any license |
| Python runtime (frozen) | PSF-2.0 | |
| Tauri 2.12.5, tauri-plugin-dialog 2.8.4 and their Rust crates (`desktop/src-tauri/Cargo.lock`) | MIT OR Apache-2.0 (Tauri); crates mostly MIT/Apache-2.0 | Every crate and its license files are listed in `THIRD_PARTY_NOTICES.txt`, with a CycloneDX SBOM (`runweave-desktop-rust.cdx.json`) |
| Microsoft Edge WebView2 (Windows) | Microsoft software license | Not bundled: the installer downloads Microsoft's bootstrapper if WebView2 is missing |

Every desktop installer includes `THIRD_PARTY_NOTICES.txt`, which `scripts/third-party-notices.py` generates from what is bundled: the license files of each Python package, npm package and Rust crate, PostgreSQL's COPYRIGHT, and the standard texts (`scripts/licenses/`) of licenses that some packages declare without shipping a file.
