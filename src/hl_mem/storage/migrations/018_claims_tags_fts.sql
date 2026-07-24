-- Independent topic_tags channel for deterministic tag retrieval.
CREATE VIRTUAL TABLE IF NOT EXISTS claims_tags_fts USING fts5(
    tags_text,
    content='claims',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS claims_tags_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;

CREATE TRIGGER IF NOT EXISTS claims_tags_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
END;

CREATE TRIGGER IF NOT EXISTS claims_tags_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;

INSERT INTO claims_tags_fts(rowid, tags_text)
SELECT rowid, COALESCE(topic_tags_json, '') FROM claims;
