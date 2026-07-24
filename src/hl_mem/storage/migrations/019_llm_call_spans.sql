CREATE TABLE IF NOT EXISTS llm_call_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id TEXT NOT NULL UNIQUE,
    parent_span_id TEXT,
    trace_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    structured_mode TEXT,
    attempt INTEGER DEFAULT 1,
    status TEXT NOT NULL,
    error_class TEXT,
    raw_request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    total_tokens INTEGER,
    latency_ms REAL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_spans_operation ON llm_call_spans(operation);
CREATE INDEX IF NOT EXISTS idx_llm_spans_started ON llm_call_spans(started_at);
CREATE INDEX IF NOT EXISTS idx_llm_spans_trace ON llm_call_spans(trace_id);
