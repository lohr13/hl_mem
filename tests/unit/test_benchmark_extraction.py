"""提取 benchmark 脚本的模型与凭据配置测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


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
        "LLM_API_KEY=sk-zhipu-test\nLLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4\n",
        encoding="utf-8",
    )
    hermes_path = tmp_path / "config.yaml"
    hermes_path.write_text(
        "providers:\n"
        "  dashscope:\n"
        "    api_key: sk-dashscope-test\n"
        "    base_url: https://coding.dashscope.aliyuncs.com/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "HERMES_CONFIG_PATH", hermes_path)

    assert benchmark.load_api_keys() == {
        "zhipu": {
            "key": "sk-zhipu-test",
            "url": "https://open.bigmodel.cn/api/coding/paas/v4",
        },
        "dashscope": {
            "key": "sk-dashscope-test",
            "url": "https://coding.dashscope.aliyuncs.com/v1",
        },
    }


def test_benchmark_models_use_provider_specific_credentials() -> None:
    benchmark = _load_benchmark_module()
    keys = {
        "zhipu": {"key": "sk-zhipu-test", "url": "https://open.bigmodel.cn/api/coding/paas/v4"},
        "dashscope": {"key": "sk-dashscope-test", "url": "https://coding.dashscope.aliyuncs.com/v1"},
    }

    configs = benchmark.get_model_configs(keys)

    assert benchmark.NUM_EVENTS == 20
    assert [config["model"] for config in configs] == [
        "glm-5.2",
        "glm-5",
        "glm-4.7",
        "qwen3.7-plus",
        "qwen3.6-plus",
    ]
    assert [config["provider"] for config in configs] == ["zhipu", "zhipu", "zhipu", "dashscope", "dashscope"]
    assert all(config["api_key"] == keys[config["provider"]]["key"] for config in configs)
    assert all(config["base_url"] == keys[config["provider"]]["url"] for config in configs)


def test_make_extractor_disables_dashscope_thinking() -> None:
    benchmark = _load_benchmark_module()
    config = {
        "model": "glm-5.2",
        "provider": "dashscope",
        "api_key": "sk-sp-test",
        "base_url": "https://coding.dashscope.aliyuncs.com/v1",
    }

    extractor = benchmark.make_extractor(config)

    assert extractor.llm_client.provider.enable_thinking is False


def test_make_extractor_uses_plain_zhipu_provider() -> None:
    benchmark = _load_benchmark_module()
    config = {
        "model": "glm-5.2",
        "provider": "zhipu",
        "api_key": "sk-zhipu-test",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    }

    extractor = benchmark.make_extractor(config)

    assert extractor.llm_client.provider.name == "zhipu"
    assert not hasattr(extractor.llm_client.provider, "enable_thinking")
