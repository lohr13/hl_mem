DROP TRIGGER IF EXISTS claims_ai;
DROP TRIGGER IF EXISTS claims_ad;
DROP TRIGGER IF EXISTS claims_au;

CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.subject_entity_id, '') || ' ' ||
          coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.subject_entity_id, '') || ' ' ||
          coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
END;
CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.subject_entity_id, '') || ' ' ||
          coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.subject_entity_id, '') || ' ' ||
          coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
