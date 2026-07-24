# Phase 17 设计审查

> 审查日期：2026-07-24
> 审查范围：`phase17-design.md`、两份前置分析报告，以及当前 claims 提取、冲突、去重、TTL、配置、存储和写入链路。
> 约束：本文只做设计审查，不修改代码。

## 结论摘要

Phase 17 的总方向正确：`canonical_attribute` 必须拆成“可操作槽”和“开放检索标签”，TTL 与低 importance
治理也应统一到一个生命周期策略中。但当前方案需要调整后才能实施。

| 问题 | 评估 | 核心结论 |
|---|---|---|
| 方案 E：slot + tags | **需调整** | 字段拆分可行；现有回填规则不安全，slot 集合也缺少实例限定和准入标准 |
| 跨 subject 去重 | **需调整** | `0.92` 只能作为候选阈值，不能作为自动合并依据；LLM 不能同步放进当前写事务 |
| TTL 三因子 | **需调整** | 应集中为纯策略函数；importance/scope 变化时必须从原始时间锚点重算，而非增量更新 |
| 低 importance 门槛 | **否决当前全局 0.3 硬门槛** | 应采用保护类型例外、`<0.2` 硬拒绝、`0.2–0.3` 短 TTL/audit-only 的分层策略 |
| 两批实施顺序 | **否决当前拆分** | Batch 1 已改变写入契约却把下游适配留到 Batch 2，会产生半迁移状态；建议改为四阶段发布 |

另外，设计文档引用的 `src/hl_mem/domain/claims/ttl.py` 当前不存在；TTL 计算实际内嵌在
`application/ingest.py::_build_claim_drafts`，过期执行位于 `workers/ttl.py`，配置同时散落在
`config.py` 和 `settings.py`。Phase 17 应新建集中策略模块，不能按不存在的文件直接改造。

## 1. 方案 E（slot + tags 分离）

### 1.1 可行性

方案 E 在当前架构中可行，而且是比继续扩充 `fact.*` 更稳健的长期形态：

- `predicate` 保留高层语义路由；
- `canonical_slot` 只表达能够精确更新、冲突或限定生命周期的槽；
- `topic_tags` 表达可多选的主题和事实角色，不进入 conflict key，也不作为去重硬隔离条件；
- 无稳定槽的开放事实使用 `canonical_slot = NULL`。

真正的验收目标不应是“slot 非空率 > 40%”。非空率会激励模型过度分类，反而降低冲突安全性。应改为：

- operational slot precision 达到预先约定值（建议离线标注集 `>= 0.95`）；
- NULL/abstain 校准准确；
- slot 引入后 conflict false-positive 不上升；
- tags 的检索收益通过离线 recall 指标验证。

### 1.2 migration 数量与边界

方案 E 本身建议使用 **一个不可变 SQL schema migration + 一个版本化、幂等的数据迁移任务**，不要把复杂回填塞进
多个 SQL migration，也不要修改已有 `001–015` migration。若按本文建议为跨 subject 去重新建专用审计表，则完整
Phase 17 最终共有 **两个 SQL migration**，分别随 Stage 1 和 Stage 3 发布。

1. `016_claim_slots_and_tags.sql`
   - 增加 `canonical_slot TEXT NULL`；
   - 增加 `topic_tags_json TEXT NULL`，命名与现有 `value_json/qualifiers_json` 一致；
   - 增加有界查询需要的部分索引，例如
     `(namespace_key, canonical_slot, status)`，只索引 `canonical_slot IS NOT NULL`；
   - 保留 `canonical_attribute`、`conflict_key`、`legacy_conflict_key`，Phase 17 不删除旧列。
2. 新增类似 `backfill_conflict_key_v2.py` 的版本化 Python 数据迁移：
   - 固定一份迁移时的 slot registry snapshot，不能导入未来会变化的在线本体；
   - 分批、幂等回填 `canonical_slot/topic_tags_json`；
   - 输出计数、拒绝原因和抽检样本；
   - 首次只 dry-run，显式启用后才 apply。

方案 E 的数据库 schema 只需这个新 migration。数据回填应有独立版本标识，但不必为回填再建 SQL migration。第二个 SQL
migration 仅用于 Stage 3 的 `dedup_pairs`，不是 slot 回填的一部分。只有未来确认所有消费者都已停止读取
`canonical_attribute` 后，才另开后续 phase 做破坏性清理；不属于 Phase 17。

