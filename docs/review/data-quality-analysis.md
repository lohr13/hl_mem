# HL-Mem 数据质量问题分析

> 分析日期：2026-07-24
> 范围：当前源码、单元测试，以及 `var/hl_mem.db` 的只读快照；本文不包含代码修改。

## 结论摘要

当前四类问题不是彼此独立的：

1. `canonical_attribute` 粗糙会使语义去重候选被属性兼容规则挡住；
2. subject 漂移会使现有语义去重在候选查询阶段就找不到重复项；
3. `scope=temporal` 不等于“有 TTL”，大量 `temporal + stable` 永远没有 `expires_at`；
4. `importance` 目前主要是召回排序信号，不是生命周期信号，因此低重要度 claim 可以长期保持 active。

当前库快照与问题描述一致：522 条 active 中 `fact.other` 有 240 条（46.0%）。典型的“CN 域名直连”至少存在于 `代理分流/config.network`、`xray/config.network`、`hl_mem/config.env` 三个不同 subject/attribute 组合中。它们不会被当前 conflict key 或语义去重合并。

建议优先处理写入端的属性分类与跨 subject 去重，再调整生命周期策略。只调 TTL 可以较快降低表面噪声，但不能阻止重复和错误分类继续进入库。

## 1. canonical_attribute 分类粗糙

### 1.1 当前实现

LLM 在 `src/hl_mem/ingest/llm_extractor.py:31-66` 的 `SYSTEM_PROMPT` 中生成 `canonical_attribute`：

- `src/hl_mem/ingest/llm_extractor.py:34-35` 规定 predicate 和 canonical attribute，要求使用受控的 `domain.slot`，但 prompt 中仅列举少量示例，且以 `fact.other` 收尾；
- `src/hl_mem/ingest/schemas.py:17` 只校验 `domain.slot` 的字符串格式，不在 schema 层枚举所有允许值；
- `src/hl_mem/ingest/llm_extractor.py:383-389` 先执行规则推断，再调用 `reconcile_canonical_attribute()` 调和 LLM 输出；
- `src/hl_mem/application/ingest.py:344-346` 在持久化前再次调用 `validate_canonical_attribute()`，不重新做语义推断。

项目已有预定义分类表，不是完全依赖 LLM：

- `src/hl_mem/domain/claims/attributes.py:19-50` 的 `PREDICATE_ATTRIBUTE_MAP` 是 predicate 到允许属性及 fallback 的主表；
- `src/hl_mem/domain/claims/attributes.py:52-56` 生成全局 allowlist；
- `src/hl_mem/domain/claims/attributes.py:75-144` 的 `ATTRIBUTE_HINTS` 是关键词推断表；
- `src/hl_mem/domain/claims/attributes.py:146-201` 是高置信正则规则；
- `src/hl_mem/domain/claims/attributes.py:225-237` 负责合法性校验和 fallback；
- `src/hl_mem/domain/claims/attributes.py:240-258` 负责确定性推断；
- `src/hl_mem/domain/claims/attributes.py:261-285` 负责 LLM 与规则结果的调和。

事实类当前允许 `fact.capability / fact.implementation / fact.issue / fact.cause / fact.resolution / fact.constraint / fact.project_membership / fact.tool_choice / fact.other`，见 `attributes.py:44-48`。因此项目实际上已经有 implementation、issue、resolution 等细分类，只是覆盖和命名还不足。

### 1.2 根因

这是“prompt 覆盖不足 + 受控词表不足 + 后置规则召回率低”的组合问题，不是单一代码 bug。

