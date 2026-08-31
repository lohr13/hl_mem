from __future__ import annotations

from dataclasses import fields


def test_legacy_imports_are_thin_identity_facades() -> None:
    from hl_mem.config.loader import load_settings as canonical_loader
    from hl_mem.config.models import Settings as canonical_settings
    from hl_mem.config_loader import load_settings
    from hl_mem.settings import Settings

    assert Settings is canonical_settings
    assert load_settings is canonical_loader


def test_typed_groups_own_every_configuration_field_once() -> None:
    from hl_mem.config.models import (
        DatabaseConfig,
        ExtractionConfig,
        GovernanceConfig,
        IntegrationConfig,
        LifecycleConfig,
        ObservabilityConfig,
        PluginsConfig,
        RetrievalConfig,
        Settings,
    )

    groups = (
        DatabaseConfig,
        ExtractionConfig,
        RetrievalConfig,
        GovernanceConfig,
        LifecycleConfig,
        IntegrationConfig,
        ObservabilityConfig,
        PluginsConfig,
    )
    owners: dict[str, str] = {}
    for group in groups:
        for config_field in fields(group):
            assert config_field.name not in owners
            owners[config_field.name] = group.__name__
            assert set(config_field.metadata) in ({"toml"}, {"secret_env"})

    assert owners == {config_field.name: owners[config_field.name] for config_field in fields(Settings)}


def test_domain_constants_remain_available_from_config_package() -> None:
    from hl_mem.config import DEDUP_SEMANTIC_THRESHOLD, INGEST_DEDUP_PAIR_SIMILARITY_FLOOR

    assert DEDUP_SEMANTIC_THRESHOLD == 0.82
    assert INGEST_DEDUP_PAIR_SIMILARITY_FLOOR == 0.88
