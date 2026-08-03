# HL-Mem 变更记录

## v0.21.0（未发布）

### Memory Query and Correction

- 新增 `GET /v1/memories/{memory_id}`，沿用 `MemoryQueryService` 返回 Claim 内容、生命周期与分类字段、`superseded_by_id`、完整 evidence links、来源事件及 conflict 历史。
- 新增 application 层 `CorrectionService` 与 `POST /v1/memories/{memory_id}/correct`：显式纠正只替换内容，不重新经过 extractor；新 Claim 继承原分类与重要性字段，并原子重算 fact hash、index text、embedding 和 TTL，旧 Claim 同步转为 `superseded`，其派生 observation 标记 stale，并建立 correction event 与替代证据链；远程 embedding 窗口使用继承字段快照防止并发分类更新丢失。
- MCP 新增 `memory_get`、`memory_correct`，与 REST 复用同一查询/纠正服务；`memory_feedback` 的显式 correction 也统一走该服务，并与 REST 一致返回 `correction_event_id`，同时保留既有 `id`、`replacement_event_id`、`forgotten`/`idempotent` 兼容字段。

### Reliability

- `Database` 在首次打开与迁移前自动创建数据库父目录，修复 `hlmem init` 后默认 `var/` 尚不存在时首次启动报 `unable to open database file`；该保护同时覆盖 server、worker、MCP 与自定义数据库路径。
- 本批次不新增 migration，schema 仍为 migration 036。

### Daily CLI and Offline First Run

- `hl-mem` 新增 `init`、`server`、`remember`、`recall`、`list`、`forget` 日常命令，并提供等价的 `hlmem` entry point；日常读写默认通过本地 HTTP 服务，保持与真实部署一致的事务、审计和错误语义。
- `hl-mem init --offline` 可生成无需 API key 的安全配置：确定性 fake extraction/embedding、关闭 reranker、图片描述、query expansion、关系发现和跨主体去重，并明确 fake embedding 不代表语义检索。
- 新增 `GET /v1/memories`，通过 application service 与 ClaimRepository 共享查询路径，按 namespace/status 提供稳定的 limit/offset 分页。
- 新增 `recall.dense_enabled`（默认 `true`）；关闭时召回管线完全跳过 query embedding 和 dense 候选收集。offline 配置同时关闭 tag channel，提供诚实的 FTS-only 关键词候选召回。
- 无有效 LLM key 时，Worker 不再调度需要 LLM 的每日冲突归并和跨主体去重任务；确定性的生命周期维护保持运行。

## v0.20.2 (2026-08-02)

### Recall Quality and Diagnostics

- 修复 query expansion 结构化提示词缺少 `queries` 输出键的问题；将默认模式切换为 `auto`，支持为扩展请求单独配置 `glm-4.7`，并将单次/总超时放宽到 5/6 秒。
- slot hint 现在同时匹配 `canonical_slot` 与兼容字段 `canonical_attribute`，并补充显存、VRAM、graphics card、处理器等高价值中英文别名。
- 回滚基于首轮 FTS 命中数和 dense top score 的 `low_recall` 触发升级，恢复候选数不足时才触发的保守规则，等待针对性评测完成后再决定是否扩大触发面。
- dense 检索结果携带真实 cosine `_score`，SearchTrace 的 dense channel scores 不再缺失原始通道分数。

### Evaluation

- 新增 28 条针对性配对评测集，并扩展 `eval_runner` 输出 `pair_id`、dense cosine 与 reranker raw score，支持 query expansion 开关的逐对基线比较。

### Configuration

- 恢复仓库与示例 TOML 的 `recall.relevance_keep_top1 = true`，保护低分但正确的 top-1 结果。
- 将仓库与示例 TOML 的 `recall.default_limit` 从 20 调整为 5、`recall.relevance_reranker_floor` 从 0.4 调整为 0.15；`Settings` 静态代码默认值保持 20 / 0.4，部署覆盖与代码默认在配置文档中明确区分。

### Reliability and Operations

