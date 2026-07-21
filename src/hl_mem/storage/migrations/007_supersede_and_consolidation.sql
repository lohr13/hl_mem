ALTER TABLE claims ADD COLUMN superseded_by_id TEXT REFERENCES claims(id);

DELETE FROM evidence_links
WHERE rowid NOT IN (
    SELECT min(rowid) FROM evidence_links
    GROUP BY derived_type, derived_id, evidence_type, evidence_id, relation
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique_relation
ON evidence_links(derived_type, derived_id, evidence_type, evidence_id, relation);

CREATE TABLE IF NOT EXISTS consolidation_pairs (
    pair_key TEXT NOT NULL,
    embedding_signature TEXT NOT NULL,
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    similarity REAL NOT NULL,
    decision TEXT NOT NULL,
    confidence REAL,
    rationale TEXT,
    run_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (pair_key, embedding_signature)
);

INSERT INTO claims_fts(claims_fts) VALUES ('delete-all');
INSERT INTO claims_fts(rowid, search_text)
SELECT rowid, coalesce(subject_entity_id, '') || ' ' || coalesce(predicate, '') || ' ' ||
       coalesce(value_json, '')
FROM claims;
