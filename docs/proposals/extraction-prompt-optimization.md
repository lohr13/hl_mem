# hl_mem 记忆提取 Prompt 优化方案

> 状态：Proposal  
> 日期：2026-07-29  
> 范围：仅讨论 memory extraction prompt 与分类语义，不包含代码实现。

## 摘要

当前 `llm_extractor.py` 已有 schema 约束、低价值黑名单、scope 规则和单个完整输出示例，但仍有四个结构性问题：

1. “准入、事实抽取、taxonomy、时间解析、打分、JSON 格式”混在一个长 prompt 中，模型容易先抽取、后为结果寻找分类；
2. `scope` 与 `volatility` 虽被声明为独立字段，但没有用同一组对照样例展示二维组合；
3. 唯一完整示例把 `confidence` 锚定在 `0.95`，同时缺少低、中置信度样例；
4. `事实` 是无成本 fallback，导致评审意见、建议、假设和客观事实混为一类。

建议把单次调用中的推理顺序改为：

`候选命题 → 来源/言语行为判定 → 准入门 → 原子化 → predicate/slot → scope → volatility → evidence confidence → 输出`

不建议只继续扩充关键词黑名单。更有效的策略是以“未来是否可行动、是否有证据、是否仍有区分价值”为正向准入条件，
再用来源和言语行为过滤过程噪声。第一阶段保持 JSON schema 兼容；第二阶段再评估新增 predicate 和
`evidence_level`。

## 第一部分：业界方案调研

### 1. Mem0

Mem0 的公开 prompt 把提取目标定义为可用于未来个性化的离散事实，显式列出偏好、身份与重要个人信息、计划、
服务偏好、健康、职业等类别，并使用大量 few-shot：`Hi` 和常识陈述返回空列表，姓名、职业、偏好和带时间的经历
被提取。其 user-memory prompt 还严格限制只从 user message 提取，防止 assistant 的附和或自述污染用户记忆。
公开仓库中的新版 additive prompt 则更偏 recall：要求同时关注用户与 assistant 的新增信息，跳过 assistant 对用户
内容的复述、寒暄、填充语和过于泛化的内容，并把观察日期作为相对时间的唯一锚点。它甚至明确采用
“有疑问时提取、下游去重”的 recall-first 策略。因此，Mem0 的 admission policy 适合个人助理，但不能原样用于
hl_mem：hl_mem 当前主要问题是 precision 和过程噪声，应借鉴它的消息角色隔离、空输出反例、时间锚定与
“请求中的附带事实”，不应照搬其宽松准入倾向。

Mem0 的传统两阶段流程先抽事实，再把新事实与已有记忆比较，执行 `ADD / UPDATE / DELETE / NONE`；这把“是否是
候选事实”和“是否改变记忆状态”分开。公开提取 schema 没有可校准的 confidence 字段，质量主要由明确类别、
few-shot、角色边界及下游合并保证，而不是依赖 LLM 自报概率。

