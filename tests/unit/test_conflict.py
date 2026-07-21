import pytest

from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key


def test_conflict_key_is_canonical_and_stable() -> None:
    assert compute_conflict_key(" Default ", "用 户", "preference.ui_theme", {"scope": "ＵＩ", "note": 1}) == \
           compute_conflict_key("default", "用户", "preference.ui_theme", {"note": 2, "scope": "UI"})


def test_conflict_key_aligns_cross_predicate_tool_choice_slots() -> None:
    assert compute_conflict_key("default", "用户", "choice.tool", {}) == compute_conflict_key(
        "default", "用户", "fact.tool_choice", {}
    )


def test_conflict_key_keeps_nonexclusive_configuration_slots_separate() -> None:
    port = compute_conflict_key("default", "代理", "config.port", {})
    network = compute_conflict_key("default", "代理", "config.network", {})
    path = compute_conflict_key("default", "代理", "config.path", {})
    environment = compute_conflict_key("default", "代理", "config.env", {})
    assert port == network
    assert len({port, path, environment}) == 3


def test_conflict_key_rejects_unsupported_version() -> None:
    with pytest.raises(ValueError, match="version 2"):
        compute_conflict_key("default", "用户", "fact.other", {}, version=1)


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


def test_resolver_compares_different_predicates_in_same_canonical_slot() -> None:
    resolver = ConflictResolver()
    existing = {
        "predicate": "使用",
        "canonical_attribute": "choice.tool",
        "value_json": '"Codex"',
    }
    same_fact = {
        "predicate": "事实",
        "canonical_attribute": "fact.tool_choice",
        "value_json": '"Codex"',
    }
    assert resolver.resolve(existing, same_fact) == "entails"
