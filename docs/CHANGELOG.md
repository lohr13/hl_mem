# HL-Mem 变更记录

本文件记录影响跨对话交接的设计与实现变化。代码提交历史仍由 Git 保存；这里记录"为什么改"和"哪些模块受影响"。

---

## v0.10.1 — 2026-07-24

### 代码质量重构（基于 Hermes × Codex 双分析师共识评估）

#### P0：评测闭环 + 配置单一来源
- **冻结排序因子**：排序链已稳定，不再增加新 boost/channel/weight
- **新增 MRR + binary nDCG@10**：扩充离线评测指标
- **报告口径分离**：分别报告测试层（passed/failed/skipped）与 retrieval 指标（recall@5/MRR/nDCG@10/p50/p95）
- **移除管线内部 Settings()**：引入 RecallConfig dataclass，配置由应用入口提供，消除第二配置入口

#### P1：类型化重构 + 行为测试
- **state dict → RecallContext dataclass**：召回管线从 35 字段的 dict[str, Any] 迁移到显式类型标注的 dataclass，消除字段名拼写错误风险
- **behavioral scenarios 独立**：tests/scenarios/ 现可独立执行和报告

#### P2：清理 + benchmark
- **删除死代码**：_link_event_atomically、Database.__enter__/__exit__
- **IngestService 幽灵依赖**：删除未使用的 embedder 参数，收紧 connection 类型
- **StoreClaimResult → dataclass**：从 str 子类改为 @dataclass(frozen=True)
- **中文常量收敛**：新建 domain/constants.py，替换散落的硬编码中文
- **加 CI**：.github/workflows/test.yml（push/PR 自动跑单测）
- **向量检索 benchmark**：522/2k/10k 三档延迟与内存基准

## v0.10.0 — 2026-07-24

### Phase 18: Topic Tags 检索接入
- **Soft boost（方案 D）**：FTS/Dense RRF 融合后，命中的 query tags 给予 0.05 微小权重作为 tie-breaker，默认开启
- **独立 Tag channel（方案 B）**：第三召回通道（独立 tags FTS），weight=0.15，默认关闭待评测
- **中英文 query→tag 解析**：确定性词典匹配（"架构决策" → [architecture, decision]）
- **migration 018**：claims_tags_fts 独立 FTS 表 + 3 triggers
- 影响：`recall/staged_pipeline.py`、`domain/claims/query_tags.py`、`application/recall.py`、migration `018_claims_tags_fts.sql`

### v0.9.1 — 2026-07-24

### 审查修复（11 个 P0/P1）
- **conflict_key v3**：移除 predicate，减少假冲突
- **去重 min_confidence**：跨 subject 去重必须满足最小置信度阈值
- **qualifier 降级**：slot 无匹配时 canonical_slot=NULL 而非填默认
- **TTL UTC 统一**：retention 计算全部使用 UTC
- **回填 CAS**：TTL/slot 回填使用 compare-and-swap 防并发
- 影响：26 文件，测试 277 passed

### v0.9.0 — 2026-07-24

### Phase 17: 数据质量治理（4 Stages）
- **Stage 1 — slot+tags 双层分类**：migration 016 引入 canonical_slot（15 operational）+ topic_tags_json（开放多值）
- **Stage 2 — 行为切换**：claim 写入/冲突/去重/TTL 全面切换到 slot 模型
- **Stage 3 — 跨 subject 去重**：DedupJudge worker（audit-only 默认），dedup_pairs 审计表
- **Stage 4 — TTL + importance 联动**：retention 纯函数（scope × importance 三档矩阵），migration 017
- 影响：`domain/claims/`（新建）、`workers/deduplicate.py`（新建）、`workers/backfill_expires_at.py`（新建）、`recall/staged_pipeline.py`（新）

### v0.7.0 — 2026-07-24

### Phase 16: 代码收敛
- 统一 RRF/vector/retry/stage 实现（删除 9 个重复实现 + 4 个兼容层）
- Hermes provider 同步契约收敛 + circuit breaker 修复
- 事务所有权统一到 application 层
- 净减 333 行

### v0.6.0 — 2026-07-24

### Phase 15: 复杂度治理（14 个 P1/P2）
- Settings 统一注入（消除 config.py 双轨）
- LLM 调用全部走 LLMClient（消除散落的 httpx.post）
- 上帝函数拆阶段函数
- repository 按职责拆分 5 文件
- Hermes provider 拆三子对象
- 多跳 BFS 预备（默认 max_depth=1）

### v0.4.3 — 2026-07-23

