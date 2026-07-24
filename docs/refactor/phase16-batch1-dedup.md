# Phase 16 Batch 1: 消除重复实现

## 目标

每个核心功能只有一份实现。不是删代码，是收敛到正确的那一份。

---

## 1. 向量 BLOB 编解码：统一到 ingest/embedder.py

### 现状
- `core/vector.py`: `encode_vector()` / `decode_vector()` — 返回 list[float]
- `ingest/embedder.py`: `pack_vector()` / `unpack_vector()` — 返回 tuple[float, ...]
- 两者都做 float32 array → bytes → float32 array，但返回类型和校验不同

### 修复
- 删除 `core/vector.py` 中的 `encode_vector()` / `decode_vector()`
- 如果 `core/vector.py` 还有其他内容（如 cosine_similarity），保留
- 全仓搜索 `encode_vector` / `decode_vector` 的引用，改为 `pack_vector` / `unpack_vector`
- 如果 `core/vector.py` 删空了，删除文件

### 涉及文件
- `src/hl_mem/core/vector.py` — 删除重复函数
- 搜索全仓引用 `encode_vector` / `decode_vector` 的位置，改为 `pack_vector` / `unpack_vector`

---

## 2. RRF + 上下文装箱：统一到正式实现

### 现状
- `recall/recall_pipeline.py` 有内联 RRF 融合逻辑
- `recall/extended_pipeline.py` 有独立 `reciprocal_rank_fusion()` + `budget_pack()`
- `application/recall.py` 有自己的上下文装箱逻辑

### 修复
- 如果 `extended_pipeline.py` 的 RRF 和 budget_pack **没有被生产代码调用**（只有测试），直接删除 `extended_pipeline.py`
- 如果有生产调用方，将 `recap_pipeline.py` 的内联 RRF 提取为使用 `extended_pipeline` 的函数（或反过来统一到一个位置）
- 目标：RRF 只有一份实现，budget_pack 只有一份实现

### 涉及文件
- `src/hl_mem/recall/extended_pipeline.py` — 删除或保留
- `src/hl_mem/recall/recall_pipeline.py` — 确保使用唯一 RRF 实现
- `src/hl_mem/application/recall.py` — 确保使用唯一 budget_pack 实现

---

## 3. HTTP retry：统一到 http_utils.py

### 现状
- `http_utils.py`: `retry_http()` — 统一重试，处理 ConnectError/Timeout/429/5xx
- `ingest/embedder.py:59-79`: 内联 retry 循环 — 自己做指数退避

### 修复
- `ingest/embedder.py` 的 Embedder._request() 改用 `retry_http()`
- 删除 embedder 内联的 for attempt 循环和 time.sleep
- 保留 embedder 的 max_attempts 参数，传给 retry_http

### 涉及文件
- `src/hl_mem/ingest/embedder.py` — 删除内联 retry，改用 http_utils.retry_http

---

## 4. 召回管线假阶段：真正分阶段

### 现状
`recall_pipeline.py` 的 `_filter_and_score` / `_expand_related` / `_rerank` / `_finalize` 全是 no-op（原样 return），所有工作在 `_collect_candidates` 里。

### 修复

**按双方共识的方案，真正分配工作到各阶段：**

1. `_collect_candidates(request) → CandidateSet`：只做 FTS + 向量检索，返回两个有序通道 + 统一时间快照
2. `_filter_and_score(candidates) → list[dict]`：应用 `claim_is_visible()`、去重、RRF 融合、helpful rate、多因子先验评分
3. `_expand_related(scored) → list[dict]`：可选关系扩展（默认关闭时原样返回）
4. `_rerank(expanded) → list[dict]`：reranker 调用 + 降级 + 部分结果补尾
5. `_finalize(ranked) → list[dict]`：limit 截断 + preference 保留 + trace + audit + `_score` 装配

**关键约束：**
- 保持排序结果不变（这是重构不是调参）
- 中间结果可以用简单 dict（暂不引入 dataclass，避免过度改动）
- `hybrid_claims()` 的对外签名完全不变

---

## 5. TypeError 兼容猜测：删除

### 现状
`recall_pipeline.py` 多处用 `except TypeError` 猜测旧仓储签名，以及 `hasattr` 检查不存在的旧方法。这会把真实的 TypeError 误判为签名问题。

### 修复
- 删除所有 `except TypeError` 兼容分支（L201, L219, L224 等）
- 删除 `hasattr(repo, "search_claims_vector")` / `hasattr(repo, "helpful_rates")` 等旧签名检查
- 直接使用当前仓储的实际方法签名（ClaimRepository 已有 search_claims_fts / list_embedded 等方法）

### 涉及文件
- `src/hl_mem/recall/recall_pipeline.py`

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**（测试断言更新由 Hermes 负责）
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(converge): eliminate duplicate implementations (vector/rrf/retry/stages/typeerror)"`
4. **不要用 `git add -A`**
5. **保持排序结果不变** — 召回管线的输出必须与重构前完全一致
6. 如果删除 `extended_pipeline.py` 导致测试 import 失败，保留文件但清空内容只留 re-export 指向正式实现
7. **版本号 bump**: 0.6.0 → 0.6.1
