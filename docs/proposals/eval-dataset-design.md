# HL-Mem 三套评测数据集构造方案

## 总体原则

1. 先冻结一个 corpus snapshot：1134 条 active claim 的 ID、文本、slot、predicate、qualifiers、状态及 embedding 元数据，并计算 fingerprint。
2. 所有 SQL 只生成候选，不产生 gold。
3. LLM 只用于改写、生成 query 草稿和证据 span 建议，不参与最终标注。
4. 标注界面隐藏 cosine、旧 LLM decision、来源类别，避免锚定偏差。
5. 使用固定 seed 做可复现抽样；不要用 `ORDER BY random()`。
6. dev/test 按来源实体分组切分，建议 60%/40%。test 在方案选择前不得用于调阈值。
7. 所有数据保存 claim/event 文本快照，不能只保存数据库 ID，否则数据库变化后无法复现。

---

# 1. Claim-pair 等价判定集

## 1.1 规模与组成

前一轮的 `80+50+50+80+50+30=340` 应视为候选生成上限，不是最终冻结数。

建议：

- 生成约 340–400 对候选。
- 去掉重复 pair、无效 claim 后人工标注。
- 最终冻结约 270 对：
  - 主评测集约 250 对。
  - `uncertain` 审计集不超过 20 对，不参与主指标。
- 尽量得到：
  - equivalent：约 90
  - compatible：约 55
  - conflict：约 55
  - unrelated：约 50
  - uncertain：不超过 20

如果自然等价正例不足，用真实 claim 的受控改写补足，不应通过改变标签配额“硬凑”。

另外，原建议中的“30 对随机易负例”建议改成“30 对按当前 dedup 候选分布抽取的 operational control”。从全部 claims 随机配对几乎全是无关项，价值很低。

## 1.2 基础快照 SQL

所有查询使用只读连接，并先执行：

```sql
PRAGMA query_only = ON;
```

冻结 active claims：

```sql
SELECT
    id,
    namespace_key,
    subject_entity_id,
    predicate,
    value_json,
    COALESCE(qualifiers_json, '{}') AS qualifiers_json,
    canonical_attribute,
    canonical_slot,
    conflict_key,
    index_text,
    status,
    recorded_from,
    valid_from,
    valid_to,
    embedding_model,
    embedding_dim,
    embedding_dense
FROM claims
WHERE status = 'active'
  AND index_text IS NOT NULL
ORDER BY id;
```

`embedding_dense` 只用于离线计算 baseline cosine；冻结数据集正文不必保存 BLOB，只保存计算出的分数和 embedding signature。

## 1.3 候选类别

### A. 同 subject + slot，按 cosine 分层：生成 80 对

SQL 生成 pair universe：

```sql
WITH active AS (
    SELECT
        id,
        namespace_key,
        subject_entity_id,
        predicate,
        value_json,
        qualifiers_json,
        canonical_slot,
        index_text,
        embedding_dense
    FROM claims
    WHERE status = 'active'
      AND canonical_slot IS NOT NULL
      AND embedding_dense IS NOT NULL
)
SELECT
    l.id AS left_id,
    r.id AS right_id,
    l.subject_entity_id,
    l.canonical_slot,
    l.index_text AS left_text,
    r.index_text AS right_text,
    l.value_json AS left_value,
    r.value_json AS right_value,
    l.qualifiers_json AS left_qualifiers,
    r.qualifiers_json AS right_qualifiers,
    l.embedding_dense AS left_embedding,
    r.embedding_dense AS right_embedding
FROM active l
JOIN active r
  ON l.namespace_key = r.namespace_key
 AND l.subject_entity_id = r.subject_entity_id
 AND l.canonical_slot = r.canonical_slot
 AND l.id < r.id;
```

候选生成器使用当前 v4 embedding 重算 cosine，并分层抽取：

- `[0.95, 1.00]`：20 对
- `[0.90, 0.95)`：20 对
- `[0.82, 0.90)`：20 对
- `[0.60, 0.82)`：20 对

限制：

- 单一 subject 不超过这一类别的 40%。
- 单一 slot 不超过 20%。
- 同一 claim 最多进入 3 对。
- 不足的分层从相邻区间补，不降低到完全无关区间。

当前 active 数据中同 subject+slot 有约 7846 个潜在 pair，数量足够，但分布会被 `hl_mem/config.env` 主导，因此必须设上限。

### B. FTS 重叠高、cosine 低：生成 50 对

先导出 FTS token：

```sql
SELECT
    c.id,
    c.subject_entity_id,
    c.predicate,
    c.canonical_slot,
    c.index_text,
    f.terms
FROM claims c
JOIN claims_fts_v2 f ON f.rowid = c.rowid
WHERE c.status = 'active'
  AND c.embedding_dense IS NOT NULL;
```

对每个 anchor：

1. 从 `terms` 中选择 2–4 个低 document-frequency token。
2. 查询 FTS top 10：

```sql
SELECT
    c.id,
    c.index_text,
    bm25(claims_fts_v2) AS fts_score
FROM claims_fts_v2
JOIN claims c ON c.rowid = claims_fts_v2.rowid
WHERE claims_fts_v2 MATCH :fts_query
  AND c.status = 'active'
  AND c.id <> :anchor_id
ORDER BY bm25(claims_fts_v2), c.id
LIMIT 10;
```

3. 过滤 baseline cosine `< 0.82`。
4. 按 FTS 排名、token overlap 和 cosine 区间分层抽 50 对。

这一类别重点覆盖：

- 相同技术名词但事实不同。
- 路径前缀相同。
- 版本号或配置键相同。
- 文本包含关系明显，但不一定等价。

不要直接用 `index_text` 全文作为 MATCH 表达式，标点和高频词会产生不稳定结果。

### C. 跨 subject/实体别名：生成 50 对

先导出 active claims，再使用生产代码中的 `normalize_entity_id()` 和 `DEFAULT_ENTITY_ALIASES` 分组。候选条件：

- 原始 subject 不同。
- 归一化 subject 相同；或
- 同 slot/predicate 且 baseline cosine 位于跨 subject top-K。

SQL 导出：

```sql
SELECT
    id,
    namespace_key,
    subject_entity_id,
    predicate,
    canonical_slot,
    value_json,
    qualifiers_json,
    index_text,
    embedding_dense
FROM claims
WHERE status = 'active'
  AND subject_entity_id IS NOT NULL
  AND embedding_dense IS NOT NULL
ORDER BY subject_entity_id, id;
```

优先覆盖：

- `hlmem / hl_mem / hl_mem 项目 / hl_mem 服务`
- `Hermes / Hermes 插件 / hermes-agent`
- `Codex / Codex CLI`
- `Hindsight / hindsight`
- `用户 / user / 本地小马`，但这些不能自动认定为同一实体。

另外把 17 条 `dedup_pairs` pending 全部加入候选池：

```sql
SELECT
    dp.id AS source_pair_id,
    dp.left_claim_id AS left_id,
    dp.right_claim_id AS right_id,
    dp.similarity,
    l.subject_entity_id AS left_subject,
    r.subject_entity_id AS right_subject,
    l.index_text AS left_text,
    r.index_text AS right_text
FROM dedup_pairs dp
JOIN claims l ON l.id = dp.left_claim_id
JOIN claims r ON r.id = dp.right_claim_id
WHERE dp.decision IS NULL
ORDER BY dp.id;
```

