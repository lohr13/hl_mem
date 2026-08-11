# 重复/矛盾双层治理设计

## 目标

在不扩写提取 prompt、不增加同步 LLM 调用、不删除 Claim 的前提下，减少近义重复 Claim 对召回 Top-K 的挤占，并让 LongMemEval 在检索前执行与生产一致的轻量维护。

矛盾仍沿用现有 `conflict_key`、`conflict_cases` 与 `auto_resolve_conflicts`；本设计不另造冲突模型。

## 复杂度结论

采用四个小改动：

1. 入库时复用现有 semantic candidate，增加严格的 deterministic near-copy 安全门；命中后复用旧 Claim，并把新事件证据链接到旧 Claim。
2. 维护循环只审查已有 `dedup_pairs` pending 候选，限额沿用 `dedup.scan_limit`；满足安全门的 pair 标为 `equivalent`，其余保持 pending。
3. 召回阶段在已有 `fold_similar_claims` 中同时使用持久化 equivalent 边和同一安全门，组内保留最终分最高的 Claim，并逻辑合并组内证据。
4. LongMemEval 在 retrieval 前运行一次 deterministic dedup review 和 `auto_resolve_conflicts`，记录维护统计。

明确不做：新表或 migration、全库 O(n²) 扫描、LLM pair judge、物理删除或 supersede、prompt 改动、阈值调参。

## 确定性安全门

`is_safe_near_duplicate(left, right, similarity, semantic_threshold, allow_subject_mismatch)` 只在以下条件全部成立时返回真：

- 两条 Claim 都不是 disputed，namespace 相同；入库路径还要求规范化 subject 相同。维护/召回若 subject 不同，
  只接受 value 同时明确包含投影实体的 `user ↔ user's <entity>`，不接受任意跨人物折叠。
- normalized predicate、canonical slot、canonical attribute、qualifiers 一致。
- value 都是非空字符串，有效时间区间重叠。
- cosine 不低于调用方已有阈值：入库/维护使用 `dedup.threshold`，召回使用 `recall.dedup_threshold`。
- NFKC + casefold + 空白/标点归一后的字符近似度不低于固定保守值 `0.90`。
- 数字、版本、路径、月份、星期、否定词、引号内容和明显专名等 protected atoms 按原文顺序及出现次数完全一致。

该规则是充分条件，不是完整语义判定。未命中只表示“无法无 LLM 安全确认”，不得自动判 distinct。

## 数据流

### 入库源头治理

`Deduplicator.find_duplicate()` 仍先做 fact hash 和完全一致判定。对高 cosine gray candidates，再尝试 near-copy 安全门：命中返回已有 Claim，现有 `IngestService` 自动追加 evidence；未命中仍写入新 Claim 和 pending `dedup_pairs`，保持原行为。

### 维护兜底

新增 `review_pending_near_duplicates()`：优先读取从未审查的 pending pair，再按最旧 `reviewed_at` 轮转，单轮最多
`dedup.scan_limit` 条；使用已保存 similarity 和 Claim 内容做确定性审查。命中后写
`decision='equivalent'`、规则版本和 reviewed_at；不命中保持 pending 并更新 reviewed_at，避免高相似但不安全的
pair 永久堵住后续候选，仍可供现有可选后台 judge 或人工审计。

该项加入 `_run_maintenance`，默认每 600 秒触发。候选由入库增量产生，因此维护成本受 scan limit 约束，不随全库 Claim 数形成 O(n²) 扫描。

near-copy equivalent 只作逻辑分组。即使 `dedup.audit_only=false`，现有 destructive apply 也不得对该规则产生的 pair 执行 supersede。

### 召回折叠与证据

`ClaimRepository` 批量返回候选 Claim 间已确认的 equivalent pair。`fold_similar_claims()` 先合并持久化边，再对候选窗口应用相同 near-copy 安全门；每组保留排序最高者，并在内部 `_equivalent_claim_ids` 记录被折叠成员。

`RecallService._assemble_results()` 用代表 Claim 与 `_equivalent_ids` 一次批量读取 evidence，按证据标识去重后返回，并公开 `equivalent_claim_ids` 供调试。数据库中的原 Claim、状态和时间字段均不修改。

### Benchmark 对齐

普通 LongMemEval case 在 fresh ingest 或 `--skip-ingest` cache 打开后、retrieval 前调用轻量 helper，仅执行：

- `review_pending_near_duplicates`
- `auto_resolve_conflicts`

结果写入 case 的 `maintenance` 字段。不会运行 TTL、decay、事件清理、派生记忆、LLM consolidation 或定时任务 enqueue。

## 风险控制与验证

- 误折叠：由结构、lexical、cosine、protected atoms 四重门和 disputed 禁止规则控制；数字、日期、实体或限定条件变化必须保留。
- 旧行为退化：未命中 near-copy 时完全沿用 pending pair 流程；等价折叠只影响返回集合，不改 Claim 状态。
- 成本：入库复用已有候选与 embedding；维护最多审查 200 个已有 pair；召回仍受原 `recall.dedup_candidate_limit=100` 限制。
- 测试：覆盖安全命中、数字/qualifier/subject 保护、维护非破坏性标记、召回组内最高分、证据合并、benchmark 维护路由。
- 评测：只重放 1–2 个失败 case；不跑全量 benchmark，不修改数据集和评测阈值。
