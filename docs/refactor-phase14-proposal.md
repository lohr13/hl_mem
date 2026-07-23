# Phase 14 完整设计方案

> 基于 `docs/refactor-phase14-brief.md` 与 hl_mem v0.3.5 当前源码；设计日期：2026-07-24

## 1. 目标、边界与基线

Phase 14 解决五个问题：长输入导致提取输出截断、提取结果缺少严格 schema 校验、召回未利用既有关系、召回过程不可解释，以及提取器直接绑定单一 LLM provider。

本方案坚持以下边界：

- 不引入文档摄入、持久化 chunk、ANN、动态 taxonomy、新数据库或新 migration。
- 保持同步调用链；不把 worker、`LLMExtractor.extract()` 或 `retry_http()` 改为 async。
- 默认行为保持兼容：不开启 debug 时 REST/MCP 响应形状不变；关闭关系扩展时排序不变；现有 `ExtractorProtocol.extract()` 调用方不变。
- 所有阈值、预算和 provider 能力由配置或环境变量注入，不在业务逻辑中硬编码。
- 题目要求评估“现有 206 个测试”的兼容性；当前工作区实际执行 `pytest --collect-only` 收集到 224 项（其中 unit 198 项）。下文逐项回答 206 项兼容性问题，但实施回归门槛采用更严格的“当前 224 项全部继续通过”。

推荐总体数据流：

```text
event content
  -> 结构识别与边界安全分块
  -> LLMExtractor（prompt/解析/领域规范化）
  -> LLMClient（provider payload/structured output/retry）
  -> Pydantic 本地校验
  -> chunk 结果稳定合并与去重

query -> FTS + dense seeds -> 一跳关系扩展 -> 可见性/namespace 过滤
      -> 低权重候选融合 -> reranker -> limit -> SearchTrace -> response/audit
```

## 2. 公共设计决策

### 2.1 配置

在 `src/hl_mem/config.py` 增加由环境变量读取的默认值，在 `src/hl_mem/settings.py::Settings`/`Settings.from_env()` 做类型和范围校验：

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `HL_MEM_EXTRACT_CHUNK_CHARS` | `12000` | 单块目标字符数 |
| `HL_MEM_EXTRACT_CHUNK_OVERLAP_TURNS` | `1` | 对话块携带的只读前文 turn 数 |
| `HL_MEM_EXTRACT_SPLIT_MAX_DEPTH` | `4` | 输出截断递归二分深度 |
| `HL_MEM_EXTRACT_SCHEMA_RETRIES` | `1` | schema 内容级重试次数（不含 HTTP retry） |
| `HL_MEM_LLM_STRUCTURED_MODE` | `auto` | `auto/json_schema/json_object` |
| `HL_MEM_RELATION_EXPANSION` | `off` | `off/on`；Phase 14 灰度开关 |
| `HL_MEM_RELATION_SEED_LIMIT` | `10` | 参与扩展的最高排名 seed 数 |
| `HL_MEM_RELATION_CANDIDATE_LIMIT` | `20` | 扩展候选独立预算 |
| `HL_MEM_RELATION_WEIGHT` | `0.35` | 关系通道相对语义通道权重 |

`config.py` 只提供默认配置，构造参数显式传入时优先于环境变量。测试使用构造参数，不依赖进程环境。

### 2.2 兼容策略

新增参数一律为 keyword-only 且有保持旧行为的默认值。`hybrid_claims()` 仍默认返回 `list[dict[str, Any]]`；需要 trace 时使用独立的可选 collector，而不是把返回值改成 tuple。`RecallService.recall()` 仅在 `debug=True` 时附加 `search_trace`。`ExtractedClaim` 继续作为 ingest/application 层的稳定领域 DTO，不直接替换成 Pydantic 模型。

## 3. P0-C1：长输入结构感知分块与输出超限恢复

### 3.1 数据结构

新建 `src/hl_mem/ingest/chunking.py`：

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

class ContentStructure(StrEnum):
    TEXT = "text"
    CONVERSATION = "conversation"
    JSONL = "jsonl"

@dataclass(frozen=True)
class ExtractionChunk:
    index: int
    text: str
    structure: ContentStructure
    start_unit: int
    end_unit: int
    context_prefix: str = ""

@dataclass(frozen=True)
class ChunkingPolicy:
    target_chars: int
    overlap_turns: int
    max_split_depth: int
```

`context_prefix` 只帮助消解 subject，不作为可提取事实来源。prompt 明确标记 `<context_only>` 与 `<extract_from>`，要求模型只从后者产出 claim。合并时不依赖 overlap 去重；仍由既有 ingest `fact_hash v2`/semantic dedup 做最终幂等，本层仅按规范化后的 `(subject, predicate, canonical_attribute, value, qualifiers)` 做稳定精确去重，避免同一次调用重复计费后的重复对象。

### 3.2 接口与算法

```python
def detect_content_structure(content: dict[str, Any] | str) -> ContentStructure: ...