### 1.3 回填规则

原设计的“`fact.other → NULL + ["fact"]`，其他非 `.other → slot=原值`”不安全。现有 54 类中大量值本质上是
tag，而不是 operational slot，例如：

- `fact.implementation/issue/cause/resolution/constraint`；
- `plan.goal/decision/migration/evaluation`；
- `preference.architecture/workflow`；
- `state.deployment/test_suite/process`（是否可成为槽还取决于限定对象和生命周期用途）。

把它们原样变成 slot 会继续保留旧本体的职责混杂，并可能让原本无关的事实进入同一冲突池。

安全回填规则应为：

1. 只有在新 registry 明确列出的旧值，才回填为 `canonical_slot`；
2. `.other`、`custom.unknown` 和不在 registry 中的旧值全部回填为 `NULL`；
3. 所有旧值都转换成保语义 tags：
   - `fact.implementation → ["fact", "implementation"]`；
   - `plan.migration → ["plan", "migration"]`；
   - `fact.other → ["fact"]`；
4. 原 `canonical_attribute` 列继续原样保留，保证没有不可逆语义丢失；
5. tags 做 NFKC、lowercase、去重、稳定排序和数量/长度限制，不接受回填时自由生成的新词；
6. 回填不得重算或覆盖现有 conflict key。新 key 必须在行为切换阶段另行生成，并保留旧 key 供审计。

这样回填不会丢失旧分类信息。仅把 `fact.other` 变成 `["fact"]` 并没有新增语义损失，因为旧值本来就没有更细信息；但应继续保留
旧列和 evidence，避免未来无法追溯。

### 1.4 canonical slot 集合

设计中的约 15 个候选不是都能直接作为“全局单值槽”。最大问题不是数量，而是**槽实例缺少限定键**：

- 一个系统可以同时使用多个 tool/model/provider/database；
- 不同服务可以有不同 port；
- `path` 必须区分用途或配置项；
- `env` 必须区分变量名；
- `network` 必须区分 endpoint、route 或目标；
- 一个项目可以有多个 plan，每个 plan 各有 deadline。

建议保留以下候选 registry，但为每项声明用途，而不是默认全部互斥：

| 候选 slot | 建议 | 必需限定 |
|---|---|---|
| `preference.ui_theme` | 保留，互斥 | context/channel 可选 |
| `preference.response_style` | 保留，通常互斥 | channel/context 可选 |
| `preference.tool_choice` | 保留，但不默认全局互斥 | task/workflow |
| `choice.database` | 保留 | project/component/role |
| `choice.model` | 保留 | component/purpose |
| `choice.provider` | 保留 | component/purpose |
| `choice.memory_system` | 保留 | project/agent |
| `choice.tool` | 暂不作为互斥槽 | 更适合作为 tag，除非有明确 role |
| `config.port` | 保留 | service/component + port role |
| `config.path` | 保留 | config key/purpose |
| `config.env` | 保留 | env variable name |
| `config.network` | 拆分或严格限定 | endpoint/routing target/network key |
| `state.service_health` | 保留、短 TTL | service/component |
| `identity.name` | 保留 | subject identity |
| `plan.deadline` | 保留 | plan/goal identifier |

因此可接受的首版不是“15 个平面互斥槽”，而是约 13–15 个**带 policy 和 qualifier requirements 的 registry
entry**。registry 至少包含：合法 predicate、是否参与 conflict、dedup 边界、TTL class、必需 qualifiers、定义、反例和 aliases。
`MUTUALLY_EXCLUSIVE_SLOTS` 应并入该 registry，避免两份集合漂移。

### 1.5 prompt 与 schema 的具体重写

prompt 应从“列举若干例子”改成“字段职责 + 完整枚举 + abstain + 边界示例”。建议核心内容如下：

