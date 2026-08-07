CREATE TABLE IF NOT EXISTS vector_index_state (
    backend TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL,
    build_status TEXT NOT NULL,
    embedding_model TEXT,
    embedding_dim INTEGER NOT NULL,
    extension_version TEXT,
    started_at TEXT,
    ready_at TEXT,
    last_checked_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS claim_vector_dirty (
    claim_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS claim_vector_dirty_ai AFTER INSERT ON claims
WHEN NEW.embedding_dense IS NOT NULL AND EXISTS(
    SELECT 1 FROM vector_index_state WHERE backend='sqlite_vec' AND enabled=1
)
BEGIN
    INSERT INTO claim_vector_dirty(claim_id,reason,queued_at)
    VALUES(NEW.id,'insert',CURRENT_TIMESTAMP)
    ON CONFLICT(claim_id) DO UPDATE SET reason='insert',queued_at=CURRENT_TIMESTAMP;
END;

CREATE TRIGGER IF NOT EXISTS claim_vector_dirty_au AFTER UPDATE OF
    embedding_dense,embedding_model,embedding_dim,namespace_key ON claims
WHEN EXISTS(
    SELECT 1 FROM vector_index_state WHERE backend='sqlite_vec' AND enabled=1
)
AND (
    NEW.embedding_dense IS NOT OLD.embedding_dense
    OR NEW.embedding_model IS NOT OLD.embedding_model
    OR NEW.embedding_dim IS NOT OLD.embedding_dim
    OR NEW.namespace_key IS NOT OLD.namespace_key
)
BEGIN
    INSERT INTO claim_vector_dirty(claim_id,reason,queued_at)
    VALUES(NEW.id,'update',CURRENT_TIMESTAMP)
    ON CONFLICT(claim_id) DO UPDATE SET reason='update',queued_at=CURRENT_TIMESTAMP;
END;

CREATE TRIGGER IF NOT EXISTS claim_vector_dirty_ad AFTER DELETE ON claims
WHEN EXISTS(
    SELECT 1 FROM vector_index_state WHERE backend='sqlite_vec' AND enabled=1
)
BEGIN
    INSERT INTO claim_vector_dirty(claim_id,reason,queued_at)
    VALUES(OLD.id,'delete',CURRENT_TIMESTAMP)
    ON CONFLICT(claim_id) DO UPDATE SET reason='delete',queued_at=CURRENT_TIMESTAMP;
END;