### D. Hard negatives：生成 80 对

以“相同 conflict_key/subject+slot，但关键值不同”为基础池：

```sql
WITH active AS (
    SELECT
        id,
        namespace_key,
        subject_entity_id,
        predicate,
        canonical_slot,
        conflict_key,
        value_json,
        qualifiers_json,
        index_text,
        valid_from,
        valid_to,
        occurred_start,
        occurred_end
    FROM claims
    WHERE status = 'active'
)
SELECT
    l.id AS left_id,
    r.id AS right_id,
    l.canonical_slot,
    l.index_text AS left_text,
    r.index_text AS right_text,
    l.value_json AS left_value,
    r.value_json AS right_value,
    CASE
        WHEN l.index_text GLOB '*[0-9]*'
         AND r.index_text GLOB '*[0-9]*' THEN 1 ELSE 0
    END AS has_number,
    CASE
        WHEN instr(lower(l.index_text), '版本') > 0
          OR instr(lower(r.index_text), '版本') > 0
          OR lower(l.index_text) GLOB '*v[0-9]*'
          OR lower(r.index_text) GLOB '*v[0-9]*'
          OR l.canonical_slot = 'config.version'
          OR r.canonical_slot = 'config.version'
        THEN 1 ELSE 0
    END AS has_version,
    CASE
        WHEN instr(l.index_text, '/') > 0
          OR instr(r.index_text, '/') > 0
          OR instr(l.index_text, '\') > 0
          OR instr(r.index_text, '\') > 0
        THEN 1 ELSE 0
    END AS has_path,
    CASE
        WHEN l.index_text GLOB '*不*'
          OR r.index_text GLOB '*不*'
          OR l.index_text GLOB '*未*'
          OR r.index_text GLOB '*未*'
          OR l.index_text GLOB '*禁止*'
          OR r.index_text GLOB '*禁止*'
          OR l.index_text GLOB '*拒绝*'
          OR r.index_text GLOB '*拒绝*'
          OR lower(l.index_text) GLOB '*not *'
          OR lower(r.index_text) GLOB '*not *'
        THEN 1 ELSE 0
    END AS has_negation
FROM active l
JOIN active r
  ON l.id < r.id
 AND l.namespace_key = r.namespace_key
 AND (
       (l.conflict_key IS NOT NULL AND l.conflict_key = r.conflict_key)
       OR (
            l.subject_entity_id = r.subject_entity_id
        AND l.canonical_slot IS NOT NULL
        AND l.canonical_slot = r.canonical_slot
       )
 )
WHERE l.value_json <> r.value_json;
```

按以下子类各取约 12–16 对：

- 否定或禁止关系。
- 数字/单位不同。
- 版本号不同。
- 路径不同或父子路径。
- 时间 qualifier/current-state 不同。
- 环境变量、端口、provider、model 等配置值不同。

`consolidation_pairs` 的 contradiction/state_change 应全部进入候选池，包括已经 superseded 的历史 claim：

```sql
SELECT
    cp.pair_key,
    cp.left_claim_id AS left_id,
    cp.right_claim_id AS right_id,
    cp.similarity,
    cp.decision AS hidden_previous_decision,
    l.status AS left_status,
    r.status AS right_status,
    l.index_text AS left_text,
    r.index_text AS right_text
FROM consolidation_pairs cp
JOIN claims l ON l.id = cp.left_claim_id
JOIN claims r ON r.id = cp.right_claim_id
WHERE cp.decision IN ('contradiction', 'state_change', 'manual_review')
ORDER BY cp.pair_key;
```

现有 decision 只能作为抽样 slice，标注时必须隐藏。尤其不能把 `compatible` 自动映射成 `equivalent`。

### E. LLM 辅助改写正例：生成 50 对

从真实 active claim 中分层选 anchor：

```sql
WITH ranked AS (
    SELECT
        id,
        subject_entity_id,
        predicate,
        canonical_slot,
        value_json,
        qualifiers_json,
        index_text,
        ROW_NUMBER() OVER (
            PARTITION BY COALESCE(canonical_slot, 'predicate:' || predicate)
            ORDER BY id
        ) AS rn
    FROM claims
    WHERE status = 'active'
      AND length(index_text) BETWEEN 12 AND 240
      AND value_json IS NOT NULL
)
SELECT *
FROM ranked
WHERE rn <= 8
ORDER BY COALESCE(canonical_slot, predicate), id;
```

再用固定 seed 选 50 个 anchor。改写提示必须要求：

- 不改变 subject、极性、模态和时间。
- 数字、单位、版本号、路径、端口必须原样保留。
- 不增加原 claim 没有的因果或范围。
- 只输出一个原子 claim。

LLM 输出不是自动正例，必须人工确认。synthetic ID 使用：

```text
synthetic:<anchor_claim_id>:paraphrase-1
```

### F. Operational control：30 对

从“当前生产 dedup 实际会考虑的候选分布”中固定 seed 均匀抽样，而不是从全部 claim 随机组合。

来源包括：

- 同 subject+slot。
- 无 slot 时同 subject+predicate。
- 跨 subject dedup 候选。
- 当前阈值附近候选。

该 slice 用于检查 challenge set 上的结论是否偏离生产分布。总体 accuracy 不应跨 challenge/operational 直接混算，应分别报告。

## 1.4 标注格式

建议 JSONL：

```json
{
  "schema_version": 1,
  "pair_id": "pair-0001",
  "source_slice": "same_subject_slot",
  "left": {
    "claim_id": "01...",
    "subject": "hl_mem",
    "predicate": "配置",
    "value": "embedding 模型为 text-embedding-v4",
    "canonical_slot": "choice.model",
    "qualifiers": {"task": "embedding"},
    "status_at_snapshot": "active"
  },
  "right": {
    "claim_id": "01...",
    "subject": "hl_mem",
    "predicate": "使用",
    "value": "向量模型采用 text-embedding-v4",
    "canonical_slot": "choice.model",
    "qualifiers": {"task": "embedding"},
    "status_at_snapshot": "active"
  },
  "gold": {
    "label": "equivalent",
    "conflict_subtype": null,
    "merge_safe": true,
    "rationale": "主体、任务、模型版本和时态相同，仅表述不同",
    "annotator": "human"
  },
  "mining_features": {
    "embedding_signature": "text-embedding-v4:2048",
    "cosine": 0.91,
    "lexical_overlap": 0.42,
    "previous_consolidation_decision": null
  },
  "split": "dev"
}
```

标签定义：

- `equivalent`：同一主体、同一原子命题，极性、数值、单位、版本、路径、时间和必要 qualifier 一致；可以安全合并。
- `compatible`：两者可同时为真，但不是同一原子事实；不能合并。
- `conflict`：在相同主体、时间和 qualifier 下不能同时为真。用 `conflict_subtype=contradiction|state_change` 补充。
- `unrelated`：不描述同一事实，也不构成冲突。
- `uncertain`：缺少主体、时间或上下文，人工无法安全决定。

只有 `equivalent` 的 `merge_safe=true`。

## 1.5 Dev/Test 切分

采用 60% dev / 40% test，按 claim 图的 connected component 分组：

