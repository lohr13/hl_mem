DROP TRIGGER IF EXISTS claims_mutation_audit_update;
DROP TRIGGER IF EXISTS claims_mutation_audit_delete;

CREATE TABLE IF NOT EXISTS claim_mutation_audit_context (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    trace_id TEXT,
    tenant_id TEXT,
    event_id TEXT,
    related_claim_id TEXT,
    query_id TEXT,
    job_id TEXT,
    claim_mutation_source TEXT
);

CREATE TRIGGER claims_mutation_audit_update
AFTER UPDATE ON claims
BEGIN
    INSERT INTO audit_log(
        occurred_at,
        phase,
        action,
        outcome,
        trace_id,
        tenant_id,
        event_id,
        claim_id,
        related_claim_id,
        query_id,
        job_id,
        detail_json
    ) VALUES (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'claim_mutation',
        'updated',
        'applied',
        COALESCE(
            (SELECT trace_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT event_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT query_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT job_id FROM claim_mutation_audit_context WHERE singleton=1),
            lower(hex(randomblob(16)))
        ),
        COALESCE(
            (SELECT tenant_id FROM claim_mutation_audit_context WHERE singleton=1),
            NEW.namespace_key
        ),
        (SELECT event_id FROM claim_mutation_audit_context WHERE singleton=1),
        OLD.id,
        (SELECT related_claim_id FROM claim_mutation_audit_context WHERE singleton=1),
        (SELECT query_id FROM claim_mutation_audit_context WHERE singleton=1),
        (SELECT job_id FROM claim_mutation_audit_context WHERE singleton=1),
        json_object(
            'schema_version', 'claim_mutation_audit_v1',
            'operation', 'update',
            'source', COALESCE(
                (SELECT claim_mutation_source FROM claim_mutation_audit_context WHERE singleton=1),
                'database'
            ),
            'changed_fields', (
                SELECT json_group_array(value)
                FROM json_each(json_array(
                    CASE WHEN OLD.id IS NOT NEW.id THEN 'id' END,
                    CASE WHEN OLD.namespace_key IS NOT NEW.namespace_key THEN 'namespace_key' END,
                    CASE WHEN OLD.subject_entity_id IS NOT NEW.subject_entity_id THEN 'subject_entity_id' END,
                    CASE WHEN OLD.predicate IS NOT NEW.predicate THEN 'predicate' END,
                    CASE WHEN OLD.value_json IS NOT NEW.value_json THEN 'value_json' END,
                    CASE WHEN OLD.qualifiers_json IS NOT NEW.qualifiers_json THEN 'qualifiers_json' END,
                    CASE WHEN OLD.conflict_key IS NOT NEW.conflict_key THEN 'conflict_key' END,
                    CASE WHEN OLD.valid_from IS NOT NEW.valid_from THEN 'valid_from' END,
                    CASE WHEN OLD.valid_to IS NOT NEW.valid_to THEN 'valid_to' END,
                    CASE WHEN OLD.recorded_from IS NOT NEW.recorded_from THEN 'recorded_from' END,
                    CASE WHEN OLD.recorded_to IS NOT NEW.recorded_to THEN 'recorded_to' END,
                    CASE WHEN OLD.observed_at IS NOT NEW.observed_at THEN 'observed_at' END,
                    CASE WHEN OLD.expires_at IS NOT NEW.expires_at THEN 'expires_at' END,
                    CASE WHEN OLD.refresh_after IS NOT NEW.refresh_after THEN 'refresh_after' END,
                    CASE WHEN OLD.volatility IS NOT NEW.volatility THEN 'volatility' END,
                    CASE WHEN OLD.status IS NOT NEW.status THEN 'status' END,
                    CASE WHEN OLD.confidence IS NOT NEW.confidence THEN 'confidence' END,
                    CASE WHEN OLD.importance IS NOT NEW.importance THEN 'importance' END,
                    CASE WHEN OLD.source_authority IS NOT NEW.source_authority THEN 'source_authority' END,
                    CASE WHEN OLD.supersedes_id IS NOT NEW.supersedes_id THEN 'supersedes_id' END,
                    CASE WHEN OLD.extractor_version IS NOT NEW.extractor_version THEN 'extractor_version' END,
                    CASE WHEN OLD.embedding_dense IS NOT NEW.embedding_dense THEN 'embedding_dense' END,
                    CASE WHEN OLD.embedding_sparse IS NOT NEW.embedding_sparse THEN 'embedding_sparse' END,
                    CASE WHEN OLD.embedding_model IS NOT NEW.embedding_model THEN 'embedding_model' END,
                    CASE WHEN OLD.embedding_dim IS NOT NEW.embedding_dim THEN 'embedding_dim' END,
                    CASE WHEN OLD.fact_hash IS NOT NEW.fact_hash THEN 'fact_hash' END,
                    CASE WHEN OLD.scope IS NOT NEW.scope THEN 'scope' END,
                    CASE WHEN OLD.access_count IS NOT NEW.access_count THEN 'access_count' END,
                    CASE WHEN OLD.last_accessed_at IS NOT NEW.last_accessed_at THEN 'last_accessed_at' END,
                    CASE WHEN OLD.last_decayed_at IS NOT NEW.last_decayed_at THEN 'last_decayed_at' END,
                    CASE WHEN OLD.canonical_attribute IS NOT NEW.canonical_attribute THEN 'canonical_attribute' END,
                    CASE WHEN OLD.conflict_key_version IS NOT NEW.conflict_key_version THEN 'conflict_key_version' END,
                    CASE WHEN OLD.legacy_conflict_key IS NOT NEW.legacy_conflict_key THEN 'legacy_conflict_key' END,
                    CASE WHEN OLD.superseded_by_id IS NOT NEW.superseded_by_id THEN 'superseded_by_id' END,
                    CASE WHEN OLD.canonical_slot IS NOT NEW.canonical_slot THEN 'canonical_slot' END,
                    CASE WHEN OLD.topic_tags_json IS NOT NEW.topic_tags_json THEN 'topic_tags_json' END,
                    CASE WHEN OLD.occurred_start IS NOT NEW.occurred_start THEN 'occurred_start' END,
                    CASE WHEN OLD.occurred_end IS NOT NEW.occurred_end THEN 'occurred_end' END,
                    CASE WHEN OLD.entities_json IS NOT NEW.entities_json THEN 'entities_json' END,
                    CASE WHEN OLD.index_text IS NOT NEW.index_text THEN 'index_text' END,
                    CASE WHEN OLD.activation_base IS NOT NEW.activation_base THEN 'activation_base' END,
                    CASE WHEN OLD.activation IS NOT NEW.activation THEN 'activation' END,
                    CASE WHEN OLD.decay_below_since IS NOT NEW.decay_below_since THEN 'decay_below_since' END,
                    CASE WHEN OLD.assertion_kind IS NOT NEW.assertion_kind THEN 'assertion_kind' END,
                    CASE
                        WHEN OLD.subject_canonical_entity_id IS NOT NEW.subject_canonical_entity_id
                        THEN 'subject_canonical_entity_id'
                    END,
                    CASE
                        WHEN OLD.canonical_target_entity_id IS NOT NEW.canonical_target_entity_id
                        THEN 'canonical_target_entity_id'
                    END
                ))
                WHERE value IS NOT NULL
            ),
            'old_status', OLD.status,
            'new_status', NEW.status,
            'old_canonical_slot', OLD.canonical_slot,
            'new_canonical_slot', NEW.canonical_slot,
            'old_importance', OLD.importance,
            'new_importance', NEW.importance
        )
    );
    DELETE FROM claim_mutation_audit_context WHERE singleton=1;
