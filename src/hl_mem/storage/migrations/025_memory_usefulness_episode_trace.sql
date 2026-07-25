CREATE TABLE memory_usefulness_backup AS
SELECT * FROM memory_usefulness;

DROP TABLE memory_usefulness;

CREATE TABLE memory_usefulness (
    memory_type TEXT NOT NULL
        CHECK (memory_type IN ('claim','observation','policy','episode','trace')),
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

INSERT INTO memory_usefulness (
    memory_type,
    memory_id,
    helpful_count,
    unhelpful_count,
    success_sum,
    outcome_count,
    usefulness_score,
    retention_bonus_days,
    last_positive_at,
    last_negative_at,
    updated_at
)
SELECT
    memory_type,
    memory_id,
    helpful_count,
    unhelpful_count,
    success_sum,
    outcome_count,
    usefulness_score,
    retention_bonus_days,
    last_positive_at,
    last_negative_at,
    updated_at
FROM memory_usefulness_backup;

DROP TABLE memory_usefulness_backup;

CREATE INDEX idx_memory_usefulness_score
ON memory_usefulness(memory_type, usefulness_score DESC, updated_at DESC);
