# HL-Mem 配置参考

HL-Mem 0.18.0 使用单个 TOML 文件保存非敏感配置，并用 `.env` 或同名进程环境变量保存四个密钥。
`Settings` 是唯一 schema；下表由 `Settings` 字段 metadata 自动生成。未写入 TOML 的字段使用代码默认值。

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

### `[dedup]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `dedup.audit_only` | 布尔值 | `true` | `true`、`false` | `dedup_audit_only` |
| `dedup.auto_merge_min_confidence` | 数值 | `0.98` | dedup.threshold - 1.0 | `dedup_auto_merge_min_confidence` |
| `dedup.cron` | 字符串 | `"03:00"` | HH:MM（00:00 - 23:59） | `dedup_cron` |
| `dedup.enabled` | 布尔值 | `true` | `true`、`false` | `dedup_enabled` |
| `dedup.scan_limit` | 整数 | `200` | >= 1 | `dedup_scan_limit` |
| `dedup.threshold` | 数值 | `0.92` | 0.0 - 1.0 | `dedup_threshold` |

### `[embedding]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `embedding.base_url` | 字符串 | `"https://dashscope.aliyuncs.com/compatible-mode/v1"` | 任意字符串 | `embedding_base_url` |
| `embedding.connect_timeout` | 数值 | `5.0` | 任意数值 | `embedding_connect_timeout` |
| `embedding.dim` | 整数 | `2048` | 任意整数 | `embedding_dim` |
| `embedding.max_attempts` | 整数 | `3` | 任意整数 | `embedding_max_attempts` |
| `embedding.mode` | 字符串 | `"fake"` | `fake`、`real` | `embedder_mode` |
| `embedding.model` | 字符串 | `"text-embedding-v4"` | 任意字符串 | `embedding_model` |
| `embedding.read_timeout` | 数值 | `30.0` | 任意数值 | `embedding_read_timeout` |

### `[entity]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `entity.aliases_path` | 字符串 | 未设置 | 非空字符串；可省略 | `entity_aliases_path` |

### `[extraction]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `extraction.chunk_overlap_turns` | 整数 | `2` | >= 0 | `extraction_chunk_overlap_turns` |
| `extraction.chunk_target_chars` | 整数 | `12000` | >= 1 | `extraction_chunk_target_chars` |
| `extraction.max_split_depth` | 整数 | `3` | >= 0 | `extraction_max_split_depth` |
| `extraction.mode` | 字符串 | `"fake"` | `fake`、`real`、`llm` | `extractor_mode` |
| `extraction.pre_filter` | 布尔值 | `false` | `true`、`false` | `extract_pre_filter` |

### `[hermes]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `hermes.circuit_failure_threshold` | 整数 | `5` | >= 1 | `hermes_circuit_failure_threshold` |
| `hermes.circuit_open_seconds` | 数值 | `60.0` | > 0 | `hermes_circuit_open_seconds` |
| `hermes.enabled` | 布尔值 | `false` | `true`、`false` | `hermes_enabled` |
| `hermes.home` | 字符串 | 未设置 | 非空字符串；可省略 | `hermes_home` |
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
| `index.text_mode` | 字符串 | `"legacy"` | `legacy`、`value_only`、`natural`、`answerable` | `index_text_mode` |
| `index.text_version` | 字符串 | `"v1"` | 非空字符串 | `index_text_version` |