- 移除 Bash watchdog，新增纯标准库 `scripts/healthcheck.py`，并补充 systemd、Windows Task Scheduler 与容器平台的跨平台探测、恢复和告警职责说明。
- 新增 Windows 单次执行的静默 `scripts/hlmem_supervisor.py`：兼容 `pythonw.exe`，提供连续失败阈值、重启冷却、跨次状态、陈旧锁回收、端口进程树终止和无窗口拉起。

### Compatibility

- 无新增数据库 migration，当前 schema 仍为 migration 036；无 REST 或 MCP 契约变更。`query_expansion_mode` 的 `Settings` 默认值从 `off` 变为 `auto`，生产部署需要提供 `LLM_API_KEY`，不希望产生扩展调用的环境应显式配置为 `off`。

## v0.20.1 (2026-08-02)

### Reliability and Observability

- 放宽 `canonical_attribute` 的 LLM 结构化输出 schema：非规范值不再导致提取任务直接失败，而是进入既有领域校验并回退为 `custom.unknown`。
- 将 `/healthz` 改为不依赖数据库的异步端点，仅返回进程内状态，避免线程池耗尽或数据库锁竞争使健康探针饿死。
- 历史记录：v0.20.1 曾新增 P0 Windows watchdog；后续架构改造已移除 `scripts/hlmem_watchdog.sh`，改用纯标准库的 `scripts/healthcheck.py` 提供跨平台探测，并将重启、告警等监督职责交给 systemd、Windows 服务管理器或容器编排平台。
- 新增 P1 HTTP 请求生命周期日志中间件：记录 `request_started` / `request_finished`、方法、路径、状态码、耗时及经过清理的可选 `X-Request-ID`，异常请求也会记录完成事件。
- 修复生产启动的 logging 配置：为 `hl_mem` logger 单独启用 INFO 输出，确保请求日志中间件事件不再被过滤，同时不开启第三方库的 INFO 日志。

### Configuration

- 在实例与示例配置中启用 relevance gate `observe` 模式，仅观测 `current_state` intent；阈值调整为 reranker `0.4`、dense `0.3`、relative drop `0.30`，并关闭 `keep_top1`。

### Compatibility

- 无新增数据库 migration，当前 schema 仍为 migration 036；无 REST 或 MCP 路由变更。`/healthz` 不再返回需要查询数据库的 24 小时 `llm_stats` 聚合，数据库与 LLM span 统计应使用专用只读审计路径。

## v0.20.0 (2026-08-01)

### Tokenized FTS v2 Migration

- 新增确定性中文 lexicalizer：NFKC 归一化、jieba 领域词典与 stopwords、技术标识符整体词和分段词；claims、events 与 claim topic tags 的生产稀疏检索统一切换到预分词 FTS v2，dense、RRF 与 reranker 通道保持不变。
- 新增不可变 migration 036，创建 `claims_fts_v2`、`events_fts_v2`、`claims_tags_fts_v2` 与源行删除清理 trigger；启动时校验 source/v2 rowid 集合，不完整时在单一 `BEGIN IMMEDIATE` 事务中原子重建三个通道。
- claim/event 写入、`index_text` compare-and-set 回填和 v2 文档更新共享事务边界；新增 `backfill_tokenized_fts` 工具支持按 channel 原子重建。
- 旧 trigram/raw FTS 表保留用于回滚窗口，但生产查询不再调用 legacy sanitizer 或 trigram fallback；计划在后续 migration 037 删除旧表。

### Evaluation

| 版本 / 口径 | 正例数 | Hit@1 | Hit@5 | MRR |
|---|---:|---:|---:|---:|
| v0.19.1 原始标签 | 20 | 0.6500 | 0.7500 | 0.6917 |
| v0.19.1 有证据子集 | 17 | 0.7647 | 0.8824 | 0.8137 |
| v0.20.0 tokenized FTS v2 + real dense/reranker | 17 | 0.8235 | 0.9412 | 0.8725 |

- 30-case 数据集经原文核对后将无库内事实的 q13、q16、q18 修正为 no-answer；当前口径为 17 条有证据正例与 13 条 no-answer。
- 发布门禁保持 hybrid Hit@5 >= 0.75；纯 FTS 的 Hit@5 为 0.1765，仅作为语义改写问题的诊断指标。

