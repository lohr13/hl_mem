CREATE TABLE IF NOT EXISTS dedup_pairs (
    id TEXT PRIMARY KEY,
    pair_key TEXT UNIQUE NOT NULL,
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    similarity REAL NOT NULL,
    embedding_text_version TEXT,
    policy_version TEXT,
    predicate TEXT,
    decision TEXT CHECK (decision IN ('equivalent', 'distinct', 'uncertain') OR decision IS NULL),
    judge_confidence REAL,
    judge_reason TEXT,
    judge_model TEXT,
    reviewed_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (left_claim_id) REFERENCES claims(id),
    FOREIGN KEY (right_claim_id) REFERENCES claims(id)
);

CREATE INDEX idx_dedup_pairs_decision ON dedup_pairs(decision) WHERE decision IS NULL;
CREATE INDEX idx_dedup_pairs_namespace ON dedup_pairs(namespace_key);
