import json
from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runweave.agents.contracts import Evidence, IncidentRequest, IncidentResult, ToolCall
from runweave.approvals import (
    TICKET_CALL_ID,
    TICKET_POLICY,
    ApprovalDecision,
    ApprovalError,
    ApprovalSnapshot,
    AwaitingApproval,
    TicketArguments,
)
from runweave.checkpoints import operation_identity
from runweave.effects import EffectConfirmation, EffectRunner
from runweave.security import ContentPolicy, Principal
from runweave.storage.postgres import Claim, PostgresRuns


async def expire_approvals(conn: AsyncConnection) -> None:
    await conn.execute(
        text("""
        WITH candidates AS (
            SELECT r.id FROM runweave_runs r JOIN runweave_approvals a ON a.run_id = r.id
            WHERE r.finished_at IS NULL AND a.status IN ('pending', 'approved')
                AND a.expires_at <= clock_timestamp()
            ORDER BY a.expires_at LIMIT 128 FOR UPDATE OF r SKIP LOCKED
        ), changed AS (
            UPDATE runweave_runs r SET status = 'TIMED_OUT', finished_at = clock_timestamp(),
                lease_token = NULL, lease_until = NULL,
                approval_wait_seconds = approval_wait_seconds + COALESCE(
                    extract(epoch FROM clock_timestamp() - approval_wait_started_at), 0),
                approval_wait_started_at = NULL,
                error = '{"code":"APPROVAL_EXPIRED","message":"Approval expired"}'::jsonb
            FROM candidates c WHERE r.id = c.id RETURNING r.id, r.attempt
        ), attempts AS (
            UPDATE runweave_attempts a SET finished_at = clock_timestamp(), outcome = 'TIMED_OUT'
            FROM changed c WHERE a.run_id = c.id AND a.attempt = c.attempt
                AND a.finished_at IS NULL
        ) UPDATE runweave_approvals SET status = 'expired'
          WHERE run_id IN (SELECT id FROM changed)
    """)
    )


def approval_snapshot(row: RowMapping) -> ApprovalSnapshot:
    return ApprovalSnapshot(
        id=row["id"],
        run_id=row["run_id"],
        tenant=row["tenant"],
        call=ToolCall.model_validate(row["call"]),
        argument_digest=row["argument_digest"],
        policy_version=row["policy_version"],
        expires_at=row["expires_at"],
        status=row["status"],
        decided_by=row["decided_by"],
        ticket_id=row["ticket_id"],
    )