### Known Issues and Compatibility

- q08“向量模型”仍依赖后续语义别名与 reranker 融合改进；dense 通道仍会为 no-answer 查询补足候选，当前 no-answer precision 为 0，拒答阈值标定不在本版本范围内。
- 绕过 repository 的原始 SQL UPDATE 不会运行 Python lexicalizer，需用 `backfill_tokenized_fts` 修复内容陈旧；原始 SQL INSERT 造成的缺行会在下次启动时自动重建。
- lexicalizer 首次加载 jieba 词典会增加冷启动时间，首次升级还会同步重建三个 v2 channel。
- 无公共 API breaking change。数据库 migration 036 不可变且生产稀疏检索直切 v2；升级前应按既有流程备份数据库并预留首次重建时间。

## v0.19.1 (2026-07-31)

### Bug Fixes

- 修复 Hermes Provider hook 签名不匹配：`on_memory_write(action, target, content, metadata)` 匹配 Hermes 调用约定，`on_session_end(messages)` 接受位置参数，修复参数错位和 `invalidate_session` 从未执行的缺陷。
- `sync_turn()` 和 `on_memory_write()` 成功写入后主动失效 prefetch 缓存，消除同一会话内最多 300 秒的 stale bundle 窗口。
- A/B 测试 runner 增加实验有效性 gate：两 arm 投影 `text_hash` 相同时标记 `result.status = "inconclusive"`，避免从无效实验生成结论。

### Enhancements

- JSONL import 在 `jobs_queued > 0` 时向 stderr 输出 extractor model warning，提高 CLI 透明度。

## v0.19.0 (2026-07-31)

### Context Delivery and Feedback

- 新增严格版本化的 Context Packet v1：召回完成相关性过滤和最终 token 预算打包后，统一输出有序记忆条目、证据、answerability、截断状态及逐条 `feedback_id`。
- Claim 写入、FTS、dense embedding、回填与一致性检查统一消费持久化 `index_text`，避免同一 Claim 在不同检索通道使用不同文本投影。
- feedback exposure 改为在最终 Context Packet 物化时批量创建；Hermes 只在文本实际跨过 Agent host/model 输入边界后异步确认 `injected`，失败可降级并有限重试。
- 新增不可变 migration `035_retrieval_feedback_injected`，将 `retrieval_feedback.used_by_model` 收口为语义明确的 `injected` 字段。

### Operations and Compatibility

- 新增整库 `hl-mem backup` / `hl-mem restore` CLI：备份附带 SHA-256 manifest，恢复前校验 sidecar、大小、哈希和 SQLite integrity，并通过同目录临时库原子替换。
- JSONL import 默认按稳定幂等键重建缺失的 `extract_event` Job，使 Event 归档恢复后可由 Worker 重建 Claims；取证场景可显式跳过。
- REST 显式记忆与 MCP `memory_save` 支持调用方幂等键，重试会返回原始 Event/Claim，不再生成重复记忆。
- 数据集与 quality-smoke baseline hash 统一使用 CRLF/LF/CR 无关的 `sha256-utf8-lf-v1`；未知 schema 或 hash 算法拒绝静默比较。
- 修复 Windows/POSIX 启动脚本：从脚本位置解析仓库根目录、统一调用 `start_server.py`，删除旧 `start_v017.sh` 和失效的 `HL_MEM_*` 覆盖。
- 公共接口以 `namespace` 为唯一现行名称，`tenant_id` 仅保留为已弃用兼容别名；文档明确 namespace 是相关性软分区而非安全边界。

### Evaluation and Release Gates

- 修正评测中 Hit@5 与 Recall@5 的定义和聚合，Hit@5 仅表示是否至少命中一条，Recall@5 按相关集合覆盖率计算。
- 改善实验性 `answerable` 文本投影及其回填/一致性校验；受控 A/B 实验结果为 inconclusive（两 arm 投影文本相同），保留 `legacy` 为保守选择。
- 建立 v0.19 eval gate 基础设施：冻结 dataset/fixture/config、compatibility 与 non-regression gate、关键 slice 门禁、受控单变量 A/B 报告和显式 baseline 警告。
- CI 合成 fixture 的 no-answer 指标保留为已知限制，不作为真实 provider 的生产拒答质量证据。

