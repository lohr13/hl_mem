ALTER TABLE claims ADD COLUMN activation_base REAL NOT NULL DEFAULT 1.0
    CHECK (activation_base >= 0.0 AND activation_base <= 1.0);
ALTER TABLE claims ADD COLUMN activation REAL NOT NULL DEFAULT 1.0
    CHECK (activation >= 0.0 AND activation <= 1.0);
ALTER TABLE claims ADD COLUMN decay_below_since TEXT;

CREATE INDEX IF NOT EXISTS idx_claims_activation_decay
    ON claims(status, activation, decay_below_since)
    WHERE status IN ('active', 'disputed');
