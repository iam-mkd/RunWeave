import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from runweave.app import create_app
from runweave.core.runtime import Runtime
from runweave.core.settings import Settings
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy, PolicyDenied, Principal
from runweave.tools.browser_fixtures import ORIGIN
from runweave.tools.container_runtime import IsolationError
from runweave.tools.isolated import BrowserRead, IsolatedTools, PythonProgram

pytestmark = pytest.mark.asyncio(loop_scope="module")
ROOT = Path(__file__).resolve().parents[1]
CANARY = "synthetic-private-broker-canary"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_tools() -> AsyncIterator[IsolatedTools]:
    python_image, browser_image = (
        os.environ.get("TEST_SANDBOX_PYTHON_IMAGE"),
        os.environ.get("TEST_SANDBOX_BROWSER_IMAGE"),
    )
    if not python_image or not browser_image:
        pytest.skip(
            "Set TEST_SANDBOX_PYTHON_IMAGE and TEST_SANDBOX_BROWSER_IMAGE "
            "for native isolation checks"
        )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="fake",
        auth_mode="local",
        isolated_tools_enabled=True,
        sandbox_python_image=python_image,
        sandbox_browser_image=browser_image,
        browser_allowed_origins=(ORIGIN,),
        sandbox_fixture_credential=SecretStr(CANARY),
    )
    tools = IsolatedTools(settings, ContentPolicy((CANARY,)))
    try:
        await tools.start()
        output = ROOT / "target" / "isolation" / "native-proof.json"
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text, json.dumps(tools.proof, indent=2) + "\n", "utf-8"
        )
        yield tools
    finally:
        await tools.close()
        assert tools.runtime.names == set()


async def test_real_kernel_isolation_and_limits(native_tools: IsolatedTools) -> None:
    assert native_tools.ready and native_tools.proof["passed"]
    result = await native_tools.python(
        PythonProgram(code="result=sum(x*x for x in input_data)", data=[1, 2, 3]), LOCAL_PRINCIPAL
    )
    assert result.result == 14 and not native_tools.runtime.names


@pytest.mark.parametrize("program", ["while True: pass", "result='x'*5000", "result=[0]*100000000"])
async def test_cpu_memory_and_output_exhaustion_are_contained(
    native_tools: IsolatedTools, program: str
) -> None:
    with pytest.raises(IsolationError):
        await native_tools.python(PythonProgram(code=program), LOCAL_PRINCIPAL)
    assert not native_tools.runtime.names and native_tools.ready


async def test_worker_cannot_see_host_environment_or_host_paths(
    native_tools: IsolatedTools,
) -> None:
    os.environ["RW_HOST_ONLY_CANARY"] = CANARY
    try:
        code, raw = await native_tools.runtime.run(
            native_tools.policy(),
            {},
            probe=(
                "import os,pathlib,json;print(json.dumps(dict("
                "secret='RW_HOST_ONLY_CANARY' in os.environ,"
                "socket=pathlib.Path('/var/run/docker.sock').exists(),"
                "escape=pathlib.Path('/host/.env').exists())))"
            ),
        )
    finally:
        os.environ.pop("RW_HOST_ONLY_CANARY", None)
    assert code == 0 and json.loads(raw) == {"secret": False, "socket": False, "escape": False}
    assert not native_tools.runtime.names


async def test_browser_reads_controlled_page_without_disabling_chromium_sandbox(
    native_tools: IsolatedTools,
) -> None:
    result = await native_tools.browser(BrowserRead(url=ORIGIN + "/"), LOCAL_PRINCIPAL)
    assert isinstance(result.result, dict)
    assert result.result["text"] == "RunWeave browser fixture"
    assert not native_tools.runtime.names


@pytest.mark.parametrize("path", ["/egress", "/subresources"])
async def test_redirects_subresources_and_write_attempts_cannot_escape(
    native_tools: IsolatedTools, path: str
) -> None:
    result = await native_tools.browser(BrowserRead(url=ORIGIN + path), LOCAL_PRINCIPAL)
    assert isinstance(result.result, dict)
    assert isinstance(result.result["blocked"], int) and result.result["blocked"] > 0
    visited = result.result["visited"]
    assert isinstance(visited, list)
    assert all(str(url).startswith(ORIGIN + "/") for url in visited)
    with pytest.raises(IsolationError):
        await native_tools.browser(BrowserRead(url=ORIGIN + "/redirect"), LOCAL_PRINCIPAL)