### Configuration and Maintenance

- 发布版本更新为 `0.19.0`；DashScope 默认 LLM 模型与 provider 对齐为 `qwen3.7-plus`。
- GitHub Actions 中仍使用 Node 20 的 setup/upload action 已升级到 Node 24 运行时版本。
- 清理无入口、硬编码本机路径或生产数据库的一次性调试脚本，并扩充临时日志/pytest 输出忽略规则。

## v0.18.0 (2026-07-30)

### Breaking Changes

- 非敏感运行配置改为单一 TOML 配置源。所有正式入口默认从进程当前工作目录读取 `hl_mem.toml`；文件缺失、未知表或键、类型错误都会直接阻止启动。
- 删除运行环境 profile、`Settings.from_env()`、fake 自动回退以及全部 `HL_MEM_*` 配置变量。未在 TOML 中列出的字段使用 `Settings` 的静态安全默认值。
- `.env` 和进程环境现在只识别 `LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`；图片描述器不再复用 LLM 密钥。
- 提取、Embedding、Reranker、图片描述器的代码默认模式分别为 `fake`、`fake`、`off`、`off`。真实能力必须在 TOML 中显式启用并提供各自密钥。

### Configuration and Startup

- 新增 `config.example.toml` 常用配置模板和由 `Settings` metadata 生成的完整配置参考 `docs/configuration.md`。
- `start_server.py` 在组件创建前只加载、校验一次配置，并向 API 与 Worker 注入同一个不可变 `Settings` 快照。
- 升级步骤：复制 `config.example.toml` 为 `hl_mem.toml`，复制 `.env.example` 为 `.env`，填写已启用组件的密钥，并确保 systemd `WorkingDirectory` 指向这两个文件所在目录。

## v0.17.4 (2026-07-29)

### Bug Fixes
- Fixed an FTS column-name mismatch that caused rebuild failures (migration 034)
- Changed the `install_to_hermes.py` plugin path from `plugins/memory/` to `plugins/hl_mem/`

### Features
- Added the `hl_mem doctor` diagnostic command with 9 checks
- Added `.env` placeholder validation so production mode rejects fake keys

### Documentation
- Improved the README installation section with a systemd template, three-step verification, and FAQ

## v0.17.3 (2026-07-29)

### Bug Fixes
- **SQLite busy_timeout**: increased from 5s to 30s to prevent "database is locked" errors on slow disk IO
- Added configurable `HL_MEM_DB_BUSY_TIMEOUT_SECONDS` environment variable (default 30)
- Applied to both migration and regular connections

### Tests
- New test verifying busy_timeout configuration is applied correctly

## v0.17.2 (2026-07-29)

### Extraction Prompt Optimization

Rewrote SYSTEM_PROMPT with structured 10-section design based on industry research (Mem0/Letta/Zep/LangMem/Cognee):

- **Four-gate admission**: evidence gate, future utility gate, temporal gate, distinctiveness gate
- **Speech act classification**: asserted/committed admitted; proposed/hypothetical/procedural/phatic rejected
- **Discrete confidence anchors**: 0.98/0.90/0.75/0.55 (no arbitrary decimals)
- **Scope x volatility four-quadrant**: replaces single "one year" criterion
- **3 positive + 3 negative few-shot examples**: covering different confidence levels
- **Tightened predicates**: reviews/suggestions/hypotheses cannot use fact predicate
- **Process noise rejection**: execution status, CI snapshots, duration estimates

### Validation
- 7/7 dry-run extraction tests correct (3 noise rejected, 4 valid extracted)
- Confidence distribution now differentiated (0.98 vs 0.90 vs 0.75)
- 605 tests passed, 0 failed

## v0.17.1 (2026-07-29)

### Code Review Fixes (13 items)

