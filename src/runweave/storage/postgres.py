import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from runweave.agents.catalog import TRIAGE_AGENT
from runweave.agents.contracts import IncidentRequest
from runweave.budgets import BudgetExceeded, BudgetPolicy, BudgetState, ChangeResult
from runweave.checkpoints import Workflow, initial_checkpoint
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus, RunSummary
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy, Principal, TenantQuotaExceeded
from runweave.steps import STEP_EVENT_CAP, StepEvent

_COLUMNS = (
    "id, status, submitted_at, started_at, finished_at, result, error, principal, budget, "
    "approval_wait_started_at, approval_wait_seconds, agent, agent_version, steps, incident"
)
_OWNER = "principal->>'tenant' = :tenant AND principal->>'subject' = :subject"
_FINISH_ATTEMPT = """
    UPDATE runweave_attempts a SET finished_at = clock_timestamp(), outcome = changed.status
    FROM changed WHERE a.run_id = changed.id AND a.attempt = changed.attempt
    AND a.finished_at IS NULL
"""

if TYPE_CHECKING:
    from runweave.core.settings import Settings
    from runweave.storage.approvals import PostgresApprovals, PostgresTicketGate
    from runweave.storage.checkpoints import PostgresCheckpoints
    from runweave.storage.memory import PostgresMemory


@dataclass(frozen=True)
class Claim:
    snapshot: RunSnapshot
    input: dict[str, Any]
    token: UUID
    remaining_seconds: float
    attempt: int = 1

    @property
    def incident(self) -> IncidentRequest:
        return IncidentRequest.model_validate(self.input)


def snapshot(row: RowMapping) -> RunSnapshot:
    fields = {key: row[key] for key in _COLUMNS.split(", ") if key != "budget"}
    fields["usage"] = BudgetState.model_validate(row["budget"]).summary() if row["budget"] else None
    fields["steps"] = tuple(fields["steps"] or ())
    fields["input"] = fields.pop("incident")
    return RunSnapshot.model_validate({**fields, "status_url": f"/v1/runs/{row['id']}"})


