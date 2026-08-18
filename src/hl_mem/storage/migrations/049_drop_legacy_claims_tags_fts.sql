-- Irreversible v0.29.1 cleanup. The fleet-wide >=v0.29.0 runtime floor is an
-- operator deployment precondition; 047+048 are the database-local evidence.
CREATE TEMP TABLE _hl_mem_049_guard(gate TEXT NOT NULL);

CREATE TEMP TRIGGER _hl_mem_049_runtime_floor_guard
BEFORE INSERT ON _hl_mem_049_guard
WHEN new.gate = 'runtime_floor'
 AND (SELECT count(*) FROM schema_migrations
      WHERE version IN ('047_claim_assertion_kind','048_dedup_pair_injection_signals')) != 2
BEGIN
  SELECT RAISE(ABORT, 'migration 049 runtime floor evidence is missing');
END;

CREATE TEMP TRIGGER _hl_mem_049_consumer_guard
BEFORE INSERT ON _hl_mem_049_guard
WHEN new.gate = 'database_consumers'
 AND EXISTS (
   SELECT 1
   FROM sqlite_schema
   WHERE type IN ('view','trigger')
     AND name NOT IN ('claims_tags_ai','claims_tags_ad','claims_tags_au')
     AND instr(
       replace(lower(COALESCE(sql,'')), 'claims_tags_fts_v2', ''),
       'claims_tags_fts'
     ) > 0
 )
BEGIN
  SELECT RAISE(ABORT, 'migration 049 database consumer still references claims_tags_fts');
END;

INSERT INTO _hl_mem_049_guard(gate) VALUES ('runtime_floor');
INSERT INTO _hl_mem_049_guard(gate) VALUES ('database_consumers');

DROP TRIGGER _hl_mem_049_runtime_floor_guard;
DROP TRIGGER _hl_mem_049_consumer_guard;
DROP TABLE _hl_mem_049_guard;

DROP TRIGGER IF EXISTS claims_tags_ai;
DROP TRIGGER IF EXISTS claims_tags_ad;
DROP TRIGGER IF EXISTS claims_tags_au;
DROP TABLE IF EXISTS claims_tags_fts;