- 同一个 claim 出现的所有 pair 必须进入同一 split。
- synthetic paraphrase 与其 anchor 必须同 split。
- 同一重复簇、同一 consolidation chain 必须同 split。
- 不要按 subject 整体切分，否则 `hl_mem` 占比过高，会使其中一个 split 严重失衡。

在 component 层做分层装箱，尽量保持：

- 五种标签比例。
- source slice 比例。
- cosine 区间。
- slot/predicate 分布。
- active/history 比例。

---

# 2. Recall Query-Claim 匹配集

## 2.1 目标规模

建议冻结 150 条：

| 类型 | 数量 |
|---|---:|
| 现有回归 query | 30 |
| 普通自然问法 | 18 |
| 深度改写/隐式表达 | 18 |
| 实体别名、中英混合、同义表达 | 14 |
| 专名、缩写、罕见技术词 | 12 |
| 路径、环境变量、端口 | 10 |
| 版本号、模型名、数字、单位 | 10 |
| 多 gold/聚合型 query | 8 |
| hard no-answer | 30 |
| 合计 | 150 |

最终约 110 条 answerable、40 条 no-answer。

### 重要修正

现有 `recall_regression_v1.jsonl` 中的 `identity-name`、`config-gpu` 等是逻辑 fixture ID，不是当前 `var/hl_mem.db` 中的真实 claim ID。可以复用 query，但 20 条 answerable case 的 `gold_ids` 必须重新映射到冻结的 1134 条 claim。

`recall_labels_v1.jsonl` 只有 feature 和 label，没有 query/claim ID，不能直接并入新 gold；只能保留为旧 calibration 回归材料。

## 2.2 Query 构造

### 普通问法

从不同 subject、slot、predicate 中选真实 claim，人工生成自然问题。例如：

- 当前配置是什么？
- 用户偏好哪个工具？
- 某服务监听哪个端口？

每个 claim 最多生成两条 query，避免少量事实支配数据集。

### 深度改写

要求 query 不复用 claim 的核心表面词：

- “向量模型是什么” → “记忆检索把文本转换成向量时调用哪一个模型？”
- “工作目录” → “开发时默认在哪个项目根目录执行命令？”

这类最能评估 dense/instruct。

### 专名与精确标识符

覆盖：

- 模型名、provider、库名。
- 环境变量。
- 文件名、命令、URL、端口。
- 中英混写和大小写变体。

这类用于衡量 FTS 和模型 sparse 的增量。

### 路径、版本、数值

必须同时包含：

- 精确查询：“qwen 模型版本号是什么？”
- 局部查询：“项目目录在哪？”
- 近似干扰：“旧版本号是什么？”
- 数值单位：“embedding 维度是多少？”

不要把路径中的关键目录全部删掉，否则测到的是一般语义，不是 sparse 能力。

### 多 gold 查询

选可合理返回多个事实的 query，例如：

- “当前 embedding 配置有哪些？”
- “hl_mem 使用哪些模型？”
- “网络代理配置是什么？”

需要人工列出所有直接回答该 query 的 gold claim，并可增加：

```json
"gold_groups": [
  ["claim-a", "claim-a-duplicate"],
  ["claim-b"]
]
```

同一 group 内命中任一等价 claim 即可，避免数据库重复导致 Recall@5 被不合理惩罚。

## 2.3 Gold IDs 确定方法

每条 answerable query 执行以下流程：

1. 创建 query 时选定一个真实 anchor claim。
2. 取以下候选的并集：
   - v4 dense top 30。
   - qwen 各候选配置 top 30。
   - FTS top 30。
   - 相同 subject+slot/predicate 的全部 active claim。
3. 人工逐条标：
   - `directly_relevant`
   - `partially_relevant`
   - `irrelevant`
4. `gold_ids` 只包含能够直接回答 query 的 claim。
5. 等价重复放进同一 `gold_group`；相关背景不能冒充 gold。

不要只把 anchor 作为唯一 gold，否则会错误惩罚检索到另一条同义 active claim 的模型。

## 2.4 Hard No-Answer 构造

新增 30 条 hard no-answer，分为：

- 8 条：已知 subject，但询问数据库中不存在的 sibling attribute。
- 6 条：相同专名或模型名，但询问未存储的参数。
- 5 条：路径/版本/端口近似查询，但目标实体或用途不同。
- 5 条：实体替换，例如已有 hl_mem 的配置，却询问另一服务的同类配置。
- 4 条：时间窗口无有效 claim。
- 2 条：普通域外 no-answer，作为 sanity check。

例子原则：

- 好：数据库有 qwen 模型选择，但没有记录其 temperature，询问 temperature。
- 好：数据库有工作目录，但没有缓存目录，询问缓存目录。
- 不好：询问一个与 corpus 完全无词汇重叠的随机事实；这种拒答太容易。

每条 no-answer 必须经过：

1. 所有配置的 top 50 候选并集检查。
2. FTS 搜索。
3. subject+slot/predicate SQL 检查。
4. 人工确认不存在直接支持证据。

如果发现相关 claim，转为 answerable 或删除，不能强行标 no-answer。

## 2.5 标注格式

```json
{
  "schema_version": 2,
  "id": "rq-0042",
  "query": "Hermes 当前通过哪个 memory provider 工作？",
  "query_type": "deep_paraphrase",
  "intent": "current_state",
  "as_of": null,
  "gold_ids": ["01..."],
  "gold_groups": [["01...", "01..."]],
  "no_answer": false,
  "anchor_ids": ["01..."],
  "gold_rationale": "两条 claim 是同一 provider 配置的重复表述",
  "corpus_fingerprint": "sha256:...",
  "split": "dev"
}
```

切分规则：

- 同一 anchor/gold group 的所有 query 进入同一 split。
- 现有 q01/q02/q03 这类同义 query 必须同 split。
- dev/test 都保持 answerable/no-answer 和 query_type 比例。
- 建议 dev 90 条、test 60 条，其中 no-answer 分别约 24/16 条。

---

# 3. Extraction/Entailment 集

## 3.1 当前数据的准确口径

现状不是“50 条都已有 gold”：

- `extraction_testset.jsonl`：50 条原始 event。
- `after_qwen_v0211.jsonl`：50 条模型预测，其中 11 条输出 claim、共 37 条 claim。
- `gold_dataset.jsonl`：目前只标了 20 条 event、36 条 gold claim，其中 4 条零 gold。

因此，`after_qwen_v0211.jsonl` 的 `claims_data` 不能视为 gold。

## 3.2 先完整标注现有 50 条

对每条 event：

1. 从 `extraction_testset.jsonl` 解析 `content.text`。
2. 合并已有 20 条 `gold_dataset.jsonl` 标注。
3. 复核已有 20 条是否完整、是否原子化。
4. 人工标注剩余 30 条：
   - `should_memorize`
   - 完整 gold claims
   - 每条 gold claim 的证据 span
5. 将 `after_qwen_v0211` 的 37 条候选 claim 与原文配对，逐条标 support relation。

Gold claim 应表示“值得存储且被原文支持”的原子事实；原文支持但不值得记忆的操作日志不能放入 gold。

## 3.3 Entailment 候选组成

从 50 条 event 构造三种 pair：

### Gold positives

每条 gold claim 与原文组成一对，默认候选标签为 `entailed`，但仍需复核证据 span。

### 真实模型候选

