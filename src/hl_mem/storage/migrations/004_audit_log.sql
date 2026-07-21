CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    phase TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_us INTEGER,
    trace_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    event_id TEXT,
    claim_id TEXT,
    related_claim_id TEXT,
    query_id TEXT,
    job_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    CHECK (duration_us IS NULL OR duration_us >= 0)
);

CREATE INDEX IF NOT EXISTS idx_audit_phase_time ON audit_log(phase, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_trace_time ON audit_log(trace_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_audit_event_time ON audit_log(event_id, occurred_at)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_claim_time ON audit_log(claim_id, occurred_at)
    WHERE claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_related_claim_time
    ON audit_log(related_claim_id, occurred_at) WHERE related_claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_query_time ON audit_log(query_id, occurred_at)
    WHERE query_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_review (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL
        CHECK (target_type IN ('audit','event','claim','observation','query')),
    target_id TEXT NOT NULL,
    question TEXT NOT NULL,
    label TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(target_type, target_id, question, reviewer)
);
CREATE INDEX IF NOT EXISTS idx_audit_review_question ON audit_review(question, label);
