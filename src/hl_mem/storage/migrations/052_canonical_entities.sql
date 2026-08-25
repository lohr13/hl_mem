CREATE UNIQUE INDEX idx_events_tenant_id_id ON events(tenant_id, id);
CREATE TABLE canonical_entities (
    id TEXT NOT NULL,
    namespace_key TEXT NOT NULL CHECK (length(trim(namespace_key)) > 0),
    entity_type TEXT NOT NULL
        CHECK (entity_type IN ('person','agent','device','environment','instrument','project','topic')),
    canonical_key TEXT NOT NULL CHECK (
        length(canonical_key) > 0
        AND substr(canonical_key, 1, 1) GLOB '[A-Za-z0-9]'
        AND canonical_key NOT GLOB '*[^A-Za-z0-9_.:-]*'
        AND (substr(canonical_key, 1, 2) <> 'e_' OR (
            length(canonical_key) = 22
            AND substr(canonical_key, 3) NOT GLOB '*[^0-9a-f]*'
        ))
    ),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    status TEXT NOT NULL CHECK (status IN ('active','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_key, id),
    UNIQUE (namespace_key, entity_type, canonical_key),
    UNIQUE (namespace_key, entity_type, id),
    CHECK (id = entity_type || ':' || canonical_key)
);
CREATE TRIGGER canonical_entities_coordinates_immutable
BEFORE UPDATE OF id, namespace_key, entity_type, canonical_key ON canonical_entities
BEGIN SELECT RAISE(ABORT, 'canonical entity coordinates are immutable'); END;
CREATE TABLE entity_aliases (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL CHECK (length(trim(namespace_key)) > 0),
    alias_normalized TEXT NOT NULL CHECK (
        length(alias_normalized) > 0
        AND alias_normalized = hl_mem_normalize_alias(alias_normalized)
    ),
    entity_type TEXT NOT NULL
        CHECK (entity_type IN ('person','agent','device','environment','instrument','project','topic')),
    canonical_entity_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('builtin','config_explicit','user_explicit','migration_exact')),
    source_event_id TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT CHECK (valid_to IS NULL OR valid_to > valid_from),
    created_at TEXT NOT NULL,
    UNIQUE (namespace_key, entity_type, alias_normalized, version),
    FOREIGN KEY (namespace_key, entity_type, canonical_entity_id)
        REFERENCES canonical_entities(namespace_key, entity_type, id),
    FOREIGN KEY (namespace_key, source_event_id) REFERENCES events(tenant_id, id)
);
CREATE UNIQUE INDEX idx_entity_aliases_one_active
ON entity_aliases(namespace_key, entity_type, alias_normalized)
WHERE valid_to IS NULL;
CREATE INDEX idx_entity_aliases_active_lookup
ON entity_aliases(namespace_key, alias_normalized, entity_type, canonical_entity_id, version)
WHERE valid_to IS NULL;
CREATE INDEX idx_entity_aliases_target
ON entity_aliases(namespace_key, canonical_entity_id, valid_to);
CREATE INDEX idx_entity_aliases_source_event ON entity_aliases(namespace_key, source_event_id);
CREATE TRIGGER entity_aliases_fields_immutable BEFORE UPDATE OF id, namespace_key, alias_normalized,
entity_type, canonical_entity_id, version, source_kind, source_event_id, valid_from, created_at ON entity_aliases
BEGIN SELECT RAISE(ABORT, 'entity alias history is immutable'); END;
CREATE TRIGGER entity_aliases_close_guard BEFORE UPDATE OF valid_to ON entity_aliases
WHEN OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR NEW.valid_to <= OLD.valid_from
BEGIN SELECT RAISE(ABORT, 'entity alias history is immutable'); END;
CREATE TRIGGER entity_aliases_delete_immutable BEFORE DELETE ON entity_aliases
BEGIN SELECT RAISE(ABORT, 'entity alias history is immutable'); END;
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
    FOREIGN KEY (namespace_key, from_entity_id) REFERENCES canonical_entities(namespace_key, id),
    FOREIGN KEY (namespace_key, to_entity_id) REFERENCES canonical_entities(namespace_key, id),
    FOREIGN KEY (namespace_key, source_event_id) REFERENCES events(tenant_id, id)
);
CREATE INDEX idx_entity_relations_from ON entity_relations(namespace_key, from_entity_id, relation, valid_to);
CREATE INDEX idx_entity_relations_to ON entity_relations(namespace_key, to_entity_id, relation, valid_to);
CREATE INDEX idx_entity_relations_source_event ON entity_relations(namespace_key, source_event_id);
CREATE TRIGGER entity_relations_fields_immutable BEFORE UPDATE OF id, namespace_key, from_entity_id,
to_entity_id, relation, source_event_id, confidence, valid_from ON entity_relations
BEGIN SELECT RAISE(ABORT, 'entity relation history is immutable'); END;
CREATE TRIGGER entity_relations_close_guard BEFORE UPDATE OF valid_to ON entity_relations
WHEN OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR NEW.valid_to <= OLD.valid_from
BEGIN SELECT RAISE(ABORT, 'entity relation history is immutable'); END;
CREATE TRIGGER entity_relations_delete_immutable BEFORE DELETE ON entity_relations
BEGIN SELECT RAISE(ABORT, 'entity relation history is immutable'); END;