1. **Prompt 给出的可见词表过窄。** `llm_extractor.py:35` 的例子没有列出代码中已经允许的 `fact.implementation`、`fact.issue`、`fact.resolution`、`fact.constraint` 等。LLM 被要求“不得创造新值”，最稳妥的选择自然是 prompt 明示的 `fact.other`。
2. **事实分类缺少项目语义槽位。** 当前没有 `fact.architecture`、`fact.decision`。`plan.decision` 只适用于尚处于计划语义的决定，已经生效的架构决策只能落到 `fact.implementation`、`fact.constraint` 或 `fact.other`。
3. **规则只识别少数表面词。** `attributes.py:136-143` 依赖“已实现、问题、因为、已修复”等关键词；架构描述、设计决策、重构结果、一次性操作等没有稳定命中规则。
4. **调和逻辑有意保守。** `attributes.py:269-285` 仅在高置信规则命中，或 LLM 给出 fallback/unknown 时覆盖 LLM。这个设计可以避免误改合法 LLM 分类，但无法纠正“合法但语义错误”的细分类。
5. **历史/兼容默认值放大 fallback。** `llm_extractor.py:308-333` 对旧响应缺字段时补 `fact.other`；历史数据回填也可能保留粗分类。
6. **测试覆盖偏“能命中”，缺少分布质量约束。** `tests/unit/test_attribute_map.py:13-28` 每类只有少量正例，没有 architecture、decision、bugfix，以及“fallback 比例不能异常升高”的回归样本。

### 1.3 解决方案

#### A. 扩展受控 ontology

建议最小新增：

| 新属性 | 含义 | 示例 |
|---|---|---|
| `fact.architecture` | 已存在的架构、组件边界、数据流 | “REST 和 MCP 共用 application 服务层” |
| `fact.decision` | 已生效且仍有效的设计/技术决策 | “首版向量检索采用暴力余弦” |
| `fact.bugfix` | 已完成修复的结果 | “修复 FTS5 token 转义” |
| `fact.operation` | 一次性操作记录，仅在确有保留价值时使用 | “执行了数据库回填” |

保留现有 `fact.implementation`、`fact.issue`、`fact.cause`、`fact.resolution`，并明确边界：

- implementation：新增能力或实现形态；
- architecture：稳定结构；
- decision：选择及其约束；
- issue：尚存在的问题；
- bugfix：已完成的修复事实；
- resolution：通用问题解决结果，可逐步作为 `fact.bugfix` 的兼容别名或保留为非代码问题的解决。

不建议同时引入大量过细槽位。首轮以 4 个高频、可判定的新槽位为上限，先通过真实样本混淆矩阵验证。

#### B. 同步 prompt、schema 和规则

代码改动：

- `src/hl_mem/domain/claims/attributes.py`
  - 扩展 `PREDICATE_ATTRIBUTE_MAP`；
  - 增加中英文 hints 和高置信模式；
  - 如合并 `fact.resolution` 与 `fact.bugfix`，在 `ATTRIBUTE_ALIASES` 中明确兼容方向。
- `src/hl_mem/ingest/llm_extractor.py`
  - 在 prompt 中列出事实类的完整允许值，而不是少量示例；
  - 为 architecture / decision / implementation / issue / bugfix 各给一个正例和一个易混反例；
  - 明确“本次执行了命令”通常应跳过，不应因为存在 `fact.operation` 就默认记忆。
- `src/hl_mem/ingest/schemas.py`
  - 可把 `canonical_attribute` 从正则字符串收紧为动态生成的 JSON Schema enum；Pydantic 本地校验仍调用领域 allowlist，避免 schema 与运行时表漂移。
- `tests/unit/test_attribute_map.py`、`tests/unit/test_llm_extractor.py`
  - 增加新槽位、混淆对和 fallback 行为测试；
  - 加一组来自现有库的固定评测样本，统计 `fact.other` 比例和分类准确率。

配置改动：

- ontology 本身是领域契约，应放代码/版本化配置，不建议用环境变量注入；
- 增加可观测阈值，例如 `HL_MEM_ATTRIBUTE_FALLBACK_WARN_RATIO`，只用于审计告警，不用于改变分类结果；
- 记录 `attribute_reason`（`llm_preserved / high_confidence_rule / fallback_reconciled`）的聚合指标，超过阈值时报警。

历史数据不能通过修改 allowlist 自动修复。需要另行提供可审计的离线 reclassify/backfill：先 dry-run 输出旧值、新值、原因和置信度；确认后再重算 `canonical_attribute`、`conflict_key`，并重新执行重复检测。不可修改既有 migration 006。

