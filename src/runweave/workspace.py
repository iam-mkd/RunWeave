import sys
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from runweave.agents.catalog import RegisteredAgent
from runweave.agents.provider_wire import Provider
from runweave.app import create_app
from runweave.core.settings import Settings


class LocalBrowserBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            host = headers.get(b"host", b"")
            origin = headers.get(b"origin")
            document_navigation = (
                scope["method"] == "GET"
                and scope["path"] == "/"
                and headers.get(b"sec-fetch-mode") == b"navigate"
                and headers.get(b"sec-fetch-dest") == b"document"
            )
            if (origin is not None and origin != b"http://" + host) or (
                headers.get(b"sec-fetch-site") in (b"cross-site", b"same-site")
                and not document_navigation
            ):
                await JSONResponse({"detail": "Use the local RunWeave origin"}, 403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_workspace(
    config: Settings, assets: Path, *, agents: Sequence[RegisteredAgent] | None = None
) -> FastAPI:
    if config.auth_mode != "local":
        raise ValueError("The local workspace requires RUNWEAVE_AUTH_MODE=local")
    if not (assets / "index.html").is_file() or not (assets / "assets").is_dir():
        raise ValueError("Build the workspace first with npm --prefix frontend run build")

    app = create_app(config, agents=agents)
    app.add_middleware(LocalBrowserBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
    app.mount("/assets", StaticFiles(directory=assets / "assets"), name="workspace-assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            assets / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
            },
        )

    @app.get("/v1/workspace")
    async def workspace() -> dict[str, Provider | str | bool]:
        return {
            "provider": config.model_provider,
            "model": config.selected_model,
            "workflow": config.agent_workflow,
            "durable": config.database_url is not None,
        }

    return app


def main() -> None:
    """Deprecated alias for `runweave serve`."""
    from runweave.cli import main as cli_main

    cli_main(["serve", *sys.argv[1:]])


if __name__ == "__main__":
    main()
