# HL-Mem 数据正确性重构与评测集方案 v2

> 设计日期：2026-07-22
> 范围：第一阶段 P0-1～P0-7 设计；第二阶段离线评测集设计。本文只定义方案，不包含实现代码。

## 1. 基线审计与结论

### 1.1 已审阅范围

已完整审阅指定的 conflict、dedup、observation、recall、ranking、写入/API、repository、database、5 个 SQL migration、全部 worker 与 LLM extractor；同时检查了相关单元/集成测试和最近提交。

关键代码事实：

- `recall/conflict.py:13` 的 `compute_conflict_key()` 使用 `namespace + subject + predicate + exclusive qualifiers`；`ConflictResolver.resolve()` 在 predicate 不同时直接返回 `compatible`。
- `api/pipeline.py:74` 先按 conflict_key 查找，`api/pipeline.py:131` 在每条 claim 写入后同步执行 `_build_observation()`。
- `recall/observation.py:11` 以分号拼接 value，并未做语义归纳。
- `storage/repository.py:99` 和 `:155` 已按 valid interval 与 expires_at 过滤，但 current query 仍允许 disputed，historical query 只按 valid time 判断且没有 recorded time语义；`hybrid_claims()` 自身没有 intent/scope/status 过滤层。
- `storage/repository.py:140` 的 `supersede()` 只更新 status、valid_to、recorded_to；没有内联新值，也没有显式 evidence supersedes link。
- `api/server.py:143` 把全部 active observations 无条件追加到每次 recall，既不按 query 检索，也不按 claim 依赖状态校验。
- `migrations/005_memory_management.sql:13` 的 FTS update trigger 仅监听 subject、predicate、value_json；因此改写 value_json 会自动同步 FTS。
- `workers/worker.py:79` 目前只分派 extract/expire/decay/retry；周期维护固定每 600 秒执行 TTL、decay 和 audit cleanup。

### 1.2 数据库实态（只读查询）

任务描述中的“82 events / 119 claims”是 2026-07-21 某条历史事件里的快照。2026-07-22 实际 `var/hl_mem.db` 为：

| 项目 | 数量 |
|---|---:|
| events | 96 |
| claims | 202 |
| active | 63 |
| candidate | 8 |
| disputed | 109 |
| expired | 2 |
| superseded | 20 |
| active observations | 3 |

全部 109 条 disputed 已逐条查询，并按现有 conflict_key 聚合如下（计数总和 109）：

| conflict_key | subject | predicate | disputed |
|---|---|---|---:|
| `74c1733c52a6d54c` | hl_mem | 事实 | 29 |
| `840a786c49b888fb` | 用户 | 计划 | 14 |
| `28c08410ba09af00` | hl_mem | 状态 | 9 |
| `e9ff98b39079581a` | xray | 配置 | 9 |
| `6acd5242c9970fea` | 用户 | 使用 | 8 |
| `e314c80c48c32b6e` | 项目 | 事实 | 7 |
| `d1c32871652e2f99` | 用户 | 事实 | 4 |
| `f90d2555a9e93b87` | 用户 | 配置 | 4 |
| `046aa8cdeae4a027` | hl_mem | 计划 | 3 |
| `7944c4b57e9526e2` | hl_mem | 使用 | 3 |
| `a0446a01c31ce895` | hl_mem | 配置 | 3 |
| `c90468638801ff2e` | MemOS | 事实 | 3 |
| `095fc764de6d6177` | 百炼 API | 事实 | 2 |
| `6a2fa65dc1039d75` | hlmem | 状态 | 2 |
| `a189aea94f2dfc75` | Hermes | 配置 | 2 |
| `a5467e55d26cf47d` | 项目 | 使用 | 2 |
| `b9aca59e60fc69f9` | Codex | 配置 | 2 |
| `fff531199b7c2b6e` | httpx | 事实 | 2 |
| `6724bef7fce03b9e` | Codex CLI | 事实 | 1 |

典型误标包括：同一 `hl_mem+事实` 下的“FTS5 修复”“双时间模型”“缺少 entity graph”等互相独立事实；同一 `xray+配置` 下端口、路由、脚本和环境变量；同一 `用户+使用` 下 Codex、V2RayN、VPS。它们并不互斥。3 条 Observation 也都来自旧的 `用户+配置` 粗键，其中 2 条仍引用 superseded claim，却保持 active，已构成召回污染。

## 2. 总体决策与实施顺序

采用“受控 canonical_attribute + 确定性 key + LLM 只处理灰区”的方案。纯静态 predicate 映射无法区分端口、路径、模型等槽；完全依赖 LLM 又会造成 key 漂移。推荐顺序：

1. 立即止损：停 Observation 写入和 API 返回。
2. 引入 canonical_attribute/schema/key v2，完成全量 backfill，但先保留旧 key 审计快照。
3. dry-run 清理 disputed，再事务性应用。
4. 上线 inline supersede 与 recall intent/双时间过滤。
5. 上线离线 consolidation worker；稳定后再启用 LLM Observation worker。
6. 冻结数据库快照，建立并运行 50 条评测集。

## 3. P0-1：重设计 conflict_key

### 当前问题（代码引用）

`recall/conflict.py:13-23` 将 predicate 直接写入 hash；`:29-30` 又在 predicate 不同情况下跳过冲突。与此同时，同 predicate 内部的不同属性会被错误合并。这造成“双向错误”：跨 predicate 漏检、同 predicate 误报。

