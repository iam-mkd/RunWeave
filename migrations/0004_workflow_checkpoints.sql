ALTER TABLE runweave_runs ADD COLUMN checkpoint jsonb;
ALTER TABLE runweave_runs ADD COLUMN checkpoint_seq integer NOT NULL DEFAULT 0
    CHECK (checkpoint_seq >= 0);
UPDATE runweave_runs SET status = 'FAILED', finished_at = clock_timestamp(),
    lease_token = NULL, lease_until = NULL,
    error = jsonb_build_object('code', 'CHECKPOINT_UNAVAILABLE',
                              'message', 'Legacy run has no pinned workflow checkpoint')
    WHERE finished_at IS NULL;
UPDATE runweave_attempts a SET finished_at = clock_timestamp(), outcome = 'FAILED'
    FROM runweave_runs r WHERE a.run_id = r.id AND a.finished_at IS NULL
    AND r.error->>'code' = 'CHECKPOINT_UNAVAILABLE';
CREATE TABLE runweave_operations (
    run_id uuid NOT NULL REFERENCES runweave_runs(id) ON DELETE CASCADE,
    operation_id text NOT NULL CHECK (length(operation_id) = 67),
    digest text NOT NULL CHECK (length(digest) = 64),
    kind text NOT NULL CHECK (kind IN ('read', 'effect')),
    status text NOT NULL CHECK (status IN ('started', 'committed', 'needs_resolution')),
    evidence jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    committed_at timestamptz,
    PRIMARY KEY (run_id, operation_id),
    CHECK ((status = 'committed') = (evidence IS NOT NULL)),
    CHECK ((status = 'committed') = (committed_at IS NOT NULL)),
    CHECK (kind = 'effect' OR status != 'needs_resolution')
);
