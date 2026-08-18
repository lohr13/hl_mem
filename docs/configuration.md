# HL-Mem 配置参考

HL-Mem 0.29.0 使用单个 TOML 文件保存非敏感配置，并用 `.env` 或同名进程环境变量保存四个密钥。
`Settings` 是唯一 schema；下表由 `Settings` 字段 metadata 自动生成。未写入 TOML 的字段使用代码默认值。
模型型号不在活文档中固化：LLM、Embedding、Reranker 和图片描述器的 API 密钥通过 `.env` 配置，provider/model 等非敏感选项通过 TOML 配置。

v0.29.0 的受限 assertion 门控没有配置键；存量 `unknown` 只可观测，不改变 supersede、召回或注入行为。

## 加载规则

- 默认读取当前工作目录的 `hl_mem.toml`；文件缺失、TOML 语法错误、未知表、未知键或类型错误都会阻止启动。
- `.env` 也是相对当前工作目录读取，但可以缺失。进程环境中的同名密钥覆盖 `.env`。
- 除四个密钥外，环境变量不参与配置；所有 `HL_MEM_*` 变量均被忽略。
- TOML 使用原生类型；仅允许数组转换为 tuple、字符串转换为枚举。密钥不得写入 TOML。
- 可从 [`config.example.toml`](../config.example.toml) 复制常用配置；该示例显式启用真实能力，推荐值不等于代码默认值。

```bash
cp config.example.toml hl_mem.toml
cp .env.example .env
uv run hl-mem doctor
uv run python start_server.py
```

## 启动入口

- Windows 使用 `start_production.bat`，Git Bash/POSIX 使用 `./start_hl_mem.sh`。两个脚本都从脚本自身位置定位仓库根目录，并调用 `start_server.py`，因此可以从任意当前目录启动。
- 脚本使用仓库内的虚拟环境；Shell 入口兼容 `.venv/bin/python` 和 `.venv/Scripts/python.exe`。
- 启动脚本不保存第二份运行配置，也不选择 provider/model，且不再设置旧版 `HL_MEM_*` 覆盖。除四个密钥外，所有有效配置都只来自仓库根目录的 `hl_mem.toml`；loader 会忽略继承到进程中的 `HL_MEM_*`。
- 直接执行 `uv run python start_server.py` 时，`hl_mem.toml` 和 `.env` 仍相对进程当前目录解析。

## 部署边界

HL-Mem 面向受信任环境中的本地单租户部署。API 的 `namespace` 只是用于召回、Episode、Policy 和维护任务的
相关性/profile 软标签，不是认证、授权、加密或侧信道安全边界；`tenant_id` 仅作为已弃用的兼容别名。备份与
恢复始终覆盖整个 SQLite 数据库，不提供按 namespace 导出、RBAC、按租户密钥、计费或 SaaS 多租户隔离。

## 备份与恢复

```bash
hl-mem backup var/backup.db --db var/hl_mem.db
hl-mem restore var/backup.db --manifest var/backup.db.manifest.json \
  --db var/hl_mem.db --confirm-overwrite
```

`backup` 会在首次运行时创建并绑定 `<source>.tombstones.db`，输出的 manifest v2 记录 ledger ID 与 schema
version；JSON 结果同时包含 `ledger_id` 和 `ledger_schema_version`。ledger 不嵌入主库 backup，必须与 backup
分别受保护保存。恢复到目标库前，应把权威 ledger 放在 `<target>.tombstones.db`；缺失、ID/版本错配或无 ledger
identity 的 v1 manifest 均 fail-closed，不能静默恢复。

`restore` 会先校验 manifest、大小、哈希以及 backup/manifest/ledger 三方身份，再在目标同目录的临时数据库
重放全部 tombstone 并执行 `PRAGMA integrity_check`，成功后才原子替换目标；任何失败都保留原目标。结果额外
报告 `tombstones_replayed`、`claims_removed` 和 `events_removed`。目标已存在时必须提供
`--confirm-overwrite`，且 source、backup、manifest、target 不得解析为同一路径。校验与恢复会拒绝 backup、
target 或 ledger 旁残留的 `-wal`、`-shm`、`-journal` sidecar，防止未纳入验证的页面影响结果。执行 restore
前必须停止 API、Worker 及其他数据库/ledger 使用者，成功后再重启服务。