### 设计方案

新增 claim 字段：

- `canonical_attribute TEXT NOT NULL`：受控属性槽。
- `conflict_key_version INTEGER NOT NULL DEFAULT 2`。
- `legacy_conflict_key TEXT`：迁移期保留旧值，至少保留一个版本用于审计和回滚。

canonical_attribute 使用小写 ASCII `domain.slot`。LLM extractor 必须输出该字段；应用层执行 allow-list、别名归一化和确定性回退，绝不直接信任任意 LLM 字符串。

#### 完整 predicate → attribute 映射表

“映射”是每个 predicate 的允许集合与判定优先级，不是一对一替换：

| predicate | 允许/推荐 attribute | 判定线索 | 无法判定时回退 |
|---|---|---|---|
| 偏好 | `preference.ui_theme`、`preference.response_style`、`preference.workflow`、`preference.architecture`、`preference.tool_choice`、`preference.other` | 深色/浅色；详细/简洁；本地优先；喜欢/不喜欢某工具 | `preference.other` |
| 使用 | `choice.tool`、`choice.database`、`choice.os`、`choice.model`、`choice.api`、`choice.framework`、`choice.provider`、`choice.protocol`、`choice.memory_system` | Codex/V2RayN；SQLite/PostgreSQL；qwen；DashScope；hl_mem/MemOS | `choice.tool` |
| 状态 | `state.service_health`、`state.process`、`state.deployment`、`state.test_suite`、`state.connectivity`、`state.job`、`state.other` | ok/挂了；运行中；已部署；tests passed；超时/不可达 | `state.other` |
| 身份 | `identity.name`、`identity.role`、`identity.contact`、`identity.account`、`identity.other` | 姓名/昵称；开发者/角色；邮箱/电话；用户名 | `identity.other` |
| 配置 | `config.port`、`config.path`、`config.env`、`config.network`、`config.routing`、`config.provider`、`config.model`、`config.timeout`、`config.schedule`、`config.hardware`、`config.other` | 10808；文件路径；NO_PROXY；代理；CN 直连；provider；模型；90s；cron；GPU | `config.other` |
| 计划 | `plan.goal`、`plan.deadline`、`plan.decision`、`plan.migration`、`plan.evaluation`、`plan.other` | 打算做；截止时间；选择/不切换；迁移；评测集 | `plan.other` |
| 事实 | `fact.capability`、`fact.implementation`、`fact.issue`、`fact.cause`、`fact.resolution`、`fact.constraint`、`fact.project_membership`、`fact.tool_choice`、`fact.other` | 支持/具备；已实现；缺陷；因为；已修复；只允许；有项目；“当前采用 X” | `fact.other` |
| explicit_memory | 根据内容路由到上述全部属性；仅完全无法分类时用 `memory.explicit` | `/v1/memories` 的 subject/value/qualifiers | `memory.explicit` |
| 非标准/历史 predicate | 先走 `PREDICATE_NORMALIZE`；仍未知则 `custom.<normalized_predicate>` | 英文别名或历史值 | `custom.unknown` |

关键跨 predicate 对齐别名：`preference.tool_choice`、`choice.tool`、`fact.tool_choice` 在计算 key 前统一成 key slot `tool_choice`；`choice.database`/相关事实统一为 `database_choice`；“状态事实”可统一为对应 `service_health` 等槽。存储保留细粒度 canonical_attribute，key 使用 `canonical_conflict_slot(attribute)`，便于解释与演进。

新签名：

```text
compute_conflict_key(
    namespace: str,
    subject: str,
    canonical_attribute: str,
    qualifiers: Mapping[str, Any] | None,
    *,
    version: int = 2,
) -> str
```

伪代码：

```text
function compute_conflict_key(namespace, subject, canonical_attribute, qualifiers, version=2):
    require version == 2
    ns = NFKC(namespace).strip().casefold()
    canonical_subject = NFKC(subject).remove_all_whitespace().casefold()
    attribute = normalize_attribute_alias(canonical_attribute)
    if attribute not in ATTRIBUTE_ALLOWLIST: attribute = "custom.unknown"
    slot = canonical_conflict_slot(attribute)
    exclusive = {}
    for key in [scope, context, environment, project, channel]:
        if qualifiers contains key:
            exclusive[key] = canonicalize_json_scalar_or_collection(qualifiers[key])
    raw = canonical_json(["v2", ns, canonical_subject, slot, exclusive])
    return sha256(raw.utf8).hexdigest()[0:16]
```

迁移 `006_canonical_attribute.sql` 添加三列与 `(namespace_key, conflict_key, status)` 索引。由于 SQLite 无内置 SHA-256，数据回填不能伪装成纯 SQL：新增幂等 Python data migration `storage/migrations/backfill_conflict_key_v2.py`，由 `Database._migrate()` 在 006 schema 后调用，并在 `schema_migrations` 记录独立版本 `006_data_conflict_key_v2`。单事务 `BEGIN IMMEDIATE`，逐行保存 legacy key、确定 attribute、计算 v2 key；遇到未知值回滚全批，不允许半迁移。生产部署先备份并 dry-run 输出 attribute/key 分布。

### 影响范围

`recall/conflict.py`、`ingest/extractors.py`、`ingest/llm_extractor.py`、`api/pipeline.py`、`storage/database.py`、新增 migration/data migration，及 conflict/pipeline/extractor/repository 测试。