async def test_fresh_sessions_cannot_share_cookies_or_storage(native_tools: IsolatedTools) -> None:
    one = await native_tools.browser(BrowserRead(url=ORIGIN + "/state/set"), LOCAL_PRINCIPAL)
    two = await native_tools.browser(BrowserRead(url=ORIGIN + "/state/read"), LOCAL_PRINCIPAL)
    assert one.session_id != two.session_id
    assert "private" not in str(two.result) and "empty" in str(two.result)


async def test_malicious_page_remains_untrusted_text(native_tools: IsolatedTools) -> None:
    result = await native_tools.browser(BrowserRead(url=ORIGIN + "/malicious"), LOCAL_PRINCIPAL)
    assert "invoke shell" in str(result.result)
    assert not native_tools.runtime.names


async def test_private_fixture_uses_only_scoped_host_broker_credentials(
    native_tools: IsolatedTools,
) -> None:
    alice = Principal(tenant="a", subject="alice", scopes=LOCAL_PRINCIPAL.scopes)
    bob = Principal(tenant="b", subject="bob", scopes=LOCAL_PRINCIPAL.scopes)
    for principal in (alice, bob):
        result = await native_tools.browser(BrowserRead(url=ORIGIN + "/private"), principal)
        assert f"tenant {principal.tenant}" in str(result.result) and CANARY not in str(
            result.result
        )
    restricted = alice.model_copy(update={"scopes": alice.scopes - {"tools:browser_private"}})
    with pytest.raises(PolicyDenied):
        await native_tools.browser(BrowserRead(url=ORIGIN + "/private"), restricted)


@pytest.mark.parametrize("phase", ["creation", "running"])
async def test_timeout_and_cancellation_remove_entire_container(
    native_tools: IsolatedTools,
    phase: str,
) -> None:
    with pytest.raises(IsolationError) as error:
        await native_tools.runtime.run(
            native_tools.policy(), {}, probe="import time;time.sleep(20)", seconds=0.2
        )
    assert error.value.code == "SANDBOX_TIMEOUT" and not native_tools.runtime.names
    task = asyncio.create_task(
        native_tools.browser(BrowserRead(url=ORIGIN + "/loop"), LOCAL_PRINCIPAL)
    )
    async with asyncio.timeout(8):
        while True:
            if native_tools.runtime.names:
                if phase == "creation":
                    break
                name = next(iter(native_tools.runtime.names))
                code, raw = await native_tools.runtime.command(
                    "container", "inspect", name, "--format", "{{.State.Running}}"
                )
                if code == 0 and raw.strip() == b"true":
                    break
            await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not native_tools.runtime.names
    code, raw = await native_tools.runtime.command(
        "container",
        "ls",
        "-a",
        "--filter",
        f"label=runweave.isolation={native_tools.runtime.owner}",
        "--format",
        "{{.ID}}",
    )
    assert code == 0 and not raw.strip()


async def test_native_tools_api_returns_validated_results(native_tools: IsolatedTools) -> None:
    app = create_app(Settings(worker_count=1))
    async with app.router.lifespan_context(app):
        runtime: Runtime = app.state.runtime
        app.state.runtime = replace(runtime, isolated=native_tools)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            python = await client.post(
                "/v1/tools/python", json={"code": "result=sum(input_data)", "data": [1, 2, 3]}
            )
            assert python.status_code == 200 and python.json()["result"] == 6
            browser = await client.post("/v1/tools/browser", json={"url": ORIGIN + "/"})
            assert browser.status_code == 200
            assert (
                await client.post("/v1/tools/browser", json={"url": ORIGIN + "/", "method": "POST"})
            ).status_code == 422
            assert (await client.get("/health/ready")).status_code == 200
