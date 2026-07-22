ALTER TABLE derivations ADD COLUMN proof_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_derivations_kind_status
ON derivations(kind, status);
