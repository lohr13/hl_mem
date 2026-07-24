import pytest

from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key


def test_conflict_key_is_canonical_and_stable() -> None:
    assert compute_conflict_key(" Default ", "用 户", "preference.ui_theme", {"scope": "ＵＩ", "note": 1}) == \
           compute_conflict_key("default", "用户", "preference.ui_theme", {"note": 2, "scope": "UI"})


def test_conflict_key_aligns_cross_predicate_tool_choice_slots() -> None:
    assert compute_conflict_key("default", "用户", "choice.tool", {}) != compute_conflict_key(
        "default", "用户", "fact.tool_choice", {}
    )


def test_conflict_key_keeps_nonexclusive_configuration_slots_separate() -> None:
    port = compute_conflict_key("default", "代理", "config.port", {})
    network = compute_conflict_key("default", "代理", "config.network", {})
    path = compute_conflict_key("default", "代理", "config.path", {})
    environment = compute_conflict_key("default", "代理", "config.env", {})
    assert port != network
    assert len({port, network, path, environment}) == 4


def test_conflict_key_rejects_unsupported_version() -> None:
    with pytest.raises(ValueError, match="version 2"):
        compute_conflict_key("default", "用户", "fact.other", {}, version=1)


def test_deterministic_conflict_rules() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "preference",
        "canonical_attribute": "preference.ui_theme",
        "value": "深色",
        "source_authority": "medium",
    }
    assert resolver.resolve(base, {**base}) == "entails"
    assert resolver.resolve(base, {**base, "value": "浅色"}) == "state_change"
    assert resolver.resolve(base, {"predicate": "uses", "value": "SQLite"}) == "compatible"
    generic = {"predicate": "count", "value": 1, "source_authority": "high"}
    assert resolver.resolve(generic, {**generic, "value": 2}) == "compatible"


def test_change_qualifier_signals_state_change() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "配置",
        "canonical_attribute": "config.model",
        "value": "qwen",
        "source_authority": "medium",
    }
    changed = {**base, "value": "gpt", "qualifiers": {"change": True}}
    assert resolver.resolve(base, changed) == "state_change"


def test_resolver_compares_different_predicates_in_same_canonical_slot() -> None:
    resolver = ConflictResolver()
    existing = {
        "predicate": "使用",
        "canonical_attribute": "choice.tool",
        "value": "Codex",
    }
    same_fact = {
        "predicate": "事实",
        "canonical_attribute": "fact.tool_choice",
        "value": "Codex",
    }
    assert resolver.resolve(existing, same_fact) == "compatible"


@pytest.mark.parametrize("canonical_attribute", ["plan.deadline", "choice.tool", "config.env"])
def test_nonexclusive_attributes_with_different_values_are_compatible(canonical_attribute) -> None:
    resolver = ConflictResolver()
    existing = {
        "predicate": "fact",
        "canonical_attribute": canonical_attribute,
        "value": "old",
        "source_authority": "medium",
    }
    new = {**existing, "value": "new"}
    assert resolver.resolve(existing, new) == "compatible"


def test_config_port_deterministic_conflict_rules() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "config",
        "canonical_attribute": "config.port",
        "value": 8080,
        "source_authority": "medium",
    }
    assert resolver.resolve(base, {**base}) == "entails"
    assert resolver.resolve(base, {**base, "value": 8081}) == "contradicts"
    assert resolver.resolve(
        base,
        {**base, "value": 8081, "qualifiers": {"state_change": True}},
    ) == "state_change"