def split_extraction_content(
    content: dict[str, Any] | str,
    policy: ChunkingPolicy,
) -> list[ExtractionChunk]: ...

def bisect_extraction_chunk(chunk: ExtractionChunk) -> tuple[ExtractionChunk, ExtractionChunk] | None: ...
```

结构规则：

1. conversation：识别 `messages`、`conversation`、`turns` 等 list[dict]，每个 turn 是不可拆单元；按字符预算贪心装箱。后块仅携带前 `overlap_turns` 个 turn 的 role/content/稳定实体摘要。
2. JSONL：逐行 `json.loads()` 成功率足够高时按完整行分块，永不拆 object 行；单行超限时将该行作为独立块。
3. text：优先按空行/标题段落，其次句界，最后才按字符边界切分；单一超长段落允许二分。
4. 普通 dict：先走现有 `parse_content()` 得到文本；若 dict 内存在 conversation 数组则保留 turn 边界，否则按 text 处理。不会持久化中间 chunk。

修改 `src/hl_mem/ingest/llm_extractor.py::LLMExtractor.extract()`：

```python
def extract(
    self,
    content: dict[str, Any] | str,
    event_context: dict[str, Any] | None = None,
) -> list[ExtractedClaim]: ...

def _extract_chunk_with_auto_split(
    self,
    chunk: ExtractionChunk,
    event_context: dict[str, Any],
    depth: int,
) -> list[ExtractedClaim]: ...

def _extract_one_chunk(
    self,
    chunk: ExtractionChunk,
    event_context: dict[str, Any],
) -> list[ExtractedClaim]: ...

@staticmethod
def _merge_chunk_claims(chunks: list[list[ExtractedClaim]]) -> list[ExtractedClaim]: ...
```

`extract()` 同步顺序遍历 chunk，并同步调用 `_extract_one_chunk()`。这完全适配当前 `httpx.post`/`httpx.Client.post` 模式，不引入并发、事件循环或共享 client 的线程安全问题。`last_usage_tokens` 改为所有请求（包括 schema retry 和递归 split）的 `usage.total_tokens` 累加值，而非最后一次覆盖。

输出超限判断集中到 P1-C4 的 `LLMResponse.finish_reason` 与异常类型：`finish_reason in {"length", "max_tokens"}`、响应 content 空/JSON 明显截断且校验失败时抛 `LLMOutputTruncatedError`。只有该异常触发递归二分；HTTP 429/5xx/timeout 仍由 `retry_http()` 处理，普通 schema 错误走 P0-C2 内容级重试，不误触发无限拆分。达到 `max_split_depth` 或不可再分时抛带 `chunk.index/start_unit/end_unit/depth` 的具体异常。

### 3.3 改动文件与方法

- 新增 `src/hl_mem/ingest/chunking.py`：上述五个类型/函数。
- 修改 `src/hl_mem/ingest/llm_extractor.py`：`LLMExtractor.__init__()` 注入 `ChunkingPolicy`；重构 `extract()`；新增 `_extract_chunk_with_auto_split()`、`_extract_one_chunk()`、`_merge_chunk_claims()`。
- 修改 `src/hl_mem/config.py`：分块默认值。
- 修改 `src/hl_mem/settings.py::Settings`、`Settings.from_env()`：分块配置及校验。
- 修改 `src/hl_mem/components.py::make_extractor()`：把配置传入 extractor/client。
- 新增 `src/hl_mem/errors.py::LLMOutputTruncatedError`（若保持异常集中管理，则加在现有异常族中）。

### 3.4 测试策略与验收标准

新增 `tests/unit/test_extraction_chunking.py`：

- conversation 不拆 turn，块顺序稳定，overlap 只进入 `context_prefix`。
- JSONL 不拆行/object；普通文本优先段落边界；超长单元可二分。
- 短输入只生成一个 chunk，payload 与现有语义等价。
- `finish_reason=length` 后二分并同步顺序调用，合并结果稳定去重。
- 非截断 HTTP 错误不二分；达到最大深度给出精确错误。
- `last_usage_tokens` 累加所有子请求。
- 100 个 turn 的合成输入：每个 turn 恰好作为一次主提取内容出现，最终 claim 无丢失、无同调用重复。

验收：短输入行为与 Phase 13 一致；长输入不因单次输出截断整批失败；无 async；递归请求数上界为 `2^(max_depth+1)-1` 且受配置限制。

### 3.5 兼容性

不会有意破坏现有 206 项。主要风险是现有 `test_llm_extractor.py` 假设一次 HTTP 请求以及 `last_usage_tokens == 12`；短输入仍单块，所以断言保持成立。构造函数新增参数必须有默认值。现有 monkeypatch `httpx.post` 仍由默认 client 路径命中。

## 4. P0-C2：严格 Pydantic schema 约束

### 4.1 API 能力核验结论

截至 2026-07-24：

- 百炼官方的 Qwen Chat Completions 文档只列出 `text` 与 `json_object`；Qwen structured output 文档明确要求生产端继续用 jsonschema 等校验并在失败时重试。`qwen3.7-plus` 支持 JSON Mode，但没有公开保证 OpenAI `json_schema` + `strict: true`。[百炼 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[百炼结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)
- 智谱官方 Chat Completions 文档同样只公开 `text` 与 `json_object` 两种 `response_format`，未公开 `json_schema` strict。[智谱对话补全](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8)

因此当前生产默认不能直接发送 strict schema。实现必须把“本地严格校验”与“远端 strict 能力”分开。

### 4.2 Pydantic 模型

新建 `src/hl_mem/ingest/schemas.py`（不要放入 REST `api/schemas.py`）：

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class ExtractedClaimSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    canonical_attribute: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    value: str = Field(min_length=1)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    volatility: Literal["stable", "ephemeral"]
    reason: str = ""
    scope: Literal["temporal", "permanent"]
    importance: float = Field(ge=0.0, le=1.0)

class ExtractionResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ExtractedClaimSchema]
    entities: list[str] = Field(default_factory=list)
    should_memorize: bool
    sensitivity: Literal["normal", "sensitive", "restricted"] = "normal"
```

