CREATE TABLE IF NOT EXISTS conflict_cases (
    id TEXT PRIMARY KEY,
    pair_key TEXT NOT NULL UNIQUE,
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT,
    rationale TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (left_claim_id) REFERENCES claims(id),
    FOREIGN KEY (right_claim_id) REFERENCES claims(id)
);

CREATE INDEX IF NOT EXISTS idx_conflict_cases_status
ON conflict_cases(status, resolved_at);
