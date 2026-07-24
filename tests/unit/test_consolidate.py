"""M5 冲突归并 worker 测试。"""

from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.consolidate import ConflictConsolidator, ConsolidationDecision, enqueue_daily_consolidation


class Judge:
    def __init__(self, kind="compatible", confidence=1.0, current_claim_id=None):
        self.decision = ConsolidationDecision(kind, confidence, "测试", current_claim_id)

    def judge(self, _left, _right):
        return self.decision


def _claim(connection, claim_id, vector, **values):
    row = {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "用户",
        "canonical_attribute": "choice.tool",
        "predicate": "使用",
        "value_json": f'"{claim_id}"',
        "status": "active",
        "scope": "permanent",
        "valid_from": f"2026-01-0{claim_id == 'b' and 2 or 1}T00:00:00Z",
        "recorded_from": "2026-01-01T00:00:00Z",
        "embedding_dense": pack_vector(vector),
        "embedding_model": "fake-v1",
    }
    row.update(values)
    assert ClaimRepository(connection).insert_claim(row)


def test_candidate_thresholds_and_pair_idempotency(tmp_path) -> None:
    connection = Database(tmp_path / "pairs.db").open()
    _claim(connection, "a", [1.0, 0.0])
    _claim(connection, "b", [0.8, 0.6])
    worker = ConflictConsolidator(connection, Judge())
    assert [(pair.left["id"], pair.right["id"]) for pair in worker.scan_candidates("default", None, 10)] == [("a", "b")]
    assert worker.run_batch(10)["reviewed"] == 1
    assert worker.run_batch(10)["reviewed"] == 0


def test_state_change_supersedes_and_low_confidence_does_not_mutate(tmp_path) -> None:
    connection = Database(tmp_path / "state.db").open()
    _claim(connection, "a", [1.0, 0.0])
    _claim(connection, "b", [0.8, 0.6])
    result = ConflictConsolidator(connection, Judge("state_change", 1.0, "b")).run_batch(10)
    assert result["state_change"] == 1
    assert ClaimRepository(connection).get_claim("a")["status"] == "superseded"

    other = Database(tmp_path / "low.db").open()
    _claim(other, "a", [1.0, 0.0])
    _claim(other, "b", [0.8, 0.6])
    assert (
        ConflictConsolidator(other, Judge("contradiction", 0.1), confidence_threshold=0.8).run_batch(10)[
            "manual_review"
        ]
        == 1
    )
    assert {row[0] for row in other.execute("SELECT status FROM claims")} == {"active"}


def test_daily_scheduler_is_idempotent_and_configurable(tmp_path) -> None:
    connection = Database(tmp_path / "schedule.db").open()
    assert enqueue_daily_consolidation(connection, "2026-07-22T03:29:00+00:00", "03:30") is False
    assert enqueue_daily_consolidation(connection, "2026-07-22T03:30:00+00:00", "03:30") is True
    assert enqueue_daily_consolidation(connection, "2026-07-22T12:00:00+00:00", "03:30") is False
    row = connection.execute("SELECT job_type,idempotency_key FROM jobs").fetchone()
    assert tuple(row) == ("consolidate_conflicts", "consolidate:2026-07-22")
