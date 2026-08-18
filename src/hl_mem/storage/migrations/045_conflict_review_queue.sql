ALTER TABLE conflict_cases ADD COLUMN namespace_key TEXT;
ALTER TABLE conflict_cases ADD COLUMN group_key TEXT;
ALTER TABLE conflict_cases ADD COLUMN generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1);
ALTER TABLE conflict_cases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0);
ALTER TABLE conflict_cases ADD COLUMN overflow INTEGER NOT NULL DEFAULT 0 CHECK (overflow IN (0, 1));

CREATE TABLE conflict_case_candidates (
    case_id TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    canonical_value_json TEXT NOT NULL,
    representative_claim_id TEXT NOT NULL,
    support_count INTEGER NOT NULL DEFAULT 1 CHECK (support_count >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (case_id, candidate_key),
    FOREIGN KEY (case_id) REFERENCES conflict_cases(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_claim_id) REFERENCES claims(id)
);

CREATE TABLE conflict_candidate_members (
    case_id TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    attached_at TEXT NOT NULL,
    PRIMARY KEY (case_id, claim_id),
    FOREIGN KEY (case_id, candidate_key)
        REFERENCES conflict_case_candidates(case_id, candidate_key) ON DELETE CASCADE,
    FOREIGN KEY (claim_id) REFERENCES claims(id)
);

CREATE TABLE conflict_review_state (
    case_id TEXT PRIMARY KEY,
    dirty_at TEXT,
    dirty_reason TEXT,
    not_before TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    last_reviewed_at TEXT,
    input_fingerprint TEXT,
    left_tip_id TEXT,
    right_tip_id TEXT,
    FOREIGN KEY (case_id) REFERENCES conflict_cases(id) ON DELETE CASCADE
);

CREATE TABLE maintenance_cursors (
    task TEXT PRIMARY KEY,
    cursor_time TEXT,
    cursor_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_conflict_cases_left_open
ON conflict_cases(left_claim_id, status, resolved_at);

CREATE INDEX idx_conflict_cases_right_open
ON conflict_cases(right_claim_id, status, resolved_at);

CREATE INDEX idx_conflict_candidate_representative
ON conflict_case_candidates(representative_claim_id);

CREATE INDEX idx_conflict_candidate_member_claim
ON conflict_candidate_members(claim_id);

CREATE INDEX idx_conflict_review_dirty
ON conflict_review_state(dirty_at, not_before, case_id)
WHERE dirty_at IS NOT NULL;

CREATE INDEX idx_conflict_review_left_tip
ON conflict_review_state(left_tip_id);

CREATE INDEX idx_conflict_review_right_tip
ON conflict_review_state(right_tip_id);

-- Backfill a group identity only for the canonical representative of an
-- already-open mutually-exclusive group. Legacy duplicate pair cases remain
-- ungrouped until the bounded maintenance path converges them.
UPDATE conflict_cases AS cases
SET namespace_key = (
        SELECT left_claim.namespace_key
        FROM claims AS left_claim
        WHERE left_claim.id = cases.left_claim_id
    ),
    group_key = (
        SELECT left_claim.conflict_key
        FROM claims AS left_claim
        WHERE left_claim.id = cases.left_claim_id
    )
WHERE cases.status IN ('pending', 'auto_resolved', 'manual_required')
  AND cases.resolved_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM claims AS left_claim
      JOIN claims AS right_claim ON right_claim.id = cases.right_claim_id
      WHERE left_claim.id = cases.left_claim_id
        AND left_claim.namespace_key = right_claim.namespace_key
        AND left_claim.conflict_key IS NOT NULL
        AND left_claim.conflict_key = right_claim.conflict_key
        AND left_claim.canonical_slot = right_claim.canonical_slot
        AND left_claim.canonical_slot IN (
            'choice.model',
            'config.model',
            'config.port',
            'preference.response_style',
            'preference.ui_theme',
            'state.service_health'
        )
  )
  AND cases.id = (
      SELECT MIN(peer.id)
      FROM conflict_cases AS peer
      JOIN claims AS peer_left ON peer_left.id = peer.left_claim_id
      JOIN claims AS current_left ON current_left.id = cases.left_claim_id
      WHERE peer.status IN ('pending', 'auto_resolved', 'manual_required')
        AND peer.resolved_at IS NULL
        AND peer_left.namespace_key = current_left.namespace_key
        AND peer_left.conflict_key = current_left.conflict_key
  );

CREATE UNIQUE INDEX idx_conflict_open_group_unique
ON conflict_cases(namespace_key, group_key)
WHERE namespace_key IS NOT NULL
  AND group_key IS NOT NULL
  AND status IN ('pending', 'auto_resolved', 'manual_required')
  AND resolved_at IS NULL;

INSERT INTO conflict_case_candidates(
    case_id,
    candidate_key,
    canonical_value_json,
    representative_claim_id,
    support_count,
    first_seen_at,
    last_seen_at
)
SELECT
    cases.id,
    member.value_json,
    member.value_json,
    MIN(member.id),
    COUNT(*),
    MIN(member.recorded_from),
    MAX(member.recorded_from)
FROM conflict_cases AS cases
JOIN claims AS member
  ON member.namespace_key = cases.namespace_key
 AND member.conflict_key = cases.group_key
WHERE cases.group_key IS NOT NULL
  AND cases.status IN ('pending', 'auto_resolved', 'manual_required')
  AND cases.resolved_at IS NULL
  AND member.status IN ('active', 'candidate', 'disputed')
  AND member.value_json IS NOT NULL
GROUP BY cases.id, member.value_json;

INSERT INTO conflict_candidate_members(case_id, candidate_key, claim_id, attached_at)
SELECT
    cases.id,
    member.value_json,
    member.id,
    member.recorded_from
FROM conflict_cases AS cases
JOIN claims AS member
  ON member.namespace_key = cases.namespace_key
 AND member.conflict_key = cases.group_key
WHERE cases.group_key IS NOT NULL
  AND cases.status IN ('pending', 'auto_resolved', 'manual_required')
  AND cases.resolved_at IS NULL
  AND member.status IN ('active', 'candidate', 'disputed')
  AND member.value_json IS NOT NULL;

INSERT INTO conflict_review_state(case_id, dirty_at, dirty_reason)
SELECT
    id,
    CASE
        WHEN status IN ('pending', 'auto_resolved') THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ELSE NULL
    END,
    CASE
        WHEN status IN ('pending', 'auto_resolved') THEN 'migration_open_case'
        ELSE 'migration_manual_clean'
    END
FROM conflict_cases
WHERE status IN ('pending', 'auto_resolved', 'manual_required')
  AND resolved_at IS NULL;

CREATE TRIGGER conflict_case_review_insert
AFTER INSERT ON conflict_cases
WHEN NEW.status IN ('pending', 'auto_resolved', 'manual_required')
  AND NEW.resolved_at IS NULL
BEGIN
  INSERT INTO conflict_review_state(case_id, dirty_at, dirty_reason)
  VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'case_opened')
  ON CONFLICT(case_id) DO UPDATE SET
    dirty_at = excluded.dirty_at,
    dirty_reason = excluded.dirty_reason,
    not_before = NULL,
    attempt_count = 0,
    last_error = NULL;