### 1.4 工作量

- 代码：约 80-140 行；
- 测试与评测样本：约 100-180 行；
- 涉及 4-6 个文件；
- 预计 1.5-2.5 人日；若包含历史 240 条 `fact.other` 的 dry-run 回填与人工抽检，再加 1-2 人日。

## 2. 跨 subject 去重不够

### 2.1 当前实现

`conflict_key` 在 `src/hl_mem/domain/claims/conflicts.py:26-49` 计算。v2 payload 是：

```text
["v2", normalized_namespace, normalized_subject, canonical_attribute_slot, exclusive_qualifiers]
```

因此当前 key **不是**“subject + predicate + canonical_attribute”的精确组合：

- 包含 namespace、规范化 subject、canonical attribute 和互斥 qualifiers；
- 不包含 predicate；
- predicate 的语义已经由 canonical attribute 承担；
- `src/hl_mem/domain/claims/attributes.py:288-291` 明确“不跨属性合并”。

写入路径：

- `src/hl_mem/application/ingest.py:342-374` 规范化 subject，计算 fact hash 和 conflict key；
- `src/hl_mem/application/ingest.py:395-404` 只有互斥属性才按 conflict key 查冲突候选；
- `src/hl_mem/application/ingest.py:260-283` 在没有互斥冲突候选时执行语义去重。

现有语义去重并非不存在：

- 默认阈值为 `src/hl_mem/config.py:6` 的 `DEDUP_SEMANTIC_THRESHOLD = 0.82`；
- `src/hl_mem/domain/claims/dedup.py:40-70` 选取最高 cosine 候选并在阈值以上合并证据；
- 但 `dedup.py:41-45` 查询时传入规范化 subject；
- `src/hl_mem/storage/claims.py:124-134` 虽扫描 namespace 内候选，最终只保留 subject 相同者；
- `dedup.py:59-63` 又要求属性相同或位于兼容组；当前兼容组只有 `choice.model/config.model`，见 `dedup.py:23-31`。

### 2.2 “CN 域名直连”出现多次的直接原因

当前库中可见：

- `代理分流 / config.network`：“CN 域名直连（0.1s），国际域名走代理（2.6s）”；
- `xray / config.network`：相同文本；
- `hl_mem / config.env`：相同文本。

三条记录绕过合并有三层原因：

1. subject 不同，`conflict_key` 必然不同；
2. semantic dedup 的候选集只保留同 subject；
3. 即使放开 subject，`config.network` 与 `config.env` 当前也不属于兼容属性。

此外 `Deduplicator._text()` 在 `dedup.py:104-109` 把 subject 拼入 embedding 文本，subject 漂移会进一步压低相同事实的相似度。

### 2.3 解决方案

不建议简单删除 subject 或把所有 cosine > 0.9 的记录直接合并。不同主体说相似的话可能是不同事实，例如“xray 监听 10808”和“API 服务监听 10808”。建议增加独立于 conflict detection 的“跨 subject 重复归并”阶段：

#### A. 两阶段候选生成

1. 保留现有同 subject 去重，阈值继续由现有配置控制；
2. 新增跨 subject 高置信去重：
   - 同 namespace、active/candidate/disputed；
   - canonical attribute 相同或在显式兼容组；
   - 使用**不含 subject**的语义文本：`normalized predicate + canonical attribute + value + relevant qualifiers`；
   - cosine 默认 `>= 0.90`；
   - exact normalized value/hash 可直接进入高置信候选，但仍需属性兼容检查。

代码改动：

- `src/hl_mem/storage/claims.py`
  - 新增有界的 `find_cross_subject_dedup_candidates(namespace, attributes, limit)`；
  - 不要继续每次全 namespace 无界扫描，至少按 status/attribute 预筛并配置扫描上限。
- `src/hl_mem/domain/claims/dedup.py`
  - 拆分 same-subject 与 cross-subject 策略；
  - 新增 subject-independent embedding 文本；
  - 返回 match ID、score、match mode 和拒绝原因，供审计。
