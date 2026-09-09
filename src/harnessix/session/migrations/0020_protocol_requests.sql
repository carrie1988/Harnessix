CREATE TABLE protocol_requests (
    client_instance_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    method TEXT NOT NULL,
    params_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('accepted', 'completed', 'failed')),
    outcome_json TEXT,
    outcome_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (client_instance_id, request_id),
    CHECK ((outcome_json IS NULL) = (outcome_sha256 IS NULL)),
    CHECK ((state = 'accepted') = (outcome_json IS NULL))
);
CREATE INDEX protocol_requests_state_idx ON protocol_requests(state, updated_at);