远端 JSON Schema 用 `ExtractionResponseSchema.model_json_schema()` 生成，并规范为 provider 接受的 Draft 子集。`additionalProperties: false` 递归保留。Pydantic 校验之后再调用现有 `_claim()`，继续执行 predicate/canonical_attribute 调和、scope normalization 与低价值过滤；Pydantic 不取代领域规范化。

### 4.3 strict 能力与 fallback

P1-C4 定义：

```python
class StructuredOutputMode(StrEnum):
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"

@dataclass(frozen=True)
class LLMCapabilities:
    json_object: bool
    json_schema_strict: bool
```

- DashScope Qwen 与 Zhipu GLM 的内置 capability 默认 `json_object=True, json_schema_strict=False`。
- `HL_MEM_LLM_STRUCTURED_MODE=auto` 依 capability 选择；`json_schema` 仅允许管理员显式配置或未来 provider 声明支持。
- strict payload 采用 OpenAI 形状：`{"type":"json_schema","json_schema":{"name":"extraction_response","schema": ...,"strict":true}}`。
- 若 `auto` 下 strict 请求收到 provider 的 400/422 且错误明确指向 `response_format/json_schema/strict`，同一次业务调用降级为 `json_object`，记录 audit outcome `structured_fallback`；认证、配额、内容安全错误绝不降级掩盖。
- 无论远端模式如何，响应必须通过 `ExtractionResponseSchema.model_validate_json()`/`model_validate()`。`ValidationError` 触发最多 `schema_retries` 次新的内容级 LLM 请求，重试 prompt 只携带错误路径/类型和原始 chunk，不回显可能很长或敏感的无效响应。
- 内容级重试耗尽后抛 `LLMSchemaValidationError`，包含 provider/model/chunk/schema error paths，不包含 API key 或完整原文。

这比启动时发送探测请求更安全：不产生额外费用，也不把暂时性网络失败误判为永久能力。能力由 provider adapter 声明，`auto` 的首次不支持响应可在 client 实例内缓存降级结果。

### 4.4 改动文件与方法

- 新增 `src/hl_mem/ingest/schemas.py`：`ExtractedClaimSchema`、`ExtractionResponseSchema`、schema 生成函数。
- 修改 `src/hl_mem/ingest/llm_extractor.py::LLMExtractor._extract_one_chunk()`：调用 client、Pydantic 校验、内容级重试；`_claim()` 保留领域转换。
- 修改 `src/hl_mem/errors.py`：`LLMSchemaValidationError`、`LLMStructuredOutputUnsupportedError`。
- 修改 P1-C4 的 `src/hl_mem/llm/client.py::LLMClient.complete()` 和 provider adapter payload 构造。
- 修改 `src/hl_mem/config.py`、`settings.py::Settings.from_env()`、`components.py::make_extractor()`：structured mode/schema retries。
- `pyproject.toml` 无需新增直接依赖：FastAPI 已依赖 Pydantic；若项目要求锁定直接使用的库，则将 `pydantic>=2` 显式加入 dependencies 并更新 `uv.lock`。

### 4.5 测试策略与验收标准