```text
每个 claim 必须包含：
- predicate：高层语义类型；
- canonical_slot：可为 null。只有事实表示一个可精确更新的单值/限定槽时才填写；
- topic_tags：0–5 个受控标签，可多选，只描述主题或事实角色；
- value、qualifiers、confidence、volatility、scope、importance、reason。

canonical_slot 不是主题分类。architecture、decision、requirement、implementation、
bugfix、dependency、migration 等只能放入 topic_tags，不能作为 slot。

如果无法确定唯一 operational slot，必须返回 null；不要猜测，不要使用 other/unknown。
填写需要 qualifier 的 slot 时，必须同时给出实例限定：
- config.port：service/component 与 port_role；
- config.env：env_key；
- config.path：path_role/config_key；
- plan.deadline：plan/goal；
- choice.model/provider/database：component/purpose。

完整合法 slot：
<由同一 slot registry 生成 enum、中文定义、必需 qualifier、正例和反例>

topic_tags 合法值：
architecture, decision, requirement, implementation, bugfix, behavior,
dependency, version, migration, constraint, capability, operation ...
复合事实允许多个 tags；没有合适标签时返回 []，不得创造新标签。
```

还需同步修改 structured-output schema：

- `canonical_slot` 使用 `enum + null`，不能只用正则；
- `topic_tags` 使用 `array`、`uniqueItems=true`、`maxItems`，items 使用受控 enum；
- 旧 `canonical_attribute` 只在兼容解析路径出现，不再要求新响应生成；
- `_parse_legacy_defaults` 对旧响应映射到新字段，但必须记录 audit，不能默认制造 `fact.other` slot；
- `normalize_scope` 和 `_is_low_value_claim` 改读 slot registry/predicate，而不是字符串前缀；
- `SYSTEM_PROMPT`、JSON schema 和本地验证必须由同一 registry 生成，防止三套定义漂移。

## 2. 跨 subject 去重

### 2.1 `0.92` 是否安全

`0.92` 作为**候选召回阈值**偏保守，可以用于首版 audit-only；作为自动合并阈值仍不安全。典型误合并包括：

- “xray 监听 10808”与“API 服务监听 10808”；
- “使用 qwen3.7-plus”与“不使用 qwen3.7-plus”；
- 同文本但不同项目、environment、版本或时间窗口；
- 一个是规则/计划，一个是已经发生的事实；
- 值中只差端口、版本、路径或否定词。

另一个实现问题是当前 embedding 文本为
`subject + predicate + value`。不同 subject 会主动降低相似度，因而 `0.92` 的真实召回率未知。跨 subject 去重必须使用新的
subject-independent 表示：

```text
predicate + canonical_slot + normalized value + exclusive qualifiers
```

这意味着不能直接假设现有 `embedding_dense` 的 `0.92` 分布可复用。应在固定标注集上分别评估 `0.90/0.92/0.95` 的
precision-recall，再定默认值。上线默认建议 `0.92 + audit-only`，自动动作还必须满足：

- predicate 相同；
- 两边均无 slot，或 slot/qualifier 明确兼容；
- namespace 相同；
- exclusive qualifiers 一致；
- 数字、端口、版本、路径、日期和否定极性无冲突；
- 无 change/supersede 信号；
- LLM 明确判为 `equivalent` 且置信度达到独立阈值；
- claim 在应用前仍未变化（CAS）。

### 2.2 LLM 二次确认的成本与延迟

当前 `LLMConflictJudge` 每对候选执行一次远程调用。即使候选很少，也会增加一次模型调用的 token 成本和网络延迟；当前
LLM timeout 为 90 秒，失败重试还会扩大尾延迟。

更严重的是，`IngestService.store_extracted` 在 `_find_resolution` 前开启 `BEGIN IMMEDIATE`。如果把 LLM 二次确认直接放进
`Deduplicator.find_duplicate`，SQLite 写锁会跨越远程调用，阻塞所有写入。这一实现方式应明确禁止。

建议：

1. 写入同步路径只做 fact hash、同 subject 去重和确定性安全检查；
2. 跨 subject 候选写入审计/任务，由 background consolidation 批处理；
3. 先 `audit-only`，不改变 claim 状态；
4. 通过 precision 验收后，再允许 evidence-preserving merge；
5. 对重复项使用 supersede/专用 merged 语义，绝不物理删除；迁移 evidence link，并保留审计原因。

如果业务强制要求写入前判重，则必须在事务外先生成候选和完成 LLM 判断，再在短事务内以 claim version/status 做 CAS；当前代码没有
claim version，复杂度和竞态风险明显更高，不推荐首版采用。

### 2.3 consolidation 能否复用

