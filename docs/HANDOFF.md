# HL-Mem 项目交接状态

> 最后更新：2026-08-14 · v0.26.0

## 当前状态

- **分支**：`main`
- **版本**：v0.26.0
- **阶段**：v0.26.0 已发布
- **服务**：FastAPI on port 8200；非敏感配置来自必需的 `hl_mem.toml`，四个独立密钥来自 `.env` 或进程环境
- **存储**：SQLite WAL + FTS5 + 向量 BLOB（`var/hl_mem.db`）；默认 `sqlite_scan`，可选 `sqlite_vec`；40 migrations（SQL 001-040）+ Python data migrations；实时数据量以数据库只读审计为准
- **FTS**：预分词 FTS v2（claims/events/tags）；旧 trigram/raw 表仅保留在回滚窗口

## 已完成

### 2026-08-14 v0.26.0 评测、拒答与 active claim 收敛

- 提取评测 v2 冻结原子事实、来源 Event、角色—动作—对象、专名、speaker、canonical subject、禁止传播、modality、多次采样与 dedup 消费者契约；E2E scorer 升级为审核式 `deterministic-rubric-v2`。
- 摄入按整个 conflict 组收敛，reclassify mutation 增加碰撞守卫；维护 CLI 支持 active claim `audit` 与显式 `repair --dry-run/--apply`，不确定组进入 disputed。
- `no_evidence` 作为 hard abstention 阻断 reader，`low_confidence` 作为 soft 元数据继续 QA；评测分别报告 hard/soft 指标，A/B 证实默认应保持 observe。
- entities hybrid、专有名词 prompt 与 enforce 默认开启均因冻结门禁失败而未纳入；具体差额记录在 CHANGELOG。

### 2026-08-14 v0.25.3 实体保真修复

- 双语提取 prompt 将具体实体名提升为最高优先级的无损信息，禁止用关系角色或泛化类别替换专名；跨行结构化记录必须联合读取姓名、描述与关系字段。
- PerLTQA adapter v2 不再只保留社会关系的 `Description`，而是同时传递 `Supporting Characters`、描述和相对主角的关系，避免在 LLM 提取前丢失人名。
- 当前 `PROMPT_HASH=e2d8f433b71c`、提取器身份为 `llm-v2+e2d8f433b71c`。

### 2026-08-12 v0.25.1 偏好召回补丁

