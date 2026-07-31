from __future__ import annotations

from dataclasses import fields, replace

from hl_mem.api.schemas import RecallInput
from hl_mem.config import DEDUP_SEMANTIC_THRESHOLD
from hl_mem.settings import Settings


def test_settings_contract_has_authoritative_defaults() -> None:
    settings = Settings()

    assert len(fields(Settings)) == 151
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_timeout == 90
    assert settings.llm_structured_mode == "json_object"
    assert settings.extractor_mode == "fake"
    assert settings.embedder_mode == "fake"
    assert settings.reranker_mode == "off"
    assert settings.image_describer_mode == "off"
    assert settings.query_expansion_mode == "off"
    assert settings.relation_discovery_mode == "off"
    assert settings.dedup_threshold == 0.92
    assert settings.daily_token_limit == 500_000


def test_settings_contract_includes_bypass_and_recall_fields() -> None:
    settings = Settings()

    assert settings.hermes_enabled is False
    assert settings.hermes_url == "http://127.0.0.1:8200"
    assert settings.hermes_timeout == 30
    assert settings.hermes_home is None
    assert settings.entity_aliases_path is None
    assert settings.database_pool_size == 8
    assert settings.database_busy_timeout_seconds == 30
    assert settings.decay_temporal_days == 7
    assert settings.archive_temporal_days == 30
    assert settings.decay_permanent_days == 90
    assert settings.archive_permanent_days == 180
    assert settings.access_bonus_every == 5
    assert settings.access_bonus_days == 1
    assert settings.access_bonus_cap_days == 30
    assert settings.decay_rollout_grace_days == 7
    assert settings.decay_min_confidence == 0.05
    assert settings.recall_default_limit == 20
    assert settings.recall_vector_scan_limit == 200
    assert settings.snapshot()["recall_default_limit"] == 20
    assert settings.snapshot()["recall_vector_scan_limit"] == 200
    assert RecallInput(query="memory").limit is None


def test_dedup_thresholds_have_independent_contracts() -> None:
    settings = Settings()

    assert DEDUP_SEMANTIC_THRESHOLD == 0.82
    assert settings.dedup_threshold == 0.92
    assert settings.recall_dedup_threshold == 0.95

    cross_subject = replace(settings, dedup_threshold=0.90)
    recall_fold = replace(settings, recall_dedup_threshold=0.97)
    assert cross_subject.recall_dedup_threshold == 0.95
    assert recall_fold.dedup_threshold == 0.92
    assert DEDUP_SEMANTIC_THRESHOLD == 0.82
