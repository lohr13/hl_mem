DROP TABLE IF EXISTS events_fts;

DROP TRIGGER IF EXISTS events_ai;
DROP TRIGGER IF EXISTS events_ad;
DROP TRIGGER IF EXISTS events_au;

CREATE VIRTUAL TABLE events_fts USING fts5(
    content_json,
    content='events',
    content_rowid='rowid',
    tokenize='unicode61'
);

INSERT INTO events_fts(rowid, content_json)
SELECT rowid, content_json FROM events;

CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, content_json)
  VALUES (new.rowid, new.content_json);
END;

CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content_json)
  VALUES ('delete', old.rowid, old.content_json);
END;

CREATE TRIGGER events_au AFTER UPDATE OF content_json ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content_json)
  VALUES ('delete', old.rowid, old.content_json);
  INSERT INTO events_fts(rowid, content_json)
  VALUES (new.rowid, new.content_json);
END;
