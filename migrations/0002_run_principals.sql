ALTER TABLE runweave_runs ADD COLUMN principal jsonb NOT NULL DEFAULT
    '{"tenant":"local","subject":"developer","scopes":["runs:submit","runs:read","runs:cancel","tools:check_health","tools:lookup_runbook"],"expires_at": null}'::jsonb;
ALTER TABLE runweave_runs ALTER COLUMN principal DROP DEFAULT;
CREATE INDEX runweave_tenant_unfinished_idx ON runweave_runs ((principal->>'tenant'))
    WHERE finished_at IS NULL;
