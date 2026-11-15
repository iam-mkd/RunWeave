ALTER TABLE runweave_runs ADD COLUMN budget jsonb;
UPDATE runweave_runs SET status = 'FAILED', finished_at = clock_timestamp(),
    lease_token = NULL, lease_until = NULL,
    error = jsonb_build_object('code', 'BUDGET_ACCOUNTING_UNAVAILABLE',
                              'message', 'Legacy run has no usage accounting')
    WHERE finished_at IS NULL;
UPDATE runweave_attempts a SET finished_at = clock_timestamp(), outcome = 'FAILED'
    FROM runweave_runs r WHERE a.run_id = r.id AND a.finished_at IS NULL
    AND r.error->>'code' = 'BUDGET_ACCOUNTING_UNAVAILABLE';
