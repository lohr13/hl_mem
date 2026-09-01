ALTER TABLE events
ADD COLUMN origin_class TEXT NOT NULL DEFAULT 'unknown'
CHECK (origin_class IN ('direct_user', 'agent', 'external', 'external_derived', 'system', 'unknown'));

ALTER TABLE events
ADD COLUMN session_kind TEXT NOT NULL DEFAULT 'unknown'
CHECK (session_kind IN ('interactive', 'cron', 'heartbeat', 'subagent', 'unknown'));