### 测试策略

- 表驱动测试覆盖上表所有 predicate、英文别名、Unicode/空白、qualifier 顺序。
- 断言“使用 Codex”与“事实 当前采用 Codex”同 key；端口与路径不同 key。
- migration fixture 放置 v1 active/disputed/superseded 数据，验证幂等、legacy 保存、异常全回滚。
- 属性 allow-list/prompt 合同测试，未知 LLM 输出必须确定性回退。

### 预计工作量

2.0～2.5 人日（schema/backfill 0.75，extractor 0.5，冲突键与测试 1.0，数据复核 0.25）。

## 4. P0-2：清理现有 disputed 污染

### 当前问题（代码引用）

`api/pipeline.py:89-92` 对 `contradicts` 同时把旧、新 claim 设 disputed；而 resolver 对同 subject/predicate 的任意不同值，在同 authority 时倾向 `contradicts`。当前 109/202（54.0%）为 disputed，且 29 条独立事实共享一个键。

### 设计方案

新增一次性 `scripts/clean_disputed_claims.py`，默认且必须显式 `--dry-run`；应用模式要求 `--apply --backup-path <path> --report-path <path>`。

#### 假冲突判定规则

对同一 v2 conflict_key 的 claim 两两判定，优先级如下：

1. exact/fact_hash 相同：合并 evidence，保留最高 authority/最早 recorded 的 canonical claim，其他标记 `archived_duplicate`（若不扩 status，则 `archived`）。
2. 同 subject 且 value embedding cosine `> 0.85`：默认误标/近义重复；若包含明确否定或数值差异，则进入人工/LLM 灰区，不自动合并。
3. v2 key 不同：确定为旧 key 污染，恢复为 `active`（除非其 TTL/valid_to 已过期或它已被新 claim supersede）。
4. v2 key 相同但 attribute 本身可多值（capability、implementation、plan.goal 等）：判为兼容，恢复 active。
5. v2 key 相同、互斥单值槽（port/provider/model/tool_choice/state）且有效期不重叠或有 change 信号：状态变化；按时间建立 supersede 链。
6. v2 key 相同、有效期重叠、值相异且 authority 相当：保留 disputed；authority 明显更高时低权威 claim disputed，高权威 claim active。
7. cosine `0.72～0.85` 或否定/数字敏感：不自动修改，输出 `needs_review`。

基于当前聚合，`hl_mem+事实`、`项目+事实`、`xray+配置` 等大组预计大部分会因 v2 key 拆分而恢复 active；不能仅凭 0.85 cosine 自动把所有相似句设为 active，必须先经过 slot 与否定/数值保护。

#### 安全保障与回滚

```text
open source DB read-only
verify schema/version and compute source SHA-256
copy DB with SQLite backup API (not filesystem copy while WAL is live)
BEGIN IMMEDIATE on target
re-read counts; abort if changed since dry-run
write cleanup_run(run_id, source_hash, rules_version, before_counts, report_hash)
for each planned mutation:
    store before-image in claim_cleanup_history
    update status/key/value only if row still matches expected before-image
run invariants + foreign_key_check + FTS integrity check
COMMIT; emit after counts
```

回滚模式 `--rollback <run_id>` 从 `claim_cleanup_history` 恢复 before-image；同样验证当前行仍等于该 run 的 after-image，否则停止并报告并发冲突。报告必须包含全部 109 条 disputed 的 id、原值、v1/v2 key、相似度、判定、动作与原因，不把敏感 value 打入普通日志。

### 影响范围

新增 `scripts/clean_disputed_claims.py`、cleanup audit migration/table；复用 conflict/embedding/repository，补充运维文档。

### 测试策略

- 用当前 19 个 disputed 分组构造匿名化 fixture，覆盖 compatible/duplicate/state_change/true_conflict/review。
- dry-run 断言数据库字节级无业务表变化；apply 后计数和 evidence 不丢失。
- 模拟 WAL 在线备份、并发修改、执行中异常、重复执行与 rollback。
- 特别测试“10808”与“端口 10808”近义、“允许”与“不允许”反义、数值 8080/9090 真冲突。

### 预计工作量

1.5～2.0 人日（规则与报告 0.75，安全/回滚 0.5，实库复核 0.5）。

## 5. P0-3：Observation 修复

### 当前问题（代码引用）

`observation.py:18` 直接 `；`.join values；`pipeline.py:126` 每次写入同步构建；`server.py:161-169` 把所有 active observation 追加到任何查询。当前 3 条均为噪声式拼接，且 2 条引用 superseded claim。

### 设计方案

止损版本：删除/feature flag 关闭 `store_extracted()` 对 `_build_observation()` 的调用；`/v1/recall` 固定返回 `"observations": []`，`results` 不追加 observation，`total` 只计 claims。已有 derivations 保留审计但 migration 标为 `stale`，不删除。

后续新增 `ObservationSummarizer`，接口：

```text
class ObservationSummarizer:
    summarize(claims: Sequence[ClaimView], *, language: str = "zh-CN")
        -> ObservationSummary | None

ObservationSummary(summary: str, confidence: float, claim_ids: list[str], prompt_version: str)
```

