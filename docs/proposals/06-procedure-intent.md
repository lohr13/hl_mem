# Tool / Procedure 专用召回 Intent 实现方案

## 目标与边界

为“该用什么工具”和“具体怎么做”建立专用召回路由，复用已有 episode → trace → policy Experience 通道、evidence chain 和 packed token budget。新增 `TOOL`、`PROCEDURE` intent，但不创建新的 tool/skill memory 表，不复制 MemOS 的多存储体系。

## 当前路由与集成点

- `domain/temporal.py::RecallIntent` 当前包含 current/historical/preference 等事实可见性意图。
- `domain/recall.py::route_recall_intent()` 当前显式处理 preference、历史措辞和过去 as_of；`route_query()` 已有 `"procedure"` 字符串通道路由，但未成为端到端 RecallIntent。
- `application/recall.py::RecallService.recall()` 将 intent 传给 staged Claim pipeline，随后组装 observations。
- `experience/service.py::ExperienceService` 提供 episode/trace/policy 写入和 policy 生命周期。
- `storage/experience.py::ExperienceRepository.list_policies()` 等方法可扩展为专用检索。
- migration 008 已有 `episodes(goal,status,reward,outcome_summary)`、`traces(action,observation,error_signature,value,priority)`、`policies(trigger,procedure,reliability,success_count,failure_count,status,procedure_status)`。
- `RecallService._pack_context()` 当前只装配 claims/relations/observations，没有 episode/policy/trace 配额；本特性必须扩展统一 assembler，使 Experience 类型也受同一个 packed token budget 控制，而不是建立第二套预算。

## Intent 定义与路由

在 `domain/temporal.py::RecallIntent` 增加：

```python
class RecallIntent(StrEnum):
    # 既有成员保持不变
    TOOL = "tool"
    PROCEDURE = "procedure"
```

TOOL 表示“选择/识别工具或调用能力”，PROCEDURE 表示“步骤、运行手册、如何完成任务”。二者在 Claim 双时间可见性上按 `CURRENT_STATE` 处理，不能因为新增 enum 绕过 status/namespace/known_as_of 过滤。

确定性规则按优先级：

1. 显式 API intent 永远优先；
2. 有过去 `as_of` 或明确历史询问，仍为 HISTORICAL；
3. 偏好词仍为 PREFERENCE；
4. TOOL 强信号：`用什么工具/哪个工具/tool/命令/接口/API/插件`；
5. PROCEDURE 强信号：`怎么做/如何/步骤/流程/部署/安装/配置/排障/上次怎么/照上次`；
6. 其余 CURRENT_STATE。

“上次”单独出现有歧义：与动作词共现走 PROCEDURE，否则保持历史/当前规则。`部署` 默认 PROCEDURE；“部署工具有哪些”因 TOOL 强组合走 TOOL。

可选 LLM 路由只在规则无强信号且配置为 auto 时调用，输出：

```python
@dataclass(frozen=True)
class IntentDecision:
    intent: RecallIntent
    confidence: float
    rationale_code: str


class IntentRouterProtocol(Protocol):
    def route(
        self,
        query: str,
        *,
        allowed: tuple[RecallIntent, ...],
        timeout_seconds: float,
    ) -> IntentDecision: ...
```

confidence 低于阈值、超时或非法结果退回确定性路由。LLM 不能改变 as_of/known_as_of。

## 专用召回数据流

新建 `src/hl_mem/recall/procedure_pipeline.py`，返回统一 `MemoryCandidate`：

```python
@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: Literal["policy", "episode", "trace", "claim"]
    memory_id: str
    text: str
    score: float
    evidence: tuple[dict[str, object], ...]
    features: dict[str, float]
```

### TOOL intent

按以下顺序与配额召回：

1. active policy：`status='active'` 且 `procedure_status` 非 retired，trigger/procedure 命中工具词；
2. success episode：`status='success'`、reward 高，goal/outcome 与查询相关；
3. 相关 traces：优先 action 名称和成功 episode 下的 trace；
4. claims 补充：现有 FTS+dense，提供版本、路径、限制、凭据偏好等事实。

TOOL 的展示以“工具 + 可靠 procedure + 成功证据”为核心，不单独返回脱离 episode 的低价值 trace。

### PROCEDURE intent

同样保持“active policy → success episode → traces → claims”，但提高 policy/trace 配额，并把完整步骤放入 packed context。失败 episode 默认不作为步骤推荐，只可在剩余预算中作为 `pitfall`，且必须标记 error/outcome。

