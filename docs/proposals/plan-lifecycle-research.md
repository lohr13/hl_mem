# 计划类记忆生命周期调研

## 结论

建议实现，但只实现一个低侵入的“结果证据使计划失效”闭环，不建设通用任务编排状态机。

`predicate='计划'` 的 Claim 本质上仍是一个带双时间和证据链的事实：“某主体在某时点有此计划”。计划完成后，它不应被物理删除，也不应继续作为当前事实召回；正确动作是关闭其当前有效区间，并建立“计划由哪个结果兑现”的证据关系。首版只需识别 `completed`，不引入 pending/in_progress/blocked/cancelled/failed 等完整状态集合。

推荐优先级为 P1。当前问题会直接污染 current-state 召回，而且 hl_mem 已具备双时间、Claim 失效、证据链、关系和后台 worker 等大部分基础设施。增量成本可控，收益比继续缩短 TTL 更明确。若实现范围扩张到任务管理、进度跟踪或多级状态机，则应立即暂缓。

## 调研范围与共同观察

本次查看了 Mem0、Letta、Zep/Graphiti、LangMem 的官方文档。四者都能更新、删除或失效过时记忆，但都没有面向“承诺/计划完成”的开箱即用任务生命周期模型。它们解决的是通用记忆一致性，是否把一句结果解释为某个计划的完成，仍取决于应用 schema、抽取提示或代理行为。

| 系统 | 相关能力 | 对计划/承诺的实际处理 | 对 hl_mem 的启示 |
|---|---|---|---|
| Mem0 | 记忆 CRUD、历史、自动抽取后的 ADD/UPDATE/DELETE | 可由新对话更新或删除旧记忆，但官方公开模型没有计划状态或 plan→result 关系 | 通用 CRUD 足够支撑人工/LLM 修正，却不能保证计划自动收敛 |
| Letta | Agent 可编辑 memory block；archival memory 可检索；block 可更新、分离、删除 | 计划通常是 block/scratchpad 中的文本，由 agent 自己改写；没有事实级时态失效或完成状态 | 对少量高频工作记忆很简单，但依赖 agent 自律，不适合 hl_mem 的可审计事实层 |
| Zep/Graphiti | 时态知识图；事实边有 `valid_at`、`invalid_at`、`created_at`、`expired_at`；新 episode 可使旧事实失效 | 最接近所需能力：新事实经抽取/矛盾判断使旧边失效并保留历史；仍没有专门的计划状态机 | 复用双时间和失效语义，比另建 task 表/状态机更自然 |
| LangMem | Memory manager 根据新对话 create/update/delete；删除可配置；应用决定软删、硬删或降权 | 可提示 LLM 删除“不再有效”的计划，但是否完成及如何保留证据由应用定义 | 背景收敛器 + 受控删除/失效是合理模式，首版不必做复杂 workflow |

### Mem0

Mem0 的官方 REST 能创建、搜索、更新、删除记忆并查询历史；更新文档把典型场景描述为用户改变偏好或澄清事实后替换旧内容。这个抽象是“内容过时后的 CRUD”，没有承诺、计划、完成结果之间的领域关系，也没有计划专用终态。因而 Mem0 可以承载“把计划改成已完成”，但完成识别仍需调用方或抽取模型决定。

参考：