END;

CREATE TRIGGER claims_mutation_audit_delete
AFTER DELETE ON claims
BEGIN
    INSERT INTO audit_log(
        occurred_at,
        phase,
        action,
        outcome,
        trace_id,
        tenant_id,
        event_id,
        claim_id,
        related_claim_id,
        query_id,
        job_id,
        detail_json
    ) VALUES (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'claim_mutation',
        'deleted',
        'applied',
        COALESCE(
            (SELECT trace_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT event_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT query_id FROM claim_mutation_audit_context WHERE singleton=1),
            (SELECT job_id FROM claim_mutation_audit_context WHERE singleton=1),
            lower(hex(randomblob(16)))
        ),
        COALESCE(
            (SELECT tenant_id FROM claim_mutation_audit_context WHERE singleton=1),
            OLD.namespace_key
        ),
        (SELECT event_id FROM claim_mutation_audit_context WHERE singleton=1),
        OLD.id,
        (SELECT related_claim_id FROM claim_mutation_audit_context WHERE singleton=1),
        (SELECT query_id FROM claim_mutation_audit_context WHERE singleton=1),
        (SELECT job_id FROM claim_mutation_audit_context WHERE singleton=1),
        json_object(
            'schema_version', 'claim_mutation_audit_v1',
            'operation', 'delete',
            'source', COALESCE(
                (SELECT claim_mutation_source FROM claim_mutation_audit_context WHERE singleton=1),
                'database'
            ),
            'old_status', OLD.status,
            'old_canonical_slot', OLD.canonical_slot,
            'old_importance', OLD.importance
        )
    );
    DELETE FROM claim_mutation_audit_context WHERE singleton=1;
END;
