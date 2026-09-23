CREATE INDEX agent_threads_archive_list_idx ON agent_threads (
    COALESCE(json_type(snapshot_json, '$.archive') NOT IN ('null'), 0),
    thread_id
);
