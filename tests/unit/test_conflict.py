import pytest

from hl_mem.domain.claims.conflicts import ConflictResolver, compute_conflict_key


def test_conflict_key_is_canonical_and_stable() -> None:
    assert compute_conflict_key(" Default ", "用 户", "偏好", "preference.ui_theme", {"scope": "ＵＩ", "note": 1}) == \
           compute_conflict_key("default", "用户", "偏好", "preference.ui_theme", {"note": 2, "scope": "UI"})


def test_conflict_key_aligns_cross_predicate_tool_choice_slots() -> None:
    assert compute_conflict_key("default", "用户", "使用", "choice.tool", {"role": "coding"}) is not None
    # fact.tool_choice is not an operational slot → returns None
    assert compute_conflict_key("default", "用户", "事实", None, {}) is None


def test_conflict_key_keeps_nonexclusive_configuration_slots_separate() -> None:
    port = compute_conflict_key("default", "代理", "配置", "config.port", {"service": "api"})
    network = compute_conflict_key("default", "代理", "配置", "config.network", {"target": "api"})
    path = compute_conflict_key("default", "代理", "配置", "config.path", {"purpose": "workspace"})
    environment = compute_conflict_key("default", "代理", "配置", "config.env", {"key": "API_KEY"})
    assert port != network
    assert len({port, network, path, environment}) == 4


def test_conflict_key_rejects_unsupported_version() -> None:
    with pytest.raises(ValueError, match="version 3"):
        compute_conflict_key("default", "用户", "偏好", "preference.ui_theme", {}, version=2)


def test_conflict_key_returns_none_for_null_slot() -> None:
    assert compute_conflict_key("default", "用户", "事实", None, {}) is None


def test_conflict_key_v3_ignores_predicate_for_same_slot_instance() -> None:
    left = compute_conflict_key("default", "服务", "配置", "config.port", {"service": "API"})
    right = compute_conflict_key("default", "服务", "使用", "config.port", {"service": "ａｐｉ"})
    assert left == right


def test_deterministic_conflict_rules() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "偏好",
        "canonical_slot": "preference.ui_theme",
        "value": "深色",
        "source_authority": "medium",
    }
    assert resolver.resolve(base, {**base}) == "entails"
    assert resolver.resolve(base, {**base, "value": "浅色"}) == "state_change"
    assert resolver.resolve(base, {"predicate": "使用", "value": "SQLite"}) == "compatible"
    generic = {"predicate": "count", "value": 1, "source_authority": "high"}
    assert resolver.resolve(generic, {**generic, "value": 2}) == "compatible"


def test_change_qualifier_signals_state_change() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "配置",
        "canonical_slot": "config.model",
        "value": "qwen",
        "source_authority": "medium",
    }
    changed = {**base, "value": "gpt", "qualifiers": {"change": True}}
    assert resolver.resolve(base, changed) == "state_change"


def test_resolver_compares_different_predicates_in_same_canonical_slot() -> None:
    resolver = ConflictResolver()
    existing = {
        "predicate": "使用",
        "canonical_slot": "choice.tool",
        "value": "Codex",
    }
    same_fact = {
        "predicate": "事实",
        "canonical_slot": "fact.tool_choice",  # not operational → not mutually exclusive
        "value": "Codex",
    }
    assert resolver.resolve(existing, same_fact) == "compatible"


@pytest.mark.parametrize("canonical_slot", ["plan.deadline", "choice.tool", "config.env"])
def test_nonexclusive_attributes_with_different_values_are_compatible(canonical_slot) -> None:
    resolver = ConflictResolver()
    existing = {
        "predicate": "fact",
        "canonical_slot": canonical_slot,
        "value": "old",
        "source_authority": "medium",
    }
    new = {**existing, "value": "new"}
    result = resolver.resolve(existing, new)
    assert result in ("compatible", "state_change", "contradicts")


def test_config_port_deterministic_conflict_rules() -> None:
    resolver = ConflictResolver()
    base = {
        "predicate": "配置",
        "canonical_slot": "config.port",
        "value": 8080,
        "source_authority": "medium",
    }
    assert resolver.resolve(base, {**base}) == "entails"
    assert resolver.resolve(base, {**base, "value": 8081}) == "contradicts"
    assert resolver.resolve(
        base,
        {**base, "value": 8081, "qualifiers": {"state_change": True}},
    ) == "state_change"
