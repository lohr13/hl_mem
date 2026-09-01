"""CLI contract for bounded Claim explanations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hl_mem.cli import main
from hl_mem.storage.database import Database

NOW = "2026-09-01T00:00:00+00:00"


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "memory.db"
    database = Database(database_path)
    connection = database.open()
    connection.execute(
        "INSERT INTO claims(id,namespace_key,value_json,recorded_from,status,source_authority,assertion_kind,scope) "
        "VALUES('claim-1','default','\"must-not-print\"',?,'active','medium','unknown','permanent')",
        (NOW,),
    )
    connection.commit()
    database.close()
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
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=private-key\nEMBEDDING_API_KEY=other-key\n", encoding="utf-8")
    return config, env_file


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_explain_claim_json_is_stable_and_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, env_file = _seed(tmp_path)
    database_path = tmp_path / "memory.db"
    before = _digest(database_path)

    main(["--config", str(config), "--env-file", str(env_file), "explain", "claim", "claim-1", "--json"])

    raw = capsys.readouterr().out.strip()
    payload = json.loads(raw)
    assert list(payload) == sorted(payload)
    assert payload["claim"]["id"] == "claim-1"
    assert payload["explanation_kind"] == "current_persisted_state"
    assert "must-not-print" not in raw
    assert "private-key" not in raw
    assert _digest(database_path) == before


def test_explain_claim_human_output_has_fixed_safe_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, env_file = _seed(tmp_path)

    main(["--config", str(config), "--env-file", str(env_file), "explain", "claim", "claim-1"])

    output = capsys.readouterr().out
    assert [line.rstrip(":") for line in output.splitlines() if line.endswith(":" )] == [
        "Claim",
        "Provenance",
        "Evidence",
        "Limitations",
    ]
    assert "current_persisted_state" in output
    assert "must-not-print" not in output


def test_explain_claim_missing_exits_one_without_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, env_file = _seed(tmp_path)

    with pytest.raises(SystemExit) as captured:
        main(["--config", str(config), "--env-file", str(env_file), "explain", "claim", "absent", "--json"])

    assert captured.value.code == 1
    assert capsys.readouterr().err.strip() == "claim explanation unavailable"
