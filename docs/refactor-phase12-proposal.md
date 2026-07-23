# Phase 12：记忆库数据质量与提取精度改进方案

## 1. 结论摘要

本次检查确认 Hermes 指出的 5 类质量问题都真实存在，但“semantic dedup 阈值过高”不是重复数据的唯一根因，
且阈值所在文件定位有误。`recall_pipeline.py` 只负责召回；写入去重的真实调用链是
`application/ingest.py::store_extracted()` → `recall/dedup.py::Deduplicator.find_duplicate()` →
`storage/repository.py::ClaimRepository.find_active()`。

建议采用组合方案：

1. subject 使用“Prompt 约束 + 代码级确定性归一化”，代码结果作为最终权威；
2. semantic dedup 默认阈值由 `0.85` 调为 `0.82`，但同时增加 namespace、归一化 subject、属性兼容和
   冲突保护，禁止无约束全库相似度合并；
3. canonical attribute 使用 Prompt 示例提升首判精度，并用高精度规则修正明显错配；
4. scope 使用中文定义、正反例和后置规则共同判定，不能用“出现数字/版本号”作为唯一规则；
5. 过时治理优先依赖正确的 subject/attribute/conflict slot 触发 supersede；TTL 只用于确有时效边界的
   temporal claim，decay 继续作为低优先级兜底。

本方案不新增依赖，不修改 migration 001–014，并保持现有 API 数据结构兼容。

## 2. 数据库验证结果

按要求对 `var/hl_mem.db` 做了只读查询，结果如下：

- active claims：599；
- 完全相同 `value_json`：49 组、107 行；若每组保留一行，多余副本为 58 行；
- 高频重复包括 `gpt-5.6-sol` 5 条、`启动和成功后都打印` 3 条、`gte-rerank-v2` 3 条、
  `glm-5.1` 3 条；
- subject 前几位：`hl_mem` 254、`用户` 157、`Hermes` 29、`Codex` 19；
- 明显别名仍并存：`hlmem` 5、`hermes-agent` 6、`Hermes 插件` 5、`Hermes memory` 4、
  `Codex CLI` 5、`llm_extractor` 3、`LLMExtractor` 3；
- scope：permanent 434，temporal 165；
- active 且设置 `expires_at` 的 claim 为 35 条，当前没有已到期但仍 active 的记录。

补充诊断表明，同值重复经常跨越多个维度。例如：

- `gpt-5.6-sol` 分布在 `Codex`、`Codex CLI`、`用户`，predicate 同时有“使用/配置”，attribute
  同时有 `choice.tool/config.env/config.other`，两两 cosine 为 0.7410–0.9796；
- `gte-rerank-v2` 分布在 `HL_MEM/hl_mem/用户`，cosine 为 0.7290–0.9459；
- 同为 `hl_mem` 的 `SQLite WAL` 因 predicate/attribute 不同形成重复，cosine 为 0.9719。

因此背景文档中的“86 条（14%）”应保留为人工语义审计口径；SQL 的“完全同值”口径得出 107 行或
58 条多余副本，两者不可混用。

## 3. 问题 1：语义重复

### 3.1 根因分析

Hermes 关于阈值偏保守的判断部分正确，但需补充以下根因：

1. 当前阈值实际在 `config.py`，默认 `HL_MEM_DEDUP_THRESHOLD=0.85`，由 `recall/dedup.py` 使用，
   不在 `recall_pipeline.py`；
2. `find_duplicate()` 只调用 `find_active(namespace, subject_entity_id)`，subject 必须逐字相同。
   `hl_mem/hlmem/HL_MEM` 等别名会在相似度计算前就被排除；
3. embedding 文本包含 subject、predicate 和 `value_json`。相同 value 只要 subject 或 predicate 不同，
   cosine 就可能降到 0.65–0.75；
4. fact hash 同样包含未归一化 subject 和 predicate，只能发现严格结构相同的事实；
5. 当前 semantic 分支只比较向量，不检查 canonical attribute 是否兼容，也不区分“近义重复”和
   “语义相近但值相反”，单纯降低阈值会增加误合并风险；
