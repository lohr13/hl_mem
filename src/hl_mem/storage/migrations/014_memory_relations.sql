CREATE TABLE IF NOT EXISTS memory_relations (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (from_id) REFERENCES claims(id),
    FOREIGN KEY (to_id) REFERENCES claims(id)
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON memory_relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON memory_relations(to_id);
