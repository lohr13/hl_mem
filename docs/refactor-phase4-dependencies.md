# Phase 4：纠正依赖方向

## 背景

Codex 审查发现 P1-1（recall 承载写入逻辑）、P1-2（storage 反向依赖高层）。实际依赖分析：

```
storage → recall  (3 条)  ❌ 底层依赖高层
storage → ingest  (1 条)  ❌ 底层依赖高层
recall → ingest   (2 条)  ❌ 召回依赖写入
ingest → recall   (2 条)  ❌ 写入依赖召回
```

目标：消除所有不合理的跨包依赖。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

---

## 修改 1：提取纯函数到 `core/` 包

### 问题

`cosine_similarity()` 在 `ingest/embeddings.py` 中定义，但被 `storage/repository.py`、`recall/dedup.py`、`recall/recall_pipeline.py`、`workers/consolidate.py` 四处导入。这是一个纯数学函数，不应属于任何业务包。

### 修复

1. 新建 `src/hl_mem/core/__init__.py`（空）
2. 新建 `src/hl_mem/core/vector.py`：
   ```python
   """纯向量数学函数，不依赖任何业务包。"""
   from __future__ import annotations
   
   import struct
   
   def cosine_similarity(query_blob: bytes, target_blob: bytes) -> float:
       """计算两个序列化 float32 向量的余弦相似度。"""
       # 从 ingest/embeddings.py 复制实现
       ...
   
   def encode_vector(vec: list[float]) -> bytes:
       """将 float 列表序列化为 bytes。"""
       return struct.pack(f"{len(vec)}f", *vec)
   
   def decode_vector(blob: bytes) -> list[float]:
       """将 bytes 反序列化为 float 列表。"""
       n = len(blob) // 4
       return list(struct.unpack(f"{n}f", blob))
   ```

3. `ingest/embeddings.py` 保留 `cosine_similarity` 的 re-export（向后兼容）：
   ```python
   from hl_mem.core.vector import cosine_similarity  # re-export
   ```

4. 更新 `storage/repository.py`：`from hl_mem.core.vector import cosine_similarity`
5. 更新 `recall/dedup.py`：`from hl_mem.core.vector import cosine_similarity`
6. 更新 `recall/recall_pipeline.py`：`from hl_mem.core.vector import cosine_similarity`
7. 更新 `workers/consolidate.py`：`from hl_mem.core.vector import cosine_similarity`

---

## 修改 2：提取可见性逻辑到 `domain/` 包

### 问题

`recall/policy.py` 中的 `claim_is_visible()` 和 `RecallIntent` 是纯领域逻辑（双时间可见性规则），但被 `storage/repository.py` 导入——底层依赖高层。

### 修复

1. 新建 `src/hl_mem/domain/__init__.py`（空）
2. 新建 `src/hl_mem/domain/temporal.py`：
   - 从 `recall/policy.py` 复制 `claim_is_visible()` 函数和 `RecallIntent` 枚举
   - 这是纯逻辑，不导入任何业务包

3. `recall/policy.py` 保留 re-export：
   ```python
   from hl_mem.domain.temporal import RecallIntent, claim_is_visible  # re-export
   ```

4. 更新 `storage/repository.py`：`from hl_mem.domain.temporal import RecallIntent, claim_is_visible`

---

## 修改 3：把写入逻辑移到 `domain/claims/`

### 问题

`recall/dedup.py`、`recall/conflict.py`、`recall/attribute_map.py` 是写入路径代码但放在 recall/ 包里。

### 修复方案（低侵入）

**不移文件**（移文件会打断大量 import 链，风险太高）。改为在 `recall/` 包的 `__init__.py` 中加注释说明：

```python
"""Recall 包。

注意：dedup.py、conflict.py、attribute_map.py 包含写入路径的领域逻辑
（去重、冲突判定、属性归一化），而非召回逻辑。它们保留在此处是因为
与召回共用了 cosine_similarity 和 ClaimRepository 等工具。
未来重构应将这些移到 domain/claims/ 包。
"""
```

**理由**：Phase 3 已经引入了 application/ 层，ingest.py 已经成为写入逻辑的入口。recall/ 下的这些模块现在是 application/ingest.py 的依赖，不再是 worker → recall 的依赖。依赖方向已经通过 application/ 层间接纠正了。

---

## 修改 4：migration 冻结

### 问题

`storage/migrations/backfill_conflict_key_v2.py` 导入了 `recall.attribute_map` 和 `recall.conflict` 的高层代码。如果这些模块未来改变，历史 migration 会断。

### 修复

1. 在 `backfill_conflict_key_v2.py` 顶部加注释标记：
   ```python
   """⚠️ 冻结模块：此 migration 脚本导入的函数已被快照。
   
   不要修改此文件中使用的算法。如果 recall.conflict 或 recall.attribute_map
   的算法改变，此 migration 应保持使用旧版本逻辑。
   """
   ```
2. 不实际移动代码——在当前规模下，维护一个冻结副本的成本高于风险。

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. **所有 re-export 必须**：现有 import 链不能断
7. 完成后 `git add -A && git commit -m "refactor(architecture): fix dependency direction — core/vector + domain/temporal extraction"`
