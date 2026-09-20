-- Agent Session v26：为Session、Protocol Request和Artifact建立不可变清理计划与可恢复进度。
ALTER TABLE agent_artifacts ADD COLUMN created_at TEXT;
UPDATE agent_artifacts SET created_at = expires_at WHERE created_at IS NULL;
CREATE INDEX agent_artifacts_created_at ON agent_artifacts(created_at);
CREATE TABLE store_maintenance_plans (
    plan_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    created_at TEXT NOT NULL
) STRICT;
CREATE TABLE store_maintenance_items (
    plan_id TEXT NOT NULL REFERENCES store_maintenance_plans(plan_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('artifact_body', 'protocol_request', 'session_thread')),
    key_json TEXT NOT NULL,
    key_sha256 TEXT NOT NULL CHECK (length(key_sha256) = 64),
    precondition_sha256 TEXT NOT NULL CHECK (length(precondition_sha256) = 64),
    PRIMARY KEY (plan_id, ordinal)
) STRICT;
CREATE TABLE store_maintenance_progress (
    plan_id TEXT PRIMARY KEY REFERENCES store_maintenance_plans(plan_id),
    state TEXT NOT NULL CHECK (state IN ('planned', 'running', 'completed')),
    next_ordinal INTEGER NOT NULL DEFAULT 0 CHECK (next_ordinal >= 0),
    applied_items INTEGER NOT NULL DEFAULT 0 CHECK (applied_items >= 0),
    skipped_items INTEGER NOT NULL DEFAULT 0 CHECK (skipped_items >= 0),
    backup_sha256 TEXT CHECK (backup_sha256 IS NULL OR length(backup_sha256) = 64),
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;
