from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from runweave.agents.catalog import AgentCatalog, RegisteredAgent
from runweave.agents.contracts import AgentExecutor, AgentModel
from runweave.agents.fake import FakeAgentModel
from runweave.agents.http import (
    AgentUrlNotAllowed,
    HttpAgent,
    HttpAgentConnector,
    ManifestRejected,
    RetryPolicy,
)
from runweave.agents.providers import ProviderAgentModel
from runweave.agents.runner import AgentRunner
from runweave.agents.triage import TriageSteps
from runweave.core.settings import Settings
from runweave.orchestration.backend import MemoryBackend, RunBackend
from runweave.orchestration.durable import DurableScheduler
from runweave.orchestration.registry import RunRegistry
from runweave.orchestration.scheduler import Scheduler
from runweave.proxy.bindings import ProxyBindings
from runweave.security import ContentPolicy
from runweave.storage.engine import postgres_engine
from runweave.storage.postgres import PostgresRuns
from runweave.telemetry import Telemetry
from runweave.tools.fake import FakeHealthTool, fake_health_response
from runweave.tools.isolated import IsolatedTools
from runweave.tools.registry import ToolRegistry
from runweave.tools.retry import RetryingHealthTool


@dataclass(frozen=True)
class Runtime:
    client: httpx.AsyncClient
    runner: AgentExecutor
    scheduler: RunBackend
    telemetry: Telemetry
    content_policy: ContentPolicy
    isolated: IsolatedTools | None = None
    agents: AgentCatalog | None = None
    connector: HttpAgentConnector | None = None
    proxy: ProxyBindings | None = None


def _agent_mounts(settings: Settings) -> dict[str, httpx.AsyncBaseTransport]:
    real = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
        trust_env=False,
    )
    hosts = ["127.0.0.1", "localhost", "[::1]", *settings.http_agent_allowed_hosts]
    return {f"all://{host}": real for host in hosts}


async def connect_startup_agents(
    connector: HttpAgentConnector, urls: Sequence[str], *, limit: int
) -> list[HttpAgent]:
    """Discover configured HTTP agents; any failure stops startup with a clear message."""
    found: list[HttpAgent] = []
    for url in dict.fromkeys(urls):
        try:
            found.extend(await connector.connect(url, removable=False))
        except (ManifestRejected, AgentUrlNotAllowed) as error:
            code = getattr(error, "code", "AGENT_URL_NOT_ALLOWED")
            raise RuntimeError(f"HTTP agent {url}: {code}: {error}") from None
    if len(found) > limit:
        raise RuntimeError(f"At most {limit} HTTP agents can be registered")
    return found


