"""Recall API 合同与快照保护测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.eval.fixtures.build_snapshot import build_snapshot


def test_recall_contract_keeps_observations_disabled(eval_client: TestClient) -> None:
    response = eval_client.post("/v1/recall", json={"query": "不存在的记忆", "limit": 5})

    assert response.status_code == 200
    assert response.json()["observations"] == []


def test_snapshot_builder_does_not_modify_source(eval_database_path: Path, tmp_path: Path) -> None:
    with eval_database_path.open("rb") as stream:
        before = stream.read()
    snapshot = tmp_path / "snapshot.db"

    manifest = build_snapshot(eval_database_path, snapshot, tmp_path / "manifest.json")

    assert eval_database_path.read_bytes() == before
    assert snapshot.is_file()
    assert manifest["snapshot_sha256"]
