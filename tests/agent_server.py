"""Serve SDK agents over real loopback HTTP inside a test's event loop."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn

from runweave.sdk_http import create_agent_app


@asynccontextmanager
async def agent_server(*functions: Any, port: int = 0) -> AsyncIterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_agent_app(*functions), log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while True:
                if server.started:
                    break
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()
