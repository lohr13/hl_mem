from __future__ import annotations

import json
import sqlite3

import pytest

from hl_mem.api.pipeline import store_extracted
from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.ingest.embeddings import FakeEmbedder, pack_vector
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor, SYSTEM_PROMPT
from hl_mem.recall.ranking import blend_reranker_score, memory_features, memory_score
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository


NOW = "2026-07-21T00:00:00+00:00"


def _claim(connection, claim_id="c", **values):
    data = {"id": claim_id, "recorded_from": NOW, "status": "active",
            "subject_entity_id": "user", "predicate": "likes", "value_json": '"tea"',
            "confidence": 1.0, "importance": 0.5, "embedding_dense": pack_vector([1.0])}
    data.update(values)
    assert ClaimRepository(connection).insert_claim(data)
    return claim_id


def test_migration_defaults_and_index(tmp_path):
    connection = Database(tmp_path / "m.db").open()
    _claim(connection)
    row = connection.execute("SELECT scope,access_count,last_accessed_at,last_decayed_at FROM claims").fetchone()
    assert tuple(row) == ("permanent", 0, None, None)
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='idx_claims_decay'").fetchone()


def test_migration_constraints(tmp_path):
    connection = Database(tmp_path / "m.db").open()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO claims(id,recorded_from,scope) VALUES ('bad-scope',?,?)",
                           (NOW, "other"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO claims(id,recorded_from,access_count) VALUES ('bad-count',?,-1)",
                           (NOW,))


def test_narrowed_trigger_ignores_metadata_but_refreshes_text(tmp_path):
    connection = Database(tmp_path / "fts.db").open()
    _claim(connection)
    before = connection.total_changes
    connection.execute("UPDATE claims SET access_count=1,confidence=.8,status='disputed' WHERE id='c'")
    assert connection.total_changes - before == 1
    connection.execute("UPDATE claims SET value_json='\"coffee\"' WHERE id='c'")
    assert connection.execute("SELECT count(*) FROM claims_fts WHERE claims_fts MATCH 'coffee'").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM claims_fts WHERE claims_fts MATCH 'tea'").fetchone()[0] == 0


@pytest.mark.parametrize("query", ["[", "]", "(", ")", ":", "*", "^", '"', "   "])
@pytest.mark.parametrize("as_of", [None, NOW])
def test_claim_fts_special_characters_do_not_raise(tmp_path, query, as_of):
    connection = Database(tmp_path / "fts-special.db").open()
    _claim(connection)

    assert ClaimRepository(connection).search_claims_fts(query, as_of=as_of) == []


def test_claim_fts_quoted_tokens_still_match_text(tmp_path):
    connection = Database(tmp_path / "fts-literal.db").open()
    _claim(connection)

    assert [claim["id"] for claim in ClaimRepository(connection).search_claims_fts("likes tea")] == ["c"]


def test_extracted_fields_are_appended_defaults():
    claim = ExtractedClaim("p", "v", .9, "stable", "s", {}, "r")
    assert claim.scope == "permanent" and claim.importance == .5


def test_llm_claim_parses_and_clamps():
    claim = LLMExtractor._claim({"value": "x", "scope": "temporal",
                                 "importance": 4, "confidence": -2})
    assert (claim.scope, claim.importance, claim.confidence) == ("temporal", 1.0, 0.0)


def test_llm_claim_invalid_defaults_and_prompt():
    claim = LLMExtractor._claim({"value": "x", "scope": "bad",
                                 "importance": "bad", "confidence": None})
    assert (claim.scope, claim.importance, claim.confidence) == ("permanent", .5, .5)
    assert "independent from volatility" in SYSTEM_PROMPT


