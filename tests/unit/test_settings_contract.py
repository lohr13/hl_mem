from __future__ import annotations

from dataclasses import fields, replace

import pytest

from hl_mem.api.schemas import RecallInput
from hl_mem.config import DEDUP_SEMANTIC_THRESHOLD
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings


def test_settings_contract_has_authoritative_defaults() -> None:
    settings = Settings()

    assert len(fields(Settings)) == 210
    assert settings.schema_version == 1
    assert settings.plugins_enabled == ()
    assert settings.plugin_options == {}
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_reasoning_effort is None
    assert settings.llm_max_tokens is None
    assert settings.llm_timeout == 90
    assert settings.llm_structured_mode == "json_object"
    assert settings.llm_thinking_control == "auto"
    assert settings.extractor_mode == "llm"
    assert settings.verification_mode == "off"
    assert settings.extraction_soft_split_enabled is False
    assert settings.extraction_delta_repair_enabled is False
    assert settings.embedder_mode == "real"
    assert settings.embedding_api_mode == "compatible"
    assert settings.reranker_mode == "off"
    assert settings.image_describer_mode == "off"
    assert settings.query_expansion_mode == "off"
    assert settings.query_expansion_model is None
    assert settings.query_expansion_provider is None
    assert settings.query_expansion_base_url is None
    assert settings.query_expansion_api_key is None
    assert settings.query_expansion_timeout_seconds == 15.0
    assert settings.query_expansion_total_timeout_seconds == 16.0
    assert settings.relation_discovery_mode == "off"
    assert settings.resurrection_mode == "off"
    assert settings.decay_model == "activation_halflife"
    assert settings.dedup_threshold == 0.92
    assert settings.daily_token_limit == 500_000
    assert settings.usage_daily_request_limit == 0
    assert settings.usage_daily_cost_limit_microunits == 0
    assert settings.usage_reservation_lease_seconds == 300
    assert settings.conflict_auto_resolve_enabled is True
    assert settings.conflict_auto_mode == "l0_only"
    assert settings.conflict_maintenance_max_cases == 50
    assert settings.conflict_maintenance_budget_ms == 1_000
    assert settings.conflict_failure_backoff_seconds == 300
    assert settings.conflict_writer_yield_ms == 25
    assert settings.conflict_auto_resolve_max_candidates == 8
    assert not any(settings_field.name.startswith("maintenance_judge_") for settings_field in fields(Settings))
    assert settings.price_target_mode == "enforce"
    assert settings.plan_fulfillment_mode == "enforce"
    assert settings.latest_wins_mode == "observe"
    assert settings.latest_wins_slots == ("config.version",)
    assert settings.operational_cleanup_enabled is True
    assert settings.operational_batch_size == 2_000
    assert settings.expired_cleanup_mode == "observe"
    assert settings.expired_claim_retention_days == 90
    assert settings.expired_cleanup_batch_size == 100
    assert settings.job_succeeded_days == 30
    assert settings.job_dead_days == 90
    assert settings.llm_span_days == 30
    assert settings.dedup_pair_days == 90
    assert settings.feedback_uninjected_days == 7
    assert settings.feedback_unlabeled_days == 90
    assert settings.dedup_max_pending_pairs == 10_000
    assert settings.snapshot()["conflict_maintenance_max_cases"] == 50
    assert settings.snapshot()["extraction_soft_split_enabled"] is False
    assert settings.snapshot()["extraction_delta_repair_enabled"] is False
    assert settings.snapshot()["price_target_mode"] == "enforce"
    assert settings.snapshot()["plan_fulfillment_mode"] == "enforce"
    assert settings.snapshot()["latest_wins_mode"] == "observe"
    assert settings.snapshot()["latest_wins_slots"] == ("config.version",)
    assert settings.snapshot()["operational_batch_size"] == 2_000
    assert settings.echo_suppression_mode == "enforce"
    assert settings.echo_session_window_seconds == 1800
    assert settings.echo_pending_review_enabled is False
    assert settings.echo_pending_similarity_threshold == 0.95
    assert settings.echo_pending_max_seconds == 7200
    assert settings.snapshot()["echo_suppression_mode"] == "enforce"
    assert settings.freshness_annotation_mode == "render"
    assert settings.snapshot()["freshness_annotation_mode"] == "render"
    assert settings.snapshot()["query_expansion_provider"] is None
    assert settings.snapshot()["query_expansion_base_url"] is None
    assert settings.snapshot()["query_expansion_api_key_configured"] is False


def test_settings_snapshot_reports_query_expansion_secret_without_exposing_it() -> None:
    settings = replace(
        Settings(),
        query_expansion_provider="dashscope",
        query_expansion_base_url="https://qe.example.com/v1",
        query_expansion_api_key="qe-secret",
    )

    snapshot = settings.snapshot()

    assert snapshot["query_expansion_provider"] == "dashscope"
    assert snapshot["query_expansion_base_url"] == "https://qe.example.com/v1"
    assert snapshot["query_expansion_api_key_configured"] is True
    assert "qe-secret" not in repr(snapshot)


def test_settings_without_config_enable_injection_governance_by_default() -> None:
    settings = Settings()

    assert settings.echo_suppression_mode == "enforce"
    assert settings.freshness_annotation_mode == "render"


