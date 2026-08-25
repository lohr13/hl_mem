from __future__ import annotations

import pytest

from hl_mem.domain.entity import typed_builtin_seeds
from hl_mem.domain.entity_coordinates import (
    ActiveAlias,
    AmbiguousEntityAliasError,
    EntityCoordinateError,
    EntityTypeMismatchError,
    build_external_entity_id,
    normalize_typed_alias,
    resolve_unique_alias,
    validate_controlled_entity_id,
)


def test_typed_alias_normalization_is_nfkc_whitespace_collapsed_and_casefolded() -> None:
    assert normalize_typed_alias("  ＬＯＣＡＬ\t Pony  ") == "local pony"


@pytest.mark.parametrize(
    "entity_id",
    [
        "person:user",
        "agent:local_pony",
        "device:user_local_pc",
        "environment:local_runtime",
        "instrument:CN:600519",
        "project:hl_mem",
    ],
)
def test_controlled_entity_ids_accept_stable_typed_keys(entity_id: str) -> None:
    assert validate_controlled_entity_id(entity_id) == entity_id


@pytest.mark.parametrize(
    "entity_id",
    [
        "user",
        "service:gateway",
        "agent:",
        "agent:local pony",
        "agent:e_short",
        "agent:e_12345678901234567890",
    ],
)
def test_controlled_entity_ids_reject_untyped_unknown_or_external_keys(entity_id: str) -> None:
    with pytest.raises(EntityCoordinateError):
        validate_controlled_entity_id(entity_id)


def test_controlled_entity_id_rejects_expected_type_mismatch() -> None:
    with pytest.raises(EntityTypeMismatchError):
        validate_controlled_entity_id("environment:local_runtime", expected_type="agent")


def test_external_entity_id_uses_boundary_safe_deterministic_source_coordinates() -> None:
    assert build_external_entity_id("tenant-a", "agent", "source-42") == "agent:e_c501c28703ac9bb807ce"
    assert build_external_entity_id("tenant-a", "agent", "source-42") != build_external_entity_id(
        "tenant-aa", "agent", "source-42"
    )


def test_external_entity_id_rejects_blank_namespace_and_source_coordinates() -> None:
    for namespace_key, source_id in (("   ", "source-42"), ("tenant-a", "\t")):
        with pytest.raises(EntityCoordinateError):
            build_external_entity_id(namespace_key, "agent", source_id)


def test_cross_type_same_name_requires_expected_type() -> None:
    candidates = (
        ActiveAlias("agent:local_pony", "agent", 2),
        ActiveAlias("environment:local_runtime", "environment", 4),
    )

    with pytest.raises(AmbiguousEntityAliasError):
        resolve_unique_alias(" shared name ", candidates)

    resolved = resolve_unique_alias("shared name", candidates, expected_type="agent")
    assert resolved is not None
    assert (resolved.canonical_entity_id, resolved.entity_type, resolved.alias_version) == (
        "agent:local_pony",
        "agent",
        2,
    )


def test_cross_type_binding_is_rejected_instead_of_silently_missing() -> None:
    candidates = (ActiveAlias("environment:local_runtime", "environment", 1),)

    with pytest.raises(EntityTypeMismatchError):
        resolve_unique_alias("本地环境", candidates, expected_type="agent")


def test_topic_cannot_bind_to_subject_role() -> None:
    candidates = (ActiveAlias("topic:memory", "topic", 1),)

    with pytest.raises(EntityTypeMismatchError):
        resolve_unique_alias("memory", candidates, expected_type="topic", role="subject")


def test_environment_and_agent_builtin_aliases_never_cross_resolve() -> None:
    seeds = typed_builtin_seeds()
    local_environment = tuple(
        ActiveAlias(alias.canonical_entity_id, alias.entity_type, 1)
        for alias in seeds.aliases
        if normalize_typed_alias(alias.alias) == normalize_typed_alias("本地环境")
    )
    local_pony = tuple(
        ActiveAlias(alias.canonical_entity_id, alias.entity_type, 1)
        for alias in seeds.aliases
        if normalize_typed_alias(alias.alias) == normalize_typed_alias("本地小马")
    )

    assert resolve_unique_alias("本地环境", local_environment).canonical_entity_id == "environment:local_runtime"
    assert resolve_unique_alias("本地小马", local_pony).canonical_entity_id == "agent:local_pony"
    with pytest.raises(EntityTypeMismatchError):
        resolve_unique_alias("本地环境", local_environment, expected_type="agent")
    with pytest.raises(EntityTypeMismatchError):
        resolve_unique_alias("本地小马", local_pony, expected_type="environment")


def test_builtin_seed_adapter_contains_exact_required_typed_ids() -> None:
    seeds = typed_builtin_seeds()
    assert {
        "person:user",
        "agent:local_pony",
        "device:user_local_pc",
        "environment:local_runtime",
        "project:hl_mem",
    } <= {entity.id for entity in seeds.entities}
    assert {
        (normalize_typed_alias(alias.alias), alias.canonical_entity_id)
        for alias in seeds.aliases
        if alias.alias in {"本地小马", "用户本地电脑", "本地环境"}
    } == {
        ("本地小马", "agent:local_pony"),
        ("用户本地电脑", "device:user_local_pc"),
        ("本地环境", "environment:local_runtime"),
    }


def test_builtin_seed_adapter_maps_only_semantically_safe_remaining_legacy_aliases() -> None:
    seeds = typed_builtin_seeds()
    aliases = {normalize_typed_alias(alias.alias): alias.canonical_entity_id for alias in seeds.aliases}

    assert aliases["hermes-agent"] == "agent:hermes"
    assert "agent:hermes" in {entity.id for entity in seeds.entities}
    assert "hermes memory" not in aliases
    assert "codex cli" not in aliases
    assert "llmextractor" not in aliases
    assert "watchdog" not in aliases


def test_alias_candidate_must_point_directly_to_matching_canonical_type() -> None:
    with pytest.raises(EntityTypeMismatchError):
        resolve_unique_alias(
            "本地环境",
            (ActiveAlias("agent:local_pony", "environment", 1),),
            expected_type="environment",
        )
