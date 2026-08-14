CREATE TRIGGER IF NOT EXISTS claims_active_exclusive_guard_insert
BEFORE INSERT ON claims
WHEN NEW.status = 'active'
  AND NEW.conflict_key IS NOT NULL
  AND NEW.canonical_slot IN (
    'choice.model',
    'config.model',
    'config.port',
    'preference.response_style',
    'preference.ui_theme',
    'state.service_health'
  )
  AND EXISTS (
    SELECT 1
    FROM claims AS existing
    WHERE existing.namespace_key = NEW.namespace_key
      AND existing.conflict_key = NEW.conflict_key
      AND existing.status = 'active'
  )
BEGIN
  SELECT RAISE(ABORT, 'exclusive conflict group already has an active claim');
END;

CREATE TRIGGER IF NOT EXISTS claims_active_exclusive_guard_update
BEFORE UPDATE OF status, namespace_key, conflict_key, canonical_slot ON claims
WHEN (
    OLD.status IS NOT NEW.status
    OR OLD.namespace_key IS NOT NEW.namespace_key
    OR OLD.conflict_key IS NOT NEW.conflict_key
    OR OLD.canonical_slot IS NOT NEW.canonical_slot
  )
  AND NEW.status = 'active'
  AND NEW.conflict_key IS NOT NULL
  AND NEW.canonical_slot IN (
    'choice.model',
    'config.model',
    'config.port',
    'preference.response_style',
    'preference.ui_theme',
    'state.service_health'
  )
  AND EXISTS (
    SELECT 1
    FROM claims AS existing
    WHERE existing.namespace_key = NEW.namespace_key
      AND existing.conflict_key = NEW.conflict_key
      AND existing.status = 'active'
      AND existing.rowid <> OLD.rowid
  )
BEGIN
  SELECT RAISE(ABORT, 'exclusive conflict group already has an active claim');
END;