6. 遍历候选后命中第一条即返回，没有选择最高分，也没有在审计日志中记录实际相似度与判定原因。

### 3.2 修复方案

推荐先完成 subject 和 attribute 归一化，再把 semantic dedup 改成受约束的最高分匹配。

旧逻辑：

```python
candidates = repo.find_active(namespace, subject)
for claim in candidates:
    if cosine_similarity(existing_blob, blob) > threshold:
        return claim["id"], "semantic"
```

新逻辑设计：

```python
normalized_subject = normalize_entity_id(new_claim["subject_entity_id"])
candidates = repo.find_active_for_dedup(namespace, normalized_subject)
compatible = [
    claim
    for claim in candidates
    if attributes_are_dedup_compatible(claim["canonical_attribute"], new_claim["canonical_attribute"])
    and not values_are_deterministically_conflicting(claim, new_claim)
]
best = max(
    ((claim, cosine_similarity(claim["embedding_dense"], blob)) for claim in compatible),
    key=lambda item: item[1],
    default=None,
)
if best is not None and best[1] >= threshold:
    return best[0]["id"], "semantic", best[1]
```

具体约束：

- 只在同 namespace 内比较；
- subject 必须经同一函数归一化后相同；
- canonical attribute 相同才自动合并；仅允许显式兼容组（如 `choice.model/config.model`）跨 slot 比较；
- mutually exclusive slot 的不同值先走冲突判定，不得被 semantic dedup 吞掉；
- 返回最高相似度候选，不再返回数据库顺序中的第一个命中；
- audit detail 增加 `threshold`、`similarity`、`match_kind`、`candidate_count`；
- repository 新查询按归一化 subject 获取 active/candidate/disputed 候选；不应借用召回的
  `list_embedded()` 做全库扫描。

### 3.3 阈值建议

建议默认值从 **0.85 调至 0.82**。

理由：

- 0.85 已漏掉数据库中 0.82–0.85 区间的明显改写；
- 直接降至 0.75 会进入大量“同主题、非同事实”区域，当前样本中完全同值也能因 subject/predicate
  差异低至 0.65–0.75，说明低阈值不能替代结构归一化；
- 在“同归一化 subject + 同/兼容 attribute + 无确定性冲突”的前置约束下，0.82 是较稳健的初始值；
- 比较符统一为 `>=`，使配置值具有直观边界语义。

上线前应建立标注集：至少 100 个 duplicate 正例、100 个 related-but-distinct 反例、50 个 contradiction
反例，离线比较 0.80/0.82/0.85。验收目标建议 precision ≥ 0.98，随后在满足精度前提下最大化 recall。
阈值继续通过 `HL_MEM_DEDUP_THRESHOLD` 注入，不硬编码运行时配置。

## 4. 问题 2：subject 实体碎片化

### 4.1 根因分析

Hermes 判断正确。Prompt 只要求“使用具体名称”，没有给出标准名、别名表或实体复用规则；
`LLMExtractor._claim()` 只归一化 value 中的 PostgreSQL 别名，subject 原样透传；
`store_extracted()` 又直接用 `extracted.subject` 生成 fact hash、conflict key 和去重候选范围。
碎片化因此同时破坏精确去重、语义去重、冲突归并和按实体召回。

### 4.2 最佳方案：Prompt + 代码结合

仅 Prompt 级方案无法保证稳定性，模型升级、上下文差异和大小写仍会产生漂移；仅代码级方案若只有静态
alias 表，也无法处理新实体。最佳方案是：

1. Prompt 负责语义选择：明确“组件事实归属项目还是组件”，并要求复用事件上下文提供的标准实体名；
2. `_claim()` 边界做通用规范化：Unicode NFKC、trim、合并空白；
3. `store_extracted()` 在生成 hash/key 和查询前调用确定性 `normalize_entity_id()`；
4. 项目已知别名由配置注入，不把本机路径或用户特有实体硬编码在通用源码；
5. 未命中 alias 的实体保留原显示名，另用稳定 canonical id 参与 key；首版若不新增列，可直接将 canonical
   name 写入 `subject_entity_id`，未来再拆 `display_name/entity_id`。