扩展 `tests/unit/test_llm_extractor.py`，新增 `tests/unit/test_ingest_schemas.py`：

- 合法完整响应通过；额外字段、缺字段、错误 enum、越界数值、错误 claims 类型均拒绝。
- `model_json_schema()` 中根及嵌套对象均 `additionalProperties=false`，required 字段符合设计。
- json_schema-capable fake provider 收到 strict payload；DashScope/Zhipu 默认收到 json_object。
- strict 400/422 在 auto 模式仅降级一次；401/403/429/5xx 不作能力降级。
- 首次 malformed、第二次合法时成功；耗尽后抛具体 schema 异常。
- 当前 fenced JSON 兼容：json_object fallback 仍先 `_parse_json()` 再 `model_validate()`；严格模式不依赖 fence 修复。

验收：任何进入 `_claim()` 的 item 都已通过结构校验；当前两家 provider 无需 strict 支持也能工作；未来 strict provider 无需修改 extractor 业务代码。

### 4.6 兼容性

风险中等。当前测试 fixture 省略 `entities/sensitivity` 以及部分 claim 字段；若 schema 将所有项目字段设 required，会使现有测试失败。为兼容历史模型响应，建议 Phase 14 的本地 schema 对 `entities/sensitivity/reason` 提供默认值，但对核心 claim 字段严格；`subject/predicate/value/qualifiers/confidence/volatility/scope/importance/canonical_attribute` 应在新的 prompt 中 required。迁移期可由 `_parse_legacy_defaults()` 在校验前只补齐与现有 `_claim()` 默认值完全一致的缺失字段，并记录 audit；下一阶段再关闭。这样现有 206 项可保持通过，同时新响应走严格契约。

## 5. P1-C4：Provider 能力解耦

P1-C4 是 P0-C1/P0-C2 的基础设施依赖，实施顺序应提前，但业务验收仍按五项分别完成。

### 5.1 分层与接口

新建 `src/hl_mem/llm/`：

```text
llm/
  __init__.py
  types.py       # 中立请求/响应/能力数据结构
  client.py      # 同步 transport、retry、fallback
  providers.py   # DashScope/Zhipu/OpenAI-compatible payload adapter
```

```python
@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "user", "assistant"]
    content: str

@dataclass(frozen=True)
class StructuredOutputSpec:
    name: str
    schema: dict[str, Any]
    preferred_mode: StructuredOutputMode

@dataclass(frozen=True)
class LLMRequest:
    messages: list[LLMMessage]
    structured_output: StructuredOutputSpec | None = None

@dataclass(frozen=True)
class LLMResponse:
    content: str | dict[str, Any]
    finish_reason: str | None
    usage_total_tokens: int
    raw_request_id: str | None = None

class LLMProviderProtocol(Protocol):
    name: str
    capabilities: LLMCapabilities
    def build_payload(self, model: str, request: LLMRequest, mode: StructuredOutputMode) -> dict[str, Any]: ...
    def parse_response(self, payload: dict[str, Any]) -> LLMResponse: ...
    def is_structured_mode_unsupported(self, error: httpx.HTTPStatusError) -> bool: ...

class LLMClient:
    def __init__(
        self, api_key: str, base_url: str, model: str,
        provider: LLMProviderProtocol, timeout: httpx.Timeout,
        max_attempts: int, client: httpx.Client | None = None,
    ) -> None: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...
```

`LLMClient.complete()` 负责 URL/header、同步 post、`raise_for_status()`、`retry_http()`、响应外壳解析、structured mode 选择/降级和 usage。它必须复用 `hl_mem.http_utils.retry_http()`，删除 `LLMExtractor._post()` 内联的三次循环。连接错误当前不在 `retry_http()` 的重试集合中；设计保持现有语义或先以独立测试确认是否将 `httpx.ConnectError` 纳入统一 retry，不能悄然改变重试次数。

`LLMExtractor` 只负责：构建 extraction prompt、分块编排、schema 验证、把 schema DTO 转成 `ExtractedClaim`、领域规范化。`ExtractorProtocol` 保持：

```python
class ExtractorProtocol(Protocol):
    def extract(
        self,
        content: dict[str, Any] | str,
        context: dict[str, Any] | None = None,
    ) -> list[ExtractedClaim]: ...
```

这同时修正当前 protocol 只接受 dict、实际实现接受 dict/str 的偏差。建议用 `TYPE_CHECKING` 避免协议层运行时循环导入。

### 5.2 Provider 选择

`components.py::make_extractor()` 根据新增 `HL_MEM_LLM_PROVIDER`（默认 `dashscope`）创建 adapter；允许 `dashscope/zhipu/openai_compatible`。base URL、model、key、timeout、attempts 全由现有/新增环境变量读取。未知 provider 立即抛 `ConfigurationError`，生产环境不 silent fallback 到 fake。