### Phase 14: Hindsight 对标
- LLMClient + Provider 解耦（百炼/智谱/OpenAI-compatible）
- 长输入结构感知分块 + 输出超限递归二分恢复
- 统一 SearchTrace（候选/分数/过滤原因/耗时可回放）
- 一跳关系扩展召回（默认关闭，灰度开关）

## v0.3.0 — 2026-07-23

### Phase 12: 数据质量提升

- **实体归一化**：6 组 alias 映射（如 `GLM-5.1`→`glm-5.2`），合并同一实体的不同写法
- **语义去重升级**：阈值从 0.95 降至 0.82，算法从全量比较改为 best-match（每条候选只匹配最相似的现有 claim），显著减少假阴性
- **canonical attribute reconcile**：新 claim 写入时自动与同实体的 canonical attribute 对账，低置信度新 claim 不覆盖高置信度旧值
- **scope 后置规则**：`normalize_scope` 在写入后自动修正 scope（如 "port" + 整数值 → `config.port`）
- **TTL policy**：`ATTRIBUTE_TTL_DAYS` 配置化，仅短期状态类型（service_health/process/connectivity/test_suite）设 7 天 TTL
- **decay priority**：衰减 worker 按 priority 排序执行，高优先级 claim 先处理
- 影响：`config.py`、`recall/dedup.py`、`recall/attribute_map.py`、`domain/entity.py`、`workers/decay.py`、migration `006_canonical_attribute.sql`

### Phase 10-11: 冲突检测大修（v0.3.0 bump）

- **互斥模型翻转**：从"默认互斥 + 白名单排除"改为"默认非互斥 + 白名单包含"，只有 5 个真正单值槽位参与冲突检测：`ui_theme`、`response_style`、`config.port`、`config.model`、`service.health`
- **ConflictResolver 误报修复**：根因是 conflict_key 生成时 predicate 未规范化，导致同一属性的不同表述产生不同 key，冲突检测失效
- **predicate 规范化**：统一大小写、空格、别名映射
- **ingest 冲突案例**：新增 `conflict_cases` 表和状态机（pending → auto_resolved / manual_required → resolved / rejected）
- **authority tie-break**：同 conflict_key 的 claim 按 authority 排序，高 authority 覆盖低 authority
- **低值过滤**：importance < 0.3 的 claim 不参与冲突检测
- **semantic dedup 旁路修复**：修复了 mutual-exclusivity 检查短路导致 semantic dedup 被跳过的 bug
- 影响：`recall/conflict.py`、`config.py`、migration `013_conflict_cases.sql`

### Phase 0-9: 架构重构

#### P0 数据正确性

- 事务原子化：整个写入流程（update_status + insert_claim + supersede + evidence_link）在单一 BEGIN IMMEDIATE 中
- fact_hash v2：JSON 数组有边界哈希，替代 v1 的自由文本拼接（migration `011_fact_hash_v2.sql`）
- MCP pipeline 修复：MCP 工具委托 application 服务，不再绕过事务边界

#### 架构分层