class PostgresRuns:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(
                text("SELECT id, principal, budget, checkpoint FROM runweave_runs LIMIT 0")
            )
            await conn.execute(text("SELECT run_id FROM runweave_attempts LIMIT 0"))
            await conn.execute(text("SELECT run_id FROM runweave_operations LIMIT 0"))
            await conn.execute(text("SELECT run_id FROM runweave_approvals LIMIT 0"))
            await conn.execute(text("SELECT run_id FROM runweave_sandbox_tickets LIMIT 0"))
            await conn.execute(text("SELECT id FROM runweave_memory LIMIT 0"))
            await conn.execute(
                text("SELECT agent, agent_version, steps FROM runweave_runs LIMIT 0")
            )

    async def admit(
        self,
        input: BaseModel,
        *,
        capacity: int,
        run_seconds: float,
        attempts: int,
        history_max: int,
        principal: Principal = LOCAL_PRINCIPAL,
        tenant_limit: int = 1000,
        budget_policy: BudgetPolicy | None = None,
        workflow: Workflow = "baseline",
        max_steps: int = 7,
        agent: str = TRIAGE_AGENT,
        agent_version: str | None = None,
    ) -> RunSnapshot | None:
        policy = budget_policy or BudgetPolicy()
        # Typed checkpoints and replay exist only for the built-in triage workflows.
        checkpoint = (
            initial_checkpoint(input, workflow, max_steps, policy).model_dump_json()
            if agent == TRIAGE_AGENT and isinstance(input, IncidentRequest)
            else None
        )
        async with self._engine.begin() as conn:
            # Serialize admission across replicas. Worker transactions never take this lock.
            await conn.execute(text("SELECT pg_advisory_xact_lock(795940314, 1)"))
            unfinished = await conn.scalar(
                text(
                    "SELECT count(*) FROM runweave_runs WHERE finished_at IS NULL "
                    "AND principal->>'tenant' = :tenant"
                ),
                {"tenant": principal.tenant},
            )
            if unfinished is not None and unfinished >= tenant_limit:
                raise TenantQuotaExceeded()
            result = await conn.execute(
                text(f"""
                INSERT INTO runweave_runs
                    (id, incident, status, submitted_at, available_at, deadline_at,
                     max_attempts, principal, budget, checkpoint, agent, agent_version)
                SELECT :id, CAST(:incident AS jsonb), 'QUEUED', statement_timestamp(),
                    statement_timestamp(), statement_timestamp() + :timeout * interval '1 second',
                    :attempts, CAST(:principal AS jsonb), CAST(:budget AS jsonb),
                    CAST(:checkpoint AS jsonb), :agent, :agent_version
                WHERE (SELECT count(*) FROM runweave_runs WHERE finished_at IS NULL) < :capacity
                  AND (SELECT count(*) FROM runweave_runs) < :max_rows
                RETURNING {_COLUMNS}
            """),
                dict(
                    id=uuid4(),
                    incident=input.model_dump_json(),
                    timeout=run_seconds,
                    attempts=attempts,
                    capacity=capacity,
                    max_rows=capacity + history_max,
                    principal=principal.model_dump_json(),
                    budget=BudgetState(policy=policy).model_dump_json(),
                    checkpoint=checkpoint,
                    agent=agent,
                    agent_version=agent_version,
                ),
            )
            row = result.mappings().one_or_none()
        return snapshot(row) if row is not None else None

    async def get(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        condition = f" AND {_OWNER}" if principal is not None else ""
        params: dict[str, object] = {"id": run_id}
        if principal is not None:
            params.update(tenant=principal.tenant, subject=principal.subject)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_COLUMNS} FROM runweave_runs WHERE id = :id{condition}"), params
            )
            row = result.mappings().one_or_none()
        return snapshot(row) if row is not None else None

    async def claim(self, lease_seconds: float) -> Claim | None:
        token = uuid4()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                WITH candidate AS (
                    SELECT id FROM runweave_runs
                    WHERE status = 'QUEUED' AND available_at <= statement_timestamp()
                      AND deadline_at > statement_timestamp() AND attempt < max_attempts
                    ORDER BY available_at, submitted_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                ), claimed AS (
                    UPDATE runweave_runs r SET status = 'RUNNING',
                        started_at = COALESCE(r.started_at, clock_timestamp()),
                        attempt = r.attempt + 1, lease_token = :token,
                        lease_until = clock_timestamp() + :lease * interval '1 second'
                    FROM candidate WHERE r.id = candidate.id AND r.deadline_at > clock_timestamp()
                    RETURNING r.*
                ), recorded AS (
                    INSERT INTO runweave_attempts (run_id, attempt, lease_token, started_at)
                    SELECT id, attempt, lease_token, clock_timestamp() FROM claimed
                ) SELECT id, status, submitted_at, started_at, finished_at, result, error,
                    principal, budget, incident, approval_wait_started_at, approval_wait_seconds,
                    agent, agent_version, steps, attempt,
                    extract(epoch FROM deadline_at - clock_timestamp()) AS remaining
                FROM claimed
            """),
                {"token": token, "lease": lease_seconds},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return Claim(
            snapshot(row),
            dict(row["incident"]),
            token,
            float(row["remaining"]),
            int(row["attempt"]),
        )

    async def renew(self, claim: Claim, seconds: float) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                UPDATE runweave_runs
                SET lease_until = clock_timestamp() + :lease * interval '1 second'
                WHERE id = :id AND status = 'RUNNING' AND lease_token = :token
                    AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp()
                RETURNING id
            """),
                {"id": claim.snapshot.id, "token": claim.token, "lease": seconds},
            )
            return result.first() is not None

    async def list(
        self,
        principal: Principal,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[RunSummary]:
        conditions = [_OWNER]
        params: dict[str, object] = {
            "tenant": principal.tenant,
            "subject": principal.subject,
            "limit": limit,
        }
        if agent is not None:
            conditions.append("agent = :agent")
            params["agent"] = agent
        if status is not None:
            conditions.append("status = :status")
            params["status"] = status.value
        if before is not None:
            conditions.append("submitted_at < :before")
            params["before"] = before if before.tzinfo else before.replace(tzinfo=UTC)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM runweave_runs WHERE {' AND '.join(conditions)} "
                    "ORDER BY submitted_at DESC, id DESC LIMIT :limit"
                ),
                params,
            )
            rows = result.mappings().all()
        return [snapshot(row).summary() for row in rows]

    def steps(self, claim: Claim) -> "PostgresStepLog":
        return PostgresStepLog(self._engine, claim)

    def ledger(self, claim: Claim) -> "PostgresBudget":
        return PostgresBudget(self._engine, claim)

    def checkpoints(self, claim: Claim) -> "PostgresCheckpoints":
        from runweave.storage.checkpoints import PostgresCheckpoints

        return PostgresCheckpoints(self._engine, claim)

    def approvals(self) -> "PostgresApprovals":
        from runweave.storage.approvals import PostgresApprovals

        return PostgresApprovals(self._engine)

    def memory(self, settings: "Settings", content_policy: ContentPolicy) -> "PostgresMemory":
        from runweave.storage.memory import PostgresMemory

        return PostgresMemory(self._engine, settings, content_policy)

    def ticket_gate(
        self,
        claim: Claim,
        *,
        ttl: float,
        limit: int,
        authorize: Callable[[], Principal],
        content_policy: ContentPolicy,
    ) -> "PostgresTicketGate":
        from runweave.storage.approvals import PostgresTicketGate

        return PostgresTicketGate(
            self._engine,
            claim,
            ttl=ttl,
            limit=limit,
            authorize=authorize,
            content_policy=content_policy,
        )

    async def _finish(self, condition: str, params: Mapping[str, object]) -> RunSnapshot | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(f"""
                WITH changed AS (
                    UPDATE runweave_runs SET status = :status, finished_at = clock_timestamp(),
                        lease_token = NULL, lease_until = NULL,
                        approval_wait_seconds = approval_wait_seconds + COALESCE(
                            extract(epoch FROM clock_timestamp() - approval_wait_started_at), 0),
                        approval_wait_started_at = NULL,
                        result = CAST(:result AS jsonb), error = CAST(:error AS jsonb)
                    WHERE id = :id AND {condition}
                    RETURNING *
                ), recorded AS ({_FINISH_ATTEMPT}) SELECT {_COLUMNS} FROM changed
            """),
                params,
            )
            row = result.mappings().one_or_none()
            if row is not None and row["status"] == "CANCELLED":
                await conn.execute(
                    text("""
                    UPDATE runweave_approvals SET status = 'cancelled'
                    WHERE run_id = :id AND status IN ('pending', 'approved')
                """),
                    {"id": row["id"]},
                )
        return snapshot(row) if row is not None else None

    async def finish(
        self,
        claim: Claim,
        status: RunStatus,
        *,
        result: BaseModel | dict[str, Any] | None = None,
        error: RunError | None = None,
    ) -> RunSnapshot | None:
        if not status.terminal:
            raise ValueError("finish requires a terminal status")
        if isinstance(result, BaseModel):
            encoded: str | None = result.model_dump_json()
        else:
            encoded = json.dumps(result) if result is not None else None
        return await self._finish(
            "status = 'RUNNING' AND lease_token = :token AND lease_until > clock_timestamp() "
            "AND (deadline_at > clock_timestamp() OR :status = 'TIMED_OUT')",
            dict(
                id=claim.snapshot.id,
                token=claim.token,
                status=status.value,
                result=encoded,
                error=error.model_dump_json() if error else None,
            ),
        )

    async def cancel(self, run_id: UUID, principal: Principal | None = None) -> RunSnapshot | None:
        condition = "status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL')"
        params: dict[str, object] = dict(id=run_id, status="CANCELLED", result=None, error=None)
        if principal is not None:
            condition += f" AND {_OWNER}"
            params.update(tenant=principal.tenant, subject=principal.subject)
        changed = await self._finish(
            condition,
            params,
        )
        return changed if changed is not None else await self.get(run_id, principal)

    async def maintain(self, *, retry_delay: float, history_ttl: float, history_max: int) -> None:
        from runweave.storage.approvals import expire_approvals
        from runweave.storage.memory import prune_memory

        async with self._engine.begin() as conn:
            await expire_approvals(conn)
            await prune_memory(conn)
            await conn.execute(
                text("""
                WITH candidates AS (
                    SELECT id FROM runweave_runs WHERE finished_at IS NULL AND (
                        (status != 'WAITING_APPROVAL' AND deadline_at <= statement_timestamp()) OR
                        (status = 'RUNNING' AND lease_until <= statement_timestamp())
                    ) ORDER BY deadline_at, id LIMIT 128 FOR UPDATE SKIP LOCKED
                ), changed AS (
                    UPDATE runweave_runs r SET
                        status = CASE
                            WHEN deadline_at <= statement_timestamp() THEN 'TIMED_OUT'
                            WHEN attempt >= max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
                        finished_at = CASE WHEN deadline_at <= statement_timestamp()
                            OR attempt >= max_attempts THEN clock_timestamp() END,
                        error = CASE WHEN deadline_at <= statement_timestamp() THEN
                            jsonb_build_object('code', 'RUN_DEADLINE_EXCEEDED',
                                               'message', 'Execution deadline exceeded')
                            WHEN attempt >= max_attempts THEN
                            jsonb_build_object('code', 'ATTEMPTS_EXHAUSTED',
                                               'message', 'Recovery attempts exhausted') END,
                        available_at = clock_timestamp() + :delay * interval '1 second',
                        lease_token = NULL, lease_until = NULL
                    FROM candidates WHERE r.id = candidates.id RETURNING r.id, r.attempt, r.status
                ) UPDATE runweave_attempts a SET finished_at = clock_timestamp(),
                    outcome = CASE WHEN changed.status = 'QUEUED' THEN 'LEASE_EXPIRED'
                                   ELSE changed.status END
                FROM changed WHERE a.run_id = changed.id AND a.attempt = changed.attempt
                    AND a.finished_at IS NULL
            """),
                {"delay": retry_delay},
            )
            await conn.execute(
                text("""
                DELETE FROM runweave_runs WHERE id IN (
                    SELECT id FROM runweave_runs WHERE finished_at IS NOT NULL AND (
                        finished_at <= statement_timestamp() - :ttl * interval '1 second'
                        OR id IN (SELECT id FROM runweave_runs WHERE finished_at IS NOT NULL
                            ORDER BY finished_at DESC, id DESC OFFSET :history_max)
                    ) ORDER BY finished_at, id LIMIT 128 FOR UPDATE SKIP LOCKED
                )
            """),
                {"ttl": history_ttl, "history_max": history_max},
            )

    async def counts(self) -> tuple[int, int]:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("""
                SELECT count(*) FILTER (WHERE status = 'QUEUED'), count(*) FROM runweave_runs
            """)
                )
            ).one()
        return int(row[0]), int(row[1])


