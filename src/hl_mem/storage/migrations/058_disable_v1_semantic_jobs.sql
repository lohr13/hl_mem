UPDATE jobs
SET status = 'dead',
    leased_until = NULL,
    lease_token = NULL,
    last_error = 'disabled_by_v1_migration',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'pending'
  AND job_type IN (
      'consolidate_conflicts',
      'deduplicate_claims',
      'discover_relations',
      'induce_policies',
      'reclassify_claims'
  );

UPDATE deferred_tasks
SET status = 'abandoned',
    last_error = 'disabled_by_v1_migration',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'pending'
  AND task_type = 'resurrect_recalled_claim';