- `src/hl_mem/application/ingest.py`
  - 在同 subject 去重未命中后执行跨 subject 阶段；
  - 命中时保留一个 canonical claim，仅新增 evidence link，不物理删除来源。

#### B. 增加语义安全护栏

自动合并须同时满足：

- cosine `>= 0.90`；
- attribute 相同或显式兼容；
- 数字、版本、端口、否定词不存在冲突；
- exclusive qualifiers 一致；
- 没有 change/supersede 信号；
- subject 属于已知 alias、组件归属关系，或者文本值近似完全一致。

对 `0.85-0.90`、属性不一致、subject 无关系的候选，只写入审计/待审核队列，不自动合并。这里应复用现有 conflict/consolidation 能力，而不是把“语义相似”误当成“事实相同”。

#### C. 修复上游 subject 和 attribute

- `src/hl_mem/domain/entity.py` 的 alias 归一化只解决同一实体的不同写法，无法解决“组件事实错误归属到项目/用户”；
- 应在 extractor prompt 中明确事实归属：路由策略归属 `xray` 或“代理分流策略”，不能随对话上下文漂移到 `hl_mem`；
- 对 `config.env` 与 `config.network/routing` 建立窄兼容规则前，先修复属性分类；否则扩兼容组会放大误合并。

配置改动：

- 把 `DEDUP_SEMANTIC_THRESHOLD` 从 `config.py` 的常量迁移到 `Settings`：
  - `HL_MEM_DEDUP_SAME_SUBJECT_THRESHOLD=0.82`；
  - `HL_MEM_DEDUP_CROSS_SUBJECT_THRESHOLD=0.90`；
  - `HL_MEM_DEDUP_CROSS_SUBJECT_SCAN_LIMIT`；
- 启动时校验 `same_subject <= cross_subject <= 1.0`。

历史修复应以 evidence-preserving merge 执行：选择 canonical claim，把重复项的 evidence links 转接或补接，再将重复项标为 superseded/merged（若当前状态机没有 merged，优先使用 superseded 并记录原因），不可直接删除。

### 2.4 工作量

- 代码：约 140-240 行；
- 测试：约 150-250 行，重点覆盖否定、端口、版本、同值不同主体和属性不兼容；
- 涉及 5-7 个文件；
- 预计 2.5-4 人日；历史候选 dry-run、人工审核和合并工具另加 1-2 人日。

## 3. Temporal claims 过期不够快

### 3.1 当前 TTL 与过期逻辑

TTL 策略分为两部分：

1. `src/hl_mem/config.py:22-29` 的 `ATTRIBUTE_TTL_DAYS`：
   - `state.service_health`：7 天；
   - `state.process`：7 天；
   - `state.connectivity`：7 天；
   - `state.test_suite`：7 天。
2. `src/hl_mem/settings.py:65,126` 的通用 `memory_temporal_ttl_days`，由 `HL_MEM_TEMPORAL_TTL_DAYS` 注入，默认 7 天。

实际 `expires_at` 在 `src/hl_mem/application/ingest.py:348-357` 计算：

- 命中 `ATTRIBUTE_TTL_DAYS` 时直接使用属性 TTL，并在 `ingest.py:379` 强制 volatility 为 ephemeral；
- 否则只有 `volatility == "ephemeral" and scope == "temporal"` 才使用通用 7 天 TTL；
- `scope == "temporal"` 但 `volatility == "stable"` 时 `expires_at=None`。

过期 Worker 在 `src/hl_mem/workers/ttl.py:9-27` 中只处理：

```sql
status='active'
AND volatility='ephemeral'
AND expires_at IS NOT NULL
AND expires_at < now
```

`tests/unit/test_ttl.py:6-18` 还明确断言：即使 stable claim 的 `expires_at` 已过期，也不能被 TTL Worker 过期。

另有衰减/归档兜底，但默认非常慢：

- `src/hl_mem/workers/decay.py:10-19`：temporal 90 天后开始降低 confidence，180 天后归档；
- `decay.py:61-77` 使用最后访问时间或记录时间作为 anchor，访问频率还能延后阈值；
- `decay.py:69-74` 有 `expires_at` 时才禁用访问奖励。

