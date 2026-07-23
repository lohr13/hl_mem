# Phase 10：ConflictResolver 假冲突修复方案

## 1. 结论

背景文档列出的三个问题都存在，但需要修正两个表述，并补充三个遗漏：

1. `.other` 的确是共享兜底属性，不具备互斥语义；不同值被判为 `contradicts` 是主要假冲突来源。
2. predicate 中英文不匹配的判断准确。当前真实数据使用归一化后的中文 predicate，因此
   `{"preference", "service_status"}` 分支对 LLM 写入路径不可达。
3. ingest 没有创建 `conflict_cases` 的判断准确；但“创建 case 后即可自动解决”不完全准确。
   `auto_resolve_conflicts()` 只接受 `status='auto_resolved'` 且两边都为 `disputed` 的 case，
   而 `uncertain` 当前产生的是 `active + candidate`。另外，确定性 `contradicts` 只在双方 authority
   相等时产生，自动按 authority 选胜者会在平局时任意保留 left，不属于低风险自动解决。
4. 遗漏一：`memory.explicit` 和 `custom.unknown` 也是共享、非互斥槽，和 `.other` 有相同风险。
5. 遗漏二：`candidate` 同样没有 case，既无法在 CLI 审核，也没有生命周期收敛路径。
6. 遗漏三：`auto_resolve_conflicts()` 使用 `left_score >= right_score`，authority 平局时无证据地偏向
   left。该问题已存在于 LLM consolidation 创建的 `auto_resolved` case 中。

建议把本次修复定义为“阻止新增假冲突 + 为真正未决冲突建立可审核 case + 禁止平局自动裁决”。
历史脏数据清理应作为独立、可回滚的数据修复任务，不能与在线判定逻辑修改混在同一事务或同一提交中。

## 2. 当前链路与数据库证据

### 2.1 写入链路

```text
LLMExtractor/FakeExtractor
  -> validate_canonical_attribute()
  -> compute_conflict_key()
       -> canonical_conflict_slot()
  -> ClaimRepository.find_by_conflict_key()
  -> ConflictResolver.resolve(existing[0], new)
  -> entails: 复用旧 claim
     state_change: supersede
     contradicts: 双方 disputed
     uncertain: 新 claim candidate，旧 claim 保持原状态
     compatible: 插入 active
  -> ingest 事务只写 claim/evidence，不写 conflict_cases
```

`find_by_conflict_key()` 会返回 `active/candidate/disputed`，但 `store_extracted()` 只判定排序后的
`existing[0]`。这不是本次假冲突的直接根因，但意味着一个槽存在多个未决 claim 时，新 claim
不会与全部候选逐一比较。修复时不应顺便改变这一行为，否则影响面会从 bugfix 扩大为多方冲突模型重构。

### 2.2 指定 SQL 的结果

2026-07-23 对 `var/hl_mem.db` 执行指定聚合查询，得到：

- `disputed`：222 条。
- `candidate`：93 条。
- `.other`：`disputed` 136 条，`candidate` 47 条，与背景文档一致。
- 最大分组为 `disputed/fact.other=100`、`candidate/fact.other=35`。
- 另有 `disputed/memory.explicit=3`，证明非互斥问题不限于 `.other`。
- `conflict_cases` 当前为 0 条；222 条 disputed 和 93 条 candidate 全部未关联 case。
- predicate 分布使用中文归一值：例如 `事实`、`配置`、`计划`、`状态`、`偏好`、`使用`；
  仅显式记忆为 `explicit_memory`。

因此三个原始判断均有直接数据证据，但历史异常不只来自 `.other`。

## 3. 修复设计

### 3.1 Bug 1：非互斥兜底槽被当成互斥槽

#### 设计

由 `attribute_map.py` 统一定义“非互斥属性”语义，避免在 resolver 中散落字符串判断。以下属性应视为
非互斥：

- 所有以 `.other` 结尾的 allowlist 属性；
- `memory.explicit`；
- `custom.unknown`。

仅当值不同才走该规则；相同值仍应先返回 `entails`，保持精确去重语义。非互斥检查必须早于时间区间、
change qualifier 和 predicate 状态变化规则，因为共享兜底槽无法证明两条记录描述的是同一逻辑属性。

