# Phase 15 Batch 2: 模块位置纠正 + 兼容层清理

## 概述

修复 P1-2（写入逻辑错放 recall/）和 P2-1（兼容 re-export 拖延收尾）。

---

## P1-2: 写入领域逻辑从 recall/ 移到 domain/

### 问题
`recall/attribute_map.py`、`recall/conflict.py`、`recall/dedup.py` 是写入路径的领域逻辑，却放在 `recall/` 包下。
更严重的是：migration（`backfill_conflict_key_v2.py`）直接导入这些活跃函数，破坏了 migration 的不可变性假设。

### 修复方案

1. **创建写入领域模块**：
   ```
   src/hl_mem/domain/claims/
       __init__.py
       attributes.py   ← 从 recall/attribute_map.py 迁入
       conflicts.py    ← 从 recall/conflict.py 迁入
       dedup.py        ← 从 recall/dedup.py 迁入
   ```

2. **原 `recall/` 位置保留 re-export**（带 DeprecationWarning）：
   ```python
   # recall/attribute_map.py
   import warnings
   warnings.warn("Moved to hl_mem.domain.claims.attributes", DeprecationWarning, stacklevel=2)
   from hl_mem.domain.claims.attributes import *  # noqa
   ```
   这样测试和 migration 不会立即崩溃。

3. **migration 内联快照**：
   - `storage/migrations/backfill_conflict_key_v2.py` 不再 import `recall.attribute_map`
   - 创建 `storage/migrations/snapshots/v006_snapshot.py`，内联当时的算法快照
   - migration 导入快照而非活跃函数

4. **更新 `recall/__init__.py`**：
   - 删除"已知边界债务"注释
   - 改为指向新位置的迁移说明

5. **更新 `application/ingest.py` 和 `application/recall.py`**：
   - 所有 `from hl_mem.recall.attribute_map import ...` 改为 `from hl_mem.domain.claims.attributes import ...`
   - 所有 `from hl_mem.recall.conflict import ...` 改为 `from hl_mem.domain.claims.conflicts import ...`
   - 所有 `from hl_mem.recall.dedup import ...` 改为 `from hl_mem.domain.claims.dedup import ...`

---

## P2-1: 清理兼容 re-export 和 no-op

### 问题
多个旧路径只做转发（`api/pipeline.py`、`recall/router.py`、`recall/policy.py`、`ingest/embeddings.py`）。
`api/pipeline._build_observation()` 是纯 no-op 只为兼容 monkeypatch。
生产代码 `api/server` 仍从兼容层导入 `new_id`。

### 修复方案

1. **让所有 `src/` 代码改用最终路径**：
   - `api/server.py` 不再从 `api/pipeline.py` 导入 `new_id`，改为从实际定义位置导入
   - 搜索所有从兼容层导入的代码，改为从最终位置导入

2. **删除 no-op 桩函数**：
   - `api/pipeline.py` 中的 `_build_observation` no-op 删除
   - 如果测试 monkeypatch 它，测试需要更新（但本次不改 tests/，测试断言更新由 Hermes 负责）

3. **兼容 re-export 保留但加 DeprecationWarning**：
   - `api/pipeline.py`、`recall/router.py`、`recall/policy.py`、`ingest/embeddings.py`
   - 每个 re-export 加 `warnings.warn(..., DeprecationWarning, stacklevel=2)`
   - 给出明确的迁移截止版本（如 v0.6.0）

4. **不要删除文件本身**（测试可能直接导入），只标记 deprecated

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(domain+compat): move write logic to domain/claims and tag compat shims deprecated"`
4. **不要用 `git add -A`**
5. **注意循环导入**：`domain/claims/` 是纯领域模块，不要导入 recall/ 或 application/
6. **migration 快照必须是自包含的**：不能依赖任何会随业务变化的函数
