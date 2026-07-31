from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.workers.induce_policies import (
    enqueue_daily_policy_induction,
    induce_policies,
)
from hl_mem.workers.worker import Worker, dispatch_job


def test_induce_policies_clusters_recent_successes_by_task_and_tool_sequence(
    tmp_path,
) -> None:
    connection = Database(tmp_path / "induce.db").open()
    service = ExperienceService(connection)
    for index in range(3):
        episode_id = f"episode-{index}"
        service.create_episode(
            episode_id,
            f"修复部署故障 {index}",
            "2026-07-20T00:00:00+00:00",
            task_type="coding",
        )
        service.add_trace(episode_id, "inspect_logs", None, None, 1.0)
        service.add_trace(episode_id, "deploy", None, None, 1.0)
        service.update_episode(episode_id, "2026-07-20T01:00:00+00:00", "success", 0.8)
    service.record_episode("old", "旧任务", "success", 1.0, "2026-07-01T00:00:00+00:00")

    result = induce_policies(connection, "2026-07-22T04:00:00+00:00")

    assert result == {"clusters": 1, "policies_induced": 1}
    policy = service.list_policies("active")[0]
    assert "coding" in policy["trigger"]
    assert policy["procedure"] == {"steps": ["inspect_logs", "deploy"]}
    assert policy["support"] == 3


def test_daily_policy_induction_is_idempotent_and_worker_dispatches(tmp_path) -> None:
    path = tmp_path / "worker.db"
    connection = Database(path).open()

    assert enqueue_daily_policy_induction(connection, "2026-07-22T04:00:00+00:00", "03:30")
    assert not enqueue_daily_policy_induction(connection, "2026-07-22T05:00:00+00:00", "03:30")

    worker = Worker(Settings(database_path=str(path), embedding_dim=2))
    assert dispatch_job(worker, {"job_type": "induce_policies"}) == {
        "clusters": 0,
        "policies_induced": 0,
    }


def test_policy_induction_buckets_by_namespace_and_keeps_evidence_local(
    tmp_path,
) -> None:
    connection = Database(tmp_path / "namespace-induction.db").open()
    service = ExperienceService(connection)
    for namespace in ("project-a", "project-b"):
        for index in range(3):
            episode_id = f"{namespace}-{index}"
            service.create_episode(
                episode_id,
                f"修复部署故障 {index}",
                "2026-07-20T00:00:00+00:00",
                task_type="coding",
                namespace=namespace,
            )
            service.add_trace(episode_id, "inspect_logs", None, None, 1.0)
            service.add_trace(episode_id, "deploy", None, None, 1.0)
            service.update_episode(
                episode_id,
                "2026-07-20T01:00:00+00:00",
                "success",
                0.8,
            )

    assert induce_policies(
        connection,
        "2026-07-22T04:00:00+00:00",
        namespace="project-a",
    ) == {"clusters": 1, "policies_induced": 1}
    assert service.list_policies("active", namespace="project-b") == []

    assert induce_policies(connection, "2026-07-22T04:00:00+00:00") == {
        "clusters": 2,
        "policies_induced": 1,
    }
    assert len(service.list_policies("active", namespace="project-a")) == 1
    assert len(service.list_policies("active", namespace="project-b")) == 1
    linked_namespaces = connection.execute(
        "SELECT p.namespace_key,e.namespace_key "
        "FROM policies p "
        "JOIN evidence_links l ON l.derived_type='policy' AND l.derived_id=p.id "
        "AND l.evidence_type='episode' "
        "JOIN episodes e ON e.id=l.evidence_id"
    ).fetchall()
    assert linked_namespaces
    assert all(policy_namespace == episode_namespace for policy_namespace, episode_namespace in linked_namespaces)


def test_retention_worker_purges_explicit_or_all_existing_namespaces(
    tmp_path,
) -> None:
    path = tmp_path / "retention-worker.db"
    worker = Worker(
        Settings(
            database_path=str(path),
            embedding_dim=2,
            retention_days=30,
        )
    )
    try:
        worker.connection.executemany(
            "INSERT INTO events("
            "id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at"
            ") VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "project-a-old",
                    "project-a",
                    "message",
                    "user",
                    "{}",
                    "2000-01-01T00:00:00Z",
                    "2000-01-01T00:00:00Z",
                ),
                (
                    "project-b-old",
                    "project-b",
                    "message",
                    "user",
                    "{}",
                    "2000-01-01T00:00:00Z",
                    "2000-01-01T00:00:00Z",
                ),
            ],
        )
        worker.connection.commit()

        assert dispatch_job(
            worker,
            {
                "job_type": "purge_retention",
                "payload_json": '{"namespace":"project-a"}',
            },
        ) == {"purged": 1}
        assert worker.connection.execute("SELECT tenant_id FROM events").fetchone()[0] == "project-b"

        assert dispatch_job(
            worker,
            {"job_type": "purge_retention", "payload_json": "{}"},
        ) == {"purged": 1}
        assert worker.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        worker.audit.close()
        worker.database.close()