检索实现首版使用 SQLite `LIKE`/token overlap + bounded candidates，不新增 FTS 表；数据量增大且 benchmark 证明瓶颈后再为 policies/episodes 建 FTS migration。

## 排序

每类先归一到 `[0,1]`：

```text
policy_score =
  0.40 * semantic_or_text_match +
  0.35 * policy.reliability +
  0.15 * usefulness_score +
  0.10 * recency

episode_score =
  0.35 * semantic_or_text_match +
  0.30 * clamp(reward, 0, 1) +
  0.20 * recent_outcome +
  0.15 * recency

trace_score =
  0.40 * action_match +
  0.25 * parent_episode_reward +
  0.20 * trace.value +
  0.15 * recent_outcome
```

`recent_outcome` 是同 trigger/task_type 最近 N 次 episode 的指数衰减成功率，不是当前墙钟下的任意 boost；窗口 N、half-life 由 Settings 注入。policy reliability 与 episode reward 是主排序因子，特性 5 的 usefulness 只作为较小、独立因子。

跨类型不直接用不可比 raw score 全排序。packed assembler 使用配额：

```text
TOOL:      policy 35% / episode 25% / trace 15% / claim 25%
PROCEDURE: policy 40% / episode 20% / trace 25% / claim 15%
```

某类不足时剩余 token 按 policy → episode → claim → trace 的顺序回流。每个 candidate 通过现有 evidence links 或 episode_id/trace parent link附带来源；输出保持 memory_type/id，feedback 才能回写正确类型。

## SearchTrace

扩展 trace 或新增同层 `ExperienceCandidateTrace`：

```python
@dataclass
class ExperienceCandidateTrace:
    memory_type: str
    memory_id: str
    source_rank: int
    features: dict[str, float]
    final_rank: int | None
    included: bool
    filter_reasons: list[str]
```

记录 intent 来源 `explicit|keyword|llm|fallback`、各类型候选数、配额 token、回流量、最终包含项；不记录 procedure/goal 正文。召回后对所有最终 memory type 写 retrieval feedback exposure，为特性 5 提供闭环。

## 配置、文件和 migration

```text
HL_MEM_PROCEDURE_RECALL_MODE=off|keyword|auto   # 默认 keyword
HL_MEM_PROCEDURE_LLM_THRESHOLD=0.80
HL_MEM_PROCEDURE_ROUTER_TIMEOUT_SECONDS=1.5
HL_MEM_PROCEDURE_CANDIDATE_LIMIT=30
HL_MEM_PROCEDURE_RECENT_OUTCOME_WINDOW=20
HL_MEM_PROCEDURE_OUTCOME_HALF_LIFE_DAYS=30
```

- 修改 `domain/temporal.py`、`domain/recall.py`、`domain/constants.py`。
- 新建 `recall/procedure_pipeline.py`，修改 `application/recall.py` 做 intent 分派与统一 packing。
- 修改 `storage/experience.py` 增加 bounded policy/episode/trace 查询。
- 修改 `recall/trace.py`、API/MCP intent enum 与返回 schema。
- 若启用 LLM router，修改 `protocols.py`、`components.py`、`settings.py`。
- 无 migration；使用既有表和索引。实施后用 benchmark 决定是否另提 Experience FTS migration。

## 测试计划

- 路由：中文/英文 TOOL 与 PROCEDURE 词、`上次`歧义、部署组合、显式 intent、历史和偏好优先级。
- LLM fallback：低置信、timeout、非法 enum，且不能改时间参数。
- 存储查询：namespace/status 过滤、只取 success episode、trace 绑定 parent、candidate limit。
- 排序：固定 fixture 手算 reliability/reward/recent outcome；稳定 tie-break；失败 episode 不成为推荐步骤。
- packing：TOOL/PROCEDURE 配额、类型不足回流、总 token 不超预算、evidence 完整。
- feedback：最终 policy/episode/trace/claim 使用正确 memory_type 记录 exposure。
- 回归：其他 intent 继续走原 Claim pipeline；功能 off 时既有输出不变。

## 验收标准

- “怎么做/工具/部署/上次怎么做”能稳定进入正确路径。
- 返回顺序体现 active policy、成功 episode、相关 trace、claims 补充，且每项可回溯证据。
- 不新增专用存储表或独立基础设施，总上下文严格受 packed budget 控制。
