# Phase 11：冲突检测互斥性模型重构方案

## 1. 结论

同意 Hermes 的核心判断：互斥性必须从“默认互斥、少数豁免”翻转为“默认非互斥、显式列举互斥槽位”。但不建议原样采用其 6 个槽位，也不建议只修改 `ConflictResolver.resolve()`。

推荐方案：

1. 新增正向白名单 `MUTUALLY_EXCLUSIVE_SLOTS`，删除 `is_non_exclusive_attribute()`。
2. 删除全部 `CONFLICT_SLOT_ALIASES`；canonical attribute 本身就是冲突槽，不再跨属性合并。
3. 首版互斥白名单仅包含 5 个语义足够单值的槽：

   ```python
   MUTUALLY_EXCLUSIVE_SLOTS = frozenset({
       "preference.ui_theme",
       "preference.response_style",
       "config.port",
       "config.model",
       "state.service_health",
   })
   ```

4. `ConflictResolver` 先确认两个属性相同且都在互斥白名单内，再进行 entails、state change、contradicts、uncertain 分类。
5. `store_extracted()` 对非互斥槽跳过 conflict-key 候选查询，但仍继续语义去重，避免当前“只要 key 下存在 claim 就跳过语义去重”的旁路问题。
6. 低价值过滤放在 LLM extractor 的 `extract()` 输出边界，而不是通用 ingest 持久化边界；使用独立纯函数过滤，不把 `_claim()` 改为返回 `None`。
7. 不修改冻结的 v2 backfill migration，不重算既有 claim 的 conflict key；本阶段只改变新写入的判定路径，并另行清理既有假冲突。

## 2. 数据库验证

按任务给定条件查询当前数据库，结果如下：

| canonical_attribute | predicate | disputed/candidate 数量 |
|---|---|---:|
| `plan.deadline` | `计划` | 12 |
| `choice.tool` | `使用` | 6 |
| `fact.tool_choice` | `事实` | 2 |
| `config.env` | `配置` | 2 |

合计 22 条。

这与背景文档中的“新增 12 条假冲突”不是同一统计口径。写入 `contradicts` 时，`store_extracted()` 会同时把旧 claim 更新为 `disputed`，因此数据库状态查询可能同时统计冲突两端；背景中的 12 条更接近新产生的 claim/冲突事件数量。四类分布与背景描述一致，足以验证假冲突根因：

- `plan.deadline`、`choice.tool`、`config.env` 本身不是单值槽；
- `choice.tool` 与 `fact.tool_choice` 被跨谓词 alias 强制合并。

## 3. 对 Hermes 方案的逐点评估

### 3.1 翻转默认值：同意

正向互斥白名单符合保守原则。冲突是高影响判断：误判会把正常记忆改成 `disputed`、`candidate` 或 `superseded`，并创建 `conflict_cases`；漏判仍可由语义归并、人工审核和后续更细的槽位模型补救。当前数据已经证明“默认互斥”的误判成本更高。

### 3.2 Hermes 给出的互斥列表：部分同意

同意：

- `config.port`：同一精确 subject、scope/context/environment/project/channel 下，当前监听端口应为单值。
- `config.model`：同一精确 endpoint/component 和上下文下，当前模型应为单值。
- `state.service_health`：同一服务和上下文在一个有效时间点只有一个健康状态。

不同意直接加入：

- `identity.name`：姓名、昵称、英文名、历史名称可以并存；当前模型没有 `name_kind` qualifier。
- `identity.account`：同一用户可同时拥有多个平台账号；当前模型没有 `platform` qualifier。
- `identity.role`：同一人可同时是研究者、开发者、维护者；当前模型没有 `organization/project` 之外的稳定角色维度，且 qualifier 缺失时误判概率高。

Hermes 还遗漏了两个适合显式互斥的偏好槽：

- `preference.ui_theme`：同一上下文下当前 UI 主题单值。
- `preference.response_style`：同一 channel/context 下当前回复风格单值。

它们已有 predicate 归一化及偏好 state-change 语义，且 qualifiers 中的 `context`、`project`、`channel` 会进入 conflict key，可区分常见使用场景。

### 3.3 `CONFLICT_SLOT_ALIASES` 大幅缩减：结论为全删

当前 alias 均不具备安全的等价关系：

| alias | 结论 | 理由 |
|---|---|---|
| `preference.tool_choice → tool_choice` | 删除 | “偏好某工具”不等于“正在使用某工具” |
| `choice.tool → tool_choice` | 删除 | 工具使用是多值集合，不应形成单值冲突槽 |
| `fact.tool_choice → tool_choice` | 删除 | 客观事实与使用陈述可能描述不同时间、项目或来源 |
| `choice.database → database_choice` | 删除 | 没有其他属性映射到该值，纯重命名无收益 |
| `config.network → config.port` | 删除 | 网络配置不等于端口配置，语义错误 |

