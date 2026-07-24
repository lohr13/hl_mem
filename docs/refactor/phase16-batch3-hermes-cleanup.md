# Phase 16 Batch 3: Hermes 收敛 + 过期兼容层清理

## 目标

1. Hermes 同步/异步双 hook 契约收敛为一代
2. 所有已过期的兼容层实际清理
3. 熔断器线程安全
4. prefetch 缓存 TTL

---

## 1. Hermes 同步/异步双契约收敛

### 现状
`adapters/hermes/provider.py` 的 `sync_turn()` 根据输入类型在旧异步消息同步和新同步 hook 间动态分派。
`prefetch()` 根据 `session_id` 改变返回类型。

### 修复
- 确定当前 Hermes gateway 实际使用的是同步 hook（on_sync_turn / on_pre_compress 等），这是当前活跃的契约
- 删除旧异步消息同步的分派逻辑
- 如果旧异步接口仍有外部调用方（检查 Hermes 插件清单 plugin.yaml），保留方法名但明确委托到同步路径
- `prefetch()` 返回类型统一为同步结果，不再根据参数改变

### 涉及文件
- `src/hl_mem/adapters/hermes/provider.py`
- `src/hl_mem/adapters/hermes/prefetch.py`
- `src/hl_mem/adapters/hermes/http_client.py`

---

## 2. 熔断器线程安全

### 现状
`can_call()` 没有原子门，half-open 时多线程可同时通过。

### 修复
- 用 `threading.Lock` 保护熔断器状态转换
- half-open 时只允许一个探测调用（用 flag 或 lock 实现）
- 只有获得探测权的调用可以关闭或重新打开电路

### 涉及文件
- `src/hl_mem/adapters/hermes/provider.py` 或 `http_client.py`（取决于熔断器逻辑在哪）

---

## 3. Prefetch 缓存 TTL + session-end 失效

### 现状
PrefetchCache 全局只允许一条线程，缓存无 TTL、无 query/version key，旧文本会无限期按 session_id 返回。

### 修复
- 缓存条目增加 TTL（默认 300 秒）
- 缓存 key 改为 `(session_id, query_hash)` 而非只 `session_id`
- `on_session_end()` 清理该 session 的缓存条目

### 涉及文件
- `src/hl_mem/adapters/hermes/prefetch.py`
- `src/hl_mem/adapters/hermes/provider.py` — on_session_end 清理

---

## 4. 过期兼容层清理

### 4a. storage/repository.py re-export
- 搜索全仓 `from hl_mem.storage.repository import` 的引用
- 全部改为从具体模块导入（`storage.claims`、`storage.events` 等）
- 删除 `repository.py` 的 re-export 内容
- 保留文件但清空为 `# Migrated to storage.claims/events/evidence/jobs/experience` 注释

### 4b. DeprecationWarning 兼容层
以下文件写了 "will be removed in v0.6.0" 但当前版本已经是 0.6.0：
- `api/pipeline.py`
- `ingest/embeddings.py`（如果还存在）
- `recall/attribute_map.py`
- `recall/conflict.py`
- `recall/dedup.py`
- `recall/policy.py`
- `recall/router.py`

**搜索 tests/ 中对这些兼容层的 import**（不改 tests/，但需要知道哪些测试会断）。
删除这些兼容文件，如果测试 import 断裂由 Hermes 修复。

### 4c. LLMExtractor.from_env() 删除
- 删除 `from_env()` classmethod
- 搜索全仓引用确认没有生产调用

### 4d. Hermes 类名别名删除
- `HermesMemoryProvider = HLMemProvider` 等别名
- 确认插件清单 plugin.yaml 引用的是正式类名
- 删除别名

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(hermes+cleanup): converge dual contract, fix circuit breaker, clean expired compat layers"`
4. **不要用 `git add -A`**
5. **删除兼容文件前先搜索全仓引用**——包括 tests/ 中的引用（报告给 Hermes 但不改）
6. **版本号 bump**: 0.6.1 → 0.7.0
7. Hermes provider 对外接口（hook 签名）不变，只收敛内部实现
8. 如果旧异步 hook 确实还有外部调用方，保留方法签名但内部委托到同步路径
