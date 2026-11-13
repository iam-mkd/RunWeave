CREATE TABLE runweave_runs (
    id uuid PRIMARY KEY,
    incident jsonb NOT NULL,
    status text NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED'
    )),
    submitted_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    result jsonb,
    error jsonb,
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    lease_token uuid,
    lease_until timestamptz,
    CHECK ((status = 'RUNNING') = (lease_token IS NOT NULL)),
    CHECK ((status = 'RUNNING') = (lease_until IS NOT NULL)),
    CHECK ((status IN ('SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED')) = (finished_at IS NOT NULL)),
    CHECK ((status = 'SUCCEEDED') = (result IS NOT NULL)),
    CHECK ((status IN ('FAILED', 'TIMED_OUT')) = (error IS NOT NULL))
);
CREATE INDEX runweave_queue_idx ON runweave_runs (available_at, submitted_at, id)
    WHERE status = 'QUEUED';
CREATE INDEX runweave_deadline_idx ON runweave_runs (deadline_at) WHERE finished_at IS NULL;
CREATE INDEX runweave_lease_idx ON runweave_runs (lease_until) WHERE status = 'RUNNING';
CREATE INDEX runweave_history_idx ON runweave_runs (finished_at DESC, id)
    WHERE finished_at IS NOT NULL;

CREATE TABLE runweave_attempts (
    run_id uuid NOT NULL REFERENCES runweave_runs(id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    lease_token uuid NOT NULL UNIQUE,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    outcome text,
    PRIMARY KEY (run_id, attempt)
);
