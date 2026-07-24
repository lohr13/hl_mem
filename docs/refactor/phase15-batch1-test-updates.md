# Phase 15 Batch 1 测试更新：适配新构造器和工厂签名

## 背景

Batch 1（P1-1 配置统一 + P1-4 LLM迁移完成）改变了以下接口签名，导致 15 个测试失败。
这些都是**预期行为变更**（测试断言的是旧签名），需要更新测试以匹配新接口。

## 失败清单与修复方案

### 1. test_llm_extractor.py（7 个失败）

**根因**：旧测试用 `LLMExtractor("key", "https://example.test", "model")` 构造，新签名是 `LLMExtractor(llm_client, chunking_policy, *, schema_retries=2, structured_mode=...)`.

**修复**：

创建一个测试用的 FakeLLMClient（同文件内定义）：
```python
from hl_mem.llm.types import LLMRequest, LLMResponse, LLMMessage
from hl_mem.ingest.chunking import ChunkingPolicy

class _FakeLLMClient:
    """测试用 LLMClient 替身，返回预设响应。"""
    class _Provider:
        name = "fake"
    provider = _Provider()
    model = "test-model"

    def __init__(self, response_content: str, usage_tokens: int = 12):
        self._content = response_content
        self._tokens = usage_tokens
        self.last_request = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(self._content, "stop", self._tokens)
```

然后逐个修复：

- **test_parses_fenced_json_and_normalizes_entity**:
  ```python
  client = _FakeLLMClient(raw)  # raw is the JSON response
  extractor = LLMExtractor(client, ChunkingPolicy(10000, 0, 2))
  claims = extractor.extract({"text": "数据库使用 PG"})
  ```

- **test_should_memorize_false_returns_no_claims**: 同上模式

- **test_occurred_at_is_injected_into_user_prompt**:
  ```python
  client = _FakeLLMClient('{"claims":[],"should_memorize":true}')
  extractor = LLMExtractor(client, ChunkingPolicy(10000, 0, 2))
  extractor.extract("明天交付", {"occurred_at": occurred_at})
  # 检查 client.last_request.messages[1].content 包含 occurred_at
  ```

- **test_normalizes_predicate_and_preserves_chinese_value**: 同上模式

- **test_invalid_json_is_rejected**:
  ```python
  client = _FakeLLMClient("not json")
  extractor = LLMExtractor(client, ChunkingPolicy(10000, 0, 2))
  with pytest.raises(ValueError, match="valid JSON"):
      extractor.extract("内容")
  ```

- **test_http_call_has_timeout_and_two_retries**: 这个测试验证的是旧 extractor 的 httpx 重试。
  新架构中重试在 LLMClient 层。可以改为验证 LLMClient 的 max_attempts 配置：
  ```python
  def test_llm_client_has_configured_retry():
      from hl_mem.llm.client import LLMClient
      from hl_mem.llm.providers import ZhipuProvider
      client = LLMClient("key", "https://example.test", "model",
                         provider=ZhipuProvider(), max_attempts=3)
      assert client.max_attempts == 3
  ```

- **test_timeout_reads_from_env**: 旧测试检查 `ext.timeout == 60.0`。
  新架构中超时在 Settings/LLMClient。改为：
  ```python
  def test_timeout_reads_from_env(monkeypatch):
      monkeypatch.setenv("LLM_TIMEOUT", "60")
      from hl_mem.settings import Settings
      s = Settings.from_env()
      assert s.llm_timeout == 60.0
  ```

- **test_claim_validates_canonical_attribute_against_predicate**: 使用 `LLMExtractor._claim()` 静态方法，这个方法仍然存在，不需要修改。如果通过了就不用改。

### 2. test_extraction_chunking.py（2 个失败）

**根因**：`LLMExtractor("key", "https://example.test", "model", llm_client=client, chunking_policy=...)` 同时传了旧位置参数和新关键字参数，位置参数与 `llm_client` 冲突。

**修复**：去掉旧位置参数：
```python
# 旧
extractor = LLMExtractor("key", "https://example.test", "model", llm_client=client, chunking_policy=ChunkingPolicy(...))
# 新
extractor = LLMExtractor(client, ChunkingPolicy(1_000, 0, 2))
```

两个测试（test_truncated_output_is_bisected 和 test_truncation_at_max_depth）都做同样修改。

### 3. test_reranker.py（2 个失败）

**根因**：`_make_reranker` 从 `api.server` 删除，统一到 `components.make_reranker(settings)`。

**修复**：

- **test_server_reranker_on_without_key_falls_back_to_disabled**:
  ```python
  from hl_mem.components import make_reranker
  from hl_mem.settings import Settings

  def test_reranker_on_without_key_falls_back_to_disabled(monkeypatch):
      monkeypatch.setenv("HL_MEM_RERANKER", "on")
      monkeypatch.delenv("RERANKER_API_KEY", raising=False)
      monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
      settings = Settings.from_env()
      assert make_reranker(settings) is None
  ```