`ATTRIBUTE_ALIASES` 应保留。它处理的是同一 canonical attribute 的输入拼写兼容，例如 `choice.tool_choice → choice.tool`，与跨语义冲突槽合并不是一回事。

### 3.4 在 `_claim()` 增加短值校验：同意目标，不同意放置和简单规则

不建议让 `_claim()` 返回 `ExtractedClaim | None`，也不建议在 `store_extracted()` 中无条件过滤：

- `_claim()` 当前职责是解析和规范化一个结构化 claim，保持总是返回 `ExtractedClaim` 更清晰，也避免所有直接单测和调用方处理 `None`。
- `store_extracted()` 是 FakeExtractor、未来其他 extractor 与通用应用服务的共享入口。短值可能是合法事实，例如端口 `8080`、语言 `C`、地区 `北京`；在这里丢弃会扩大行为变化，并且当前返回类型是 `str`，跳过写入需要修改 worker/API 契约。
- “串行”只有两个中文字符，但可能是重要执行约束。单纯 `len(value) < 5` 不是可靠的信息价值判定。

建议在 `LLMExtractor.extract()` 中，对 `_claim()` 产物执行 extractor 专属后置过滤。首版规则应与 prompt 的可确定部分一致：

- 空字符串：过滤；
- 仅数字和点号组成：过滤，例如 `180`、`1.2.3`；
- 短服务健康噪声：当 canonical attribute 为 `state.service_health` 且规范化值属于受控噪声集合（如 `ok`、`running`、`stopped`、`健康`、`正常`）时过滤；
- 不采用裸 `len(value.strip()) < 5` 作为充分条件。

如果产品要求严格兑现 prompt 中“少于 5 个字符不提取”，也应只在 LLM extractor 应用，并在测试中显式记录会丢弃 `8080`、`北京` 等合法短值的取舍。推荐方案不接受这一高误杀规则。

### 3.5 Hermes 遗漏的关键点

Hermes 方案还需补充以下内容：

1. **候选查询必须同步收窄。** 只让 resolver 返回 `compatible` 仍会使 `store_extracted()` 因 `existing` 非空而跳过 semantic dedup 分支，造成重复记忆累积。
2. **缺失 canonical attribute 时必须默认 compatible。** 旧逻辑回退到 predicate 相等，会重新引入“同谓词即互斥”的错误默认值。
3. **相同值判断只应发生在互斥判断之后。** 非互斥集合中两个值相同是否为重复，应由 fact hash 或 semantic dedup 负责，不应由 conflict resolver 返回 `entails`。
4. **冻结 migration 不应引用可变运行时代码。** 当前 `backfill_conflict_key_v2.py` 声明算法已快照，但实际仍 import `recall.attribute_map` 和 `recall.conflict`。本阶段不修改 migration；应登记为独立技术债，未来把 v2 算法真正内嵌冻结。
5. **既有假冲突不会自动恢复。** 新逻辑只阻止新增误判，现有 22 条 `disputed/candidate` 需要独立、可审计的数据修复流程，不能在本次代码重构中静默改状态。

## 4. `MUTUALLY_EXCLUSIVE_SLOTS` 完整列表及理由

“完整”以当前 `ATTRIBUTE_ALLOWLIST` 和现有 qualifier 模型为边界，不推测未来槽位。

| 槽位 | 纳入理由 | 前提 |
|---|---|---|
| `preference.ui_theme` | 当前主题在同一 UI 上下文中单值 | 不同 context/project/channel 必须进入 qualifier |
| `preference.response_style` | 当前回复风格在同一交互上下文中单值 | 不同 channel/context 必须区分 |
| `config.port` | 精确服务实例的当前监听端口单值 | subject 必须是服务/组件，而非宽泛“用户” |
| `config.model` | 精确端点或组件的当前模型单值 | 多模型场景必须拆 subject 或 environment/context |
| `state.service_health` | 同一服务在同一有效时间点状态单值 | 依赖 valid time 或变更信号形成 state change |

其余 allowlist 全部默认非互斥，主要原因如下：

