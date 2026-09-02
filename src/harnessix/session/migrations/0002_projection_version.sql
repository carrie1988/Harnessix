ALTER TABLE agent_threads ADD COLUMN projection_version INTEGER NOT NULL DEFAULT 1 CHECK (projection_version >= 1);