## 升级到 v0.28.9

先停止 API、Worker 和全部写入者，并保留主库的离线副本。v0.28.9 首次打开主库时会继续执行 migration
045/046：前者建立单案多候选、generation/revision、dirty queue 与版本化裁决约束；后者增加运维清理索引，并将
历史上低于摄入 floor 的 pending dedup pair 一次性标记为已审查。migration 不执行存量冲突裁决。

迁移成功后立即运行一次 `hlmem backup`。若该库此前没有 tombstone sidecar，backup 会创建并绑定
`<database>.tombstones.db`，再生成 manifest v2；从此应把主库 backup、manifest 和 ledger 作为同一恢复集合保存。
旧 manifest v1 可留作升级前取证副本，但 v0.28.9 restore 会拒绝它，因为它无法证明删除历史。确认新的 v2
backup 可校验后，再恢复 API/Worker 服务。

## JSONL Event 归档

```bash
hl-mem export var/events.jsonl --db var/hl_mem.db
hl-mem import var/events.jsonl --db var/restored.db
```

默认 import 会在同一批次事务中为每个新 Event 创建 `extract_event` job，幂等键为
`extract:<event_id>`，使 Worker 能从归档重建 Claims。重复导入会跳过已有 Event/job，不增加记录。JSON
报告包含 `processed`、`events_created`、`events_skipped`、`jobs_queued`、`failed_batch` 和
`claims_not_rebuilt`；非法记录会回滚当前批次并报告 batch/line。

若 Event 已由旧版 importer 或 `--skip-extraction-jobs` 导入，但稳定 extraction job 缺失，随后执行普通 import
会验证 Event payload 并补建 job；同 ID 不同 payload 会明确失败，不会被当作重复记录静默跳过。
Event 的 `metadata_json` 属于归档与幂等冲突判定的一部分；turn locator 等 metadata 会在导入/导出后原样保留。

`--skip-extraction-jobs` 只用于不希望重建 Claims 的取证恢复。该模式仅导入 Events、不会排队提取，并明确
输出 `claims_not_rebuilt=true`。

## 密钥

| 环境变量 | Settings 字段 | 需要提供的条件 |
|---|---|---|
| `EMBEDDING_API_KEY` | `embedding_api_key` | embedding.mode = real |
| `IMAGE_API_KEY` | `image_describer_api_key` | image_describer.mode = on |
| `LLM_API_KEY` | `llm_api_key` | extraction 非 fake、query expansion 非 off 或 relation discovery 非 off |
| `RERANKER_API_KEY` | `reranker_api_key` | reranker.mode = on 或 real |

空值和常见占位值（如 `xxx`、`changeme`、`<key>`）不能用于已启用的真实组件；图片密钥不回退到 LLM 密钥。

## TOML 键

“允许值”来自字段注解及 `Settings.validate()`；标为“任意”的字段当前只做 TOML 原生类型校验。

### `[alert]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `alert.dedupe_seconds` | 数值 | `300.0` | 任意数值 | `alert_dedupe_seconds` |
| `alert.email_from` | 字符串 | 未设置 | 字符串；可省略 | `alert_email_from` |
| `alert.email_to` | 字符串 | 未设置 | 字符串；可省略 | `alert_email_to` |
| `alert.smtp_host` | 字符串 | 未设置 | 字符串；可省略 | `smtp_host` |
| `alert.smtp_port` | 整数 | `25` | 任意整数 | `smtp_port` |
| `alert.webhook_url` | 字符串 | 未设置 | 字符串；可省略 | `alert_webhook_url` |

### `[database]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `database.busy_timeout_seconds` | 整数 | `30` | >= 1 | `database_busy_timeout_seconds` |
| `database.path` | 字符串 | `"var/hl_mem.db"` | 任意字符串 | `database_path` |
| `database.pool_size` | 整数 | `8` | >= 1 | `database_pool_size` |

