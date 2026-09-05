-- 为已核对Process结果增加独立正文用途；不改写事件或Thread投影。
CREATE TABLE agent_artifacts_v11 (
    artifact_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES agent_threads(thread_id),
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    workspace_scope TEXT NOT NULL CHECK (length(workspace_scope) = 64),
    manifest_json TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 1048576),
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('published', 'expired')),
    body BLOB,
    purpose TEXT NOT NULL DEFAULT 'tool_result'
        CHECK (purpose IN ('tool_result', 'batch_plan', 'batch_effect', 'process_output')),
    UNIQUE (call_id, purpose),
    CHECK ((state = 'published' AND body IS NOT NULL AND length(body) = size_bytes)
        OR (state = 'expired' AND body IS NULL))
);
INSERT INTO agent_artifacts_v11 SELECT * FROM agent_artifacts;
DROP TABLE agent_artifacts;
ALTER TABLE agent_artifacts_v11 RENAME TO agent_artifacts;
CREATE INDEX agent_artifacts_expiry ON agent_artifacts(state, expires_at);
CREATE INDEX agent_artifacts_owner ON agent_artifacts(thread_id, turn_id);
