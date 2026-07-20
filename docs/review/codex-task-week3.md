# 任务：hl_mem Week 3 — 向量检索 + 去重 + Conflict Key + Observation

请先阅读现有代码：
- src/hl_mem/ingest/llm_extractor.py — ExtractedClaim 结构
- src/hl_mem/ingest/budget.py — TokenBudget
- src/hl_mem/storage/repository.py — ClaimRepository, EvidenceRepository
- src/hl_mem/storage/migrations/001_initial.sql — claims 表已有 embedding_dense/sparse/model/dim 列
- src/hl_mem/api/server.py — _queue_and_extract() 和 recall 端点
- src/hl_mem/ingest/extractors.py — ExtractedClaim dataclass
- docs/architecture.md Section 6.3 (Embedding策略), Section 7 (矛盾检测), Section 9.2 (Observation)
- docs/review/consensus.md — Embedding 选型 (text-embedding-v4 2048维)

## 目标
接入真实 Embedding（text-embedding-v4）、实现去重、Conflict Key 矛盾检测和 Observation 生成。

## 1. Embedding Provider

创建 `src/hl_mem/ingest/embeddings.py`：

### 配置（环境变量）
```
EMBEDDING_API_KEY=***          # 百炼通用 AK (sk-e72xxx)
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=2048
```

注意：和 LLM 使用不同的 key 和端点（LLM 走 coding.dashscope，Embedding 走 compatible-mode）。

### Embedder 接口
```python
class Embedder:
    def __init__(self, api_key, base_url, model, dim=2048):
        ...
    
    def embed(self, texts: list[str]) -> list[bytes]:
        """批量 embed，返回 float32 BLOB 列表（每条 2048维×4字节=8192字节）"""
        # text-embedding-v4 每批最多10条
        # 用 httpx 调 POST {base_url}/embeddings
        # body: {"model": model, "input": texts, "dimensions": dim}
        # 返回的 float list → struct.pack 成 bytes 存 BLOB
        ...
    
    def embed_one(self, text: str) -> bytes:
        """单条 embed"""
        ...
```

### FakeEmbedder 保留
已有的 FakeEmbedder 保留。通过 `HL_MEM_EMBEDDER=fake|real` 切换。

### 余弦相似度
```python
def cosine_similarity(blob_a: bytes, blob_b: bytes) -> float:
    """从 BLOB 解包 float32 数组，计算余弦相似度"""
    # struct.unpack 或 numpy.frombuffer
    ...
```

### 批量处理
text-embedding-v4 每批最多 10 条。批量调用时自动分片：
```python
def embed_batch(self, texts: list[str]) -> list[bytes]:
    """自动按10条分片，合并结果"""
    ...
```

## 2. Claim 去重

创建 `src/hl_mem/recall/dedup.py`：

### 精确去重（L1）
新 claim 写入前，检查是否已有相同 conflict_key + 相同 value_json 的 active claim：
```python
class Deduplicator:
    def __init__(self, claim_repo: ClaimRepository, embedder: Embedder, threshold: float = 0.95):
        ...
    
    def find_duplicate(self, new_claim: dict) -> tuple[str | None, str]:
        """返回 (existing_claim_id, match_type) 或 (None, 'new')"""
        # L1: 精确 conflict_key + value_json 匹配
        # L2: 语义相似度 > threshold (需要 embedding)
        ...
```

### 语义去重（L2）
对同 namespace 的 active claims 做 embedding 余弦相似度比较：
- 相似度 > 0.95：视为重复，合并证据（添加 evidence_link），不生成新 claim
- 0.72-0.95：交给 Conflict Resolver 判断是矛盾还是可兼容
- < 0.72：视为不同事实

性能：只比较同 namespace 下、相同 subject_entity_id 的 claims（用 SQL 过滤收窄范围）。

## 3. Conflict Key + 矛盾检测

创建 `src/hl_mem/recall/conflict.py`：

### Conflict Key 计算
```python
def compute_conflict_key(namespace: str, subject: str, predicate: str, qualifiers: dict) -> str:
    """hash(namespace + canonical_subject + predicate + sorted_exclusive_qualifiers)"""
    # 返回 SHA256 hex 的前16字符
    ...
```

### 确定性冲突规则
不需要 LLM，纯规则判断：
```python
class ConflictResolver:
    def resolve(self, existing: dict, new: dict) -> str:
        """
        返回: entails | compatible | state_change | contradicts | uncertain
        """
        # 1. Boolean/Enum/Number 比较
        # 2. 时间关系：旧 valid_to < 新 valid_from → state_change
        # 3. 相同 value → entails
        # 4. 不同 value，同 authority → contradicts
        # 5. 不同 value，不同 authority → uncertain
        ...
```

