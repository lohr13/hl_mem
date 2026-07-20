CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,           -- ULID
    idempotency_key TEXT UNIQUE,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT,
    project_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,      -- message|tool_call|tool_result|feedback|task_end|explicit_memory
    actor_type TEXT NOT NULL,      -- user|assistant|tool|system
    actor_id TEXT,
    content_json TEXT NOT NULL,    -- JSON string
    occurred_at TEXT NOT NULL,     -- ISO8601
    recorded_at TEXT NOT NULL,     -- ISO8601
    source_uri TEXT,
    content_hash TEXT,             -- SHA256 of content_json
    sensitivity TEXT DEFAULT 'normal'
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    subject_entity_id TEXT,
    predicate TEXT,
    value_json TEXT,
    qualifiers_json TEXT,
    conflict_key TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_from TEXT NOT NULL,
    recorded_to TEXT,
    observed_at TEXT,
    expires_at TEXT,
    refresh_after TEXT,
    volatility TEXT NOT NULL DEFAULT 'stable',  -- ephemeral|stable (首版只有这两档)
    status TEXT NOT NULL DEFAULT 'candidate',    -- candidate|active|superseded|disputed|retracted|expired|archived
    confidence REAL DEFAULT 0.5,
    importance REAL DEFAULT 0.5,
    source_authority TEXT DEFAULT 'medium',
    supersedes_id TEXT,
    extractor_version TEXT,
    -- 预留 embedding 列（首版用 BLOB）
    embedding_dense BLOB,          -- float32 array, 2048d
    embedding_sparse BLOB,         -- index→weight map
    embedding_model TEXT,
    embedding_dim INTEGER,
    FOREIGN KEY (supersedes_id) REFERENCES claims(id)
);
CREATE INDEX IF NOT EXISTS idx_claims_conflict_key ON claims(conflict_key) WHERE conflict_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_namespace ON claims(namespace_key);

CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    derived_type TEXT NOT NULL,    -- claim|observation
    derived_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,   -- event|claim
    evidence_id TEXT NOT NULL,
    relation TEXT NOT NULL,        -- supports|contradicts|derived_from|supersedes
    weight REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_ev_derived ON evidence_links(derived_type, derived_id);
CREATE INDEX IF NOT EXISTS idx_ev_evidence ON evidence_links(evidence_type, evidence_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed|dead
    run_after TEXT,
    leased_until TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);

CREATE TABLE IF NOT EXISTS derivations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'observation',  -- observation (首版只有这一种)
    name TEXT,
    query TEXT,
    body TEXT NOT NULL,
    scope_json TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active|stale|rebuilding|archived
    confidence REAL DEFAULT 0.5,
    generated_by_model TEXT,
    prompt_version TEXT,
    source_watermark TEXT,
    refresh_policy TEXT,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content_text,
    content_json,
    content='events',
    content_rowid='rowid'
);
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    search_text,
    content='claims',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, content_text, content_json)
  VALUES (new.rowid, json_extract(new.content_json, '$.text'), new.content_json);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content_text, content_json)
  VALUES ('delete', old.rowid, json_extract(old.content_json, '$.text'), old.content_json);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, content_text, content_json)
  VALUES ('delete', old.rowid, json_extract(old.content_json, '$.text'), old.content_json);
  INSERT INTO events_fts(rowid, content_text, content_json)
  VALUES (new.rowid, json_extract(new.content_json, '$.text'), new.content_json);
END;
CREATE TRIGGER IF NOT EXISTS claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
CREATE TRIGGER IF NOT EXISTS claims_ad AFTER DELETE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
END;
CREATE TRIGGER IF NOT EXISTS claims_au AFTER UPDATE ON claims BEGIN
  INSERT INTO claims_fts(claims_fts, rowid, search_text)
  VALUES ('delete', old.rowid, coalesce(old.predicate, '') || ' ' || coalesce(old.value_json, ''));
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, coalesce(new.predicate, '') || ' ' || coalesce(new.value_json, ''));
END;