可以复用部分基础设施，但不能原样复用：

- 可复用：
  - `CandidatePair`、pair key、CAS 检查、batch/dry-run 框架；
  - `consolidation_pairs` 的审计思想；
  - worker 调度和 LLMClient。
- 不能原样复用：
  - 当前 judge 只有 `contradiction/compatible/state_change/unrelated`，没有 `equivalent`；
  - 当前候选规则要求同 slot 或同 subject，不能覆盖“无 slot + 跨 subject”；
  - 当前灰区只扫描 `0.72 <= similarity < 0.95`，会排除 `>=0.95` 的高置信重复；
  - 当前 embedding 包含 subject；
  - `embedding_signature` 只拼模型名，未区分 embedding 文本版本、dedup policy 或 judge schema；
  - `consolidation_pairs.decision` 混合冲突与重复语义后难以统计。

推荐新增专用 `DedupJudge`（输出 `equivalent/distinct/uncertain`），并给审计记录增加
`purpose/policy_version/embedding_text_version`。若要继续使用 `consolidation_pairs`，需要新 migration 增加这些字段并修改主键/唯一性语义；
相比之下，新建 `dedup_pairs` 更清楚，避免破坏已有冲突归并历史。首选新表。

## 3. TTL 三因子

### 3.1 策略模型

原设计称为 scope + volatility + importance 三因子，但给出的矩阵实际只使用 scope + importance band，并说 volatility
只影响初始 importance。这会让两个概念耦合且名称不一致。

建议明确：

- `scope` 决定是否进入 TTL；
- `importance` 决定 temporal 的保留档位；
- `volatility` 只作为覆盖项或 reason，例如 transient state 可比普通 temporal 更短；
- `valid_to` 是显式业务有效期，优先级高于推断 TTL；
- `expires_at = min(valid_to, anchor + policy_ttl)`（忽略空值）。

新建 `src/hl_mem/domain/claims/retention.py`，提供纯函数，例如
`compute_expiration(scope, volatility, canonical_slot, importance, valid_to, observed_at, recorded_from, policy)`，
返回 `expires_at` 和稳定的 reason code。设计中不存在的 `domain/claims/ttl.py` 应改为该集中模块。

高 importance temporal 首版建议 14 天而不是 30 天，除非标注集证明 14 天召回不足；计划类有明确 deadline 时由
`valid_to/deadline` 控制。30 天不是错误，但会明显削弱本 phase 清理 temporal 噪声的目标。

### 3.2 importance 或 scope 改变时如何更新

必须**重新计算绝对 expires_at**，不能在旧 `expires_at` 上增减天数。增量更新会因多次重分类产生漂移，也无法解释最终时间。

统一规则：

1. anchor 优先使用可信 `observed_at`，否则 `recorded_from`；
2. 用新 scope/importance/slot 和同一 anchor 从头计算；
3. 若新结果早于当前时间，不在 `update_classification` 内直接提交过期，而是标记/返回 `due_now`，由同一受控事务或 TTL worker
   执行状态转换；
4. temporal → permanent：清空策略生成的 `expires_at`，但不得覆盖更早的显式 `valid_to`；
5. permanent → temporal：立即生成 expires_at；
6. importance 升高：允许延长策略 TTL，但不复活已经 expired/superseded/retracted 的 claim；
7. importance 降低：允许缩短，进入 grace/audit 队列后再过期；
8. `ClaimRepository.update_classification` 必须同时更新 scope、importance、expires_at 和 expiration reason，保证原子一致。

为区分显式截止与策略 TTL，建议增加 `expiration_reason`/`ttl_policy_version`，或至少写审计日志；否则后续无法安全判断哪些
`expires_at` 可以因重分类而清空。

### 3.3 permanent + high importance

`scope=permanent` 默认无 TTL 是合理的；是否 high importance 不应改变这一点。永久表示事实有效期，importance 只是价值等级。
但它不是“永不治理”：

- 新事实仍可 supersede/retract；
- 明确 `valid_to` 仍然生效；
- 长期无访问可 archive，但不应自动判事实失效；
- `permanent + low importance` 应进入异常重分类/audit，而不是永久保存或直接删除；
- explicit memory、身份、明确偏好和安全约束应受保护。

