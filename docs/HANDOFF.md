# HL-Mem 项目交接状态

> 最后更新：2026-08-04 · v0.21.2

## 当前状态

- **分支**：`main`
- **版本**：v0.21.2
- **阶段**：v0.21.2 发版准备
- **服务**：FastAPI on port 8200；非敏感配置来自必需的 `hl_mem.toml`，四个独立密钥来自 `.env` 或进程环境
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`），36 migrations；实时数据量以数据库只读审计为准
- **FTS**：预分词 FTS v2（claims/events/tags）；旧 trigram/raw 表仅保留在回滚窗口

## 已完成

### 2026-08-04 v0.21.2 冲突终态收敛

- 自动冲突维护回访 `pending`、`auto_resolved`、`manual_required` 全部未决 case，并沿 supersede 链识别汇聚端点和唯一存活端，消除已分类 case 的扫描盲区。
- CLI 人工 `keep_left`/`keep_right` 裁决同步将 loser 收敛为 `superseded`，补齐 `superseded_by_id` 与双时间边界；列表输出补充左右 Claim 的 value、status、authority、`recorded_from`。
- `/healthz` 新增 `conflict_open_count`，直接暴露未决冲突积压；该端点复用应用生命周期数据库连接，不调用外部 provider。
- v0.21.2 无新增 migration；数据库 schema 保持在 migration 036。

### 2026-08-03 v0.21.1 MCP runtime 与发布准备

- MCP 七工具契约接入官方 Python SDK 2.x 低层 `Server` 与 stdio transport，提供 `hl-mem-mcp`/`python -m hl_mem.mcp` 入口、线程化同步调用和 `isError` 业务错误。
- 新增 PyPI Trusted Publishing tag workflow、PyPI-first 中英文快速开始、Codex/Claude/Cursor MCP 配置说明和 TOML/密钥恢复型错误信息。
- v0.21.1 无新增 migration；数据库 schema 保持在 migration 036。

### 2026-08-02 v0.20.2 召回质量与部署监督

- query expansion 修复结构化输出提示词，默认切换到 `auto`，支持独立模型，并将单次/总超时放宽到 5/6 秒；低召回证据触发升级已回滚到原有候选数规则。
- slot hint 同时匹配 `canonical_slot` 与 `canonical_attribute`，补充显存、处理器等高价值别名；dense cosine 进入 SearchTrace channel scores。
- 新增 28 条针对性配对评测，runner 输出 `pair_id`、dense cosine 与 reranker raw score，便于比较 query expansion 前后变化。
- 仓库与示例 TOML 将默认召回条数设为 5、reranker floor 设为 0.15，并恢复 `keep_top1 = true`；`Settings` 静态默认值仍单独保留在配置参考中。
- 监督方案以跨平台 `healthcheck.py` 为基础；Windows 可选用 `hlmem_supervisor.py` 由 Task Scheduler 静默执行连续失败恢复。
- v0.20.2 无新增 migration；数据库 schema 保持在 migration 036，REST 与 MCP 契约不变。

### 2026-08-02 v0.20.1 可靠性热修复

- LLM 提取 schema 接受非规范 `canonical_attribute` 字符串，交由领域校验回退，避免提取 Job 因格式偏差直接 dead。
- `/healthz` 已改为 DB-free async liveness probe，不再因数据库锁或同步线程池饥饿而超时；历史 LLM span 聚合不再放入该响应。
- 跨平台 `scripts/healthcheck.py` 通过 `/healthz` 返回监督退出码；systemd、Windows 服务管理器或容器编排平台负责定时探测、重启与告警，部署示例见 `docs/watchdog.md`。
- FastAPI P1 请求日志记录 start/end、状态码、单调时钟耗时和受控 `X-Request-ID`，覆盖异常退出路径。
- v0.20.1 无新增 migration；数据库 schema 保持在 migration 036。

### 2026-07-31 v0.19.0 集成交付

- Context Packet v1 已成为召回最终交付契约：相关性过滤和 token 预算之后输出有序条目、证据、answerability、截断诊断及逐条 `feedback_id`。
- feedback exposure 在 packet 物化时批量落库；Hermes 在文本实际进入 Agent host/model 输入边界后确认 `injected`，失败不阻断交付并通过有界队列重试。
- migration 035 将 `retrieval_feedback.used_by_model` 重命名为 `injected`；现有 35 个 SQL migration 仍保持不可变、仅向前执行。
- Claim 写入、FTS、dense embedding、回填和投影一致性检查统一消费持久化 `index_text`。
- `answerable` 投影完成规则、回填、校验和单变量 A/B 基础设施；受控 A/B 实验结果为 inconclusive（两 arm 投影文本相同），保留 `legacy` 为保守选择。
- Hit@5 与 Recall@5 已按“是否命中”与“相关集合覆盖率”分别计算；v0.19 legacy compatibility 与关键 slice gate 已接入 CI，candidate non-regression/win condition 配置供显式 A/B 使用。
- 整库 backup/restore CLI 支持 manifest、SHA-256、sidecar/integrity 校验及原子替换。
- JSONL import 默认为新增或缺失的 Event 补建稳定 `extract_event` Job，使 Worker 可重建 Claims。
- REST 显式记忆与 MCP `memory_save` 支持调用方幂等键；CRLF/LF/CR 数据集 hash 统一为 `sha256-utf8-lf-v1`。
- Windows/POSIX 启动脚本统一定位仓库根目录并调用 `start_server.py`；旧版本脚本与失效环境变量覆盖已移除。
- 公共调用面统一使用 `namespace`；`tenant_id` 只作为已弃用兼容别名，且 namespace 不构成安全隔离。
- LLM、Embedding 与 Reranker 的 API 密钥通过 `.env` 配置，provider/model 等非敏感选项通过 TOML 配置；活文档不固化具体型号。

### 2026-07-28 提取与召回治理

- scope：LLM 给出 `permanent` 后仍经过确定性语义规则复核；运行态、测试结果、版本态等可降级为 `temporal`，并记录原因码。
- predicate：canonical attribute 完成协调后反向投影 canonical predicate，避免属性与 predicate 漂移。
- subject：无效、共享占位或代词主体优先替换为有效实体，否则生成事件隔离 subject，防止跨事件污染。
- repair：结构化输出先做确定性 JSON repair 和兼容字段补齐，再进入有界 schema retry；repair 数量进入诊断信息。
- recall：claim 使用独立 `index_text`，支持 `legacy` / `value_only` / `natural` 对照；provider 调用、扩展路径与 score trace 可观测。
- benchmark：已具备固定测试集、manifest、断点恢复、gold evaluation 和多模型横评脚本；运行产物不纳入源码版本控制。
- P0：完成数据清洗只读审计与治理后召回实测；报告保留在 `docs/`，执行型计划和研究草稿归档。

### 核心功能

- 3 种记忆类型（event + claim + observation）
- LLM 提取（前序上下文 + 时间锚定 + ADD-only）+ Embedding + Reranker（模型和维度由 TOML 配置）
- 三层去重：fact_hash v2 → conflict_key（白名单互斥）→ semantic (best-match, 0.82)
- 冲突检测：确定性 ConflictResolver（5 slots）+ LLM ConflictConsolidator（灰区）+ 全未决状态回访与链汇聚收敛
- 数据质量：实体归一化 + canonical attribute reconcile + scope 后置规则 + TTL policy
- 混合召回：FTS5 BM25 + Dense Vector → RRF → 多因子排序 → Reranker → 上下文预算打包
- Experience 通道：Episode + Trace + Policy + 奖励回传
- 生命周期：TTL 过期 → 线性衰减 → 归档 → 重分类
- 显式遗忘：级联撤回 + 向量清除 + stale 传播
- Hermes Provider（可配置 timeout + circuit breaker + prefetch/delivery receipt）
- MCP Server（7 工具契约，官方 SDK 2.x stdio runtime，beta）
- REST API 新增 `POST /v1/extract/dry-run`、`POST /v1/consolidate`
- LLM 可观测性：`llm_call_spans` 持久化调用 span；`healthz` 不查询 span 表，但会读取并暴露 `conflict_open_count`
- 审计日志 + 整库备份/恢复 + 可重建 Job 的 JSONL 导入导出
- 实验性 PostgreSQL 连接探针（尚无 HL-Mem 存储语义）

### v0.12.0 六大特性

- 多查询召回：默认 `auto`，短/指代查询按需扩展，失败回退原始 query
- 关系候选发现：默认 `audit`，只记录 proposal，不自动写边
- Benchmark suite：LongMemEval adapter + extraction/retrieval/lifecycle 三层指标，CLI 按需运行
- 图片证据入口：视觉描述与证据落库已实现，默认 `off`
- 反馈驱动维护：usefulness 聚合已接入，默认 `observe`，不影响 TTL/decay
- Tool/Procedure intent：Experience pipeline 确定性路由，默认 `keyword`

### 架构重构（v0.10.0）

- P0 数据正确性：事务原子化 + fact_hash v2 + MCP pipeline 修复
- 分层架构：api/ → application/ → domain/core/ → storage/
- 状态机统一：ClaimStatus + EpisodeStatus 集中到 lifecycle.py
- 配置集中化：config.py + settings.py + components.py
- Provider 合并：删除冗余 adapter
- P2 质量：Protocol 接口化、错误分类化、retry 工具化

## 下一步

- 观察 relation proposal 准确率与 usefulness 聚合数据，再评估 `auto` / `on` 模式
- 接入实际图片输入源后评估开启视觉描述器
- 根据实际使用反馈调优提取 prompt 和召回质量
- 用真实 provider 与明确 abstention 语义评估 no-answer precision/recall；CI 合成 fixture 的 0/0 仅作已知限制
- 仅在受控 A/B 显示显著收益后再考虑将 `answerable` 改为默认投影
- Mental Model 推理增强（基础已实现）
- 多租户（架构设计保留）

## 关键文档索引

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 版本变更时间线 |
| [architecture.md](architecture.md) | 当前已实现架构 |
| [implementation-plan.md](implementation-plan.md) | 实现计划 |
| [adr/0001-core-strategy.md](adr/0001-core-strategy.md) | 核心策略决策 |
| [adr/0002-mvp-scope-and-embedding.md](adr/0002-mvp-scope-and-embedding.md) | 首版范围 + Embedding 选型 |
| [archive/refactor/](archive/refactor/) | 架构重构各阶段历史记录 |
| [api.md](api.md) | REST API 端点与请求约定 |
| [capability-matrix.md](capability-matrix.md) | 能力成熟度与默认模式 |
| [archive/reviews/consensus.md](archive/reviews/consensus.md) | 首版共识（历史归档） |
| [archive/reviews/optimization-consensus.md](archive/reviews/optimization-consensus.md) | 优化共识（历史归档） |
| [archive/](archive/) | 历史任务单和中间讨论 |

## 已知风险

- LLM 提取可能产生假事实 → 原始证据链保留
- 中文实体归一化/时间表达容易出错 → 独立中文测试集
- 自动遗忘可能误删低频关键信息 → 首版只降权和归档，不物理删除
- 当前 embedding 模型批量上限 10 条/批（取决于 TOML 中选择的模型） → 异步受控并发
