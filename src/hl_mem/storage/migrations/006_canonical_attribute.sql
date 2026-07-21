ALTER TABLE claims ADD COLUMN canonical_attribute TEXT NOT NULL DEFAULT 'custom.unknown';
ALTER TABLE claims ADD COLUMN conflict_key_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE claims ADD COLUMN legacy_conflict_key TEXT;

CREATE INDEX idx_claims_v2_key ON claims(namespace_key, conflict_key, status);

UPDATE derivations SET status = 'stale'
WHERE kind = 'observation' AND status = 'active';