- **application/** 层：新增 IngestService、RecallService、ForgetService，REST/MCP/Worker 统一入口
- **domain/** + **core/**：纯函数，不依赖基础设施。domain/temporal 独立双时间可见性逻辑
- **依赖方向修复**：`core/vector` + `domain/temporal` 从 storage 中提取，消除循环依赖
- **统一状态枚举**：ClaimStatus + EpisodeStatus 集中到 `lifecycle.py`，`assert_transition()` 守卫所有状态变更

#### 维护与质量

- schemas 拆分：`schemas.py` 从 server.py 独立
- 集中配置：`config.py`（常量）+ `settings.py`（Settings dataclass + from_env() 校验）+ `components.py`（工厂）
- Hermes provider 合并：删除冗余 adapter，统一为 `adapters/hermes/provider.py`（358 行）
- P2 质量修复：Protocol 接口化、错误分类化、retry 工具化、router 合并、zombie fields 清理

#### 功能增强

- 3 个核心问题修复：observation recall、conflict resolution、context budget
- 5 项改进：记忆关系（summarizes/supports/follows/about）、多模态内容协议、提取器路由、偏好专用召回 intent、Settings 配置快照

---

## 2026-07-22 — Phase 3-7

- 增加带 proof count、source watermark、证据准入和 stale 传播的派生记忆维护
- 完成 Episode、Trace、反馈归因以及内嵌 Procedure 的 Policy 生命周期
- 增加确定性查询路由、RRF/MMR、预算装箱、MCP 工具契约和 CLI 导入导出
- 增加可选 PostgreSQL 连接边界、SQLite 在线备份恢复、租户配额和保留策略
- SQLite WAL 仍是默认后端，离线测试不依赖外部 API 或 PostgreSQL

---

## 2026-07-24 — v0.9.0 · Phase 17 数据质量治理

### Stage 1-2: canonical_slot + topic_tags 分类体系（方案 E）
- 新增 SLOT_REGISTRY（55 attribute，15 operational slot）
- 新增 migration 016：canonical_slot + topic_tags_json 字段
- conflict_key 改用 canonical_slot（无 slot 返回 NULL，不参与冲突）
- dedup 适配：有 slot 按 slot 隔离，无 slot 按 predicate+embedding
- LLM prompt 展示完整 slot 定义 + abstain 规则
- 回填工具 backfill_claim_slots_v1.py（dry-run + apply）
- 解决 fact.other 占 46% 的分类粗糙问题

### Stage 3: 跨 subject 语义去重
- 新增 migration 017：dedup_pairs 审计表
- DedupJudge（LLM 判断 equivalent/distinct/uncertain）
- 后台 worker workers/deduplicate.py（audit-only 默认开启）
- 安全护栏：threshold 0.92 + LLM 二次确认 + supersede 语义
- 解决同一事实因 subject 不同被重复存储的问题

### Stage 4: TTL 三因子 + importance 治理
- 新建 domain/claims/retention.py 纯函数模块
- TTL = f(scope, importance)：temporal 低→3天/中→7天/高→14天
- 写入门槛：importance < 0.2 不写入（保护类型例外）
- reclassify 从原始锚点重算 expires_at（不增量更新）
- 存量回填脚本 workers/backfill_expires_at.py

---

## 2026-07-24 — v0.7.0 · Phase 16 代码收敛

### Batch 1: 消除重复实现
- 向量编解码统一到 pack_vector/unpack_vector
- RRF + budget_pack 统一
- HTTP retry 统一到 http_utils.retry_http()
- 召回管线假阶段改为真实分阶段
- TypeError 兼容猜测删除

### Batch 2: 统一契约
- 事务所有权统一（repository 不 commit，application/worker 拥有）
- API monkeypatch 删除
- value_json 双轨消除（统一为 value Python 值）

### Batch 3: Hermes 收敛 + 兼容层清理
- Hermes 同步/异步双契约收敛为同步一代
- 熔断器线程安全（Lock + half-open 单探测）
- prefetch TTL + session-end 清理
- 过期兼容层全部删除（-7 文件 -333 行）

---

## 2026-07-23/24 — v0.4.3 → v0.6.0 · Phase 13-15 复杂度治理

### Phase 13: 架构修复
- 幂等竞态修复 + 去重 TOCTOU + lease token + 预算硬限 + N+1 批量 + domain 纯化

### Phase 14: Hindsight 对标
- LLMClient/Provider 解耦 + Pydantic schema 约束
- 长输入结构感知分块 + 输出超限递归二分
- 统一 SearchTrace（候选/分数/过滤原因/耗时可回放）
- 一跳关系扩展召回（默认关闭）

### Phase 15: 品质审查修复
- Settings 统一入口 + LLM 全部走 LLMClient
- 写入逻辑迁 domain/claims/ + 上帝函数拆阶段
- repository 拆 5 文件 + domain/types.py dataclass
- Hermes provider 拆 3 子对象 + 多跳 BFS 预备 + magic number 集中

---

## 2026-07-22 — v0.3.5 · Phase 8-12 核心功能

- 冲突检测互斥白名单模型（5 真正单值槽位）
- TTL 矩阵（scope × volatility）
- 多因子召回排序（semantic + recency + access）
- canonical_attribute v2 + conflict_key v2
- access_count + last_accessed_at + 软衰减
- 记忆关系图 + 派生记忆维护

- 建立文档入口、交接状态、MemOS/Hindsight 选型分析、核心 ADR、系统架构和分阶段实施计划
- 完成 Hermes × Codex 三轮 review 并形成一致接受的首版共识
- **决策**：统一事件溯源双通道设计，事实通道参考 Hindsight，经验通道参考 MemOS
- **决策**：MVP 使用 SQLite，不引入 Neo4j，不依赖 Hindsight/MemOS 运行时
- **决策**：Embedding 选 `text-embedding-v4` 2048 维 Dense+Sparse
- **决策**：首版范围精简为 3 种记忆类型、2 档 volatility、2 档 visibility
- 完成首版完整实现：事件日志、LLM 提取、混合检索、矛盾检测、TTL、遗忘、Worker、Hermes Provider
- Prompt 调优（中文值保持、predicate 标准化、conflict 检测修复）
- qwen3.7-plus + text-embedding-v4 端到端验证通过
