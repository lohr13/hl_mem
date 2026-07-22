# Phase 6：可维护性整理

## 背景

Codex 审查发现 P1-6（server.py 过载）、P1-8（repository.py 职责过重）、P2-1（接口 Any 泛用）、P2-7（docstring 不足）、P2-4（magic number 散落）。这是最后一个阶段，主要做代码整洁化。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

---

## 修改 1：拆分 server.py（344行 → 多个模块）

### 问题

server.py 混合了 DTO（Pydantic models）、工厂方法、所有路由端点、lifespan 管理。

### 修复

将 server.py 拆分为：

1. `api/schemas.py` — 所有 Pydantic 模型（EventInput, RecallInput, MemoryInput, EpisodeInput, TraceInput, EpisodeUpdate, FeedbackInput）
2. `api/routers.py` — 所有路由端点函数（从 create_app 内部提取为独立函数）
3. `server.py` — 只保留 `create_app()` + lifespan + `app = create_app()`

**注意**：FastAPI 的路由绑定需要 app 实例，所以不能完全拆成独立模块。可以改为：
- `api/schemas.py` — Pydantic 模型（可以干净拆出）
- `server.py` — 保留 create_app + 路由（路由绑定 app 实例），但用 schemas.py 替换内联的 Pydantic 模型

**最小改动方案**：只拆 schemas.py，server.py 其余保留。这样 server.py 从 ~344 行降到 ~260 行，Pydantic 模型集中管理。

---

## 修改 2：magic number 集中化

### 问题

散落的 threshold：
- `recall/dedup.py:11` — threshold=0.85
- `workers/consolidate.py` — 0.72~0.95 灰区
- `recall/recall_pipeline.py:70` — limit 默认值
- `workers/worker.py:79` — 600 秒 maintenance interval
- `workers/worker.py` — 5 分钟 job lease

### 修复

在 `src/hl_mem/config.py`（新建）中集中定义所有 magic number：

```python
"""集中化的配置常量。所有 magic number 都在此定义，可通过环境变量覆盖。"""
from __future__ import annotations
import os

# 去重 / 冲突阈值
DEDUP_SEMANTIC_THRESHOLD = float(os.getenv("HL_MEM_DEDUP_THRESHOLD", "0.85"))
CONSOLIDATE_GRAY_ZONE_MIN = float(os.getenv("HL_MEM_CONSOLIDATE_GRAY_MIN", "0.72"))
CONSOLIDATE_GRAY_ZONE_MAX = float(os.getenv("HL_MEM_CONSOLIDATE_GRAY_MAX", "0.95"))

# Worker 调度
WORKER_MAINTENANCE_INTERVAL = float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600"))
WORKER_JOB_LEASE_MINUTES = int(os.getenv("HL_MEM_WORKER_LEASE_MINUTES", "5"))
WORKER_POLL_INTERVAL = float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0"))

# 召回
RECALL_DEFAULT_LIMIT = int(os.getenv("HL_MEM_RECALL_DEFAULT_LIMIT", "20"))
RECALL_VECTOR_SCAN_LIMIT = int(os.getenv("HL_MEM_RECALL_VECTOR_SCAN_LIMIT", "200"))

# 数据保留
RETENTION_DAYS = int(os.getenv("HL_MEM_RETENTION_DAYS", "30"))
```

更新各模块从此处导入。

---

## 修改 3：核心模块 docstring 补充

### 问题

多个核心模块没有模块级 docstring，公开类/函数缺少说明。

### 修复

为以下模块补充中文模块级 docstring（不需要给每个私有函数写）：

- `application/ingest.py` — """记忆写入应用服务。处理事件接收、记忆保存、Claim 提取管线、去重和冲突检测。"""
- `application/recall.py` — """记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""
- `application/forget.py` — """记忆撤回应用服务。原子化撤回 Claim，清除向量，传播 stale 标记。"""
- `storage/repository.py` — """SQLite 数据访问层。提供 Claim、Event、Job、Evidence 的 CRUD 和查询操作。"""
- `lifecycle.py` — """领域状态机。定义 ClaimStatus 和 EpisodeStatus 枚举、合法转换矩阵和守卫函数。"""
- `components.py` — """统一组件工厂。集中管理 embedder、reranker、extractor 的创建逻辑和环境变量配置。"""
- `core/vector.py` — """纯向量数学函数。不依赖任何业务包。"""
- `domain/temporal.py` — """双时间可见性领域逻辑。纯函数，不依赖基础设施。"""

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. 完成后 `git add -A && git commit -m "refactor(maintenance): split schemas + centralize config + docstrings"`