建议的代码边界：

```python
# old
namespace, subject = event.get("tenant_id", "default"), extracted.subject

# new
namespace = event.get("tenant_id", "default")
subject = entity_normalizer.normalize(extracted.subject)
```

首版内置的通用、无项目偏见规则只处理大小写、Unicode、空白、下划线/连字符的安全等价形式；
项目别名使用配置文件，例如：

```json
{
  "hlmem": "hl_mem",
  "hl_mem": "hl_mem",
  "hermes-agent": "Hermes",
  "hermes 插件": "Hermes",
  "hermes memory": "Hermes",
  "codex cli": "Codex",
  "llmextractor": "llm_extractor",
  "watchdog": "hlmem-watchdog",
  "cleanup_data.py": "scripts/cleanup_data.py"
}
```

该表是本实例的初始建议，必须由 `HL_MEM_ENTITY_ALIASES_PATH` 指向配置文件加载；加载失败应给出具体错误，
不能静默忽略。为防止错误合并，alias 必须是精确匹配，不使用模糊字符串相似度自动改名。

Prompt 增补：

```text
subject 必须复用标准实体名。同一实体不得因大小写、空格、连字符、产品后缀或“插件/memory/CLI”等描述
产生新名称。若事件上下文提供 canonical_entities，必须从其中选择；组件级事实仍归组件，项目级事实归项目。
示例：hlmem/HL_MEM → hl_mem；Codex CLI → Codex；LLMExtractor → llm_extractor。
```

## 5. 问题 3：过时数据未过期

### 5.1 根因分析

Hermes 指出 decay 只按访问因素治理并不完整，但准确地说，当前 decay 同时依据 scope、最后访问时间、
access_count bonus 和 confidence；它完全不理解 claim 内容，也不会判断新旧版本关系。更严重的是，
错误 scope 会让短期状态套用 permanent 的 180/365 天策略，而被频繁召回的旧事实还会额外延后归档。

现有 `expires_at` 只在 `volatility == "ephemeral" and scope == "temporal"` 时设置。版本、测试数、评分、
文件行数等若被误判 stable/permanent，就不会进入 TTL。Prompt 已要求跳过部分低价值数据，但历史数据和
模型漏判仍会留下。

### 5.2 修复方案

不建议“所有版本号/数字 claim 自动 temporal + 短 TTL”。端口、日期、显存、阈值等数字可能是长期配置，
模型名也可能是当前有效配置；粗暴规则会误删重要事实。

采用三层治理：

1. **提取边界过滤**：继续丢弃孤立测试数、行数、评分、commit、migration 编号和纯状态播报；
2. **高精度时效分类**：识别 `state.test_suite`、构建结果、当前版本、临时评分、文件行数等模式，
   强制 `scope=temporal`；只有明确短期状态才设 `volatility=ephemeral` 和 TTL；
3. **新值驱动 supersede**：模型、版本、端口、部署状态等当前状态必须映射到稳定 subject +
   mutually-exclusive canonical slot，使新值写入时关闭旧值的双时间区间。这比等待 decay 更准确。

建议新增可配置的 TTL policy，而不是在 decay 中解析自然语言：

- service/process/connectivity 临时状态：1–7 天；
- test count/build result/file line count：7 天，且默认在提取边界丢弃；
- evaluation score/current project version：30 天，若新值出现则立即 supersede；
- durable configuration/preferences：不设 TTL。

运行时天数应从 Settings/环境变量注入。decay worker 仅消费已经确定的 scope/expires_at，不承担 LLM 语义
分类；TTL worker 继续负责到期状态转换。对访问加成增加保护：高访问不能延长明确 `expires_at`，也不能阻止
已被 supersede 的事实失效。

历史数据需单独执行可回滚治理任务：dry-run 导出候选 → 按规则生成报告 → 备份 → 将确定的旧值
supersede/expire → 重建受影响索引。不要直接删除 claim，保留证据链和历史召回能力。

