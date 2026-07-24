# Phase 15 Batch 4: 内部类型收紧 + 存储拆分 + Protocol 修正

## 概述

修复 P1-5（内部 dict/Any 贯穿）、P1-6（repository.py 黑洞）、P2-3（Protocol 不一致）。

---

## P1-5: 增加稳定内部类型

### 问题
Pydantic 只保护了 API 入口，内部全是 `dict[str, Any]` 和 JSON 字符串穿透各层。
字段改名无法被类型检查发现。

### 修复方案

**不把 ORM/Pydantic 推入所有层，只增加少量稳定 dataclass/TypedDict：**

```python
# domain/types.py
from dataclasses import dataclass
from typing import TypedDict

@dataclass
class StoredEvent:
    id: str
    source: str
    content: str
    metadata: dict
    created_at: str

@dataclass
class ClaimDraft:
    predicate: str
    canonical_attribute: str
    value: str
    scope: str
    importance: float
    qualifiers: dict
    evidence: dict | None

@dataclass
class StoredClaim:
    id: str
    entity_id: str
    predicate: str
    canonical_attribute: str
    value: str
    scope: str
    importance: float
    status: str
    qualifiers: dict
    created_at: str
    valid_from: str
    valid_until: str | None

@dataclass
class RecallResult:
    claim: StoredClaim
    score: float
    source: str  # "fts" | "vector" | "related"

@dataclass
class FeedbackRecord:
    claim_id: str
    feedback_type: str
    weight: float
    created_at: str
```

**JSON 编解码集中到 Repository 边界：**
- Repository 返回 dataclass（内部用 dict→dataclass 转换）
- Application/Domain 层操作 Python 值，不碰 json.dumps/loads
- `value_json`/`qualifiers_json` 只在 Repository 内部存在

**注意：这是一个渐进式改造。** 先定义类型，在 Repository 返回时做转换，Application 层逐步替换 dict 为 dataclass。不需要一次性替换所有 dict 引用。

---

## P1-6: 拆分 repository.py

### 问题
单文件 518 行承载 Event、Claim、Evidence、Job、Derivation 五类 repository。
Application Service 仍绕过 repository 直接写 SQL。

### 修复方案

**按聚合拆成多个文件：**
```
src/hl_mem/storage/
    __init__.py
    database.py          (不变)
    events.py            ← EventRepository
    claims.py            ← ClaimRepository
    evidence.py          ← EvidenceRepository
    jobs.py              ← JobRepository
    experience.py        ← Episode/Trace/Feedback/Policy
    _shared.py           ← _insert, batch helpers (共享工具)
    base.py              (不变或精简)
    migrations/          (不变)
    repository.py        ← 保留为 re-export，加 DeprecationWarning
```

**为 Application Service 的裸 SQL 补仓储方法：**
- 找出 `application/ingest.py`、`application/recall.py`、`experience/service.py` 中直接写 SQL 的地方
- 将这些 SQL 移到对应的 repository 中作为方法
- Application 层改为调用 repository 方法

**注意：** 不要为每条 SQL 建接口，只迁移会跨层复用或承载领域不变量的操作。

---

## P2-3: Protocol 采用一致性修正

### 问题
- `Embedder/Reranker` Protocol 有用（有真实+fake 实现），保留
- `Extractor` 有两个实现但签名不一致
- `StorageDatabase` Protocol 只有最小声明，实际应用硬依赖 SQLite，未被消费
- 工厂返回 `Any` 削弱了 Protocol 的收益

### 修复方案

1. **工厂返回类型修正**：
   ```python
   def make_embedder(settings) -> EmbedderProtocol: ...
   def make_reranker(settings) -> RerankerProtocol | None: ...
   def make_extractor(settings) -> ExtractorProtocol: ...
   ```

2. **统一 Extractor 签名**：确保两个实现的 `context` 参数签名一致

3. **删除未使用的 `StorageDatabase` Protocol**（或明确标记为实验性连接探针）

4. **PostgresDatabase 标注为实验性**（加 docstring 说明）

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(types+storage): add stable internal types, split repository, fix protocols"`
4. **不要用 `git add -A`**
5. **repository.py 保留为 re-export**（向后兼容），内部拆分后原路径转发
6. **domain/types.py 不要导入 application 或 storage 层**（纯领域类型）
7. **渐进式**：先定义类型和拆分文件，不需要一次性替换所有 dict 引用
8. **保留 `repository.py` 作为统一入口的 re-export**，让现有导入不崩溃
