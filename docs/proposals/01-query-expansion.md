# 受控多查询召回实现方案

## 目标与边界

在不替换现有 FTS5 + dense + tag + reranker 主链的前提下，为短查询、指代查询和首轮候选不足提供最多 2 条语义改写。原始查询永远参与召回且权重为 `1.0`，扩展查询权重固定为 `0.6`。功能默认关闭，超时、超 token、解析失败或 provider 不可用时退回原始查询。

不做多轮 Agent、查询规划树、扩展结果写库，也不让 LLM 决定 namespace、时间过滤或 RecallIntent。

## 现状与集成点

- `src/hl_mem/application/recall.py::RecallService.recall()`：生成 query embedding、创建 `SearchTracer`、调用 staged pipeline，是组件注入和总 deadline 的入口。
- `src/hl_mem/recall/staged_pipeline.py::_collect_candidates()`：当前对单一 query 各执行一次 `ClaimRepository.search_claims_fts()` 与 `search_claims_vector()`；`_filter_and_score()` 用 `_weighted_rrf_scores()` 融合。
- `src/hl_mem/recall/trace.py::{SearchTrace,CandidateTrace,SearchPhaseMetrics}`：当前只保存 query hash、通道排名和耗时，不保存查询明文。
- `src/hl_mem/components.py`：已有 LLM client 工厂，可按 operation 构造共享重试、超时和 span 记录能力。
- `src/hl_mem/settings.py::Settings`：使用冻结 dataclass、`from_env()` 和 `validate()` 集中配置。

## 协议与类型

在 `src/hl_mem/protocols.py` 增加：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QueryExpansion:
    """单条查询改写及其可审计来源。"""

    text: str
    source: str  # llm_short | llm_coreference | llm_low_recall
    weight: float = 0.6


@dataclass(frozen=True)
class QueryExpansionResult:
    """查询扩展结果与模型用量。"""

    expansions: tuple[QueryExpansion, ...]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class QueryExpansionProtocol(Protocol):
    """生成受限语义改写，不改变查询约束。"""

    def expand(
        self,
        query: str,
        *,
        intent: RecallIntent,
        max_expansions: int,
        timeout_seconds: float,
        token_ceiling: int,
    ) -> QueryExpansionResult: ...
```

在 `src/hl_mem/recall/query_expansion.py` 实现 `QueryExpander`。它通过现有 `LLMClient` 使用 JSON mode，输出 schema 为 `{"queries": ["..."]}`；去空、Unicode 规范化、与原 query 去重、扩展间去重后截断到 2 条。prompt 明确禁止添加人物、时间、namespace 或原查询未给出的事实。

内部候选通道类型：

```python
@dataclass(frozen=True)
class WeightedQuery:
    text: str
    source: str  # original 或 QueryExpansion.source
    weight: float
```

## 触发与数据流

配置 `query_expansion_mode=off|auto|always`：

1. `off`：只构造 `WeightedQuery(query, "original", 1.0)`。
2. `always`：召回前调用 expander；空结果仍只跑原查询。
3. `auto`：
   - `len(query.strip()) < 10`，触发原因 `short_query`；
   - 含独立指代线索 `这、这个、那个、上次、之前那个、它、他们`，触发原因 `coreference`；
   - 以上均不满足时先执行原查询 FTS+dense；去重且通过可见性过滤的候选数小于 `query_expansion_candidate_floor`，再触发 `low_recall`。

为避免 auto 模式重复跑原 query，`_collect_candidates()` 先缓存原始 FTS/dense 结果；只有生成扩展后才补跑扩展通道。每条查询独立 embedding、独立 FTS、独立 dense；tag 仍只从原始 query 提取，避免 LLM 改写制造 tag。

融合输入由“通道”扩为“查询 × 通道”：

```text
score(d) = Σquery_weight × channel_weight / (RRF_K + rank(query, channel, d))
```

- original/FTS 与 original/dense：`1.0 × 1.0`
- expansion-N/FTS 与 expansion-N/dense：`0.6 × 1.0`
- 原始 tag channel：沿用现有 `tag_channel_weight`
- normalization 改为实际启用权重之和除以 `RRF_K + 1`，保持 semantic feature 在 `[0, 1]` 附近。

同一 Claim 在不同查询或通道重复命中时累加 RRF 贡献；随后完全复用可见性、多因子排序、关系扩展、reranker 和 final limit。reranker 仍使用原 query，避免最终语义目标漂移。

## Trace、预算与降级

`SearchTrace` 增加：

```python
@dataclass
class QueryExpansionTrace:
    expansion_text: str
    text_hash: str
    source: str
    weight: float
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    outcome: str = "applied"  # applied|empty|timeout|token_ceiling|error|disabled