### 处理结果
- `entails`：合并证据，不生成新 claim
- `compatible`：并存
- `state_change`：旧 claim → superseded，新 claim → active，写 supersedes_id
- `contradicts`：两个都标 disputed
- `uncertain`：新 claim 保持 candidate

首版不调 LLM 做冲突分类（留到后续迭代），只用确定性规则。

## 4. Observation 规则

创建 `src/hl_mem/recall/observation.py`：

### 触发条件
```python
class ObservationBuilder:
    MIN_PROOFS = 2       # 最少2个独立证据
    MIN_SOURCES = 1      # 最少1个不同 source（首版宽松）
    
    def try_build(self, claims: list[dict]) -> dict | None:
        """检查是否有足够证据生成 Observation"""
        # 条件：同一 conflict_key 或同一 subject+predicate 下
        # 有 >= MIN_PROOFS 个 active claims
        # 且来自 >= MIN_SOURCES 个不同 event（evidence 不可全来自同一 event）
        ...
```

### Observation 内容
生成的 Observation body 格式：
```
基于 {n} 条证据：{claim_summary}
来源：{event_ids}
最早观察：{earliest_date}，最近观察：{latest_date}
```

写入 derivations 表（kind='observation'），并为每条支持的 claim 写 evidence_link。

## 5. 改造 server.py

修改 `_queue_and_extract()`：
1. FakeExtractor/LLMExtractor 提取出 claim
2. 计算 conflict_key
3. 调 Deduplicator 检查重复 → 重复则合并证据跳过
4. 调 ConflictResolver 检查矛盾 → 按结果处理状态
5. 写入 claim（含 embedding BLOB，如果 embedder 可用）
6. 检查是否满足 Observation 条件 → 生成 observation

修改 `POST /v1/recall`：
- 除了 FTS，也做向量检索（暴力余弦，扫描 embedding_dense BLOB）
- RRF 合并 FTS + Dense 结果
- 返回结果包含 observation（如果有）

## 6. 新增 API
- `POST /v1/memories` — 显式保存（映射为 pinned claim，source_authority=high）
- `DELETE /v1/memories/{id}` — 显式遗忘（级联：claim status→retracted + embedding 清空 + evidence_link 保留 + 关联 observation → stale）

## 7. 测试

### 单元测试
- `tests/unit/test_embeddings.py`：
  - FakeEmbedder 返回正确维度
  - cosine_similarity 正确计算（相同=1.0，正交=0.0）
  - BLOB pack/unpack 一致性
  - 批量分片（>10条自动分2批）

- `tests/unit/test_dedup.py`：
  - L1 精确去重（相同 conflict_key+value）
  - L2 语义去重（高相似度合并）
  - 新事实不误杀

- `tests/unit/test_conflict.py`：
  - state_change（深色→浅色模式）
  - entails（相同事实再次出现）
  - contradicts（同 authority 不同值）
  - compatible（不同 predicate 不冲突）

- `tests/unit/test_observation.py`：
  - 不足2个证据不生成
  - 2个独立证据生成 observation
  - 同一 event 的2个 claim 不算独立

### 集成测试
- `tests/integration/test_conflict_pipeline.py`：
  - 发送"我喜欢深色模式"→ active claim
  - 发送"现在用浅色模式"→ 旧的 superseded，新的 active
  - recall "模式偏好" → 只返回浅色模式（当前值），历史查询返回演化

- `tests/integration/test_forget.py`：
  - 保存一个 memory → recall 能查到
  - delete → claim retracted, embedding 清空, observation stale
  - recall 不再返回

### 验收标准
1. 所有现有测试通过（19个）
2. 新增测试全绿
3. 去重：相同事实不重复写入
4. 矛盾检测：偏好更新正确触发 supersede
5. Observation：2个独立证据生成
6. forget 级联生效
7. 向量检索 + FTS RRF 合并
8. 不安装任何 LLM/Embedding SDK
9. HL_MEM_EMBEDDER=fake 时所有测试通过（不依赖外部 API）

## 约束
- Embedding 用 httpx 调 HTTP，不装 dashscope SDK
- 每批最多10条（text-embedding-v4 限制）
- 余弦相似度用 numpy（已在依赖中）或纯 struct+math
- Conflict Resolver 首版不用 LLM，纯确定性规则
- 每个文件不超过 200 行
- 完成后运行 pytest 验证
