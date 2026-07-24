from __future__ import annotations

import httpx

from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.ingest.embeddings import pack_vector
from hl_mem.recall.reranker import FakeReranker, Reranker


def test_server_reranker_on_without_key_falls_back_to_disabled(monkeypatch) -> None:
    from hl_mem.components import make_reranker
    from hl_mem.settings import Settings

    monkeypatch.setenv("HL_MEM_ALLOW_FAKE_FALLBACK", "true")
    monkeypatch.setenv("HL_MEM_RERANKER", "on")
    monkeypatch.delenv("RERANKER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    assert make_reranker(Settings.from_env()) is None


def test_server_reranker_initialization_failure_falls_back(monkeypatch) -> None:
    import hl_mem.components as components
    from hl_mem.settings import Settings

    monkeypatch.setenv("HL_MEM_RERANKER", "on")
    monkeypatch.setenv("RERANKER_API_KEY", "test-key")
    monkeypatch.setattr(components, "Reranker", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad")))

    assert components.make_reranker(Settings.from_env()) is None


def _claims() -> list[dict]:
    return [
        {
            "id": claim_id,
            "subject_entity_id": "用户",
            "predicate": "偏好",
            "value_json": value,
            "embedding_dense": pack_vector([score]),
        }
        for claim_id, value, score in (
            ("first", '"中文一"', 1.0),
            ("second", '"中文二"', 0.8),
            ("third", '"中文三"', 0.6),
        )
    ]


class Repo:
    def __init__(self) -> None:
        self.claims = _claims()

    def search_claims_fts(self, query, limit, as_of):
        return self.claims[:limit]

    def list_embedded(self, as_of):
        return self.claims


def test_fake_reranker_returns_input_order() -> None:
    assert FakeReranker().rerank("查询", ["甲", "乙", "丙"], top_n=2) == [
        (0, 1.0), (1, 0.99),
    ]


def test_reranker_empty_documents(monkeypatch) -> None:
    def unexpected_post(*args, **kwargs):
        raise AssertionError("empty input must not make an HTTP request")

    monkeypatch.setattr(httpx, "post", unexpected_post)
    assert Reranker("key").rerank("查询", []) == []


def test_pipeline_with_fake_reranker_reorders() -> None:
    class ReverseReranker:
        def rerank(self, query, documents, top_n=20):
            assert "中文" in " ".join(documents)
            return [(index, float(index)) for index in range(len(documents) - 1, -1, -1)][:top_n]

    result = hybrid_claims(Repo(), "查询", pack_vector([1.0]), 2, None, ReverseReranker())
    assert [claim["id"] for claim in result] == ["third", "second"]


def test_pipeline_without_reranker_unchanged() -> None:
    result = hybrid_claims(Repo(), "查询", pack_vector([1.0]), 2, None)
    assert [claim["id"] for claim in result] == ["first", "second"]


def test_pipeline_reranker_failure_falls_back_to_rrf() -> None:
    class FailedReranker:
        def rerank(self, query, documents, top_n=20):
            return []

    result = hybrid_claims(Repo(), "查询", pack_vector([1.0]), 2, None, FailedReranker())
    assert [claim["id"] for claim in result] == ["first", "second"]