保留 `LLMExtractor(api_key, base_url, model, timeout=None, client=None)` 旧构造签名作为兼容 facade：内部创建默认 DashScope-compatible `LLMClient`。新增推荐签名用 keyword-only `llm_client: LLMClient | None = None`，避免现有 e2e 和单测重写。

### 5.3 改动文件与方法

- 新增 `src/hl_mem/llm/types.py`：中立 DTO、capabilities、protocol。
- 新增 `src/hl_mem/llm/providers.py`：`DashScopeProvider`、`ZhipuProvider`、`OpenAICompatibleProvider` 的 `build_payload()`/`parse_response()`。
- 新增 `src/hl_mem/llm/client.py::LLMClient.complete()`、`_post_once()`、`_select_structured_mode()`。
- 修改 `src/hl_mem/ingest/llm_extractor.py::LLMExtractor.__init__()`、`_extract_one_chunk()`；删除或 deprecated `._post()`。
- 修改 `src/hl_mem/protocols.py::ExtractorProtocol.extract()` 的精确类型。
- 修改 `src/hl_mem/components.py::make_extractor()`：provider/client 工厂装配。
- 修改 `src/hl_mem/settings.py::Settings`/`from_env()` 和 `config.py`：provider/retry/structured 配置。
- 修改 `src/hl_mem/http_utils.py::retry_http()` 仅在测试证明需要时扩展 `ConnectError`，保留 timeout/429/5xx 规则。

### 5.4 测试策略与验收标准

新增 `tests/unit/test_llm_client.py`、`tests/unit/test_llm_providers.py`：

- provider adapter 的 payload/response 契约快照；finish_reason、usage、request id 正确映射。
- 注入 `httpx.Client` 与全局 `httpx.post` 两条路径均可测试。
- timeout、429、5xx 重试；400/401 不重试；退避由 monkeypatch 隔离。
- provider 切换不改变 extractor prompt 与领域输出。
- `make_extractor()` 对三种 provider、缺 key、未知 provider、production 行为正确。
- `isinstance(fake, ExtractorProtocol)`（若加 `@runtime_checkable`）或静态契约测试覆盖 dict/str。

验收：`llm_extractor.py` 不再导入/调用 `httpx.post`，不包含 provider URL/header/重试循环；现有 Extractor 调用方无改动。

### 5.5 兼容性

低到中风险。最大风险是现有 `test_llm_extractor.py` monkeypatch `httpx.post` 和断言三次 ConnectError 调用。兼容 facade 与 `LLMClient` 的默认全局 post 路径可保持这些测试；如果统一 retry 后刻意改变 ConnectError 规则，需要同步测试并在变更说明中标记。`ExtractorProtocol` 的类型收紧不影响运行时。

## 6. P1-C2：一跳关系扩展召回

### 6.1 数据结构与边界

新建 `src/hl_mem/recall/relation_expansion.py`：

```python
@dataclass(frozen=True)
class RelationExpansionConfig:
    enabled: bool = False
    seed_limit: int = 10
    candidate_limit: int = 20
    relation_weight: float = 0.35
    allowed_relations: frozenset[str] = frozenset(
        {"summarizes", "supports", "follows", "about", "derived_from"}
    )

@dataclass(frozen=True)
class ExpandedCandidate:
    claim_id: str
    seed_id: str
    relation: str
    source: Literal["memory_relations", "evidence_links"]
    edge_confidence: float
    expansion_score: float

def expand_related_claims(
    connection: sqlite3.Connection,
    repo: ClaimRepository,
    seeds: list[dict[str, Any]],
    reference: str,
    known_as_of: str | None,
    intent: RecallIntent,
    namespace: str,
    config: RelationExpansionConfig,
) -> tuple[list[dict[str, Any]], list[ExpandedCandidate]]: ...
```

只扩展一跳。seed 为 FTS+dense 融合后、reranker 前的前 `seed_limit` 项。候选预算与主通道分离，最多 `candidate_limit` 个。扩展分数：

```text
seed_semantic_normalized * edge_confidence * relation_weight / (1 + hop)
```

本阶段 hop 恒为 1，所以衰减项为 1/2。若同一候选由多条边命中，取最大分而非求和，避免高 degree 节点主导。关系候选只能补充：主通道候选的原分不因关系边增加；最终排序 tie-break 保持 `_recorded_epoch`、id 的确定性。

### 6.2 复用 `get_relations_batch()` 与 `evidence_links`

扩展 `src/hl_mem/domain/relations.py::get_relations_batch()`，保持旧两参数调用的现有返回形状，新增 keyword-only 选项：

