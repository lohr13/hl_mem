import json

from hl_mem.storage.database import Database
from scripts.reclassify_predicates import reclassify_unknown_attributes


def _insert_unknown(connection, claim_id: str, predicate: str, value: str) -> None:
    connection.execute(
        "INSERT INTO claims(id,subject_entity_id,predicate,value_json,qualifiers_json,"
        "recorded_from,status,canonical_attribute) VALUES(?,?,?,?,?,?,?,?)",
        (
            claim_id,
            "hl_mem",
            predicate,
            json.dumps(value, ensure_ascii=False),
            "{}",
            "2026-08-04T00:00:00+00:00",
            "active",
            "custom.unknown",
        ),
    )
    connection.commit()


def test_custom_unknown_reclassification_supports_dry_run_and_apply(tmp_path) -> None:
    connection = Database(tmp_path / "reclassify.db").open()
    _insert_unknown(connection, "architecture", "架构", "hl_mem 采用事件溯源双通道架构")
    _insert_unknown(connection, "dependency", "依赖", "hl_mem 依赖 numpy>=2.0")
    _insert_unknown(connection, "version", "版本", "hl_mem 当前版本为 v0.21.0")
    _insert_unknown(connection, "behavior", "行为", "发送 /restart 可以重启 Hermes gateway")
    _insert_unknown(connection, "unmatched", "其他", "无法确定分类的文本")

    dry_run = reclassify_unknown_attributes(connection, dry_run=True)

    assert dry_run == {"eligible": 5, "updated": 0, "proposed": 4, "skipped": 1}
    assert (
        connection.execute("SELECT count(*) FROM claims WHERE canonical_attribute='custom.unknown'").fetchone()[0] == 5
    )

    applied = reclassify_unknown_attributes(connection, dry_run=False)

    assert applied == {"eligible": 5, "updated": 4, "proposed": 4, "skipped": 1}
    rows = {
        claim_id: (predicate, canonical_attribute)
        for claim_id, predicate, canonical_attribute in connection.execute(
            "SELECT id,predicate,canonical_attribute FROM claims"
        )
    }
    assert rows == {
        "architecture": ("架构", "fact.architecture"),
        "behavior": ("行为", "fact.capability"),
        "dependency": ("依赖", "fact.dependency"),
        "unmatched": ("其他", "custom.unknown"),
        "version": ("版本", "config.version"),
    }


def test_custom_unknown_reclassification_rolls_back_as_one_transaction(tmp_path) -> None:
    connection = Database(tmp_path / "rollback.db").open()
    _insert_unknown(connection, "architecture", "架构", "hl_mem 采用事件溯源架构")
    _insert_unknown(connection, "version", "版本", "hl_mem 当前版本为 v0.21.0")
    connection.execute(
        "CREATE TRIGGER reject_version_reclassification "
        "BEFORE UPDATE OF canonical_attribute ON claims "
        "WHEN NEW.id='version' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )

    try:
        reclassify_unknown_attributes(connection, dry_run=False)
    except Exception as error:
        assert "blocked" in str(error)
    else:
        raise AssertionError("expected reclassification to fail")

    assert (
        connection.execute("SELECT count(*) FROM claims WHERE canonical_attribute='custom.unknown'").fetchone()[0] == 2
    )