输入只允许同 namespace、subject、v2 conflict_key 的 active claims；至少 2 个独立 event，排除 disputed/candidate/superseded/expired；先做 token 上限与确定性排序。输出 JSON：`{"summary": string, "supported": bool, "reason": string}`，summary 限 15～50 个中文字符，不得包含 claim 中不存在的新事实。

Prompt：

```text
系统：你是记忆归纳器。只根据给定事实写一句自然中文总结，不添加推测，不列来源，
不使用“基于N条证据”等模板。事实若互相冲突、主题不一致或不足以归纳，返回 supported=false。
优先概括稳定偏好/共同模式，不拼接原句。输出严格 JSON。
用户：subject=<...>, attribute=<...>, claims=[
  {id, value, valid_from, confidence, evidence_event_ids}, ...]
]
```

示例输出：“用户偏好本地优先、低基础设施依赖的架构方案”。

触发采用 Worker 周期而非写入时：写入只 enqueue 幂等 `summarize_observation:<conflict_key>:<watermark>`；低优先级 worker 每 30 分钟批量处理，或累计新增 2 条独立证据后立即变为可运行。理由是隔离 LLM 延迟/失败、便于批处理和幂等重试。旧 observation 在依赖 claim 状态变化时先 stale；新总结成功后原子替换。

### 影响范围

`api/pipeline.py`、`api/server.py`、`recall/observation.py`（重构为输入验证/DTO）、新增 `ingest/observation_summarizer.py` 与 worker job 分派，derivation metadata。

### 测试策略

- 止损集成测试断言任何 recall 均无 observations，claim total 不受 derivations 影响。
- Fake summarizer 测试同 key/独立 evidence/状态过滤、幂等 watermark、stale replacement。
- Prompt contract 测试 JSON、长度、unsupported；LLM 错误重试且旧有效结果不被覆盖。
- 人工抽查 30 组：事实忠实率、非拼接率、冲突拒绝率。

### 预计工作量

止损 0.5 人日；LLM 版 1.5～2.0 人日。

## 6. P0-4：Superseded 内联改写

### 当前问题（代码引用）

`repository.py:140-146` 只变更状态与双时间，旧 value 仍是裸值；API 返回时看不到 `supersedes_id` 或反向替代者。FTS 命中旧词时不能直接解释当前值。

### 设计方案

旧 claim 的 `value_json` 改为版本化 envelope；新 active claim 仍保持原始值，避免所有消费者立刻迁移：

```json
{
  "_type": "superseded_value",
  "schema_version": 1,
  "old_value": "深色模式",
  "new_value": "浅色模式",
  "superseded_by_id": "new-claim-id",
  "changed_at": "2026-01-20T00:00:00+00:00"
}
```

新增反向列 `superseded_by_id` 比每次查询 `WHERE supersedes_id=old_id` 更稳定；新 claim 的 `supersedes_id=old_id` 保持不变。增加 evidence link：derived=new claim、evidence=old claim、relation=`supersedes`。

签名：

```text
ClaimRepository.supersede_with_inline(
    old_id: str,
    new_claim_id: str,
    new_value: JSONValue,
    changed_at: str,
) -> SupersedeResult
```

伪代码：

```text
BEGIN IMMEDIATE (由 store_extracted 的整体事务持有)
old = SELECT ... WHERE id=old_id AND status IN (active,candidate,disputed)
require old exists and new_claim_id != old_id
old_value = unwrap old.value_json if already envelope else json_decode(old.value_json)
envelope = canonical_json(type/version/old_value/new_value/new_claim_id/changed_at)
UPDATE claims
 SET status='superseded', valid_to=changed_at, recorded_to=now,
     value_json=envelope, superseded_by_id=new_claim_id
 WHERE id=old_id AND status=expected_status
set new_claim.supersedes_id = old_id
INSERT evidence_link relation='supersedes' idempotently
COMMIT with new claim insertion
```

`recorded_to` 应取系统执行时间 `now`，不能错误使用业务 `changed_at`；`valid_to` 才取状态变化时间。005 的 `AFTER UPDATE OF ... value_json` trigger 会自动重建 FTS，另加 migration 级 FTS rebuild/integrity check。API 对 envelope 解码成 `text=old_value`，并返回 `replacement={id,text,valid_from}`；historical recall 可同时展示旧值与当前值，current recall 不返回旧 claim。

### 影响范围

`repository.py`、`api/pipeline.py`、`api/server.py`、新增 `superseded_by_id` migration、evidence link 唯一性约束/索引、相关测试。

### 测试策略

- 原子性、并发 compare-and-set、重复 supersede 幂等、链 A→B→C。
- 验证 valid_to 与 recorded_to 使用不同时间。
- FTS 同时可用旧值和新值命中旧 claim，current filter 仍排除它。
- API historical result 包含 replacement 且 evidence 链正确。

### 预计工作量

1.0～1.5 人日。

## 7. P0-5：scope 进入召回过滤

### 当前问题（代码引用）

`hybrid_claims()` 仅将 `as_of` 下传 repository；没有 query intent。repository 用“是否传 as_of”隐式决定 status 集合，语义不清，且 current query 仍召回 disputed。

### 设计方案

新增 `RecallIntent = current_state | historical`。API 允许显式 `intent`；未提供时简单路由：存在“当时/以前/历史/曾经/截至/as_of”或提供过去 as_of → historical，否则 current_state。路由结果写 audit。

第一版 scope 规则：