```python
def get_relations_batch(
    connection: sqlite3.Connection,
    claim_ids: list[str],
    *,
    include_memory_relations: bool = False,
    include_reverse_evidence: bool = False,
) -> dict[str, list[dict[str, Any]]]: ...
```

- 默认值仍只返回当前 outbound `evidence_links`，保护 `RecallService._batch_relations()` 的 API 输出。
- 扩展召回传两个开关为 True，一次批量读取 `memory_relations` 的 from/to 双向边。
- evidence 正向：`derived_type='claim' AND derived_id IN seeds AND evidence_type='claim'`，邻居为 `evidence_id`。
- evidence 反向：`evidence_type='claim' AND evidence_id IN seeds AND derived_type='claim'`，邻居为 `derived_id`。
- event/episode/observation 等非 claim 端不是召回 claim，过滤掉；`supersedes/contradicts` 默认不扩展，避免把历史失效或冲突 claim 当正向补充。它们仍可进入 trace 的 `relation_filtered` 原因。
- 批量 SQL 继续按 500 id 分片，禁止 N+1。

收集邻居 id 后必须复用 `ClaimRepository.batch_get_claims()` 加载完整行，并调用 `claim_is_visible()`，同时检查 `namespace_key == namespace`。这阻止跨 namespace、retracted/archived、双时间不可见候选泄漏。

### 6.3 接入 `hybrid_claims()`

签名新增 keyword-only 依赖：

```python
def hybrid_claims(
    ...,
    namespace: str = "default",
    *,
    relation_connection: sqlite3.Connection | None = None,
    relation_config: RelationExpansionConfig | None = None,
    trace: SearchTracer | None = None,
) -> list[dict[str, Any]]: ...
```

接入点在第 146 行形成 `ranked_claims` 后、reranker 前。扩展候选建立与主候选相同的 `memory_features`，其 semantic 特征使用受上限约束的 expansion score；随后和主候选一起送入 reranker。最终仍由原 `limit` 截断。若 relation connection/config 缺失或 disabled，则完全不查询关系表，排序逐字节兼容。

`RecallService.recall()` 传 `self.connection` 与构造时注入的 config。访问记录、feedback、evidence、relations 组装自然覆盖最终被选中的扩展 claim。

### 6.4 改动文件与方法

- 新增 `src/hl_mem/recall/relation_expansion.py`：config、candidate、`expand_related_claims()`。
- 修改 `src/hl_mem/domain/relations.py::get_relations_batch()`：可选 memory/reverse evidence 查询。
- 修改 `src/hl_mem/recall/recall_pipeline.py::hybrid_claims()`：扩展接入与低权重融合。
- 修改 `src/hl_mem/application/recall.py::RecallService.__init__()`、`recall()`：注入/传递 config。
- 修改 `src/hl_mem/config.py`、`settings.py::Settings.from_env()`、`components.py` 或 `api/server.py::create_app()`：灰度配置装配。

### 6.5 测试策略与验收标准

新增 `tests/unit/test_relation_expansion.py`：

- `memory_relations` from/to 双向一跳可发现邻居；`evidence_links` 正反向 claim 边可发现邻居。
- 明确验证复用了 `get_relations_batch()`，且 501 个 seed 分两批、无逐条查询。
- relation allowlist、生效 confidence、重复路径取 max、独立预算、只一跳。
- namespace、status、valid time、recorded time、intent 全部过滤。
- 关系候选不能挤掉分数明显更高的主通道候选；主通道不足时可补位。
- disabled 时不访问关系表且与当前结果/分数完全相同。
- reranker 能收到扩展候选；reranker 失败仍按扩展后的 pre-rank fallback。

验收：召回遗漏但与高质量 seed 有允许关系的 active claim 能进入候选；扩展候选不主导 top results；无 N+1；旧 relation response 不变。

### 6.6 兼容性

默认关闭时不会破坏现有 206 项。开启后排序是有意变化，应使用新测试/离线 eval 验证而不是修改旧的 disabled 基线。`get_relations_batch()` 默认参数必须保持现有输出，否则 `_assemble_results()` 的 `relations` 字段可能变化并破坏 API 测试。

## 7. P1-C3：统一 SearchTrace

### 7.1 数据模型

新建 `src/hl_mem/recall/trace.py`，使用 dataclass 而非 Pydantic（内部热路径对象）：

