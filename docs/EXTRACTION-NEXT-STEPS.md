# Extraction 下一步工程优先级与模型选择

## 结论

当前不应先换模型，也不应建设复杂的实体服务。最高 ROI 的顺序是：

1. 把 benchmark 变成可审计的测量工具：所有模型保存 `value` 和 schema 失败路径，并标注一小份 gold set。
2. 补全现有 scope 规则，使 tool/status/引用报告中的高波动事实只能从 `permanent` 降为 `temporal`，保留原因审计。
3. 让 `canonical_attribute` 确定性决定标准 predicate，消除“事实”万能 fallback。
4. 扩充并收紧现有 entity alias 写前归一化，拒绝文件、类、函数和环境变量成为顶层 subject。
5. 有了 error-path 数据后，再按真实高频错误扩展 deterministic repair。

这五项完成前，没有足够证据支持从 `glm-5.2` 迁移。两份报告都支持把 `glm-5.2` 视为质量优先候选，把
`qwen3.7-plus` 视为结构/成本更均衡的默认候选；独立审查同时指出，后四个模型没有保存任何 claim `value`，
所以不存在可复现的语义质量精确排名。

本地数据库当前共有 4,139 个 event、388 条 claim（372 active、12 disputed、4 superseded），event 只覆盖
2026-07-21 至 2026-07-24，claim 只覆盖 2026-07-21 至 2026-07-23。active claim 中 299 条 permanent、
73 条 temporal，`事实` 196/372（52.7%）。这是个人项目的早期样本：适合做小而确定的规则和回归集，不适合
现在引入在线 entity resolution、向量实体聚类或全量 second-pass LLM。

## 问题 1：工程优化优先级

### P0：先修 benchmark 的证据链

**具体改动**

- `scripts/benchmark_extraction.py::run_single_extraction()` 已在新产物的 `claims_data` 中保存 `value`，应将其作为
  必须字段，并同时保存 `canonical_attribute`、`canonical_slot`、`topic_tags`、`confidence`、`volatility`、
  `qualifiers` 和 `reason`。不要再接受只有 subject/predicate/scope/importance 的结果。
- 在 `hl_mem.ingest.llm_extractor.LLMExtractor::_request_chunk()` 暴露每次失败的脱敏诊断快照：
  `_schema_error_details()` 的 `path/error_type/invalid_value/allowed_values`、首轮响应的 hash 与安全截断、
  repair 前后 diff、最终是 repair、retry 还是 reject。benchmark 在 `run_single_extraction()` 的成功和异常路径
  都落盘这些数据。
- 将当前 50 个 event 中先人工标注 15–20 个高信息样本，字段至少包括 `should_memorize`、原子 `value`、
  canonical subject/predicate/scope；覆盖 status、quoted report、tool workflow、稳定偏好和空确认。等规则稳定后
  再补齐 50 条。采样 category 只能作为场景标签，不能作为 gold。

**预期效果**

直接解决“4 个模型没有 value”和“28 次 retry 无法归因”两个证据缺口。后续可以报告 value precision、关键事实
recall 和字段准确率，而不是用 claims 数或人工印象决定换模；也能知道哪些 retry 真能由 repair 消除。

**工作量**

6–10 小时：产物字段与错误诊断 3–5 小时，15–20 条人工 gold 及评分脚本 3–5 小时。补齐全部 50 条另需
4–6 小时。

**风险**

原始响应可能含敏感信息，必须只保存 hash、脱敏后的错误字段和有长度上限的片段；产物 schema 变化会使旧结果
不可直接合并，应通过 `manifest` 的 artifact/schema version 明确隔离，不能伪造旧模型缺失的 value。

**现在做还是等待**

现在做。它是判断其余投入和模型选择是否有效的前提，且当前脚本已经保存新结果的 `value`，补齐成本很低。

### P0：补齐 scope 的确定性降级规则

**具体改动**

- 扩展 `src/hl_mem/ingest/llm_extractor.py::normalize_scope()`：在现有文本信号和 slot TTL 规则之外，增加
  `source_kind`/event context。来源为 tool result、status report、quoted/historical report 时，或 value 命中
  当前版本、测试数量、健康状态、一次运行、进程状态、代理、CLI 路径、本次模型、审查缺陷等高波动模式时，
  将 LLM 给出的 `permanent` 降为 `temporal`。
