# HL-Mem 项目交接状态

> 最后更新：2026-07-31 · v0.19.1

## 当前状态

- **分支**：`main`
- **版本**：v0.19.0
- **阶段**：v0.19.0
- **服务**：FastAPI on port 8200；非敏感配置来自必需的 `hl_mem.toml`，四个独立密钥来自 `.env` 或进程环境
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`），35 migrations；实时数据量以 `/healthz` 和只读审计为准
- **FTS**：trigram（claims/tags），unicode61（events）

## 已完成

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
- DashScope 默认 LLM 模型已对齐为 `qwen3.7-plus`，真实模型仍以 `.env` 密钥和 TOML 非敏感配置为准。

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
- 冲突检测：确定性 ConflictResolver（5 slots）+ LLM ConflictConsolidator（灰区）
- 数据质量：实体归一化 + canonical attribute reconcile + scope 后置规则 + TTL policy
- 混合召回：FTS5 BM25 + Dense Vector → RRF → 多因子排序 → Reranker → 上下文预算打包
- Experience 通道：Episode + Trace + Policy + 奖励回传
- 生命周期：TTL 过期 → 线性衰减 → 归档 → 重分类
- 显式遗忘：级联撤回 + 向量清除 + stale 传播
- Hermes Provider（可配置 timeout + circuit breaker + prefetch/delivery receipt）
- MCP Server（5 工具契约，可嵌入工具套件，beta）
- REST API 新增 `POST /v1/extract/dry-run`、`POST /v1/consolidate`
- LLM 可观测性：`llm_call_spans` 持久化调用 span，`healthz` 暴露 24h 聚合
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
