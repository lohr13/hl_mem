"""提取 benchmark 脚本的模型与凭据配置测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hl_mem.ingest.llm_extractor import PROMPT_HASH


def _load_benchmark_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_extraction.py"
    spec = importlib.util.spec_from_file_location("benchmark_extraction", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_api_keys_reads_zhipu_from_dotenv_and_dashscope_from_hermes(tmp_path, monkeypatch) -> None:
    benchmark = _load_benchmark_module()
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=sk-sp-test\nLLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(benchmark, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "HERMES_CONFIG_PATH", tmp_path / "missing-hermes-config.yaml")

    assert benchmark.load_api_keys() == {
        "zhipu": {
            "key": "sk-sp-test",
            "url": "https://coding.dashscope.aliyuncs.com/v1",
        },
        "dashscope": {"key": None, "url": None},
    }


def test_benchmark_model_matrix_routes_glm52_to_zhipu() -> None:
    benchmark = _load_benchmark_module()
    keys = {
        "zhipu": {"key": "zhipu-test", "url": "https://open.bigmodel.cn/api/paas/v4"},
        "dashscope": {
            "key": "sk-sp-test",
            "url": "https://coding.dashscope.aliyuncs.com/v1",
        },
    }

    configs = benchmark.get_model_configs(keys)

    assert benchmark.NUM_EVENTS == 50
    assert [config["model"] for config in configs] == [
        "glm-5.2",
        "glm-5",
        "glm-4.7",
        "qwen3.7-plus",
        "qwen3.6-plus",
    ]
    assert configs[0]["provider"] == "zhipu"
    assert configs[0]["api_key"] == keys["zhipu"]["key"]
    assert configs[0]["base_url"] == keys["zhipu"]["url"]
    assert all(config["provider"] == "dashscope" for config in configs[1:])
    assert all(config["api_key"] == keys["dashscope"]["key"] for config in configs[1:])
    assert all(config["base_url"] == keys["dashscope"]["url"] for config in configs[1:])
    assert all(config["enable_thinking"] is False for config in configs)


def test_make_extractor_disables_dashscope_thinking() -> None:
    benchmark = _load_benchmark_module()
    config = {
        "model": "glm-5.2",
        "provider": "dashscope",
        "api_key": "sk-sp-test",
        "base_url": "https://coding.dashscope.aliyuncs.com/v1",
        "enable_thinking": False,
    }

    extractor = benchmark.make_extractor(config)

    assert extractor.llm_client.provider.enable_thinking is False


def test_benchmark_manifest_records_prompt_hash(monkeypatch) -> None:
    benchmark = _load_benchmark_module()
    monkeypatch.setattr(benchmark, "git_commit_sha", lambda: "abc123")

    manifest = benchmark.build_manifest(
        "validation",
        [
            {
                "model": "glm-5.2",
                "provider": "zhipu",
                "base_url": "https://example.test/v1",
                "enable_thinking": False,
            }
        ],
        event_count=3,
        fingerprint="testset-hash",
    )

    assert manifest["prompt_hash"] == PROMPT_HASH


def test_resume_manifest_requires_matching_prompt_hash() -> None:
    benchmark = _load_benchmark_module()

    benchmark.validate_resume_manifest(
        {"testset_fingerprint": "testset-hash", "prompt_hash": PROMPT_HASH},
        "testset-hash",
    )
    with pytest.raises(RuntimeError, match="prompt 指纹"):
        benchmark.validate_resume_manifest(
            {"testset_fingerprint": "testset-hash"},
            "testset-hash",
        )
    with pytest.raises(RuntimeError, match="prompt 指纹"):
        benchmark.validate_resume_manifest(
            {"testset_fingerprint": "testset-hash", "prompt_hash": "000000000000"},
            "testset-hash",
        )


def test_full_unlock_requires_validation_for_current_prompt_hash(tmp_path, monkeypatch) -> None:
    benchmark = _load_benchmark_module()
    validation_dir = tmp_path / "validation"
    validation_dir.mkdir()
    (validation_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "actual_call_count": benchmark.VALIDATION_EVENTS * len(benchmark.MODELS),
                "error_count": 0,
                "prompt_hash": "000000000000",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "RUNS_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="prompt 指纹"):
        benchmark.assert_full_is_unlocked()