因此矩阵中的 `("permanent", "any"): None` 可以通过，但描述应改成“无策略 TTL”，而不是“永不过期且只能人工 expire”。

### 3.4 bulk expire 风险

“回填后 age > TTL 立即 expire”风险过高。当前存量 scope/importance 本身来自旧 prompt，且 stable temporal 过去从未受 TTL
约束；一次性应用会把分类错误转化为批量数据损失。

安全流程：

1. 固定数据库 snapshot 和 policy version；
2. dry-run 只计算 proposed_expires_at、reason、would_expire；
3. 按 predicate/slot/importance/age 分层统计并人工抽检；
4. 排除 explicit memory、identity、preference、安全 constraint、有未完成 deadline/active relation 的 claim；
5. 先只回填 expires_at，不改 status，设置 3–7 天 grace period；
6. 分批执行过期，每批有 run_id、claim IDs、旧值和新值；
7. 过期只改状态与 `valid_to`，不删除 claim、embedding 或 evidence，保持可恢复；
8. 监控 recall miss、恢复率和 supersede/expire 比例后再扩大批次。

同时必须修改 `workers/ttl.py`：到期应以 `expires_at` 为唯一条件，去掉
`volatility='ephemeral'`，否则新矩阵为 stable temporal 生成了时间也不会执行。

## 4. 低 importance 写入门槛

### 4.1 对 `0.3` 的判断

全局 `importance < 0.3` 直接丢弃不安全，原因有：

- importance 是 LLM 的非校准分数，`0.29` 与 `0.31` 没有可靠语义断点；
- explicit memory、身份、偏好、安全约束可能因模型打分偏低而被误删；
- 与当前 prompt 的档位有空洞：文本写 `0.0–0.3 incidental`，设计又把 `0.3–0.4` 定义为可写一次性记录；
- event 虽保留，但没有 claim 时普通 recall 不一定能恢复该信息；
- 一次性操作有时是重要证据，例如 migration 是否已执行、备份是否完成。

建议首版：

| 条件 | 动作 |
|---|---|
| 受保护类型：explicit memory、identity、明确 preference、安全/合规 constraint | 不因 importance 硬拒绝 |
| `importance < 0.2` 且非保护类型 | 不落 claim，保留 event，写 audit reason |
| `0.2 <= importance < 0.3` | 落 temporal claim，1–3 天 TTL；首版可 audit-only |
| `0.3 <= importance < 0.4` | 落 temporal claim，3 天 TTL |
| `importance >= 0.4` | 按统一 TTL policy |

上线前统计真实分布。如果 `<0.2` 样本过少或误拒绝率不可接受，门槛继续保持 audit-only。硬门槛应属于 Settings，并校验
`0 <= drop_threshold <= short_ttl_threshold <= 1`；保护类型属于领域 registry，不能由环境变量定义。

### 4.2 prompt 中的 importance 指南

应避免只给宽区间，改成“先判断保留价值，再用锚点和反例打分”：

```text
importance 表示这条 claim 对未来任务的价值，不表示语气强烈程度、事实置信度或变化频率。

1.0：用户明确要求“必须记住”的长期记忆。
0.9：核心身份、长期安全/合规约束、稳定且跨任务适用的明确偏好。
0.8：已采纳的重要架构决策、关键工具/模型/数据库选择、生产关键配置。
0.6：当前项目中未来数周仍会影响工作的计划、状态或一般事实。
0.4：短期但可能用于后续核对的运行结果、临时状态或一次性操作。
0.2：可从事件日志恢复、通常不会影响未来决策的机械步骤或中间结果。
0.0：闲聊、重复内容、无上下文数字、进度播报；不要生成 claim。

不要因为“必须/非常/严重”等情绪词自动提高分数。
同一事实的 importance 不因 stable/ephemeral 自动加减固定值；分别判断 scope、volatility 和 importance。
```

不建议采用“ephemeral 一律 importance -0.2”。变化快不等于不重要，例如生产事故或临时安全状态可能非常重要。三字段应独立
判断，再由 TTL policy 组合。

## 5. 实施顺序与依赖

### 5.1 原 Batch 1/2 的问题

当前拆分把“更新 claim draft 构建逻辑”放在 Batch 1，却把 conflict key、dedup、TTL 和 recall 的新字段适配放在 Batch 2。
这会产生以下半迁移状态：