- `LLMExtractor::_extract_one_chunk()` 调用 `normalize_scope()` 时传入 actor/event type/source kind；当前只传
  claim 字段，无法区分“稳定规则”与“某次命令输出中的同一句话”。
- 保留已有 `scope_normalized` audit，并扩展 reason code，例如 `tool_snapshot`、`quoted_report`、
  `runtime_configuration`。规则只做 `permanent → temporal` 的保守降级；明确身份、偏好、长期约束和 allowlist
  operational slot 才允许保持 permanent。
- 在 `tests/unit/test_hybrid_priors.py` 或新增 `test_scope_normalization.py` 覆盖报告中的 status/tool/历史报告案例，
  同时覆盖“项目使用 pytest”“固定端口”等不可误降级的反例。

**预期效果**

这是唯一被五模型共同暴露的系统性错误：status_report 共 45 条 claim，仅 1 条 temporal；`glm-5.2` 自身的
8 条 status claim 也全部 permanent。规则可跨模型建立安全下限，直接降低错误 TTL、过期事实冲突和后续清理成本。

**工作量**

5–8 小时，包括规则、context 传递、审计和回归测试。

**风险**

过宽的关键词会把稳定能力或架构约束误降为 temporal。应以来源类型和窄模式组合判定，不按 benchmark category
一刀切；降级结果可审计，暂不自动把 temporal 重新升级为 permanent。

**现在做还是等待**

现在做。错误 permanent 的长期成本明显高于暂时把少量稳定事实标为 temporal，而且现有函数和审计点已经具备，
无需新组件。

### P1：由 canonical attribute 投影 predicate

**具体改动**

- 在 `src/hl_mem/domain/claims/attributes.py` 增加单一映射函数，例如
  `predicate_for_canonical_attribute(attribute, llm_predicate)`：`preference.* → 偏好`、
  `identity.* → 身份`、`config.* → 配置`、`state.* → 状态`、`plan.* → 计划`、`choice.* → 使用`；
  `fact.*` 才允许 `事实`。映射表应与 `SLOT_REGISTRY`/`PREDICATE_ATTRIBUTE_MAP` 共置，避免两套真相。
- 在 `src/hl_mem/ingest/llm_extractor.py::LLMExtractor._claim()` 中，完成
  `reconcile_canonical_attribute()` 后再投影 predicate；同时记录 `predicate_normalized` audit。
- `src/hl_mem/application/ingest.py::_build_claim_drafts()` 做最后一次轻量一致性校验，防止 FakeExtractor、显式调用或
  未来其他 extractor 绕过 LLM 边界。不要从 value 自由生成新 predicate。
- 扩充 `tests/unit/test_attribute_map.py` 和 `tests/unit/test_extraction_prompt_quality.py`，覆盖每个 canonical family、
  fallback 和未知属性。

**预期效果**

解决 `事实` 占比 26.0%–56.8% 的万能 fallback，并让 conflict key、按属性召回、合并和统计使用一致语义。
`glm-5.2` 的主要结构短板会明显改善；`qwen3.7-plus` 本来 predicate 最均衡，收益较小但一致性更强。

**工作量**

5–7 小时。

**风险**

attribute 本身错时会把 predicate 一起改错；`choice.*` 到“使用”并非所有自然语言都完美。只映射已知 allowlist
family，`custom.unknown` 保留模型 predicate，并用审计观察。不要现在扩展大量新 slot；先覆盖现有高频属性。

**现在做还是等待**

现在做。现有 canonical attribute 体系已经存在，确定性投影是低侵入闭环，不是新分类系统。

### P1：收紧现有 subject 归一化，不建设动态 entity service

**具体改动**

- 扩充 `src/hl_mem/domain/entity.py::DEFAULT_ENTITY_ALIASES` 与 `normalize_entity_id()`，覆盖报告中的
  `hl_mem 项目`、`hl_mem 服务`、`hl_mem_plugin` 等明确别名；继续支持
  `HL_MEM_ENTITY_ALIASES_PATH` 配置，不把个人路径或项目名散落在代码中。
- 增加 subject 类型守卫：文件名、路径、类名、函数名、环境变量默认不能作为顶层 subject；将它们保留在 value、
  entities、qualifier 或 topic tag 中。未知 subject 不应静默并入 `hl_mem`，也不应立即注册永久实体；先记录
  candidate/audit。