- current_state：只允许 active；排除 expired/superseded/disputed/candidate/retracted/archived。temporal claim 还须 TTL 与 valid interval 当前有效。
- historical：允许 active/superseded/expired；仍排除 candidate/retracted/archived/disputed（true conflict 后续由专门 conflict result 表达）。必须提供 as_of；若只说“历史”但无时间，可使用当前时刻并返回完整历史链，但标记 `status_filter=all`。
- `scope=permanent` 不是“永远有效”；仍检查 valid interval。`scope=temporal` 也不是 status，不能只凭 scope 丢弃仍有效 claim。

修改后伪代码：

```text
function hybrid_claims(repo, query, query_blob, limit, as_of, intent, reranker, now):
    reference = parse(as_of) if as_of else parse(now or utc_now)
    policy = RecallPolicy.for_intent(intent)
    fts = repo.search_claims_fts(query, candidate_limit, policy, reference)
    dense = repo.search_claims_vector(query_blob, candidate_limit, policy, reference)
    candidates = unique(fts + dense)
    candidates = [c for c in candidates if status_allowed(c, policy)]
    candidates = [c for c in candidates if scope_allowed(c, policy, reference)]
    candidates = [c for c in candidates if time_valid(c, reference, intent)]
    rank and rerank candidates
    return final
```

过滤必须尽量下推 SQL，pipeline 再做同一 policy 的防御性过滤，避免 FTS/vector 两路语义漂移。

### 影响范围

`api/server.py` 的 RecallInput、`recall_pipeline.py`、`repository.py`、audit detail、provider adapter（透传 intent/as_of）。

### 测试策略

状态×scope×intent 参数矩阵；显式/自动 intent 路由；FTS 与 vector 返回集合一致；current 绝不出现 superseded/expired/disputed。

### 预计工作量

1.0 人日。

## 8. P0-6：双时间进入召回逻辑

### 当前问题（代码引用）

repository 目前只检查 valid interval，并把 expires_at 同时用于 historical 查询，导致已经过期的事实无法在其历史有效时点召回；recorded_from/to 完全未参与“系统当时知道什么”的查询。`supersede()` 还把 recorded_to 错设为 new_valid_from。

### 设计方案

区分两个可选时间：`valid_as_of`（事实何时为真，沿用 API `as_of`）与未来可扩展的 `known_as_of`（系统何时知道）。第一阶段 API 只暴露 `as_of=valid_as_of`，内部函数同时支持 known_as_of，避免二次侵入。

```text
function interval_contains(start, end, point):
    s = parse_utc(start) if start else NEGATIVE_INFINITY
    e = parse_utc(end) if end else POSITIVE_INFINITY
    t = parse_utc(point)
    return s <= t and t < e

function claim_is_temporally_visible(claim, valid_as_of, known_as_of, intent):
    if not interval_contains(claim.valid_from, claim.valid_to, valid_as_of): return false
    if known_as_of is not null and
       not interval_contains(claim.recorded_from, claim.recorded_to, known_as_of): return false
    if intent == current_state:
        if claim.status != active: return false
        if claim.expires_at and parse(claim.expires_at) <= valid_as_of: return false
    else if intent == historical:
        if claim.status not in [active, superseded, expired]: return false
        # 不用“现在的 expires_at > as_of”排除；valid interval 才决定历史有效性。
    return true
```

所有时间解析统一接受 `Z`/offset，转 UTC，区间采用 `[from, to)`。无效 ISO 时间不是字符串比较回退，而是记录具体错误并拒绝该请求/隔离坏数据。TTL worker 将 expired claim 的 `valid_to` 设为 `min(existing_valid_to, expires_at)`，保证历史可见、当前不可见。

### 影响范围

`recall_pipeline.py`、`repository.py`、`ttl.py`、supersede 实现、API schema/audit，时间工具模块。

### 测试策略

- 边界前/等于 from/等于 to/边界后；naive/Z/offset；坏时间。
- expired claim 在过期前 historical 命中、当前不命中。
- valid time 与 recorded time 正交的四象限测试（事实已发生但系统尚未知等）。
- FTS/vector 与 Python 防御过滤结果一致。

### 预计工作量

1.0～1.5 人日。

## 9. P0-7：consolidate_conflicts Worker

### 当前问题（代码引用）

当前 resolver 是单条写入路径上的确定性规则，只检查同 conflict_key，无法发现旧数据、跨 predicate 或灰区语义冲突；同步调用 LLM 会增加写延迟并扩大故障面。

### 设计方案

新增 `workers/consolidate.py`：

```text
class ConflictConsolidator:
    scan_candidates(namespace, watermark, batch_size) -> list[CandidatePair]
    classify_pair(pair) -> ConsolidationDecision
    apply_decision(pair, decision, run_id) -> ApplyResult
    run_batch(limit) -> ConsolidationStats

class ConflictJudge:
    judge(left: ClaimView, right: ClaimView) -> Decision

Decision.kind = contradiction | compatible | state_change | unrelated
Decision.confidence: 0..1
Decision.rationale: short string
Decision.current_claim_id: optional
```

候选生成只扫描 active、embedding 非空、同 namespace；优先同 canonical conflict slot 或 subject，避免全局 O(n²)。余弦区间为 `[0.72, 0.95)`；`>=0.95` 交给 dedup，`<0.72` 不处理。pair key 使用排序后的 claim IDs，`consolidation_pairs` 唯一约束确保幂等；记录 embedding model/version，模型变化后允许重扫。