- [GitHub Release](https://github.com/lohr13/hl_mem/releases/tag/v0.25.1) 与 [PyPI 0.25.1](https://pypi.org/project/hl-mem/0.25.1/) 已发布；tag 与包版本一致，OIDC Trusted Publishing 成功。
- 生产召回用零 LLM 成本的中英文高精度规则识别个性化推荐意图；显式 `intent` 和历史语义仍优先。
- preference 的既有 3 个保留位内优先选择归一化为 `user` 的偏好，不硬过滤其他主体，不影响其他 intent。
- LongMemEval 适配器不再对 preference 证据二次重排，保留生产 Top-10 顺序；评测工具同时增加 full-context 与 raw-session native-RAG 对照模式。

### 2026-08-12 v0.25.0 发版收口

- 生产提取使用默认 `5 Event / 120 秒` 的同会话微批，speaker 来自 Event `actor_type`，`source_event_indices` 可覆盖配置允许的最多 32 个来源；`POST /v1/events/batch` 保持请求数组顺序和事务原子性。
- FTS `auto` 查询以 raw/stem 两个 conjunctive 分支兼容 v0.24.0 存量索引，repository 与 FTS 重建显式消费活动 `fts_language`；升级不要求仅为该兼容修复强制重建 FTS。
- Event JSONL 归档已覆盖 `metadata_json`；长任务 lease 由独立连接周期续租全部窗口 job，终态 ownership 丢失返回 `lease_lost`，禁止 0-row completion 伪装为成功。
- `extract_event` 仅在 HTTP 429 耗尽普通 job 重试后登记通用 deferred task，由维护循环按 1/4/12 小时有界重放；成功收敛、非 429 不延续，多次 429 后放弃，pending Event 不被 retention 清除。
- 重复治理使用同一确定性安全门覆盖摄入、维护和候选窗召回：protected atoms 保序保次数，跨 subject 仅允许文本可证明的 `user ↔ user's <entity>`；维护候选按 `reviewed_at` 轮转，召回公开 `equivalent_claim_ids` 并汇总组内 evidence，全程不删除或 supersede Claim。
- 双语提取 prompt 已对齐复合事实、关系动作、一次性事件和枚举/总数规则；当前 `PROMPT_HASH=86c522e45f92`、提取器身份为 `llm-v2+86c522e45f92`。原始响应恰好 20 条时记录 `claim_limit_reached`，但不放宽 schema。
- 固定 extractor-only A/B 中三个英文关系/枚举样本改前已正确、改后保持；中文“关系+枚举”组合样本改前后都返回空。该 prompt 变更是规则补齐，不是关系召回率已提升的统计证据。
- LongMemEval 结果持久化 dense/reranker 原始分、通道、最终排名与 `search_trace`。冻结官方口径为 **40/50（80%）**：`deepseek-v4-flash-0731`、全 reader thinking、Top-10、自有 judge；temporal gate 诊断口径为 **40/48（83.3%）**，不得与官方分数混报。
- 评测已知边界：内容审查隔离跳过 2 个 Event；剩余错误主要是 multi-session 聚合、temporal 计算和 single-session 限定词。benchmark reader 是评测工具，不是生产 recall API 的组成部分，Top-10 也不等同于生产的可配置召回/packing 窗口。
- v0.24.1/v0.24.2 仅为仓库内过渡版本、没有 release tag；v0.25.0 从 v0.24.0 升级时会执行 migration 038 数据回写、migration 039 nullable metadata 列与 migration 040 deferred task 队列。大库必须先备份、停写并为 038 的全表扫描与写锁安排维护窗口。
- v0.25.0 已于 2026-08-12 通过 GitHub Release 与 PyPI 发布；后续个性化推荐召回修正纳入 v0.25.1。

### 2026-08-10 v0.24.2 DeepSeek 与 holdout 诊断收口

- `bdd8391` 增加 DeepSeek V4 Flash benchmark 配置、显式 thinking 控制和 QA model override；manifest/resume identity 覆盖 endpoint、effective provider、extractor payload 与 QA/query-expansion model，禁止跨配置复用缓存。
- `43bb3ae` 修复 holdout50 的超长 turn 分块、按 session/turn index 重建 reader 相邻证据、temporal 歧义 gate、脱敏 HTTP 错误取证和 Claim 膨胀诊断。
- `aaf4440` 交付可无损还原的 `semantic-turn-fragments-v1`，将 fragment/reader 协议及 chunk 参数纳入 cache/resume/merge 身份；物理 Claim 密度、legacy resume 空值、结构化诊断脱敏和问题时区边界均已加固。
- CI 收口恢复 extraction benchmark 的旧诊断函数兼容导出，并按非终态 lifecycle status 隔离相邻复述候选，避免 candidate/disputed 交叉计数。
- 仓库治理已将 `.env.dsv4`、`evaluation/datasets/` 纳入忽略；24 个 dataset 文件及 LongMemEval fixture 外移到 `C:/Users/Administrator/hl_mem_eval_data/`，两份未跟踪 proposal 外移到 `C:/Users/Administrator/hl_mem_docs/`；reader investigation 停止 Git 跟踪并保留本地文件。

### 2026-08-09 v0.24.2 LongMemEval 全量评测前修复

- 时间解析对逗号分组数字、完整边界、超大历史年龄和 `datetime` 上下界 fail-soft；任一坏 match 不再使 Claim/case 崩溃。
- preference intent 只预留前三个偏好槽位；assistant durable output 有界提取，LongMemEval 改为逐 turn、speaker-aware Event。
- reader 对事实、偏好推荐和 temporal 问题使用不同约束；当前/历史基准选择与条件偏移的顺序已显式固定。
- assistant 引用型问题可在 case namespace 内 OR 检索 Top-1 原始 assistant turn，和 Claim 证据去重后共享 1,200-token evidence 配额。
- coverage、claim retrieval、session retrieval 使用独立分母并报告 R@K 分子/分母；429/quota case 在等待窗口后可用原参数 `--resume` 自动重跑。
- 无新增 SQL migration、无依赖变化、生产阈值 `0.82/0.92/0.95` 不变；旧 LongMemEval cache 因 turn-event/prompt fingerprint 变化需要重新 ingest。

### 2026-08-07 v0.24.0 向量检索与依赖边界

- `sqlite_scan` 从 `SELECT *` 全量扫描改为“轻量向量评分 + 候选回表”两阶段流程，并在可见性过滤后循环扩大候选，直到满足 `limit` 或耗尽扫描结果。
- 新增可选 `sqlite_vec` 后端与 `hl-mem[sqlite-vec]` extra；默认仍为精确 `sqlite_scan`，向量维度/模型漂移、dirty 投影和查询降级均有显式守卫。
- 启动时自动 drain `claim_vector_dirty`，同步更新或删除 sqlite-vec 派生投影，避免旁路 SQL 令服务长期停留在扫描回退路径。
- query expansion 不再因查询短而直接调用 LLM；指代触发要求存在可用 session context，同时保留原始候选不足时的 low-recall fallback。
- MCP SDK 从核心依赖移到 `mcp` extra，并完整接入 dev 依赖、CI 与安装文档，消除与 `claude-agent-sdk` 的 MCP 版本冲突。
- 公共候选物化器统一 `sqlite_scan`/`sqlite_vec` 的回表与时间可见性语义；`/healthz` 新增 `vector_backend`，doctor 正确区分 SQL migration 与 Python data migration。
- 新增不可变 SQL migration 037 管理向量索引控制表与 dirty triggers；`sqlite_vec.py` 作为独立 Python data migration 构建可选向量投影。

### 2026-08-06 v0.23.1 提取治理与 Benchmark 基线

- LLM 提取改为约 63 行极简 prompt 与 6 字段 compact schema；`AdmissionPolicy` 统一执行 notability、证据可定位性、敏感值和操作快照准入。
- 后处理从 compact 输出恢复 choice、qualifiers、时间边界、entities 及完整 Claim schema；旧 14 字段输出走同一准入链路。
- 稳定 preference/architecture 豁免一次性操作快照规则，数字/IP/端口证据要求精确一致；非空 claims 优先于矛盾的 `should_memorize=false`。
- native embedding 默认不发送 `text_type`，显式 query/document、sparse 与 instruct 仍可配置；存量向量应按部署最终配置一致地生成或重建。
- Reranker 的默认型号已迁移，但运行时型号仍由 TOML 配置、密钥仍由 `.env` 或进程环境提供，活文档不依赖具体型号。
- LongMemEval-S runner 支持 extract-once/config-compare 和 claim/session 双层指标；二元 Recall@K 已正名为 Hit@K，claim relevance 默认阈值为 0.5。
- 新增 50 case、190 条 gold claim 的中文记忆测试集；12 题阈值分析将 0.40 识别为探索性最优点，当前 runner 保留 0.5 作为较保守默认值。
- 995 项 unittest 全部通过；v0.23.1 无新增 migration，数据库 schema 保持在 migration 036。

### 2026-08-05 v0.22.0 Embedding 迁移与安全门

- Embedder 新增 compatible/native 双 API 模式与 query/document `text_type` 角色；provider、model 和维度均由 TOML 配置。
- 提取链新增 fail-open 的 entailment verifier；默认关闭，生产可用 `audit` 只记录支持度结果而不拦截 Claim。
- dedup 升级为 cosine 候选、确定性安全门、异步 LLM 灰区判断三层链路，`policy_version` 更新为 v2。
- 新增 claim-pair、recall、extraction/entailment 冻结评测集，以及 embedding 消融和 no-answer calibration 工具。
- v0.22.0 无新增 migration；数据库 schema 保持在 migration 036。

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
- 跨平台 `scripts/healthcheck.py` 通过 `/healthz` 返回监督退出码；systemd、Windows 服务管理器或容器编排平台负责定时探测、重启与告警。
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
- LLM compact 提取 + AdmissionPolicy + 完整 schema 后处理 + Embedding + Reranker（模型和维度由 TOML 配置）
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
- Benchmark suite：LongMemEval-S runner、中文记忆测试集 + extraction/retrieval/lifecycle 三层指标，CLI 按需运行
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

- 对“高盛债券/大宗商品”关系链做 evidence-group context A/B，同时保留“推荐≠执行”的 modality 负例。
- 为关系链评测补充 answer-entity/role coverage，避免 event-level R@5 在答案叶子未进入 Top-5 时产生假阳性。
- 接入实际图片输入源后评估视觉描述器；Mental Model 推理增强与多租户继续按独立版本范围决策。

## 关键文档索引

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 版本变更时间线 |
| [architecture.md](architecture.md) | 当前已实现架构 |
| [archive/implementation-plan.md](archive/implementation-plan.md) | 已完成里程碑与历史路线图 |
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
