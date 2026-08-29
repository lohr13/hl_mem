INSERT INTO conflict_review_state (
    case_id,
    dirty_at,
    dirty_reason,
    not_before,
    attempt_count,
    last_error,
    policy_version
)
SELECT DISTINCT
    conflict_cases.id,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'conflict_l2_retired',
    NULL,
    0,
    NULL,
    'conflict-auto-v1'
FROM jobs
JOIN conflict_cases
    ON conflict_cases.id = CASE
        WHEN json_valid(jobs.payload_json)
        THEN json_extract(jobs.payload_json, '$.case_id')
        ELSE NULL
    END
WHERE jobs.job_type = 'resolve_conflict_llm'
  AND jobs.status IN ('pending', 'running', 'failed')
  AND conflict_cases.status IN ('pending', 'auto_resolved', 'manual_required')
  AND conflict_cases.resolved_at IS NULL
ON CONFLICT(case_id) DO UPDATE SET
    dirty_at = excluded.dirty_at,
    dirty_reason = excluded.dirty_reason,
    not_before = NULL,
    attempt_count = 0,
    last_error = NULL;

UPDATE jobs
SET status = 'dead',
    leased_until = NULL,
    lease_token = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE job_type = 'resolve_conflict_llm'
  AND status IN ('pending', 'running', 'failed');