### `[decay]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `decay.halflife_archive_grace_days` | 整数 | `7` | >= 1 | `decay_halflife_archive_grace_days` |
| `decay.halflife_archive_threshold` | 数值 | `0.05` | 0.0 - 1.0（不含端点） | `decay_halflife_archive_threshold` |
| `decay.identity_half_life_days` | 整数 | `365` | >= 1 | `decay_identity_half_life_days` |
| `decay.model` | 字符串 | `"activation_halflife"` | `legacy_linear`、`activation_halflife`、`confidence_halflife` | `decay_model` |
| `decay.permanent_half_life_days` | 整数 | `90` | >= 1 | `decay_permanent_half_life_days` |
| `decay.temporal_half_life_days` | 整数 | `45` | >= 1 | `decay_temporal_half_life_days` |

### `[dedup]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `dedup.audit_only` | 布尔值 | `true` | `true`、`false` | `dedup_audit_only` |
| `dedup.auto_merge_min_confidence` | 数值 | `0.98` | dedup.threshold - 1.0 | `dedup_auto_merge_min_confidence` |
| `dedup.cron` | 字符串 | `"03:00"` | HH:MM（00:00 - 23:59） | `dedup_cron` |
| `dedup.enabled` | 布尔值 | `true` | `true`、`false` | `dedup_enabled` |
| `dedup.max_pending_pairs` | 整数 | `10000` | >= 1 | `dedup_max_pending_pairs` |
| `dedup.scan_limit` | 整数 | `200` | >= 1 | `dedup_scan_limit` |
| `dedup.threshold` | 数值 | `0.92` | 0.0 - 1.0 | `dedup_threshold` |

### `[embedding]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `embedding.api_mode` | 字符串 | `"compatible"` | `compatible`、`native` | `embedding_api_mode` |
| `embedding.base_url` | 字符串 | `"https://dashscope.aliyuncs.com/compatible-mode/v1"` | 任意字符串 | `embedding_base_url` |
| `embedding.connect_timeout` | 数值 | `5.0` | 任意数值 | `embedding_connect_timeout` |
| `embedding.dim` | 整数 | `2048` | 任意整数 | `embedding_dim` |
| `embedding.max_attempts` | 整数 | `3` | 任意整数 | `embedding_max_attempts` |
| `embedding.mode` | 字符串 | `"fake"` | `fake`、`real` | `embedder_mode` |
| `embedding.model` | 字符串 | `"text-embedding-v4"` | 任意字符串 | `embedding_model` |
| `embedding.read_timeout` | 数值 | `30.0` | 任意数值 | `embedding_read_timeout` |
| `embedding.text_type` | 字符串 | 未设置 | `document`、`query`；可省略 | `embedding_text_type` |

`embedding.text_type` 仅在 native API 模式下发送；默认不设置，compatible 模式不使用该参数。显式启用、修改或取消
该角色后，应以同一最终配置重建存量 Claim 向量，避免查询与文档向量混用不同表示约定。sparse/instruct 变体仅用于
显式 benchmark 配置，生产默认关闭。

### `[entity]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `entity.aliases_path` | 字符串 | 未设置 | 非空字符串；可省略 | `entity_aliases_path` |

### `[extraction]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `extraction.batch_max_events` | 整数 | `5` | 1 - 32 | `extraction_batch_max_events` |
| `extraction.batch_max_wait_seconds` | 数值 | `120.0` | >= 0 | `extraction_batch_max_wait_seconds` |
| `extraction.chunk_overlap_turns` | 整数 | `2` | >= 0 | `extraction_chunk_overlap_turns` |
| `extraction.chunk_target_chars` | 整数 | `12000` | >= 1 | `extraction_chunk_target_chars` |
| `extraction.max_split_depth` | 整数 | `3` | >= 0 | `extraction_max_split_depth` |
| `extraction.mode` | 字符串 | `"fake"` | `fake`、`real`、`llm` | `extractor_mode` |
| `extraction.pre_filter` | 布尔值 | `false` | `true`、`false` | `extract_pre_filter` |
| `extraction.verification_mode` | 字符串 | `"off"` | `off`、`audit`、`enforce` | `verification_mode` |

Worker 只合并同一 namespace/session 的 `message` Event；窗口满 `batch_max_events` 时立即提取，否则最多等待
`batch_max_wait_seconds`。显式记忆、无 session 事件和非 message 事件不等待。Hermes 的 `sync_turn` 会原子写入
user/assistant 一对 Event，通常在该上限内与后续相邻 turn 合并；Claim 仍分别链接实际来源 Event。
默认值偏向降低提取调用成本：增大批量上限或等待时间有利于合并更多相邻 Event、摊薄 LLM 调用成本，
但会增加低流量 session 的提取延迟；需要低延迟时可调小这两个值。

