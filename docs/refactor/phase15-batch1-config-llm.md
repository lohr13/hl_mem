# Phase 15 Batch 1: 配置统一 + LLM 迁移完成

## 概述

修复审查报告中的 P1-1（三套配置真相源）和 P1-4（LLM Provider 半迁移）。

---

## P1-1: 统一配置与工厂

### 问题
同一参数有三处管理：`Settings.from_env()` + `config.py` 模块常量 + 各工厂内的 `os.getenv()`。
reranker 构造逻辑在 `components.make_reranker()` 和 `api.server._make_reranker()` 中重复。
API 创建了 Settings 对象，但构造 embedder/reranker 时没传给工厂。

### 修复方案

1. **`Settings` 为唯一非敏感配置对象**：
   - 启动时只解析一次 `Settings.from_env()`
   - 显式注入 `make_embedder(settings)` / `make_reranker(settings)` / `make_extractor(settings)` / `Worker(settings)` / `McpMemoryServer(settings)`
   - 工厂函数签名改为接收 `Settings` 对象（或其中需要的子集），不再自己 `os.getenv()`

2. **`config.py` 只保留领域常量**：
   - 保留真正不随部署变化的常量（如 `CONFLICT_SLOT_ALIASES`、ranking 权重等算法常量）
   - 删除从环境变量读取的配置（embedding dim、model name、API key 等），这些应该从 Settings 来

3. **删除 `api.server._make_reranker()`**：统一调用 `components.make_reranker(settings)`

4. **Worker 也接收 Settings**：不再混用 config dict + 模块常量 + os.getenv()

### 涉及文件
- `src/hl_mem/settings.py` — 确保所有配置项都在这里
- `src/hl_mem/config.py` — 删除环境变量读取，只留算法常量
- `src/hl_mem/components.py` — 工厂函数接收 Settings，删除内部 os.getenv()
- `src/hl_mem/api/server.py` — 删除 `_make_reranker()`，传 Settings 给工厂
- `src/hl_mem/workers/worker.py` — 接收 Settings，不再自己读环境变量

---

## P1-4: 完成 LLM Provider 迁移

### 问题
`LLMClient → Provider` 只有 `LLMExtractor` 在用。
`LLMConflictJudge` 还在自行拼 HTTP、重试和解析。
`reclassify.classify_batch()` 调用 `extractor._post()`，但当前 `LLMExtractor` 已没有该方法（死代码引用！）。
`LLMExtractor` 构造器同时接收旧 transport 参数和可选 `llm_client`，产生双重状态。

### 修复方案

1. **`LLMConflictJudge` 消费 `LLMClient`**：
   - 删除自己拼 HTTP 的代码（httpx.post、retry 逻辑）
   - 用 `LLMClient.complete()` 或类似方法
   - 定义冲突归并的 `StructuredOutputSpec`（如果有结构化输出需求）

2. **修复 `reclassify.py`**：
   - 删除 `extractor._post()` 调用（方法已不存在）
   - 改为通过 `LLMClient` 调用
   - reclassify 需要自己的 `StructuredOutputSpec`

3. **收敛 `LLMExtractor.__init__`**：
   - 删除旧 transport 参数（base_url, api_key, model, timeout 等）
   - 构造器只接收 `llm_client: LLMClient` + `extraction_policy`
   - 如果需要向后兼容，保留一个 `@classmethod from_env()` legacy factory，但标记 deprecated

4. **确保所有 LLM 调用路径统一**：提取（LLMExtractor）、冲突归并（LLMConflictJudge）、重分类（reclassify）全部走 LLMClient

### 涉及文件
- `src/hl_mem/llm/client.py` — 可能需要扩展接口（如支持 conflict judge 的需求）
- `src/hl_mem/llm/providers.py` — 确保三个 provider 的能力声明完整
- `src/hl_mem/ingest/llm_extractor.py` — 收敛构造器，删除旧 transport
- `src/hl_mem/workers/consolidate.py` — LLMConflictJudge 改用 LLMClient
- `src/hl_mem/workers/reclassify.py` — 修复 _post() 引用，改用 LLMClient
- `src/hl_mem/components.py` — make_extractor 工厂适配新构造器

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**（测试断言更新由 Hermes 负责）
2. **不要运行 pytest**（Windows 管道兼容性问题，测试由外部执行）
3. **完成后运行**：`git add src/ && git commit -m "refactor(config+llm): unify settings source and complete LLM provider migration"`
4. **不要用 `git add -A`**（会误提交 db 备份等垃圾文件）
5. **保留向后兼容的 re-export**：如果移动了函数，在原位置保留 `from new_location import func` 的 re-export，避免循环导入
6. **版本号 bump**：将 `src/hl_mem/__init__.py` 的 `__version__` 和 `pyproject.toml` 的 version 从 `0.4.3` 改为 `0.5.0`（架构改进 minor+1, 且改动量大用 major）