@asynccontextmanager
async def open_runtime(
    settings: Settings,
    *,
    executor: AgentExecutor | None = None,
    mock_transport: httpx.AsyncBaseTransport | None = None,
    agents: Sequence[RegisteredAgent] = (),
    http_agents: Sequence[str] | None = None,
) -> AsyncIterator[Runtime]:
    mock_transport = mock_transport or httpx.MockTransport(fake_health_response)
    provider = settings.model_provider
    live = provider != "fake"
    async with httpx.AsyncClient(
        base_url="https://mock-tools.invalid",
        transport=None if live else mock_transport,
        # Offline mode sends everything to the mock except HTTP agents, which are real
        # processes on loopback (or explicitly allowed hosts).
        mounts={"https://mock-tools.invalid": mock_transport} if live else _agent_mounts(settings),
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        telemetry = Telemetry(json_logs=settings.json_logs, console_traces=settings.console_traces)
        content_policy = ContentPolicy(
            (
                settings.groq_api_key.get_secret_value(),
                settings.model_api_key.get_secret_value(),
                settings.auth_signing_key.get_secret_value(),
                settings.sandbox_fixture_credential.get_secret_value(),
                *settings.auth_test_identities.keys(),
                *(value.get_secret_value() for value in settings.protected_values),
            )
        )
        health_tool = RetryingHealthTool(
            FakeHealthTool(client),
            attempts=settings.tool_retry_attempts,
            base_delay=settings.tool_retry_base_delay_seconds,
            max_delay=settings.tool_retry_max_delay_seconds,
            telemetry=telemetry,
        )
        model: AgentModel = FakeAgentModel()
        if provider != "fake":
            groq = provider == "groq"
            model = ProviderAgentModel(
                client,
                settings.groq_api_key if groq else settings.model_api_key,
                provider=provider,
                model=settings.selected_model,
                concurrency=settings.groq_concurrency if groq else settings.model_concurrency,
                requests_per_minute=(
                    settings.groq_requests_per_minute
                    if groq
                    else settings.model_requests_per_minute
                ),
            )
        isolated = IsolatedTools(settings, content_policy)
        try:
            await isolated.start()
            runner: AgentExecutor
            if settings.agent_workflow == "coordinated":
                from runweave.agents.coordination import CoordinatedRunner

                runner = CoordinatedRunner(
                    model,
                    ToolRegistry(
                        health_tool,
                        runbook_delay=settings.mock_runbook_delay_seconds,
                        telemetry=telemetry,
                    ),
                    telemetry,
                    budget_policy=settings.budget_policy(),
                    content_policy=content_policy,
                    max_steps=settings.agent_max_steps,
                    completion_budget=settings.agent_completion_tokens,
                    step_timeout=settings.step_timeout_seconds,
                    concurrency=settings.coordination_concurrency,
                )
            elif settings.agent_workflow == "pydanticai":
                try:
                    from runweave.agents.workflow import PydanticDecisionModel, PydanticWorkflow
                except ModuleNotFoundError as exc:
                    if exc.name not in {"pydantic_ai", "pydantic_graph"}:
                        raise
                    raise RuntimeError("Run uv sync --locked --extra workflow") from exc
                runner = PydanticWorkflow(
                    TriageSteps(
                        PydanticDecisionModel(model),
                        ToolRegistry(
                            health_tool,
                            runbook_delay=settings.mock_runbook_delay_seconds,
                            telemetry=telemetry,
                        ),
                        telemetry,
                        settings.step_timeout_seconds,
                        content_policy=content_policy,
                        budget_policy=settings.budget_policy(),
                    ),
                    settings.agent_max_steps,
                    settings.agent_completion_tokens,
                )
            else:
                runner = AgentRunner(
                    model,
                    health_tool,
                    step_timeout=settings.step_timeout_seconds,
                    max_steps=settings.agent_max_steps,
                    completion_budget=settings.agent_completion_tokens,
                    telemetry=telemetry,
                    runbook_delay=settings.mock_runbook_delay_seconds,
                    content_policy=content_policy,
                    budget_policy=settings.budget_policy(),
                )
            run_executor = executor if executor is not None else runner
            connector = HttpAgentConnector(
                client,
                allowed_hosts=settings.http_agent_allowed_hosts,
                retry=RetryPolicy(
                    attempts=settings.http_agent_retry_attempts,
                    base_delay=settings.http_agent_retry_base_delay_seconds,
                    max_delay=settings.http_agent_retry_max_delay_seconds,
                ),
                max_response_bytes=settings.http_agent_response_max_bytes,
                connect_timeout=settings.http_agent_connect_timeout_seconds,
            )
            remote = await connect_startup_agents(
                connector,
                settings.http_agents if http_agents is None else http_agents,
                limit=settings.http_agent_max_registered,
            )
            proxy = ProxyBindings(settings.proxy_public_url)
            catalog = AgentCatalog.with_triage(
                run_executor,
                version=settings.agent_workflow if executor is None else "custom",
                max_steps=settings.agent_max_steps,
                extra=[*agents, *remote],
            )
            if settings.database_url is not None:
                engine = postgres_engine(settings)
                try:
                    durable = DurableScheduler(
                        PostgresRuns(engine),
                        catalog,
                        settings,
                        telemetry,
                        content_policy,
                        proxy=proxy,
                    )
                    async with durable.running():
                        yield Runtime(
                            client,
                            runner,
                            durable,
                            telemetry,
                            content_policy,
                            isolated,
                            catalog,
                            connector,
                            proxy,
                        )
                finally:
                    await engine.dispose()
            else:
                scheduler = Scheduler(
                    catalog,
                    RunRegistry(
                        settings.history_max_records,
                        settings.history_ttl_seconds,
                        telemetry=telemetry,
                    ),
                    worker_count=settings.worker_count,
                    queue_capacity=settings.queue_capacity,
                    cleanup_interval=settings.history_cleanup_interval_seconds,
                    run_timeout=settings.run_timeout_seconds,
                    shutdown_grace=settings.shutdown_grace_seconds,
                    tenant_limit=settings.tenant_max_unfinished,
                    content_policy=content_policy,
                    budget_policy=settings.budget_policy(),
                    output_max_bytes=settings.agent_output_max_bytes,
                    proxy=proxy,
                )
                async with scheduler.running():
                    yield Runtime(
                        client,
                        runner,
                        MemoryBackend(scheduler),
                        telemetry,
                        content_policy,
                        isolated,
                        catalog,
                        connector,
                        proxy,
                    )
        finally:
            await isolated.close()
            await telemetry.close()