### 3.2 为什么 importance 0.7、2.7 天仍 active

这是当前策略的预期结果：

1. 默认 TTL 是 7 天，2.7 天尚未到期；
2. 如果该 claim 是 `temporal + stable` 且不属于四个短期 state 属性，它根本没有 `expires_at`；
3. `importance=0.7` 不参与 `expires_at` 计算；
4. decay 要到 90/180 天才产生明显生命周期动作。

当前库的 7/21 规划讨论大多是 `plan.other / plan.evaluation / plan.deadline + temporal + stable`，`expires_at` 为空，所以不会在第 7 天由 TTL Worker 过期；它们可能一直 active 到 decay 的 180 天归档边界。

### 3.3 解决方案

应调整的是“TTL policy 的输入维度”，不只是把全局 7 天改成 3 天。

建议统一计算：

```text
expires_at = recorded/observed time
             + ttl(scope, canonical_attribute, volatility, importance_band)
```

推荐首版矩阵：

| 条件 | 建议 TTL |
|---|---:|
| `scope=permanent` | 无 TTL |
| `state.service_health/process/connectivity/test_suite` | 1 天或 3 天 |
| `fact.operation` 且 temporal | 1-3 天 |
| `plan.*` 且 temporal、importance < 0.7 | 3 天 |
| `plan.*` 且 temporal、importance >= 0.7 | 7 天 |
| 其他 temporal、importance < 0.4 | 3 天 |
| 其他 temporal、importance 0.4-0.6 | 7 天 |
| 其他 temporal、importance >= 0.7 | 14 天，或要求显式截止时间 |

这里 importance 应当**提高保留时间**，不是让 importance 0.7 更快过期。用户提到的 7/21 规划讨论如果已经完成，正确机制首先应是新事件触发 supersede/完成状态，TTL 只是兜底。单纯因为“已经 2.7 天”而过期一个高重要计划，可能丢失尚未完成的承诺。

代码改动：

- 新建集中生命周期策略函数，例如 `domain/claims/retention.py`：
  - 输入 scope、attribute、volatility、importance、显式 valid_to；
  - 输出 TTL 天数和 reason code；
  - 纯函数，便于表驱动测试。
- `src/hl_mem/application/ingest.py`
  - 用策略函数替代当前二分判断；
  - 所有有 TTL 的 claim 统一写 `expires_at`；
  - 不再要求 `scope=temporal` 必须同时是 ephemeral 才有 TTL。
- `src/hl_mem/workers/ttl.py`
  - 以 `expires_at` 作为唯一到期事实来源，去掉 `volatility='ephemeral'` 限制；
  - 保持 valid_to 收敛与状态机守卫。
- `src/hl_mem/workers/decay.py`
  - 保持“无明确 TTL 的长期兜底”职责；
  - 有 `expires_at` 的 claim 继续禁止访问奖励延长到期日。
- 增加观测指标：active temporal 中 `expires_at IS NULL` 的比例、过期原因分布、到期前被 supersede 的比例。

配置改动：

- 不再只提供一个 `HL_MEM_TEMPORAL_TTL_DAYS`；
- 在 `Settings` 中增加少量稳定参数，例如：
  - `HL_MEM_TTL_TRANSIENT_STATE_DAYS=1`；
  - `HL_MEM_TTL_LOW_IMPORTANCE_TEMPORAL_DAYS=3`；
  - `HL_MEM_TTL_DEFAULT_TEMPORAL_DAYS=7`；
  - `HL_MEM_TTL_HIGH_IMPORTANCE_TEMPORAL_DAYS=14`；
- 属性到 band 的映射属于领域规则，保留在版本化代码/配置文件中；具体天数通过 Settings 注入；
- 对正整数和 `low <= default <= high` 做启动校验。

历史 active temporal 需要一次 backfill 才能享受新策略。应以 `recorded_from/observed_at` 为基准计算；已经超过新 TTL 的记录先 dry-run，再批量 expired，避免上线瞬间不可逆地清理大量记忆。

