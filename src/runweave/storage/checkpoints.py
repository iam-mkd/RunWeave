import json
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runweave.agents.contracts import Evidence
from runweave.budgets import BudgetState
from runweave.checkpoints import (
    CheckpointError,
    OperationRecord,
    TriageCheckpoint,
    decode_checkpoint,
)
from runweave.storage.postgres import Claim

_LIVE = (
    "id = :id AND status = 'RUNNING' AND lease_token = :token "
    "AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp()"
)


class PostgresCheckpoints:
    def __init__(self, engine: AsyncEngine, claim: Claim) -> None:
        self._engine = engine
        self._claim = claim
        self.run_id = claim.snapshot.id
        self._params = {"id": self.run_id, "token": claim.token}

    async def _lock(self, conn: AsyncConnection) -> RowMapping:
        result = await conn.execute(
            text(
                "SELECT checkpoint, checkpoint_seq, budget FROM runweave_runs "
                f"WHERE {_LIVE} FOR UPDATE"
            ),
            self._params,
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise CheckpointError("CHECKPOINT_LEASE_LOST")
        return row

    async def load(self) -> TriageCheckpoint:
        async with self._engine.begin() as conn:
            row = await self._lock(conn)
            checkpoint = decode_checkpoint(row["checkpoint"])
            if checkpoint.sequence != row["checkpoint_seq"]:
                raise CheckpointError("CHECKPOINT_CONFLICT")
            return checkpoint

    async def save(self, checkpoint: TriageCheckpoint, expected_sequence: int) -> TriageCheckpoint:
        async with self._engine.begin() as conn:
            row = await self._lock(conn)
            previous = decode_checkpoint(row["checkpoint"])
            if (
                row["checkpoint_seq"] != expected_sequence
                or checkpoint.sequence != expected_sequence
                or previous.sequence != expected_sequence
            ):
                raise CheckpointError("CHECKPOINT_CONFLICT")
            if previous.identity != checkpoint.identity or previous.incident != checkpoint.incident:
                raise CheckpointError("CHECKPOINT_CONFLICT")
            updated = checkpoint.model_copy(
                update={
                    "sequence": expected_sequence + 1,
                    "usage": BudgetState.model_validate(row["budget"]),
                }
            )
            updated = decode_checkpoint(updated.model_dump(mode="json"))
            changed = await conn.scalar(
                text(
                    "UPDATE runweave_runs SET checkpoint = CAST(:checkpoint AS jsonb), "
                    f"checkpoint_seq = :sequence WHERE {_LIVE} "
                    "AND checkpoint_seq = :expected RETURNING id"
                ),
                {
                    **self._params,
                    "checkpoint": updated.model_dump_json(),
                    "sequence": updated.sequence,
                    "expected": expected_sequence,
                },
            )
            if changed is None:
                raise CheckpointError("CHECKPOINT_LEASE_LOST")
            return updated

    async def prepare_operation(
        self, operation_id: str, digest: str, kind: Literal["read", "effect"]
    ) -> tuple[OperationRecord, bool]:
        async with self._engine.begin() as conn:
            await self._lock(conn)
            params = {**self._params, "operation": operation_id, "digest": digest, "kind": kind}
            inserted = await conn.scalar(
                text("""
                INSERT INTO runweave_operations (run_id, operation_id, digest, kind, status)
                SELECT :id, :operation, :digest, :kind, 'started'
                WHERE (SELECT count(*) FROM runweave_operations WHERE run_id = :id) < 40
                ON CONFLICT (run_id, operation_id) DO NOTHING RETURNING operation_id
            """),
                params,
            )
            result = await conn.execute(
                text("""
                SELECT operation_id AS id, digest, kind, status, evidence
                FROM runweave_operations WHERE run_id = :id AND operation_id = :operation
            """),
                params,
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise CheckpointError("OPERATION_LIMIT_EXCEEDED")
            try:
                record = OperationRecord.model_validate(dict(row))
            except ValidationError:
                raise CheckpointError("CHECKPOINT_INVALID") from None
            if record.digest != digest or record.kind != kind:
                raise CheckpointError("OPERATION_IDENTITY_CONFLICT")
            # Recheck the lease after any wait on the journal's unique key.
            await self._lock(conn)
            return record, inserted is not None

    async def commit_operation(self, operation_id: str, evidence: Evidence) -> None:
        if not 1 <= len(evidence.id) <= 100 or len(evidence.content) > 16384:
            raise CheckpointError("CHECKPOINT_INVALID")
        encoded = json.dumps({"id": evidence.id, "content": evidence.content})
        async with self._engine.begin() as conn:
            await self._lock(conn)
            changed = await conn.scalar(
                text(f"""
                UPDATE runweave_operations SET status = 'committed',
                    evidence = CAST(:evidence AS jsonb), committed_at = clock_timestamp()
                WHERE run_id = :id AND operation_id = :operation AND status = 'started'
                  AND EXISTS (SELECT id FROM runweave_runs WHERE {_LIVE})
                RETURNING operation_id
            """),
                {**self._params, "operation": operation_id, "evidence": encoded},
            )
            if changed is None:
                existing = await conn.scalar(
                    text("""
                    SELECT evidence FROM runweave_operations WHERE run_id = :id
                    AND operation_id = :operation AND status = 'committed'
                """),
                    {**self._params, "operation": operation_id},
                )
                if existing != json.loads(encoded):
                    raise CheckpointError("OPERATION_COMMIT_CONFLICT")

    async def require_resolution(self, operation_id: str) -> None:
        async with self._engine.begin() as conn:
            await self._lock(conn)
            await conn.execute(
                text(f"""
                UPDATE runweave_operations SET status = 'needs_resolution'
                WHERE run_id = :id AND operation_id = :operation AND kind = 'effect'
                    AND status = 'started'
                    AND EXISTS (SELECT id FROM runweave_runs WHERE {_LIVE})
            """),
                {**self._params, "operation": operation_id},
            )
