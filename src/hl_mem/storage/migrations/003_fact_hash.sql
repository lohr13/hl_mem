ALTER TABLE claims ADD COLUMN fact_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_claims_fact_hash ON claims(fact_hash) WHERE fact_hash IS NOT NULL;
