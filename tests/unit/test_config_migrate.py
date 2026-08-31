from __future__ import annotations

import json
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.config.migrate import apply_config_migration, plan_config_migration
from hl_mem.storage.backup import backup_database
from hl_mem.storage.database import Database

FIXTURES = Path(__file__).parents[1] / "fixtures" / "config"
SECRETS = {"LLM_API_KEY": "llm-secret", "EMBEDDING_API_KEY": "embedding-secret"}


def _legacy(tmp_path: Path) -> Path:
    path = tmp_path / "hl_mem.toml"
    path.write_bytes((FIXTURES / "v0361-online.toml").read_bytes())
    return path


def test_plan_is_deterministic_complete_and_read_only(tmp_path: Path) -> None:
    source = _legacy(tmp_path)
    original = source.read_bytes()

    first = plan_config_migration(source, environ=SECRETS)
    second = plan_config_migration(source, environ=SECRETS)

    assert first == second
    assert first.document == (FIXTURES / "v1-online.toml").read_text(encoding="utf-8")
    assert source.read_bytes() == original
    assert first.blockers == ()
    assert first.recovery_required is False
    assert {change.path for change in first.changes} >= {
        "schema_version",
        "recall.query_expansion_mode",
        "recall.resurrection_mode",
        "relation.discovery_mode",
        "dedup.llm_enabled",
        "worker.semantic_conflict_consolidation_enabled",
        "worker.policy_induction_enabled",
        "worker.reclassify_enabled",
        "plugins.enabled",
    }
    assert set(first.removed) == {
        "extraction.pre_filter",
        "recall.tag_channel_enabled",
        "recall.tag_channel_weight",
        "recall.tag_candidate_limit",
        "relation.auto_apply_confidence",
        "relation.conflict_confidence",
    }


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ('[extraction]\nmode = "fake"\n', "extraction.mode"),
        ('[embedding]\nmode = "fake"\n', "embedding.mode"),
        ("[mystery]\nenabled = true\n", "unknown TOML table"),
    ),
)
def test_plan_reports_actionable_blockers(tmp_path: Path, body: str, message: str) -> None:
    source = tmp_path / "legacy.toml"
    source.write_text(body, encoding="utf-8")

    plan = plan_config_migration(source, environ={})

    assert any(message in blocker for blocker in plan.blockers)
    with pytest.raises(ValueError, match="blocked"):
        apply_config_migration(plan, environ={})


def test_v1_plan_is_noop_and_apply_refuses_rewrite(tmp_path: Path) -> None:
    source = tmp_path / "hl_mem.toml"
    source.write_bytes((FIXTURES / "v1-online.toml").read_bytes())

    plan = plan_config_migration(source, environ=SECRETS)

    assert plan.no_op is True
    with pytest.raises(ValueError, match="already uses schema_version 1"):
        apply_config_migration(plan, environ=SECRETS)


def test_apply_preserves_exact_source_and_refuses_stale_plan(tmp_path: Path) -> None:
    source = _legacy(tmp_path)
    original = source.read_bytes()
    plan = plan_config_migration(source, environ=SECRETS)
    source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed since migration was planned"):
        apply_config_migration(plan, environ=SECRETS)
    assert not source.with_name("hl_mem.toml.v0.bak").exists()

    source.write_bytes(original)
    plan = plan_config_migration(source, environ=SECRETS)
    backup = apply_config_migration(plan, environ=SECRETS)
    assert backup.read_bytes() == original
    assert source.read_text(encoding="utf-8") == plan.document


def test_existing_database_requires_matching_recovery_set(tmp_path: Path) -> None:
    source = _legacy(tmp_path)
    live = tmp_path / "memory.db"
    database = Database(live)
    database.open()
    database.close()
    plan = plan_config_migration(source, environ=SECRETS)
    assert plan.recovery_required is True

    with pytest.raises(ValueError, match="backup and manifest"):
        apply_config_migration(plan, environ=SECRETS)

    backup = tmp_path / "recovery.db"
    manifest = backup_database(live, backup)
    apply_config_migration(
        plan,
        backup_path=backup,
        manifest_path=manifest,
        environ=SECRETS,
    )


def test_recovery_set_must_match_live_database_identity(tmp_path: Path) -> None:
    source = _legacy(tmp_path)
    live = tmp_path / "memory.db"
    other = tmp_path / "other.db"
    for path in (live, other):
        database = Database(path)
        database.open()
        database.close()
    backup = tmp_path / "other-backup.db"
    manifest = backup_database(other, backup)

    plan = plan_config_migration(source, environ=SECRETS)
    with pytest.raises(ValueError, match="live database"):
        apply_config_migration(
            plan,
            backup_path=backup,
            manifest_path=manifest,
            environ=SECRETS,
        )


def test_cli_dry_run_is_redacted_json_and_does_not_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _legacy(tmp_path)
    original = source.read_bytes()
    monkeypatch.setenv("LLM_API_KEY", "never-print-llm")
    monkeypatch.setenv("EMBEDDING_API_KEY", "never-print-embedding")

    cli_module.main(["config", "migrate", "--config", str(source)])

    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["status"] == "ready"
    assert report["dry_run"] is True
    assert "document" not in report
    assert "never-print" not in raw
    assert source.read_bytes() == original
