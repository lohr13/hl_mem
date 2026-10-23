from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _gate_module():
    try:
        return importlib.import_module("evaluation.tools.run_identity_gate")
    except ModuleNotFoundError:
        pytest.fail("evaluation.tools.run_identity_gate is not implemented")


def _expected_environment(config_path: Path) -> dict[str, str]:
    from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION

    root = Path(__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "HL_MEM_EVAL_EXPECTED_GIT_HEAD": head,
        "HL_MEM_EVAL_EXPECTED_REPO_ROOT": str(root),
        "HL_MEM_EVAL_EXPECTED_EXTRACTOR_VERSION": LLM_EXTRACTOR_VERSION,
        "HL_MEM_EVAL_EXPECTED_CONFIG_SHA256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "LLM_API_KEY": "sk-sp-unit-test-secret",
    }


def test_preflight_accepts_matching_runtime_identity_without_printing_the_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = _gate_module()
    config_path = tmp_path / "arm.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    environ = _expected_environment(config_path)

    result = gate.assert_run_identity_from_env(
        config_path=config_path,
        env_path=tmp_path / ".env",
        required=True,
        environ=environ,
    )

    output = capsys.readouterr().out
    assert result is not None
    assert result.expected_extractor_version == environ["HL_MEM_EVAL_EXPECTED_EXTRACTOR_VERSION"]
    assert result.checks == {
        "git_head": True,
        "hl_mem_path": True,
        "chinese_e2e_path": True,
        "extractor_version": True,
        "config_sha256": True,
        "llm_api_key_prefix": True,
    }
    assert '"llm_api_key_prefix": true' in output
    assert environ["LLM_API_KEY"] not in output


def test_preflight_does_not_import_chinese_e2e_or_replace_memdaily_reader(
    tmp_path: Path,
) -> None:
    gate = _gate_module()
    from evaluation.tools import run_memdaily_benchmark as runner

    module_name = "tests.eval.chinese_e2e"
    previous_reader = runner._qa_dashscope_chat
    previous_module = sys.modules.pop(module_name, None)
    try:
        original_reader = runner._qa_dashscope_chat
        config_path = tmp_path / "arm.toml"
        config_path.write_text("schema_version = 1\n", encoding="utf-8")

        gate.assert_run_identity_from_env(
            config_path=config_path,
            env_path=tmp_path / ".env",
            required=True,
            environ=_expected_environment(config_path),
        )

        assert module_name not in sys.modules
        assert runner._qa_dashscope_chat is original_reader
    finally:
        sys.modules.pop(module_name, None)
        if previous_module is not None:
            sys.modules[module_name] = previous_module
        runner._qa_dashscope_chat = previous_reader


def test_preflight_is_optional_only_when_no_expectation_is_supplied(tmp_path: Path) -> None:
    gate = _gate_module()
    config_path = tmp_path / "arm.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")

    assert (
        gate.assert_run_identity_from_env(
            config_path=config_path,
            env_path=tmp_path / ".env",
            required=False,
            environ={},
        )
        is None
    )

    with pytest.raises(gate.EvaluationIdentityError, match="missing required identity expectations"):
        gate.assert_run_identity_from_env(
            config_path=config_path,
            env_path=tmp_path / ".env",
            required=True,
            environ={},
        )

    with pytest.raises(gate.EvaluationIdentityError, match="missing required identity expectations"):
        gate.assert_run_identity_from_env(
            config_path=config_path,
            env_path=tmp_path / ".env",
            required=False,
            environ={"HL_MEM_EVAL_EXPECTED_GIT_HEAD": "0" * 40},
        )


def test_preflight_rejects_any_identity_mismatch_and_redacts_the_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = _gate_module()
    config_path = tmp_path / "arm.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    environ = _expected_environment(config_path)
    environ["HL_MEM_EVAL_EXPECTED_GIT_HEAD"] = "0" * 40
    environ["HL_MEM_EVAL_EXPECTED_CONFIG_SHA256"] = "f" * 64
    environ["LLM_API_KEY"] = "wrong-prefix-private-value"

    with pytest.raises(gate.EvaluationIdentityError) as captured:
        gate.assert_run_identity_from_env(
            config_path=config_path,
            env_path=tmp_path / ".env",
            required=True,
            environ=environ,
        )

    combined = captured.value.args[0] + capsys.readouterr().out
    assert "git_head=False" in combined
    assert "config_sha256=False" in combined
    assert "llm_api_key_prefix=False" in combined
    assert environ["LLM_API_KEY"] not in combined