```python
@dataclass
class CandidateTrace:
    claim_id: str
    channels: dict[str, int] = field(default_factory=dict)  # channel -> 1-based rank
    channel_scores: dict[str, float] = field(default_factory=dict)
    pre_rank: int | None = None
    pre_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    final_rank: int | None = None
    included: bool = False
    filter_reasons: list[str] = field(default_factory=list)
    relation_paths: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class SearchPhaseMetrics:
    fts_us: int = 0
    dense_us: int = 0
    relation_us: int = 0
    fusion_us: int = 0
    reranker_us: int = 0
    assembly_us: int = 0
    total_us: int = 0

@dataclass
class SearchTrace:
    query_id: str
    query_hash: str
    intent: str
    limit: int
    candidate_limit: int
    candidates: dict[str, CandidateTrace]
    phases: SearchPhaseMetrics
    outcome: str = "success"
    truncated: bool = False

class SearchTracer:
    def __init__(self, trace: SearchTrace, max_candidates: int = 200) -> None: ...
    def record_channel(self, channel: str, claims: list[dict[str, Any]]) -> None: ...
    def record_filter(self, claim_id: str, reason: str) -> None: ...
    def record_pre_rank(self, claims: list[dict[str, Any]], scores: dict[str, float]) -> None: ...
    def record_rerank(self, results: list[tuple[str, float]]) -> None: ...
    def record_final(self, claims: list[dict[str, Any]]) -> None: ...
    def to_dict(self) -> dict[str, Any]: ...
```

filter reason 使用受控字符串：`not_visible_valid_time`、`not_visible_recorded_time`、`status_filtered`、`namespace_filtered`、`relation_not_allowed`、`candidate_budget`、`reranker_omitted`、`final_limit`。为了回答“为什么没召回”，trace 只承诺解释“进入任一候选通道或关系邻居集合的 claim”；它不能证明一个从未被 FTS/vector scan 命中的任意数据库 claim 为何缺席。文档/API 必须明确这一语义边界。

### 7.2 生命周期与输出

- `RecallService.recall(debug: bool = False)` 在 debug 时创建 tracer，并把它传给 `hybrid_claims()`；`query_id` 在创建 trace 前确定。
- `hybrid_claims()` 在每阶段调用 tracer。可见性过滤应从列表推导改为显式循环，以记录具体原因，但不改变过滤规则。
- `RecallService` 在 `_assemble_results()` 后记录 assembly timing，最终 `response["search_trace"] = tracer.to_dict()`。
- 无论 debug 是否开启，都把同一份 compact trace 摘要写入现有 audit `detail_json`；debug=false 只写 channel ids、returned ids、timing、outcome，保持 16 KB 限制。debug=true 也不能把 query 原文、claim value 或 evidence 原文写入 audit。
- `SearchTracer.max_candidates` 超过后只保留最终项和各通道前 N，设置 `truncated=true`；不得让审计失败影响召回。

API schema：

```python
class RecallInput(BaseModel):
    ...
    debug: bool = False
```

MCP 若需暴露，同样新增默认 false 的 `debug`；不新增独立数据库表或 migration。

### 7.3 改动文件与方法

- 新增 `src/hl_mem/recall/trace.py`：上述模型与 `SearchTracer` 方法。
- 修改 `src/hl_mem/recall/recall_pipeline.py::hybrid_claims()`：可选 tracer、阶段记录、精确过滤原因。
- 修改 `src/hl_mem/application/recall.py::RecallService.recall()`、`_assemble_results()` 周边 timing。
- 修改 `src/hl_mem/api/schemas.py::RecallInput`：`debug`。
- 修改 `src/hl_mem/api/server.py::recall()`（当前 `/v1/recall` handler）：透传 debug。
- 修改 `src/hl_mem/mcp/server.py` 对应 recall tool handler：可选透传 debug。
- 修改 `src/hl_mem/observability/audit.py::AuditLogger._detail_json()`：原则上无需改；测试其截断行为即可。若增加 helper，命名 `emit_search_trace()` 并仍走 `emit()`。

### 7.4 测试策略与验收标准

新增 `tests/unit/test_search_trace.py`，扩展 API/MCP 测试：

- 记录 FTS/dense/relation rank、pre-rank、rerank 前后、final rank 与 timing。
- 对时间不可见、namespace 不符、relation 不允许、超预算、reranker omission、limit 截断给出受控原因。
- debug=false 响应严格不含 `search_trace`；debug=true 含 schema 稳定、可 JSON 序列化的 trace。
- trace 不包含 query 明文、claim value、API key；audit 超 16 KB 正确 compact/truncated。
- NullAuditLogger/写 audit 失败不影响召回。
- tracer disabled 时不创建逐候选对象，结果与当前排序相同。

验收：对任何“曾进入候选但未返回”的 claim，至少有 channel/rank 和一个排除阶段或原因；trace timing 与现有 audit 字段一致；默认 API 契约不变。

### 7.5 兼容性

低风险。`RecallInput.debug=False` 是向后兼容字段；服务方法新增默认参数不影响调用方。最大的回归风险是为了记录 filter 原因而重写可见性逻辑，必须继续以 `claim_is_visible()` 为唯一真源，tracer 只能解释结果，不能复制一套规则。

## 8. 测试与回归总门槛