class PostgresApprovals:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def get(self, run_id: UUID, principal: Principal) -> ApprovalSnapshot | None:
        principal.require("runs:read")
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("""
                SELECT a.*, t.ticket_id FROM runweave_approvals a
                JOIN runweave_runs r ON r.id = a.run_id
                LEFT JOIN runweave_sandbox_tickets t ON t.run_id = a.run_id
                WHERE r.id = :id AND r.principal->>'tenant' = :tenant
                    AND r.principal->>'subject' = :subject
            """),
                        {"id": run_id, "tenant": principal.tenant, "subject": principal.subject},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return approval_snapshot(row) if row is not None else None

    async def decide(
        self, run_id: UUID, principal: Principal, decision: ApprovalDecision
    ) -> ApprovalSnapshot | None:
        principal.require("approvals:decide")
        principal.require("runs:read")
        conflict = False
        async with self.engine.begin() as conn:
            # All approval paths lock the run first, then the approval row.
            run = (
                (
                    await conn.execute(
                        text("""
                SELECT id, status FROM runweave_runs WHERE id = :id
                    AND principal->>'tenant' = :tenant AND principal->>'subject' = :subject
                FOR UPDATE
            """),
                        {"id": run_id, "tenant": principal.tenant, "subject": principal.subject},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                return None
            row = (
                (
                    await conn.execute(
                        text("""
                SELECT *, expires_at > clock_timestamp() AS live
                FROM runweave_approvals WHERE run_id = :id FOR UPDATE
            """),
                        {"id": run_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            bound = (
                decision.approval_id == row["id"]
                and decision.call_id == row["call"]["id"]
                and decision.argument_digest == row["argument_digest"]
                and decision.policy_version == row["policy_version"] == TICKET_POLICY
                and decision.expires_at == row["expires_at"]
                and row["tenant"] == principal.tenant
            )
            if not bound:
                raise ApprovalError("APPROVAL_BINDING_MISMATCH")
            if row["decision"] is not None:
                if row["decision"] != decision.decision:
                    raise ApprovalError("APPROVAL_DECISION_CONFLICT")
            elif not row["live"] or run["status"] != "WAITING_APPROVAL":
                conflict = True
            else:
                approved = decision.decision == "approve"
                principal.require("approvals:decide")
                changed = await conn.scalar(
                    text("""
                    UPDATE runweave_approvals SET status = :status, decision = :decision,
                        decided_at = clock_timestamp(), decided_by = :subject
                    WHERE run_id = :id AND expires_at > clock_timestamp() RETURNING id
                """),
                    {
                        "id": run_id,
                        "status": "approved" if approved else "rejected",
                        "decision": decision.decision,
                        "subject": principal.subject,
                    },
                )
                if changed is None:
                    raise ApprovalError("APPROVAL_NOT_PENDING")
                await conn.execute(
                    text("""
                    UPDATE runweave_runs SET status = :status,
                        deadline_at = deadline_at + (clock_timestamp() - approval_wait_started_at),
                        approval_wait_seconds = approval_wait_seconds +
                            extract(epoch FROM clock_timestamp() - approval_wait_started_at),
                        approval_wait_started_at = NULL, available_at = clock_timestamp(),
                        finished_at = CASE WHEN :approved THEN NULL ELSE clock_timestamp() END,
                        error = CASE WHEN :approved THEN NULL ELSE
                            '{"code":"APPROVAL_REJECTED","message":"Approval rejected"}'::jsonb END
                    WHERE id = :id
                """),
                    {
                        "id": run_id,
                        "status": "QUEUED" if approved else "FAILED",
                        "approved": approved,
                    },
                )
        if conflict:
            raise ApprovalError("APPROVAL_NOT_PENDING")
        return await self.get(run_id, principal)


class PostgresTicketGate:
    def __init__(
        self,
        engine: AsyncEngine,
        claim: Claim,
        *,
        ttl: float,
        limit: int,
        authorize: Callable[[], Principal],
        content_policy: ContentPolicy,
    ) -> None:
        self.engine = engine
        self.claim = claim
        self.ttl = ttl
        self.limit = limit
        self.authorize = authorize
        self.content_policy = content_policy
        self.store = PostgresRuns(engine).checkpoints(claim)

    async def _lock(self, conn: AsyncConnection) -> RowMapping:
        principal = self.authorize()
        principal.require("tools:create_ticket")
        row = (
            (
                await conn.execute(
                    text("""
            SELECT id, checkpoint FROM runweave_runs WHERE id = :id AND status = 'RUNNING'
                AND lease_token = :token AND lease_until > clock_timestamp()
                AND deadline_at > clock_timestamp() FOR UPDATE
        """),
                    {"id": self.claim.snapshot.id, "token": self.claim.token},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApprovalError("CHECKPOINT_LEASE_LOST")
        return row

    async def run(self, incident: IncidentRequest, result: IncidentResult) -> None:
        from runweave.agents.ticket import ticket_agent

        self.authorize().require("tools:create_ticket")
        arguments = TicketArguments(
            service=incident.service, title=result.summary[:200], severity=result.severity
        )
        self.content_policy.check(arguments.model_dump_json())
        async with self.engine.connect() as conn:
            saved = (
                (
                    await conn.execute(
                        text("SELECT * FROM runweave_approvals WHERE run_id = :id"),
                        {"id": self.claim.snapshot.id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if saved is None:
            args, messages = await ticket_agent(arguments, TICKET_CALL_ID, self._never_execute)
            if messages is None:
                raise ApprovalError("APPROVAL_REQUIRED")
            call = ToolCall(id=TICKET_CALL_ID, name="create_ticket", arguments=args)
            _, digest = operation_identity(self.store.run_id, "effect", call.name, call.id, args)
            self.content_policy.check(messages)
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT pg_advisory_xact_lock(795940314, 2)"))
                await self._lock(conn)
                count = await conn.scalar(
                    text("""
                    SELECT count(*) FROM runweave_approvals a
                    JOIN runweave_runs r ON r.id = a.run_id
                    WHERE a.status IN ('pending', 'approved') AND r.finished_at IS NULL
                """)
                )
                if count is not None and count >= self.limit:
                    raise ApprovalError("APPROVAL_CAPACITY_EXCEEDED")
                await conn.execute(
                    text("""
                    INSERT INTO runweave_approvals
                        (id, run_id, tenant, call, argument_digest, policy_version,
                         messages, status, expires_at)
                    VALUES (:approval, :id, :tenant, CAST(:call AS jsonb), :digest, :policy,
                        CAST(:messages AS jsonb), 'pending',
                        clock_timestamp() + :ttl * interval '1 second')
                """),
                    {
                        "approval": uuid4(),
                        "id": self.claim.snapshot.id,
                        "tenant": self.claim.snapshot.principal.tenant,
                        "call": call.model_dump_json(),
                        "digest": digest,
                        "policy": TICKET_POLICY,
                        "messages": messages,
                        "ttl": self.ttl,
                    },
                )
                changed = await conn.scalar(
                    text("""
                    UPDATE runweave_runs SET status = 'WAITING_APPROVAL', lease_token = NULL,
                        lease_until = NULL, approval_wait_started_at = clock_timestamp(),
                        max_attempts = max_attempts + 1 WHERE id = :id AND status = 'RUNNING'
                            AND lease_token = :token AND lease_until > clock_timestamp()
                            AND deadline_at > clock_timestamp() RETURNING id
                """),
                    {"id": self.claim.snapshot.id, "token": self.claim.token},
                )
                if changed is None:
                    raise ApprovalError("CHECKPOINT_LEASE_LOST")
                await conn.execute(
                    text("""
                    UPDATE runweave_attempts SET finished_at = clock_timestamp(),
                        outcome = 'WAITING_APPROVAL' WHERE run_id = :id AND lease_token = :token
                """),
                    {"id": self.claim.snapshot.id, "token": self.claim.token},
                )
            raise AwaitingApproval()
        if saved["status"] not in ("approved", "executed"):
            raise ApprovalError("APPROVAL_NOT_AUTHORIZED")
        call = await self._validate_saved(saved, arguments)

        async def perform(ticket: TicketArguments) -> str:
            if ticket != arguments:
                raise ApprovalError("APPROVAL_BINDING_MISMATCH")
            effect = SandboxTicketEffect(self, call, arguments)
            evidence = await EffectRunner(self.store).run(call, effect)
            self.content_policy.check(evidence.content)
            return evidence.id

        await ticket_agent(arguments, call.id, perform, json.dumps(saved["messages"]))

    async def _never_execute(self, arguments: TicketArguments) -> str:
        raise ApprovalError("APPROVAL_REQUIRED")

    async def _validate_saved(self, saved: RowMapping, arguments: TicketArguments) -> ToolCall:
        call = ToolCall.model_validate(saved["call"])
        _, digest = operation_identity(
            self.store.run_id, "effect", call.name, call.id, call.arguments
        )
        if (
            saved["tenant"] != self.claim.snapshot.principal.tenant
            or saved["policy_version"] != TICKET_POLICY
            or digest != saved["argument_digest"]
            or call.id != TICKET_CALL_ID
            or call.name != "create_ticket"
            or json.loads(call.arguments) != {"ticket": arguments.model_dump(mode="json")}
        ):
            raise ApprovalError("APPROVAL_BINDING_MISMATCH")
        self.content_policy.check(json.dumps(saved["messages"]))
        return call


class SandboxTicketEffect:
    idempotent = True

    def __init__(
        self, gate: PostgresTicketGate, call: ToolCall, arguments: TicketArguments
    ) -> None:
        self.gate = gate
        self.call = call
        self.arguments = arguments

    async def perform(self, operation_id: str) -> Evidence:
        async with self.gate.engine.begin() as conn:
            await self.gate._lock(conn)
            saved = (
                (
                    await conn.execute(
                        text("""
                SELECT *, expires_at > clock_timestamp() AS live FROM runweave_approvals
                WHERE run_id = :id FOR UPDATE
            """),
                        {"id": self.gate.claim.snapshot.id},
                    )
                )
                .mappings()
                .one()
            )
            await self.gate._validate_saved(saved, self.arguments)
            if saved["status"] != "approved" or not saved["live"]:
                raise ApprovalError("APPROVAL_NOT_AUTHORIZED")
            self.gate.content_policy.check(self.arguments.model_dump_json())
            self.gate.authorize().require("tools:create_ticket")
            ticket_id = f"sandbox-ticket:{operation_id[-24:]}"
            confirmed_id = await conn.scalar(
                text("""
                INSERT INTO runweave_sandbox_tickets
                    (operation_id, run_id, tenant, arguments, ticket_id)
                SELECT :operation, :run, :tenant, CAST(:arguments AS jsonb), :ticket
                FROM runweave_runs r JOIN runweave_approvals a ON a.run_id = r.id
                WHERE r.id = :run AND r.status = 'RUNNING' AND r.lease_token = :token
                    AND r.lease_until > clock_timestamp() AND r.deadline_at > clock_timestamp()
                    AND a.status = 'approved' AND a.expires_at > clock_timestamp()
                ON CONFLICT (operation_id) DO UPDATE SET operation_id = EXCLUDED.operation_id
                WHERE runweave_sandbox_tickets.run_id = EXCLUDED.run_id
                    AND runweave_sandbox_tickets.tenant = EXCLUDED.tenant
                    AND runweave_sandbox_tickets.arguments = EXCLUDED.arguments
                RETURNING ticket_id
            """),
                {
                    "operation": operation_id,
                    "run": self.gate.claim.snapshot.id,
                    "tenant": saved["tenant"],
                    "arguments": self.arguments.model_dump_json(),
                    "ticket": ticket_id,
                    "token": self.gate.claim.token,
                },
            )
            if confirmed_id != ticket_id:
                raise ApprovalError("APPROVAL_NOT_AUTHORIZED")
            await conn.execute(
                text("""
                UPDATE runweave_approvals SET status = 'executed' WHERE run_id = :id
            """),
                {"id": self.gate.claim.snapshot.id},
            )
        return Evidence(ticket_id, "Confirmed sandbox ticket")

    async def reconcile(self, operation_id: str) -> EffectConfirmation:
        async with self.gate.engine.connect() as conn:
            ticket_id = await conn.scalar(
                text("""
                SELECT ticket_id FROM runweave_sandbox_tickets
                WHERE operation_id = :operation AND run_id = :id AND tenant = :tenant
            """),
                {
                    "operation": operation_id,
                    "id": self.gate.claim.snapshot.id,
                    "tenant": self.gate.claim.snapshot.principal.tenant,
                },
            )
        return EffectConfirmation(
            status="committed" if ticket_id else "absent",
            evidence=Evidence(ticket_id, "Confirmed sandbox ticket") if ticket_id else None,
        )