class PostgresBudget:
    def __init__(self, engine: AsyncEngine, claim: Claim) -> None:
        self._engine = engine
        self._claim = claim

    async def read(self) -> BudgetState:
        async with self._engine.connect() as conn:
            raw = await conn.scalar(
                text("SELECT budget FROM runweave_runs WHERE id = :id"),
                {"id": self._claim.snapshot.id},
            )
        if raw is None:
            raise BudgetExceeded("BUDGET_LEASE_LOST")
        return BudgetState.model_validate(raw)

    async def update(self, change: Callable[[BudgetState], ChangeResult]) -> ChangeResult:
        async with self._engine.begin() as conn:
            raw = await conn.scalar(
                text("""
                SELECT budget FROM runweave_runs
                WHERE id = :id AND status = 'RUNNING' AND lease_token = :token
                    AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp()
                FOR UPDATE
            """),
                {"id": self._claim.snapshot.id, "token": self._claim.token},
            )
            if raw is None:
                raise BudgetExceeded("BUDGET_LEASE_LOST")
            state = BudgetState.model_validate(raw)
            result = change(state)
            changed = await conn.scalar(
                text(
                    "UPDATE runweave_runs SET budget = CAST(:budget AS jsonb) "
                    "WHERE id = :id AND status = 'RUNNING' AND lease_token = :token "
                    "AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp() "
                    "RETURNING id"
                ),
                {
                    "id": self._claim.snapshot.id,
                    "token": self._claim.token,
                    "budget": state.model_dump_json(),
                },
            )
            if changed is None:
                raise BudgetExceeded("BUDGET_LEASE_LOST")
        return result


