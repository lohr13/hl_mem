from hl_mem.storage.database import Database
from hl_mem.workers.mental_models import DerivedMemoryMaintainer


def _claim(connection, claim_id: str, recorded_from: str, status: str = "active") -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,predicate,value_json,status,recorded_from) "
        "VALUES (?,?,?,?,?,?)",
        (claim_id, "default", "preference", f'"{claim_id}"', status, recorded_from),
    )
    connection.commit()


def test_rebuild_requires_evidence_and_tracks_proof_count_and_watermark(tmp_path) -> None:
    connection = Database(tmp_path / "derived.db").open()
    maintainer = DerivedMemoryMaintainer(connection)
    _claim(connection, "c1", "2026-01-01T00:00:00Z")
    _claim(connection, "c2", "2026-01-02T00:00:00Z")

    result = maintainer.rebuild(
        "model-1", "mental_model", "用户偏好", ["c1", "c2"], "2026-01-03T00:00:00Z"
    )
    assert result["status"] == "active"
    assert result["proof_count"] == 2
    assert result["source_watermark"] == "2026-01-02T00:00:00Z"

    repeated = maintainer.rebuild(
        "model-1", "mental_model", "用户偏好", ["c1", "c2"], "2026-01-03T00:00:00Z"
    )
    assert repeated == result
    assert connection.execute(
        "SELECT count(*) FROM evidence_links WHERE derived_id='model-1'"
    ).fetchone()[0] == 2


def test_retracted_dependency_marks_derivation_stale(tmp_path) -> None:
    connection = Database(tmp_path / "stale.db").open()
    maintainer = DerivedMemoryMaintainer(connection)
    _claim(connection, "c1", "2026-01-01T00:00:00Z")
    maintainer.rebuild("model-1", "session_summary", "摘要", ["c1"], "2026-01-02T00:00:00Z")
    connection.execute("UPDATE claims SET status='retracted' WHERE id='c1'")
    connection.commit()

    assert maintainer.mark_stale_dependencies() == 1
    assert maintainer.get("model-1")["status"] == "stale"


def test_rebuild_rejects_empty_or_inactive_evidence(tmp_path) -> None:
    connection = Database(tmp_path / "admission.db").open()
    maintainer = DerivedMemoryMaintainer(connection)
    _claim(connection, "c1", "2026-01-01T00:00:00Z", "retracted")

    for evidence_ids in ([], ["c1"]):
        try:
            maintainer.rebuild("model-1", "mental_model", "内容", evidence_ids, "2026-01-02T00:00:00Z")
        except ValueError:
            pass
        else:
            raise AssertionError("inactive or empty evidence must be rejected")
