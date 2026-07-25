CREATE TABLE IF NOT EXISTS relation_proposals (
    id TEXT PRIMARY KEY,
    source_claim_id TEXT NOT NULL,
    target_claim_id TEXT NOT NULL,
    relation TEXT NOT NULL
        CHECK (relation IN ('about','follows','supports','contradicts','summarizes')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    rationale TEXT NOT NULL,
    supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(supporting_claim_ids_json)),
    model TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('audit','auto')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','applied','conflict_created','rejected','failed')),
    decision_reason TEXT,
    relation_id TEXT,
    conflict_case_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (source_claim_id) REFERENCES claims(id),
    FOREIGN KEY (target_claim_id) REFERENCES claims(id),
    FOREIGN KEY (relation_id) REFERENCES memory_relations(id),
    FOREIGN KEY (conflict_case_id) REFERENCES conflict_cases(id),
    UNIQUE (source_claim_id, target_claim_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_relation_proposals_status
ON relation_proposals(status, created_at);

CREATE INDEX IF NOT EXISTS idx_relation_proposals_source
ON relation_proposals(source_claim_id, created_at);
