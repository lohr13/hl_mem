# Phase 15 Batch 1 遗留修复（2 个测试失败）

## 失败 1: test_reranker_on_without_key_falls_back_to_disabled

**文件**: tests/unit/test_reranker.py

**根因**: `allow_fake_fallback` 默认 False。测试没有设置该环境变量，导致 `make_reranker` 在缺 key 时抛 ConfigurationError 而非返回 None。

**修复**: 在测试中添加 `monkeypatch.setenv("HL_MEM_ALLOW_FAKE_FALLBACK", "true")`：

```python
def test_server_reranker_on_without_key_falls_back_to_disabled(monkeypatch) -> None:
    from hl_mem.components import make_reranker
    from hl_mem.settings import Settings

    monkeypatch.setenv("HL_MEM_ALLOW_FAKE_FALLBACK", "true")
    monkeypatch.setenv("HL_MEM_RERANKER", "on")
    monkeypatch.delenv("RERANKER_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    assert make_reranker(Settings.from_env()) is None
```

## 失败 2: test_worker_extractor_fail_fast_in_production

**文件**: tests/unit/test_comprehensive_fixes.py

**根因**: `components.make_extractor()` 在 `extractor_mode="fake"` 时直接返回 FakeExtractor，不检查 environment。但生产环境不允许 fake extractor。

**需要 src/ 修复**: `src/hl_mem/components.py` 的 `make_extractor` 函数，在 `extractor_mode == "fake"` 时增加生产环境检查：

```python
def make_extractor(settings: Settings, *, require_real: bool = False) -> Any:
    """依据统一配置创建 LLM 提取组件。"""
    if settings.extractor_mode == "fake" and not require_real:
        if settings.environment == "production":
            raise ConfigurationError(
                "HL_MEM_EXTRACTOR=fake is not allowed in production"
            )
        return FakeExtractor()
    ...
```

**同时修复测试**: 更新第二段的断言匹配：

```python
def test_worker_extractor_fail_fast_in_production() -> None:
    worker = Worker.__new__(Worker)
    worker.settings = Settings(environment="production", extractor_mode="real")
    worker.config = {}
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        worker._make_extractor()

    worker.settings = Settings(environment="production", extractor_mode="fake")
    with pytest.raises(ConfigurationError, match="HL_MEM_EXTRACTOR"):
        worker._make_extractor()
```

## 约束

1. 修改 src/hl_mem/components.py（make_extractor 加 production 检查）
2. 修改 tests/unit/test_reranker.py（加 env var）
3. 修改 tests/unit/test_comprehensive_fixes.py（改断言匹配）
4. 不要运行 pytest
5. 完成后运行：`git add src/ tests/ && git commit -m "fix: reject fake extractor in production and fix reranker test env"`
6. 不要用 git add -A
