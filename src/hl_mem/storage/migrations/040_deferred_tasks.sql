-- Generic, bounded maintenance queue for work that must outlive a failed job.
CREATE TABLE deferred_tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'abandoned')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    run_after TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_deferred_tasks_due
ON deferred_tasks(status, run_after, created_at);

CREATE INDEX idx_deferred_tasks_resource
ON deferred_tasks(resource_type, resource_id, status);