CREATE TABLE claim_entity_links (
    claim_id TEXT NOT NULL,
    canonical_entity_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('subject','actor','target','device','environment','project','about')),
    mention_text TEXT NOT NULL CHECK (
        length(mention_text) > 0
        AND mention_text = hl_mem_normalize_alias(mention_text)
    ),
    resolution_confidence REAL NOT NULL CHECK (resolution_confidence >= 0 AND resolution_confidence <= 1),
    alias_version INTEGER NOT NULL CHECK (alias_version >= 1),
    proof_id TEXT NOT NULL,
    PRIMARY KEY (claim_id, canonical_entity_id, role),
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE,
    FOREIGN KEY (proof_id) REFERENCES evidence_links(id) ON DELETE CASCADE
);

CREATE INDEX idx_claim_entity_links_entity ON claim_entity_links(canonical_entity_id, role, claim_id);
CREATE INDEX idx_claim_entity_links_proof ON claim_entity_links(proof_id);

CREATE TRIGGER claim_entity_links_proof_insert
BEFORE INSERT ON claim_entity_links
WHEN NOT EXISTS (
    SELECT 1
    FROM claims AS claim
    JOIN canonical_entities AS entity
      ON entity.namespace_key = claim.namespace_key AND entity.id = NEW.canonical_entity_id
    JOIN entity_aliases AS alias
      ON alias.namespace_key = claim.namespace_key AND alias.entity_type = entity.entity_type
     AND alias.canonical_entity_id = entity.id AND alias.alias_normalized = NEW.mention_text
     AND alias.version = NEW.alias_version
    JOIN evidence_links AS proof
      ON proof.id = NEW.proof_id AND proof.derived_type = 'claim' AND proof.derived_id = NEW.claim_id
    WHERE claim.id = NEW.claim_id
      AND (
          (NEW.role = 'subject' AND entity.entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'actor' AND entity.entity_type IN ('person','agent'))
          OR (NEW.role = 'target' AND entity.entity_type IN ('person','agent','device','environment','instrument','project'))
          OR (NEW.role = 'device' AND entity.entity_type = 'device')
          OR (NEW.role = 'environment' AND entity.entity_type = 'environment')
          OR (NEW.role = 'project' AND entity.entity_type = 'project')
          OR (NEW.role = 'about' AND entity.entity_type = 'topic')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'claim entity alias or evidence proof mismatch');
END;

CREATE TRIGGER claim_entity_links_immutable BEFORE UPDATE ON claim_entity_links
BEGIN SELECT RAISE(ABORT, 'claim entity link history is immutable'); END;
CREATE TRIGGER linked_claim_namespace_immutable BEFORE UPDATE OF namespace_key ON claims
WHEN OLD.namespace_key IS NOT NEW.namespace_key
AND EXISTS (SELECT 1 FROM claim_entity_links WHERE claim_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'linked claim namespace is immutable'); END;
ALTER TABLE claims ADD COLUMN subject_canonical_entity_id TEXT;
ALTER TABLE claims ADD COLUMN canonical_target_entity_id TEXT;
CREATE TRIGGER canonical_entities_claim_refs_delete BEFORE DELETE ON canonical_entities
WHEN EXISTS (
    SELECT 1 FROM claims AS claim JOIN claim_entity_links AS link ON link.claim_id = claim.id
    WHERE claim.namespace_key = OLD.namespace_key AND link.canonical_entity_id = OLD.id
) OR EXISTS (
    SELECT 1 FROM claims WHERE namespace_key = OLD.namespace_key
    AND (subject_canonical_entity_id = OLD.id OR canonical_target_entity_id = OLD.id)
)
BEGIN SELECT RAISE(ABORT, 'canonical entity is referenced by a claim'); END;

CREATE TRIGGER claims_canonical_entities_insert
BEFORE INSERT ON claims
WHEN (
    NEW.subject_canonical_entity_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE namespace_key = NEW.namespace_key
          AND id = NEW.subject_canonical_entity_id
          AND entity_type IN ('person','agent','device','environment','instrument','project')
    )
) OR (
    NEW.canonical_target_entity_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE namespace_key = NEW.namespace_key AND id = NEW.canonical_target_entity_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical entity type or namespace mismatch');
END;

CREATE TRIGGER claims_canonical_entities_update
BEFORE UPDATE OF namespace_key, subject_canonical_entity_id, canonical_target_entity_id ON claims
WHEN (
    NEW.subject_canonical_entity_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE namespace_key = NEW.namespace_key
          AND id = NEW.subject_canonical_entity_id
          AND entity_type IN ('person','agent','device','environment','instrument','project')
    )
) OR (
    NEW.canonical_target_entity_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM canonical_entities
        WHERE namespace_key = NEW.namespace_key AND id = NEW.canonical_target_entity_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'claim canonical entity type or namespace mismatch');
END;

CREATE INDEX idx_claims_subject_canonical_entity
ON claims(namespace_key, subject_canonical_entity_id, status)
WHERE subject_canonical_entity_id IS NOT NULL;
CREATE INDEX idx_claims_canonical_target_entity
ON claims(namespace_key, canonical_target_entity_id, status)
WHERE canonical_target_entity_id IS NOT NULL;
