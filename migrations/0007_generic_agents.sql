ALTER TABLE runweave_runs ADD COLUMN agent text NOT NULL DEFAULT 'incident-triage'
    CHECK (agent ~ '^[a-z][a-z0-9-]{0,62}$');
ALTER TABLE runweave_runs ADD COLUMN agent_version text
    CHECK (agent_version IS NULL OR length(agent_version) BETWEEN 1 AND 40);
ALTER TABLE runweave_runs ADD COLUMN steps jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(steps) = 'array' AND jsonb_array_length(steps) <= 200);
CREATE INDEX runweave_runs_owner_recent ON runweave_runs
    ((principal->>'tenant'), (principal->>'subject'), submitted_at DESC, id DESC);
COMMENT ON COLUMN runweave_runs.incident IS
    'Agent input JSON (the incident body for the built-in incident-triage agent)'
