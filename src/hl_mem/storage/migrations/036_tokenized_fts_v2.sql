-- Self-contained FTS5 v2 tables populated with pre-tokenized documents.
CREATE VIRTUAL TABLE claims_fts_v2 USING fts5(
  terms,
  tokenize='unicode61'
);

CREATE VIRTUAL TABLE events_fts_v2 USING fts5(
  terms,
  tokenize='unicode61'
);

CREATE VIRTUAL TABLE claims_tags_fts_v2 USING fts5(
  tags_text,
  tokenize='unicode61'
);

-- Tokenization cannot run in SQLite triggers. Repositories will handle
-- insert/update synchronization; these triggers only remove orphaned rows.
CREATE TRIGGER claims_fts_v2_ad
AFTER DELETE ON claims BEGIN
  DELETE FROM claims_fts_v2 WHERE rowid=old.rowid;
  DELETE FROM claims_tags_fts_v2 WHERE rowid=old.rowid;
END;

CREATE TRIGGER events_fts_v2_ad
AFTER DELETE ON events BEGIN
  DELETE FROM events_fts_v2 WHERE rowid=old.rowid;
END;