END;

CREATE TRIGGER conflict_case_review_reopen
AFTER UPDATE OF status, resolved_at ON conflict_cases
WHEN (
    OLD.status IS NOT NEW.status
    OR OLD.resolved_at IS NOT NEW.resolved_at
  )
  AND NEW.status IN ('pending', 'auto_resolved', 'manual_required')
  AND NEW.resolved_at IS NULL
BEGIN
  INSERT INTO conflict_review_state(case_id, dirty_at, dirty_reason)
  VALUES (NEW.id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'case_reopened')
  ON CONFLICT(case_id) DO UPDATE SET
    dirty_at = excluded.dirty_at,
    dirty_reason = excluded.dirty_reason,
    not_before = NULL,
    attempt_count = 0,
    last_error = NULL;
END;

CREATE TRIGGER conflict_case_review_terminal
AFTER UPDATE OF status, resolved_at ON conflict_cases
WHEN NEW.status IN ('resolved', 'rejected') OR NEW.resolved_at IS NOT NULL
BEGIN
  DELETE FROM conflict_review_state WHERE case_id = NEW.id;
END;

CREATE TRIGGER conflict_candidate_review_insert
AFTER INSERT ON conflict_case_candidates
WHEN EXISTS (
    SELECT 1
    FROM conflict_cases AS cases
    WHERE cases.id = NEW.case_id
      AND cases.status IN ('pending', 'auto_resolved', 'manual_required')
      AND cases.resolved_at IS NULL
)
BEGIN
  UPDATE conflict_cases
  SET revision = revision + 1
  WHERE id = NEW.case_id;
  UPDATE conflict_review_state
  SET dirty_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
      dirty_reason = 'candidate_set_changed',
      not_before = NULL,
      attempt_count = 0,
      last_error = NULL
  WHERE case_id = NEW.case_id;
