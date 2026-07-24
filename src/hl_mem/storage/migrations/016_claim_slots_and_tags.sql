-- Phase 17 Stage 1: additive operational slots and open retrieval tags.
ALTER TABLE claims ADD COLUMN canonical_slot TEXT NULL;
ALTER TABLE claims ADD COLUMN topic_tags_json TEXT NULL;

CREATE INDEX idx_claims_slot ON claims(namespace_key, canonical_slot, status)
    WHERE canonical_slot IS NOT NULL;