## 6. 问题 4：scope 大面积错分

### 6.1 根因分析

Hermes 判断正确。当前 scope 说明后追加为英文，主体 Prompt 是中文；定义把 “configuration” 一概列为
permanent，容易把“当前部署/已经实现/当前模型”误判为永久事实。`_claim()` 只校验枚举，`store_extracted()`
也只做同样的 fallback，没有内容级纠偏。volatility 与 scope 虽声明独立，却缺少容易混淆的成对反例。

另一个口径问题是“已部署最新代码”“实现了 FTS5 修复”未必都应判 permanent：架构能力可长期保留，
一次部署状态是 temporal。应先判断事实语义，而不能仅凭完成时态统一改 permanent。

### 6.2 修复方案

Prompt 改为完整中文决策表：

```text
scope 表示事实的有效期，不表示变化频率：
- temporal：有截止期、仅描述当前/本次/某版本/某次运行，或未来会被新状态替换；
- permanent：身份、稳定偏好、长期约束、设计原则，以及不依赖某次运行或版本的系统能力。
判断题：一年后且脱离本次会话，这条事实仍应作为当前事实成立吗？是 → permanent；否 → temporal。
反例：
“当前测试 180 passed” → temporal；“项目使用 pytest” → permanent。
“已部署 v0.3.0” → temporal；“系统支持在线备份” → permanent。
“本次修复了 FTS5 查询” → temporal；“FTS5 查询会转义用户 token” → permanent。
“端口固定为 8200” → permanent；“服务现在监听 8200” → temporal。
```

新增确定性 `normalize_scope()` 后置规则，优先级如下：

1. 明确日期范围、deadline、临时/本次/当前运行、测试数量、构建结果、版本查询、评分、行数 → temporal；
2. identity/preference/explicit memory/长期约束 → permanent；
3. `state.*` 默认 temporal，除非表示稳定能力而应先改为 `fact.capability`；
4. `plan.deadline` → temporal；`config.*` 不自动 permanent，结合“固定配置”与“当前运行状态”判断；
5. 无高置信规则时保留 LLM 结果，不进行激进覆盖。

`normalize_scope()` 应同时返回 `scope` 和 `reason_code`，audit 记录 LLM 原值、最终值和命中规则，以便抽样评估。
不要通过 predicate 单字段覆盖全部结果。

## 7. 问题 5：canonical_attribute 错配

### 7.1 根因分析

Hermes 判断正确，但现有 `ATTRIBUTE_HINTS` 目前只用于 `infer_canonical_attribute()`，而新 LLM claim 的
`_claim()` 调用的是 `validate_canonical_attribute()`。只扩充 hints 不会自动修复“合法但选错”的 LLM
attribute，例如“使用 + choice.tool + gpt”仍在允许集合内，校验会直接接受。

此外，现有 hints 使用简单 substring 且顺序优先：

- “配置”中的路径提示包含 `/`，会把 `https://...` 优先归为 `config.path`；
- `127.0.0.1` 是 host，不是端口；背景文档把它与 8200 一起归 `config.port` 需要修正；
- “秒”作为 timeout 提示过宽；
- 模型只覆盖 qwen/gpt，缺 glm、Claude、embedding/rerank 型号；
- URL 的语义取决于内容：API endpoint 可为 `config.network`，环境变量名/值可为 `config.env`，
  普通引用 URL 应为 `fact.other`，不能统一归一类。

### 7.2 ATTRIBUTE_HINTS 补充清单

建议按“高精度模式在前、宽泛词在后”排序，并允许 hints 使用命名正则规则，避免纯 substring 误判。