class PostgresStepLog:
    """Appends timeline events fenced by the live lease. Best effort: never fails a run."""

    def __init__(self, engine: AsyncEngine, claim: Claim) -> None:
        self._engine = engine
        self._claim = claim

    async def begin(self, title: str, detail: str | None) -> StepEvent | None:
        event = StepEvent(
            index=0,
            attempt=self._claim.attempt,
            title=title,
            detail=detail,
            status="running",
            started_at=datetime.now(UTC),
        )
        async with self._engine.begin() as conn:
            index = await conn.scalar(
                text("""
                UPDATE runweave_runs SET steps = steps || jsonb_build_array(jsonb_set(
                    CAST(:event AS jsonb), '{index}', to_jsonb(jsonb_array_length(steps))))
                WHERE id = :id AND status = 'RUNNING' AND lease_token = :token
                    AND jsonb_array_length(steps) < :cap
                RETURNING jsonb_array_length(steps) - 1
            """),
                {
                    "id": self._claim.snapshot.id,
                    "token": self._claim.token,
                    "event": event.model_dump_json(),
                    "cap": STEP_EVENT_CAP,
                },
            )
        return None if index is None else event.model_copy(update={"index": int(index)})

    async def end(self, event: StepEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                UPDATE runweave_runs
                SET steps = jsonb_set(steps, ARRAY[CAST(:index AS text)], CAST(:event AS jsonb))
                WHERE id = :id AND lease_token = :token AND jsonb_array_length(steps) > :index
            """),
                {
                    "id": self._claim.snapshot.id,
                    "token": self._claim.token,
                    "index": event.index,
                    "event": event.model_dump_json(),
                },
            )