把 `after_qwen_v0211.jsonl` 的所有 37 条 `claims_data` 与对应 event 配对，人工判断：

- 是否完全被支持。
- 是否只支持一部分。
- 是否与原文矛盾。
- 是否完全无证据。
- 即使被支持，是否值得存储。

### 受控负例

对部分 gold claim 做一次单变量 mutation：

- 改主体。
- 改否定极性。
- 改数字或单位。
- 改版本号。
- 改路径。
- 改时间状态。
- 增加原文没有的因果或范围。

每次只改一类关键信息，并由人工确认标签。目标是补足 verifier 真正容易误判的 hard negatives，而不是生成明显荒谬文本。

## 3.4 是否补充新 event

建议补充 20 条，最终冻结 70 条 event。现有 50 条偏长文本、工具输出和项目开发上下文，且正负分布并非为 entailment 专门设计。

新增 20 条建议：

- 8 条：一段话包含 3–5 个独立事实。
- 4 条：否定、纠正、版本替换、状态变化。
- 4 条：路径、模型名、端口、数字和单位密集。
- 4 条：不应记忆的闲聊、工具噪声或重复确认。

优先从真实 `events` 表抽取，而不是完全合成。

多事实候选 SQL：

```sql
SELECT
    e.id,
    e.actor_type,
    e.event_type,
    e.content_json,
    COUNT(DISTINCT el.derived_id) AS linked_claim_count
FROM events e
JOIN evidence_links el
  ON el.evidence_type = 'event'
 AND el.evidence_id = e.id
 AND el.derived_type = 'claim'
WHERE length(json_extract(e.content_json, '$.text')) BETWEEN 80 AND 4000
GROUP BY e.id
HAVING COUNT(DISTINCT el.derived_id) >= 2
ORDER BY linked_claim_count DESC, e.id;
```

无 claim/noise 候选：

```sql
SELECT
    e.id,
    e.actor_type,
    e.event_type,
    e.content_json
FROM events e
LEFT JOIN evidence_links el
  ON el.evidence_type = 'event'
 AND el.evidence_id = e.id
 AND el.derived_type = 'claim'
WHERE el.evidence_id IS NULL
  AND length(json_extract(e.content_json, '$.text')) BETWEEN 20 AND 2000
ORDER BY e.actor_type, e.event_type, e.id;
```

已有 evidence link 只能辅助抽样，不能证明现有 claim 正确。

## 3.5 Event Gold 格式

```json
{
  "schema_version": 2,
  "event_id": "cbbc...",
  "category": "user_pref",
  "actor_type": "user",
  "text": "完整解析后的 content.text",
  "text_sha256": "sha256:...",
  "should_memorize": true,
  "gold_claims": [
    {
      "gold_claim_id": "cbbc...:g01",
      "subject": "hl_mem",
      "predicate": "状态",
      "value": "记忆数据库存在严重语义重复",
      "canonical_slot": null,
      "qualifiers": {},
      "scope": "temporal",
      "evidence_spans": [
        {
          "start": 143,
          "end": 169,
          "text": "同一个事实被反复提取，微小改写后各存一份"
        }
      ]
    }
  ],
  "split": "dev"
}
```

Span offset 以解析后的 `text` 为准，`text_sha256` 用来检测文本变化。

## 3.6 Entailment Pair 格式

```json
{
  "schema_version": 1,
  "pair_id": "ent-cbbc-p01",
  "event_id": "cbbc...",
  "candidate_source": "qwen_after_v0211",
  "claim": {
    "subject": "hl_mem",
    "predicate": "状态",
    "value": "记忆数据库存在严重语义重复",
    "qualifiers": {}
  },
  "gold": {
    "support_label": "entailed",
    "memory_worthy": true,
    "evidence_spans": [
      {"start": 143, "end": 169}
    ],
    "rationale": "原文明确说明存在重复存储"
  },
  "mutation": null,
  "split": "dev"
}
```

`support_label`：

- `entailed`：整个原子命题均有直接支持。
- `partially_entailed`：核心事实有支持，但候选增加了未经支持的范围、原因、主体或限定。
- `contradicted`：原文明确支持相反命题。
- `unsupported`：原文既不支持也不直接反驳。
- `uncertain`：引用层级、主体或时间上下文不足。

二分类 verifier 评测时：

- 正例：`entailed`
- 负例：`partially_entailed / contradicted / unsupported`
- `uncertain`：不参与主指标

`scope`、canonical slot 和 retention 决策不属于自然语言 entailment，应另外评测，不能因为 scope 错误就把语义支持标成 unsupported。

所有来自同一 event 的 gold、模型候选和 mutation 必须进入同一 split。

---

# 4. 实操建议

## 4.1 Codex 可以自动完成

- 只读导出 claims/events/evidence/pair 表。
- 计算 v4 cosine、token overlap、数字/路径/版本/否定标记。
- 按固定 seed 和配额生成候选池。
- 去除 unordered duplicate pair。
- 使用 LLM 生成：
  - claim 改写草稿。
  - recall query 草稿。
  - entailment mutation。
  - evidence span 候选。
- 汇总各 embedding 配置 top-K 候选，生成待标注清单。
- 检查：
  - JSON schema。
  - claim/event ID 是否存在。
  - span 是否精确匹配。
  - dev/test 泄漏。
  - 标签和 slice 分布。
  - corpus/dataset fingerprint。

## 4.2 必须人工完成

- 五级 claim-pair 标签和 merge-safe 判断。
- 判断实体别名是否真的指同一实体。
- Recall 的完整 gold 集，不仅确认 anchor。
- 每条 hard no-answer 的“确实无答案”认证。
- Event 的 `should_memorize` 和完整 gold claims。
- Entailment support label、memory-worthiness、证据 span 终审。
- `uncertain` 样本处置。
- 冻结前一致性复核。

单人标注建议在完成后隔 48 小时，盲重标 10%：

- Claim pair 一致率目标 ≥90%。
- Entailment support 一致率目标 ≥90%。
- 不一致项用于修订标注规范，然后统一回查同类样本。

## 4.3 推荐执行顺序

1. 冻结 active corpus 和 metadata fingerprint。
2. 写定标签定义与边界示例。
3. 自动生成三套候选池。
4. 先标 30 对/20 query/10 event 做 pilot。
5. 根据 pilot 修订标注规则一次。
6. 完成全量标注。
7. 做盲重标和泄漏检查。
8. 固定 dev/test。
9. 生成最终 dataset fingerprint。
10. 此后才运行 qwen 配置消融。

## 4.4 工作量预估

| 工作 | 人工时间 |
|---|---:|
| Claim-pair 约 270 对标注与复核 | 10–14 小时 |
| Recall 150 条 query、gold 和 no-answer 认证 | 10–14 小时 |
| 现有 50 + 新增 20 event 的 gold/entailment | 12–16 小时 |
| Pilot、规范修订、盲重标、冻结检查 | 4–6 小时 |
| 合计 | 36–50 小时 |

如果第一版暂不补 20 条新 event，可压缩至约 28–38 小时，但 extraction/entailment 结论只能视为方向性结果。

最终建议：冻结 **270 对 claim-pair、150 条 recall query、70 条 extraction event 及约 150–220 条 entailment pair**。这套规模对单人项目仍可完成，同时足以区分 dense、text_type、instruct 和 sparse 的真实增量。
tokens used
617,447
# HL-Mem 三套评测数据集构造方案

