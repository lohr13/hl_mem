# Extraction Pre-filter 设计

## 问题分析

2026-07-26 对 `var/hl_mem.db` 做了只读分析。快照包含 4,139 个 events 和 1,384 个
`extract_event` jobs（任务书记录分析开始前为 1,379）。现有 audit 保留窗口内有 701 次完成的
extraction 判定：344 次返回 claims，254 次返回空，103 次报错。任务书覆盖的更长时间窗口中，
865/1,379（63%）为返回空的 LLM 调用。

空提取并非主要由 extractor 能力不足导致。抽样和分组显示，空结果集中在以下内容：

- agent 的编排话术，例如“让我检查”“继续等待”“跑一下测试”；
- process poll/kill、timeout、background-started、clarify 等工具控制帧；
- runtime 注入的后台进程终止通知；
- 没有业务语义的工具协议回执。

反例同样重要：宽泛地过滤 tool actor、短文本或代码块会漏掉版本、配置、路径、测试结果和架构
决策等真实事实。audit 窗口中 tool 消息既有 122 次空结果，也有 61 次非空结果；因此不能用
actor_type 或“包含代码”作为单独拒绝条件。现有 `EventFilter` 是通用入口合法性/低价值过滤，
而本功能专门判断是否值得支付一次 extraction LLM 调用。

### 特征差异与风险口径

有效信号不是“文本长”，而是持久语义：显式记忆、偏好、约束、身份、配置、决定、版本或明确的
状态变化。低价值信号是 transport/control 语义以及描述下一步动作、但没有动作结果的短消息。

离线评估必须按 event 去重，并至少报告：

- `empty_reduction = 被跳过的空提取 / 全部空提取`，目标不低于 50%；
- `fact_loss = 被跳过且人工确认包含真实事实的 event / 全部有效事实 event`，目标低于 5%；
- 各 reason 的 skipped、false-negative 数量和置信区间。

“LLM 返回 claims”只能作为弱标签，因为生产样本中控制消息也曾被误提取成运行状态。发布前应对
所有命中规则的非空样本和随机空样本人工复核。当前规则覆盖任务书数据中的主要空结果簇；audit
按 reason 记录，可在其他部署上先观察再开启。

对 audit 保留窗口的 598 次成功调用做逐条回放，规则命中 127/254 个 `no_claims`，即 50.0%。
同时有 90/344 个 `claims` 弱标签被命中；复核显示其中混有大量从 control frame 误提取的路径、
进程状态和执行参数，因此该比例不能等同于真实事实损失。由于当前生产数据没有独立人工 gold
label，不能诚实地宣称已经从该快照证明 `<5%`；默认 off、durable keep-signal、逐 reason audit
和启用后的人工抽检共同构成安全上线条件。

## 方案比较

### 确定性规则（采用）

成本接近零、结果可解释、无外部依赖，适合默认关闭后由部署方显式启用。缺点是语言和 agent
协议会演进，因此规则必须保守、带 keep-signal、按 reason 可评测，不能成为无限增长的用户专属
关键词表。

### 轻量 LLM 分类

比固定规则更能理解语义，但仍产生一次模型调用、延迟、provider 配置和失败面。使用较小模型会
引入新的召回损失，使用同级模型又无法实现主要成本目标。本期不采用。

### Embedding 分类

可批量、延迟低于生成模型，但需要标注集、模型一致性、阈值校准和额外向量调用。不同语言、行业
和 embedding provider 的迁移风险高，不适合作为开源默认能力的首版。

## 最终设计

`ExtractionPreFilter` 是 `src/hl_mem/ingest/pre_filter.py` 中的纯本地分类器。它返回
`PreFilterDecision(should_extract, reason)`，不修改 event，不访问数据库，不调用外部服务。

判定顺序：

1. `explicit_memory`、含图片的事件和命中 durable-memory cue 的文本始终允许；
2. 拒绝确定性的 runtime notice；
3. 拒绝 process/clarify/单行 tool wrapper、timeout、killed、background-started 等 control frame；
4. 拒绝以动作/等待/检查为主体且长度受限的 assistant narration；
5. 其他内容允许，交给正常 extractor。

集成点位于 Worker 已完成内容解析/图片描述及现有 `EventFilter` 之后、token budget 与 extractor
之前。这个位置有四个性质：

- 原始 event 已经持久化，跳过不会破坏事件留存；
- 尚未消费 LLM token；
- 图片描述等现有预处理语义保持不变；
- job 正常成功完成，不产生无意义重试。

开启时，每次判定写 `phase=extraction_pre_filter, action=evaluated` audit。跳过使用
`outcome=skip`，detail 只含 reason、event_type、actor_type、content_chars 和规则版本，不保存
正文。分类器抛出任何异常时，Worker 写 `outcome=error_fallback`，然后继续正常 extraction；
audit 自身仍沿用 best-effort 语义。

## 配置

```text
HL_MEM_EXTRACT_PRE_FILTER=off  # 默认；保持历史行为
HL_MEM_EXTRACT_PRE_FILTER=on   # 启用确定性预筛
```

仅接受 `on` 或 `off`（大小写不敏感）。非法值在 Settings 启动校验时报出具体配置错误。healthz
的非敏感 settings snapshot 暴露 `extract_pre_filter`，便于确认部署状态。

## 风险与缓解

- **漏掉短但重要的事实**：显式记忆、图片和 durable-memory cue 优先放行；不使用单纯长度、
  actor 或代码比例规则；默认关闭。
- **agent/tool 协议变化**：每个跳过都记录稳定 reason 和规则版本，部署方可按 reason 回放；
  未识别格式默认允许。
- **规则异常阻断提取**：捕获预筛异常并 audit，随后走原 extraction。
- **audit 影响证据链**：audit 只记录决策；event 不删除，已有 evidence link 不改写。
- **单一用户过拟合**：规则描述通用的 control/runtime 协议和动作叙述，不包含项目名、用户名、
  本机路径或某个 provider 专属业务事实。
- **指标漂移**：开启前后按周比较 extraction `no_claims`、pre-filter skip reason 和人工抽检；
  fact_loss 达到 5% 时立即关闭开关并收窄相应规则。
