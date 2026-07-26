-- v0.10 historical data fixture, loaded after migration 018.
INSERT INTO events(
    id,idempotency_key,event_type,actor_type,content_json,occurred_at,recorded_at
) VALUES
    ('event-018-1','fixture-event-1','message','user','{"text":"历史迁移保留测试"}',
     '2025-01-01T00:00:00Z','2025-01-01T00:00:01Z'),
    ('event-018-2','fixture-event-2','tool_result','tool','{"text":"vector snapshot"}',
     '2025-01-02T00:00:00Z','2025-01-02T00:00:01Z');

INSERT INTO claims(
    id,subject_entity_id,predicate,value_json,qualifiers_json,valid_from,valid_to,
    recorded_from,recorded_to,status,embedding_dense,embedding_model,embedding_dim,
    fact_hash,scope,canonical_slot,topic_tags_json
) VALUES
    ('claim-018-1','project','description','"历史迁移保留测试"','{}',
     '2025-01-01T00:00:00Z',NULL,'2025-01-01T00:00:01Z',NULL,'active',
     X'0000803F0000004000004040','fixture-3d',3,'fixture-hash-1','permanent','description','["project"]'),
    ('claim-018-2','agent','state','"ready"','{}',
     '2025-01-02T00:00:00Z','2025-12-31T23:59:59Z','2025-01-02T00:00:01Z',NULL,'active',
     X'0000803E0000003F0000403F','fixture-3d',3,'fixture-hash-2','temporal','state','["agent"]');

INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) VALUES
    ('link-018-1','claim','claim-018-1','event','event-018-1','derived_from',1.0),
    ('link-018-2','claim','claim-018-2','event','event-018-2','derived_from',1.0);

INSERT INTO episodes(id,goal,status,started_at,ended_at,reward,outcome_summary,scope_json) VALUES
    ('episode-018-1','validate historical upgrade','success','2025-01-03T00:00:00Z',
     '2025-01-03T00:01:00Z',1.0,'completed','{}');

INSERT INTO traces(id,episode_id,sequence_no,action,observation,value,priority) VALUES
    ('trace-018-1','episode-018-1',1,'open database','snapshot readable',1.0,0.5),
    ('trace-018-2','episode-018-1',2,'run migrations','upgrade complete',1.0,0.5);