END;

CREATE TRIGGER conflict_candidate_review_update
AFTER UPDATE OF candidate_key, canonical_value_json, representative_claim_id ON conflict_case_candidates
WHEN OLD.candidate_key IS NOT NEW.candidate_key
  OR OLD.canonical_value_json IS NOT NEW.canonical_value_json
  OR OLD.representative_claim_id IS NOT NEW.representative_claim_id
BEGIN
  UPDATE conflict_cases
  SET revision = revision + 1
  WHERE id = NEW.case_id
    AND status IN ('pending', 'auto_resolved', 'manual_required')
    AND resolved_at IS NULL;
  UPDATE conflict_review_state
  SET dirty_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
      dirty_reason = 'candidate_set_changed',
      not_before = NULL,
      attempt_count = 0,
      last_error = NULL
  WHERE case_id = NEW.case_id;
END;

CREATE TRIGGER conflict_candidate_review_delete
AFTER DELETE ON conflict_case_candidates
WHEN EXISTS (
    SELECT 1
    FROM conflict_cases AS cases
    WHERE cases.id = OLD.case_id
      AND cases.status IN ('pending', 'auto_resolved', 'manual_required')
      AND cases.resolved_at IS NULL
)
BEGIN
  UPDATE conflict_cases
  SET revision = revision + 1
  WHERE id = OLD.case_id;
  UPDATE conflict_review_state
  SET dirty_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
      dirty_reason = 'candidate_set_changed',
      not_before = NULL,
      attempt_count = 0,
      last_error = NULL
  WHERE case_id = OLD.case_id;
END;

CREATE TRIGGER conflict_claim_input_changed
AFTER UPDATE OF
    status,
    source_authority,
    superseded_by_id,
    namespace_key,
    conflict_key,
    canonical_slot,
    value_json,
    qualifiers_json,
    valid_from,
    valid_to
ON claims
WHEN OLD.status IS NOT NEW.status
  OR OLD.source_authority IS NOT NEW.source_authority
  OR OLD.superseded_by_id IS NOT NEW.superseded_by_id
  OR OLD.namespace_key IS NOT NEW.namespace_key
  OR OLD.conflict_key IS NOT NEW.conflict_key
  OR OLD.canonical_slot IS NOT NEW.canonical_slot
  OR OLD.value_json IS NOT NEW.value_json
  OR OLD.qualifiers_json IS NOT NEW.qualifiers_json
  OR OLD.valid_from IS NOT NEW.valid_from
  OR OLD.valid_to IS NOT NEW.valid_to
BEGIN
  UPDATE conflict_cases
  SET revision = revision + 1
  WHERE id IN (
      SELECT cases.id
      FROM conflict_cases AS cases
      LEFT JOIN conflict_review_state AS review ON review.case_id = cases.id
      WHERE cases.status IN ('pending', 'auto_resolved', 'manual_required')
        AND cases.resolved_at IS NULL
        AND (
            cases.left_claim_id = NEW.id
            OR cases.right_claim_id = NEW.id
            OR review.left_tip_id = NEW.id
            OR review.right_tip_id = NEW.id
            OR EXISTS (
                SELECT 1
                FROM conflict_candidate_members AS members
                WHERE members.case_id = cases.id
                  AND members.claim_id = NEW.id
            )
        )
  );
  UPDATE conflict_review_state
  SET dirty_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
      dirty_reason = 'claim_input_changed',
      not_before = NULL,
      attempt_count = 0,
      last_error = NULL
  WHERE case_id IN (
      SELECT cases.id
      FROM conflict_cases AS cases
      WHERE cases.status IN ('pending', 'auto_resolved', 'manual_required')
        AND cases.resolved_at IS NULL
        AND (
            cases.left_claim_id = NEW.id
            OR cases.right_claim_id = NEW.id
            OR conflict_review_state.left_tip_id = NEW.id
            OR conflict_review_state.right_tip_id = NEW.id
            OR EXISTS (
                SELECT 1
                FROM conflict_candidate_members AS members
                WHERE members.case_id = cases.id
                  AND members.claim_id = NEW.id
            )
        )
  );
END;
