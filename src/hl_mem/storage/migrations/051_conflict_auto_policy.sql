ALTER TABLE conflict_cases ADD COLUMN policy_version TEXT;
ALTER TABLE conflict_cases ADD COLUMN last_tier TEXT;
ALTER TABLE conflict_cases ADD COLUMN last_decision_hash TEXT;
ALTER TABLE conflict_cases ADD COLUMN resolution_rule TEXT;
ALTER TABLE conflict_cases ADD COLUMN resolver_model TEXT;

ALTER TABLE conflict_review_state ADD COLUMN policy_version TEXT;

UPDATE conflict_review_state
SET dirty_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    dirty_reason = 'v030_policy_upgrade',
    not_before = NULL,
    attempt_count = 0,
    last_error = NULL,
    policy_version = 'conflict-auto-v1'
WHERE COALESCE(policy_version, '') <> 'conflict-auto-v1'
  AND case_id IN (
      SELECT id
      FROM conflict_cases
      WHERE status IN ('pending', 'auto_resolved', 'manual_required')
        AND resolved_at IS NULL
  );

CREATE INDEX idx_conflict_review_policy
ON conflict_review_state(policy_version, dirty_at, case_id);