#### `src/hl_mem/recall/attribute_map.py`

新增公开纯函数，现有签名不变：

```python
def is_non_exclusive_attribute(attribute: str | None) -> bool:
    """判断 canonical attribute 是否为不能据此推断冲突的共享兜底槽。"""
    if not attribute:
        return False
    normalized = normalize_canonical_attribute(attribute)
    return normalized.endswith(".other") or normalized in {"memory.explicit", "custom.unknown"}
```

#### `src/hl_mem/recall/conflict.py`

old：

```python
old_value, new_value = self._value(existing), self._value(new)
if old_value == new_value:
    return "entails"
if self._before(existing.get("valid_to"), new.get("valid_from")):
    return "state_change"
```

new：

```python
old_value, new_value = self._value(existing), self._value(new)
if old_value == new_value:
    return "entails"
if is_non_exclusive_attribute(existing_attribute) or is_non_exclusive_attribute(new_attribute):
    return "compatible"
if self._before(existing.get("valid_to"), new.get("valid_from")):
    return "state_change"
```

`ConflictResolver.resolve(existing, new) -> str` 不变，只增加 import。`compute_conflict_key()` 也不改：
共享 key 仍可用于候选聚合和审计，但“同 key”不再被错误解释为“值必须唯一”。

#### 影响面

- 新写入的 `.other`、显式记忆和未知属性不同值将保持 `active`。
- 相同值仍由 `fact_hash` 或 `entails` 合并。
- 精细互斥槽（如 `config.port`、`plan.deadline`）行为不变。
- `compatible` 分支当前不会执行语义去重。这是既有行为，不应在本修复中顺带调整；可另开去重任务。

### 3.2 Bug 2：状态变化 predicate 未归一化

#### 设计

复用 `attribute_map.normalize_predicate()`，不要同时维护中英文集合。判断使用 new claim 的 predicate，
保持现有“新事实类型决定是否为状态变化”的语义。

#### `src/hl_mem/recall/conflict.py`

old：

```python
if new.get("predicate") in {"preference", "service_status"}:
    return "state_change"
```

new：

```python
new_predicate = normalize_predicate(str(new.get("predicate", "")))
if new_predicate in {"偏好", "状态"}:
    return "state_change"
```

函数签名不变；新增 `normalize_predicate` import。

#### 影响面

- 中文 `偏好`、`状态` 恢复预期的 `state_change` 行为。
- 英文 `preference`、`service_status` 经同一映射后仍兼容。
- `status` 也会归一为 `状态`，覆盖历史英文输入。
- 该规则只在 same slot、不同值、非兜底槽、非显式时间接续、无 change qualifier 时触发。

不建议改成 `canonical_attribute.startswith(("preference.", "state."))`：predicate 是该规则现有的业务输入，
改用 attribute domain 会扩大行为，并使跨 predicate alias（例如 tool choice）更难解释。

### 3.3 Bug 3：ingest 未创建 conflict case

#### 设计

在 `store_extracted()` 的同一个 `BEGIN IMMEDIATE` 事务中创建 case，使“双方状态变化、new claim 插入、
evidence link、case 插入”原子提交。不能在事务外补写，否则会再次产生无 case 的 disputed/candidate。

为避免 ingest 与 worker 分别实现不同的 pair key，建议在 `recall/conflict.py` 新增：

```python
def compute_claim_pair_key(left_claim_id: str, right_claim_id: str) -> str:
    """按 claim ID 无序计算稳定的冲突对标识。"""
    claim_ids = sorted((left_claim_id, right_claim_id))
    return hashlib.sha256("\0".join(claim_ids).encode()).hexdigest()[:24]
```

并把 `ConflictConsolidator.scan_candidates()` 的内联 pair key 计算替换为该函数。该签名是新增接口，
不改变现有调用方签名。

在 ingest 中保留参与判定的 `current`，并在事务内插入 case：

old：

```python
if existing and resolution == "contradicts":
    claims.update_status(existing[0]["id"], "disputed", commit=False)
claims.insert_claim(claim, commit=False)
```

new（示意，字段必须与 migration 013 一致）：