| 目标属性 | 关键词/模式 |
|---|---|
| `choice.model` | `gpt-`, `glm-`, `qwen`, `claude`, `gemini`, `deepseek`, `llama`, `mistral`, `model`, `模型` |
| `config.model` | `LLM_MODEL`, `EMBEDDING_MODEL`, `RERANKER_MODEL`, `模型名`, `model=` |
| `choice.provider` | `百炼`, `dashscope`, `智谱`, `zhipu`, `openai`, `anthropic`, `provider`, `供应商` |
| `config.port` | `端口`, `port`, `listen`, `监听`，以及独立的 1–65535 整数（须有配置语境） |
| `config.network` | `host`, `hostname`, `IP`, `IPv4`, `127.0.0.1`, `localhost`, `域名`, `endpoint`, `base_url`, `代理` |
| `config.env` | `环境变量`, `env`, `*_URL`, `*_HOST`, `*_PORT`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `API_KEY` |
| `config.path` | `路径`, `目录`, `文件`, `path`, Windows drive path、UNC path、相对源码路径、`.py/.toml/.json/.db` 后缀 |
| `choice.api` | `API`, `SDK`, `接口`, `OpenAI-compatible` |
| `choice.protocol` | `HTTP`, `HTTPS`, `gRPC`, `WebSocket`, `SSE`, `MCP`, `协议` |
| `choice.framework` | `FastAPI`, `PyTorch`, `Django`, `Flask`, `pytest`, `uvicorn`, `框架` |
| `state.test_suite` | `passed`, `failed`, `pytest`, `测试通过`, `测试数` |
| `state.deployment` | `部署`, `deployed`, `上线`, `发布` |
| `fact.implementation` | `已实现`, `新增`, `接入`, `支持`, `修复实现` |

规则优先级建议：

1. 环境变量名；
2. URL/IP/host；
3. Windows/UNC/源码文件路径；
4. port；
5. model/provider/protocol；
6. 宽泛 fallback。

### 7.3 old → new 改动

旧逻辑：

```python
canonical_attribute = validate_canonical_attribute(predicate, llm_attribute)
```

新逻辑：

```python
validated = validate_canonical_attribute(predicate, llm_attribute)
inferred = infer_canonical_attribute(predicate, subject, value, qualifiers)
canonical_attribute, reason = reconcile_canonical_attribute(
    predicate=predicate,
    llm_attribute=validated,
    inferred_attribute=inferred,
    subject=subject,
    value=value,
    qualifiers=qualifiers,
)
```

`reconcile_canonical_attribute()` 只在高精度规则命中或 LLM 返回 fallback slot 时覆盖；模糊匹配保留 LLM
结果并记录 audit。Prompt 同时补充上述典型正反例，避免把所有纠错压力放到代码。

需要同步评估 `MUTUALLY_EXCLUSIVE_SLOTS`：`choice.model` 当前不在集合，而 `config.model` 在集合。
若语义是“当前选用模型”，应统一选择一个稳定 slot，或显式声明二者分别代表“技术选择”和“配置值”，
并建立兼容冲突组，防止旧模型长期并存。

## 8. 实施顺序与文件影响

建议按以下顺序实施，以免重复修复互相遮蔽：

1. `llm_extractor.py`：Prompt、subject 通用清洗、scope/attribute 初步解析；
2. 新增聚焦的 entity normalization 模块，并由 `store_extracted()` 在生成任何 key 前调用；
3. `attribute_map.py`：扩充 hints、加入高精度模式与 reconcile；
4. `ingest.py`：统一采用最终 subject/scope/attribute，再计算 fact hash/conflict key/TTL；
5. `repository.py` 与 `dedup.py`：受约束候选查询、best-match、阈值 0.82、审计分数；
6. `decay.py`/TTL worker：保持职责分离，仅增加 expires_at 优先级和策略配置；
7. 新增独立、可回滚的数据治理脚本，先 dry-run，不修改 migration 001–014。

`recall_pipeline.py` 无需为 semantic dedup 改动；旧数据 canonical id 变化后，如召回查询支持按 subject 精确过滤，
才需复用同一 normalizer，避免读写两套规则。

## 9. 测试影响分析

本次只写方案，未运行 pytest。实施时应新增或调整以下测试：

- `tests/unit/test_dedup.py`
  - 0.82 边界使用 `>=`；
  - 返回最高分而非首个命中；
  - 同归一化 subject 近义改写合并；
  - 不同 namespace、不同不兼容 attribute 不合并；
  - 反义/冲突值即使 cosine 高也不合并；
  - audit 含 similarity/threshold/reason；
