PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT,
    request_fingerprint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    tool_json TEXT NOT NULL,
    status TEXT NOT NULL,
    policy_json TEXT,
    approval_json TEXT,
    result_json TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS actions_tenant_idempotency_uq
    ON actions (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS actions_status_lease_idx
    ON actions (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS action_events (
    action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_id, sequence)
);

CREATE TABLE IF NOT EXISTS demo_issues (
    issue_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);