- `src/hl_mem/workers/worker.py::Worker._extract()` 构建 event context 时，从已知别名/事件实体提供
  `canonical_entities`。`LLMExtractor` prompt 已声明使用该字段，但当前主链路没有稳定地产生候选。
- 在 `src/hl_mem/application/ingest.py::_build_claim_drafts()` 保持最终 `normalize_entity_id()` 防线，并为 alias 命中、
  非法 subject 降级和未知候选记录原因；测试放在 `tests/unit/test_entity.py` 及 ingest 回归中。

**预期效果**

同时缓解 `glm-5` 的 33 subject 碎片化和 Qwen 的过度归一。项目级事实稳定归到 `hl_mem`，但 Hermes、Codex、
用户等真实语义主体不会被强塞给项目。

**工作量**

6–10 小时。若只补静态 alias 和类型守卫约 4–6 小时；接入 candidate audit 再加 2–4 小时。

**风险**

文件或组件有时确实是用户要长期跟踪的实体；硬拒绝会损失细粒度召回。应允许显式记忆例外，并保留原始名称，
不要删除语义。全局进程级 alias 还需注意测试隔离。

**现在做还是等待**

现在只做静态 alias、候选注入和类型守卫。可写入的动态 entity registry、模糊匹配、embedding 聚类等至少等到
实体数量和真实误差样本明显增长后再做；388 条 claim 不足以证明其 ROI。

### P1：按 error path 扩展 repair

**具体改动**

- 先完成 P0 benchmark 诊断，再扩展 `src/hl_mem/ingest/repair.py::repair_extraction_json()`。
- 可安全加入：`entities`/`topic_tags` 单字符串转数组、空字符串转空数组或 schema 允许的 null；已知 enum 的
  大小写、连字符/下划线和中英文一对一白名单；`importance`/`confidence` 的合法数字字符串转 float；schema
  有明确默认值的缺失容器字段补空值。
- `LLMExtractor::_parse_json()` 已支持代码围栏和单个对象提取，不必重写 parser。每个 repair 继续通过
  `_emit_repair()` 记录 path/type/before/after，并给每种 repair 加独立单测。
- 不自动修复未知 tag、任意截断 JSON、越界分数 clamp、未知字段删除，也不从其他字段猜 scope/subject/value。

**预期效果**

减少无语义变化的 schema retry、延迟和 token 成本。当前总计 28 次 retry、repair 0 次，但在拿到失败类型分布前
不能承诺可消除多少；合理目标是只消除高频且一对一确定的失败。

**工作量**

诊断落盘已计入 P0；首批 repair 约 4–6 小时。

**风险**

过度 repair 会把模型语义错误伪装成 schema 成功，并使 100% schema success 失去意义。必须坚持 allowlist，
repair 后仍走完整 Pydantic validation。

**现在做还是等待**

错误观测现在做，repair 实现等一次带 error path 的 50-event benchmark 后做。不要为“repair=0”本身追求触发率。

### P2：`enable_thinking` 配置合入主线

**具体改动**

- `src/hl_mem/llm/providers.py::DashScopeProvider` 已能在 payload 写入 `enable_thinking`；还需在
  `src/hl_mem/settings.py::Settings`/`from_env()` 增加布尔配置（如 `HL_MEM_LLM_ENABLE_THINKING`），并在
  `src/hl_mem/components.py::make_llm_client()` 仅对 DashScope provider 传入该值。
- `scripts/benchmark_extraction.py::get_model_configs()` 和 manifest 必须逐模型记录该变量。thinking on/off 必须是
  独立 A/B，固定模型、prompt、testset 和治理规则，不能与换 provider 同时变化。

**预期效果**

允许用户在质量、延迟和 token 成本之间显式选择，也让 benchmark 与生产配置一致。它不会直接解决 scope、
predicate 或 subject 的系统性错误。

**工作量**

2–4 小时实现与配置测试；A/B benchmark 另计调用时间和费用。

**风险**

可能显著增加延迟/token，且部分 OpenAI-compatible endpoint 不接受该字段。必须 provider-specific，默认
`false`，不能全局透传。

**现在做还是等待**

配置接线可顺手完成，但 A/B 和默认开启应等 gold set。优先级低于规则治理；当前没有证据表明 thinking 能带来
足以抵消成本的语义收益。

