"""Native Uvicorn TCP startup, API and graceful-shutdown verification, offline model only."""

import argparse
import asyncio
import json
import platform
import socket
from pathlib import Path

import httpx
import uvicorn

from runweave.app import create_app
from runweave.core.settings import Settings


async def check() -> dict[str, object]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="fake",
        auth_mode="local",
        database_url=None,
        worker_count=8,
        mock_runbook_delay_seconds=0.2,
    )
    app = create_app(settings)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            async with asyncio.timeout(15):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Server did not start")
                    await asyncio.sleep(0.01)
            assert (await client.get("/health/ready")).status_code == 200
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/v1/runs",
                        json={"service": "payments", "description": "Native TCP fixture"},
                    )
                    for _ in range(8)
                ]
            )
            assert all(r.status_code == 202 for r in responses)
            locations = [r.headers["Location"] for r in responses]
            cancelled = await client.post(locations[-1] + "/cancel")
            assert cancelled.json()["status"] == "CANCELLED"
            async with asyncio.timeout(15):
                while True:
                    states = [(await client.get(url)).json()["status"] for url in locations]
                    if all(s in {"SUCCEEDED", "CANCELLED"} for s in states):
                        break
                    await asyncio.sleep(0.02)
            assert states.count("SUCCEEDED") == 7 and states.count("CANCELLED") == 1
            assert (await client.get("/metrics")).status_code == 200
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            listener.close()
    assert app.state.runtime is None
    assert not any(t.get_name().startswith("runweave-") for t in asyncio.all_tasks())
    return {
        "passed": True,
        "transport": "real loopback TCP and Uvicorn; client shares process",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "admitted": 8,
        "succeeded": 7,
        "cancelled": 1,
        "graceful_shutdown": True,
        "paid_model_calls": 0,
        "bind_address": "127.0.0.1",
        "workers": 8,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
