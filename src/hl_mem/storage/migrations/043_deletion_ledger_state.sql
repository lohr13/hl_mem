CREATE TABLE deletion_ledger_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    ledger_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    bound_at TEXT NOT NULL,
    last_identity_hash TEXT,
    last_applied_at TEXT
);
