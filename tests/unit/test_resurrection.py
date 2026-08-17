"""Deterministic cold-path resurrection tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.application.resurrection import ResurrectionService
from hl_mem.ingest.embedder import pack_vector
from hl_mem.lifecycle import InvalidTransitionError, assert_transition
from hl_mem.observability.audit import audit_scope
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-15T00:00:00+00:00"


class _Embedder:
    dim = 2
    model = "resurrection-test"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_one(self, text: str) -> bytes:
        self.texts.append(text)
        return pack_vector([1.0, 0.0])

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_one(text) for text in texts]


class _FailingEmbedder(_Embedder):
    def embed_one(self, text: str) -> bytes:
        raise RuntimeError("embedding provider timeout")


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def emit(self, *args: object, **kwargs: object) -> bool:
        self.events.append((args, kwargs))
        return True


def _connection(tmp_path):
    return Database(tmp_path / "resurrection.db").open()


def _insert_event(connection, event_id: str = "event-1") -> None:
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES (?,?,?,?,?,?)",
        (event_id, "message", "user", '{"text":"团队采用海风看板"}', NOW, NOW),
    )


def _insert_claim(connection, claim_id: str = "claim-1", **overrides: object) -> None:
    claim = {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "团队",
        "predicate": "采用",
        "value": "海风看板",
        "recorded_from": NOW,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "status": "archived",
        "confidence": 0.73,
        "scope": "permanent",
        "embedding_dense": None,
    }
    claim.update(overrides)
    assert ClaimRepository(connection).insert_claim(claim, commit=False)


def _link_source(connection, claim_id: str = "claim-1", event_id: str = "event-1") -> None:
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES (?,?,?,?,?,?)",
        (f"link-{claim_id}", "claim", claim_id, "event", event_id, "derived_from"),
    )
    connection.commit()


def _settings(**overrides: object) -> Settings:
    settings = replace(
        Settings.for_test(),
        resurrection_mode="auto",
        resurrection_candidate_limit=3,
        resurrection_min_term_coverage=0.8,
    )
    return replace(settings, **overrides)


def test_archived_to_active_is_the_only_new_terminal_exit() -> None:
    assert_transition("archived", "active")
    for terminal in ("retracted", "superseded", "expired"):
        with pytest.raises(InvalidTransitionError):
            assert_transition(terminal, "active")


def test_resurrection_reembeds_activates_preserves_confidence_and_audits(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    _insert_claim(connection)
    _link_source(connection)
    embedder = _Embedder()
    audit = _Audit()

    with audit_scope(audit, query_id="query-1"):
        resurrected = ResurrectionService(connection, embedder, _settings()).try_resurrect(
            "团队 海风看板",
            namespace="default",
            as_of=NOW,
        )

    assert resurrected is not None and resurrected["id"] == "claim-1"
    row = connection.execute(
        "SELECT status,confidence,embedding_dense,embedding_model,embedding_dim FROM claims WHERE id='claim-1'"
    ).fetchone()
    assert tuple(row[:2]) == ("active", 0.73)
    assert row[2] == pack_vector([1.0, 0.0])
    assert tuple(row[3:]) == ("resurrection-test", 2)
    assert embedder.texts == ["团队：海风看板"]
    assert audit.events[0][0] == ("recall", "resurrection", "resurrected")
    assert audit.events[0][1]["claim_id"] == "claim-1"


def test_resurrection_is_idempotent_and_cold_searches_archived_only(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    _insert_claim(connection)
    _link_source(connection)
    embedder = _Embedder()
    service = ResurrectionService(connection, embedder, _settings())

    assert service.try_resurrect("团队 海风看板", namespace="default", as_of=NOW) is not None
    assert service.try_resurrect("团队 海风看板", namespace="default", as_of=NOW) is None
    assert embedder.texts == ["团队：海风看板"]


def test_resurrection_embedding_failure_falls_back_to_original_recall(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    _insert_claim(connection)
    _link_source(connection)

    response = RecallService(
        connection,
        _FailingEmbedder(),
        settings=_settings(recall_dense_enabled=False),
    ).recall("团队 海风看板", as_of=NOW)

    assert response["total"] == 0
    assert connection.execute("SELECT status FROM claims WHERE id='claim-1'").fetchone()[0] == "archived"


def test_resurrection_rejects_partially_dangling_evidence(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    _insert_claim(connection)
    _link_source(connection)
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('dangling','claim','claim-1','event','missing-event','derived_from')"
    )
    connection.commit()

    assert (
        ResurrectionService(connection, _Embedder(), _settings()).try_resurrect(
            "团队 海风看板",
            namespace="default",
            as_of=NOW,
        )
        is None
    )


def test_resurrection_rejects_retracted_claim_evidence(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_claim(connection)
    _insert_claim(connection, "source-claim", status="retracted", value="旧证据")
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('claim-source','claim','claim-1','claim','source-claim','derived_from')"
    )
    connection.commit()

    assert (
        ResurrectionService(connection, _Embedder(), _settings()).try_resurrect(
            "团队 海风看板",
            namespace="default",
            as_of=NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_source",
        "expired_valid_time",
        "active_conflict_rival",
        "low_term_coverage",
    ],
)
def test_resurrection_rechecks_safety_gates(tmp_path, mutation: str) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    claim_overrides: dict[str, object] = {}
    query = "团队 海风看板"
    if mutation == "expired_valid_time":
        claim_overrides["valid_to"] = "2026-08-01T00:00:00+00:00"
    if mutation == "active_conflict_rival":
        claim_overrides.update(conflict_key="default:config.model:team", canonical_slot="config.model")
    _insert_claim(connection, **claim_overrides)
    if mutation != "missing_source":
        _link_source(connection)
    if mutation == "active_conflict_rival":
        _insert_claim(
            connection,
            "rival",
            status="active",
            conflict_key="default:config.model:team",
            canonical_slot="config.model",
            value="山岚看板",
        )
        connection.commit()
    if mutation == "low_term_coverage":
        query = "团队 海风看板 不存在的额外词"

    result = ResurrectionService(connection, _Embedder(), _settings()).try_resurrect(
        query,
        namespace="default",
        as_of=NOW,
    )

    assert result is None
    assert connection.execute("SELECT status FROM claims WHERE id='claim-1'").fetchone()[0] == "archived"


def test_041_trigger_allows_safe_resurrection_and_blocks_competing_active(tmp_path) -> None:
    connection = _connection(tmp_path)
    _insert_claim(
        connection,
        conflict_key="default:config.model:team",
        canonical_slot="config.model",
    )
    connection.commit()

    connection.execute("UPDATE claims SET status='active' WHERE id='claim-1'")
    connection.commit()
    assert connection.execute("SELECT status FROM claims WHERE id='claim-1'").fetchone()[0] == "active"

    _insert_claim(
        connection,
        "claim-2",
        conflict_key="default:config.model:team",
        canonical_slot="config.model",
    )
    connection.commit()
    with pytest.raises(Exception, match="exclusive conflict group"):
        connection.execute("UPDATE claims SET status='active' WHERE id='claim-2'")


def test_resurrection_mode_defaults_auto_and_validates() -> None:
    assert Settings().resurrection_mode == "auto"
    with pytest.raises(Exception, match="recall.resurrection_mode"):
        replace(Settings.for_test(), resurrection_mode="invalid").validate()


@pytest.mark.parametrize(("mode", "expected_total"), [("off", 0), ("auto", 1)])
def test_main_recall_only_uses_cold_path_when_feature_is_auto(tmp_path, mode: str, expected_total: int) -> None:
    connection = _connection(tmp_path)
    _insert_event(connection)
    _insert_claim(connection)
    _link_source(connection)

    response = RecallService(
        connection,
        _Embedder(),
        settings=_settings(resurrection_mode=mode, recall_dense_enabled=False),
    ).recall("团队 海风看板", as_of=NOW)

    assert response["total"] == expected_total
    expected_status = "active" if mode == "auto" else "archived"
    assert connection.execute("SELECT status FROM claims WHERE id='claim-1'").fetchone()[0] == expected_status


def test_readonly_recall_defers_resurrection_and_returns_the_safe_candidate(tmp_path) -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.resurrections: list[tuple[str, str, bytes, str, int]] = []

        def submit_access(self, _query_id: str, _claim_ids: list[str], _accessed_at: str) -> bool:
            return True

        def submit_exposures(self, _query_id: str, _exposures: list[tuple[object, ...]]) -> bool:
            return True

        def submit_resurrection(
            self,
            query_id: str,
            claim_id: str,
            embedding: bytes,
            embedding_model: str,
            embedding_dim: int,
            *,
            namespace: str,
            as_of: str,
            known_as_of: str | None,
        ) -> bool:
            del namespace, as_of, known_as_of
            self.resurrections.append((query_id, claim_id, embedding, embedding_model, embedding_dim))
            return True

    database = Database(tmp_path / "readonly-resurrection.db")
    writer = database.open()
    _insert_event(writer)
    _insert_claim(writer)
    _link_source(writer)
    sink = RecordingSink()

    with database.connect_readonly() as reader:
        response = RecallService(
            reader,
            _Embedder(),
            settings=_settings(recall_dense_enabled=False),
            side_effect_sink=sink,
        ).recall("团队 海风看板", as_of=NOW, query_id="query-readonly")

    assert response["total"] == 1
    assert response["results"][0]["id"] == "claim-1"
    assert writer.execute("SELECT status FROM claims WHERE id='claim-1'").fetchone()[0] == "archived"
    assert sink.resurrections == [("query-readonly", "claim-1", pack_vector([1.0, 0.0]), "resurrection-test", 2)]
    database.close()
