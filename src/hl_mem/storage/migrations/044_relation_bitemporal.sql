ALTER TABLE memory_relations ADD COLUMN valid_from TEXT;
ALTER TABLE memory_relations ADD COLUMN valid_to TEXT;

UPDATE memory_relations
SET valid_from = created_at
WHERE valid_from IS NULL;

CREATE TRIGGER memory_relations_default_valid_from
AFTER INSERT ON memory_relations
WHEN NEW.valid_from IS NULL
BEGIN
  UPDATE memory_relations
  SET valid_from = NEW.created_at
  WHERE id = NEW.id;
END;

CREATE TRIGGER claims_close_terminal_relations
AFTER UPDATE OF status ON claims
WHEN OLD.status IS NOT NEW.status
  AND NEW.status IN ('retracted', 'superseded', 'expired')
BEGIN
  UPDATE memory_relations
  SET valid_to = COALESCE(
    NEW.valid_to,
    NEW.recorded_to,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  )
  WHERE (from_id = NEW.id OR to_id = NEW.id)
    AND valid_to IS NULL;
END;
