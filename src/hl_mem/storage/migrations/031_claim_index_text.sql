ALTER TABLE claims ADD COLUMN index_text TEXT;

UPDATE claims
SET index_text = trim(
    COALESCE(subject_entity_id, '') || ' ' || COALESCE(predicate, '') || ' ' || COALESCE(value_json, '') || ' ' ||
    COALESCE(canonical_slot, '') || ' ' || COALESCE(topic_tags_json, '')
);

DROP TRIGGER claims_ai;
DROP TRIGGER claims_ad;
DROP TRIGGER claims_au;
DROP TABLE claims_fts;

CREATE VIRTUAL TABLE claims_fts USING fts5(
  search_text,
  content='claims',
  content_rowid='rowid',
  tokenize='trigram'
);

CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, search_text) VALUES (new.rowid, COALESCE(new.index_text, ''));
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text) VALUES ('delete', old.rowid, COALESCE(old.index_text, ''));
END;
CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text) VALUES ('delete', old.rowid, COALESCE(old.index_text, ''));
  INSERT INTO claims_fts(rowid, search_text) VALUES (new.rowid, COALESCE(new.index_text, ''));
END;

INSERT INTO claims_fts(rowid, search_text)
SELECT rowid, COALESCE(index_text, '') FROM claims;
