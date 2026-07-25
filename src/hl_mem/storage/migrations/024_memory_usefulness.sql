CREATE TABLE IF NOT EXISTS memory_usefulness (
    memory_type TEXT NOT NULL CHECK (memory_type IN ('claim','observation','policy')),
    memory_id TEXT NOT NULL,
    helpful_count INTEGER NOT NULL DEFAULT 0 CHECK (helpful_count >= 0),
    unhelpful_count INTEGER NOT NULL DEFAULT 0 CHECK (unhelpful_count >= 0),
    success_sum REAL NOT NULL DEFAULT 0.0,
    outcome_count INTEGER NOT NULL DEFAULT 0 CHECK (outcome_count >= 0),
    usefulness_score REAL NOT NULL DEFAULT 0.5 CHECK (usefulness_score >= 0.0 AND usefulness_score <= 1.0),
    retention_bonus_days INTEGER NOT NULL DEFAULT 0 CHECK (retention_bonus_days >= 0),
    last_positive_at TEXT,
    last_negative_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (memory_type, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_usefulness_score
ON memory_usefulness(memory_type, usefulness_score DESC, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_memory_created
ON retrieval_feedback(memory_type, memory_id, created_at);

CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_query_memory
ON retrieval_feedback(query_id, memory_type, memory_id);
