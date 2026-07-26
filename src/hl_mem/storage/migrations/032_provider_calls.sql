CREATE TABLE provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_type TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    query_id TEXT,
    error_class TEXT,
    http_status INTEGER,
    provider_code TEXT,
    fallback INTEGER NOT NULL DEFAULT 0,
    recorded_at REAL NOT NULL
);

CREATE INDEX idx_provider_calls_recorded_at ON provider_calls(recorded_at);
CREATE INDEX idx_provider_calls_query_id ON provider_calls(query_id);
