CREATE INDEX idx_audit_cleanup ON audit_log(occurred_at, id);
CREATE INDEX idx_jobs_retention ON jobs(status, updated_at, id);
CREATE INDEX idx_llm_spans_cleanup ON llm_call_spans(started_at, id);
CREATE INDEX idx_retrieval_feedback_retention
ON retrieval_feedback(injected, helpful, task_outcome, created_at, id);

-- Extend the terminal audit vocabulary without weakening the table-level
-- CHECK constraint. Rebuilding is required because SQLite cannot alter CHECK.
ALTER TABLE dedup_pairs RENAME TO dedup_pairs_legacy_046;

CREATE TABLE dedup_pairs (
    id TEXT PRIMARY KEY,
    pair_key TEXT UNIQUE NOT NULL,
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    similarity REAL NOT NULL,
    embedding_text_version TEXT,
    policy_version TEXT,
    predicate TEXT,
    decision TEXT CHECK (
        decision IN ('equivalent', 'distinct', 'uncertain', 'dismissed_below_floor')
        OR decision IS NULL
    ),
    judge_confidence REAL,
    judge_reason TEXT,
    judge_model TEXT,
    reviewed_at TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (left_claim_id) REFERENCES claims(id),
    FOREIGN KEY (right_claim_id) REFERENCES claims(id)
);

INSERT INTO dedup_pairs(
    id,
    pair_key,
    left_claim_id,
    right_claim_id,
    namespace_key,
    similarity,
    embedding_text_version,
    policy_version,
    predicate,
    decision,
    judge_confidence,
    judge_reason,
    judge_model,
    reviewed_at,
    applied_at,
    created_at
)
SELECT
    id,
    pair_key,
    left_claim_id,
    right_claim_id,
    namespace_key,
    similarity,
    embedding_text_version,
    policy_version,
    predicate,
    CASE
        WHEN decision IS NULL AND similarity < 0.88 THEN 'dismissed_below_floor'
        ELSE decision
    END,
    judge_confidence,
    CASE
        WHEN decision IS NULL AND similarity < 0.88 THEN 'v0.28.9_below_current_floor'
        ELSE judge_reason
    END,
    judge_model,
    CASE
        WHEN decision IS NULL AND similarity < 0.88
            THEN COALESCE(reviewed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ELSE reviewed_at
    END,
    applied_at,
    created_at
FROM dedup_pairs_legacy_046;

DROP TABLE dedup_pairs_legacy_046;

CREATE INDEX idx_dedup_pairs_decision ON dedup_pairs(decision) WHERE decision IS NULL;
CREATE INDEX idx_dedup_pairs_namespace ON dedup_pairs(namespace_key);
CREATE INDEX idx_dedup_pairs_retention ON dedup_pairs(decision, reviewed_at, id);