# SearchTrace 新字段
expansion_trigger: str | None = None
expansions: list[QueryExpansionTrace] = field(default_factory=list)
expansion_total_tokens: int = 0
```

`expansion_text` 默认保存经控制字符清理并截断到 256 字符的实际扩展文本，以满足结果归因；同时保存完整规范化文本的 SHA-256。trace 不保存 prompt、原始响应或 Claim 正文，部署方可通过既有 trace 访问控制保护扩展文本。`CandidateTrace.channels` 的键使用 `original:fts`、`expansion_1:dense`，从而归因每条查询的贡献。

建议配置：

```text
HL_MEM_QUERY_EXPANSION_MODE=off
HL_MEM_QUERY_EXPANSION_MAX=2
HL_MEM_QUERY_EXPANSION_CANDIDATE_FLOOR=8
HL_MEM_QUERY_EXPANSION_TOKEN_CEILING=256
HL_MEM_QUERY_EXPANSION_TIMEOUT_SECONDS=2.0
HL_MEM_QUERY_EXPANSION_TOTAL_TIMEOUT_SECONDS=3.0
HL_MEM_QUERY_EXPANSION_MODEL=<由现有 LLM 配置注入>
```

`Settings.validate()` 限制 max 为 `0..2`、所有预算为正。单次 LLM 超时受 component timeout 限制，总扩展阶段用 monotonic deadline；超过总 ceiling 时停止尚未执行的扩展。任何异常由 audit 记录具体错误类，但 `RecallService.recall()` 继续原始查询。LLM call 由现有 retry 与 `llm_call_spans` 记录。

## 文件变更

- 新建 `src/hl_mem/recall/query_expansion.py`：触发器、清洗器、`QueryExpander`。
- 修改 `src/hl_mem/protocols.py`：协议和结果类型。
- 修改 `src/hl_mem/settings.py`、`src/hl_mem/components.py`：配置、验证、工厂。
- 修改 `src/hl_mem/application/recall.py::RecallService.__init__/recall`：注入 expander 和 deadline。
- 修改 `src/hl_mem/recall/staged_pipeline.py::_collect_candidates/_filter_and_score`：缓存首轮结果及 weighted query-channel RRF。
- 修改 `src/hl_mem/recall/trace.py`：扩展归因与预算字段。
- 无 migration。

## 测试计划

- 单元：短查询、每类指代词、候选不足触发；边界长度 9/10；off/auto/always。
- 单元：JSON 异常、重复/空/超过 2 条、扩展等于原 query、token ceiling 和 timeout 均回退。
- 单元：构造固定排名验证 `1.0/0.6` weighted RRF 数值、跨扩展去重和 normalization。
- 单元：tag 只使用原 query；reranker 收到原 query；namespace/双时间过滤不被改写绕过。
- 单元：trace 含受截断 expansion 文本、完整文本 hash、来源、token、耗时与候选贡献，不含 prompt/原始响应。
- 集成：fake expander + fake embedder 下首轮不足补回目标 Claim；expander 抛错时结果与 off 模式一致。
- 回归：扩展关闭时既有召回顺序和 SearchTrace 序列化保持兼容。

## 验收标准

- 默认配置不增加 LLM 调用和延迟。
- 一次 recall 最多生成 2 条扩展，原 query 始终执行且权重最高。
- 任意扩展故障不使 recall 失败。
- 可从 SearchTrace 解释每个候选由哪条查询、哪个通道贡献。
