from hl_mem.recall.observation import ObservationBuilder


def claim(identifier, event):
    return {"id": identifier, "status": "active", "conflict_key": "same", "predicate": "p",
            "subject_entity_id": "u", "value_json": '"v"', "event_ids": [event],
            "observed_at": f"2026-01-0{identifier}", "confidence": .8}


def test_observation_needs_two_independent_events() -> None:
    builder = ObservationBuilder()
    assert builder.try_build([claim("1", "event-a")]) is None
    assert builder.try_build([claim("1", "event-a"), claim("2", "event-a")]) is None
    result = builder.try_build([claim("1", "event-a"), claim("2", "event-b")])
    assert result and "基于 2 条证据" in result["body"]