LLM prompt 提供 subject/attribute/value/qualifiers/valid times/authority，严格 JSON 四分类：

- contradiction：同一有效期、同一互斥属性且不能同时为真 → 两者 disputed 或按 authority 选择。
- compatible：同一主题的可并存信息 → 不改 status，记录 reviewed。
- state_change：先后状态 → 调 `supersede_with_inline(old,new)`。
- unrelated：不共享事实槽 → 不改，记录 negative pair，防止重复花费。

伪代码：

```text
for left in active claims after watermark:
    for right in ANN/full-scan neighbors(left):
        if pair already reviewed for current versions: continue
        similarity = cosine(left.embedding, right.embedding)
        if not 0.72 <= similarity < 0.95: continue
        if cheap guards say unrelated namespace or incompatible subject: continue
        decision = judge.with_retry_timeout(left, right)
        if decision.confidence < configured_threshold: queue manual review
        else in BEGIN IMMEDIATE:
            re-read both and verify status/value hash unchanged
            apply contradiction/compatible/state_change/unrelated
            insert pair audit and evidence links
```

调度：每天低峰期完整增量扫描一次（默认 03:30，本地时区由 `HL_MEM_CONSOLIDATE_CRON` 配置），每 30 分钟处理最多一个小批次 backlog；不要复用当前硬编码 600 秒维护循环。Job scheduler 每日 enqueue 唯一键 `consolidate:<UTC-date>`，Worker 普通 lease/retry；LLM timeout/retry 沿用外部 API 标准，预算独立配置。

### 影响范围

新增 `workers/consolidate.py`、pair/audit migration；修改 worker dispatch/scheduler、repository、LLM client 的可复用 JSON 调用层与配置文档。

### 测试策略

- 候选阈值 0.72/0.95 边界、同一 pair 幂等、watermark 增量、模型版本重扫。
- Fake judge 四分类分别验证无修改、disputed、inline supersede、negative cache。
- LLM 超时/坏 JSON/低信心不修改数据；并发状态变化触发 CAS 放弃。
- 当前 202 claims 快照 dry-run，人工复核 top candidates 的分类准确率。

### 预计工作量

2.0～2.5 人日。

## 10. 第二阶段：评测集设计

### 10.1 数据冻结与样本原则

不能继续引用漂移的“82/119”作为可复现基线。先用 SQLite backup API 将当前 96/202 快照复制为本地、不提交敏感原文的 fixture；同时生成脱敏 manifest：source DB SHA-256、schema versions、event/claim/status counts、抽样 claim IDs。评测 JSONL 只保留 query、期望关键词、允许 claim IDs/status/evidence event IDs，不复制无关对话全文。

选取当前库真实 claim（如 `用户/身份=本地小马`、`Hermes/provider=hl_mem`、`hl_mem/SQLite WAL`、NO_PROXY、xray 端口/脚本、重构历史、MemOS 决策），并为无答案样本选择库中不存在的实体。评测集固定 50 条，场景分布：当前偏好 8、历史偏好/替代链 9、项目配置 13、冲突更新 8、无答案 7、时间点 5。

### 10.2 标注格式

```json
{
  "id": "current-001",
  "query": "用户偏好的技术方案是什么？",
  "intent": "current_state",
  "as_of": null,
  "expected_type": "claim",
  "expected_min_confidence": 0.80,
  "expected_status_filter": "active",
  "expected_keywords": ["零基础设施", "高可靠性", "可维护性"],
  "relevant_claim_ids": ["729bdd2799064007bea19518fe8dab24"],
  "expected_evidence_event_ids": ["..."],
  "forbidden_statuses": ["superseded", "expired", "disputed"],
  "notes": "关键词满足 OR/AND 规则由 keyword_match=all 指定"
}
```

`expected_min_confidence` 校验返回 claim 自身 confidence 下限，不把 ranking score 混为 confidence。`expected_type=empty` 时 confidence/keywords/relevant IDs 为空。historical 的 `expected_status_filter=all` 表示可返回 active/superseded/expired，但仍禁止 disputed/candidate/retracted/archived。

### 10.3 50 条 query 清单

下表关键词以“全部包含”为默认；历史日期在生成 fixture 时从对应 claim 的 `valid_from/valid_to` 精确填入，避免手写日期漂移。