### `[hermes]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `hermes.circuit_failure_threshold` | 整数 | `5` | >= 1 | `hermes_circuit_failure_threshold` |
| `hermes.circuit_open_seconds` | 数值 | `60.0` | > 0 | `hermes_circuit_open_seconds` |
| `hermes.enabled` | 布尔值 | `false` | `true`、`false` | `hermes_enabled` |
| `hermes.home` | 字符串 | 未设置 | 非空字符串；可省略 | `hermes_home` |
| `hermes.on_demand_recall_timeout_seconds` | 数值 | `8.0` | > 0 | `hermes_on_demand_recall_timeout_seconds` |
| `hermes.prefetch_cache_ttl_seconds` | 数值 | `300.0` | > 0 | `hermes_prefetch_cache_ttl_seconds` |
| `hermes.timeout` | 整数 | `30` | >= 1 | `hermes_timeout` |
| `hermes.url` | 字符串 | `"http://127.0.0.1:8200"` | 任意字符串 | `hermes_url` |

### `[image_describer]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `image_describer.allow_file_uris` | 布尔值 | `false` | `true`、`false` | `image_allow_file_uris` |
| `image_describer.base_url` | 字符串 | `"https://coding.dashscope.aliyuncs.com/v1"` | 任意字符串 | `image_describer_base_url` |
| `image_describer.file_allow_roots` | 字符串 数组 | `[]` | 任意字符串 数组 | `image_file_allow_roots` |
| `image_describer.max_bytes` | 整数 | `10485760` | >= 1 | `image_max_bytes` |
| `image_describer.max_parts` | 整数 | `4` | >= 1 | `image_max_parts` |
| `image_describer.mode` | 字符串 | `"off"` | `off`、`on` | `image_describer_mode` |
| `image_describer.model` | 字符串 | `"qwen3.7-plus"` | 任意字符串 | `image_describer_model` |
| `image_describer.provider` | 字符串 | `"dashscope"` | `dashscope` | `image_describer_provider` |
| `image_describer.timeout_seconds` | 数值 | `20.0` | > 0 | `image_describer_timeout_seconds` |

### `[index]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `index.backfill_batch_size` | 整数 | `100` | >= 1 | `index_backfill_batch_size` |
| `index.backfill_max_attempts` | 整数 | `3` | >= 1 | `index_backfill_max_attempts` |
| `index.text_mode` | 字符串 | `"natural"` | `legacy`、`value_only`、`natural`、`answerable` | `index_text_mode` |
| `index.text_version` | 字符串 | `"v2"` | 非空字符串 | `index_text_version` |

`natural` 生成 `subject：value`，不把内部 predicate、slot 或 topic tags 混入 FTS/embedding 文本。已有数据库不会在启动时自动重算 embedding；先运行 `hlmem backfill-index-text --mode natural --dry-run` 查看影响，再显式运行不带 `--dry-run` 的同一命令完成可续跑回填。

### `[llm]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `llm.base_url` | 字符串 | `"https://coding.dashscope.aliyuncs.com/v1"` | 任意字符串 | `llm_base_url` |
| `llm.enable_thinking` | 布尔值 | `false` | `true`、`false` | `enable_llm_thinking` |
| `llm.max_attempts` | 整数 | `3` | >= 1 | `llm_max_attempts` |
| `llm.model` | 字符串 | `"qwen3.7-plus"` | 非空字符串 | `llm_model` |
| `llm.provider` | 字符串 | `"dashscope"` | `dashscope`、`zhipu`、`openai_compatible` | `llm_provider` |
| `llm.schema_retries` | 整数 | `2` | >= 0 | `llm_schema_retries` |
| `llm.structured_mode` | 字符串 | `"json_object"` | `auto`、`json_object`、`json_schema` | `llm_structured_mode` |
| `llm.timeout` | 数值 | `90.0` | > 0 | `llm_timeout` |

