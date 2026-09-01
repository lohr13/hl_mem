"""Operational report CLI contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hl_mem.cli import main
from hl_mem.observability.usage_types import default_usage_ledger_path
from hl_mem.storage.database import Database


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "hl_mem.toml"
    config.write_text(
        """schema_version = 1

[database]
path = "memory.db"

[llm]
provider = "openai_compatible"
base_url = "https://llm.example.test/v1"
model = "quality-llm"

[extraction]
mode = "llm"

[embedding]
mode = "real"
base_url = "https://embedding.example.test/v1"
model = "quality-embedding"
dim = 8
api_mode = "compatible"

[recall]
query_expansion_mode = "off"
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LLM_API_KEY=test-secret\nEMBEDDING_API_KEY=other-secret\n", encoding="utf-8")
    return config


def _database_fingerprint(tmp_path: Path) -> dict[str, tuple[int, str]]:
    return {
        path.name: (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(tmp_path.iterdir())
        if path.is_file() and path.suffix in {".db", ".toml"}
    }


def _seed_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "memory.db"
    database = Database(database_path)
    database.open()
    database.close()
    return database_path


def test_ops_report_json_is_versioned_and_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    _seed_database(tmp_path)
    before = _database_fingerprint(tmp_path)

    main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report", "--since", "24h", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["warnings"] == ["unknown_usage", "worker_unknown"]
    assert _database_fingerprint(tmp_path) == before


def test_ops_report_human_mode_uses_fixed_safe_sections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    _seed_database(tmp_path)

    main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report"])

    output = capsys.readouterr().out
    assert [line.rstrip(":") for line in output.splitlines() if line.endswith(":")] == [
        "Summary",
        "Providers",
        "Jobs",
        "Worker",
        "Storage",
        "Conflicts",
        "Warnings",
    ]
    assert "test-secret" not in output
    assert "quality-llm" not in output


def test_ops_report_rejects_invalid_window_with_argparse_exit_two(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(SystemExit) as captured:
        main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report", "--since", "31d"])

    assert captured.value.code == 2


def test_ops_report_fails_safely_for_corrupt_usage_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    database_path = _seed_database(tmp_path)
    ledger_path = default_usage_ledger_path(database_path)
    ledger_path.write_text("not a database", encoding="utf-8")

    with pytest.raises(SystemExit) as captured:
        main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report", "--json"])

    assert captured.value.code == 1
    assert capsys.readouterr().err.strip() == "ops report unavailable"


@pytest.mark.parametrize("database_kind", ("missing", "corrupt", "future_migration"))
def test_ops_report_fails_closed_without_leaking_or_rewriting_invalid_main_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    database_kind: str,
) -> None:
    config = _config(tmp_path)
    database_path = tmp_path / "memory.db"
    if database_kind == "corrupt":
        database_path.write_text("not a database", encoding="utf-8")
    elif database_kind == "future_migration":
        _seed_database(tmp_path)
        with sqlite3.connect(database_path) as connection:
            connection.execute("INSERT INTO schema_migrations(version) VALUES ('999_future_feature')")
    before = _database_fingerprint(tmp_path)

    with pytest.raises(SystemExit) as captured:
        main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report", "--json"])

    output = capsys.readouterr()
    assert captured.value.code == 1
    assert output.out == ""
    assert output.err.strip() == "ops report unavailable"
    assert str(database_path) not in output.err
    assert _database_fingerprint(tmp_path) == before


def test_ops_report_accepts_recognized_legacy_subject_migration_marker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    database_path = _seed_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version='038_data_subject_canonicalization_v2'")
        connection.execute("INSERT INTO schema_migrations(version) VALUES ('038_data_subject_canonicalization_v1')")

    main(["--config", str(config), "--env-file", str(tmp_path / ".env"), "ops", "report", "--json"])

    assert json.loads(capsys.readouterr().out)["schema_version"] == 1


def test_ops_report_schema_checker_validates_real_empty_and_seeded_reports() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_ops_report_schema.py"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
