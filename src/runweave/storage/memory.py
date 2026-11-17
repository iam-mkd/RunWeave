import hashlib
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runweave.checkpoints import CheckpointError, decode_checkpoint
from runweave.core.settings import Settings
from runweave.memory import (
    MEMORY_POLICY,
    FactProvenance,
    MemoryFact,
    MemoryRejected,
    MemorySelection,
    MemoryWrite,
)
from runweave.security import ContentPolicy, PolicyDenied, Principal
from runweave.tools.contracts import HealthObservation, HealthQuery
from runweave.tools.registry import HEALTH, RUNBOOK


async def prune_memory(conn: AsyncConnection) -> None:
    await conn.execute(
        text("""
        DELETE FROM runweave_memory WHERE id IN (
            SELECT id FROM runweave_memory WHERE expires_at <= clock_timestamp()
            ORDER BY expires_at, id LIMIT 128 FOR UPDATE SKIP LOCKED
        )
    """)
    )


class PostgresMemory:
    def __init__(
        self, engine: AsyncEngine, settings: Settings, content_policy: ContentPolicy
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._content_policy = content_policy
        self.recall_bytes = settings.memory_recall_bytes

    def authorize(self, principal: Principal, selection: MemorySelection, scope: str) -> None:
        principal.require(scope)
        if (principal.tenant, principal.subject) in self._settings.auth_revoked_subjects:
            raise PolicyDenied()
        if selection.project is not None:
            members = self._settings.memory_project_memberships.get(principal.tenant, {}).get(
                selection.project, frozenset()
            )
            if principal.subject not in members:
                raise PolicyDenied()

    def _fact(self, row: RowMapping) -> MemoryFact:
        try:
            fact = MemoryFact.model_validate(dict(row))
        except ValidationError:
            raise MemoryRejected("MEMORY_INVALID") from None
        self._content_policy.check(fact.model_dump_json())
        return fact

    async def recall(
        self, service: str, selection: MemorySelection, principal: Principal
    ) -> tuple[MemoryFact, ...]:
        self.authorize(principal, selection, "memory:read")
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                SELECT * FROM runweave_memory
                WHERE tenant = :tenant AND service = :service
                    AND expires_at > clock_timestamp() AND policy_version = :policy
                    AND ((CAST(:project AS text) IS NULL AND project IS NULL
                          AND owner_subject = :subject)
                         OR (CAST(:project AS text) IS NOT NULL AND project = :project))
                ORDER BY created_at DESC, id DESC LIMIT :limit
            """),
                        {
                            "tenant": principal.tenant,
                            "subject": principal.subject,
                            "project": selection.project,
                            "service": service,
                            "policy": MEMORY_POLICY,
                            "limit": self._settings.memory_recall_limit,
                        },
                    )
                )
                .mappings()
                .all()
            )
        self.authorize(principal, selection, "memory:read")
        return tuple(self._fact(row) for row in rows)

    async def write(self, request: MemoryWrite, principal: Principal) -> MemoryFact:
        self.authorize(principal, request, "memory:write")
        principal.require("runs:read")
        async with self._engine.begin() as conn:
            # Bound global, tenant and scope rows atomically across replicas.
            await conn.execute(text("SELECT pg_advisory_xact_lock(795940314, 3)"))
            await prune_memory(conn)
            params = {
                "tenant": principal.tenant,
                "subject": principal.subject,
                "project": request.project,
                "run": str(request.source_run_id),
                "evidence": request.evidence_id,
            }
            key = (
                "tenant = :tenant AND owner_subject = :subject "
                "AND project IS NOT DISTINCT FROM CAST(:project AS text) "
                "AND provenance->>'run_id' = :run AND provenance->>'evidence_id' = :evidence"
            )
            await conn.execute(
                text(
                    f"DELETE FROM runweave_memory WHERE {key} AND expires_at <= clock_timestamp()"
                ),
                params,
            )
            existing = (
                (await conn.execute(text(f"SELECT * FROM runweave_memory WHERE {key}"), params))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                self.authorize(principal, request, "memory:write")
                return self._fact(existing)
            source = (
                (
                    await conn.execute(
                        text("""
                SELECT checkpoint, finished_at FROM runweave_runs WHERE id = :id
                    AND status = 'SUCCEEDED' AND principal->>'tenant' = :tenant
                    AND principal->>'subject' = :subject
            """),
                        {
                            "id": request.source_run_id,
                            "tenant": principal.tenant,
                            "subject": principal.subject,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise MemoryRejected("MEMORY_SOURCE_UNAVAILABLE")
            try:
                checkpoint = decode_checkpoint(source["checkpoint"])
            except CheckpointError:
                raise MemoryRejected("MEMORY_SOURCE_UNAVAILABLE") from None
            if checkpoint.next_node != "done":
                raise MemoryRejected("MEMORY_SOURCE_UNAVAILABLE")
            exchange = next(
                (entry for entry in checkpoint.history if entry.evidence.id == request.evidence_id),
                None,
            )
            if exchange is None or exchange.call.name not in (HEALTH, RUNBOOK):
                raise MemoryRejected("MEMORY_SOURCE_UNAVAILABLE")
            try:
                query = HealthQuery.model_validate_json(exchange.call.arguments, strict=True)
            except ValidationError:
                raise MemoryRejected("MEMORY_SOURCE_INVALID") from None
            if query.service != checkpoint.incident.service:
                raise MemoryRejected("MEMORY_SOURCE_INVALID")
            content = exchange.evidence.content
            if not 1 <= len(content.encode()) <= 2048:
                raise MemoryRejected("MEMORY_SOURCE_TOO_LARGE")
            if exchange.call.name == HEALTH:
                try:
                    health = HealthObservation.model_validate_json(content, strict=True)
                except ValidationError:
                    raise MemoryRejected("MEMORY_SOURCE_INVALID") from None
                if (
                    health.service != checkpoint.incident.service
                    or health.evidence_id != exchange.evidence.id
                ):
                    raise MemoryRejected("MEMORY_SOURCE_INVALID")
            provenance = FactProvenance(
                run_id=request.source_run_id,
                evidence_id=exchange.evidence.id,
                source_sha256=hashlib.sha256(content.encode()).hexdigest(),
                workflow=checkpoint.identity.workflow,
                prompt_sha256=checkpoint.identity.prompt_sha256,
                checkpoint_sequence=checkpoint.sequence,
                observed_at=source["finished_at"],
            )
            self._content_policy.check(content)
            counts = (
                (
                    await conn.execute(
                        text("""
                SELECT count(*) AS total,
                    count(*) FILTER (WHERE tenant = :tenant) AS tenant_count,
                    count(*) FILTER (WHERE tenant = :tenant AND (
                        (CAST(:project AS text) IS NULL AND project IS NULL
                         AND owner_subject = :subject)
                        OR (CAST(:project AS text) IS NOT NULL
                            AND project = :project))) AS scope_count
                FROM runweave_memory
            """),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            if (
                counts["total"] >= self._settings.memory_max_records
                or counts["tenant_count"] >= self._settings.memory_max_per_tenant
                or counts["scope_count"] >= self._settings.memory_max_per_scope
            ):
                raise MemoryRejected("MEMORY_CAPACITY_EXCEEDED")
            self.authorize(principal, request, "memory:write")
            row = (
                (
                    await conn.execute(
                        text("""
                INSERT INTO runweave_memory
                    (id, tenant, owner_subject, project, service, kind, content, provenance,
                     policy_version, expires_at)
                VALUES (:id, :tenant, :subject, :project, :service, :kind, :content,
                    CAST(:provenance AS jsonb), :policy,
                    clock_timestamp() + :ttl * interval '1 second') RETURNING *
            """),
                        {
                            **params,
                            "id": uuid4(),
                            "service": checkpoint.incident.service,
                            "kind": "health_observation"
                            if exchange.call.name == HEALTH
                            else "runbook_excerpt",
                            "content": content,
                            "provenance": provenance.model_dump_json(),
                            "policy": MEMORY_POLICY,
                            "ttl": self._settings.memory_ttl_seconds,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return self._fact(row)

    async def delete(self, fact_id: UUID, principal: Principal) -> bool:
        principal.require("memory:delete")
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("""
                SELECT project FROM runweave_memory WHERE id = :id AND tenant = :tenant
                    AND owner_subject = :subject FOR UPDATE
            """),
                        {"id": fact_id, "tenant": principal.tenant, "subject": principal.subject},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            self.authorize(principal, MemorySelection(), "memory:delete")
            await conn.execute(text("DELETE FROM runweave_memory WHERE id = :id"), {"id": fact_id})
            return True
