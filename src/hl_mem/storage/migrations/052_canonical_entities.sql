CREATE TABLE canonical_entities (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL CHECK (length(trim(namespace_key)) > 0),
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('person','agent','device','environment','instrument','project','topic')
    ),
    canonical_key TEXT NOT NULL CHECK (
        length(canonical_key) > 0
        AND canonical_key = trim(canonical_key)
        AND instr(canonical_key, ' ') = 0
    ),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    status TEXT NOT NULL CHECK (status IN ('active','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (namespace_key, entity_type, canonical_key),
    CHECK (id = entity_type || ':' || canonical_key)
);

CREATE TRIGGER canonical_entities_coordinates_immutable
BEFORE UPDATE OF id, namespace_key, entity_type, canonical_key ON canonical_entities
WHEN OLD.id IS NOT NEW.id
  OR OLD.namespace_key IS NOT NEW.namespace_key
  OR OLD.entity_type IS NOT NEW.entity_type
  OR OLD.canonical_key IS NOT NEW.canonical_key
BEGIN
    SELECT RAISE(ABORT, 'canonical entity coordinates are immutable');
END;

CREATE TABLE entity_aliases (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL CHECK (length(trim(namespace_key)) > 0),
    alias_normalized TEXT NOT NULL CHECK (
        length(alias_normalized) > 0 AND alias_normalized = trim(alias_normalized)
    ),
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('person','agent','device','environment','instrument','project','topic')
    ),
    canonical_entity_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('builtin','config_explicit','user_explicit','migration_exact')
    ),
    source_event_id TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT CHECK (valid_to IS NULL OR valid_to > valid_from),
    created_at TEXT NOT NULL,
    UNIQUE (namespace_key, entity_type, alias_normalized, version),
    FOREIGN KEY (canonical_entity_id) REFERENCES canonical_entities(id),
    FOREIGN KEY (source_event_id) REFERENCES events(id)
);

CREATE UNIQUE INDEX idx_entity_aliases_one_active
ON entity_aliases(namespace_key, entity_type, alias_normalized)
WHERE valid_to IS NULL;

CREATE INDEX idx_entity_aliases_active_lookup
ON entity_aliases(namespace_key, alias_normalized, entity_type, canonical_entity_id, version)
WHERE valid_to IS NULL;

CREATE INDEX idx_entity_aliases_target
ON entity_aliases(canonical_entity_id, valid_to);

CREATE TRIGGER entity_aliases_target_insert
BEFORE INSERT ON entity_aliases
WHEN EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_entity_id
      AND namespace_key = NEW.namespace_key
      AND entity_type = NEW.entity_type
)
BEGIN
    SELECT RAISE(ABORT, 'entity alias target type or namespace mismatch');
END;

CREATE TRIGGER entity_aliases_target_update
BEFORE UPDATE OF namespace_key, entity_type, canonical_entity_id ON entity_aliases
WHEN EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_entity_id
      AND namespace_key = NEW.namespace_key
      AND entity_type = NEW.entity_type
)
BEGIN
    SELECT RAISE(ABORT, 'entity alias target type or namespace mismatch');
END;

CREATE TABLE entity_relations (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL CHECK (length(trim(namespace_key)) > 0),
    from_entity_id TEXT NOT NULL,
    to_entity_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('runs_on','owned_by','operates_in','part_of','about')),
    source_event_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    valid_from TEXT NOT NULL,
    valid_to TEXT CHECK (valid_to IS NULL OR valid_to > valid_from),
    FOREIGN KEY (from_entity_id) REFERENCES canonical_entities(id),
    FOREIGN KEY (to_entity_id) REFERENCES canonical_entities(id),
    FOREIGN KEY (source_event_id) REFERENCES events(id)
);

CREATE INDEX idx_entity_relations_from
ON entity_relations(namespace_key, from_entity_id, relation, valid_to);

CREATE INDEX idx_entity_relations_to
ON entity_relations(namespace_key, to_entity_id, relation, valid_to);

CREATE TRIGGER entity_relations_namespace_insert
BEFORE INSERT ON entity_relations
WHEN (
    EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.from_entity_id)
    AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE id = NEW.from_entity_id AND namespace_key = NEW.namespace_key
    )
) OR (
    EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.to_entity_id)
    AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE id = NEW.to_entity_id AND namespace_key = NEW.namespace_key
    )
)
BEGIN
    SELECT RAISE(ABORT, 'entity relation namespace mismatch');
END;

CREATE TRIGGER entity_relations_namespace_update
BEFORE UPDATE OF namespace_key, from_entity_id, to_entity_id ON entity_relations
WHEN (
    EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.from_entity_id)
    AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE id = NEW.from_entity_id AND namespace_key = NEW.namespace_key
    )
) OR (
    EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.to_entity_id)
    AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE id = NEW.to_entity_id AND namespace_key = NEW.namespace_key
    )
)
BEGIN
    SELECT RAISE(ABORT, 'entity relation namespace mismatch');
END;

