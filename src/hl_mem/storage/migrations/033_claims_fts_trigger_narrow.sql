DROP TRIGGER claims_au;

CREATE TRIGGER claims_au AFTER UPDATE OF index_text ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, COALESCE(old.index_text, ''));
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, COALESCE(new.index_text, ''));
END;