### 3.4 工作量

- 代码：约 100-170 行；
- 测试：约 120-200 行；
- 涉及 5-7 个文件；
- 预计 2-3 人日；历史 `expires_at` 回填与上线观测另加 0.5-1 人日。

## 4. 低 importance claims 噪声

### 4.1 当前 importance 来源与用途

importance 主要由 LLM 决定：

- `src/hl_mem/ingest/llm_extractor.py:49-55` 在 prompt 中定义 0.0-1.0 档位；
- `llm_extractor.py:400-402` 将模型值 clamp 到 `[0, 1]`，非法值回退 0.5；
- `src/hl_mem/application/ingest.py:359-362` 持久化前再次 clamp；
- `src/hl_mem/ingest/schemas.py:24` 只做数值范围校验。

存量重分类位于 `src/hl_mem/workers/reclassify.py:18-23,81-105`，但只选择 `scope=permanent and importance=0.5` 的默认记录。已经是 0.3 的 claim 不会再次评估。

importance 当前用途很有限：

- `src/hl_mem/recall/ranking.py:8,46-58` 把它作为召回排序的一个权重，权重为 0.075；
- TTL、decay 和 archive 逻辑都不读取 importance；
- 提取后的低价值过滤 `src/hl_mem/ingest/llm_extractor.py:112-119` 只过滤空值、部分纯数字/版本、少数健康状态，不依据 importance。

因此当前没有基于 importance 的自动过期或归档机制。importance 0.3 的一次性操作保持 active 是必然结果；如果同时被标为 permanent/stable，它可以无限期 active。

### 4.2 根因

1. importance 被设计成排序特征，却被数据质量需求当作生命周期特征使用，语义没有闭环；
2. LLM prompt 说 0.0-0.3 是 incidental，但代码没有执行“incidental 应跳过或短留”的政策；
3. 一次性操作可能被错误标成 permanent，importance 无法纠正 scope；
4. reclassify 只处理默认值 0.5，不能修复已分类但低质量的历史记录；
5. decay 按 scope 和不活跃天数处理，与 importance 无关。

### 4.3 解决方案

采用“写入门槛 + 短 TTL + 慢归档”三层策略：

#### A. 写入门槛

- `importance < 0.2` 且不是 explicit memory、identity、preference、constraint：默认不落 claim，只保留 event 证据；
- `0.2 <= importance < 0.4`：允许落库，但一次性操作、运行结果和临时路径必须标 temporal，并设置短 TTL；
- 永久身份、明确偏好、安全约束不能仅因低 importance 自动丢弃，属性语义优先于分数。

这比全局 `importance < 0.4` 直接拒绝更安全。

#### B. 生命周期

采纳“`importance < 0.4` 的 temporal claim 3 天过期”作为首版默认规则，纳入第 3 节统一 TTL policy。对于 `importance < 0.4 + permanent`：

- 不自动 3 天过期；
- 先审计为分类异常；
- 若属性是 operation/test result/transient state，由确定性规则改为 temporal；
- 其余交给 reclassify 或人工抽检，避免删除低频但长期有效的身份/配置。

#### C. 召回与归档

- 召回侧可增加最低 importance 软过滤，但不建议默认硬过滤；低 importance 仍可能与查询高度相关；
- decay policy 可让低 importance 的**无 TTL** temporal claim 更早归档，例如 30/60 天，而不是现有 90/180 天；
- explicit query/historical intent 应能检索 expired/archived 证据，避免“降噪”等于“不可追溯”。

代码改动：

- `src/hl_mem/ingest/llm_extractor.py`：在 `_is_low_value_claim()` 或独立 policy 中加入属性感知的写入门槛；
- `src/hl_mem/domain/claims/retention.py`：实现 importance band 与 TTL；
- `src/hl_mem/workers/reclassify.py`：增加可配置筛选模式，覆盖低 importance permanent、temporal 无 expires_at，而不只处理默认 0.5；
- `src/hl_mem/workers/decay.py`：可选读取 importance band 决定无 TTL claim 的 decay/archive 边界；
- 审计被跳过、短期保留和重分类的原因。