| ID | 场景 | query | expected_type | min_conf | status | expected_keywords |
|---|---|---|---|---:|---|---|
| C01 | 当前偏好 | 用户偏好的技术方案是什么？ | claim | 0.80 | active | 零基础设施、高可靠性、可维护性 |
| C02 | 当前偏好 | 用户主要偏好做哪类技术工作？ | claim | 0.80 | active | Python/ML |
| C03 | 当前偏好 | 用户是否希望直接切换到 MemOS？ | claim | 0.90 | active | 不要直接切换、旁路试运行 |
| C04 | 当前偏好 | 用户当前选择哪个记忆系统做主力？ | claim | 0.90 | active | hl_mem |
| C05 | 当前偏好 | 用户对 Superpowers 的态度是什么？ | claim | 0.80 | active | 不需要、交互模式 |
| C06 | 当前偏好 | 用户当前使用什么代码代理工具？ | claim | 0.80 | active | codex |
| C07 | 当前偏好 | 用户对架构基础设施依赖的偏好？ | claim | 0.80 | active | 零基础设施 |
| C08 | 当前偏好 | MemOS 在方案中扮演什么角色？ | claim | 0.90 | active | 旁路试运行 |
| H01 | 历史替代 | GPU 显存的旧记录是什么，当前硬件是什么？ | claim | 0.90 | all | 16GB、RTX 5070 Ti |
| H02 | 历史替代 | Hermes provider 从什么切换成了什么？ | claim | 0.90 | all | hindsight、hl_mem |
| H03 | 历史替代 | watchdog 之前失败，后来如何处理？ | claim | 0.90 | all | 退出码 1、Python 脚本 |
| H04 | 历史替代 | api/pipeline.py 重构前后职责如何变化？ | claim | 0.85 | all | 写入、recall_pipeline |
| H05 | 历史替代 | e2e 测试过去在哪里建数据库，现在如何处理？ | claim | 0.85 | all | 仓库根目录、tmp_path |
| H06 | 历史替代 | Codex 数据库路径的旧新关系是什么？ | claim | 0.90 | all | var/hl_mem.db、HL_MEM_DB_PATH |
| H07 | 历史替代 | NO_PROXY 之前做过什么调整，当前值是什么？ | claim | 0.85 | all | aliyuncs.com、bigmodel.cn |
| H08 | 历史替代 | ALL_PROXY 清理前后的记录是什么？ | claim | 0.85 | all | ALL_PROXY、清空 |
| H09 | 历史替代 | hl_mem 第一阶段计划后来扩展了哪些项目？ | claim | 0.80 | all | conflict_key、Observation、Superseded |
| P01 | 项目配置 | hl_mem 默认数据库路径是什么？ | claim | 0.90 | active | var/hl_mem.db |
| P02 | 项目配置 | hl_mem 使用什么数据库模式？ | claim | 0.90 | active | SQLite WAL |
| P03 | 项目配置 | hl_mem 的召回组合是什么？ | claim | 0.90 | active | embedding、FTS5、多因子排序 |
| P04 | 项目配置 | LLM 提取使用哪个模型 API？ | claim | 0.90 | active | qwen3.7-plus、百炼 |
| P05 | 项目配置 | Hermes 当前 memory provider 是什么？ | claim | 0.90 | active | hl_mem |
| P06 | 项目配置 | watchdog 多久检查一次，如何防误报？ | claim | 0.90 | active | 2 分钟、双重确认 |
| P07 | 项目配置 | Worker 的 CLI 入口在哪里？ | claim | 0.90 | active | workers/worker.py、main |
| P08 | 项目配置 | FakeEmbedder 现在位于哪里、接口是什么？ | claim | 0.90 | active | embeddings.py、bytes |
| P09 | 项目配置 | 测试文件重构后如何组织？ | claim | 0.90 | active | tests/unit、拆成 3 个文件 |
| P10 | 项目配置 | NO_PROXY 当前包含哪些域名？ | claim | 0.90 | active | aliyuncs.com、bigmodel.cn |
| P11 | 项目配置 | xray 使用哪个端口？ | claim | 0.85 | active | 10808 |
| P12 | 项目配置 | 代理分流规则是什么？ | claim | 0.85 | active | CN、直连、国际域名、代理 |
| P13 | 项目配置 | V2RayN 窗口为什么看不到？ | claim | 0.80 | active | Session 0、桌面快捷方式 |
| U01 | 冲突更新 | hl_mem 当前服务健康状态是什么？ | claim | 0.90 | active | ok |
| U02 | 冲突更新 | watchdog 当前采用什么健康检查实现？ | claim | 0.90 | active | Python 脚本 |
| U03 | 冲突更新 | api.openai.com 不通时 Codex 如何工作？ | claim | 0.80 | active | OAuth、HTTPS fallback |
| U04 | 冲突更新 | recall 500 的根因是什么？ | claim | 0.85 | active | embedding API、连接问题 |
| U05 | 冲突更新 | curl body parse 错误是服务故障吗？ | claim | 0.85 | active | 引号转义、不是服务问题 |
| U06 | 冲突更新 | xray 当前是否会随 Hermes gateway 稳定保活？ | claim | 0.90 | active | gateway 重启、丢失 |
| U07 | 冲突更新 | 当前测试通过数量是多少？ | claim | 0.90 | active | 90 tests passed |
| U08 | 冲突更新 | hl_mem 当前最关键的数据正确性缺陷有哪些？ | claim | 0.85 | active | conflict_key、Observation、scope、双时间 |
| N01 | 无答案 | 用户最喜欢的咖啡豆产地是什么？ | empty | — | active | — |
| N02 | 无答案 | 生产 Kubernetes 集群有多少节点？ | empty | — | active | — |
| N03 | 无答案 | 用户的家庭住址是什么？ | empty | — | active | — |
| N04 | 无答案 | hl_mem 使用的 Redis 端口是多少？ | empty | — | active | — |
| N05 | 无答案 | 用户下周二的航班号是什么？ | empty | — | active | — |
| N06 | 无答案 | 项目采用哪个前端框架？ | empty | — | active | — |
| N07 | 无答案 | 用户的电话号码是什么？ | empty | — | active | — |
| T01 | 时间点 | 在 watchdog 失败记录有效时，它的状态是什么？ | claim | 0.90 | all | 执行失败、退出码 1 |
| T02 | 时间点 | 在 provider 切换前 Hermes 使用什么 provider？ | claim | 0.90 | all | hindsight |
| T03 | 时间点 | 在 e2e 重构前测试数据库写在哪里？ | claim | 0.85 | all | 仓库根目录 |
| T04 | 时间点 | hl_mem 服务过期前的运行状态是什么？ | claim | 0.90 | all | 正常运行中 |
| T05 | 时间点 | 在 ALL_PROXY 清空前，代理配置是什么状态？ | claim | 0.85 | all | 系统环境变量、代理配置 |

