# ADR-0001：采用统一的事件溯源双通道记忆服务

- 状态：Accepted
- 日期：2026-07-20
- 决策者：项目发起人与后续维护者

## 背景

HL-Mem 需要给 Hermes 及其他 Agent 提供跨会话记忆，同时覆盖长期事实、实时信息、总结归纳、经验复用、程序性 Skill、遗忘和矛盾处理。

单纯的向量库只能解决相似文本搜索；单纯的聊天摘要无法保留来源和历史；只使用图数据库也不能自动解决何时写入、怎样失效以及如何遗忘。

MemOS 和 Hindsight 分别覆盖了需求的不同部分，但 Hermes 当前限制为一个外部 MemoryProvider。双 Provider 同时写入会产生重复、生命周期不一致和删除不完整的问题。

## 决策

实现一个名为 `hl_mem` 的独立记忆服务和一个薄 Hermes Provider。

核心采用不可变事件日志，并在事件之上建立两个派生通道：

1. 事实通道：Event → Atomic Claim → Observation → Mental Model。
2. 经验通道：Episode/Trace → Reward → Policy → Procedure/Skill。

两个通道共享：

- namespace、visibility 和权限；
- evidence 和 provenance；
- 双时间字段和状态机；
- 检索、反馈、审计、删除和维护任务；
- 统一的 REST/MCP/Hermes 接口。

首版使用 SQLite 和普通关系表，不引入独立图数据库。图关系以 `entities`、`relations`、`claim_entities` 和依赖边表表达。确认多跳召回是瓶颈后，再评估 PostgreSQL/pgvector、Neo4j 或 Graphiti 后端。

Hindsight 和 MemOS 在 Phase 0 作为可运行基线与算法参考，不作为首版运行时硬依赖。

## 选择原因

- 单一服务可以统一事实有效期、作用域、删除和审计。
- 原始事件保留后，提取器和总结算法可以升级并重放。
- 双通道避免把“用户现在喜欢什么”和“上次怎样成功部署”压进同一种记录。
- 关系表足以验证绝大多数 MVP 假设，部署成本低于图数据库。
- 一个 Provider 对 Hermes 的工具数量、Prompt 注入和故障边界更容易控制。

## 被放弃的方案

### 直接使用 MemOS 作为唯一系统

优点是 Hermes、本地 SQLite、任务经验和 Skill 已较成熟。放弃原因是通用事实 TTL、双时间历史和系统化事实冲突不是其最直接的数据模型。

### 直接使用 Hindsight 作为唯一系统

优点是事实、时间、Observations 和 Mental Models 最贴近需求。放弃原因是完整的程序性 Skill、Reward 驱动学习和业务级遗忘仍需另建子系统。

### Hermes 同时启用 MemOS 与 Hindsight

放弃原因是 Hermes 当前只允许一个外部 Provider，且即便绕过限制，也会造成重复写入、召回重复、冲突和级联删除无法统一。

### Fork MemOS 或 Hindsight 后直接大改

初期看似更快，但会把 HL-Mem 的升级周期绑定到大型上游内部结构。首版只复用思想和公开接口；如果后续测量证明某个引擎显著优于自研，再通过存储/算法适配器集成。

## 后果

正面后果：

- 数据和生命周期完全可控；
- 能覆盖事实与执行经验两种 Agent 记忆；
- 易于解释、重放、迁移和合规删除；
- 可跨 Hermes、MCP Client 和自定义 Agent 共用。

负面后果：

- 首版工作量大于直接安装现成 Provider；
- 需要自己维护提取、合并、重排和评测；
- 初期准确率很可能低于成熟项目，需要 Phase 0 基线持续对照。

## 重新评估条件

满足以下任一条件时新增 ADR 重新评估：

- Hindsight 暴露了满足 TTL、删除、Procedure 和作用域要求的稳定接口，可显著减少一半以上维护成本；
- MemOS 增加完整的双时间事实与通用矛盾账本；
- SQLite 在目标数据量上无法满足 P95 延迟或并发写入；
- 真实评测证明多跳图召回是主要误差来源；
- 项目目标缩小为只服务编码/工具 Agent 或只服务个人事实助手。