```python
if current is not None and resolution == "contradicts":
    claims.update_status(current["id"], "disputed", commit=False)
claims.insert_claim(claim, commit=False)
if current is not None and resolution in {"contradicts", "uncertain"}:
    connection.execute(
        "INSERT OR IGNORE INTO conflict_cases "
        "(id,pair_key,left_claim_id,right_claim_id,status,decision,confidence,rationale,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_id(),
            compute_claim_pair_key(current["id"], claim["id"]),
            current["id"],
            claim["id"],
            "manual_required",
            resolution,
            None,
            "deterministic_ingest_resolution",
            now,
        ),
    )
```

`current` 应在 `existing` 分支前声明为 `dict[str, Any] | None = None`，避免依赖局部变量是否赋值。
`store_extracted(...) -> str` 的公开签名不变。

case 建议使用 `manual_required`，而不是伪装成 `auto_resolved`：

- `contradicts` 当前只在双方 authority 相等时产生，没有安全的 authority 胜者；
- `uncertain` 是 authority 不同但语义不确定，当前状态为 `active + candidate`，不满足自动解决器的前置条件；
- CLI 已支持审核 `pending/manual_required`，并支持激活 candidate/disputed。

如果产品必须让新 case 自动收敛，应另行定义明确的 loser 终态和严格胜出规则，不能只把 status 写成
`auto_resolved` 来绕过模型缺口。

#### 影响面

- 新增真实冲突会出现在 CLI 审核队列。
- `conflict_cases.pair_key` 的唯一约束提供幂等保护。
- `INSERT OR IGNORE` 后建议检查是否已存在同 pair case并记录审计，但不能因重复 case 回滚合法 claim 写入。
- 事务时长仅增加一次本地索引插入，风险较低。
- `consolidate.py` 只改 pair key 的计算来源，结果应完全兼容历史算法。

### 3.4 遗漏修复：禁止 authority 平局自动裁决

#### `src/hl_mem/workers/consolidate.py`

old：

```python
winner_side = "left" if left_score >= right_score else "right"
```

new：

```python
if left_score == right_score:
    connection.execute(
        "UPDATE conflict_cases SET status='manual_required' WHERE id=?",
        (case["id"],),
    )
    continue
winner_side = "left" if left_score > right_score else "right"
```

`auto_resolve_conflicts(connection, now) -> dict[str, int]` 建议把返回值扩为：

```python
{"auto_resolved": resolved, "manual_required": deferred}
```

这是本方案唯一的返回结构变化。现有 worker 调用未消费返回值，但单元测试和任何外部调用者需要确认。
若必须保持严格向后兼容，也可暂不增加键，仅更新 case 状态；不过新增计数更利于维护审计。

还应检查 `UPDATE claims` 的 `rowcount`。只有 winner 从 `disputed` 成功转为 `active` 后才能把 case
标记为 `resolved`；否则保持 case 未解决，避免 case 与 claim 状态不一致。

## 4. 历史数据处理

在线代码修复不会自动恢复现有 315 条异常 claim。由于已有 migration 不可变，应另建新 migration 或
一次性维护命令，并先备份数据库。

建议分两级处理：

1. 可确定恢复：canonical attribute 为 `*.other`、`memory.explicit`、`custom.unknown` 的
   disputed/candidate，按新语义不构成互斥冲突，可恢复为 `active`。当前至少涉及 `.other` 183 条和
   `memory.explicit` 3 条。
2. 需重放判定：`状态`、`偏好`等精细槽数据，用修复后的 resolver 按 conflict key 和时间顺序重放；
   `state_change` 建立 supersede 链，真正 contradiction 建 case。不能简单批量激活或批量 supersede。

历史修复必须先提供 dry-run 汇总（按原状态、属性、拟议新状态统计），再在单一事务中应用。该数据任务
不属于本次只写方案的交付物。

## 5. 测试影响分析

本次未运行 pytest。实施时应先新增失败用例，再修改实现。

### 5.1 直接受影响

`tests/unit/test_conflict.py`

