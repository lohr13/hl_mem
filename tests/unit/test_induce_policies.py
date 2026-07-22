import json

from hl_mem.experience.service import ExperienceService
from hl_mem.storage.database import Database
from hl_mem.workers.induce_policies import enqueue_daily_policy_induction, induce_policies
from hl_mem.workers.worker import Worker


def test_induce_policies_clusters_recent_successes_by_task_and_tool_sequence(tmp_path) -> None:
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
    assert json.loads(policy["procedure"]) == {"steps": ["inspect_logs", "deploy"]}
    assert policy["support"] == 3


def test_daily_policy_induction_is_idempotent_and_worker_dispatches(tmp_path) -> None:
    path = tmp_path / "worker.db"
    connection = Database(path).open()

    assert enqueue_daily_policy_induction(connection, "2026-07-22T04:00:00+00:00", "03:30")
    assert not enqueue_daily_policy_induction(connection, "2026-07-22T05:00:00+00:00", "03:30")

    worker = Worker(path, {"embedding_dim": 2})
    assert worker._dispatch({"job_type": "induce_policies"}) == {"clusters": 0, "policies_induced": 0}
