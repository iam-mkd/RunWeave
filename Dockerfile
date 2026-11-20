FROM node:24.22.1-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.17@sha256:03bdc89bb9798628846e60c3a9ad19006c8c3c724ccd2985a33145c039a0577b AS uv
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --locked --no-dev --extra durable --extra workflow --no-install-project
COPY src ./src
COPY scripts/bundle-workspace-ui.py ./scripts/
COPY --from=ui /ui/dist ./frontend/dist
RUN python scripts/bundle-workspace-ui.py \
    && uv sync --locked --no-dev --extra durable --extra workflow --no-editable

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
# Provider keys come from the environment; there is no OS credential store in the image.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 RUNWEAVE_KEY_STORE=none
USER 10001:10001
EXPOSE 8000
CMD ["runweave", "serve", "--container", "--port", "8000"]
