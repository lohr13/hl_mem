from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import hl_mem.doctor as doctor_module
from hl_mem.doctor import CheckResult, CheckStatus, DaemonProbe, run_doctor
from hl_mem.storage.backup import backup_database
from hl_mem.storage.database import Database
from hl_mem.storage.tombstones import default_tombstone_ledger_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_doctor_with_recovery_evidence_is_locally_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
dim = 2048
api_mode = "compatible"
""",
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=test-llm\nEMBEDDING_API_KEY=test-embedding\n", encoding="utf-8")
    database_path = tmp_path / "memory.db"
    database = Database(database_path)
    connection = database.open()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    backup = tmp_path / "recovery.db"
    manifest = backup_database(database_path, backup)
    ledger = default_tombstone_ledger_path(database_path)

    monkeypatch.setattr(
        doctor_module,
        "probe_model_components",
        lambda _settings: [
            CheckResult(CheckStatus.OK, "LLM API", "verified"),
            CheckResult(CheckStatus.OK, "Embedding API", "verified"),
        ],
    )
    monkeypatch.setattr(doctor_module, "_probe_daemon", lambda _settings: DaemonProbe(None, "offline"))
    monkeypatch.setattr(
        doctor_module,
        "_check_port",
        lambda: CheckResult(CheckStatus.WARN, "服务端口", "not running"),
    )

    entries_before = {path.name for path in tmp_path.iterdir()}
    protected = (config, env, database_path, ledger, backup, manifest)
    hashes_before = {path: _sha256(path) for path in protected}
    sidecars_before = {suffix: Path(f"{database_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")}

    results = run_doctor(
        config_path=config,
        env_path=env,
        environ={},
        backup_path=backup,
        manifest_path=manifest,
    )

    assert next(item for item in results if item.code == "recovery").status is CheckStatus.OK
    assert {path.name for path in tmp_path.iterdir()} == entries_before
    assert {path: _sha256(path) for path in protected} == hashes_before
    assert {
        suffix: Path(f"{database_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    } == sidecars_before
