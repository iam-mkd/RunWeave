from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncEngine

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.orchestration.models import RunError, RunSnapshot, RunStatus

_COLUMNS = "id, status, submitted_at, started_at, finished_at, result, error"
_FINISH_ATTEMPT = """
    UPDATE runweave_attempts a SET finished_at = clock_timestamp(), outcome = changed.status
    FROM changed WHERE a.run_id = changed.id AND a.attempt = changed.attempt
    AND a.finished_at IS NULL
"""


@dataclass(frozen=True)
class Claim:
    snapshot: RunSnapshot
    incident: IncidentRequest
    token: UUID
    remaining_seconds: float


def snapshot(row: RowMapping) -> RunSnapshot:
    fields = {key: row[key] for key in _COLUMNS.split(", ")}
    return RunSnapshot.model_validate({**fields, "status_url": f"/v1/runs/{row['id']}"})


class PostgresRuns:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT id FROM runweave_runs LIMIT 0"))
            await conn.execute(text("SELECT run_id FROM runweave_attempts LIMIT 0"))

    async def admit(
        self,
        incident: IncidentRequest,
        *,
        capacity: int,
        run_seconds: float,
        attempts: int,
        history_max: int,
    ) -> RunSnapshot | None:
        async with self._engine.begin() as conn:
            # Serialize admission across replicas. Worker transactions never take this lock.
            await conn.execute(text("SELECT pg_advisory_xact_lock(795940314, 1)"))
            result = await conn.execute(
                text(f"""
                INSERT INTO runweave_runs
                    (id, incident, status, submitted_at, available_at, deadline_at, max_attempts)
                SELECT :id, CAST(:incident AS jsonb), 'QUEUED', statement_timestamp(),
                    statement_timestamp(), statement_timestamp() + :timeout * interval '1 second',
                    :attempts
                WHERE (SELECT count(*) FROM runweave_runs WHERE finished_at IS NULL) < :capacity
                  AND (SELECT count(*) FROM runweave_runs) < :max_rows
                RETURNING {_COLUMNS}
            """),
                dict(
                    id=uuid4(),
                    incident=incident.model_dump_json(),
                    timeout=run_seconds,
                    attempts=attempts,
                    capacity=capacity,
                    max_rows=capacity + history_max,
                ),
            )
            row = result.mappings().one_or_none()
        return snapshot(row) if row is not None else None

    async def get(self, run_id: UUID) -> RunSnapshot | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_COLUMNS} FROM runweave_runs WHERE id = :id"), {"id": run_id}
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
                ) SELECT id, status, submitted_at, started_at, finished_at, result, error, incident,
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
            IncidentRequest.model_validate(row["incident"]),
            token,
            float(row["remaining"]),
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

    async def _finish(self, condition: str, params: Mapping[str, object]) -> RunSnapshot | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(f"""
                WITH changed AS (
                    UPDATE runweave_runs SET status = :status, finished_at = clock_timestamp(),
                        lease_token = NULL, lease_until = NULL,
                        result = CAST(:result AS jsonb), error = CAST(:error AS jsonb)
                    WHERE id = :id AND {condition}
                    RETURNING *
                ), recorded AS ({_FINISH_ATTEMPT}) SELECT {_COLUMNS} FROM changed
            """),
                params,
            )
            row = result.mappings().one_or_none()
        return snapshot(row) if row is not None else None

    async def finish(
        self,
        claim: Claim,
        status: RunStatus,
        *,
        result: IncidentResult | None = None,
        error: RunError | None = None,
    ) -> RunSnapshot | None:
        if not status.terminal:
            raise ValueError("finish requires a terminal status")
        return await self._finish(
            "status = 'RUNNING' AND lease_token = :token AND lease_until > clock_timestamp() "
            "AND (deadline_at > clock_timestamp() OR :status = 'TIMED_OUT')",
            dict(
                id=claim.snapshot.id,
                token=claim.token,
                status=status.value,
                result=result.model_dump_json() if result else None,
                error=error.model_dump_json() if error else None,
            ),
        )

    async def cancel(self, run_id: UUID) -> RunSnapshot | None:
        changed = await self._finish(
            "status IN ('QUEUED', 'RUNNING')",
            dict(id=run_id, status="CANCELLED", result=None, error=None),
        )
        return changed if changed is not None else await self.get(run_id)

    async def maintain(self, *, retry_delay: float, history_ttl: float, history_max: int) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                WITH candidates AS (
                    SELECT id FROM runweave_runs WHERE finished_at IS NULL AND (
                        deadline_at <= statement_timestamp() OR
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
