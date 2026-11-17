CREATE TABLE runweave_memory (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    owner_subject text NOT NULL,
    project text,
    service text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('health_observation', 'runbook_excerpt')),
    content text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 2048),
    provenance jsonb NOT NULL,
    policy_version text NOT NULL CHECK (policy_version = 'observed-evidence-v1'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX runweave_memory_source_idx ON runweave_memory
    (tenant, owner_subject, COALESCE(project, ''), (provenance->>'run_id'),
     (provenance->>'evidence_id'));
CREATE INDEX runweave_memory_recall_idx ON runweave_memory
    (tenant, project, service, created_at DESC, id);
CREATE INDEX runweave_memory_subject_idx ON runweave_memory
    (tenant, owner_subject, service, created_at DESC, id) WHERE project IS NULL;
CREATE INDEX runweave_memory_expiry_idx ON runweave_memory (expires_at);