Observation 在第一阶段止损期不作为正样本，因此所有非空 query 的 expected_type 均为 claim；另加一个 API contract 测试断言 `observations=[]`。LLM Observation 重启后，应建立独立 v2 eval 文件，不悄悄改变本基线标签。

### 10.4 指标定义

- **Recall@5**：每条非空 query 的 top 5 中是否至少包含一个 `relevant_claim_ids`；对多答案 query 同时报告 micro recall（命中相关 ID 数/相关 ID 总数）。
- **Top-1 correctness**：top 1 claim ID 属于 relevant 集合，且满足关键词、status、time/evidence 条件。只看语义文本不够。
- **No-answer precision**：系统返回 empty 的 query 中，标注确为 empty 的比例；同时报告 no-answer recall，防止通过“从不返回空”掩盖问题。无答案判定需设最小检索/置信阈值。
- **Stale/disputed hit rate**：current_state 返回结果中 `status in {superseded, expired, disputed}` 的条数 / current_state 返回总条数；目标为 0。historical 中合法 superseded/expired 不计 stale，但 disputed 始终计入。
- **Evidence correctness**：命中的 claim 至少有一个 event evidence，且 event ID 属于标注允许集合；按正确 evidence link 数/返回 evidence link 数计算 precision，并报告 missing-evidence rate。
- 辅助指标：scope leakage rate、temporal validity violation rate、historical replacement completeness、p50/p95 latency、按场景切片的 Recall@5。

验收门槛建议：Recall@5 ≥ 0.90、top-1 ≥ 0.80、no-answer precision ≥ 0.90、current stale/disputed hit rate = 0、evidence correctness ≥ 0.95。真实 reranker/embedding 结果另存 run manifest，不用 FakeEmbedder 分数冒充线上质量。

### 10.5 评测脚本结构

```text
tests/eval/
├── README.md
├── conftest.py
├── datasets/
│   ├── recall_v2.jsonl
│   └── recall_v2.manifest.json
├── fixtures/
│   └── build_snapshot.py
├── metrics.py
├── runner.py
├── test_dataset_schema.py
├── test_recall_eval.py
├── test_temporal_eval.py
├── test_no_answer_eval.py
└── reports/                 # gitignore，仅保留 README/.gitkeep
```

`conftest.py` 提供：session-scoped frozen DB copy、只读 source guard、FastAPI TestClient、真实/Fake embedder 可选 fixture、JSONL loader、固定 `now`。默认 CI 使用冻结 embedding/结果以保证确定性；带 `HL_MEM_EVAL_REAL_API=1` 才调用真实 API，并要求环境 key。

运行方式：

```bash
uv run pytest tests/eval/ -v -m "not real_api"
uv run pytest tests/eval/ -v --eval-report=var/eval/recall-v2.json
HL_MEM_EVAL_REAL_API=1 uv run pytest tests/eval/ -v -m real_api
```

Windows PowerShell 中第三条使用 `$env:HL_MEM_EVAL_REAL_API='1'` 后运行。runner 输出逐 query top-5、过滤原因、evidence、各指标和 source/model/prompt 版本；禁止覆写冻结数据库。

## 11. 里程碑、总工作量与退出条件

| 里程碑 | 内容 | 预计 |
|---|---|---:|
| M1 止损 | 关闭 Observation 写入/返回 | 0.5 人日 |
| M2 正确性底座 | canonical_attribute/key v2 + migration | 2.0～2.5 人日 |
| M3 数据修复 | disputed dry-run/apply/rollback | 1.5～2.0 人日 |
| M4 召回语义 | inline supersede + intent/scope/双时间 | 3.0～4.0 人日 |
| M5 异步智能 | consolidate + LLM observation | 3.5～4.5 人日 |
| M6 评测 | 50 条标注、fixture、metrics/runner | 2.0～2.5 人日 |
| **合计** | 含测试与实库演练，不含人工标签复核等待 | **12.5～16.0 人日** |

第一阶段退出条件：备份可恢复；109 条 disputed 均有机器报告和动作理由；current recall 零 stale/disputed；历史替代链可解释；Observation API 为空；全量单元/集成测试通过。第二阶段退出条件：50 条 query 全部绑定冻结快照与 evidence 标签，指标可在 CI 离线复现，真实 API run 单独可追踪。

## 12. 自检结论

- 无待定占位符；关键阈值、状态集合、时间边界、签名、迁移和回滚均已明确。
- canonical_attribute 同时解决跨 predicate 漏检与同 predicate 误报，没有把 predicate 简单换名。
- scope 与双时间是正交规则：scope 决定召回策略，valid/recorded interval 决定时间可见性。
- Observation 在修复前完全止损；consolidation 与 summarization 均异步，不阻塞写入。
- 评测集使用当前 96/202 实态并明确记录旧 82/119 快照，避免基线歧义。
