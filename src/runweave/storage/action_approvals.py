"""PostgreSQL storage for agent-declared approvals (see `runweave.action_approvals`).

Lock order everywhere: the run row, then the approval row (as for ticket approvals).
"""

import asyncio
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runweave.action_approvals import (
    ActionApproval,
    Approval,
    ApprovalChoice,
    ApprovalExpired,
    ApprovalRefused,
    argument_digest,
    canonical,
)
from runweave.approvals import ApprovalError
from runweave.deadline import paused_deadline
from runweave.security import Principal
from runweave.storage.postgres import Claim

_OWNER = "r.principal->>'tenant' = :tenant AND r.principal->>'subject' = :subject"
_LIST_LIMIT = 200


def _snapshot(row: RowMapping) -> ActionApproval:
    return ActionApproval(
        id=row["id"],
        run_id=row["run_id"],
        tenant=row["tenant"],
        agent=row["agent"],
        action=row["action_key"],
        arguments=row["arguments"],
        argument_digest=row["argument_digest"],
        policy_version=row["policy_version"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
    )


async def cancel_action_approvals(conn: AsyncConnection, run_id: UUID) -> None:
    await conn.execute(
        text("""
        UPDATE runweave_action_approvals SET status = 'cancelled'
        WHERE run_id = :id AND status = 'pending'
    """),
        {"id": run_id},
    )


async def expire_action_approvals(conn: AsyncConnection) -> None:
    """Expire overdue requests; their waiting agents (or re-runs) then see `expired`."""
    await conn.execute(
        text("""
        UPDATE runweave_action_approvals SET status = 'expired'
        WHERE id IN (
            SELECT id FROM runweave_action_approvals
            WHERE status = 'pending' AND expires_at <= clock_timestamp()
            ORDER BY expires_at LIMIT 256 FOR UPDATE SKIP LOCKED
        )
    """)
    )


class PostgresActionApprovals:
    """What reviewers see and decide, scoped to the run owner."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def list(
        self, principal: Principal, *, status: Literal["pending", "all"] = "pending"
    ) -> list[ActionApproval]:
        principal.require("runs:read")
        live = (
            "AND a.status = 'pending' AND a.expires_at > clock_timestamp()"
            if status == "pending"
            else ""
        )
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(f"""
                SELECT a.* FROM runweave_action_approvals a
                JOIN runweave_runs r ON r.id = a.run_id
                WHERE {_OWNER} AND a.tenant = :tenant {live}
                ORDER BY a.created_at, a.id LIMIT {_LIST_LIMIT}
            """),
                    {"tenant": principal.tenant, "subject": principal.subject},
                )
            ).mappings()
            return [_snapshot(row) for row in rows]

    async def get(self, approval_id: UUID, principal: Principal) -> ActionApproval | None:
        principal.require("runs:read")
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(f"""
                SELECT a.* FROM runweave_action_approvals a
                JOIN runweave_runs r ON r.id = a.run_id
                WHERE a.id = :id AND a.tenant = :tenant AND {_OWNER}
            """),
                        {
                            "id": approval_id,
                            "tenant": principal.tenant,
                            "subject": principal.subject,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _snapshot(row) if row is not None else None

    async def decide(
        self, approval_id: UUID, principal: Principal, choice: ApprovalChoice
    ) -> ActionApproval | None:
        principal.require("approvals:decide")
        principal.require("runs:read")
        failure: str | None = None
        params = {"id": approval_id, "tenant": principal.tenant, "subject": principal.subject}
        async with self.engine.begin() as conn:
            run = await conn.scalar(
                text(f"""
                SELECT r.id FROM runweave_runs r
                JOIN runweave_action_approvals a ON a.run_id = r.id
                WHERE a.id = :id AND a.tenant = :tenant AND {_OWNER} FOR UPDATE OF r
            """),
                params,
            )
            if run is None:
                return None
            row = (
                (
                    await conn.execute(
                        text("""
                SELECT *, expires_at > clock_timestamp() AS live
                FROM runweave_action_approvals WHERE id = :id FOR UPDATE
            """),
                        {"id": approval_id},
                    )
                )
                .mappings()
                .one()
            )
            if (
                choice.argument_digest != row["argument_digest"]
                or choice.policy_version != row["policy_version"]
                or choice.expires_at != row["expires_at"]
                or row["tenant"] != principal.tenant
            ):
                raise ApprovalError("APPROVAL_BINDING_MISMATCH")
            if row["decision"] is not None:
                if row["decision"] != choice.decision:
                    raise ApprovalError("APPROVAL_DECISION_CONFLICT")
            elif row["status"] != "pending" or not row["live"]:
                if row["status"] == "pending":
                    await conn.execute(
                        text(
                            "UPDATE runweave_action_approvals SET status = 'expired' WHERE id = :id"
                        ),
                        {"id": approval_id},
                    )
                failure = "APPROVAL_NOT_PENDING"
            else:
                await conn.execute(
                    text("""
                    UPDATE runweave_action_approvals SET decision = :decision,
                        status = CASE WHEN :decision = 'approve' THEN 'approved'
                            ELSE 'rejected' END,
                        decided_by = :subject, decided_at = clock_timestamp()
                    WHERE id = :id
                """),
                    {"id": approval_id, "decision": choice.decision, "subject": principal.subject},
                )
        if failure is not None:
            raise ApprovalError(failure)
        return await self.get(approval_id, principal)


class PostgresActionGate:
    """`ctx.request_approval` for one claimed run attempt."""

    def __init__(
        self,
        engine: AsyncEngine,
        claim: Claim,
        *,
        agent: str,
        version: str | None,
        ttl: float,
        limit: int,
        poll: float,
    ) -> None:
        self.engine = engine
        self.claim = claim
        self.agent = agent
        self.policy_version = f"agent:{agent}@{version or 'unversioned'}"[:100]
        self.ttl = ttl
        self.limit = limit
        self.poll = poll

    async def request(self, action: str, arguments: Any, expires_in: float | None) -> Approval:
        seconds = min(self.ttl, expires_in) if expires_in is not None else self.ttl
        row = await self._record(action, arguments, seconds)
        if row["status"] == "pending":
            loop = asyncio.get_running_loop()
            until = loop.time() + max(0.0, float(row["left"])) + self.poll * 4
            async with paused_deadline(until):
                row = await self._wait(row["id"])
            await self._resume()
        if row["status"] in ("approved", "rejected"):
            return Approval(
                id=row["id"],
                status=row["status"],
                decided_by=row["decided_by"],
                decided_at=row["decided_at"],
            )
        if row["status"] == "expired":
            raise ApprovalExpired()
        raise ApprovalRefused("APPROVAL_CANCELLED")

    async def _record(self, action: str, arguments: Any, seconds: float) -> RowMapping:
        digest = argument_digest(arguments)
        run_id, token = self.claim.snapshot.id, self.claim.token
        refused: str | None = None
        async with self.engine.begin() as conn:
            locked = await conn.scalar(
                text("""
                SELECT id FROM runweave_runs WHERE id = :id AND status = 'RUNNING'
                    AND lease_token = :token AND lease_until > clock_timestamp() FOR UPDATE
            """),
                {"id": run_id, "token": token},
            )
            if locked is None:
                raise ApprovalRefused("CHECKPOINT_LEASE_LOST")
            existing = await self._find(conn, action, digest)
            if existing is None:
                # Serialize the pending count so concurrent requests respect the limit.
                await conn.execute(text("SELECT pg_advisory_xact_lock(795940314, 3)"))
                pending = await conn.scalar(
                    text("""
                    SELECT count(*) FROM runweave_action_approvals
                    WHERE status = 'pending' AND expires_at > clock_timestamp()
                """)
                )
                if pending is not None and pending >= self.limit:
                    refused = "APPROVAL_CAPACITY_EXCEEDED"
                else:
                    await conn.execute(
                        text("""
                        INSERT INTO runweave_action_approvals (id, run_id, tenant, agent,
                            action_key, arguments, argument_digest, policy_version, status,
                            expires_at)
                        VALUES (:approval, :run, :tenant, :agent, :action,
                            CAST(:arguments AS jsonb), :digest, :policy, 'pending',
                            clock_timestamp() + :seconds * interval '1 second')
                    """),
                        {
                            "approval": uuid4(),
                            "run": run_id,
                            "tenant": self.claim.snapshot.principal.tenant,
                            "agent": self.agent,
                            "action": action,
                            "arguments": canonical(arguments),
                            "digest": digest,
                            "policy": self.policy_version,
                            "seconds": seconds,
                        },
                    )
                    existing = await self._find(conn, action, digest)
            elif existing["status"] == "pending" and existing["left"] <= 0:
                await conn.execute(
                    text("UPDATE runweave_action_approvals SET status = 'expired' WHERE id = :id"),
                    {"id": existing["id"]},
                )
                existing = await self._find(conn, action, digest)
            if refused is None and existing is not None and existing["status"] == "pending":
                await conn.execute(
                    text("""
                    UPDATE runweave_runs SET status = 'WAITING_APPROVAL',
                        approval_wait_started_at = clock_timestamp() WHERE id = :id
                """),
                    {"id": run_id},
                )
        if refused is not None:
            raise ApprovalRefused(refused)
        assert existing is not None
        return existing

    async def _find(self, conn: AsyncConnection, action: str, digest: str) -> RowMapping | None:
        return (
            (
                await conn.execute(
                    text("""
                SELECT *, extract(epoch FROM expires_at - clock_timestamp()) AS left
                FROM runweave_action_approvals
                WHERE run_id = :run AND action_key = :action AND argument_digest = :digest
            """),
                    {"run": self.claim.snapshot.id, "action": action, "digest": digest},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _wait(self, approval_id: UUID) -> RowMapping:
        while True:
            async with self.engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            text("""
                    SELECT *, expires_at > clock_timestamp() AS live
                    FROM runweave_action_approvals WHERE id = :id
                """),
                            {"id": approval_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                if row["status"] != "pending":
                    return row
                if not row["live"]:
                    expired = (
                        (
                            await conn.execute(
                                text("""
                        UPDATE runweave_action_approvals SET status = 'expired'
                        WHERE id = :id AND status = 'pending' RETURNING *
                    """),
                                {"id": approval_id},
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if expired is not None:
                        return expired
                    continue
            await asyncio.sleep(self.poll)

    async def _resume(self) -> None:
        """Back to RUNNING; the waited time is added to the stored deadline."""
        async with self.engine.begin() as conn:
            resumed = await conn.scalar(
                text("""
                UPDATE runweave_runs SET status = 'RUNNING',
                    deadline_at = deadline_at + (clock_timestamp() - approval_wait_started_at),
                    approval_wait_seconds = approval_wait_seconds +
                        extract(epoch FROM clock_timestamp() - approval_wait_started_at),
                    approval_wait_started_at = NULL
                WHERE id = :id AND status = 'WAITING_APPROVAL' AND lease_token = :token
                    AND lease_until > clock_timestamp()
                RETURNING id
            """),
                {"id": self.claim.snapshot.id, "token": self.claim.token},
            )
        if resumed is None:
            raise ApprovalRefused("CHECKPOINT_LEASE_LOST")