- `preference.workflow`、`preference.architecture`、`preference.tool_choice`：可同时偏好多种工作流、架构原则或工具。
- `choice.*`：描述使用集合；用户、项目或系统通常同时使用多个数据库、OS、API、框架、provider、协议和工具。
- `state.process/deployment/test_suite/connectivity/job`：槽位仍过粗，一个 subject 下可有多个进程、部署、测试套件、连接或任务；需要更细 subject/qualifier 后才能转为互斥。
- `identity.*`：名称、联系方式、账号和角色均可能多值。
- `config.path/env/network/routing/provider/timeout/schedule/hardware`：一个系统可有多个路径、变量、路由、provider、超时项、计划和硬件。
- `plan.*`：同一主体可并行拥有多个目标、截止日期、决策、迁移和评估。
- `fact.*`：事实天然是集合，canonical attribute 只是分类而非单值字段。
- `*.other`、`memory.explicit`、`custom.unknown`：兜底或自由文本，不具备可证明的互斥语义。

白名单不是永久 schema。未来若新增 `qualifiers` 维度（如 `account.platform`、`config.key`、`process.name`），可以在数据契约和提取 prompt 同步收紧后再扩充。

## 5. `state.service_health` 与 state-change

当前 predicate 归一化覆盖 `status`、`service_status`、`状态`，都会变成 `状态`；`ConflictResolver` 对规范化后的 `偏好`、`状态` 返回 `state_change`。此外，以下任一条件也会形成 state change：

- 旧 `valid_to <=` 新 `valid_from`；
- 新 claim qualifier 含 `state_change`、`current` 或 `change`。

因此 predicate 覆盖本身足够，但当前规则“所有状态 predicate 都是 state change”过宽。白名单生效后，该规则只会在已经确认是互斥的 `state.service_health` 上运行，风险显著降低。

建议保留判定顺序：

1. 两边 canonical attribute 完整且相同；
2. 槽位在互斥白名单；
3. 值相同则 `entails`；
4. 时间不重叠或存在显式 change signal 则 `state_change`；
5. 规范化 predicate 为 `偏好`/`状态` 则 `state_change`；
6. 同 authority 为 `contradicts`，否则 `uncertain`。

对于缺少 canonical attribute 的历史/畸形输入，直接返回 `compatible`，不再按 predicate 回退。

## 6. 具体 old → new 代码改动

本节是后续实现清单，本次仅写方案，不修改源码。

### 6.1 `src/hl_mem/recall/attribute_map.py`

Old：

```python
CONFLICT_SLOT_ALIASES = {...}

def is_non_exclusive_attribute(attribute: str | None) -> bool:
    ...

def canonical_conflict_slot(attribute: str) -> str:
    ...
    return CONFLICT_SLOT_ALIASES.get(normalized, normalized)
```

New：

```python
MUTUALLY_EXCLUSIVE_SLOTS = frozenset({
    "preference.ui_theme",
    "preference.response_style",
    "config.port",
    "config.model",
    "state.service_health",
})

def canonical_conflict_slot(attribute: str) -> str:
    """返回经校验的 canonical conflict slot，不跨属性合并。"""
    normalized = normalize_canonical_attribute(attribute)
    return normalized if normalized in ATTRIBUTE_ALLOWLIST else "custom.unknown"

def is_mutually_exclusive_attribute(attribute: str | None) -> bool:
    """判断 canonical attribute 是否可参与确定性冲突检测。"""
    if not attribute:
        return False
    return canonical_conflict_slot(attribute) in MUTUALLY_EXCLUSIVE_SLOTS
```

删除 `CONFLICT_SLOT_ALIASES` 和 `is_non_exclusive_attribute()`。保留 `canonical_conflict_slot()` 作为规范化边界，避免扩散 allowlist 校验细节。

### 6.2 `src/hl_mem/recall/conflict.py`

Old：

```python
if existing_attribute and new_attribute:
    same_slot = canonical_conflict_slot(existing_attribute) == canonical_conflict_slot(new_attribute)
else:
    same_slot = existing.get("predicate") == new.get("predicate")
if not same_slot:
    return "compatible"
old_value, new_value = ...
if old_value == new_value:
    return "entails"
if is_non_exclusive_attribute(...):
    return "compatible"
```

New：

```python
if not (
    is_mutually_exclusive_attribute(existing_attribute)
    and is_mutually_exclusive_attribute(new_attribute)
):
    return "compatible"
if canonical_conflict_slot(existing_attribute) != canonical_conflict_slot(new_attribute):
    return "compatible"

old_value, new_value = self._value(existing), self._value(new)
if old_value == new_value:
    return "entails"
```

`compute_conflict_key()` 的结构和版本仍保持 v2，仅因 `canonical_conflict_slot()` 不再应用 alias，新写入的跨属性 key 自然分离。不要把版本改成 v3：此次 key 的序列化结构未变，且 alias 移除是错误修正；若决定回填全库或改变 key payload，才应设计 v3 migration。