### `[llm]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `llm.base_url` | 字符串 | `"https://coding.dashscope.aliyuncs.com/v1"` | 任意字符串 | `llm_base_url` |
| `llm.enable_thinking` | 布尔值 | `false` | `true`、`false` | `enable_llm_thinking` |
| `llm.max_attempts` | 整数 | `3` | >= 1 | `llm_max_attempts` |
| `llm.model` | 字符串 | `"glm-5.2"` | 非空字符串 | `llm_model` |
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
| `recall.default_limit` | 整数 | `20` | 1 - 100 | `recall_default_limit` |
| `recall.expansion_circuit_failure_threshold` | 整数 | `5` | >= 1 | `expansion_circuit_failure_threshold` |
| `recall.expansion_circuit_open_seconds` | 数值 | `60.0` | > 0 | `expansion_circuit_open_seconds` |
| `recall.feedback_min_samples` | 整数 | `3` | >= 1 | `feedback_min_samples` |
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
| `recall.query_expansion_mode` | 字符串 | `"off"` | `off`、`auto`、`always` | `query_expansion_mode` |
| `recall.query_expansion_timeout_seconds` | 数值 | `2.0` | > 0 | `query_expansion_timeout_seconds` |
| `recall.query_expansion_token_ceiling` | 整数 | `256` | > 0 | `query_expansion_token_ceiling` |
| `recall.query_expansion_total_timeout_seconds` | 数值 | `3.0` | > 0 | `query_expansion_total_timeout_seconds` |
| `recall.relevance_dense_floor` | 数值 | `0.3` | 0.0 - 1.0 | `relevance_dense_floor` |
| `recall.relevance_gate_mode` | 字符串 | `"off"` | `off`、`observe`、`enforce` | `relevance_gate_mode` |
| `recall.relevance_intents` | 字符串 数组 | `["current_state"]` | 非空数组；元素为 current_state、preference、historical、tool、procedure | `relevance_intents` |
| `recall.relevance_keep_top1` | 布尔值 | `true` | `true`、`false` | `relevance_keep_top1` |
| `recall.relevance_relative_drop` | 数值 | `0.15` | 0.0 - 1.0 | `relevance_relative_drop` |
| `recall.relevance_reranker_floor` | 数值 | `0.4` | 0.0 - 1.0 | `relevance_reranker_floor` |
| `recall.side_effect_backoff_seconds` | 数值 | `0.05` | >= 0 | `recall_side_effect_backoff_seconds` |
| `recall.side_effect_max_attempts` | 整数 | `3` | >= 1 | `recall_side_effect_max_attempts` |
| `recall.tag_boost_enabled` | 布尔值 | `true` | `true`、`false` | `tag_boost_enabled` |
| `recall.tag_boost_weight` | 数值 | `0.05` | 0.0 - 1.0 | `tag_boost_weight` |
| `recall.tag_candidate_limit` | 整数 | `20` | >= 1 | `tag_candidate_limit` |
| `recall.tag_channel_enabled` | 布尔值 | `false` | `true`、`false` | `tag_channel_enabled` |
| `recall.tag_channel_weight` | 数值 | `0.15` | 0.0 - 1.0 | `tag_channel_weight` |
| `recall.vector_backend` | 字符串 | `"sqlite_scan"` | `sqlite_scan` | `vector_backend` |
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
| `reranker.model` | 字符串 | `"gte-rerank-v2"` | 任意字符串 | `reranker_model` |
| `reranker.provider` | 字符串 | `"dashscope"` | `dashscope` | `reranker_provider` |

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
| `retention.event_days` | 整数 | `30` | 任意整数 | `retention_days` |
| `retention.feedback_bonus_cap_days` | 整数 | `180` | >= 0 | `feedback_bonus_cap_days` |
| `retention.feedback_bonus_days` | 整数 | `14` | >= 0 | `feedback_bonus_days` |
| `retention.feedback_bonus_every` | 整数 | `3` | > 0 | `feedback_bonus_every` |
| `retention.feedback_lifecycle_mode` | 字符串 | `"observe"` | `off`、`observe`、`on` | `feedback_lifecycle_mode` |
| `retention.importance_high_threshold` | 数值 | `0.7` | 见字段联动约束 | `importance_high_threshold` |
| `retention.importance_low_threshold` | 数值 | `0.4` | 见字段联动约束 | `importance_low_threshold` |
| `retention.importance_write_floor` | 数值 | `0.2` | 见字段联动约束 | `importance_write_floor` |
| `retention.slot_short_ttl_seconds` | 整数 | `86400` | >= 1 | `slot_short_ttl_seconds` |
| `retention.temporal_cleanup_age_days` | 整数 | `30` | >= 1 | `temporal_cleanup_age_days` |
| `retention.temporal_cleanup_expiry_days` | 整数 | `90` | >= 1 | `temporal_cleanup_expiry_days` |
| `retention.temporal_ttl_days` | 整数 | `7` | 任意整数 | `memory_temporal_ttl_days` |
| `retention.temporal_ttl_days_high` | 整数 | `14` | >= 1 | `temporal_ttl_days_high` |
| `retention.temporal_ttl_days_low` | 整数 | `3` | >= 1 | `temporal_ttl_days_low` |
| `retention.temporal_ttl_days_normal` | 整数 | `7` | >= 1 | `temporal_ttl_days_normal` |
| `retention.ttl_backfill_batch_size` | 整数 | `100` | >= 1 | `ttl_backfill_batch_size` |
| `retention.ttl_backfill_grace_hours` | 整数 | `0` | >= 0 | `ttl_backfill_grace_hours` |

### `[server]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `server.max_request_body` | 整数 | `2097152` | 任意整数 | `max_request_body` |

### `[worker]`

| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |
|---|---|---|---|---|
| `worker.audit_retention_days` | 整数 | `30` | 任意整数 | `audit_retention_days` |
| `worker.consolidate_batch_size` | 整数 | `100` | 任意整数 | `consolidate_batch_size` |
| `worker.consolidate_confidence` | 数值 | `0.8` | 任意数值 | `consolidate_confidence` |
| `worker.consolidate_cron` | 字符串 | `"03:30"` | 任意字符串 | `consolidate_cron` |
| `worker.daily_token_limit` | 整数 | `500000` | 任意整数 | `daily_token_limit` |
| `worker.induce_policies_cron` | 字符串 | `"04:00"` | 任意字符串 | `induce_policies_cron` |
| `worker.job_lease_minutes` | 整数 | `5` | 任意整数 | `worker_job_lease_minutes` |
| `worker.maintenance_interval` | 数值 | `600.0` | 任意数值 | `worker_maintenance_interval` |
| `worker.policy_induction_lookback_days` | 整数 | `7` | >= 1 | `policy_induction_lookback_days` |
| `worker.policy_induction_min_episodes` | 整数 | `3` | >= 1 | `policy_induction_min_episodes` |
| `worker.poll_interval` | 数值 | `2.0` | 任意数值 | `worker_poll_interval` |
| `worker.reclassify_cron` | 字符串 | `"04:30"` | 任意字符串 | `reclassify_cron` |

## 字段联动

- `retention.importance_write_floor <= retention.importance_low_threshold <= retention.importance_high_threshold`，且三者都在 `0.0 - 1.0`。
- `retention.decay_temporal_days <= retention.archive_temporal_days`；`retention.decay_permanent_days <= retention.archive_permanent_days`。
- `dedup.auto_merge_min_confidence` 不得低于 `dedup.threshold`。
- `image_describer.mode = "on"` 时，base URL 必须使用 HTTPS，模型名不能为空；若同时允许 `file:` URI，`file_allow_roots` 不能为空。
- `hermes.enabled = true` 时，`hermes.url` 不能为空。

权威实现见 [`src/hl_mem/settings.py`](../src/hl_mem/settings.py) 和 [`src/hl_mem/config_loader.py`](../src/hl_mem/config_loader.py)。