- **test_server_reranker_initialization_failure_falls_back**:
  ```python
  def test_reranker_initialization_failure_falls_back(monkeypatch):
      import hl_mem.components as components
      monkeypatch.setenv("HL_MEM_RERANKER", "on")
      monkeypatch.setenv("RERANKER_API_KEY", "test-key")
      monkeypatch.setattr(components, "Reranker", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad")))
      settings = Settings.from_env()
      assert make_reranker(settings) is None
  ```

### 4. test_comprehensive_fixes.py（3 个失败）

**根因**：
- `server._make_embedder()` 和 `server._make_reranker()` 已删除
- `Worker.__new__(Worker)` 跳过 `__init__`，导致 `self.settings` 不存在

**修复**：

- **test_production_requires_real_embedder_and_reranker**:
  ```python
  from hl_mem.components import make_embedder, make_reranker
  from hl_mem.settings import Settings

  def test_production_requires_real_embedder_and_reranker(monkeypatch):
      monkeypatch.setenv("HL_MEM_ENV", "production")
      monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
      monkeypatch.delenv("RERANKER_API_KEY", raising=False)
      monkeypatch.delenv("HL_MEM_EMBEDDER", raising=False)
      monkeypatch.delenv("HL_MEM_RERANKER", raising=False)
      settings = Settings.from_env()
      with pytest.raises((RuntimeError, ConfigurationError), match="EMBEDDING_API_KEY"):
          make_embedder(settings)
      with pytest.raises((RuntimeError, ConfigurationError), match="RERANKER_API_KEY|EMBEDDING_API_KEY"):
          make_reranker(settings)
  ```
  注意：`ConfigurationError` 从 `hl_mem.errors` 导入。如果 `make_embedder` 抛的是 `ConfigurationError` 而非 `RuntimeError`，需要匹配正确的异常类型。

- **test_worker_extractor_fail_fast_in_production** 和 **test_worker_extractor_fake_allowed_in_dev**:
  Worker 现在通过 `self.settings` 构造 extractor。需要提供 settings：
  ```python
  def test_worker_extractor_fail_fast_in_production(monkeypatch):
      monkeypatch.setenv("HL_MEM_ENV", "production")
      monkeypatch.delenv("LLM_API_KEY", raising=False)
      settings = Settings.from_env()
      worker = Worker.__new__(Worker)
      worker.settings = settings
      worker.config = {"extractor_name": "real"}
      with pytest.raises((RuntimeError, ConfigurationError), match="LLM_API_KEY"):
          worker._make_extractor()
  ```
  （同理 dev 版本需要设置 `worker.settings = Settings.from_env()`）

### 5. test_reclassify.py（1 个失败）

**根因**：
- 旧测试用 `LLMExtractor("key", "http://example", "model")` 构造 extractor
- `reclassify_claims` 签名从 `(connection, extractor, batch_size)` 变为 `(connection, llm_client, batch_size)`
- monkeypatch 目标 `classify_batch` 的签名从 `(_extractor, claims)` 变为 `(llm_client, claims)`

**修复**：
```python
def test_reclassify_batches_updates_and_is_idempotent(tmp_path, monkeypatch):
    connection = Database(tmp_path / "reclass.db").open()
    for index in range(6):
        _claim(connection, str(index))

    # 创建一个假的 llm_client（不需要真实 API 调用，因为 classify_batch 被 mock 了）
    from hl_mem.llm.types import LLMResponse
    class FakeClient:
        model = "test"
    fake_client = FakeClient()

    calls = []
    def fake_batch(_client, claims):
        calls.append(len(claims))
        return [{"id": claim["id"], "scope": "temporal", "importance": 0.8} for claim in claims]

    monkeypatch.setattr("hl_mem.workers.reclassify.classify_batch", fake_batch)
    assert reclassify_claims(connection, fake_client, 5)["updated"] == 6
    assert calls == [5, 1]
    assert reclassify_claims(connection, fake_client, 5)["eligible"] == 0
```

---

## 约束

1. **只修改 tests/ 目录下的文件，不要修改 src/ 下的任何文件**
2. **不要运行 pytest**（由外部执行验证）
3. **完成后运行**：`git add tests/ && git commit -m "test: adapt to unified settings and LLM client migration"`
4. **不要用 `git add -A`**
5. **保持测试的验证意图不变**，只改构造方式和断言目标
6. **import 语句要正确**：`from hl_mem.settings import Settings`, `from hl_mem.components import make_embedder, make_reranker`, `from hl_mem.errors import ConfigurationError`, `from hl_mem.ingest.chunking import ChunkingPolicy`
7. **注意异常类型**：components.py 的工厂函数可能抛 `ConfigurationError` 而非 `RuntimeError`，检查源码确认后选择正确的匹配
8. **保留通过的测试不变**，只改失败的测试