#### P1 fixes
- Multi-channel fallback now enforces dense_floor when dense channel is present
- Session context SQL filters by actor_type before LIMIT
- Backfill CAS includes embedding_model and dimension; version tracks re-embedding
- Observe/enforce relative-drop computation unified (shared function, >= threshold)
- Observe mode writes relevance_reasons instead of filter_reasons (trace semantic fix)
- Eval runner consumes answerability for no-answer detection
- Gate check validates forbidden_hits, http_success_rate, no_answer_recall, slice coverage
- Session context get_recent_events() adds optional user_id parameter (defensive)

#### P2 fixes
- Context budget skips oversized messages instead of breaking
- Context outcome structured in trace (ok/empty/missing_session/read_error/deadline_exhausted)
- Batch cosine streaming via fetchmany (reduces peak memory)
- Vector sort key uses claim_id for deterministic tie-break
- Backfill retry adds backoff and error classification

## v0.17.0 (2026-07-29)

### Recall Pipeline Improvements

#### A: Evaluation Infrastructure
- Expanded recall evaluation dataset from 50 to 80 cases with 7 slice categories
- Added eval_runner.py for Recall@K, MRR, nDCG@5, Top-3 precision, latency metrics
- Added gate_check.py for release quality gating
- Versioned baseline tracking (baseline_v1.json)

#### E: NumPy Batch Cosine
- Replaced pure-Python cosine loop with NumPy batch matrix computation
- 100-500x faster dense vector scanning, same exact results
- Added vector_batch_size setting (default 512) for memory control
- Added numpy>=2.0 as runtime dependency

#### B: Relevance Gate (observe + enforce)
- New HL_MEM_RELEVANCE_GATE_MODE=off|observe|enforce (default off)
- observe: computes per-candidate relevance diagnostics without truncation
- enforce: truncates low-relevance tail for current_state intent (whitelist)
- Per-candidate score_path and reranker_raw_score in API response
- answerability field promoted to API schema

#### C: Answerable Index Text
- New index_text mode "answerable": subject + readable label + value
- Template from SLOT_REGISTRY, deterministic, no LLM
- CLI: python -m hl_mem.cli backfill-index-text --dry-run
- Safe compare-and-set backfill with cursor resume

#### D: Session-Aware Coreference Resolution
- session_id now flows from API through to QueryExpander
- coreference queries use session context for disambiguation
- Fixed: get_recent_events() now filters by namespace (tenant_id)
- Fixed: Settings.from_env() default query_expansion_mode unified to off
- New: HL_MEM_QUERY_CONTEXT_MODE=off|coreference (default off)

### Bug Fixes
- session_id was defined in RecallInput but never passed to RecallService
- get_recent_events() lacked namespace isolation (tenant_id filter)
- Settings.from_env() implicitly defaulted to query_expansion_mode=auto

### Dependencies
- Added numpy>=2.0

## Unreleased

### Fixed

- 行为兼容修正：未设置 `HL_MEM_QUERY_EXPANSION_MODE` 时现在与 `Settings` 默认值一致为 `off`；需要旧隐式行为的部署可显式设置为 `auto`。

### Added

- 新增默认关闭的会话感知指代查询改写，并按 namespace/session 隔离读取最小文本上下文。

## v0.16.1 — 2026-07-28

### Fixed

- 精确化 trace observation 的 error 检测逻辑：改用行首模式匹配（Traceback / `Error:` / `FAILED`）与非零 exit_code 判断，不再因输出中包含 "error" 子串而误标。

### Changed

- 新增 `scripts/reclassify_predicates.py`：用 LLM 对历史 predicate="事实" 的 claims 重新分类，实际数据治理将"事实"占比从 52% 降至 14%。

## v0.16.0 — 2026-07-28

### Added

- 新增召回诊断、`index_text` 三模式 A/B、provider 调用观测与跨模型结构化提取 benchmark。
- 新增 `HL_MEM_LLM_ENABLE_THINKING`、`HL_MEM_INDEX_TEXT_MODE`、结构化输出重试与提取分块配置。
- 新增可审计的 scope 降级、canonical attribute → predicate 投影、subject 守卫与扩展 JSON repair。

