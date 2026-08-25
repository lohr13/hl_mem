from pathlib import Path

from hl_mem.storage.database import Database


def test_plan_fulfillment_migration_is_additive_and_indexed(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh-053.db").open()

    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='053_plan_fulfillment'").fetchone()
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(plan_outcomes)")}
    assert columns == {
        "id",
        "namespace_key",
        "plan_claim_id",
        "result_claim_id",
        "outcome_type",
        "coordinate_hash",
        "matched_quantity_text",
        "unit",
        "cumulative_quantity_text",
        "match_rule",
        "match_confidence",
        "input_fingerprint",
        "policy_version",
        "status",
        "relation_id",
        "created_at",
        "applied_at",
    }
    indexes = {row["name"] for row in connection.execute("PRAGMA index_list(plan_outcomes)")}
    assert {"idx_plan_outcomes_result", "idx_plan_outcomes_plan_status"} <= indexes
    foreign_tables = {row["table"] for row in connection.execute("PRAGMA foreign_key_list(plan_outcomes)")}
    assert foreign_tables == {"claims", "memory_relations"}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_plan_outcome_unique_key_and_status_checks_are_declared(tmp_path: Path) -> None:
    connection = Database(tmp_path / "checks-053.db").open()
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='plan_outcomes'").fetchone()[0]

    assert "UNIQUE (plan_claim_id, result_claim_id, outcome_type, policy_version)" in sql
    assert "complete','cancel','replace','partial" in sql
    assert "candidate','observed','applied','ambiguous','rejected','rolled_back" in sql