### `[recall]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `recall.candidate_floor` | 整数 | `50` | >= 1 | `recall_candidate_floor` |
| `recall.dedup_candidate_limit` | 整数 | `100` | >= 1 | `recall_dedup_candidate_limit` |
| `recall.dedup_threshold` | 数值 | `0.95` | 0.0 - 1.0；0 关闭折叠 | `recall_dedup_threshold` |
| `recall.default_limit` | 整数 | `5` | 1 - 100 | `recall_default_limit` |
| `recall.dense_enabled` | 布尔值 | `true` | `true`、`false` | `recall_dense_enabled` |
| `recall.echo_pending_max_seconds` | 整数 | `7200` | >= 60 | `echo_pending_max_seconds` |
| `recall.echo_pending_review_enabled` | 布尔值 | `false` | `true`、`false` | `echo_pending_review_enabled` |
| `recall.echo_pending_similarity_threshold` | 数值 | `0.95` | 0.0 - 1.0 | `echo_pending_similarity_threshold` |
| `recall.echo_session_window_seconds` | 整数 | `1800` | 60 - 14400 | `echo_session_window_seconds` |
| `recall.echo_suppression_mode` | 字符串 | `"off"` | `off`、`observe`、`enforce` | `echo_suppression_mode` |
| `recall.freshness_annotation_mode` | 字符串 | `"off"` | `off`、`observe`、`render` | `freshness_annotation_mode` |
| `recall.expansion_circuit_failure_threshold` | 整数 | `5` | >= 1 | `expansion_circuit_failure_threshold` |
| `recall.expansion_circuit_open_seconds` | 数值 | `60.0` | > 0 | `expansion_circuit_open_seconds` |
| `recall.feedback_min_samples` | 整数 | `3` | >= 1 | `feedback_min_samples` |
| `recall.fts_language` | 字符串 | `"auto"` | `auto`、`zh`、`en` | `fts_language` |
| `recall.packed_context_token_budget` | 整数 | `2000` | >= 1 | `packed_context_token_budget` |
| `recall.preference_recency_boost` | 数值 | `0.12` | 0.0 - 1.0 | `preference_recency_boost` |
| `recall.procedure_candidate_limit` | 整数 | `30` | > 0 | `procedure_candidate_limit` |
| `recall.procedure_llm_threshold` | 数值 | `0.8` | 0.0 - 1.0 | `procedure_llm_threshold` |
| `recall.procedure_mode` | 字符串 | `"keyword"` | `off`、`keyword`、`auto` | `procedure_recall_mode` |
| `recall.procedure_outcome_half_life_days` | 整数 | `30` | > 0 | `procedure_outcome_half_life_days` |
| `recall.procedure_recent_outcome_window` | 整数 | `20` | > 0 | `procedure_recent_outcome_window` |
| `recall.procedure_router_timeout_seconds` | 数值 | `1.5` | > 0 | `procedure_router_timeout_seconds` |
| `recall.query_context_max_events` | 整数 | `5` | > 0 | `query_context_max_events` |
| `recall.query_context_mode` | 字符串 | `"off"` | `off`、`coreference` | `query_context_mode` |
| `recall.query_context_token_budget` | 整数 | `256` | > 0 | `query_context_token_budget` |
| `recall.query_expansion_candidate_floor` | 整数 | `8` | > 0 | `query_expansion_candidate_floor` |
| `recall.query_expansion_max` | 整数 | `2` | 0 - 2 | `query_expansion_max` |
| `recall.query_expansion_max_concurrency` | 整数 | `4` | > 0 | `query_expansion_max_concurrency` |
| `recall.query_expansion_mode` | 字符串 | `"auto"` | `off`、`auto`、`always` | `query_expansion_mode` |
| `recall.query_expansion_model` | 字符串 | 未设置 | 字符串；可省略 | `query_expansion_model` |
| `recall.query_expansion_timeout_seconds` | 数值 | `5.0` | > 0 | `query_expansion_timeout_seconds` |
| `recall.query_expansion_token_ceiling` | 整数 | `256` | > 0 | `query_expansion_token_ceiling` |
| `recall.query_expansion_total_timeout_seconds` | 数值 | `6.0` | > 0 | `query_expansion_total_timeout_seconds` |
| `recall.relevance_dense_floor` | 数值 | `0.3` | 0.0 - 1.0 | `relevance_dense_floor` |
| `recall.relevance_gate_mode` | 字符串 | `"off"` | `off`、`observe`、`enforce` | `relevance_gate_mode` |
| `recall.relevance_intents` | 字符串 数组 | `["current_state"]` | 非空数组；元素为 current_state、preference、historical、tool、procedure | `relevance_intents` |
| `recall.relevance_keep_top1` | 布尔值 | `true` | `true`、`false` | `relevance_keep_top1` |
| `recall.relevance_relative_drop` | 数值 | `0.15` | 0.0 - 1.0 | `relevance_relative_drop` |
| `recall.relevance_reranker_floor` | 数值 | `0.15` | 0.0 - 1.0 | `relevance_reranker_floor` |
| `recall.resurrection_candidate_limit` | 整数 | `3` | >= 1 | `resurrection_candidate_limit` |
| `recall.resurrection_min_term_coverage` | 数值 | `0.8` | 0.0 - 1.0（不含 0） | `resurrection_min_term_coverage` |
| `recall.resurrection_mode` | 字符串 | `"auto"` | `off`、`auto` | `resurrection_mode` |
| `recall.side_effect_backoff_seconds` | 数值 | `0.05` | >= 0 | `recall_side_effect_backoff_seconds` |
| `recall.side_effect_max_attempts` | 整数 | `3` | >= 1 | `recall_side_effect_max_attempts` |
| `recall.tag_boost_enabled` | 布尔值 | `true` | `true`、`false` | `tag_boost_enabled` |
| `recall.tag_boost_weight` | 数值 | `0.05` | 0.0 - 1.0 | `tag_boost_weight` |
| `recall.tag_candidate_limit` | 整数 | `20` | >= 1 | `tag_candidate_limit` |
| `recall.tag_channel_enabled` | 布尔值 | `false` | `true`、`false` | `tag_channel_enabled` |
| `recall.tag_channel_weight` | 数值 | `0.15` | 0.0 - 1.0 | `tag_channel_weight` |
| `recall.vector_backend` | 字符串 | `"sqlite_scan"` | `sqlite_scan`、`sqlite_vec` | `vector_backend` |
| `recall.vector_batch_size` | 整数 | `512` | >= 1 | `vector_batch_size` |
| `recall.vector_scan_limit` | 整数 | `200` | >= 1 | `recall_vector_scan_limit` |

