ALTER TABLE memory_relations
ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy'
CHECK (provenance IN ('legacy', 'deterministic', 'manual', 'approved_proposal'));

ALTER TABLE memory_relations
ADD COLUMN proposal_id TEXT REFERENCES relation_proposals(id);

CREATE UNIQUE INDEX idx_memory_relations_proposal
ON memory_relations(proposal_id)
WHERE proposal_id IS NOT NULL;

CREATE TRIGGER memory_relations_approved_proposal_guard
BEFORE INSERT ON memory_relations
WHEN NEW.provenance = 'approved_proposal'
  AND NOT EXISTS (
      SELECT 1
      FROM relation_proposals AS proposal
      WHERE proposal.id = NEW.proposal_id
        AND proposal.status = 'pending'
        AND proposal.source_claim_id = NEW.from_id
        AND proposal.target_claim_id = NEW.to_id
        AND proposal.relation = NEW.relation
  )
BEGIN
  SELECT RAISE(ABORT, 'approved_proposal requires matching pending proposal');
END;

CREATE TRIGGER memory_relations_nonproposal_guard
BEFORE INSERT ON memory_relations
WHEN NEW.provenance <> 'approved_proposal'
  AND NEW.proposal_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'proposal_id requires approved_proposal provenance');
END;

CREATE TRIGGER memory_relations_provenance_immutable
BEFORE UPDATE OF provenance, proposal_id ON memory_relations
WHEN NEW.provenance IS NOT OLD.provenance
  OR NEW.proposal_id IS NOT OLD.proposal_id
BEGIN
  SELECT RAISE(ABORT, 'memory relation provenance is immutable');
END;
