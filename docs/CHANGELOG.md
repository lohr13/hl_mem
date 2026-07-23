# HL-Mem 变更记录

本文件记录影响跨对话交接的设计与实现变化。代码提交历史仍由 Git 保存；这里记录"为什么改"和"哪些模块受影响"。

---

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

## 2026-07-20 — MVP

- 建立文档入口、交接状态、MemOS/Hindsight 选型分析、核心 ADR、系统架构和分阶段实施计划
- 完成 Hermes × Codex 三轮 review 并形成一致接受的首版共识
- **决策**：统一事件溯源双通道设计，事实通道参考 Hindsight，经验通道参考 MemOS
- **决策**：MVP 使用 SQLite，不引入 Neo4j，不依赖 Hindsight/MemOS 运行时
- **决策**：Embedding 选 `text-embedding-v4` 2048 维 Dense+Sparse
- **决策**：首版范围精简为 3 种记忆类型、2 档 volatility、2 档 visibility
- 完成首版完整实现：事件日志、LLM 提取、混合检索、矛盾检测、TTL、遗忘、Worker、Hermes Provider
- Prompt 调优（中文值保持、predicate 标准化、conflict 检测修复）
- qwen3.7-plus + text-embedding-v4 端到端验证通过