def test_conflict_l0_only_mode_is_a_valid_configuration() -> None:
    replace(Settings.for_test(), conflict_auto_mode="l0_only").validate()


def test_max_request_body_validation_accepts_zero_and_rejects_negative() -> None:
    replace(Settings.for_test(), max_request_body=0).validate()

    with pytest.raises(ConfigurationError, match="server.max_request_body must be non-negative"):
        replace(Settings.for_test(), max_request_body=-1).validate()


@pytest.mark.parametrize("retired_mode", ("observe", "enforce"))
def test_retired_conflict_modes_fail_programmatic_validation(retired_mode: str) -> None:
    with pytest.raises(ConfigurationError, match="conflict.auto_mode must be 'off' or 'l0_only'"):
        replace(Settings.for_test(), conflict_auto_mode=retired_mode).validate()  # type: ignore[arg-type]


def test_settings_contract_includes_bypass_and_recall_fields() -> None:
    settings = Settings()

    assert settings.hermes_enabled is False
    assert settings.hermes_url == "http://127.0.0.1:8200"
    assert settings.hermes_timeout == 30
    assert settings.hermes_on_demand_recall_timeout_seconds == 8.0
    assert settings.hermes_manual_conflict_notice is True
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
    assert settings.recall_default_limit == 5
    assert settings.recall_vector_scan_limit == 200
    assert settings.recall_dense_enabled is True
    assert settings.entity_constraint_mode == "observe"
    assert settings.snapshot()["recall_default_limit"] == 5
    assert settings.snapshot()["recall_vector_scan_limit"] == 200
    assert settings.snapshot()["recall_dense_enabled"] is True
    assert settings.snapshot()["entity_constraint_mode"] == "observe"
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


@pytest.mark.parametrize(
    ("changes", "toml_path"),
    [
        ({"feedback_lifecycle_mode": "invalid"}, "retention.feedback_lifecycle_mode"),
        ({"feedback_min_samples": 0}, "recall.feedback_min_samples"),
        ({"llm_provider": "Bad Provider"}, "llm.provider"),
        ({"llm_reasoning_effort": "medium"}, "llm.reasoning_effort"),
        ({"llm_thinking_control": "invalid"}, "llm.thinking_control"),
        ({"llm_max_tokens": 0}, "llm.max_tokens"),
        ({"relation_expansion_mode": "invalid"}, "relation.expansion_mode"),
        ({"relevance_gate_mode": "invalid"}, "recall.relevance_gate_mode"),
        ({"entity_constraint_mode": "invalid"}, "recall.entity_constraint_mode"),
        ({"query_context_mode": "invalid"}, "recall.query_context_mode"),
        ({"echo_suppression_mode": "invalid"}, "recall.echo_suppression_mode"),
        ({"echo_session_window_seconds": 59}, "recall.echo_session_window_seconds"),
        ({"freshness_annotation_mode": "invalid"}, "recall.freshness_annotation_mode"),
        ({"procedure_recall_mode": "invalid"}, "recall.procedure_mode"),
        ({"vector_batch_size": 0}, "recall.vector_batch_size"),
        ({"hermes_on_demand_recall_timeout_seconds": 0}, "hermes.on_demand_recall_timeout_seconds"),
        ({"dedup_threshold": 2.0}, "dedup.threshold"),
        ({"index_text_mode": "invalid"}, "index.text_mode"),
        ({"reranker_provider": "Bad Provider"}, "reranker.provider"),
        ({"usage_reservation_lease_seconds": 0}, "usage.reservation_lease_seconds"),
        ({"verification_mode": "invalid"}, "extraction.verification_mode"),
        ({"conflict_maintenance_max_cases": 0}, "worker.conflict_maintenance_max_cases"),
        ({"conflict_maintenance_budget_ms": 49}, "worker.conflict_maintenance_budget_ms"),
        ({"conflict_failure_backoff_seconds": 0}, "worker.conflict_failure_backoff_seconds"),
        ({"conflict_writer_yield_ms": 1001}, "worker.conflict_writer_yield_ms"),
        ({"operational_batch_size": 0}, "retention.operational_batch_size"),
        ({"expired_cleanup_mode": "invalid"}, "retention.expired_cleanup_mode"),
        ({"expired_claim_retention_days": 0}, "retention.expired_claim_retention_days"),
        ({"expired_cleanup_batch_size": 0}, "retention.expired_cleanup_batch_size"),
        ({"job_succeeded_days": 0}, "retention.job_succeeded_days"),
        ({"feedback_unlabeled_days": 0}, "retention.feedback_unlabeled_days"),
        ({"dedup_max_pending_pairs": 0}, "dedup.max_pending_pairs"),
    ],
)
def test_validation_errors_reference_toml_paths(changes: dict[str, object], toml_path: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        replace(Settings.for_test(), **changes).validate()

    message = str(caught.value)
    assert toml_path in message
    assert "HL_MEM_" not in message


@pytest.mark.parametrize("value", [-1, "4000", True])
def test_llm_max_tokens_rejects_negative_or_non_integer_values(value: object) -> None:
    with pytest.raises(ConfigurationError, match=r"llm\.max_tokens"):
        replace(Settings.for_test(), llm_max_tokens=value).validate()  # type: ignore[arg-type]