- `tests/unit/test_attribute_map.py`
  - glm/gpt/qwen/embedding/reranker 模型；
  - Windows/Unix/相对路径；
  - URL 不被 `/` 误判为 path；
  - 8200 判 port，127.0.0.1 判 network；
  - fallback slot 与高精度 override；
- `tests/unit/test_llm_extractor.py`
  - Prompt 包含实体复用、scope 正反例；
  - subject NFKC/空白处理；
  - scope/attribute 非法值 fallback；
  - 低价值版本/测试数过滤不误伤端口、日期等配置；
- 新增 entity normalizer 单测
  - 六组已知碎片映射；
  - 大小写/Unicode/空白；
  - 未知实体保持不变；
  - 配置文件缺失、非法 JSON、循环 alias 给出明确错误；
- `tests/unit/test_hybrid_priors.py`
  - TTL matrix 更新为规则驱动；
  - current state 与 durable config 的成对案例；
- `tests/unit/test_decay.py`
  - expires_at 不受 access bonus 延长；
  - permanent/temporal 边界仍兼容；
  - superseded/expired 不参与 decay；
- 应用层集成测试
  - normalized subject 在 fact hash、conflict key、dedup 和 evidence link 中一致；
  - 新模型值 supersede 旧模型值；
  - 重复事件只新增 evidence，不新增 claim；
- 数据治理脚本测试
  - dry-run 零写入；
  - 重复执行幂等；
  - 保留 evidence 和双时间历史；
  - 事务失败完整回滚。

现有测试数在背景文档中出现 95、项目说明中出现 180，属于不同时间点口径；实施验收应以当时仓库实际收集数
为准，不把固定数量写入断言。除相关断言预期外，所有现有单元测试必须继续通过。

## 10. 风险评估与缓解

| 风险 | 等级 | 缓解措施 |
|---|---:|---|
| 降阈值导致相近但不同事实误合并 | 高 | subject/attribute/conflict 三重保护；0.82 先 shadow；标注集要求 precision ≥ 0.98 |
| alias 错误把不同实体永久合并 | 高 | 只允许精确配置 alias；禁止模糊自动映射；dry-run 输出影响数量 |
| attribute 纠错改变 conflict key，触发错误 supersede | 高 | 仅高精度规则覆盖；记录 old/new/reason；历史修复需人工审核报告 |
| scope 后置规则误伤长期数字配置 | 中高 | 不采用“数字即 temporal”；结合 attribute 和语境；无高置信命中时保留 LLM |
| 历史清理破坏证据链/双时间查询 | 高 | 不删除 claim；使用 superseded/expired；单事务、备份、回滚、幂等 |
| Prompt 变长增加 token 与模型漂移 | 中 | 使用紧凑决策表；固定回归样例；记录 extractor_version |
| 规则顺序导致 URL/path/port 互相覆盖 | 中 | 高精度模式优先；表驱动参数化测试覆盖歧义案例 |
| 新旧 subject 在过渡期查询不一致 | 中 | 写入和召回共用同一 normalizer；历史 alias 保留查询兼容期 |
| repository 候选范围扩大影响性能 | 低 | 单 namespace + canonical subject 索引查询；单机 599 条规模可控，审计候选数 |

## 11. 验收标准

- 新写入的六组已知实体别名均落到唯一 canonical subject；
- 标注集 semantic dedup precision ≥ 0.98，且 recall 明显高于 0.85 基线；
- 模型名、路径、URL/IP、端口四类 attribute 回归集准确率 ≥ 95%；
- scope 分层抽样集准确率 ≥ 90%，并对 current-state/permanent 成对样例全部通过；
- 新值可通过统一 conflict slot supersede 旧版本/模型/状态；
- dry-run 治理报告可解释每条变更，正式执行保留 evidence 和历史查询；
- 不新增第三方依赖，不修改既有 migration，现有 API 契约保持兼容。
