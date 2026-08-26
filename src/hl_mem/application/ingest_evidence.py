import uuid
from collections.abc import Sequence
from typing import Any

from hl_mem.storage.evidence import EvidenceRepository


def link_source_events(
    repository: EvidenceRepository,
    claim_id: str,
    events: Sequence[dict[str, Any]],
) -> None:
    seen: set[str] = set()
    for event in events:
        event_id = str(event["id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        exists = repository.connection.execute(
            "SELECT 1 FROM evidence_links WHERE derived_type='claim' AND derived_id=? "
            "AND evidence_type='event' AND evidence_id=? AND relation='derived_from' LIMIT 1",
            (claim_id, event_id),
        ).fetchone()
        if exists is None:
            _add_event_link(repository, claim_id, event_id, "derived_from")
        for description_event_id in event.get("_image_description_event_ids", []):
            _add_event_link(repository, claim_id, str(description_event_id), "supports")


def _add_event_link(repository: EvidenceRepository, claim_id: str, event_id: str, relation: str) -> None:
    repository.add_link(
        {
            "id": uuid.uuid4().hex,
            "derived_type": "claim",
            "derived_id": claim_id,
            "evidence_type": "event",
            "evidence_id": event_id,
            "relation": relation,
            "weight": 1.0,
        },
        commit=False,
    )
