DROP TRIGGER IF EXISTS claims_tags_au;

CREATE TRIGGER claims_tags_au
AFTER UPDATE OF topic_tags_json ON claims
BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;