## 总体原则

1. 先冻结一个 corpus snapshot：1134 条 active claim 的 ID、文本、slot、predicate、qualifiers、状态及 embedding 元数据，并计算 fingerprint。
2. 所有 SQL 只生成候选，不产生 gold。
3. LLM 只用于改写、生成 query 草稿和证据 span 建议，不参与最终标注。
4. 标注界面隐藏 cosine、旧 LLM decision、来源类别，避免锚定偏差。
5. 使用固定 seed 做可复现抽样；不要用 `ORDER BY random()`。
6. dev/test 按来源实体分组切分，建议 60%/40%。test 在方案选择前不得用于调阈值。
7. 所有数据保存 claim/event 文本快照，不能只保存数据库 ID，否则数据库变化后无法复现。

---

# 1. Claim-pair 等价判定集

## 1.1 规模与组成

前一轮的 `80+50+50+80+50+30=340` 应视为候选生成上限，不是最终冻结数。

建议：

- 生成约 340–400 对候选。
- 去掉重复 pair、无效 claim 后人工标注。
- 最终冻结约 270 对：
  - 主评测集约 250 对。
  - `uncertain` 审计集不超过 20 对，不参与主指标。
- 尽量得到：
  - equivalent：约 90
  - compatible：约 55
  - conflict：约 55
  - unrelated：约 50
  - uncertain：不超过 20

如果自然等价正例不足，用真实 claim 的受控改写补足，不应通过改变标签配额“硬凑”。

另外，原建议中的“30 对随机易负例”建议改成“30 对按当前 dedup 候选分布抽取的 operational control”。从全部 claims 随机配对几乎全是无关项，价值很低。

## 1.2 基础快照 SQL

所有查询使用只读连接，并先执行：

```sql
PRAGMA query_only = ON;
```

冻结 active claims：

```sql
SELECT
    id,
    namespace_key,
    subject_entity_id,
    predicate,
    value_json,
    COALESCE(qualifiers_json, '{}') AS qualifiers_json,
    canonical_attribute,
    canonical_slot,
    conflict_key,
    index_text,
    status,
    recorded_from,
    valid_from,
    valid_to,
    embedding_model,
    embedding_dim,
    embedding_dense
FROM claims
WHERE status = 'active'
  AND index_text IS NOT NULL
ORDER BY id;
```

`embedding_dense` 只用于离线计算 baseline cosine；冻结数据集正文不必保存 BLOB，只保存计算出的分数和 embedding signature。

## 1.3 候选类别

### A. 同 subject + slot，按 cosine 分层：生成 80 对

SQL 生成 pair universe：

```sql
WITH active AS (
    SELECT
        id,
        namespace_key,
        subject_entity_id,
        predicate,
        value_json,
        qualifiers_json,
        canonical_slot,
        index_text,
        embedding_dense
    FROM claims
    WHERE status = 'active'
      AND canonical_slot IS NOT NULL
      AND embedding_dense IS NOT NULL
)
SELECT
    l.id AS left_id,
    r.id AS right_id,
    l.subject_entity_id,
    l.canonical_slot,
    l.index_text AS left_text,
    r.index_text AS right_text,
    l.value_json AS left_value,
    r.value_json AS right_value,
    l.qualifiers_json AS left_qualifiers,
    r.qualifiers_json AS right_qualifiers,
    l.embedding_dense AS left_embedding,
    r.embedding_dense AS right_embedding
FROM active l
JOIN active r
  ON l.namespace_key = r.namespace_key
 AND l.subject_entity_id = r.subject_entity_id
 AND l.canonical_slot = r.canonical_slot
 AND l.id < r.id;
```

候选生成器使用当前 v4 embedding 重算 cosine，并分层抽取：

- `[0.95, 1.00]`：20 对
- `[0.90, 0.95)`：20 对
- `[0.82, 0.90)`：20 对
- `[0.60, 0.82)`：20 对

限制：

- 单一 subject 不超过这一类别的 40%。
- 单一 slot 不超过 20%。
- 同一 claim 最多进入 3 对。
- 不足的分层从相邻区间补，不降低到完全无关区间。

当前 active 数据中同 subject+slot 有约 7846 个潜在 pair，数量足够，但分布会被 `hl_mem/config.env` 主导，因此必须设上限。

### B. FTS 重叠高、cosine 低：生成 50 对

先导出 FTS token：

```sql
SELECT
    c.id,
    c.subject_entity_id,
    c.predicate,
    c.canonical_slot,
    c.index_text,
    f.terms
FROM claims c
JOIN claims_fts_v2 f ON f.rowid = c.rowid
WHERE c.status = 'active'
  AND c.embedding_dense IS NOT NULL;
```

对每个 anchor：

1. 从 `terms` 中选择 2–4 个低 document-frequency token。
2. 查询 FTS top 10：

```sql
SELECT
    c.id,
    c.index_text,
    bm25(claims_fts_v2) AS fts_score
FROM claims_fts_v2
JOIN claims c ON c.rowid = claims_fts_v2.rowid
WHERE claims_fts_v2 MATCH :fts_query
  AND c.status = 'active'
  AND c.id <> :anchor_id
ORDER BY bm25(claims_fts_v2), c.id
LIMIT 10;
```

3. 过滤 baseline cosine `< 0.82`。
4. 按 FTS 排名、token overlap 和 cosine 区间分层抽 50 对。

这一类别重点覆盖：

- 相同技术名词但事实不同。
- 路径前缀相同。
- 版本号或配置键相同。
- 文本包含关系明显，但不一定等价。

不要直接用 `index_text` 全文作为 MATCH 表达式，标点和高频词会产生不稳定结果。

### C. 跨 subject/实体别名：生成 50 对

先导出 active claims，再使用生产代码中的 `normalize_entity_id()` 和 `DEFAULT_ENTITY_ALIASES` 分组。候选条件：

- 原始 subject 不同。
- 归一化 subject 相同；或
- 同 slot/predicate 且 baseline cosine 位于跨 subject top-K。

SQL 导出：

```sql
SELECT
    id,
    namespace_key,
    subject_entity_id,
    predicate,
    canonical_slot,
    value_json,
    qualifiers_json,
    index_text,
    embedding_dense
FROM claims
WHERE status = 'active'
  AND subject_entity_id IS NOT NULL
  AND embedding_dense IS NOT NULL
ORDER BY subject_entity_id, id;
```

优先覆盖：

- `hlmem / hl_mem / hl_mem 项目 / hl_mem 服务`
- `Hermes / Hermes 插件 / hermes-agent`
- `Codex / Codex CLI`
- `Hindsight / hindsight`
- `用户 / user / 本地小马`，但这些不能自动认定为同一实体。

另外把 17 条 `dedup_pairs` pending 全部加入候选池：

```sql
SELECT
    dp.id AS source_pair_id,
    dp.left_claim_id AS left_id,
    dp.right_claim_id AS right_id,
    dp.similarity,
    l.subject_entity_id AS left_subject,
    r.subject_entity_id AS right_subject,
    l.index_text AS left_text,
    r.index_text AS right_text
FROM dedup_pairs dp
JOIN claims l ON l.id = dp.left_claim_id
JOIN claims r ON r.id = dp.right_claim_id
WHERE dp.decision IS NULL
ORDER BY dp.id;
```