CREATE TABLE claim_entity_links (
    claim_id TEXT NOT NULL,
    canonical_entity_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('subject','actor','target','device','environment','project','about')
    ),
    mention_text TEXT NOT NULL CHECK (length(trim(mention_text)) > 0),
    resolution_confidence REAL NOT NULL CHECK (
        resolution_confidence >= 0 AND resolution_confidence <= 1
    ),
    alias_version INTEGER CHECK (alias_version IS NULL OR alias_version >= 1),
    proof_id TEXT,
    PRIMARY KEY (claim_id, canonical_entity_id, role),
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_entity_id) REFERENCES canonical_entities(id),
    FOREIGN KEY (proof_id) REFERENCES evidence_links(id)
);

CREATE INDEX idx_claim_entity_links_entity
ON claim_entity_links(canonical_entity_id, role, claim_id);

CREATE TRIGGER claim_entity_links_type_insert
BEFORE INSERT ON claim_entity_links
WHEN EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_entity_id
      AND (
          (NEW.role = 'subject' AND entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'actor' AND entity_type IN ('person','agent'))
          OR (NEW.role = 'target' AND entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'device' AND entity_type = 'device')
          OR (NEW.role = 'environment' AND entity_type = 'environment')
          OR (NEW.role = 'project' AND entity_type = 'project')
          OR (NEW.role = 'about' AND entity_type = 'topic')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'claim entity role/type mismatch');
END;

CREATE TRIGGER claim_entity_links_namespace_insert
BEFORE INSERT ON claim_entity_links
WHEN EXISTS (SELECT 1 FROM claims WHERE id = NEW.claim_id)
AND EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id)
AND NOT EXISTS (
    SELECT 1
    FROM claims
    JOIN canonical_entities
      ON canonical_entities.namespace_key = claims.namespace_key
    WHERE claims.id = NEW.claim_id
      AND canonical_entities.id = NEW.canonical_entity_id
)
BEGIN
    SELECT RAISE(ABORT, 'claim entity namespace mismatch');
END;

CREATE TRIGGER claim_entity_links_type_update
BEFORE UPDATE OF canonical_entity_id, role ON claim_entity_links
WHEN EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_entity_id
      AND (
          (NEW.role = 'subject' AND entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'actor' AND entity_type IN ('person','agent'))
          OR (NEW.role = 'target' AND entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'device' AND entity_type = 'device')
          OR (NEW.role = 'environment' AND entity_type = 'environment')
          OR (NEW.role = 'project' AND entity_type = 'project')
          OR (NEW.role = 'about' AND entity_type = 'topic')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'claim entity role/type mismatch');
END;

CREATE TRIGGER claim_entity_links_namespace_update
BEFORE UPDATE OF claim_id, canonical_entity_id ON claim_entity_links
WHEN EXISTS (SELECT 1 FROM claims WHERE id = NEW.claim_id)
AND EXISTS (SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_entity_id)
AND NOT EXISTS (
    SELECT 1
    FROM claims
    JOIN canonical_entities
      ON canonical_entities.namespace_key = claims.namespace_key
    WHERE claims.id = NEW.claim_id
      AND canonical_entities.id = NEW.canonical_entity_id
)
BEGIN
    SELECT RAISE(ABORT, 'claim entity namespace mismatch');
END;

ALTER TABLE claims
ADD COLUMN subject_canonical_entity_id TEXT REFERENCES canonical_entities(id);

ALTER TABLE claims
ADD COLUMN canonical_target_entity_id TEXT REFERENCES canonical_entities(id);

CREATE TRIGGER claims_canonical_subject_insert
BEFORE INSERT ON claims
WHEN NEW.subject_canonical_entity_id IS NOT NULL
AND EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.subject_canonical_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.subject_canonical_entity_id
      AND namespace_key = NEW.namespace_key
      AND entity_type IN ('person','agent','device','environment','instrument','project')
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical subject type or namespace mismatch');
END;

CREATE TRIGGER claims_canonical_subject_update
BEFORE UPDATE OF namespace_key, subject_canonical_entity_id ON claims
WHEN NEW.subject_canonical_entity_id IS NOT NULL
AND EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.subject_canonical_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.subject_canonical_entity_id
      AND namespace_key = NEW.namespace_key
      AND entity_type IN ('person','agent','device','environment','instrument','project')
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical subject type or namespace mismatch');
END;

CREATE TRIGGER claims_canonical_target_insert
BEFORE INSERT ON claims
WHEN NEW.canonical_target_entity_id IS NOT NULL
AND EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_target_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_target_entity_id
      AND namespace_key = NEW.namespace_key
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical target namespace mismatch');
END;

CREATE TRIGGER claims_canonical_target_update
BEFORE UPDATE OF namespace_key, canonical_target_entity_id ON claims
WHEN NEW.canonical_target_entity_id IS NOT NULL
AND EXISTS (
    SELECT 1 FROM canonical_entities WHERE id = NEW.canonical_target_entity_id
)
AND NOT EXISTS (
    SELECT 1
    FROM canonical_entities
    WHERE id = NEW.canonical_target_entity_id
      AND namespace_key = NEW.namespace_key
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical target namespace mismatch');
END;

CREATE INDEX idx_claims_subject_canonical_entity
ON claims(namespace_key, subject_canonical_entity_id, status)
WHERE subject_canonical_entity_id IS NOT NULL;

CREATE INDEX idx_claims_canonical_target_entity
ON claims(namespace_key, canonical_target_entity_id, status)
WHERE canonical_target_entity_id IS NOT NULL;
