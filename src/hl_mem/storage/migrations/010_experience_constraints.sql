ALTER TABLE episodes RENAME TO episodes_unchecked;

CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reward REAL CHECK (reward IS NULL OR (reward >= 0.0 AND reward <= 1.0)),
    outcome_summary TEXT,
    scope_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO episodes SELECT * FROM episodes_unchecked;

ALTER TABLE traces RENAME TO traces_unchecked;
CREATE TABLE traces (
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
INSERT INTO traces SELECT * FROM traces_unchecked;
DROP TABLE traces_unchecked;
DROP TABLE episodes_unchecked;

ALTER TABLE policies RENAME TO policies_unchecked;
CREATE TABLE policies (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    trigger TEXT NOT NULL,
    procedure TEXT NOT NULL,
    boundary TEXT NOT NULL DEFAULT '{}',
    support INTEGER NOT NULL DEFAULT 0,
    gain REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'active', 'retired')),
    procedure_status TEXT NOT NULL DEFAULT 'probationary'
        CHECK (procedure_status IN ('probationary', 'active', 'retired')),
    reliability REAL NOT NULL DEFAULT 0.0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(namespace_key, trigger)
);
INSERT INTO policies SELECT * FROM policies_unchecked;
DROP TABLE policies_unchecked;
CREATE INDEX IF NOT EXISTS idx_policies_trigger_status ON policies(namespace_key, trigger, status);