### D. Hard negatives：生成 80 对

以“相同 conflict_key/subject+slot，但关键值不同”为基础池：

```sql
WITH active AS (
    SELECT
        id,
        namespace_key,
        subject_entity_id,
        predicate,
        canonical_slot,
        conflict_key,
        value_json,
        qualifiers_json,
        index_text,
        valid_from,
        valid_to,
        occurred_start,
        occurred_end
    FROM claims
    WHERE status = 'active'
)
SELECT
    l.id AS left_id,
    r.id AS right_id,
    l.canonical_slot,
    l.index_text AS left_text,
    r.index_text AS right_text,
    l.value_json AS left_value,
    r.value_json AS right_value,
    CASE
        WHEN l.index_text GLOB '*[0-9]*'
         AND r.index_text GLOB '*[0-9]*' THEN 1 ELSE 0
    END AS has_number,
    CASE
        WHEN instr(lower(l.index_text), '版本') > 0
          OR instr(lower(r.index_text), '版本') > 0
          OR lower(l.index_text) GLOB '*v[0-9]*'
          OR lower(r.index_text) GLOB '*v[0-9]*'
          OR l.canonical_slot = 'config.version'
          OR r.canonical_slot = 'config.version'
        THEN 1 ELSE 0
    END AS has_version,
    CASE
        WHEN instr(l.index_text, '/') > 0
          OR instr(r.index_text, '/') > 0
          OR instr(l.index_text, '\') > 0
          OR instr(r.index_text, '\') > 0
        THEN 1 ELSE 0
    END AS has_path,
    CASE
        WHEN l.index_text GLOB '*不*'
          OR r.index_text GLOB '*不*'
          OR l.index_text GLOB '*未*'
          OR r.index_text GLOB '*未*'
          OR l.index_text GLOB '*禁止*'
          OR r.index_text GLOB '*禁止*'
          OR l.index_text GLOB '*拒绝*'
          OR r.index_text GLOB '*拒绝*'
          OR lower(l.index_text) GLOB '*not *'
          OR lower(r.index_text) GLOB '*not *'
        THEN 1 ELSE 0
    END AS has_negation
FROM active l
JOIN active r
  ON l.id < r.id
 AND l.namespace_key = r.namespace_key
 AND (
       (l.conflict_key IS NOT NULL AND l.conflict_key = r.conflict_key)
       OR (
            l.subject_entity_id = r.subject_entity_id
        AND l.canonical_slot IS NOT NULL
        AND l.canonical_slot = r.canonical_slot
       )
 )
WHERE l.value_json <> r.value_json;
```

按以下子类各取约 12–16 对：

- 否定或禁止关系。
- 数字/单位不同。
- 版本号不同。
- 路径不同或父子路径。
- 时间 qualifier/current-state 不同。
- 环境变量、端口、provider、model 等配置值不同。

`consolidation_pairs` 的 contradiction/state_change 应全部进入候选池，包括已经 superseded 的历史 claim：

```sql
SELECT
    cp.pair_key,
    cp.left_claim_id AS left_id,
    cp.right_claim_id AS right_id,
    cp.similarity,
    cp.decision AS hidden_previous_decision,
    l.status AS left_status,
    r.status AS right_status,
    l.index_text AS left_text,
    r.index_text AS right_text
FROM consolidation_pairs cp
JOIN claims l ON l.id = cp.left_claim_id
JOIN claims r ON r.id = cp.right_claim_id
WHERE cp.decision IN ('contradiction', 'state_change', 'manual_review')
ORDER BY cp.pair_key;
```

现有 decision 只能作为抽样 slice，标注时必须隐藏。尤其不能把 `compatible` 自动映射成 `equivalent`。

### E. LLM 辅助改写正例：生成 50 对

从真实 active claim 中分层选 anchor：

```sql
WITH ranked AS (
    SELECT
        id,
        subject_entity_id,
        predicate,
        canonical_slot,
        value_json,
        qualifiers_json,
        index_text,
        ROW_NUMBER() OVER (
            PARTITION BY COALESCE(canonical_slot, 'predicate:' || predicate)
            ORDER BY id
        ) AS rn
    FROM claims
    WHERE status = 'active'
      AND length(index_text) BETWEEN 12 AND 240
      AND value_json IS NOT NULL
)
SELECT *
FROM ranked
WHERE rn <= 8
ORDER BY COALESCE(canonical_slot, predicate), id;
```

再用固定 seed 选 50 个 anchor。改写提示必须要求：

- 不改变 subject、极性、模态和时间。
- 数字、单位、版本号、路径、端口必须原样保留。
- 不增加原 claim 没有的因果或范围。
- 只输出一个原子 claim。

LLM 输出不是自动正例，必须人工确认。synthetic ID 使用：

```text
synthetic:<anchor_claim_id>:paraphrase-1
```

### F. Operational control：30 对

从“当前生产 dedup 实际会考虑的候选分布”中固定 seed 均匀抽样，而不是从全部 claim 随机组合。

来源包括：

- 同 subject+slot。
- 无 slot 时同 subject+predicate。
- 跨 subject dedup 候选。
- 当前阈值附近候选。

该 slice 用于检查 challenge set 上的结论是否偏离生产分布。总体 accuracy 不应跨 challenge/operational 直接混算，应分别报告。

## 1.4 标注格式

建议 JSONL：

```json
{
  "schema_version": 1,
  "pair_id": "pair-0001",
  "source_slice": "same_subject_slot",
  "left": {
    "claim_id": "01...",
    "subject": "hl_mem",
    "predicate": "配置",
    "value": "embedding 模型为 text-embedding-v4",
    "canonical_slot": "choice.model",
    "qualifiers": {"task": "embedding"},
    "status_at_snapshot": "active"
  },
  "right": {
    "claim_id": "01...",
    "subject": "hl_mem",
    "predicate": "使用",
    "value": "向量模型采用 text-embedding-v4",
    "canonical_slot": "choice.model",
    "qualifiers": {"task": "embedding"},
    "status_at_snapshot": "active"
  },
  "gold": {
    "label": "equivalent",
    "conflict_subtype": null,
    "merge_safe": true,
    "rationale": "主体、任务、模型版本和时态相同，仅表述不同",
    "annotator": "human"
  },
  "mining_features": {
    "embedding_signature": "text-embedding-v4:2048",
    "cosine": 0.91,
    "lexical_overlap": 0.42,
    "previous_consolidation_decision": null
  },
  "split": "dev"
}
```

标签定义：

- `equivalent`：同一主体、同一原子命题，极性、数值、单位、版本、路径、时间和必要 qualifier 一致；可以安全合并。
- `compatible`：两者可同时为真，但不是同一原子事实；不能合并。
- `conflict`：在相同主体、时间和 qualifier 下不能同时为真。用 `conflict_subtype=contradiction|state_change` 补充。
- `unrelated`：不描述同一事实，也不构成冲突。
- `uncertain`：缺少主体、时间或上下文，人工无法安全决定。

只有 `equivalent` 的 `merge_safe=true`。

## 1.5 Dev/Test 切分

采用 60% dev / 40% test，按 claim 图的 connected component 分组：

