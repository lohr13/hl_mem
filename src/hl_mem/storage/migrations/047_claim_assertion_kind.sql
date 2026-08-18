ALTER TABLE claims
ADD COLUMN assertion_kind TEXT NOT NULL DEFAULT 'unknown'
CHECK (assertion_kind IN ('unknown', 'observation', 'inference'));
