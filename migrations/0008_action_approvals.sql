-- An agent waiting on ctx.request_approval keeps its lease (it waits in place), but a parked
-- ticket approval (milestone 12) does not. Every other state keeps the original rule.
ALTER TABLE runweave_runs DROP CONSTRAINT runweave_runs_check;
ALTER TABLE runweave_runs DROP CONSTRAINT runweave_runs_check1;
ALTER TABLE runweave_runs ADD CONSTRAINT runweave_runs_lease_check CHECK (
    CASE status WHEN 'RUNNING' THEN lease_token IS NOT NULL
        WHEN 'WAITING_APPROVAL' THEN true ELSE lease_token IS NULL END
);
ALTER TABLE runweave_runs ADD CONSTRAINT runweave_runs_lease_pair_check
    CHECK ((lease_token IS NULL) = (lease_until IS NULL));
CREATE TABLE runweave_action_approvals (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runweave_runs(id) ON DELETE CASCADE,
    tenant text NOT NULL,
    agent text NOT NULL CHECK (agent ~ '^[a-z][a-z0-9-]{0,62}$'),
    action_key text NOT NULL CHECK (action_key ~ '^[a-z][a-z0-9_.:-]{0,99}$'),
    arguments jsonb NOT NULL CHECK (octet_length(arguments::text) <= 65536),
    argument_digest text NOT NULL CHECK (argument_digest ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 100),
    status text NOT NULL CHECK (status IN
        ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    decision text CHECK (decision IN ('approve', 'reject')),
    decided_by text,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at),
    UNIQUE (run_id, action_key, argument_digest)
);
CREATE INDEX runweave_action_approvals_pending ON runweave_action_approvals (tenant, expires_at)
    WHERE status = 'pending';
COMMENT ON TABLE runweave_action_approvals IS
    'Approvals requested by agents through ctx.request_approval (milestone 23)'