来源：[Mem0 公开 prompts.py](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py)；
[Mem0 仓库及新版算法说明](https://github.com/mem0ai/mem0)

### 2. Letta / MemGPT

Letta 的关键不是离线“全自动事实抽取器”，而是分层记忆和 agent 自主管理。core memory（现称 memory blocks）
是始终放在上下文中的有限结构块，典型标签是 `human`、`persona`，也可定义 policies、scratchpad、共享状态等；
每个 block 的 `description` 是 agent 判断如何读写它的主要规则。archival memory 位于上下文外，需要显式搜索，
适合规模更大、非每轮必需的信息。官方 context hierarchy 直接用“信息规模 × 每轮重要性”决定存储层级：
少量且持续关键的信息进入 blocks，大量、按需使用的信息进入 archive。

这意味着其 admission criterion 是机会成本驱动的：只有每轮都值得占上下文预算、会稳定改变行为或个性化的信息
才配进入 core；其余有潜在未来价值的信息进入 archival；原始对话本身留在 recall/conversation history。分类不是
单纯的事实主题 taxonomy，而是“可见性和使用频率”taxonomy。Letta 的 memory formation 多由 agent 在对话热路径
通过工具完成，而不是由一个统一抽取 prompt 批量打 confidence；公开文档也没有通用概率校准机制。

对 hl_mem 的启发是把 `permanent` 理解为“跨会话有效”，不要误解为“每轮都应常驻”；同时将“明确影响未来行为的
身份、偏好、约束、政策”设为高优先级，将可检索但非持续关键的项目事实保留在普通 claim 通道。block description
的做法也说明：每个 slot 应有短而互斥的用途说明和边界反例，而不是只给正向示例。

来源：[Letta Memory Blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)；
[Letta Context Hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)

### 3. Zep / Graphiti

Graphiti 将输入原文保存为 episode，再从当前 episode 抽 entity nodes，随后只在已解析实体之间抽取 fact edges。
previous episodes 只用于指代消解与连续性，不能作为当前 episode 新事实的来源。实体 prompt 有很强的负约束：
不抽代词、抽象情绪、泛化名词、裸关系称谓、日期时间和无法独立区分的对象；并通过多组 good/do-not-extract
few-shot 强化 specificity。fact prompt 要求关系必须由当前消息明确陈述或无歧义蕴含，端点必须来自 entity list，
保留品牌、型号、数量、地点等具体细节，避免同义重复。

其管线随后单独进行 entity resolution、edge deduplication、contradiction/invalidation 与双时间处理。最新公开版本
进一步把 timestamp resolution 从结构事实提取中拆出，避免日期解析与关系抽取竞争；每个 edge 带
`episode_indices`，保存事实到来源 episode 的 provenance。Zep 产品层将 raw episodes、granular facts、
entity summaries 和跨事实 observations 分开：episode 保留原文，fact 是有精确有效期的离散关系，observation
才是有证据支持的稳定模式、决策或承诺。

Graphiti 的公开 edge schema 没有让 LLM 输出通用 confidence。它用“当前消息证据限定 + schema + 分步解析 +
去重/矛盾流程 + provenance”控制可信度。这对 hl_mem 最重要的启发是：评审意见、工具报告和历史文档可以作为
episode/evidence 保存，但只有被明确确认的命题才升级为当前事实；时间、冲突和事实抽取不应在同一认知步骤内互相
污染。

来源：[Graphiti entity extraction prompt](https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/extract_nodes.py)；
[Graphiti fact extraction prompt](https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/extract_edges.py)；
[Graphiti edge deduplication prompt](https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/dedupe_edges.py)；
[Zep Context Types](https://help.getzep.com/context-types)

### 4. LangMem

LangMem 明确区分 semantic、episodic、procedural memory；semantic 又可采用固定 profile 或可增长 collection。
memory formation 有两种方式：agent 在热路径主动写入（conscious formation），或后台对会话反思并抽取
（subconscious formation）。其 manager 接收当前消息和已有 memory state，通过 LLM 的结构化 tool calls 执行
insert、update、delete；开发者通过 Pydantic schema 和 `instructions` 定义应用自己的 formation policy，而不是
依赖一个跨领域固定 taxonomy。

官方示例用 `Triple(subject, predicate, object, context)` 说明 schema 本身就是提取策略：`User said yes` 脱离语境
无用，必须增加“回答了什么问题”的 context。它也允许同时使用 Preference、Relationship 等多个 schema，并通过
namespace 隔离用户、团队和领域。LangMem 的原则是“只记能让 agent 在未来更有帮助的信息”，并明确提醒
under-extraction 与过度创建之间需要由应用 instructions 调节。importance 更适合参与 recall，而不是充当事实
真实性概率。公开 memory manager 没有统一的 confidence calibration。

对 hl_mem 的启发是：先按言语行为和记忆类型形成候选，再映射到结构 schema；把“事实是否可信”与“未来是否有用”
分开；用带 context 的独立命题代替短状态词；通过 benchmark 反馈迭代 formation instructions。

来源：[LangMem 概念指南](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)；
[LangMem Semantic Memory 提取指南](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)；
[LangMem Memory API](https://langchain-ai.github.io/langmem/reference/memory/)

### 5. Cognee

Cognee 面向非结构化文档，其 Cognify 管线按 `document classification → chunking → graph extraction →
summarization → graph/vector persistence` 运行。LLM 从 chunk 提取 entities 与 relationships，结构化输出受
Pydantic graph model 约束；节点和摘要同时进入图与向量存储。开发者可以传入 custom prompt 或自定义 graph
model，还可以提供 RDF/OWL ontology：抽取的 entity type 与 mention 会对齐规范概念，匹配成功后标记
`ontology_valid`，并补充父类和 object-property edges。抽取后还会去重 node/edge；矛盾检测是独立、可选步骤。

Cognee 没有公开一套面向对话过程噪声的通用 admission prompt，也没有统一 confidence 标尺；其可靠性更多来自
文档分类、语义分块、结构 schema、ontology grounding、provenance 和后处理。这说明 hl_mem 不应要求模型自由创造
predicate/attribute：`SLOT_REGISTRY` 应扮演轻量 ontology，模型负责选择或 abstain，服务端负责确定性投影。

来源：[Cognee Cognify](https://docs.cognee.ai/core-concepts/main-operations/legacy-operations/cognify)；
[Cognee Ontologies](https://docs.cognee.ai/core-concepts/further-concepts/ontologies)；
[Cognee extract_graph_from_data](https://github.com/topoteretes/cognee/blob/main/cognee/tasks/graph/extract_graph_from_data.py)

## 横向结论

| 系统 | “值得记住” | taxonomy / scope | confidence | 噪声策略 | few-shot |
|---|---|---|---|---|---|
| Mem0 | 未来个性化有用的个人事实、偏好、计划与经历 | 事实类别；新旧记忆另做 ADD/UPDATE/DELETE/NONE | 无通用自报概率 | 角色隔离、跳过复述/寒暄/泛化内容 | 强，含空输出 |
| Letta | 值得持续占上下文或值得按需检索 | core blocks / archival / conversation history | 无通用标尺 | 由存储机会成本和 agent 工具决策控制 | 主要靠 block description |
| Graphiti | 当前 episode 明确支持的 entity/relation | episode / entity / fact / observation；双时间 | 无通用标尺 | 当前来源限定、实体白名单、分步去重/失效 | 强，good 与 exclusion 成对 |
| LangMem | 能提高未来帮助度的 semantic/episodic/procedural 信息 | profile / collection + 自定义 schema/namespace | 无通用标尺 | 应用级 formation instructions + consolidate/update/delete | 示例强调自足 context |
| Cognee | 可形成领域知识图谱的实体与关系 | chunk / node / edge / summary + ontology | 无通用标尺 | 文档分类、分块、schema、ontology、去重 | 由 schema/custom prompt 驱动 |

共同点是：主流系统很少相信 LLM 输出的未经校准概率；它们更依赖证据边界、结构化 schema、abstention、
provenance 和后处理。hl_mem 应保留 confidence 字段，但必须把它重新定义为离散证据等级的数值编码。

## 第二部分：hl_mem 提取 Prompt 优化方案

### 1. 准入准则

#### 1.1 从“长期值得记住”改为可执行的四门判定

每个候选命题必须依次通过四个门，任一失败即不生成 claim：

1. **证据门**：当前事件直接陈述、明确确认，或由当前事件无歧义蕴含；不得把问题、建议、假设、待验证项当事实。
2. **未来效用门**：未来会改变回答、决策、个性化、约束执行、任务连续性或冲突判断。
3. **持续/时点门**：至少在当前事件之后仍有意义；纯执行进度、耗时预估、健康检查、测试快照没有后续行动价值时
   不准入。一次事件若本身具有长期检索价值（已作出的决定、真实经历、承诺）可以准入。
4. **区分度门**：脱离上下文后仍具体、自足，且不是通用常识、礼貌话、复述、纯数字、纯路径或无主体状态。

建议 prompt 使用以下短式判定：

> 如果六个月后检索到这条信息，它是否会改变 agent 的回答或行动？  
> 当前事件是否为它提供直接证据，而非仅提出、建议、评审或预测？  
> 如果任一答案为“否”，不要生成 claim。

#### 1.2 先识别言语行为，再抽事实

要求模型先在内部把候选归为以下一种，不把该中间结果输出到 JSON：

- `asserted`：说话者明确陈述；
- `committed`：用户/有权限主体作出决定、承诺或约束；
- `reported`：工具或文档报告某次观测；
- `proposed`：建议、方案、评审意见；
- `hypothetical`：假设、示例、条件分支；
- `procedural`：正在执行、下一步、耗时、进度；
- `phatic`：寒暄、确认、感谢。

默认只允许 `asserted`、`committed`；`reported` 仅在具有未来效用且保留来源/时点时准入；其余拒绝。
特别规定：

- “评审认为 X 有风险”只能抽成“评审提出 X 风险”，不能抽成“X 是事实”；若现有 predicate 无法准确表达，
  第一阶段宁可不抽。
- assistant 的“我将运行测试”“预计 10 分钟”“正在修改”是 `procedural`，拒绝。
- assistant 的“测试已通过”是 `reported` 快照，默认拒绝；用户明确要求记住某个验收基线时例外。
- 用户明确决定“采用方案 B”是 `committed`，即使出现在过程对话中也应准入。
- assistant 对用户原话的复述或确认不产生第二条 claim。

#### 1.3 `should_memorize` 改为派生结果

不要让模型先整体决定 `should_memorize` 再抽取。先逐候选过准入门，最后规定：

`should_memorize = (claims 非空)`

顶层 reason 可以由被拒候选的主因生成审计信息，但不应影响单条 claim 的准入。

### 2. scope / volatility 分类

#### 2.1 固定为两个正交问题

`scope` 回答“事实在哪段时间内有效”，`volatility` 回答“在有效期内预计多容易变化”：

1. **scope**
   - `permanent`：没有已知结束边界；跨会话持续成立，直到新证据修改。不是“永远不变”。
   - `temporal`：只对明确时间窗、一次运行、当前阶段、某版本、某任务或某个事件成立。
2. **volatility**
   - `stable`：通常不会在短期内自然变化；需要明确决定或事件才会改变。
   - `ephemeral`：会随运行、环境、状态刷新或短期计划频繁变化。

禁止使用“一年后还成立吗”作为唯一判据，因为项目事实和身份都可能在一年内改变，但仍是无已知截止期的当前事实。
更好的问题是：

> 该命题是否绑定某次运行、明确截止日期、当前阶段或版本？是 → temporal，否则 → permanent。  
> 即使没有明确截止期，它是否预期会在数小时/数天内自动刷新？是 → ephemeral，否则 → stable。

#### 2.2 必须给出四象限对照

| 示例 | scope | volatility | 是否准入 |
|---|---|---|---|
| 用户长期偏好简洁回答 | permanent | stable | 是 |
| hl_mem 默认使用 SQLite WAL | permanent | stable | 是 |
| 用户下周三前完成 benchmark | temporal | stable | 是，明确承诺/截止期 |
| 当前服务监听临时端口 8200 | temporal | ephemeral | 通常否；仅后续任务依赖时是 |
| CI 当前 443 passed | temporal | ephemeral | 否 |

`permanent + ephemeral` 应极少出现；若模型选择该组合，应重新检查是否其实是 temporal。`temporal + stable`
是合法且重要的，适合有明确期限但期限内稳定的计划、旅行和冻结期配置。

#### 2.3 分类顺序

先确定事实类型和言语行为，再判 scope，最后判 volatility。不要从 predicate 直接推 scope：

- `配置` 可以是永久稳定默认值，也可以是本次运行临时覆盖；
- `计划` 通常 temporal + stable；
- `状态` 通常 temporal + ephemeral；
- `身份`、稳定偏好通常 permanent + stable；
- 明确的架构能力/约束通常 permanent + stable；
- source 为 tool/status/report 只降低先验，不能自动把其中明确的长期决定删除。

### 3. confidence 校准

#### 3.1 重定义

`confidence` 只表示“当前 evidence 是否足以支持该 claim 的内容和归因”，不表示：

- 该信息有多重要；
- 说话者语气有多坚定；
- 该事实能持续多久；
- 模型对 slot/predicate 的分类把握；
- 与已有记忆是否一致。

#### 3.2 用离散锚点代替任意小数

prompt 只允许模型从五档选择，再编码为数值：

| confidence | 证据条件 | 例子 |
|---:|---|---|
| 0.98 | 用户明确要求记住，或权威结构化字段直接给出 | “记住：默认端口是 8200” |
| 0.90 | 当前消息直接、无条件陈述，主体和对象明确 | “我使用 PostgreSQL 16” |
| 0.75 | 由明确上下文消解代词/省略后得到，只有一种合理解释 | “它以后都走 WAL”且上文唯一实体为 SQLite |
| 0.55 | 来源是转述、历史报告、工具推断，内容可能真实但当前性/归因较弱 | “评审报告称该接口非原子” |
| < 0.50 | 含歧义、推测、建议、未确认评审意见或主体不明 | 不准入 |

禁止输出 0.91、0.93、0.95 等未定义中间值。这样既避免 `0.95` 锚定，也让分布可审计。上线时应按 source kind
统计 reliability diagram；上述数值是等级编码，不应宣称为统计概率，直到用人工 gold set 做校准。

#### 3.3 分类不确定性与事实不确定性分离

如果事实明确但 predicate/slot 不确定，不应降低事实 confidence 来掩盖分类问题：

- 事实明确：保持 evidence confidence；
- `canonical_slot` 无法唯一确定：返回 `null`；
- predicate 无法精确表达言语行为：拒绝或进入候选审计，不使用 `事实` 强行兜底。

后续 schema 可新增 `classification_confidence`，但不建议在第一阶段同时增加两个 LLM 分数。

### 4. Few-shot 设计

few-shot 应紧邻准入和分类规则，采用“输入 → 输出 → 一句边界说明”，覆盖当前真实失败模式。完整 JSON 示例中的
confidence 应改为多个档位，避免所有样例都是 0.95。

#### 正例 1：明确偏好

输入：

> 用户：以后回答尽量简洁，先给结论，不要长篇铺垫。

输出要点：

```json
{
  "subject": "用户",
  "predicate": "偏好",
  "canonical_slot": "preference.response_style",
  "value": "用户偏好简洁回答，并要求先给结论、避免长篇铺垫",
  "confidence": 0.9,
  "scope": "permanent",
  "volatility": "stable"
}
```

说明：明确、可改变未来回答，且无已知结束边界。

#### 正例 2：有期限的计划

输入（`occurred_at=2026-07-29`）：

> 用户：我决定周五前完成 extraction benchmark，期间先不切换模型。

输出两条原子 claim：

```json
[
  {
    "predicate": "计划",
    "value": "用户计划在 2026-07-31 前完成 extraction benchmark",
    "confidence": 0.9,
    "scope": "temporal",
    "volatility": "stable"
  },
  {
    "predicate": "配置",
    "value": "extraction benchmark 完成前保持当前模型不变",
    "confidence": 0.9,
    "scope": "temporal",
    "volatility": "stable"
  }
]
```

说明：有明确截止边界，但期限内是稳定承诺，不是 ephemeral。

#### 正例 3：确认后的架构决定

输入：

> assistant：评审建议把时间解析拆成第二步。  
> 用户：同意，就按这个方案定下来，事实抽取阶段不要解析时间。

输出要点：

```json
{
  "subject": "hl_mem",
  "predicate": "事实",
  "canonical_attribute": "fact.architecture",
  "value": "hl_mem 将事实抽取与时间解析拆分为两个阶段",
  "qualifiers": {"change": true},
  "confidence": 0.9,
  "scope": "permanent",
  "volatility": "stable"
}
```

说明：assistant 的建议本身不准入；用户确认后成为架构决定。若新增 `决策` predicate，则这里应使用 `决策`。

#### 反例 1：过程状态与耗时预测

输入：

> assistant：我正在执行检索，预计还要 10 分钟，完成后会运行测试。

输出：

```json
{"claims": [], "should_memorize": false}
```

说明：全是 procedural future/progress，没有跨事件效用；不能提取“正在执行”“预计 10 分钟”或“将运行测试”。

#### 反例 2：CI / 健康快照

输入：

> assistant：CI 全绿，443 passed、1 skipped，healthz 返回 ok。

输出：

```json
{"claims": [], "should_memorize": false}
```

说明：这是会自动刷新且不改变未来行为的 tool/status snapshot。

#### 反例 3：未确认的评审意见

输入：

> reviewer：`ingest.py` 可能存在事务不原子的问题，建议进一步验证。

输出：

```json
{"claims": [], "should_memorize": false}
```

说明：“可能”“建议验证”是 proposed/hypothetical finding，不得改写为“ingest.py 的事务不原子”。如果产品需要保存
评审事项，应进入独立的 `finding`/task 通道并保留 attribution，而不是事实 claim。

### 5. Predicate 与 taxonomy 调整

#### 5.1 短期：不改 schema，收紧 `事实`

第一阶段保持现有七类 predicate，降低迁移风险，但加入以下规则：

- `事实` 只表示当前 evidence 直接支持的客观命题；
- 用户批准的架构决定暂映射 `事实 + fact.architecture`；
- 行为约束优先 `配置 + config.policy`；
- 未确认评审意见、建议、假设不得使用 `事实`；
- 能确定 canonical attribute 时先选 attribute，再由 registry 投影 predicate；
- 无法唯一确定 slot 返回 `null`，不得为了填充率猜测；
- `事实` fallback 需要在 reason 中给出具体事实类型，便于统计和后续拆类。

#### 5.2 中期：建议新增三个 predicate

建议通过 benchmark 验证后增加：

1. `决策`：已被有权限主体确认的方案或架构选择；
2. `约束`：必须/禁止/验收条件/行为政策，区别于普通配置值；
3. `发现`：带 attribution 和验证状态的评审发现、诊断结论。

其中 `发现` 必须配套 qualifier：

```json
{
  "assertion_status": "suspected | confirmed | rejected",
  "attributed_to": "reviewer | tool | user | assistant"
}
```

只有 `confirmed` 才可作为普通当前事实参与强冲突；`suspected` 应是短 TTL 或独立候选通道。不要新增泛化的
`意见` predicate，它会把大量聊天判断永久化。

`能力` 可暂由 `事实 + fact.capability`（若 registry 已有对应 attribute）表达；若 benchmark 显示能力事实占比高且
经常误入 `事实`，再新增 `能力`。不建议新增 `进度` 或 `执行状态` predicate，因为当前目标正是过滤这类内容，
需要保留的项目进度可用 temporal `计划/状态` 且受严格准入门控制。

#### 5.3 scope 不应承担来源真实性

不要用 `temporal` 容纳“可能是错的评审意见”。时间有效期、言语行为和证据强度是三个维度：

- scope：何时有效；
- predicate / assertion status：是什么类型的主张；
- confidence：当前 evidence 支持程度。

把 suspected finding 标 temporal 并不能阻止它被当事实召回。

## 推荐的新 Prompt 结构

建议将 system prompt 重排为以下短节，删除重复的中英 scope 说明和末尾重复 enum：

1. `ROLE AND OUTPUT CONTRACT`
2. `STEP 1 — GENERATE CANDIDATE PROPOSITIONS`
3. `STEP 2 — SPEECH ACT AND ADMISSION GATE`
4. `STEP 3 — ATOMIC, SELF-CONTAINED VALUE`
5. `STEP 4 — SUBJECT / PREDICATE / SLOT`
6. `STEP 5 — SCOPE THEN VOLATILITY`
7. `STEP 6 — EVIDENCE CONFIDENCE`
8. `EXCLUSIONS`
9. `CONTRASTIVE FEW-SHOTS`
10. `JSON SCHEMA CONSTRAINTS`

核心指令可压缩为：

> 只输出由当前事件直接支持、会改变未来回答或行动、且脱离上下文仍具体自足的命题。先判断言语行为：
> asserted/committed 可进入；reported 仅在有未来效用并保留时点时进入；proposed/hypothetical/procedural/phatic
> 不进入。评审建议不是事实，工具快照不是长期记忆，assistant 复述不产生新 claim。对每条准入命题先原子化，
> 再选择 subject、predicate 和唯一可确定的 slot；随后独立判断 scope 与 volatility；最后按证据等级从
> 0.98/0.90/0.75/0.55 中选择 confidence。低于 0.50 不输出。

## 评估与上线顺序

虽然本提案不包含实现，后续验证应按单变量实验进行：

1. **A：仅替换 admission gate + 过程噪声反例**  
   主指标：过程性噪声率从 30% 降至 ≤10%；关键长期事实 recall 不下降超过 5 个百分点。
2. **B：在 A 基础上加入四象限 scope/volatility 规则**  
   主指标：二者联合准确率 ≥85%，分别报告 scope 和 volatility，不再只报单一合格率。
3. **C：在 B 基础上加入离散 confidence 锚点**  
   主指标：最高档占比 <50%，各档 precision 单调上升；用 Brier score / ECE 观察校准，但在有足够 gold 前把
   分数视为 ordinal level。
4. **D：在 C 基础上评估 predicate 扩展**  
   主指标：predicate 准确率 ≥85%，`事实` fallback <20%，评审意见误标事实 ≤5%。

gold set 必须单独标注：

- admission / rejection reason；
- speech act；
- semantic subject；
- predicate 与 canonical attribute/slot；
- scope；
- volatility；
- evidence level；
- 原子 value；
- source role/kind 与 occurred_at。

正负样本至少覆盖 user、assistant、tool result、status report、historical report、code review、明确决策和时间计划。
评估时同时报告 claim-level precision/recall 与 event-level false-positive rate，尤其单列“过程状态、CI/健康快照、
未确认评审意见”三类。

## 最终建议

优先做 prompt 结构重排、言语行为准入门、scope/volatility 四象限示例和离散 confidence 锚点；这四项不要求立刻
改变存储 schema，直接对应当前四个质量缺陷。predicate 扩展应作为后续独立变量，其中 `决策`、`约束` 的收益较高，
`发现` 必须与 attribution/assertion status 一起设计，不能只加一个枚举值。

最关键的设计原则是：**原始事件可以被保存为证据，但只有被当前证据明确支持且对未来有行动价值的命题，才应成为
claim。**
