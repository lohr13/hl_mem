# Phase 16 Batch 2: 统一契约（事务/monkeypatch/数据形态）

## 目标

消除同一概念的多份实现和绕过边界的拼接代码。

---

## 1. 事务所有权统一

### 现状
四种事务所有权并存：
- repository 默认自动 commit
- application 传 `commit=False`
- `domain/relations.py` 自己 commit
- worker 直接写 SQL

### 修复目标
**统一规则：repository 不提交，application/worker 拥有事务。**

### 修复方案

1. **repository 层**：所有 `commit=True` 默认值改为 `commit=False`
   - `storage/claims.py`、`storage/events.py`、`storage/evidence.py`、`storage/jobs.py`、`storage/experience.py`
   - 调用方需要显式传 `commit=True` 或在事务内调用

2. **application 层**：已有 `commit=False` 传参的地方不变（因为 repo 默认也不提交了）
   - 但需要确保 application 在完成操作后显式 commit
   - `application/ingest.py` 的 `BEGIN IMMEDIATE` 事务块内的调用不传 commit=True（在事务内）

3. **domain/relations.py**：删除 `connection.commit()`，让调用方管理事务

4. **worker 直接 SQL**：找到 worker.py 中直接 `connection.execute()` + `connection.commit()` 的地方，改为调用 repository 方法

### 涉及文件
- `src/hl_mem/storage/claims.py` — commit 默认值改 False
- `src/hl_mem/storage/events.py` — 同上
- `src/hl_mem/storage/evidence.py` — 同上
- `src/hl_mem/storage/jobs.py` — 同上
- `src/hl_mem/storage/experience.py` — 同上
- `src/hl_mem/domain/relations.py` — 删除 commit
- `src/hl_mem/workers/worker.py` — 裸 SQL 改为 repo 调用
- `src/hl_mem/workers/reclassify.py` — 如果有裸 SQL 也改
- **需要检查所有调用 repository 方法的地方**，确保 application 层在需要时显式 commit

---

## 2. API monkeypatch 删除

### 现状
`api/server.py` 构造 `IngestService` 后用 lambda 覆盖 `_queue_event` 私有方法。

### 修复
- 删除 `api/server.py` 中的 `_queue_event` monkeypatch
- 让 `IngestService` 唯一拥有入队行为
- 如果 `api.server._queue_event()` 函数还有其他用途，内联到 `IngestService` 中或删除

### 涉及文件
- `src/hl_mem/api/server.py`
- `src/hl_mem/application/ingest.py` — 确保 `_queue_event` 是公开方法或由服务唯一拥有

---

## 3. 内部数据形态：消除 value/value_json 双轨

### 现状
Claim 在内部代码中同时暴露 `value` / `value_json`、`qualifiers` / `qualifiers_json` 两套字段。
生产主链同时接受 dict 和 dataclass 风格访问。
Batch 4 定义的 `StoredClaim` 等 dataclass 从未被实例化。

### 修复方案

**渐进式收敛（不一次性大重写）：**

1. **Repository 返回层**：`ClaimRepository` 的查询方法返回的 dict 中，只暴露 Python 值（`value` 是 Python str，`qualifiers` 是 dict），不再暴露 `value_json` / `qualifiers_json`
   - JSON 编解码只在 repository 内部（从 DB 读取时 json.loads，写入时 json.dumps）
   - 调用方拿到的永远是 Python 值

2. **写入层**：`insert_claim()` 接收的 dict 中 `value` 是 Python str，内部做 `json.dumps` 存入 DB

3. **调用方**：搜索全仓 `value_json` / `qualifiers_json` 的引用，改为直接使用 `value` / `qualifiers`

4. **domain/types.py 的 dataclass**：如果 `StoredClaim` 等当前未被使用，删除定义（不要保留未使用的类型定义）。如果 repository 返回的 dict 结构已经稳定，不需要 dataclass 包装。

### 涉及文件
- `src/hl_mem/storage/claims.py` — JSON 编解码集中到内部
- `src/hl_mem/application/ingest.py` — 改用 Python 值
- `src/hl_mem/application/recall.py` — 改用 Python 值
- `src/hl_mem/domain/types.py` — 删除未使用的 dataclass
- 搜索全仓 `value_json` / `qualifiers_json` 引用

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(contract): unify transaction ownership, remove monkeypatch, collapse json dual-track"`
4. **不要用 `git add -A`**
5. **事务改动风险较高**——确保 `BEGIN IMMEDIATE` 块内的所有 repo 调用不自行 commit
6. 如果 repository 默认 commit=False 导致 application 层遗漏 commit，测试会暴露（但本次不改 tests/，测试断言更新由 Hermes 负责）