- 同一个 claim 出现的所有 pair 必须进入同一 split。
- synthetic paraphrase 与其 anchor 必须同 split。
- 同一重复簇、同一 consolidation chain 必须同 split。
- 不要按 subject 整体切分，否则 `hl_mem` 占比过高，会使其中一个 split 严重失衡。

在 component 层做分层装箱，尽量保持：

- 五种标签比例。
- source slice 比例。
- cosine 区间。
- slot/predicate 分布。
- active/history 比例。

---

# 2. Recall Query-Claim 匹配集

## 2.1 目标规模

建议冻结 150 条：

| 类型 | 数量 |
|---|---:|
| 现有回归 query | 30 |
| 普通自然问法 | 18 |
| 深度改写/隐式表达 | 18 |
| 实体别名、中英混合、同义表达 | 14 |
| 专名、缩写、罕见技术词 | 12 |
| 路径、环境变量、端口 | 10 |
| 版本号、模型名、数字、单位 | 10 |
| 多 gold/聚合型 query | 8 |
| hard no-answer | 30 |
| 合计 | 150 |

最终约 110 条 answerable、40 条 no-answer。

### 重要修正

现有 `recall_regression_v1.jsonl` 中的 `identity-name`、`config-gpu` 等是逻辑 fixture ID，不是当前 `var/hl_mem.db` 中的真实 claim ID。可以复用 query，但 20 条 answerable case 的 `gold_ids` 必须重新映射到冻结的 1134 条 claim。

`recall_labels_v1.jsonl` 只有 feature 和 label，没有 query/claim ID，不能直接并入新 gold；只能保留为旧 calibration 回归材料。

## 2.2 Query 构造

### 普通问法

从不同 subject、slot、predicate 中选真实 claim，人工生成自然问题。例如：

- 当前配置是什么？
- 用户偏好哪个工具？
- 某服务监听哪个端口？

每个 claim 最多生成两条 query，避免少量事实支配数据集。

### 深度改写

要求 query 不复用 claim 的核心表面词：

- “向量模型是什么” → “记忆检索把文本转换成向量时调用哪一个模型？”
- “工作目录” → “开发时默认在哪个项目根目录执行命令？”

这类最能评估 dense/instruct。

### 专名与精确标识符

覆盖：

- 模型名、provider、库名。
- 环境变量。
- 文件名、命令、URL、端口。
- 中英混写和大小写变体。

这类用于衡量 FTS 和模型 sparse 的增量。

### 路径、版本、数值

必须同时包含：

- 精确查询：“qwen 模型版本号是什么？”
- 局部查询：“项目目录在哪？”
- 近似干扰：“旧版本号是什么？”
- 数值单位：“embedding 维度是多少？”

不要把路径中的关键目录全部删掉，否则测到的是一般语义，不是 sparse 能力。

### 多 gold 查询

选可合理返回多个事实的 query，例如：

- “当前 embedding 配置有哪些？”
- “hl_mem 使用哪些模型？”
- “网络代理配置是什么？”

需要人工列出所有直接回答该 query 的 gold claim，并可增加：

```json
"gold_groups": [
  ["claim-a", "claim-a-duplicate"],
  ["claim-b"]
]
```

同一 group 内命中任一等价 claim 即可，避免数据库重复导致 Recall@5 被不合理惩罚。

## 2.3 Gold IDs 确定方法

每条 answerable query 执行以下流程：

1. 创建 query 时选定一个真实 anchor claim。
2. 取以下候选的并集：
   - v4 dense top 30。
   - qwen 各候选配置 top 30。
   - FTS top 30。
   - 相同 subject+slot/predicate 的全部 active claim。
3. 人工逐条标：
   - `directly_relevant`
   - `partially_relevant`
   - `irrelevant`
4. `gold_ids` 只包含能够直接回答 query 的 claim。
5. 等价重复放进同一 `gold_group`；相关背景不能冒充 gold。

不要只把 anchor 作为唯一 gold，否则会错误惩罚检索到另一条同义 active claim 的模型。

## 2.4 Hard No-Answer 构造

新增 30 条 hard no-answer，分为：

- 8 条：已知 subject，但询问数据库中不存在的 sibling attribute。
- 6 条：相同专名或模型名，但询问未存储的参数。
- 5 条：路径/版本/端口近似查询，但目标实体或用途不同。
- 5 条：实体替换，例如已有 hl_mem 的配置，却询问另一服务的同类配置。
- 4 条：时间窗口无有效 claim。
- 2 条：普通域外 no-answer，作为 sanity check。

例子原则：

- 好：数据库有 qwen 模型选择，但没有记录其 temperature，询问 temperature。
- 好：数据库有工作目录，但没有缓存目录，询问缓存目录。
- 不好：询问一个与 corpus 完全无词汇重叠的随机事实；这种拒答太容易。

每条 no-answer 必须经过：

1. 所有配置的 top 50 候选并集检查。
2. FTS 搜索。
3. subject+slot/predicate SQL 检查。
4. 人工确认不存在直接支持证据。

如果发现相关 claim，转为 answerable 或删除，不能强行标 no-answer。

## 2.5 标注格式

```json
{
  "schema_version": 2,
  "id": "rq-0042",
  "query": "Hermes 当前通过哪个 memory provider 工作？",
  "query_type": "deep_paraphrase",
  "intent": "current_state",
  "as_of": null,
  "gold_ids": ["01..."],
  "gold_groups": [["01...", "01..."]],
  "no_answer": false,
  "anchor_ids": ["01..."],
  "gold_rationale": "两条 claim 是同一 provider 配置的重复表述",
  "corpus_fingerprint": "sha256:...",
  "split": "dev"
}
```

切分规则：

- 同一 anchor/gold group 的所有 query 进入同一 split。
- 现有 q01/q02/q03 这类同义 query 必须同 split。
- dev/test 都保持 answerable/no-answer 和 query_type 比例。
- 建议 dev 90 条、test 60 条，其中 no-answer 分别约 24/16 条。

---

# 3. Extraction/Entailment 集

## 3.1 当前数据的准确口径

现状不是“50 条都已有 gold”：

- `extraction_testset.jsonl`：50 条原始 event。
- `after_qwen_v0211.jsonl`：50 条模型预测，其中 11 条输出 claim、共 37 条 claim。
- `gold_dataset.jsonl`：目前只标了 20 条 event、36 条 gold claim，其中 4 条零 gold。

因此，`after_qwen_v0211.jsonl` 的 `claims_data` 不能视为 gold。

## 3.2 先完整标注现有 50 条

对每条 event：

1. 从 `extraction_testset.jsonl` 解析 `content.text`。
2. 合并已有 20 条 `gold_dataset.jsonl` 标注。
3. 复核已有 20 条是否完整、是否原子化。
4. 人工标注剩余 30 条：
   - `should_memorize`
   - 完整 gold claims
   - 每条 gold claim 的证据 span
5. 将 `after_qwen_v0211` 的 37 条候选 claim 与原文配对，逐条标 support relation。

Gold claim 应表示“值得存储且被原文支持”的原子事实；原文支持但不值得记忆的操作日志不能放入 gold。

## 3.3 Entailment 候选组成

从 50 条 event 构造三种 pair：

### Gold positives

每条 gold claim 与原文组成一对，默认候选标签为 `entailed`，但仍需复核证据 span。

### 真实模型候选

