# 提取架构最终版设计

## 结论

hl_mem 保留事件逐条实时落盘，但把 LLM 调度改为同 session 有界微批：`sync_turn` 原子写入 user/assistant 两个 Event，Worker 在租约阶段按 tenant/session 合租最多 4 个 `extract_event` job，首事件最多等待 2 秒。Claim 由 `source_event_indices` 指回窗口内事件，持久化时仍使用既有 `evidence_links` 连接所有来源 Event。

复杂度审查结论为“通过，采用简化方案”。不新增 batch 表、event 状态列或第二套状态机。

## 方案比较

1. 新增 extraction batch 表和状态机：恢复语义完整，但与 jobs 的 pending/running/retry/dead 重复，需 migration 和双状态一致性，不采用。
2. 每个 session 维护一个可变 JSON job：行数少，但并发追加、幂等、失败拆分难以保证，不采用。
3. 保留一事件一 job，在 lease 时形成临时窗口：复用现有事务、租约、重试、导入和审计，改动局部，采用。

## 窗口语义

- 分区键：`tenant_id + session_id`；缺少 session 的 Event 不合批。
- 只合并普通 `message` Event；显式记忆及其他类型保持单事件快车道。
- `extraction.batch_max_events = 4`。
- `extraction.batch_max_wait_seconds = 2.0`，从窗口最早 Event 的 `recorded_at` 计算。
- 达到 4 个 Event、超过 2 秒，或调用方明确 force flush 时可租用；新 Event 不重置最早等待时间。
- 不配置独立 idle：`sync_turn` 使用原子 batch API 保证配对；普通单事件仅需一个最大等待边界。
- 同一窗口的 jobs 使用同一个 lease token；成功或失败时一起进入相同终态。

默认 worker poll 为 2 秒，因此普通单 Event 的结构化记忆通常在 2～4 秒内可见；`sync_turn` 配对不会分裂。原始 Event 仍立即可见，显式记忆仍立即进入确定性 bypass。

## 生产数据流

1. `POST /v1/events` 保持不变；新增兼容性 API `POST /v1/events/batch`。
2. `HLMemProvider.sync_turn` 生成共同 `turn_id`，一次请求原子写入 user/assistant 两个 Event。
3. `IngestService.ingest_events` 在单个 `BEGIN IMMEDIATE` 内完成 Event 与逐 Event extraction job 写入。
4. Worker 原子租用一个普通 job 或一个 session window。
5. EventFilter 与 PreFilter 仍逐 Event 执行；被拒绝的 Event 不进入 LLM 源文本，但其 job 正常完成。
6. 允许提取的 Event 形成 `messages` 结构，每项含全局 `event_index`、只读 `speaker`、`turn`、`occurred_at` 和 content；现有 conversation chunker 按 Event 边界切分，超长 Event 继续自动拆分。
7. LLM compact claim 输出 `source_event_indices`。索引越界、空索引，或批输入中缺失索引的 claim 被拒绝；单 Event 兼容旧输出，缺失时回退到索引 0。
8. 每条 claim 以首个来源 Event 计算主时间与 actor 语义，并在同一 claim 写事务中链接所有来源 Event；speaker/turn/time 从 Event 元数据读取，不由模型生成。

## Benchmark 对齐

LongMemEval runner 只构造并摄入生产 Event，不再直接调用 `extractor.extract` 或 `IngestService.store_extracted`。它用外部连接创建 Worker，以 force flush 驱动同一队列、窗口、过滤、chunking、提取、准入和证据写入路径。Force flush 仅跳过在线等待时间，不改变 batch key、上限或内容格式。

评测数据集、检索阈值和 QA 判定不变。缓存 manifest 的 extraction protocol identity 更新，使旧逐 turn cache 不会被误复用。

## 错误与兼容

- 原有 `/v1/events`、`extract_event` job payload 和 `JobRepository.lease_job` 默认行为保留。
- 多 job lease 只由 Worker 显式开启；旧 job、导入归档和失败重试继续可处理。
- LLM 调用期间不持有 SQLite 写事务；claim/evidence 仍使用已有短事务。
- 同批失败时所有租约 job 一起 retry/dead，避免部分窗口被静默遗漏。Claim/evidence 自身已有幂等与去重，重试安全边界不变。
- 新增 API 与 metadata 字段是向后兼容扩展；数据库已有 `metadata_json`，无需 migration。

## 取舍

不实现供应商专属离线 Batch API。它需要上传、轮询、取消和独立恢复状态机，且不影响窗口、speaker 与证据的核心正确性。离线 runner 先复用相同微批请求；将来如接 Batch，只能替换 LLM transport。

不增加 source token 阈值、自适应窗口、`consistency=latest` 或 shutdown flush。现有 12k 字符结构化 chunking 和截断二分已经限制请求大小；继续增加调度参数会使行为难以预测。

## 测试与验收

- 仓储：同 session 合租、跨 tenant/session 隔离、4 Event 边界、2 秒边界、force flush、批量完成/失败。
- Provider/API：`sync_turn` 单请求配对、共同 turn metadata、旧单 Event API 不变、batch 原子回滚。
- Extractor：structured conversation 保留 speaker/turn；source indices 缺失/越界/去重合并；每条 evidence quote 只能引用声明的 Event。
- Ingest/Worker：多 Event evidence links、过滤边界、显式记忆快车道。
- Benchmark：runner 通过 Worker 产生 claims 和 token 统计，不再调用私有提取分支。
- 本地按任务约束只运行格式、lint、类型/编译等非 pytest 静态检查；完整测试由 GitHub Actions 执行。

## 版本、工作量与回退

该变更新增 API、配置与 prompt schema，版本升为 v0.25.0。无 migration，仍为 38 个 SQL migrations。

预计修改 12～16 个生产、测试和文档文件，核心代码约 300～450 行。主要风险是多 job lease 的并发终态和来源索引误绑；对应回退点是把 `batch_max_events` 设为 1（恢复逐 Event 调度），或整体回退该提交。旧 API 和数据库不需要降级处理。
