# Phase 14 设计简报 — 基于 Hindsight 对比的优化方向

> Hermes 独立分析 · 2026-07-23
> 基于 Codex 审查报告 + Hindsight 0.8.5 源码对比

## 背景

hl_mem v0.3.5 经过 Phase 13 修复后（13 个问题全部解决），与 Hindsight 0.8.5 做了源码级交叉对比。对比发现 hl_mem 在语义治理（双时间、冲突、证据链、生命周期、Experience）方面领先，但在以下 4 个硬伤上需要改进。

## 需要设计的 5 个改进项

### P0-C1: 长输入结构感知分块 + 输出超限恢复

**问题**: `LLMExtractor.extract()` 一次请求提取整个 event content。如果 content 很长（完整会话、日志、代码文档），LLM 输出超限时整个提取失败。

**Hindsight 做法** (`fact_extraction.py:491-621, 1698-1810`):
- 区分纯文本 / conversation JSON / JSONL 格式
- 结构感知分块：不拆断 turn/object 边界
- `_split_chunk_for_output_retry()`: 输出超限时递归二分重试
- `_extract_facts_with_auto_split()`: 先提取一个 chunk，失败则对半切分递归

**hl_mem 约束**:
- 当前 LLMExtractor 是同步的（httpx.post），不是 async
- 百炼 API 有 response_format: json_object
- content 来源是对话内容（dict），不是长文档
- 需要考虑提取质量：分块后上下文丢失可能导致 subject 混淆

### P0-C2: 严格 Pydantic schema 约束

**问题**: 当前 LLMExtractor 用 `response_format: {"type": "json_object"}` + 手工字段检查。LLM 可能返回 schema 外的字段或缺失必填字段。

**Hindsight 做法** (`fact_extraction.py:1075-1191, 1302-1310`):
- 用 Pydantic 模型生成 JSON schema
- `response_format: {"type": "json_schema", "json_schema": {"name": "facts", "schema": ..., "strict": true}}`
- 动态 taxonomy（按 entity labels 配置动态生成 schema）
- 内容级重试：malformed response 重新请求

**hl_mem 约束**:
- 百炼 API 是否支持 `json_schema` strict mode？需要验证（智谱 GLM 和百炼 qwen3.7-plus 对 response_format 的支持程度不同）
- `ExtractedClaim` 已经是 dataclass，可以转 Pydantic 模型
- 不需要动态 taxonomy（当前场景固定）
- 需要保留 `canonical_attribute`、`scope`、`importance` 等本项目特殊字段

### P1-C2: 关系扩展召回

**问题**: `memory_relations` 表和 `evidence_links` 已存在，但 recall 只做 FTS + dense，不沿关系扩展。

**Hindsight 做法** (`link_expansion_retrieval.py:103`, `graph_retrieval.py`):
- 从 semantic seed 出发，沿 entity/temporal/causal link 扩展一跳
- 设独立预算和衰减权重（1/(1+hop)）
- 扩展结果参与 RRF 融合

**hl_mem 约束**:
- 已有 `get_relations(connection, claim_id)` 和 `get_relations_batch()`
- 已有 `evidence_links`（derived_from/supports/contradicts/follows/about/supersedes）
- 需要在 `hybrid_claims()` 的 FTS+dense 结果后增加一跳扩展
- 扩展结果不应主导排序（只作为补充候选）

### P1-C3: 统一 SearchTrace

**问题**: audit 记录了各通道耗时和候选 ID，但无法回答"某条记忆为什么没被召回"。

**Hindsight 做法** (`search/trace.py`, `search/tracer.py`):
- `SearchTracer` 记录每路候选的进入原因、各通道 rank、过滤原因、reranker 前后排序
- `SearchPhaseMetrics` 记录各阶段耗时
- trace 可回放，用于 A/B 调参

**hl_mem 约束**:
- 已有 `hybrid_claims()` 中的 audit emit（含 fts_ids/dense_ids/rrf_ids/returned_ids/scores/timing）
- 需要扩展为结构化的 trace 对象，可以附在 recall response 中（debug 模式）
- 或写入 audit log 的 detail 字段（已有基础设施）

### P1-C4: Provider 能力解耦

**问题**: `LLMExtractor` 直接绑定百炼 API（httpx.post 到 coding.dashscope.aliyuncs.com）。切模型或 provider 需要改业务代码。

**Hindsight 做法** (`engine/providers/`, `engine/llm_wrapper.py`):
- provider 层封装 call/structured output/batch/prompt cache/rate-limit
- `LLMConfig` 统一配置入口
- 业务代码只依赖 `LLMConfig.call()` 接口

**hl_mem 约束**:
- 已有 `ExtractorProtocol`、`EmbedderProtocol`、`RerankerProtocol`（接口存在）
- 已有 `components.py` 工厂（从环境变量创建实例）
- 已有 `http_utils.py` 的 `retry_http()`
- 需要抽出 `LLMClient` 封装（transport + structured output + retry），让 Extractor 只负责业务逻辑（prompt 构建 + response 解析）

## 不需要设计的

- PostgreSQL/ANN（数据量不够）
- 多租户/webhook
- OpenTelemetry（先内部 stats）
- document/chunk 增量摄入（当前不摄入文档）
- 动态 taxonomy（场景固定）

## 期望产出

请对每个改进项产出：
1. **设计文档**（数据结构、接口签名、改动范围、文件列表）
2. **实施批次建议**（依赖关系、顺序、风险）
3. **测试策略**（新增测试点、验收标准）
4. **兼容性评估**（是否会破坏现有 220+ 测试）

写到 `docs/refactor-phase14-proposal.md`。