### 6.3 `src/hl_mem/application/ingest.py`

Old：

```python
existing = claims.find_by_conflict_key(claim["conflict_key"])
...
if existing:
    ...
else:
    # semantic dedup
```

New：

```python
exclusive = is_mutually_exclusive_attribute(canonical_attribute)
existing = claims.find_by_conflict_key(claim["conflict_key"]) if exclusive else []
...
if existing:
    ...
else:
    # semantic dedup，非互斥 claim 必须进入这里
```

这使 conflict-key 查询成为显式互斥槽的专用路径，并保证非互斥 claim 仍经过 dense embedding 与 semantic dedup。

仍保留 resolver 的互斥检查，形成应用层候选选择与领域判定的双重防护。不要仅依赖 `store_extracted()` 的过滤，因为 resolver 还有直接单测和潜在其他调用方。

### 6.4 `src/hl_mem/ingest/llm_extractor.py`

Old：

```python
return [self._claim(item) for item in claims if isinstance(item, dict)]
```

New：

```python
parsed = [self._claim(item) for item in claims if isinstance(item, dict)]
return [claim for claim in parsed if not _is_low_value_claim(claim)]
```

新增模块级纯函数：

```python
LOW_VALUE_HEALTH_STATES = frozenset({"ok", "running", "stopped", "健康", "正常"})
NUMERIC_OR_VERSION_RE = re.compile(r"[0-9.]+")

def _is_low_value_claim(claim: ExtractedClaim) -> bool:
    value = unicodedata.normalize("NFKC", str(claim.value)).strip()
    if not value:
        return True
    if NUMERIC_OR_VERSION_RE.fullmatch(value):
        return True
    return (
        claim.canonical_attribute == "state.service_health"
        and value.casefold() in LOW_VALUE_HEALTH_STATES
    )
```

实际实现时应为新增常量和公开/模块函数补充符合项目约定的中文 docstring，并导入 `unicodedata`。噪声集合如需可运维配置，应进入 `config.py` 或 settings；首版固定的语义枚举不是运行时参数，不属于禁止硬编码的端口、模型名、路径等配置。

不修改 `_claim()` 的返回类型，不在 `store_extracted()` 返回空 ID，也不阻止显式记忆接口保存短文本。

## 7. 测试影响分析

项目说明中的“180 个测试”是历史基线；本次未运行 pytest，以下按当前测试源码静态分析。

### 7.1 必须修改的现有测试

`tests/unit/test_attribute_map.py`：

- `test_canonical_conflict_slot_aliases` 当前断言跨属性 alias，应改为断言 canonical slot 保持自身。
- 新增参数化测试覆盖 5 个互斥槽为 `True`，其余代表槽为 `False`。
- 保留 unknown attribute 回退 `custom.unknown` 的断言。

`tests/unit/test_conflict.py`：

- `test_conflict_key_aligns_cross_predicate_tool_choice_slots` 应反转为 key 不相等。
- `test_conflict_key_keeps_nonexclusive_configuration_slots_separate` 中 `config.network == config.port` 应改为不相等，四者应全部不同。
- `test_resolver_compares_different_predicates_in_same_canonical_slot` 应改为 `compatible`。
- `test_deterministic_conflict_rules` 目前未提供 canonical attribute，旧 predicate fallback 会消失；需要为真正互斥用例补上 `preference.ui_theme`，generic 缺属性用例应期望 `compatible`。

`tests/unit/test_hybrid_priors.py`：

- `test_llm_claim_parses_and_clamps` 与 `test_llm_claim_invalid_defaults_and_prompt` 直接调用 `_claim()`，推荐方案下不受影响。这也是不把过滤塞进 `_claim()` 的兼容性收益。

### 7.2 必须新增的测试

`tests/unit/test_conflict.py`：

- 同一 `plan.deadline` 不同值返回 `compatible`。
- 同一 `choice.tool` 不同值返回 `compatible`。
- `choice.tool` 与 `fact.tool_choice` 返回 `compatible` 且 key 不同。
- 同一 `config.port` 不同值：显式 change 为 `state_change`，同 authority 且无 change 为 `contradicts`。
- 同一 `state.service_health` 不同值返回 `state_change`。
- 缺失 canonical attribute 即使 predicate 相同也返回 `compatible`。
- 非互斥槽相同值不由 resolver 返回 `entails`。

`tests/unit/test_pipeline.py`：