配置改动：

- `HL_MEM_MIN_CLAIM_IMPORTANCE=0.2`；
- `HL_MEM_LOW_IMPORTANCE_THRESHOLD=0.4`；
- `HL_MEM_TTL_LOW_IMPORTANCE_TEMPORAL_DAYS=3`；
- 所有阈值进入 `Settings` 并校验 `0 <= min <= low <= 1`；
- production 先以 audit-only 模式运行 3-7 天，统计误杀样本后再启用 hard filter。

### 4.4 工作量

若与第 3 节 TTL policy 合并实施：

- 增量代码：约 60-110 行；
- 增量测试：约 80-140 行；
- 涉及 3-5 个文件；
- 预计 1-1.5 人日。

若单独实施，会重复 TTL 配置和策略代码，预计 1.5-2.5 人日，不推荐。

## 5. 优先级与实施顺序

| 优先级 | 项目 | 理由 | 建议交付 |
|---:|---|---|---|
| P0 | canonical attribute 细化 | 46% fallback 是最明显的上游质量故障，并直接阻断属性兼容和生命周期分流 | ontology v2、prompt/schema 同步、真实样本评测、fallback 告警 |
| P0 | 跨 subject 高置信去重 | 重复会直接污染召回和统计；现有 0.82 语义去重因 subject/attribute 候选限制无法覆盖 | 两阶段去重、0.90 阈值、安全护栏、dry-run 审计 |
| P1 | 统一 temporal TTL policy | 解决 temporal+stable 永不到期的结构性缺口；比单改全局 TTL 更可靠 | retention 纯函数、importance band、TTL Worker 以 expires_at 为准 |
| P1 | 低 importance 治理 | 依赖统一 TTL policy，适合合并交付；先 audit-only 可降低误杀风险 | 写入门槛、3 天短 TTL、异常重分类 |

P0 两项的先后建议是先属性分类、后跨 subject 去重。否则 `config.env`/`config.network` 等错分会迫使去重层加入过宽兼容规则，增加误合并风险。

一个风险更低的发布顺序：

1. 增加属性分类评测和审计指标，不改历史状态；
2. 发布新 ontology/prompt，对新写入生效；
3. 跨 subject 去重先 audit-only，人工核验 `>=0.90` 候选；
4. 启用高置信自动合并；
5. 发布统一 TTL/importance policy；
6. 对历史数据 dry-run 回填属性、conflict key、expires_at，分批应用。

## 6. 总体工作量

| 方案 | 代码与测试规模 | 预计工作量 |
|---|---:|---:|
| 属性分类细化 | 180-320 行，4-6 文件 | 1.5-2.5 人日 |
| 跨 subject 去重 | 290-490 行，5-7 文件 | 2.5-4 人日 |
| 统一 TTL policy | 220-370 行，5-7 文件 | 2-3 人日 |
| 低 importance 治理（与 TTL 合并） | 140-250 行，3-5 文件 | 1-1.5 人日 |
| 历史数据 dry-run、回填、抽检 | 工具与报告视批次而定 | 2-4 人日 |

核心代码合计约 7-11 人日；包含历史数据治理、人工抽检和上线观测约 9-15 人日。四项不应一次性无审计上线，尤其是跨 subject 合并和历史 TTL 回填。

## 7. 验收指标

建议用数据指标而不是仅以测试通过作为完成标准：

- 新写入 `fact.other` 占事实类比例降到 `<15%`，且人工抽检准确率 `>=90%`；
- 跨 subject 重复候选中，自动合并 precision `>=98%`；
- active temporal 中 `expires_at IS NULL` 的比例降到 `<5%`（明确豁免需有 reason code）；
- importance `<0.4` 的 temporal claim，95% 在 3 天内 expired 或被 supersede；
- 不增加端口、版本、否定句等高风险事实的误合并；
- 所有合并、过期、重分类均保留 evidence chain 和审计原因。
