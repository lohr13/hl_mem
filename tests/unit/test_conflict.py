from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key


def test_conflict_key_is_canonical_and_stable() -> None:
    assert compute_conflict_key("Default", "用 户", "Preference", {"scope": "ui", "note": 1}) == \
           compute_conflict_key("default", "用户", "preference", {"note": 2, "scope": "ui"})


def test_deterministic_conflict_rules() -> None:
    resolver = ConflictResolver()
    base = {"predicate": "preference", "value_json": '"深色"', "source_authority": "medium"}
    assert resolver.resolve(base, {**base}) == "entails"
    assert resolver.resolve(base, {**base, "value_json": '"浅色"'}) == "state_change"
    assert resolver.resolve(base, {"predicate": "uses", "value_json": '"SQLite"'}) == "compatible"
    generic = {"predicate": "count", "value_json": "1", "source_authority": "high"}
    assert resolver.resolve(generic, {**generic, "value_json": "2"}) == "contradicts"


def test_change_qualifier_signals_state_change() -> None:
    resolver = ConflictResolver()
    base = {"predicate": "偏好", "value_json": '"深色模式"', "source_authority": "medium"}
    changed = {**base, "value_json": '"浅色模式"', "qualifiers": {"change": True}}
    assert resolver.resolve(base, changed) == "state_change"
