import json

from hl_mem.experience.service import ExperienceService, backprop_episode_reward
from hl_mem.storage.database import Database


def test_schema_embeds_procedure_in_policy_without_procedures_table(tmp_path) -> None:
    connection = Database(tmp_path / "experience.db").open()

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {row[1] for row in connection.execute("PRAGMA table_info(policies)")}

    assert {"episodes", "traces", "policies", "retrieval_feedback"} <= tables
    assert "procedures" not in tables
    assert {"procedure", "procedure_status", "reliability"} <= columns


def test_policy_requires_independent_successes_then_retires_after_failures(tmp_path) -> None:
    connection = Database(tmp_path / "lifecycle.db").open()
    service = ExperienceService(connection, min_support=2, retire_after_failures=2)
    first = service.record_episode("e1", "发布服务", "success", 1.0, "2026-01-01T00:00:00Z")
    service.add_trace(first, "运行测试", "通过", None, 1.0)
    policy_id = service.induce_policy(
        "发布服务", {"steps": ["运行测试", "发布"]}, [first], "2026-01-01T01:00:00Z"
    )
    assert service.get_policy(policy_id)["status"] == "candidate"

    second = service.record_episode("e2", "发布服务", "success", 1.0, "2026-01-02T00:00:00Z")
    service.add_support(policy_id, second)
    policy = service.get_policy(policy_id)
    assert policy["status"] == "active"
    assert policy["procedure_status"] == "probationary"
    assert json.loads(policy["procedure"])["steps"] == ["运行测试", "发布"]

    service.record_policy_outcome(policy_id, True, "2026-01-03T00:00:00Z")
    assert service.get_policy(policy_id)["procedure_status"] == "active"
    service.record_policy_outcome(policy_id, False, "2026-01-04T00:00:00Z")
    service.record_policy_outcome(policy_id, False, "2026-01-05T00:00:00Z")
    retired = service.get_policy(policy_id)
    assert retired["status"] == "retired"
    assert retired["procedure_status"] == "retired"


def test_policy_steps_link_to_supporting_episodes(tmp_path) -> None:
    connection = Database(tmp_path / "evidence.db").open()
    service = ExperienceService(connection, min_support=2)
    episodes = [
        service.record_episode(f"e{i}", "修复故障", "success", 1.0, f"2026-01-0{i}T00:00:00Z")
        for i in (1, 2)
    ]
    policy_id = service.induce_policy("修复故障", {"steps": ["检查日志"]}, episodes, "2026-01-03T00:00:00Z")

    links = connection.execute(
        "SELECT evidence_id FROM evidence_links WHERE derived_type='policy' AND derived_id=?",
        (policy_id,),
    ).fetchall()
    assert {row[0] for row in links} == set(episodes)


def test_feedback_updates_episode_reward_and_is_idempotent(tmp_path) -> None:
    connection = Database(tmp_path / "feedback.db").open()
    service = ExperienceService(connection)
    service.record_episode("e1", "修复测试", "success", 0.0, "2026-01-01T00:00:00Z")

    assert service.record_feedback(
        "feedback-1", "query-1", "episode", "e1", True, True, 0.8, "2026-01-02T00:00:00Z"
    )
    assert not service.record_feedback(
        "feedback-1", "query-1", "episode", "e1", True, True, 0.8, "2026-01-02T00:00:00Z"
    )
    assert service.get_episode("e1")["reward"] == 0.8


def test_backprop_episode_reward_updates_trace_value_and_priority(tmp_path) -> None:
    connection = Database(tmp_path / "backprop.db").open()
    service = ExperienceService(connection)
    service.record_episode("good", "修复部署", "success", 0.0, "2026-01-01T00:00:00Z")
    service.add_trace("good", "测试", "通过", None, 0.2)

    backprop_episode_reward(connection, "good", 1.0)

    episode = service.get_episode("good")
    assert episode["reward"] == 1.0
    assert episode["traces"][0]["value"] == 1.0
    assert episode["traces"][0]["priority"] == 0.6

    backprop_episode_reward(connection, "good", 0.2)
    trace = service.get_episode("good")["traces"][0]
    assert trace["value"] == 0.2
    assert trace["priority"] == 0.5


def test_episode_returns_ordered_trace(tmp_path) -> None:
    connection = Database(tmp_path / "trace.db").open()
    service = ExperienceService(connection)
    service.record_episode("e1", "部署", "success", 1.0, "2026-01-01T00:00:00Z")
    service.add_trace("e1", "测试", "通过", None, 0.5)
    service.add_trace("e1", "部署", "完成", None, 1.0)

    assert [item["action"] for item in service.get_episode("e1")["traces"]] == ["测试", "部署"]