- 新 claim 已写 `canonical_slot`，下游仍读 `canonical_attribute`；
- NULL slot 仍可能用 `custom.unknown/fact.other` 计算旧 conflict key；
- prompt 已改变输出契约，但 `ExtractedClaim`、Pydantic schema、FakeExtractor 和旧响应兼容路径未完整同步；
- 回填和在线新写入使用不同规则；
- Batch 1 已改变行为，却没有 feature flag 或回滚路径。

因此原两批拆分不合理。

### 5.2 修订后的四阶段计划

#### Stage 0：基线、契约和开关（无行为变更）

- 固定至少 200 条标注集，覆盖 `.other`、高频属性、跨 subject 重复、高风险数字/否定样本；
- 定义 slot registry、tag registry、qualifier requirements 和 policy version；
- 增加配置但默认关闭：
  - slot 写入/读取开关；
  - cross-subject audit/auto 开关；
  - importance drop audit/enforce 开关；
  - TTL backfill apply 开关；
- 验收当前与新策略的离线 precision/recall，记录基线。

#### Stage 1：加法式 schema 与双写（行为兼容）

- 应用 `016_claim_slots_and_tags.sql`；
- 新旧字段双写，所有线上行为仍读旧 `canonical_attribute/conflict_key`；
- 仓储完成 JSON tags 编解码；
- extractor schema 可接受新契约，同时保留明确审计的旧响应兼容；
- 运行 slot/tags 数据回填 dry-run，暂不改 conflict key、status 或 expires_at；
- recall 输出可携带新字段，但不改变过滤/排序。

#### Stage 2：slot 行为切换

- prompt 切换到完整 slot enum、tags 和 abstain；
- deterministic inference 只保留高 precision 规则；
- conflict、同 subject dedup、scope normalization、preference intent 改读 slot registry；
- 新 claim 的无 slot conflict key 为 `NULL`；旧 key 只用于兼容读取；
- 对比 shadow result 后逐步从旧字段切到新字段；
- 完成本阶段后再 bump `0.8.0`，不在 schema 刚加入时提前 bump。

#### Stage 3：跨 subject 去重（独立发布）

- 新建 subject-independent candidate representation 和有界仓储查询；
- 新建 `DedupJudge` 与 `dedup_pairs` 审计表；
- 先 background audit-only，评估 `0.90/0.92/0.95`；
- 通过误合并指标后才开启 evidence-preserving auto merge；
- LLM 调用始终在 SQLite 写事务外。

#### Stage 4：TTL 与 importance（独立发布）

- 引入 retention 纯函数和 Settings 参数；
- 新写入先启用统一 expires_at 计算；
- reclassify 原子重算 expires_at；
- TTL worker 去掉 volatility 限制；
- importance 门槛先 audit-only，再执行 `<0.2` 非保护类型拒绝；
- 存量回填按 dry-run、grace period、分批 expire 推进。

跨 subject 去重与 TTL/importance 没有强制代码依赖，Stage 3 和 Stage 4 在 Stage 2 稳定后可分别发布；但不要在同一批同时开启
自动 merge 和 bulk expire，否则数据指标无法归因。

## 6. 具体文件与函数改动计划

以下是修订后的实施清单，不代表本次审查已修改代码。

