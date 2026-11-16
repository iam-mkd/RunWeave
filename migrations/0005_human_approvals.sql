ALTER TABLE runweave_runs DROP CONSTRAINT runweave_runs_status_check;
ALTER TABLE runweave_runs ADD CONSTRAINT runweave_runs_status_check CHECK (status IN (
    'QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED'
));
ALTER TABLE runweave_runs ADD COLUMN approval_wait_started_at timestamptz;
ALTER TABLE runweave_runs DROP CONSTRAINT runweave_runs_max_attempts_check;
ALTER TABLE runweave_runs ADD CONSTRAINT runweave_runs_max_attempts_check
    CHECK (max_attempts BETWEEN 1 AND 11);
ALTER TABLE runweave_runs ADD COLUMN approval_wait_seconds double precision NOT NULL DEFAULT 0
    CHECK (approval_wait_seconds >= 0);
CREATE TABLE runweave_approvals (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES runweave_runs(id) ON DELETE CASCADE,
    tenant text NOT NULL,
    call jsonb NOT NULL,
    argument_digest text NOT NULL CHECK (length(argument_digest) = 64),
    policy_version text NOT NULL,
    messages jsonb NOT NULL CHECK (octet_length(messages::text) <= 65536),
    status text NOT NULL CHECK (status IN
        ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'executed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    decided_by text,
    decision text CHECK (decision IN ('approve', 'reject')),
    CHECK (expires_at > created_at)
);
CREATE INDEX runweave_approval_expiry_idx ON runweave_approvals(expires_at)
    WHERE status IN ('pending', 'approved');
CREATE TABLE runweave_sandbox_tickets (
    operation_id text PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES runweave_runs(id) ON DELETE CASCADE,
    tenant text NOT NULL,
    arguments jsonb NOT NULL,
    ticket_id text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
