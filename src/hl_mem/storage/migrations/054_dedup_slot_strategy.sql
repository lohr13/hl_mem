ALTER TABLE dedup_pairs
ADD COLUMN candidate_strategy TEXT NOT NULL DEFAULT 'legacy_no_slot'
CHECK (candidate_strategy IN ('legacy_no_slot', 'slot_cross_subject_v1'));

ALTER TABLE dedup_pairs ADD COLUMN bucket_key TEXT;

ALTER TABLE dedup_pairs ADD COLUMN entity_proof_id TEXT;

ALTER TABLE dedup_pairs
ADD COLUMN auto_apply_eligible INTEGER NOT NULL DEFAULT 0
CHECK (auto_apply_eligible IN (0, 1));

CREATE INDEX idx_dedup_pairs_slot_strategy
ON dedup_pairs(namespace_key, candidate_strategy, auto_apply_eligible, similarity DESC, created_at);
