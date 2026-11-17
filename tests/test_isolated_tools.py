import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from runweave.app import create_app
from runweave.core.settings import Settings
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy, PolicyDenied
from runweave.tools.browser_fixtures import ORIGIN, FixtureBroker, validate_fixture_url
from runweave.tools.container_runtime import ContainerPolicy, ContainerRuntime, IsolationError
from runweave.tools.isolated import BrowserRead, IsolatedTools, PythonProgram


@pytest.mark.parametrize(
    "program",
    [
        "import os",
        "result=open('/etc/passwd').read()",
        "result=(1).__class__",
        "result=eval('1')",
        "result=__builtins__",
        "while True:\n import socket",
        "result=input_data.format()",
        "sum=1",
        "def f(): return 1",
        "result=[x for x in range(1) if __import__('os')]",
    ],
)
def test_python_surface_rejects_io_reflection_and_imports(program: str) -> None:
    with pytest.raises(ValidationError):
        PythonProgram(code=program)


def test_python_accepts_bounded_data_processing_and_rejects_oversized_data() -> None:
    assert PythonProgram(code="result=sum(x*x for x in input_data)", data=[1, 2, 3]).data == [
        1,
        2,
        3,
    ]
    with pytest.raises(ValidationError):
        PythonProgram(code="result=input_data", data="x" * 17000)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "https://blocked.invalid/",
        ORIGIN + "/../private",
        ORIGIN + "/%2e%2e/private",
        ORIGIN + "/?tenant=other",
        "https://user:pass@fixtures.runweave.invalid/",
        ORIGIN + "/write",
    ],
)
def test_browser_url_policy_is_exact_and_closed(url: str) -> None:
    with pytest.raises(ValueError):
        validate_fixture_url(url, (ORIGIN,))


def test_browser_request_cannot_authorize_writes_or_supply_credentials() -> None:
    for field in ("method", "approval_id", "credentials", "script", "headers"):
        with pytest.raises(ValidationError):
            BrowserRead.model_validate({"url": ORIGIN + "/", field: "forged"})


async def test_broker_keeps_credentials_on_host_and_scopes_private_fixture() -> None:
    secret = "synthetic-broker-canary"
    broker = FixtureBroker(secret)
    public = await broker.bundle("a", private=False)
    assert ORIGIN + "/private" not in public
    private = await broker.bundle("a", private=True)
    assert "tenant a" in str(private[ORIGIN + "/private"])
    assert secret not in json.dumps(private)
    assert list(broker.authorized_tenants) == ["a"]
    assert broker.response(httpx.Request("POST", ORIGIN + "/private")).status_code == 403


@pytest.mark.parametrize("browser", [False, True])
def test_container_policy_has_no_mounts_network_or_privileges(browser: bool) -> None:
    args = ContainerPolicy("sha256:" + "a" * 64, browser).arguments("owned", "owner")
    assert "--network=none" in args and "--read-only" in args and "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges=true" in args
    assert not any(
        flag in args
        for flag in ("--privileged", "--mount", "-v", "--volume", "--env-file", "--device")
    )


async def test_disabled_tools_never_contact_docker_and_api_limits_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Docker must not be used while tools are disabled")

    monkeypatch.setattr("runweave.tools.container_runtime.ContainerRuntime.command", forbidden)
    app = create_app(Settings(worker_count=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (
                await client.post("/v1/tools/python", json={"code": "result=1"})
            ).status_code == 503
            assert (
                await client.post("/v1/tools/browser", json={"url": ORIGIN + "/"})
            ).status_code == 503
            assert (await client.post("/v1/tools/python", content=b"x" * 17000)).status_code == 413
            assert (await client.get("/health/ready")).status_code == 200


async def test_policy_cannot_enable_without_immutable_images_and_origins() -> None:
    with pytest.raises(ValidationError):
        Settings(isolated_tools_enabled=True)
    with pytest.raises(ValidationError):
        Settings(sandbox_python_image="python:latest")
    with pytest.raises(ValidationError):
        Settings(browser_allowed_origins=("https://arbitrary.invalid",))  # type: ignore[arg-type]


async def test_scope_and_content_rejections_precede_container_execution() -> None:
    tools = IsolatedTools(Settings(), ContentPolicy(("canary-secret",)))
    restricted = LOCAL_PRINCIPAL.model_copy(update={"scopes": frozenset()})
    with pytest.raises(PolicyDenied):
        await tools.python(PythonProgram(code="result=1"), restricted)
    with pytest.raises(PolicyDenied):
        await tools.python(PythonProgram(code="result='canary-secret'"), LOCAL_PRINCIPAL)
    with pytest.raises(IsolationError):
        await tools.browser(BrowserRead(url="https://blocked.invalid/"), LOCAL_PRINCIPAL)
    assert tools.runtime.names == set()
    await tools.close()


async def test_cleanup_refuses_foreign_ownership_and_unknown_daemon_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for responses in ([(0, b"foreign-owner")], [(1, b""), (1, b"")]):
        runtime = ContainerRuntime()
        command = AsyncMock(side_effect=responses)
        monkeypatch.setattr(runtime, "command", command)
        with pytest.raises(IsolationError):
            await runtime.remove("runweave-isolated-test")
        assert not runtime.healthy
        assert not any(call.args[0] == "rm" for call in command.call_args_list)


def configured() -> Settings:
    return Settings(
        isolated_tools_enabled=True,
        sandbox_python_image="sha256:" + "a" * 64,
        sandbox_browser_image="sha256:" + "b" * 64,
        browser_allowed_origins=(ORIGIN,),
        isolated_tools_concurrency=1,
    )


async def test_failed_verification_never_enables_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(tools: IsolatedTools) -> dict[str, object]:
        raise IsolationError("SANDBOX_ISOLATION_FAILED")

    monkeypatch.setattr("runweave.tools.isolation_checks.verify_runtime", reject)
    tools = IsolatedTools(configured(), ContentPolicy())
    with pytest.raises(IsolationError):
        await tools.start()
    assert not tools.ready and tools.proof == {}
    await tools.close()


async def test_admission_is_bounded_and_shutdown_awaits_owned_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = IsolatedTools(configured(), ContentPolicy())
    tools.verified = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(*args: object, **kwargs: object) -> tuple[int, bytes]:
        entered.set()
        await release.wait()
        return 0, b'{"ok":true,"result":1}'

    monkeypatch.setattr(tools.runtime, "run", block)
    one = asyncio.create_task(tools.python(PythonProgram(code="result=1"), LOCAL_PRINCIPAL))
    await entered.wait()
    two = asyncio.create_task(tools.python(PythonProgram(code="result=1"), LOCAL_PRINCIPAL))
    await asyncio.sleep(0)
    try:
        with pytest.raises(IsolationError) as error:
            await tools.python(PythonProgram(code="result=1"), LOCAL_PRINCIPAL)
        assert error.value.code == "ISOLATED_TOOLS_BUSY"
    finally:
        await tools.close()
    results = await asyncio.gather(one, two, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not tools.tasks and not tools.ready


async def test_uncertain_cleanup_fails_readiness_and_rejects_new_work() -> None:
    app = create_app(Settings(worker_count=1))
    async with app.router.lifespan_context(app):
        isolated = app.state.runtime.isolated
        isolated.settings = configured()
        isolated.verified = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
            isolated.runtime.healthy = False
            assert (await client.get("/health/ready")).status_code == 503
            assert (
                await client.post("/v1/tools/python", json={"code": "result=1"})
            ).status_code == 503