### P2：预筛/EventFilter 只做窄规则整理

**具体改动**

- 当前 `src/hl_mem/ingest/event_filter.py::EventFilter.should_extract()` 和
  `src/hl_mem/ingest/pre_filter.py::ExtractionPreFilter.evaluate()` 都在过滤低价值事件，先统一职责与 reason
  taxonomy，避免同一事件被两套相似正则以不同理由处理。
- 保留 acknowledgement、空/过短消息、纯 runtime notice、无持久信号的 tool control frame 等高 precision 拒绝；
  quoted report、长 tool output 和包含根因/稳定配置的文本仍送提取，再由 scope policy 降级。
- `glm-4.7` 的漏提不应通过全量 second pass 修补。只有 prefilter 高置信认为存在持久信号而模型返回空时，才可
  试验分块或条件 fallback；先用 gold 测量再上线。

**预期效果**

减少明显无价值的 LLM 调用和 status/tool 噪声，同时避免把真正的架构决策或根因报告提前丢掉。

**工作量**

4–6 小时整理规则和测试；条件 second pass 另需 6–10 小时及额外推理成本。

**风险**

预筛 false negative 不可恢复，伤害通常大于多一次低价调用。规则应默认 allow，拒绝必须高 precision；不能用
`status_report` 字样整体过滤长文本。

**现在做还是等待**

现在只整理重叠规则并补回归，不做学习型 filter、全量二次调用或复杂 EventFilter。当前 4,139 events/3 天数据
不足以支撑更重的优化。

## 推荐实施批次

| 批次 | 内容 | 估算 | 放行条件 |
|---|---|---:|---|
| A | benchmark value/error path + 15–20 条 gold | 6–10h | 新产物能逐 claim 审计，retry 可归因 |
| B | scope 降级 + predicate 投影 | 10–15h | status/tool 错标 permanent 显著下降，fallback predicate 可测 |
| C | subject 静态 alias/守卫 + 首批 repair | 10–16h | canonical subject 回归通过，repair 只覆盖真实高频 error |
| D | thinking A/B + 规则后五模型复测 | 4–8h 工程时间 | 同一 gold、同一治理规则、所有模型保存 value |

不建议现在做：动态实体知识图谱、向量实体聚类、自动创建永久 subject、全量 second-pass LLM、为了当前
388 条 claim 迁移存储或引入新的分类微服务。

## 问题 2：模型选择建议

### 工程治理后的预期改善

以下是方向性判断，不是可宣称的复测分数；scope/predicate/subject 的规则会收敛结构差异，却不会自动补回漏掉的
claim，也不会修正错误 value。

| 模型 | 规则后预期 | 仍然存在的主要风险 |
|---|---|---|
| `glm-5.2` | scope 规则会修掉其 8 条 status 全 permanent 及代理/模型/路径噪声；predicate 投影对其 46/81 `事实` 的收益最大；subject 本已最稳定，alias 只做小幅收敛 | 19/50 非空、覆盖偏保守；延迟和 token 最高；历史报告是否应记忆仍依赖 value/source 判断 |
| `qwen3.7-plus` | 66/73 permanent 会被 scope 降级明显纠正；subject guard 可修复过度归到 `hl_mem`；其 predicate 已最均衡，因此投影收益较小 | 复杂审查 event 可能只提 1 条，关键事实漏提不会被规则层修复；73 条 value 未保存，语义精度未知 |
| `glm-4.7` | 已相对合理的 scope 和稳定 subject 得到安全下限；结构错误进一步减少 | 仅 14/50 非空、tool workflow 严重漏提；这是规则后仍最难修的模型能力差距 |
| `glm-5` | 33 个 subject 的碎片化可被 alias/类型守卫显著收敛；13 条 status 过提取的永久化伤害会下降；predicate 会规范化 | 118 claims 的过提取仍在，规则无法证明 value 正确；过多噪声可能只是从 permanent 变成 temporal |
| `qwen3.6-plus` | 87/97 permanent 和 91/97 归 `hl_mem` 会被强规则明显改善；predicate 投影也有收益 | 长文本 51 claims 的过提取仍在，治理后仍可能产生大量短 TTL 噪声；语义质量没有 value 证据 |

