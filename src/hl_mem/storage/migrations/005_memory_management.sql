ALTER TABLE claims ADD COLUMN scope TEXT NOT NULL DEFAULT 'permanent'
    CHECK (scope IN ('temporal', 'permanent'));
ALTER TABLE claims ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0
    CHECK (access_count >= 0);
ALTER TABLE claims ADD COLUMN last_accessed_at TEXT;
ALTER TABLE claims ADD COLUMN last_decayed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_claims_decay
    ON claims(status, scope, last_accessed_at, recorded_from)
    WHERE status IN ('active', 'disputed');

DROP TRIGGER IF EXISTS claims_au;
CREATE TRIGGER claims_au
AFTER UPDATE OF subject_entity_id, predicate, value_json ON claims
BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.subject_entity_id, '') || ' ' ||
          coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.subject_entity_id, '') || ' ' ||
          coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