把 `after_qwen_v0211.jsonl` 的所有 37 条 `claims_data` 与对应 event 配对，人工判断：

- 是否完全被支持。
- 是否只支持一部分。
- 是否与原文矛盾。
- 是否完全无证据。
- 即使被支持，是否值得存储。

### 受控负例

对部分 gold claim 做一次单变量 mutation：

- 改主体。
- 改否定极性。
- 改数字或单位。
- 改版本号。
- 改路径。
- 改时间状态。
- 增加原文没有的因果或范围。

每次只改一类关键信息，并由人工确认标签。目标是补足 verifier 真正容易误判的 hard negatives，而不是生成明显荒谬文本。

## 3.4 是否补充新 event

建议补充 20 条，最终冻结 70 条 event。现有 50 条偏长文本、工具输出和项目开发上下文，且正负分布并非为 entailment 专门设计。

新增 20 条建议：

- 8 条：一段话包含 3–5 个独立事实。
- 4 条：否定、纠正、版本替换、状态变化。
- 4 条：路径、模型名、端口、数字和单位密集。
- 4 条：不应记忆的闲聊、工具噪声或重复确认。

优先从真实 `events` 表抽取，而不是完全合成。

多事实候选 SQL：

```sql
SELECT
    e.id,
    e.actor_type,
    e.event_type,
    e.content_json,
    COUNT(DISTINCT el.derived_id) AS linked_claim_count
FROM events e
JOIN evidence_links el
  ON el.evidence_type = 'event'
 AND el.evidence_id = e.id
 AND el.derived_type = 'claim'
WHERE length(json_extract(e.content_json, '$.text')) BETWEEN 80 AND 4000
GROUP BY e.id
HAVING COUNT(DISTINCT el.derived_id) >= 2
ORDER BY linked_claim_count DESC, e.id;
```

无 claim/noise 候选：

```sql
SELECT
    e.id,
    e.actor_type,
    e.event_type,
    e.content_json
FROM events e
LEFT JOIN evidence_links el
  ON el.evidence_type = 'event'
 AND el.evidence_id = e.id
 AND el.derived_type = 'claim'
WHERE el.evidence_id IS NULL
  AND length(json_extract(e.content_json, '$.text')) BETWEEN 20 AND 2000
ORDER BY e.actor_type, e.event_type, e.id;
```

已有 evidence link 只能辅助抽样，不能证明现有 claim 正确。

## 3.5 Event Gold 格式

```json
{
  "schema_version": 2,
  "event_id": "cbbc...",
  "category": "user_pref",
  "actor_type": "user",
  "text": "完整解析后的 content.text",
  "text_sha256": "sha256:...",
  "should_memorize": true,
  "gold_claims": [
    {
      "gold_claim_id": "cbbc...:g01",
      "subject": "hl_mem",
      "predicate": "状态",
      "value": "记忆数据库存在严重语义重复",
      "canonical_slot": null,
      "qualifiers": {},
      "scope": "temporal",
      "evidence_spans": [
        {
          "start": 143,
          "end": 169,
          "text": "同一个事实被反复提取，微小改写后各存一份"
        }
      ]
    }
  ],
  "split": "dev"
}
```

Span offset 以解析后的 `text` 为准，`text_sha256` 用来检测文本变化。

## 3.6 Entailment Pair 格式

```json
{
  "schema_version": 1,
  "pair_id": "ent-cbbc-p01",
  "event_id": "cbbc...",
  "candidate_source": "qwen_after_v0211",
  "claim": {
    "subject": "hl_mem",
    "predicate": "状态",
    "value": "记忆数据库存在严重语义重复",
    "qualifiers": {}
  },
  "gold": {
    "support_label": "entailed",
    "memory_worthy": true,
    "evidence_spans": [
      {"start": 143, "end": 169}
    ],
    "rationale": "原文明确说明存在重复存储"
  },
  "mutation": null,
  "split": "dev"
}
```

`support_label`：

- `entailed`：整个原子命题均有直接支持。
- `partially_entailed`：核心事实有支持，但候选增加了未经支持的范围、原因、主体或限定。
- `contradicted`：原文明确支持相反命题。
- `unsupported`：原文既不支持也不直接反驳。
- `uncertain`：引用层级、主体或时间上下文不足。

二分类 verifier 评测时：

- 正例：`entailed`
- 负例：`partially_entailed / contradicted / unsupported`
- `uncertain`：不参与主指标

`scope`、canonical slot 和 retention 决策不属于自然语言 entailment，应另外评测，不能因为 scope 错误就把语义支持标成 unsupported。

所有来自同一 event 的 gold、模型候选和 mutation 必须进入同一 split。

---

# 4. 实操建议

## 4.1 Codex 可以自动完成

- 只读导出 claims/events/evidence/pair 表。
- 计算 v4 cosine、token overlap、数字/路径/版本/否定标记。
- 按固定 seed 和配额生成候选池。
- 去除 unordered duplicate pair。
- 使用 LLM 生成：
  - claim 改写草稿。
  - recall query 草稿。
  - entailment mutation。
  - evidence span 候选。
- 汇总各 embedding 配置 top-K 候选，生成待标注清单。
- 检查：
  - JSON schema。
  - claim/event ID 是否存在。
  - span 是否精确匹配。
  - dev/test 泄漏。
  - 标签和 slice 分布。
  - corpus/dataset fingerprint。

## 4.2 必须人工完成

- 五级 claim-pair 标签和 merge-safe 判断。
- 判断实体别名是否真的指同一实体。
- Recall 的完整 gold 集，不仅确认 anchor。
- 每条 hard no-answer 的“确实无答案”认证。
- Event 的 `should_memorize` 和完整 gold claims。
- Entailment support label、memory-worthiness、证据 span 终审。
- `uncertain` 样本处置。
- 冻结前一致性复核。

单人标注建议在完成后隔 48 小时，盲重标 10%：

- Claim pair 一致率目标 ≥90%。
- Entailment support 一致率目标 ≥90%。
- 不一致项用于修订标注规范，然后统一回查同类样本。

## 4.3 推荐执行顺序

1. 冻结 active corpus 和 metadata fingerprint。
2. 写定标签定义与边界示例。
3. 自动生成三套候选池。
4. 先标 30 对/20 query/10 event 做 pilot。
5. 根据 pilot 修订标注规则一次。
6. 完成全量标注。
7. 做盲重标和泄漏检查。
8. 固定 dev/test。
9. 生成最终 dataset fingerprint。
10. 此后才运行 qwen 配置消融。

## 4.4 工作量预估

| 工作 | 人工时间 |
|---|---:|
| Claim-pair 约 270 对标注与复核 | 10–14 小时 |
| Recall 150 条 query、gold 和 no-answer 认证 | 10–14 小时 |
| 现有 50 + 新增 20 event 的 gold/entailment | 12–16 小时 |
| Pilot、规范修订、盲重标、冻结检查 | 4–6 小时 |
| 合计 | 36–50 小时 |

如果第一版暂不补 20 条新 event，可压缩至约 28–38 小时，但 extraction/entailment 结论只能视为方向性结果。

最终建议：冻结 **270 对 claim-pair、150 条 recall query、70 条 extraction event 及约 150–220 条 entailment pair**。这套规模对单人项目仍可完成，同时足以区分 dense、text_type、instruct 和 sparse 的真实增量。