- 非互斥相同 conflict key 场景仍执行 semantic dedup。
- `plan.deadline`、`choice.tool`、`config.env` 多值写入后均保持 `active`，不创建 `conflict_cases`。
- `config.port` 冲突仍进入 disputed/candidate/state-change 路径。

`tests/unit/test_llm_extractor.py`：

- 空值、纯数字、纯版本号和受控短健康状态被过滤。
- `8080` 按推荐规则会被过滤，因为它是纯数字；若端口必须保留，应进一步使用 canonical attribute 例外，这需要产品决策并在测试中固定。
- `串行`、`北京`、`Codex` 等短但有语义的值保留。
- `_claim()` 仍可独立解析短值，证明过滤属于 extractor 输出策略而不是 DTO 解析。

### 7.3 应保持不变的测试

- `tests/unit/test_pipeline.py` 中 fact-hash 精确去重、v2 key 字段写入。
- `tests/unit/test_backfill_conflict_key_v2.py`：冻结 migration 的历史期望不应随新运行时 alias 改动漂移。由于当前 migration 实际 import 运行时代码，实施前必须先确认这些测试是否暴露漂移；若暴露，应在独立修复中真正快照旧算法，而不是改历史期望。
- recall、decay、TTL、ranking、experience 等与冲突候选选择无关的测试。

## 8. 向后兼容与数据处理

### 8.1 新写入

新写入立即采用显式互斥白名单。非互斥 claim 不再创建争议状态或 conflict case。

### 8.2 既有数据

不在本次实现中自动重算所有 `conflict_key`，原因：

- v2 key 被审计、冲突 case 和召回冲突包引用；
- 直接更新 key 不能自动判断哪一方应恢复为 `active`；
- 某些 disputed claim 可能是真冲突，不能按属性批量无条件恢复。

建议独立制作 dry-run 修复脚本：

1. 仅扫描 canonical attribute 不在互斥白名单内的 `candidate/disputed`；
2. 检查对应 `conflict_cases`、supersede 关系和是否存在其他活跃 peer；
3. 输出备份与审计报告；
4. 经人工确认后恢复状态并关闭/拒绝错误 conflict case。

该数据修复不属于本方案要求的代码改动范围。

## 9. 风险评估

| 风险 | 等级 | 说明 | 缓解措施 |
|---|---|---|---|
| 真冲突漏判 | 中 | 白名单保守，identity、plan、choice 等不再自动冲突 | 依赖 semantic dedup、后续 consolidate；用真实数据迭代白名单 |
| `config.port/model` 因 subject 过宽误判 | 中 | “用户”的多个服务端口仍可能碰撞 | 强化 extractor 的 subject 具体化；确保 environment/project/context qualifier 完整 |
| 偏好槽误 supersede | 中 | 不同应用主题若缺 context 会被视为变化 | prompt 要求生成具体 subject/context；增加跨 context 测试 |
| conflict key v2 语义漂移 | 中 | 同版本下 alias 行为变化，新旧 key 并存 | 不回填旧数据；记录变更时间；若需全库一致性则另立 v3 migration |
| 冻结 migration 漂移 | 高 | migration 声称快照却 import 可变函数，新数据库重放结果可能变化 | 实施前单独冻结旧算法；不可修改已发布 SQL migration |
| 低价值过滤误杀 | 中 | 纯数字可能是合法端口、年份、数量 | 过滤仅限 LLM extractor；增加 canonical attribute 例外或提升 prompt 质量 |
| 非互斥路径计算 embedding 增加成本 | 低 | 过去因 key 碰撞跳过 semantic dedup，现在恢复正确流程 | 监控 embedding 调用量和 dedup 命中率 |
| 既有 22 条假冲突残留 | 高 | 代码修复不会自动恢复数据库状态 | 独立 dry-run、备份、审计式数据修复 |

## 10. 验收标准

1. `plan.deadline`、`choice.tool`、`fact.tool_choice`、`config.env` 的不同值不会产生 deterministic conflict。
2. `choice.tool` 与 `fact.tool_choice` 的 conflict key 不同。
3. 5 个白名单槽仍能稳定产生 entails/state_change/contradicts/uncertain。
4. 非互斥 claim 仍执行 exact dedup 和 semantic dedup。
5. 缺失或未知 canonical attribute 默认 compatible。
6. LLM 输出中的空值、纯数字/版本及受控健康噪声被过滤，合法短文本不因长度 alone 被丢弃。
7. 不修改 001–014 migration，不静默改动既有 claim 状态。
8. 相关 unit tests 全部通过后才进入数据修复；本方案编写阶段不运行 pytest。
