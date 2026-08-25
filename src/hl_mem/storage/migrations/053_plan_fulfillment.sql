CREATE TABLE plan_outcomes (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL,
    plan_claim_id TEXT NOT NULL,
    result_claim_id TEXT NOT NULL,
    outcome_type TEXT NOT NULL CHECK (outcome_type IN ('complete','cancel','replace','partial')),
    coordinate_hash TEXT NOT NULL,
    matched_quantity_text TEXT,
    unit TEXT,
    cumulative_quantity_text TEXT,
    match_rule TEXT NOT NULL,
    match_confidence REAL NOT NULL CHECK (match_confidence >= 0 AND match_confidence <= 1),
    input_fingerprint TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('candidate','observed','applied','ambiguous','rejected','rolled_back')
    ),
    relation_id TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE (plan_claim_id, result_claim_id, outcome_type, policy_version),
    FOREIGN KEY (plan_claim_id) REFERENCES claims(id),
    FOREIGN KEY (result_claim_id) REFERENCES claims(id),
    FOREIGN KEY (relation_id) REFERENCES memory_relations(id)
);

CREATE INDEX idx_plan_outcomes_result
ON plan_outcomes(result_claim_id, policy_version, status);

CREATE INDEX idx_plan_outcomes_plan_status
ON plan_outcomes(plan_claim_id, coordinate_hash, policy_version, status, created_at);
