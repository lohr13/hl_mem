-- Switch FTS5 tables from default unicode61 to trigram tokenizer.
-- trigram handles CJK text via 3-character sliding window, fixing 0% Chinese recall.

-- 1. Recreate claims_fts with trigram
DROP TRIGGER IF EXISTS claims_ai;
DROP TRIGGER IF EXISTS claims_ad;
DROP TRIGGER IF EXISTS claims_au;

DROP TABLE IF EXISTS claims_fts;

CREATE VIRTUAL TABLE claims_fts USING fts5(
    search_text,
    content='claims',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Restore triggers (final version from migration 002 + 005)
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

-- Backfill from existing claims
INSERT INTO claims_fts(rowid, search_text)
SELECT rowid, coalesce(subject_entity_id, '') || ' ' ||
       coalesce(predicate, '') || ' ' || coalesce(value_json, '')
FROM claims;

-- 2. Recreate claims_tags_fts with trigram
DROP TRIGGER IF EXISTS claims_tags_ai;
DROP TRIGGER IF EXISTS claims_tags_ad;
DROP TRIGGER IF EXISTS claims_tags_au;

DROP TABLE IF EXISTS claims_tags_fts;

CREATE VIRTUAL TABLE claims_tags_fts USING fts5(
    tags_text,
    content='claims',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER claims_tags_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;

CREATE TRIGGER claims_tags_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
END;

CREATE TRIGGER claims_tags_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;

-- Backfill tags
INSERT INTO claims_tags_fts(rowid, tags_text)
SELECT rowid, COALESCE(topic_tags_json, '') FROM claims;
