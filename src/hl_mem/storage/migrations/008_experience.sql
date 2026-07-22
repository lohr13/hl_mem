CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reward REAL,
    outcome_summary TEXT,
    scope_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    action TEXT NOT NULL,
    observation TEXT,
    error_signature TEXT,
    value REAL,
    priority REAL NOT NULL DEFAULT 0.5,
    UNIQUE(episode_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS policies (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    trigger TEXT NOT NULL,
    procedure TEXT NOT NULL,
    boundary TEXT NOT NULL DEFAULT '{}',
    support INTEGER NOT NULL DEFAULT 0,
    gain REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'candidate',
    procedure_status TEXT NOT NULL DEFAULT 'probationary',
    reliability REAL NOT NULL DEFAULT 0.0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(namespace_key, trigger)
);
CREATE INDEX IF NOT EXISTS idx_policies_trigger_status ON policies(namespace_key, trigger, status);

CREATE TABLE IF NOT EXISTS retrieval_feedback (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    rank INTEGER,
    score REAL,
    used_by_model INTEGER NOT NULL DEFAULT 0,
    helpful INTEGER,
    task_outcome REAL,
    created_at TEXT NOT NULL
);
