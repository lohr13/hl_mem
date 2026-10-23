"""RecallService v0.29.3 返回形状与副作用 characterization。

复用映射（不在本文件重复更细粒度断言）：

* Context Packet 严格字段与 exposure 原子性：
  ``test_context_packet.py::test_context_packet_v1_has_exact_shape_and_flat_exposure_ranks``。
* relevance 算法边界及最终候选副作用：
  ``test_relevance_gate.py::test_recall_service_side_effects_use_final_enforced_results``。
* 只读连接上的 deferred 零写入：
  ``test_recall_side_effects_deferred.py::test_recall_on_readonly_connection_submits_side_effects_without_sql_writes``。
* procedure 分支的 freshness 渲染：
  ``test_freshness_annotation.py::test_recall_service_decorates_before_packet_packing_and_traces_decision``。

这里补齐服务级四格式骨架、fake provider 次数、no-evidence、procedure 结果形状，
以及同步/deferred 两种副作用对主库的差异。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import hl_mem.application.recall as recall_module
from hl_mem.application.recall import RecallRequest, RecallService
from hl_mem.domain.recall import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.procedure_pipeline import MemoryCandidate
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-21T12:00:00+00:00"
CLAIM_IDS = ["claim-newer", "claim-older"]


class _CountingFakeEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(4)
        self.calls: list[str] = []

    def embed_one(self, text: str) -> bytes:
        self.calls.append(text)
        return super().embed_one(text)


class _RecordingSink:
    def __init__(self) -> None:
        self.access: list[tuple[str, list[str], str]] = []
        self.exposures: list[tuple[str, list[tuple[object, ...]]]] = []

    def submit_access(self, query_id: str, claim_ids: list[str], accessed_at: str) -> bool:
        self.access.append((query_id, claim_ids, accessed_at))
        return True

    def submit_exposures(self, query_id: str, exposures: list[tuple[object, ...]]) -> bool:
        self.exposures.append((query_id, exposures))
        return True


def _settings(**overrides: Any) -> Settings:
    return replace(
        Settings.for_test(),
        embedding_dim=4,
        freshness_annotation_mode="off",
        resurrection_mode="off",
        recall_dedup_threshold=0.0,
        echo_suppression_mode="off",
        **overrides,
    )


def _seed_claims(connection: Any) -> None:
    repository = ClaimRepository(connection)
    for index, (claim_id, value) in enumerate(
        ((CLAIM_IDS[0], "user likes jasmine tea"), (CLAIM_IDS[1], "user likes black tea"))
    ):
        repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "subject_entity_id": "user",
                "predicate": "偏好",
                "value": value,
                "index_text": value,
                "canonical_attribute": "preference.beverage",
                "assertion_kind": "observation",
                "scope": "permanent",
                "status": "active",
                "valid_from": f"2026-08-{20 - index:02d}T00:00:00+00:00",
                "recorded_from": f"2026-08-{20 - index:02d}T00:00:00+00:00",
                "confidence": 0.9,
                "importance": 0.8,
            }
        )


def _install_fixed_claim_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    connection: Any,
    *,
    reranker_scores: tuple[float, float] | None = None,
) -> None:
    repository = ClaimRepository(connection)
    claims = [repository.get_claim(claim_id) for claim_id in CLAIM_IDS]
    assert all(claim is not None for claim in claims)
    prepared = []
    for index, raw_claim in enumerate(claims):
        claim = dict(raw_claim or {})
        score = reranker_scores[index] if reranker_scores is not None else 0.9 - index * 0.2
        claim.update(
            _score=score,
            _score_path="reranker_applied" if reranker_scores is not None else "reranker_fallback",
            _reranker_raw_score=score if reranker_scores is not None else None,
            _features={"characterization_rank": index + 1},
        )
        prepared.append(claim)

    def fixed_hybrid_claims(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        tracer = kwargs["tracer"]
        tracer.record_channel("fts", prepared)
        tracer.record_channel("dense", [{**claim, "_score": claim["_score"]} for claim in prepared])
        if reranker_scores is not None:
            tracer.record_rerank(list(zip(CLAIM_IDS, reranker_scores, strict=True)))
        tracer.record_final(prepared)
        return [dict(claim) for claim in prepared]

    monkeypatch.setattr(recall_module, "hybrid_claims", fixed_hybrid_claims)


def _access_counts(connection: Any) -> list[int]:
    return [
        int(connection.execute("SELECT access_count FROM claims WHERE id=?", (claim_id,)).fetchone()[0])
        for claim_id in CLAIM_IDS
    ]


def test_query_planning_private_seam_freezes_constructor_and_prepared_shape(tmp_path: Any) -> None:
    database = Database(tmp_path / "query-planning-seam.db")
    connection = database.open()
    embedder = _CountingFakeEmbedder()
    service = RecallService(
        connection,
        embedder,
        settings=_settings(query_expansion_mode="off", entity_constraint_mode="off"),
    )
    request = RecallRequest(
        query="tea preference",
        limit=2,
        as_of=None,
        intent=RecallIntent.CURRENT_STATE,
        known_as_of=None,
        query_id="query-planning-seam",
        token_budget=None,
        context_mode=None,
        namespace="default",
        session_id=None,
        debug=True,
        response_format="legacy",
        ranking_now=NOW,
        injection_context=None,
    )
    session = service._resolve_recall_request(request)

    expansion = recall_module._QueryExpansionSession(service, session)

    assert expansion.prepare() is expansion
    assert [(item.text, item.source, item.weight) for item in expansion.weighted_queries] == [
        ("tea preference", "original", 1.0)
    ]
    assert len(expansion.query_blobs) == 1
    assert expansion.low_recall_expander is None
    assert expansion.entity_plan.scope_mode == "off"
    assert expansion.entity_fallback_reason is None
    assert embedder.calls == ["tea preference"]
    database.close()


@pytest.mark.parametrize(
    ("response_format", "expected_top_keys", "expected_feedback_rows"),
    [
        pytest.param(
            "legacy",
            {"results", "observations", "policies", "total", "query_id", "answerability"},
            2,
            id="legacy",
        ),
        pytest.param("context_packet", {"context_packet"}, 2, id="context-packet"),
        pytest.param(
            "both",
            {
                "results",
                "observations",
                "policies",
                "total",
                "query_id",
                "answerability",
                "context_packet",
            },
            2,
            id="both",
        ),
        pytest.param("retrieval_bundle", {"retrieval_bundle"}, 0, id="retrieval-bundle"),
    ],
)
def test_normal_recall_response_formats_freeze_shape_order_and_sync_side_effects(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    response_format: str,
    expected_top_keys: set[str],
    expected_feedback_rows: int,
) -> None:
    database = Database(tmp_path / f"recall-{response_format}.db")
    connection = database.open()
    _seed_claims(connection)
    _install_fixed_claim_pipeline(monkeypatch, connection)
    embedder = _CountingFakeEmbedder()

    response = RecallService(connection, embedder, settings=_settings()).recall(
        "tea preference",
        limit=2,
        intent=RecallIntent.CURRENT_STATE,
        query_id=f"query-{response_format}",
        response_format=response_format,
        ranking_now=NOW,
    )

    assert set(response) == expected_top_keys
    if response_format in {"legacy", "both"}:
        assert [item["id"] for item in response["results"]] == CLAIM_IDS
        assert all("score" in item for item in response["results"])
        assert response["answerability"] == "supported"
        assert "used_tokens_estimate" not in response
        assert "truncated" not in response
    if response_format in {"context_packet", "both"}:
        packet = response["context_packet"]
        assert [item["id"] for item in packet["items"]] == CLAIM_IDS
        assert all("score" not in item for item in packet["items"])
        assert packet["answerability"] == "supported"
        assert packet["used_tokens_estimate"] > 0
        assert packet["truncated"] is False
    if response_format == "retrieval_bundle":
        bundle = response["retrieval_bundle"]
        assert [item["id"] for item in bundle["items"]] == CLAIM_IDS
        assert all("score" in item for item in bundle["items"])
        assert bundle["answerability"] == "supported"
        assert bundle["used_tokens_estimate"] > 0
        assert bundle["truncated"] is False

    assert embedder.calls == ["tea preference"]
    assert _access_counts(connection) == [1, 1]
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == expected_feedback_rows
    database.close()


def test_relevance_enforce_freezes_final_order_score_and_answerability(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "recall-relevance.db")
    connection = database.open()
    _seed_claims(connection)
    _install_fixed_claim_pipeline(monkeypatch, connection, reranker_scores=(0.8, 0.1))
    embedder = _CountingFakeEmbedder()
    settings = _settings(
        relevance_gate_mode="enforce",
        relevance_reranker_floor=0.4,
        relevance_relative_drop=1.0,
    )

    response = RecallService(connection, embedder, settings=settings).recall(
        "tea preference",
        limit=2,
        intent=RecallIntent.CURRENT_STATE,
        query_id="query-relevance",
        response_format="both",
        ranking_now=NOW,
    )

    assert [item["id"] for item in response["results"]] == [CLAIM_IDS[0]]
    assert [item["id"] for item in response["context_packet"]["items"]] == [CLAIM_IDS[0]]
    assert response["results"][0]["score"] == pytest.approx(0.8)
    assert response["answerability"] == "supported"
    assert response["context_packet"]["answerability"] == "supported"
    assert embedder.calls == ["tea preference"]
    assert _access_counts(connection) == [1, 0]
    database.close()


def test_no_evidence_freezes_empty_both_shape_and_fake_provider_count(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "recall-empty.db")
    connection = database.open()
    embedder = _CountingFakeEmbedder()

    def no_claims(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs["tracer"].record_final([])
        return []

    monkeypatch.setattr(recall_module, "hybrid_claims", no_claims)
    response = RecallService(connection, embedder, settings=_settings()).recall(
        "missing memory",
        intent=RecallIntent.CURRENT_STATE,
        query_id="query-empty",
        response_format="both",
        ranking_now=NOW,
    )

    assert response["results"] == []
    assert response["total"] == 0
    assert response["answerability"] == "no_evidence"
    assert response["context_packet"]["items"] == []
    assert response["context_packet"]["answerability"] == "no_evidence"
    assert response["context_packet"]["used_tokens_estimate"] == 0
    assert response["context_packet"]["truncated"] is False
    assert embedder.calls == ["missing memory"]
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0
    database.close()


def test_procedure_branch_freezes_mixed_memory_order_scores_and_packet_shape(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "recall-procedure.db")
    connection = database.open()
    embedder = _CountingFakeEmbedder()

    def no_claims(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs["tracer"].record_final([])
        return []

    candidates = [
        MemoryCandidate("policy", "policy-1", "inspect logs before restart", 0.95, (), {"support": 3}),
        MemoryCandidate("episode", "episode-1", "restart restored service", 0.8, (), {"reward": 1.0}),
    ]
    monkeypatch.setattr(recall_module, "hybrid_claims", no_claims)
    monkeypatch.setattr(recall_module, "recall_procedure", lambda *_args, **_kwargs: candidates)

    response = RecallService(connection, embedder, settings=_settings()).recall(
        "how to recover the service",
        limit=2,
        intent=RecallIntent.PROCEDURE,
        query_id="query-procedure",
        response_format="both",
        token_budget=100,
        ranking_now=NOW,
    )

    assert [item["id"] for item in response["results"]] == ["policy-1", "episode-1"]
    assert [item["score"] for item in response["results"]] == [0.95, 0.8]
    assert [item["id"] for item in response["context_packet"]["items"]] == [
        "policy-1",
        "episode-1",
    ]
    assert [item["id"] for item in response["policies"]] == ["policy-1"]
    assert response["answerability"] == "supported"
    assert response["context_packet"]["used_tokens_estimate"] > 0
    assert response["context_packet"]["truncated"] is False
    assert embedder.calls == ["how to recover the service"]
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 2
    database.close()


def test_deferred_sink_keeps_access_and_exposure_out_of_request_connection(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "recall-deferred.db")
    connection = database.open()
    _seed_claims(connection)
    _install_fixed_claim_pipeline(monkeypatch, connection)
    sink = _RecordingSink()

    response = RecallService(
        connection,
        _CountingFakeEmbedder(),
        settings=_settings(),
        side_effect_sink=sink,
    ).recall(
        "tea preference",
        limit=2,
        query_id="query-deferred",
        response_format="both",
        ranking_now=NOW,
    )

    assert [item["id"] for item in response["results"]] == CLAIM_IDS
    assert sink.access[0][0:2] == ("query-deferred", CLAIM_IDS)
    assert sink.exposures[0][0] == "query-deferred"
    assert [str(exposure[3]) for exposure in sink.exposures[0][1]] == CLAIM_IDS
    assert _access_counts(connection) == [0, 0]
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0
    database.close()