- [Mem0 REST API Server](https://docs.mem0.ai/open-source/features/rest-api)
- [Mem0 Update Memory](https://docs.mem0.ai/core-concepts/memory-operations/update)

### Letta

Letta 的核心记忆是始终放在上下文中的 memory block。默认情况下 agent 可通过工具自行改写；外部程序也能整体更新或删除 block。较低频信息可放 archival memory，由 agent 写入和搜索。该设计很适合维护一个“当前计划/工作区”文本块，但它是 agent-managed working memory，不提供事实级计划到结果的自动关联、双时间失效或状态收敛。

这说明单用户场景可以采用简单模型，但 Letta 的“让 agent 自己维护一段文本”不满足 hl_mem 已有的事件溯源和证据审计目标。

参考：

- [Letta Memory Blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)
- [Letta Context Hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)

### Zep / Graphiti

Graphiti 把 episode 作为摄入事件，把事实作为时态边。新数据到来时，系统会判断是新增还是更新已有边；当新事实使旧事实不再成立时，旧边记录 `invalid_at`，同时保留历史和来源。Zep 文档明确区分事实何时成立/失效，以及系统何时获知这些变化。

这是最接近 hl_mem 的参照：计划完成并不需要把 Claim 变成一个复杂 Task 对象，只需要把“计划仍待完成”视为当前有效事实；结果出现时关闭有效区间，并链接使它失效的结果证据。

参考：

- [Graphiti Overview](https://help.getzep.com/graphiti/getting-started/overview)
- [Zep Facts and Fact Invalidation](https://help.getzep.com/facts)
- [Graphiti Adding Episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)

### LangMem

LangMem 的 memory manager 会结合新对话和已有记忆，让 LLM 创建、更新或移除记忆。`enable_deletes` 默认关闭；开启后，过时或矛盾信息可产生 `RemoveDoc`。其 functional API 特别允许应用把 removal 实现成硬删、软删或降权。

因此 LangMem 提供的是可配置的“LLM 记忆整理器”，不是计划状态机。对 hl_mem 更有价值的借鉴是：在后台、带候选集地做收敛，并把最终失效语义留给已有领域模型，而不是让 LLM 直接任意改库。

参考：

- [LangMem Semantic Memory Extraction](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)
- [LangMem Memory API](https://langchain-ai.github.io/langmem/reference/memory/)
- [LangMem Core Concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)

## hl_mem 是否需要状态机

当前不需要。

计划管理系统通常需要 pending、in_progress、blocked、completed、failed、cancelled、reopened，以及负责人、依赖、截止期和幂等命令。hl_mem 的目标是记忆，不是任务执行器。单用户场景的真实需求只有：

1. 当前召回不再把已兑现计划当作待办；
2. 历史查询仍能看到当时的计划；
3. 能解释它为何失效、由哪个结果兑现；
4. 不让一次含糊的“做完了”误关多个计划。

双时间 Claim + 证据关系已经覆盖前三项。第四项需要谨慎匹配和审计，而不是更多状态。

`cancelled`、`failed` 等语义未来若有真实数据需求，可以同样作为“终止该计划当前有效性”的结果类型渐进加入；没有必要现在预建完整状态图。

## 推荐方案

### 数据语义

- 保留原计划 Claim，不物理删除。
- 结果 Claim 仍走正常摄入和抽取，不建立第二条写入管线。
- 匹配成功后关闭计划的当前有效区间，使其不再进入 current-state 召回。
- 建立 plan→result 的可审计关系，关系语义建议为 `fulfilled_by`；若现有关系枚举不适合，再单独评审是否扩展，不能用无语义的字符串绕过领域约束。
- 结果证据、匹配版本、匹配方式和置信度进入审计记录。历史查询应能同时看到计划、结果和失效时间。

### 后台收敛流程

1. 仅在新摄入内容产生“已完成/已发布/已部署/已交付/已修复”等结果型 Claim 时触发候选查找。
2. 候选仅限 active 的 `predicate='计划'` Claim，并先按同一主体、项目/对象限定符、时间方向和强锚点过滤。
3. 强锚点包括版本号、项目名、工件名、issue/任务标识等。像 `v0.17.2` 这种唯一标识可确定性匹配。
4. 只有候选数大于一或文本语义有歧义时才调用 LLM 四分类判断：`fulfilled / unrelated / ambiguous / cancellation_or_failure`。
5. 首版仅自动执行高置信 `fulfilled`；`ambiguous` 只写审计候选，不改变 Claim。
6. 所有变更在单事务中完成：关闭计划有效区间、写关系、写审计。重跑必须幂等。

不建议每晚扫描全部计划。事件驱动只检查新结果附近的少量候选，成本更低，也更容易追溯。

### 召回保护

即使后台判断尚未执行，current-state 召回也不应仅依赖 3/7/14 天 TTL。可在结果已明确链接时硬过滤已兑现计划；未链接的计划仍按现有时态规则返回，避免查询阶段临时调用 LLM 或做不可审计推断。

### 配置与发布策略

沿用项目已有的 audit/observe/enforce 渐进模式：

1. `audit`：只生成候选与判断，不关闭计划；
2. 离线检查误匹配，重点覆盖同名版本、多个并行计划、部分完成和否定句；
3. `enforce`：仅自动执行确定性强锚点或高置信单候选；
4. 模糊案例继续依靠 TTL 或人工纠正。

阈值、候选上限、超时和模式必须由 Settings/环境变量注入。外部 LLM 判断复用统一 retry、超时和 span，不新增独立客户端。

## 成本收益与明确不做项

### 收益

- 修复 current-state 召回中的直接错误，而不牺牲历史可见性；
- 复用现有双时间、证据、关系、worker 和审计基础设施；
- 对版本发布、部署、交付、修复等强锚点计划，准确率预期较高；
- 事件驱动候选过滤后，额外 LLM 成本只发生在少量灰区。

### 主要成本与风险

- 自然语言中的“完成”可能只表示部分完成，误关计划比漏关更危险；
- 结果主体与计划主体可能不同，实体归一化错误会造成漏匹配；
- 计划粒度不一致，例如“发布版本”包含文档、打包、部署多个子结果；
- 增加关系类型或终止原因若没有真实查询需求，容易演变成任务系统。

### 首版明确不做

- 不新增 tasks 表；
- 不做通用工作流引擎或完整状态机；
- 不跟踪百分比进度、依赖、负责人和 deadline；
- 不对模糊的“完成了”自动关单；
- 不在召回热路径调用 LLM；
- 不用更短 TTL 假装解决语义完成问题。

## 最终判断

应该实现一个窄范围首版，因为问题真实、用户可见，且 hl_mem 已有约束良好的落点。推荐“结果事件触发 → 小候选集匹配 → audit 先行 → 高置信时态失效 + fulfilled_by 证据关系”。

若实现评审中发现必须新增 task 聚合、五种以上状态或跨计划依赖，说明边界已经偏离记忆系统，应暂缓该扩展，只保留 audit 候选与现有 TTL。
