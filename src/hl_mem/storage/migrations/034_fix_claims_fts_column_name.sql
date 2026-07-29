DROP TRIGGER IF EXISTS claims_ai;
DROP TRIGGER IF EXISTS claims_ad;
DROP TRIGGER IF EXISTS claims_au;
DROP TABLE claims_fts;

CREATE VIRTUAL TABLE claims_fts USING fts5(
  index_text,
  content='claims',
  content_rowid='rowid',
  tokenize='trigram'
);

CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, index_text) VALUES (new.rowid, COALESCE(new.index_text, ''));
END;
CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, index_text)
  VALUES ('delete', old.rowid, COALESCE(old.index_text, ''));
END;
CREATE TRIGGER claims_au AFTER UPDATE OF index_text ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, index_text)
  VALUES ('delete', old.rowid, COALESCE(old.index_text, ''));
  INSERT INTO claims_fts(rowid, index_text)
  VALUES (new.rowid, COALESCE(new.index_text, ''));
END;

INSERT INTO claims_fts(rowid, index_text)
SELECT rowid, COALESCE(index_text, '') FROM claims;
