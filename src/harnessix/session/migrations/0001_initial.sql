CREATE TABLE agent_events (
    thread_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_id TEXT NOT NULL UNIQUE,
    event_json TEXT NOT NULL,
    PRIMARY KEY (thread_id, sequence)
);
CREATE TABLE agent_threads (
    thread_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL
);