### `[relation]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `relation.auto_apply_confidence` | 数值 | `0.9` | 0.0 - 1.0 | `relation_auto_apply_confidence` |
| `relation.conflict_confidence` | 数值 | `0.8` | 0.0 - 1.0 | `relation_conflict_confidence` |
| `relation.discovery_max_proposals` | 整数 | `10` | >= 1 | `relation_discovery_max_proposals` |
| `relation.discovery_mode` | 字符串 | `"off"` | `off`、`audit`、`auto` | `relation_discovery_mode` |
| `relation.discovery_pool_limit` | 整数 | `40` | >= 1 | `relation_discovery_pool_limit` |
| `relation.expansion_max_depth` | 整数 | `1` | >= 1 | `relation_expansion_max_depth` |
| `relation.expansion_mode` | 字符串 | `"off"` | `off`、`on` | `relation_expansion_mode` |

### `[reranker]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `reranker.base_url` | 字符串 | `"https://dashscope.aliyuncs.com"` | 任意字符串 | `reranker_base_url` |
| `reranker.mode` | 字符串 | `"off"` | `off`、`fake`、`on`、`real` | `reranker_mode` |
| `reranker.model` | 字符串 | `"qwen3-rerank"` | 任意字符串 | `reranker_model` |
| `reranker.provider` | 字符串 | `"dashscope"` | `dashscope` | `reranker_provider` |

Reranker 的具体型号通过 `reranker.model` 配置；API 密钥由 `RERANKER_API_KEY` 提供。升级时以当前 `Settings` 或部署
TOML 为准，活文档不固定具体型号。