| 文件 | 函数/区域 | 修订内容 |
|---|---|---|
| `src/hl_mem/storage/migrations/016_claim_slots_and_tags.sql` | 新文件 | 加 `canonical_slot/topic_tags_json` 和候选查询索引；不删除旧列 |
| `src/hl_mem/storage/migrations/backfill_claim_slots_v1.py` | 新文件 | 固定 registry snapshot，幂等 dry-run/apply，保留旧属性 |
| `src/hl_mem/storage/database.py` | `_migrate` | 注册版本化数据迁移；避免每次启动无界全表重跑 |
| `src/hl_mem/domain/claims/attributes.py` | 全部本体常量与校验函数 | 改为 `SlotDefinition` registry；合并互斥集合；增加 nullable 验证、tag 验证、qualifier requirements |
| `src/hl_mem/ingest/schemas.py` | `ExtractedClaimSchema`、`extraction_response_json_schema` | 新增 nullable slot 和 tags enum；旧字段仅兼容 |
| `src/hl_mem/ingest/extractors.py` | `ExtractedClaim`、`FakeExtractor.extract` | 数据契约改为 `canonical_slot/topic_tags`；FakeExtractor 遵循同一 registry |
| `src/hl_mem/ingest/llm_extractor.py` | `SYSTEM_PROMPT` | 动态注入完整定义、abstain、qualifier 规则和 importance 锚点 |
| 同上 | `normalize_scope`、`_is_low_value_claim` | 不再依赖 `state./plan.` 字符串前缀和旧 attribute |
| 同上 | `_merge_chunk_claims` | merge key 包含 nullable slot 和规范化 tags |
| 同上 | `_parse_legacy_defaults`、`_claim` | 旧响应映射、审计 LLM/rule/final/reason；禁止默认制造 operational slot |
| `src/hl_mem/application/ingest.py` | `_build_claim_drafts` | 验证 slot/tags；调用 retention policy；importance 门槛返回明确结果而非静默丢弃 |
| 同上 | `store_extracted`、`_find_resolution` | 无 slot 不查冲突键；跨 subject LLM 不进入写事务；写审计/后台任务 |
| `src/hl_mem/domain/claims/conflicts.py` | `compute_conflict_key` | 接受 nullable slot，无 slot 返回 `None`；把必需 qualifier 纳入 slot instance key |
| 同上 | `ConflictResolver.resolve` | 读取 registry policy，只有明确 mutually-exclusive 的同实例 slot 才确定性冲突 |
| `src/hl_mem/domain/claims/dedup.py` | `Deduplicator.find_duplicate` | 拆 same-subject/cross-subject；返回 score/mode/reject reason；tags 不作硬隔离 |
| 同上 | 新 helper | subject-independent 文本、数字/版本/否定/qualifier 护栏 |
| `src/hl_mem/storage/claims.py` | `insert_claim`、`_decode_claim` | 编解码 `topic_tags_json` |
| 同上 | `find_active_for_dedup` | 改为 SQL 有界预筛，避免当前全 namespace 读取后 Python 过滤 |
| 同上 | 新 candidate query | 按 namespace/status/predicate/slot/limit 查询跨 subject 候选 |
| 同上 | `update_classification` | 与 expires_at/reason 原子更新 |
| `src/hl_mem/workers/consolidate.py` | `scan_candidates`、judge | 冲突 consolidation 保持原语义；不要硬塞 equivalent |
| `src/hl_mem/workers/deduplicate.py` | 新文件 | 专用跨 subject audit/judge/CAS/merge worker |
| `src/hl_mem/storage/migrations/017_dedup_pairs.sql` | 新文件（Stage 3） | 建专用 dedup pair 审计表，含 policy/text/judge version |
| `src/hl_mem/domain/claims/retention.py` | 新文件 | importance band、TTL policy、anchor 和 expiration reason 的纯函数 |
| `src/hl_mem/settings.py` | `Settings`、`from_env`、`_validate` | 去重阈值、scan limit、TTL 档位、importance 门槛和 feature flags；校验顺序关系 |
| `src/hl_mem/config.py` | TTL/去重常量 | 删除运行时可调常量；只保留 slot/tag 领域 registry 或纯算法默认 |
| `src/hl_mem/workers/ttl.py` | `expire_claims` | 以 expires_at 为准，移除 ephemeral 条件；保留状态守卫和 valid_to 收敛 |
| `src/hl_mem/workers/reclassify.py` | `reclassify_defaults` | 覆盖低 importance permanent、temporal 无 expires_at；更新分类时重算 TTL |
| `src/hl_mem/workers/worker.py` | job dispatch/config wiring | 注入新 Settings，增加 dedup/backfill job，避免硬编码 |
| `src/hl_mem/recall/staged_pipeline.py` | preference 判定 | 从 predicate/slot registry 判断，不再字符串搜索 canonical_attribute |
| API/MCP schema 输出 | claim DTO/序列化 | 若对外暴露 claim 元数据，兼容增加 slot/tags；旧字段标记 deprecated |

设计中“更新召回管线 filter”描述过宽。当前直接命中旧字段的明确召回点是
`recall/staged_pipeline.py` 的 preference 判断；FTS 目前只索引 subject/predicate/value，不会自动检索 tags。若 Phase 17
要求 tags 真正用于检索，还需：