@pytest.mark.parametrize(("volatility", "scope", "expires"), [
    ("ephemeral", "temporal", "2026-07-28T00:00:00+00:00"),
    ("ephemeral", "permanent", None), ("stable", "temporal", None),
    ("stable", "permanent", None),
])
def test_ttl_matrix(tmp_path, volatility, scope, expires):
    connection = Database(tmp_path / f"{volatility}-{scope}.db").open()
    extracted = ExtractedClaim("p", "v", volatility=volatility, scope=scope)
    claim_id = store_extracted(connection, extracted, {"id": "e", "actor_type": "user"},
                               NOW, FakeEmbedder(2))
    row = connection.execute("SELECT expires_at,scope,importance FROM claims WHERE id=?",
                             (claim_id,)).fetchone()
    assert tuple(row) == (expires, scope, .5)


def test_ranking_features_bounds_and_log_access():
    features = memory_features({"observed_at": "2026-06-21T00:00:00+00:00",
                                "access_count": 9, "confidence": 2, "importance": -1,
                                "helpful_rate": 0.75},
                               2, 99, NOW)
    assert features["semantic"] == 1 and features["recency"] == .5
    assert 0 < features["access_frequency"] < 1
    assert features["confidence"] == 1 and features["importance"] == 0
    assert features["utility"] == 0.75


def test_ranking_malformed_date_is_safe():
    assert memory_features({"observed_at": "bad"}, .5, 0, NOW)["recency"] == 0


def test_memory_score_exact_weights_and_semantic_dominates():
    high_semantic = {"semantic": 1, "recency": 0, "access_frequency": 0,
                     "confidence": 0, "importance": 0, "utility": 0}
    priors = {"semantic": 0, "recency": 1, "access_frequency": 1,
              "confidence": 1, "importance": 1, "utility": 1}
    assert memory_score(high_semantic) == pytest.approx(.65)
    assert memory_score(priors) == pytest.approx(.35)


def test_historical_helpful_rate_breaks_otherwise_equal_ranking(tmp_path):
    connection = Database(tmp_path / "utility.db").open()
    _claim(connection, "helpful")
    _claim(connection, "unhelpful")
    connection.executemany(
        "INSERT INTO retrieval_feedback(id,query_id,memory_type,memory_id,used_by_model,helpful,created_at) "
        "VALUES (?,?,?,?,1,?,?)",
        [
            ("f1", "q1", "claim", "helpful", 1, NOW),
            ("f2", "q2", "claim", "unhelpful", 0, NOW),
        ],
    )
    connection.commit()

    results = hybrid_claims(ClaimRepository(connection), "likes tea", pack_vector([1.0]), 2, None, now=NOW)

    assert [item["id"] for item in results] == ["helpful", "unhelpful"]


def test_reranker_blend_clamps_and_uses_prior():
    low = {"recency": 0, "access_frequency": 0, "confidence": 0, "importance": 0, "utility": 0}
    high = {key: 1 for key in low}
    assert blend_reranker_score(3, low) == .8
    assert blend_reranker_score(.5, high) == pytest.approx(.6)


def test_record_access_deduplicates_and_filters_status(tmp_path):
    connection = Database(tmp_path / "access.db").open()
    _claim(connection, "active")
    _claim(connection, "archived", status="archived")
    assert ClaimRepository(connection).record_access(["active", "active", "archived"], NOW) == 1
    assert tuple(connection.execute("SELECT access_count,last_accessed_at FROM claims WHERE id='active'").fetchone()) == (1, NOW)


def test_vector_search_is_bounded_and_sorted(tmp_path):
    connection = Database(tmp_path / "vector.db").open()
    _claim(connection, "opposite", embedding_dense=pack_vector([-1.0]))
    _claim(connection, "same", embedding_dense=pack_vector([1.0]))
    assert [c["id"] for c in ClaimRepository(connection).search_claims_vector(
        pack_vector([1.0]), 1)] == ["same"]


def test_hybrid_priors_break_semantic_tie():
    class Repo:
        claims = [{"id": "low", "confidence": 0, "importance": 0},
                  {"id": "high", "confidence": 1, "importance": 1}]
        def search_claims_fts(self, query, limit, as_of): return self.claims
        def list_embedded(self, as_of): return []
    assert hybrid_claims(Repo(), "q", pack_vector([1]), 2, None, now=NOW)[0]["id"] == "high"
