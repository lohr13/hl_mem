# HL-Mem 变更记录

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
