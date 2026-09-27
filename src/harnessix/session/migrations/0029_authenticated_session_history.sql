-- 仅建立空证明表；旧事件、投影、Artifact与普通SHA不回写、不补签。
CREATE TABLE agent_publication_store (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    seal BLOB NOT NULL CHECK (length(seal) BETWEEN 1 AND 4096)
) STRICT;
CREATE TABLE agent_event_publications (
    event_id TEXT PRIMARY KEY REFERENCES agent_events(event_id),
    prefix_sha256 TEXT NOT NULL CHECK (length(prefix_sha256) = 64),
    seal BLOB NOT NULL CHECK (length(seal) BETWEEN 1 AND 4096)
) STRICT;
CREATE TABLE agent_projection_publications (
    thread_id TEXT PRIMARY KEY REFERENCES agent_threads(thread_id),
    seal BLOB NOT NULL CHECK (length(seal) BETWEEN 1 AND 4096)
) STRICT;