### Fixed

- 修复 claim 独立索引文本上线后，仓储兼容写入未生成 `index_text` 导致 FTS 漏召回的问题。
- 将 claim FTS 更新触发器收窄到 `index_text`，避免访问计数、状态和标签更新造成无关索引写放大。
- 更新领域分层重构后的单元测试导入路径。

### Documentation

- 更新 README、配置模板、架构与交接状态；将已完成的 benchmark/research/task 过程文档移入历史归档。

## v0.15.0 — 2026-07-26

- Provider diagnostics, safe answerability shadow gating, and bounded expansion deadlines.
- Query slot hints, regression coverage, sliding-window monitoring, alerts, and health snapshots.
- Calibration, score paths, claim index text, provider-call persistence, daily reports, and controlled A/B evaluation.

本文件记录发布级变更摘要。测试数字是对应版本的发布基线；migration 数是该版本结束时的 SQL migration 总数。

## v0.14.3 — 2026-07-26

- **类型治理**：清零 mypy 错误，移除 baseline 门禁，并对 core/domain 启用 strict。
- **CI 与质量门禁**：migration 使用冻结的 uv 环境；quality smoke 收紧 Recall@5/MRR 容差、增加最大排名约束并报告 p50/p90 延迟。
- **Tests**: validated by CI workflow on tag commit

## v0.14.1 — 2026-07-26

- **治理门禁**：mypy 纳入 uv 锁文件，Ruff 扩展为全仓库检查，主 CI 增加 smoke 与 `v*` tag 触发。
- **质量 smoke v2**：17 个确定性用例覆盖干扰项、同义查询、真实 supersede 生命周期、关系存储/发现与负例，并加入哈希基线、delta 和退化阈值。
- **类型债**：按配置解析、存储行边界和可空分支根因修复指定模块，mypy 基线由 37 降至 7。
- **验证约束**：按发布任务要求未运行 pytest；发布门禁由 Ruff、导入边界、文档一致性、mypy baseline 与 quality smoke 验证。

## v0.14.0 — 2026-07-26

- **类型与 lint**：Ruff 扩展至 F/E4/E7/E9/I；mypy 基线由 68 降至 37，并清零 recall/storage 当前错误。
- **质量趋势 MVP**：新增 10 条确定性 smoke 数据、离线 runner，以及 nightly/manual GitHub Action artifact。
- **契约治理**：新增 PR contract checklist，并要求人工审查 OpenAPI/MCP snapshot 变更。
- **工程配置**：统一 uv dependency group 和 CI 安装方式，以 CI badge 替代 README 硬编码测试数字。
- **验证**：按发布任务约束未运行 pytest；最近冻结测试基线为 445 passed，1 skipped。

## v0.13.4 — 2026-07-26

- **P0 治理**：版本 SSOT 覆盖 `pyproject.toml`，mypy 新错误门禁，CI 固定 lockfile，v0.10 历史数据库升级夹具，
  Policy/Derivation 生命周期守卫，以及扩展后的分层导入边界。
- **P1 公共契约**：新增兼容性政策、OpenAPI/MCP 快照、JSONL 导出格式版本和环境变量稳定性分级。
- **P2 质量趋势**：新增 nightly/manual 趋势基础设施设计文档，未实现运行器或工作流。
- **验证**：按治理任务约束未运行 pytest；最近已验证基线为 445 passed，1 skipped。

## v0.13.3 — 2026-07-26

### Fixed

- 修复 CI dev extra 与 coverage 门禁。
- 收紧 recall fold 语义保护，并为 TTL 扫描增加 180 天候选窗口。

### Changed

- 校正文档、能力矩阵、MCP 工具数和 PostgreSQL 实验性状态。

- **Migrations**：29（无新增）。
- **测试**：445 passed，1 skipped。

## v0.13.0 — 2026-07-26

### Added

- 新增能力成熟度矩阵，以及格式、构建、Python 3.12、空库 migration 和依赖方向 CI 门禁。

### Changed

- 完成工程收敛并修正文档 SSOT。