- 修改 FTS external-content/trigger 或独立 tag 查询；
- 重建 FTS；
- 明确 tags 是 hard filter、soft boost 还是 query expansion；
- 增加 tag 查询索引。

否则 `topic_tags` 只是存储和统计字段，与“用于检索”的目标不一致。这是原设计的遗漏。

## 7. 测试与验收修订

### 7.1 migration 与仓储

- 旧数据库从 `001–015` 升级后字段和索引正确；
- migration 重跑幂等；
- 回填 operational/non-operational/`.other/custom.unknown` 的表驱动测试；
- 旧属性、evidence、status、conflict key 不丢失；
- tags JSON 规范化、去重、非法值、上限和 round-trip；
- dry-run 不写数据库，apply 可断点续跑。

### 7.2 extractor 与本体

- JSON schema 的 slot 为完整 `enum + null`；
- 复合事实可多 tag；
- 无稳定槽时 abstain，不回落到 `.other`；
- 必需 qualifier 缺失时 slot 被拒绝或降为 NULL；
- prompt、schema、registry 三者集合完全一致；
- importance 锚点、保护类型、边界 `0.19/0.20/0.29/0.30/0.39/0.40`；
- 旧 LLM response 兼容且有 audit；
- 标注集报告 slot precision/recall/abstention，不只断言 prompt 中出现字段名。

### 7.3 conflict 与 dedup

- NULL slot 不生成 conflict key、不进入确定性冲突；
- 同名 slot 不同 instance qualifier 不冲突；
- tool/model/provider 多实例不被错误替代；
- same-subject 原有 0.82 行为兼容；
- cross-subject 覆盖相同事实、同值不同主体、否定、数字、端口、版本、路径、时间、不同 namespace；
- LLM `equivalent/distinct/uncertain`、低置信、timeout/retry；
- 写事务期间不发生 LLM 调用；
- evidence-preserving merge、CAS 失败、幂等 pair；
- 性能测试保证 candidate scan 有 limit 且不退化为全表 O(n) 每次写入。

### 7.4 TTL 与 importance

- 全矩阵边界：`0.399/0.4/0.699/0.7`；
- temporal stable 也能过期；
- permanent 默认无策略 TTL，但显式 valid_to 生效；
- scope/importance 升降都从同一 anchor 重算；
- 多次重分类不漂移；
- expired claim 不因 importance 提升复活；
- `expires_at == now` 的边界语义明确（建议 `<= now`）；
- timezone、无 observed_at、无效时间；
- worker 去掉 volatility 条件后的状态转换；
- backfill dry-run/grace/batch/恢复；
- explicit memory 和保护类型不被低分门槛丢弃。

### 7.5 数据验收指标

“249+ tests passed”只能作为回归底线，不能作为 Phase 17 数据质量验收。建议替换/补充为：

- slot precision `>= 0.95`，并报告各 slot 的样本数和置信区间；
- abstain/NULL 的校准准确率；
- cross-subject auto-merge precision `>= 0.99`，高风险数字/否定样本零误合并；
- audit-only 候选召回率和人工确认率；
- active temporal 中 `expires_at IS NULL < 5%`，剩余项都有 reason；
- low-importance temporal 在 TTL 内 expired 或 superseded 的比例 `>= 95%`；
- bulk expire 的人工恢复率低于约定阈值；
- tags 对目标查询的 recall 有可测提升，否则不宣称“用于检索”。

## 8. 最终修订建议

Phase 17 可以立项，但应以“加法 schema、双写、shadow/audit、分行为切换”为发布原则。最关键的四项修正是：

1. 不把所有旧非 `.other` 属性回填成 slot，只允许 registry 白名单，旧语义通过 tags 和原列保留；
2. 不在 ingest 写事务中调用 LLM 做跨 subject 去重，改用专用后台 dedup 流程；
3. TTL 从原始 anchor 重算，并让 reclassify、ingest、backfill 共用同一纯策略函数；
4. 不执行全局 `<0.3` 硬丢弃，采用保护类型 + `<0.2` 门槛 + 短 TTL + audit-only 的渐进治理。

完成这些调整后，方案 E、跨 subject 去重、TTL 和 importance 才能形成真正闭环，而不会把分类误差放大成错误冲突、错误合并或
不可逆批量过期。
