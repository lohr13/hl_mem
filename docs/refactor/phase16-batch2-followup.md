# Phase 16 Batch 2 遗留修复：事务嵌套 + 值变更 + reranker

## 根因分析

Batch 2 把 repository 的 `commit` 默认值改为 `False`，但没有同步修改所有需要自行提交的调用路径。导致：

### 问题 1：sqlite3.OperationalError: cannot start a transaction within a transaction（13 个测试）

**根因**：repository 不再自动提交后，部分调用路径在未提交的状态下尝试开新事务（`BEGIN IMMEDIATE`）。

**受影响文件**：
- `src/hl_mem/storage/jobs.py:28` — `insert_job()` 不提交后，调用方在事务中再开事务
- `src/hl_mem/storage/experience.py:151` — 同理
- `src/hl_mem/workers/decay.py:46` — decay 直接操作 connection 但 repo 层没提交

**修复方向**：
检查所有在 `BEGIN IMMEDIATE` 事务块外调用 repository 的路径——如果调用方没有管理事务，repository 方法需要保持自行提交（不应该所有 repo 方法都 commit=False）。

**正确的区分**：
- 在 application 的 `BEGIN IMMEDIATE` 事务块**内**调用的 repo 方法 → `commit=False`（不能自行提交，由 application 统一提交）
- 在事务块**外**独立调用的 repo 方法 → 需要自行提交（`commit=True`）

**建议修复**：不要全部改 `commit=False`。而是：
- `insert_claim`、`insert_event`、`insert_job`、`supersede_with_inline` 等被 `BEGIN IMMEDIATE` 包裹的方法：保留 `commit=False` 参数但不自动提交
- 被 worker/maintenance **独立调用**的方法（如 `lease_job`、`complete_job`、`fail_job`、decay 写入、episode/trace 写入）：**必须保持自行提交**

### 问题 2：assert 'entails' == 'state_change' / 'contradicts'（3 个测试）

**根因**：Batch 2 删除了 `value_json`/`qualifiers_json` 暴露后，冲突检测逻辑的输入变了。原来从 `value_json` 读 JSON 字符串，现在从 `value` 读 Python 值。但冲突检测可能还在读 `value_json` 字段。

**检查**：`domain/claims/conflicts.py` 和 `storage/claims.py` 中读取 claim value 的逻辑——是否还在用 `row["value_json"]` 而非 `row["value"]`？

### 问题 3：reranker 测试 '中文' not in '用户 偏好 用户 偏好'（1 个测试）

**根因**：`value_json` 改为 `value` 后，reranker 测试的 mock data 格式可能不匹配。检查 `test_reranker.py` 的 `_claims()` mock 数据。

### 问题 4：dedup test assert (None, 'new') == ('one', 'exact')

**根因**：同上，dedup 逻辑读 value 的路径变了。

### 问题 5：repository test assert True is False (event idempotent)

**根因**：event repository 不提交后，测试的 idempotent check 可能因为未提交而看不到数据。

## 修复要求

1. **不要全部 commit=False**——只在 `BEGIN IMMEDIATE` 事务块内调用的方法不提交；独立调用的方法必须保持自行提交
2. **检查所有读取 claim value 的路径**——确保统一用 `value`（Python str）而非 `value_json`（JSON 字符串）
3. **修复后确保 249 passed**

## 约束

- 可以修改 src/ 和 tests/
- 不要运行 pytest
- git add src/ tests/ && git commit
- 不要用 git add -A