- **Migrations**：29（无新增）。
- **测试**：443 passed，1 skipped（沿用 v0.12.4 发布基线，本版本按发布约束未重跑 pytest）。

## v0.12.4 — 2026-07-26

### Fixed

- 修复 temporal cleanup/TTL 并发竞态。
- 收敛召回折叠语义和成本。

### Added

- 关系提案按 `run_id` 保留审计历史。
- 新增 TTL/cleanup 扫描索引。

- **Migrations**：29（新增 028、029）。
- **测试**：443 passed，1 skipped。

## v0.12.3 — 2026-07-26

### Added

- 新增默认关闭的 deterministic extraction pre-filter，在 LLM 调用前过滤低价值运行时事件。

### Changed

- 过滤结果保留审计，规则异常时回退正常提取。

- **Migrations**：27（无新增）。
- **测试**：433 passed，1 skipped。

## v0.12.2 — 2026-07-26

### Added

- 召回输出 score，并增加相似度折叠和 temporal 回填维护。

### Changed

- 清理语义重复 Claim，增强提取 prompt。

- **Migrations**：27（无新增）。
- **测试**：411 passed，1 skipped。

## v0.12.1 — 2026-07-26

### Fixed

- 修复 usefulness 类型约束、TTL 双时间和关系并发写入。
- 收敛组件降级、查询扩展并发、召回副作用和 benchmark 时间语义。

- **Migrations**：27（新增 025–027）。
- **测试**：401 passed，1 skipped。

## v0.12.0 — 2026-07-26

### Added

- 交付多查询召回、关系候选发现、Benchmark suite、图片证据入口、反馈驱动维护和 Tool/Procedure intent。

- **Migrations**：24（新增 023、024）。
- **测试**：373 passed，1 skipped。

## v0.11.2 — 2026-07-25

### Fixed

- 补齐 trigram FTS 行为与 migration 022 回归，并完成数据清理。

### Changed

- CI 扩展为全量测试套件。

- **Migrations**：22（无新增）。
- **测试**：342 passed。

## v0.11.1 — 2026-07-24

### Fixed

- 修复空 trigger、异常边界和 reranker retry。

### Changed

- 统一配置 Enum、HTTP retry、状态校验和核心类型/docstring。

- **Migrations**：22（无新增）。
- **测试**：325 passed。

## v0.11.0 — 2026-07-24

### Added

- 新增 LLM spans、Job 进度、中文 FTS 评测、后端协议、dry-run extraction 和 ConsolidationScope。

### Changed

- 将 Claim FTS 切换为 trigram。

- **Migrations**：22（新增 019–022）。
- **测试**：325 passed。

## v0.10.1 — 2026-07-24

### Added

- 增加 MRR/nDCG 和独立 behavioral scenarios。

### Changed

- 冻结排序因子，统一 RecallConfig，并类型化召回上下文。

- **Migrations**：18（无新增）。
- **测试**：292 passed，1 skipped。

## v0.10.0 — 2026-07-24

### Added

- 完成 topic tags soft boost、可选独立 tag channel 和确定性 query-to-tag 解析。

- **Migrations**：18（新增 018）。
- **测试**：292 passed，1 skipped。

## v0.9.1 — 2026-07-24

### Fixed

- 修复 qualifier 降级、TTL UTC 统一和回填 CAS。

- **Migrations**：17（无新增）。
- **测试**：277 passed。

## v0.9.0 — 2026-07-24

### Added

- 交付 slot+tags 分类、跨 subject 审计去重，以及 importance 联动 TTL。

- **Migrations**：17（新增 016、017）。
- **测试**：发布记录未保留精确计数；v0.9.1 基线为 277 passed。

## v0.7.0 — 2026-07-24

### Added

- 完成 canonical attribute、scope 后置规则、TTL policy 和 decay priority。

- **Migrations**：15。
- **测试**：发布记录未保留精确计数。

## v0.3.0 — 2026-07-23

### Added

- 完成冲突检测、事务原子化、fact_hash v2、MCP application 委托与初始架构分层。

- **Migrations**：13。
- **测试**：发布记录未保留精确计数。