每批完成后执行：

```powershell
.venv\Scripts\python.exe -m pytest tests\unit\ -q --tb=short
.venv\Scripts\python.exe -m pytest -q --tb=short
```

总体验收：

1. 题目指定的现有 206 项不受破坏；当前工作区实际收集到的 224 项全部通过；新增测试全部通过。
2. `LLMExtractor` 短输入只发一个请求，长输入同步分块，截断只重试受影响 chunk。
3. 所有提取结果进入领域转换前经过 Pydantic 校验；DashScope/Zhipu 在无 strict 支持时自动使用 json_object + 本地严格校验。
4. Provider 切换不改 extractor 业务代码，HTTP retry 唯一实现为 `retry_http()`。
5. 关系扩展关闭时结果不变；开启时一跳、批量、独立预算、双时间与 namespace 安全。
6. debug trace 可解释候选路径与淘汰阶段，不泄漏原文或秘密，不改变默认 API。
7. `black --check`（120 列配置需与项目约定一致）、`isort --check-only --profile black`、至少一次 import 检查均通过。

建议新增一个小型离线 eval（不计入 206 回归）：20 个“语义不直接匹配、只能靠关系补充”的 query，以及 20 个长对话 subject 消歧样本。指标为 seed recall、expanded recall、precision@k、重复 claim 数、LLM 请求数、schema retry 率和 trace 完整率。关系扩展上线门槛建议 expanded recall 提升且 precision@5 相对下降不超过 2 个百分点。

## 9. 批次、依赖与执行顺序

### Batch 14A：Provider 基座 + Pydantic schema（合并实施）

包含 P1-C4 与 P0-C2。先建立 `LLMClient`/provider capability，再让 Pydantic schema 驱动 structured output。两者修改同一条 HTTP/payload/parse 链，拆成不同 PR 会产生临时重复抽象和二次改造。

顺序：中立 DTO/protocol → provider adapters → `LLMClient` + `retry_http()` → Pydantic schema → extractor 兼容 facade → components/settings → 测试。

风险最高点：两家 API 能力差异、旧 fixture 缺字段、ConnectError retry 语义。以 fake provider 契约测试和 json_object fallback 降低风险。

### Batch 14B：长输入分块

包含 P0-C1，依赖 14A 的 `LLMResponse.finish_reason`、schema 异常分类和 usage 累计接口。独立完成 chunking 与递归恢复，不与召回改动混合。

顺序：纯 chunking 函数 → 单 chunk extractor → 自动二分 → 合并/usage/audit → 长输入测试。

### Batch 14C：SearchTrace 骨架

包含 P1-C3 的数据模型、现有 FTS/dense/reranker tracing、API debug 输出。它不依赖 14A/14B，可与其开发并行，但建议在关系扩展前合入，因为 14D 直接复用 relation trace channel 与 filter reasons。

### Batch 14D：关系扩展召回

包含 P1-C2，依赖 14C 的 trace 接口；不依赖 ingest 两批。先扩展 `get_relations_batch()`，再实现纯一跳 expansion，最后接入 `hybrid_claims()` 并灰度开启。

### 推荐依赖图

```text
14A Provider + Schema -> 14B Long-input chunking

14C SearchTrace      -> 14D Relation expansion

14A/14B 与 14C 可并行；最终全量回归后再开启 relation expansion 灰度。
```

不建议把五项放入一个批次：ingest 与 recall 可独立验收，且关系排序变化需要单独回滚开关。也不建议先做 P0-C1 再抽 Provider；否则分块重试会绑定旧 `_post()`，在 14A 中被二次重写。

## 10. 兼容性结论

| 改进项 | 默认是否改变行为 | 破坏现有 206 测试的风险 | 主要保护措施 |
|---|---|---|---|
| P0-C1 | 短输入否；长输入改善 | 低 | 单块 fast path、旧构造签名、同步调用 |
| P0-C2 | 校验更严格 | 中 | legacy defaults 迁移层、json_object fallback、内容级 retry |
| P1-C2 | 默认关闭时否 | 低；开启后排序有意变化 | feature flag、独立预算、一跳低权重、默认 relation API 不变 |
| P1-C3 | debug=false 否 | 低 | optional collector、`claim_is_visible()` 仍为真源、响应字段按需附加 |
| P1-C4 | 外部行为否 | 中 | extractor 兼容 facade、保留 monkeypatch 路径、统一 retry 契约测试 |

结论：按 14A → 14B、14C → 14D 两条依赖链实施，并坚持默认关闭关系扩展、默认不返回 debug trace，现有 206 项不应被破坏。P0-C2 是唯一需要显式迁移兼容层的项目；待线上 schema retry/legacy-default 指标归零后，再在后续版本移除 legacy defaults。
