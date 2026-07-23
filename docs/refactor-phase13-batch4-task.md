# Batch 4 — P2-10 覆盖率 + P2-11 HTTP复用 + P2-12 降级策略 + P2-13 domain纯化

## P2-10: 覆盖率工具

### `pyproject.toml` dev deps 加 pytest-cov:
```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
]

[tool.coverage.run]
source = ["src/hl_mem"]

[tool.coverage.report]
show_missing = true
```

### `tests/e2e_real.py` → 重命名为 `tests/test_e2e_real.py`
加 `@pytest.mark.real_api` 标记。

### 新建 `tests/conftest.py`:
```python
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "real_api: requires real API keys")

def pytest_collection_modifyitems(config, items):
    skip_real = pytest.mark.skip(reason="real_api tests skipped (set -m real_api to run)")
    for item in items:
        if "real_api" in item.keywords:
            item.add_marker(skip_real)
```

## P2-11: HTTP 连接复用

### `src/hl_mem/ingest/embeddings.py`
Embedder 类构造函数加可选 `client: httpx.Client | None = None`：
```python
class Embedder:
    def __init__(self, api_key, base_url, model, dim, connect_timeout=5, read_timeout=30, max_attempts=3, client=None):
        # ... 现有初始化 ...
        self._client = client  # 如果传入了共享 client，用它；否则每次 post 仍用 httpx.post
```
在 `_request()` 中，如果有 self._client，用 `self._client.post(...)` 代替 `httpx.post(...)`。

### `src/hl_mem/ingest/llm_extractor.py`
LLMExtractor 类构造函数加可选 `client: httpx.Client | None = None`，同理。

### `src/hl_mem/recall/reranker.py`
Reranker 类构造函数加可选 `client: httpx.Client | None = None`，同理。

### `src/hl_mem/workers/consolidate.py`
LLMConflictJudge 类构造函数加可选 `client: httpx.Client | None = None`，同理。

**注意**: 不改变现有降级语义。components.py 暂不创建共享 client（留作后续优化），只是给构造函数加能力。

## P2-12: real 模式静默降级

### `src/hl_mem/components.py`

当前 dev 环境下 `HL_MEM_EMBEDDER=real` 缺 key → 静默返回 FakeEmbedder。改为：
- 如果环境变量**显式设为** `real`/`on` 但缺 key → 抛 `ConfigurationError`
- 如果环境变量**未设置**（默认） → fake（开发友好）
- 新增 `HL_MEM_ALLOW_FAKE_FALLBACK=true` 环境变量：设为 true 时允许显式 real 缺 key 后降级到 fake

同样适用于 reranker 和 extractor。统一规则：
```python
explicit = os.getenv("HL_MEM_EMBEDDER") is not None  # 用户显式设了
allow_fallback = os.getenv("HL_MEM_ALLOW_FAKE_FALLBACK", "").lower() == "true"
if not api_key:
    if production:
        raise ConfigurationError("...")
    if explicit and not allow_fallback:
        raise ConfigurationError("HL_MEM_EMBEDDER=real but EMBEDDING_API_KEY is missing")
    return FakeEmbedder(dim)
```

## P2-13: domain 层纯化

### `src/hl_mem/domain/entity.py`

当前 `normalize_entity_id()` 直接读 `os.getenv("HL_MEM_ENTITY_ALIASES_PATH")` 和 `_load_aliases()` 直接打开文件。

改为：
1. `normalize_entity_id()` 接收可选 `aliases: dict[str, str] | None = None` 参数
2. 如果传入了 aliases，直接使用；如果没传，使用模块级默认 `DEFAULT_ENTITY_ALIASES`
3. `_load_aliases()` 保留但改为纯函数，不再读环境变量——由调用方传入路径
4. 新增模块级函数 `load_entity_aliases()` 供 settings/infrastructure 层调用：
```python
def load_entity_aliases(path=None):
    """供基础设施层调用：从路径加载 alias mapping。"""
    if path is None:
        path = os.getenv("HL_MEM_ENTITY_ALIASES_PATH")
    if path:
        return _load_aliases(path)
    return _normalize_default_aliases()
```
5. `normalize_entity_id()` 改为：
```python
_active_aliases: dict[str, str] | None = None

def set_active_aliases(aliases: dict[str, str]) -> None:
    """供启动时注入 alias mapping。"""
    global _active_aliases
    _active_aliases = aliases

def normalize_entity_id(subject, aliases=None):
    if subject is None:
        return "unknown"
    normalized = _normalize_text(subject, casefold=True)
    if not normalized:
        return "unknown"
    alias_map = aliases or _active_aliases or _normalize_default_aliases()
    return alias_map.get(normalized, normalized)

def _normalize_default_aliases():
    """从 DEFAULT_ENTITY_ALIASES 构建规范化 alias map。"""
    aliases = {}
    for alias, canonical in DEFAULT_ENTITY_ALIASES.items():
        aliases[_normalize_text(alias, casefold=True)] = _normalize_text(canonical, casefold=False)
    for canonical in tuple(aliases.values()):
        aliases.setdefault(_normalize_text(canonical, casefold=True), canonical)
    return aliases
```

### `src/hl_mem/components.py` 或 `src/hl_mem/settings.py`

在 `Settings.from_env()` 中加载 alias 并调用 `set_active_aliases()`：
```python
from hl_mem.domain.entity import load_entity_aliases, set_active_aliases

# 在 from_env() 或 create_app() 中：
aliases = load_entity_aliases()  # 读环境变量
set_active_aliases(aliases)
```

**关键**: 这样 `ClaimRepository.find_active_for_dedup()` 和 `IngestService.store_extracted()` 都通过同一个进程级 alias mapping 工作，不会出现写入侧和查询侧使用不同 alias 集合的问题。

## 约束
- 不要修改 tests/ 目录下的任何现有测试文件（可以新建 conftest.py、重命名 e2e_real.py）
- 不要运行 pytest
- 完成后运行 `git add src/ tests/ pyproject.toml` 和 `git commit`
- 版本号 0.3.4 → 0.3.5
