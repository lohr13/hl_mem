ALTER TABLE dedup_pairs
ADD COLUMN pair_source TEXT NOT NULL DEFAULT 'legacy'
CHECK (pair_source IN ('legacy', 'ingest', 'maintenance'));

ALTER TABLE dedup_pairs
ADD COLUMN new_claim_id TEXT REFERENCES claims(id);

CREATE INDEX idx_dedup_pairs_pending_new_claim
ON dedup_pairs(new_claim_id, similarity DESC, created_at)
WHERE decision IS NULL
  AND pair_source = 'ingest'
  AND new_claim_id IS NOT NULL;