规则治理会让 `qwen3.7-plus`、`glm-5`、`qwen3.6-plus` 的表面分数改善最大，因为它们当前结构错误最多；这不等于
它们会反超 `glm-5.2`。`glm-4.7` 的核心问题是 recall，收益最有限。

### 工程优化后的性价比

在现有证据下，**平衡质量、延迟、token 和治理成本的首选是 `qwen3.7-plus`，但只能作为下一轮同条件复测的
默认候选，不应立即替换生产模型**。理由是：

- 24/50 非空 event 为最高，73 条 claim 又是最低 claims/非空 event，覆盖与克制较均衡；
- `事实` 19/73（26.0%）是五模型最低，规则层需要纠正的 predicate 债务最少；
- subject 数量稳定，没有 `glm-5` 的碎片化；
- 其最严重的 scope/subject 问题正好是确定性规则最能修的部分。

保留条件是：用完整 value 和 gold set 证明其 value precision 与关键事实 recall 不低于可接受阈值。现在的历史产物
不能证明这一点。

### hl_mem 当前是否从 `glm-5.2` 切换

**暂时不换。**两份报告都把 `glm-5.2` 放在质量优先候选；独立审查的质疑是证据不足，而不是证明它更差。
它还是唯一保存了 81 条 value、可直接审查语义质量的模型。对只有几天数据、388 条 claim 的个人项目，换模的
迁移收益小于一次错误选择污染长期记忆的成本。

建议：

1. 先上线模型无关的 scope/predicate/subject 治理。
2. 用新 benchmark 同条件复测 `glm-5.2` 与 `qwen3.7-plus`，两者都保存完整 value/error path。
3. 以 gold 指标和实际单 event 成本决策；若 `qwen3.7-plus` 的 value precision、关键事实 recall 与
   `glm-5.2` 接近，而延迟/成本显著更低，再切为默认。
4. `glm-5.2` 保留为 quality profile 或高价值事件的条件 fallback，而不是立刻删除支持。

报告中的 `glm-5.2` 延迟来自四路并发墙钟时间，与其余四模型的串行数据并非严格同条件；不能据此计算精确
性价比或切换阈值。

### 开源默认与用户部署配置

开源默认建议使用 **`qwen3.7-plus` + 强规则治理**，同时把结论写成“当前结构/成本折中候选”，不要宣传为已证明
的语义质量第二名。推荐提供三个简单 profile，而不是自动模型路由：

| profile | 主模型 | 配置建议 | 适用场景 |
|---|---|---|---|
| `balanced`（默认） | `qwen3.7-plus` | thinking 默认 off；scope override、subject alias/guard、predicate projection 开启；schema retry 1–2 次 | 大多数自行部署用户 |
| `quality` | `glm-5.2` | 同一治理规则；较高 timeout；thinking 仍先 off，待 A/B；保留 1–2 次 schema retry | 延迟不敏感、重视语义质量 |
| `budget` | `glm-4.7` | 高 precision 预筛；只在高置信持久事件返回空时条件 fallback/分块；监控漏提 | 成本/速度优先且接受漏记 |

`glm-5` 不应作为默认：subject 碎片化和 status 过提取严重。`qwen3.6-plus` 只适合作为 experimental/budget
选项，并强制短 TTL、subject/scope 门控与抽样审计；它当前不是“低价即可默认”的安全选择。

用户自行部署时应显式配置 provider、model、timeout、schema retry、thinking 和 entity alias 文件；不要按模型名
在源码硬编码策略。所有 profile 共用同一套 canonical subject/predicate/scope policy，使换模只改变提取能力，
不改变长期记忆的数据治理语义。

## 复测的最小验收标准

在补齐 gold set 后再定最终默认模型，至少观察：

- value 事实 precision ≥ 85%，关键长期事实 recall ≥ 75%；
- canonical subject 准确率 ≥ 95%，未知 subject 全部进入 candidate/audit；
- status/tool/quoted snapshot 错标 permanent ≤ 5%；
- `事实`/`fact.other` fallback ≤ 20%；
- 最终 schema 成功率 ≥ 99%，且首轮失败原因可观测；
- 空确认消息 false positive 为 0；
- 每个被规则改写的 scope、predicate、subject 都有 reason code。

阈值是下一轮工程门槛，不是当前 50 条启发式样本已经达到的结果。
