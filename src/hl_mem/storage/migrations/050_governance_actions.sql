CREATE TABLE governance_actions (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    tier TEXT NOT NULL,
    decision TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    resolution_rule TEXT NOT NULL,
    resolver_model TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('observed', 'applied', 'rolled_back', 'failed')),
    created_at TEXT NOT NULL,
    applied_at TEXT,
    rolled_back_at TEXT,
    rollback_reason TEXT,
    UNIQUE (domain, subject_ref, input_fingerprint, policy_version)
);

CREATE INDEX idx_governance_actions_subject
ON governance_actions(domain, subject_ref, created_at);

CREATE INDEX idx_governance_actions_status
ON governance_actions(status, created_at);