### `[retention]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `retention.access_bonus_cap_days` | 整数 | `30` | >= 0 | `access_bonus_cap_days` |
| `retention.access_bonus_days` | 整数 | `1` | >= 0 | `access_bonus_days` |
| `retention.access_bonus_every` | 整数 | `5` | >= 1 | `access_bonus_every` |
| `retention.archive_permanent_days` | 整数 | `180` | >= 1 | `archive_permanent_days` |
| `retention.archive_temporal_days` | 整数 | `30` | >= 1 | `archive_temporal_days` |
| `retention.decay_min_confidence` | 数值 | `0.05` | 0.0 - 1.0 | `decay_min_confidence` |
| `retention.decay_permanent_days` | 整数 | `90` | >= 1；不得大于 archive_permanent_days | `decay_permanent_days` |
| `retention.decay_rollout_grace_days` | 整数 | `7` | >= 1 | `decay_rollout_grace_days` |
| `retention.decay_temporal_days` | 整数 | `7` | >= 1；不得大于 archive_temporal_days | `decay_temporal_days` |
| `retention.dedup_pair_days` | 整数 | `90` | >= 1 | `dedup_pair_days` |
| `retention.event_days` | 整数 | `30` | 任意整数 | `retention_days` |
| `retention.feedback_bonus_cap_days` | 整数 | `180` | >= 0 | `feedback_bonus_cap_days` |
| `retention.feedback_bonus_days` | 整数 | `14` | >= 0 | `feedback_bonus_days` |
| `retention.feedback_bonus_every` | 整数 | `3` | > 0 | `feedback_bonus_every` |
| `retention.feedback_lifecycle_mode` | 字符串 | `"observe"` | `off`、`observe`、`on` | `feedback_lifecycle_mode` |
| `retention.feedback_uninjected_days` | 整数 | `7` | >= 1 | `feedback_uninjected_days` |
| `retention.feedback_unlabeled_days` | 整数 | `90` | >= 1 | `feedback_unlabeled_days` |
| `retention.importance_high_threshold` | 数值 | `0.7` | 见字段联动约束 | `importance_high_threshold` |
| `retention.importance_low_threshold` | 数值 | `0.4` | 见字段联动约束 | `importance_low_threshold` |
| `retention.importance_write_floor` | 数值 | `0.2` | 见字段联动约束 | `importance_write_floor` |
| `retention.job_dead_days` | 整数 | `90` | >= 1 | `job_dead_days` |
| `retention.job_succeeded_days` | 整数 | `30` | >= 1 | `job_succeeded_days` |
| `retention.llm_span_days` | 整数 | `30` | >= 1 | `llm_span_days` |
| `retention.operational_batch_size` | 整数 | `2000` | >= 1 | `operational_batch_size` |
| `retention.operational_cleanup_enabled` | 布尔值 | `true` | `true`、`false` | `operational_cleanup_enabled` |
| `retention.slot_short_ttl_seconds` | 整数 | `86400` | >= 1 | `slot_short_ttl_seconds` |
| `retention.temporal_cleanup_age_days` | 整数 | `30` | >= 1 | `temporal_cleanup_age_days` |
| `retention.temporal_cleanup_expiry_days` | 整数 | `90` | >= 1 | `temporal_cleanup_expiry_days` |
| `retention.temporal_ttl_days` | 整数 | `7` | 任意整数 | `memory_temporal_ttl_days` |
| `retention.temporal_ttl_days_high` | 整数 | `14` | >= 1 | `temporal_ttl_days_high` |
| `retention.temporal_ttl_days_low` | 整数 | `3` | >= 1 | `temporal_ttl_days_low` |
| `retention.temporal_ttl_days_normal` | 整数 | `7` | >= 1 | `temporal_ttl_days_normal` |
| `retention.ttl_backfill_batch_size` | 整数 | `100` | >= 1 | `ttl_backfill_batch_size` |
| `retention.ttl_backfill_grace_hours` | 整数 | `0` | >= 0 | `ttl_backfill_grace_hours` |

运维历史清理默认开启，并按表独立事务和 `operational_batch_size` 分批执行。pending/running Job、pending 去重候选、
已标注 feedback 永不由该清理器删除；未注入 feedback 使用较短窗口。关闭 `operational_cleanup_enabled` 会同时跳过
运维表和 audit 的定期清理。

低于当前 `dedup.threshold` 的历史 pending pair 不再属于现行审查集合。先在离线副本运行
`hl-mem --db copy.db dedup drain-below-floor` 获取只读报告，再用同一路径追加
`--apply --expected-count <精确数量>`；apply 在单事务内复核数量，只写 `dismissed_below_floor` 和审计记录。

### `[server]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `server.max_request_body` | 整数 | `2097152` | 任意整数 | `max_request_body` |

