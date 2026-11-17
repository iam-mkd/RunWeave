import asyncio
import json
from importlib.resources import files
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from runweave.core.settings import Settings
from runweave.security import ContentPolicy, PolicyDenied, Principal
from runweave.tools.browser_fixtures import ORIGIN, FixtureBroker, validate_fixture_url
from runweave.tools.container_runtime import ContainerPolicy, ContainerRuntime, IsolationError
from runweave.tools.python_policy import validate_program


class PythonProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(min_length=1, max_length=2048)
    data: JsonValue = None

    @model_validator(mode="after")
    def bounded(self) -> "PythonProgram":
        validate_program(self.code)
        if len(self.model_dump_json().encode()) > 16384:
            raise ValueError("PYTHON_INPUT_LIMIT")
        return self


class BrowserRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    url: str = Field(min_length=1, max_length=512)


class IsolatedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: str
    kind: Literal["python", "browser"]
    result: JsonValue
    policy_version: Literal["isolated-tools-v1"] = "isolated-tools-v1"


class IsolatedTools:
    def __init__(self, settings: Settings, content_policy: ContentPolicy) -> None:
        self.settings, self.content_policy = settings, content_policy
        self.runtime = ContainerRuntime()
        self.broker = FixtureBroker(settings.sandbox_fixture_credential.get_secret_value())
        self.slots = asyncio.Semaphore(settings.isolated_tools_concurrency)
        self.tasks: set[asyncio.Task[tuple[int, bytes]]] = set()
        self.verified = False
        self.closing = False
        self.proof: dict[str, object] = {}

    @property
    def ready(self) -> bool:
        return (
            self.settings.isolated_tools_enabled
            and self.verified
            and self.runtime.healthy
            and not self.closing
        )

    def policy(self, browser: bool = False) -> ContainerPolicy:
        profile = str(files("runweave").joinpath("sandbox_assets/browser-seccomp.json"))
        return ContainerPolicy(
            self.settings.sandbox_browser_image if browser else self.settings.sandbox_python_image,
            browser,
            profile if browser else None,
        )

    def _authorize(self, principal: Principal, scope: str) -> None:
        principal.require(scope)
        if (principal.tenant, principal.subject) in self.settings.auth_revoked_subjects:
            raise PolicyDenied()

    async def _execute(
        self, policy: ContainerPolicy, payload: dict[str, object], principal: Principal, scope: str
    ) -> tuple[int, bytes]:
        async with self.slots:
            self._authorize(principal, scope)
            if not self.ready:
                raise IsolationError("ISOLATED_TOOLS_UNAVAILABLE")
            return await self.runtime.run(policy, payload)

    async def _run(
        self, policy: ContainerPolicy, payload: dict[str, object], principal: Principal, scope: str
    ) -> dict[str, object]:
        if not self.ready:
            raise IsolationError("ISOLATED_TOOLS_UNAVAILABLE")
        if len(self.tasks) >= self.settings.isolated_tools_concurrency * 2:
            raise IsolationError("ISOLATED_TOOLS_BUSY")
        task = asyncio.create_task(self._execute(policy, payload, principal, scope))
        self.tasks.add(task)
        try:
            code, raw = await task
            if code:
                raise IsolationError("ISOLATED_WORKER_FAILED")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise IsolationError("ISOLATED_PROTOCOL_INVALID") from None
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise IsolationError("ISOLATED_EXECUTION_REJECTED")
            self.content_policy.check(raw.decode())
            return result
        finally:
            self.tasks.discard(task)

    async def python(self, request: PythonProgram, principal: Principal) -> IsolatedResult:
        self._authorize(principal, "tools:python_sandbox")
        self.content_policy.check(request.model_dump_json())
        response = await self._run(
            self.policy(), request.model_dump(mode="json"), principal, "tools:python_sandbox"
        )
        return IsolatedResult(
            session_id=uuid4().hex, kind="python", result=cast(JsonValue, response.get("result"))
        )

    async def browser(self, request: BrowserRead, principal: Principal) -> IsolatedResult:
        self._authorize(principal, "tools:browser_read")
        self.content_policy.check(request.model_dump_json())
        try:
            validate_fixture_url(request.url, self.settings.browser_allowed_origins)
        except ValueError:
            raise IsolationError("BROWSER_ORIGIN_DENIED") from None
        private = request.url == ORIGIN + "/private"
        if private:
            self._authorize(principal, "tools:browser_private")
            if not self.settings.sandbox_fixture_credential.get_secret_value():
                raise IsolationError("BROWSER_CREDENTIAL_UNAVAILABLE")
        if not self.ready:
            raise IsolationError("ISOLATED_TOOLS_UNAVAILABLE")
        resources = await self.broker.bundle(principal.tenant, private=private)
        response = await self._run(
            self.policy(True),
            {
                "url": request.url,
                "origins": list(self.settings.browser_allowed_origins),
                "resources": resources,
            },
            principal,
            "tools:browser_private" if private else "tools:browser_read",
        )
        response.pop("ok", None)
        return IsolatedResult(
            session_id=uuid4().hex, kind="browser", result=cast(JsonValue, response)
        )

    async def start(self) -> None:
        if not self.settings.isolated_tools_enabled:
            return
        from runweave.tools.isolation_checks import verify_runtime

        self.proof = await verify_runtime(self)
        self.verified = True

    async def close(self) -> None:
        self.closing = True
        for task in tuple(self.tasks):
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        await self.runtime.close()