- 现有 `test_deterministic_conflict_rules` 期望英文 `preference` 不同值为 `state_change`，归一化后继续通过。
- 新增 `.other` 不同值为 `compatible`，相同值为 `entails`。
- 新增 `memory.explicit`、`custom.unknown` 不同值为 `compatible`。
- 新增中文/英文 `偏好`、`状态/service_status/status` 精细槽不同值为 `state_change`。
- 保留精细通用槽同 authority 不同值为 `contradicts`、不同 authority 为 `uncertain`。

`tests/unit/test_attribute_map.py`

- 新增 `is_non_exclusive_attribute()` 参数化测试。
- 覆盖 `.other`、`memory.explicit`、`custom.unknown` 为 true，`config.port`、`plan.deadline` 为 false。

`tests/unit/test_pipeline.py`

- 新增不同 `fact.other` 连续写入后双方均为 active 且无 case。
- 新增精细槽 contradiction 后双方 disputed、case 为 `manual_required`，并验证 case 与 claim/evidence
  同事务落库。
- 新增 authority 不同的 uncertain 后 `active + candidate` 且创建 `manual_required` case。
- 增加异常注入用例：case 插入失败时，旧 claim 状态、新 claim、evidence 和 case 全部回滚。

`tests/unit/test_consolidate.py`

- 现有 candidate 扫描、state change 和低置信度测试不应改变。
- 新增 `compute_claim_pair_key()` 与原算法一致、左右顺序无关。
- 新增 authority 严格高者获胜。
- 新增 authority 平局转 `manual_required`，双方保持 disputed。
- 新增 winner CAS 更新失败时 case 不得标记 resolved。

### 5.2 间接受影响或需回归

- `tests/unit/test_hybrid_priors.py`：依赖 disputed 不进入当前召回的行为，状态过滤不应改变。
- CLI conflict 审核相关测试（若当前缺失应补充）：新 ingest case 应可 list/resolve/reject。
- worker maintenance 测试：若断言 `auto_resolve_conflicts()` 返回值精确相等，需要适配新增
  `manual_required` 计数。
- scenario 中明确期望 disputed 的端口、节点数量用例仍应保持；它们使用精细槽，不属于 `.other`。

不建议为兼容错误行为而保留任何“.other 不同值为 disputed/candidate”的旧断言。

## 6. 风险评估

| 风险 | 等级 | 说明与缓解 |
|---|---:|---|
| 兜底槽真实冲突被判 compatible | 中 | 兜底槽本身缺少可证明互斥的属性语义；宁可保留两条 active，也不能确定性制造假冲突。通过改进 attribute 提取精度降低兜底率。 |
| 历史脏数据继续不可见 | 高 | 在线修复只影响新写入；必须单独执行带 dry-run 和备份的数据修复。 |
| 平局不再自动收敛导致人工队列增长 | 中 | 这是避免任意裁决的必要代价；通过更细属性、来源规则或人工审核收敛。 |
| case 与 claim 状态不一致 | 高 | 所有写入放入 ingest 的既有 `BEGIN IMMEDIATE`；auto resolver 检查 CAS rowcount 后再关闭 case。 |
| pair key 算法漂移导致重复 case | 中 | 抽取 `compute_claim_pair_key()`，ingest 与 consolidator 复用同一实现，并加兼容测试。 |
| `existing[0]` 忽略同槽其他候选 | 中 | 是既有多方冲突建模限制。本次保持不变，后续应设计 conflict group，而不是在 bugfix 中循环比较并产生多 case。 |
| compatible 分支跳过语义去重 | 低 | 可能增加兜底槽近义重复，但不会制造错误状态；另行评估，避免扩大本次修改。 |
| 返回字典新增键影响调用方 | 低 | worker 当前忽略返回值；通过全局搜索和单测确认。需要最严格兼容时可不扩展返回结构。 |

## 7. 推荐实施顺序

1. 先补 `ConflictResolver` 和 attribute map 的失败测试。
2. 实现非互斥属性判断和 predicate 归一化，运行定向单测。
3. 补 ingest case 原子性测试，再实现共享 pair key 与事务内 case 插入。
4. 补 auto resolver 平局/CAS 测试，再禁止平局自动裁决。
5. 运行全部 unit tests，目标保持现有 180 项并新增用例全部通过。
6. 单独设计并审阅历史数据 dry-run/修复，不与在线逻辑修复合并提交。