### `[worker]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `worker.audit_retention_days` | 整数 | `30` | 任意整数 | `audit_retention_days` |
| `worker.conflict_auto_resolve_enabled` | 布尔值 | `true` | `true`、`false` | `conflict_auto_resolve_enabled` |
| `worker.conflict_auto_resolve_max_candidates` | 整数 | `8` | 2 - 10000 | `conflict_auto_resolve_max_candidates` |
| `worker.conflict_failure_backoff_seconds` | 整数 | `300` | 1 - 86400 | `conflict_failure_backoff_seconds` |
| `worker.conflict_maintenance_budget_ms` | 整数 | `1000` | 50 - 10000 | `conflict_maintenance_budget_ms` |
| `worker.conflict_maintenance_max_cases` | 整数 | `50` | 1 - 1000 | `conflict_maintenance_max_cases` |
| `worker.conflict_writer_yield_ms` | 整数 | `25` | 0 - 1000 | `conflict_writer_yield_ms` |
| `worker.consolidate_batch_size` | 整数 | `100` | 任意整数 | `consolidate_batch_size` |
| `worker.consolidate_confidence` | 数值 | `0.8` | 任意数值 | `consolidate_confidence` |
| `worker.consolidate_cron` | 字符串 | `"03:30"` | 任意字符串 | `consolidate_cron` |
| `worker.daily_token_limit` | 整数 | `500000` | 任意整数 | `daily_token_limit` |
| `worker.induce_policies_cron` | 字符串 | `"04:00"` | 任意字符串 | `induce_policies_cron` |
| `worker.job_lease_minutes` | 整数 | `5` | >= 1 | `worker_job_lease_minutes` |
| `worker.maintenance_interval` | 数值 | `600.0` | 任意数值 | `worker_maintenance_interval` |
| `worker.policy_induction_lookback_days` | 整数 | `7` | >= 1 | `policy_induction_lookback_days` |
| `worker.policy_induction_min_episodes` | 整数 | `3` | >= 1 | `policy_induction_min_episodes` |
| `worker.poll_interval` | 数值 | `2.0` | 任意数值 | `worker_poll_interval` |
| `worker.reclassify_cron` | 字符串 | `"04:30"` | 任意字符串 | `reclassify_cron` |

Worker 在任务执行期间按 lease 时长的三分之一周期续租全部同窗口 job；进度回调也会续租。若 token ownership
在终态写入前丢失，本次执行返回 `lease_lost`，不会把更新 0 行误报为成功。
冲突维护从持久 dirty queue 按 case 数与毫秒预算有界处理；失败按 case 退避，稳定人工案不会重复扫描或写入。

## 字段联动

- `retention.importance_write_floor <= retention.importance_low_threshold <= retention.importance_high_threshold`，且三者都在 `0.0 - 1.0`。
- `retention.decay_temporal_days <= retention.archive_temporal_days`；`retention.decay_permanent_days <= retention.archive_permanent_days`。
- `dedup.auto_merge_min_confidence` 不得低于 `dedup.threshold`。
- `recall.resurrection_mode = "auto"` 只在主召回低 answerability 或空结果时执行有界 archived-only FTS；候选仍须通过当前有效时间、全部来源引用完整性、冲突竞争者和高词项覆盖门禁；重嵌入服务失败时保留原召回结果。
- `decay.model = "activation_halflife"` 是默认值，使用 `activation = base * 2^(-inactive_days / half_life)` 且不改 confidence；`"legacy_linear"` 保留旧版 confidence 线性衰减，`"confidence_halflife"` 仅保留为实验对照臂。
- activation 半衰期按 temporal/permanent/identity 分档为 45/90/365 天；新建与迁移存量的 `activation_base/activation` 均从 1.0 开始，命中只刷新 `last_accessed_at`；activation 低于阈值并持续超过宽限期后才归档；`activation_halflife` 排序臂用 activation 承接原 confidence 的 `0.075` 冻结权重。
- `image_describer.mode = "on"` 时，base URL 必须使用 HTTPS，模型名不能为空；若同时允许 `file:` URI，`file_allow_roots` 不能为空。
- `hermes.enabled = true` 时，`hermes.url` 不能为空。

权威实现见 [`src/hl_mem/settings.py`](../src/hl_mem/settings.py) 和 [`src/hl_mem/config_loader.py`](../src/hl_mem/config_loader.py)。