def test_postflight_requires_exact_unique_manifest_count_and_versions(tmp_path: Path) -> None:
    gate = _gate_module()
    expected_version = "llm-v2+123456789abc"
    cases = []
    for index in range(16):
        manifest_path = tmp_path / f"case-{index}.manifest.json"
        manifest_path.write_text(
            json.dumps({"extractor_version": expected_version}),
            encoding="utf-8",
        )
        cases.append({"ingest": {"cache_manifest": str(manifest_path)}})
    report = {"status": "completed", "run": {}, "cases": cases + [cases[0]]}

    result = gate.assert_report_manifest_identity(
        report,
        expected_extractor_version=expected_version,
        expected_manifest_count=16,
    )

    assert result.manifest_count == 16
    assert result.matching_manifest_count == 16
    assert result.checks == {"manifest_count": True, "manifest_extractor_versions": True}

    Path(cases[-1]["ingest"]["cache_manifest"]).write_text(
        json.dumps({"extractor_version": "llm-v2+stale"}),
        encoding="utf-8",
    )
    with pytest.raises(gate.EvaluationIdentityError, match="manifest_extractor_versions=False"):
        gate.assert_report_manifest_identity(
            report,
            expected_extractor_version=expected_version,
            expected_manifest_count=16,
        )


def test_finalize_report_marks_the_whole_run_invalid_before_raising(tmp_path: Path) -> None:
    gate = _gate_module()
    report_path = tmp_path / "report.json"
    report = {
        "status": "completed",
        "run": {},
        "cases": [{"ingest": {"cache_manifest": str(tmp_path / "missing.manifest.json")}}],
    }

    with pytest.raises(gate.EvaluationIdentityError):
        gate.finalize_report_identity(
            report,
            report_path=report_path,
            expected_extractor_version="llm-v2+123456789abc",
            expected_manifest_count=1,
        )

    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == written["status"] == "invalid"
    assert report["run"]["identity_gate"]["valid"] is False
    assert written["run"]["identity_gate"]["checks"] == {
        "manifest_count": True,
        "manifest_extractor_versions": False,
    }


def test_finalize_report_persists_a_successful_whole_run_gate(tmp_path: Path) -> None:
    gate = _gate_module()
    expected_version = "llm-v2+123456789abc"
    manifest_path = tmp_path / "case.manifest.json"
    manifest_path.write_text(json.dumps({"extractor_version": expected_version}), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report = {
        "status": "completed",
        "run": {},
        "cases": [{"ingest": {"cache_manifest": str(manifest_path)}}],
    }

    result = gate.finalize_report_identity(
        report,
        report_path=report_path,
        expected_extractor_version=expected_version,
        expected_manifest_count=1,
    )

    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.manifest_count == result.matching_manifest_count == 1
    assert written["status"] == "completed"
    assert written["run"]["identity_gate"] == {
        "valid": True,
        "checks": {"manifest_count": True, "manifest_extractor_versions": True},
        "expected_manifest_count": 1,
        "manifest_count": 1,
        "matching_manifest_count": 1,
    }


def test_memdaily_main_requires_preflight_before_loading_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate_module()
    from evaluation.tools import run_memdaily_benchmark as runner

    source = tmp_path / "memdaily.json"
    source.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runner.os, "environ", {})

    with pytest.raises(gate.EvaluationIdentityError, match="missing required identity expectations"):
        runner.main(
            [
                "--source",
                str(source),
                "--config",
                str(tmp_path / "missing.toml"),
                "--env-file",
                str(tmp_path / ".env"),
            ]
        )


def test_memdaily_main_rejects_clean_before_identity_or_paid_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evaluation.tools import run_memdaily_benchmark as runner

    source = tmp_path / "memdaily.json"
    source.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runner.os, "environ", {})
    monkeypatch.setattr(
        runner,
        "assert_run_identity_from_env",
        lambda **_kwargs: pytest.fail("identity lookup reached before --clean rejection"),
    )

    with pytest.raises(ValueError, match="--clean.*postflight"):
        runner.main(["--source", str(source), "--clean"])


def test_chinese_e2e_entry_requires_preflight_before_the_paid_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate_module()
    from tests.eval import test_chinese_e2e as entry

    monkeypatch.setattr(
        entry,
        "load_sample_manifest",
        lambda _path: SimpleNamespace(sources={"private": {"path": str(tmp_path / "missing.json")}}),
    )
    monkeypatch.setattr(
        entry,
        "run_chinese_e2e",
        lambda **_kwargs: pytest.fail("paid runner reached before identity preflight"),
    )
    monkeypatch.setattr(entry.os, "environ", {})
    monkeypatch.setattr(entry, "DEFAULT_REPORT_PATH", tmp_path / "report.json")

    with pytest.raises(gate.EvaluationIdentityError, match="missing required identity expectations"):
        entry.test_chinese_extraction_recall_qa_e2e()
