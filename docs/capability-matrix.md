# HL-Mem 能力成熟度矩阵

> 基线：v0.28.1。默认模式取自 `Settings` 的静态默认值；部署通过 `hl_mem.toml` 显式覆盖。`audit`/`observe` 表示会记录数据但不自动改变核心结果或生命周期。

## 成熟度定义

- **stable**：默认主路径，契约和降级行为受回归测试保护。
- **beta**：已可用且有安全默认值，仍需更多离线或生产观察才能扩大自动化范围。
- **experimental**：显式选择后使用，接口、质量阈值或运行边界仍可能调整。

## 六大特性

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| 多查询召回 | beta | `auto` | 是，触发后调用 LLM | 是，仅 LLM span/audit | 超时、预算耗尽或解析失败时只使用原始 query | 固定评测集 Recall@K/MRR 不回退，P95 满足预算，连续两个版本无高优先级故障 |
| 关系候选发现 | beta | `off` | 是，启用后调用 LLM | 是，`audit` 写 proposal/audit；不写关系边 | API 失败时不生成 proposal，核心 Claim 写入继续 | proposal precision 达到发布阈值，重复运行/并发审计稳定，`auto` 灰度无错误边回归 |
| Benchmark suite | beta | `off`（CLI 按需） | 视模式而定；真实提取/向量评测需显式配置 | 仅写隔离的临时 benchmark DB、缓存与报告，不污染生产库 | 数据集、缓存 fingerprint 或 adapter 错误时明确失败；429/quota 熔断后可在窗口恢复时用原参数 `--resume` 重跑限流 case，不影响服务运行 | LongMemEval-S extract-once/config-compare 与 50 case、190 gold claim 中文集持续版本化，结果可复现，CI/nightly 基线和回归阈值稳定 |
| 图片证据入口 | experimental | `off` | 是，开启后调用视觉 LLM | 是，成功描述后写 Event/Evidence/Claim | 描述失败则拒绝该图片提取并保留具体错误；不伪造文本证据 | 来源接入、SSRF/路径边界、安全与质量评测完成，失败率和延迟达到 SLO |
| 反馈驱动维护 | beta | `observe` | 否 | 是，写 feedback/usefulness；默认不改变 TTL/decay | 归因或聚合失败不影响 recall 主结果，记录错误并可重建 | usefulness 重建一致，离线证明生命周期收益且无错误延寿/衰减，再考虑默认 `on` |
| Tool/Procedure intent | beta | `keyword` | 否；`auto` 模式可调用 LLM | recall 会更新受控访问/观测数据 | LLM 路由失败回退 keyword；无候选时回退通用召回 | intent precision/recall、procedure 成功率和负向 outcome 处理达到阈值 |

## 核心功能

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| Event 幂等摄入与证据链 | stable | `on` | 否 | 是 | 写入或约束失败时事务回滚并返回具体错误 | 保持跨版本事务、幂等、并发和证据完整性回归 |
| LLM Claim 提取 | stable | `fake`（部署推荐显式设为 `llm`） | 是 | 是 | retry 后失败则 Job 失败，可重试；原始 Event 保留；恰好命中 20 条上限时告警但不伪造余项 | compact/legacy schema 共用 AdmissionPolicy；双语复合事实、关系与枚举原子性保持一致；解析、后处理投影、证据与调用可观测性受回归保护 |
| Extraction entailment verification | beta | `off`（可配置 `audit`/`enforce`） | 启用后是 | 是，仅 audit | verifier 失败时 fail-open，保留原始提取结果并记录错误 | 冻结评测集质量稳定、额外延迟与 token 成本达到 SLO 后再考虑真正 enforce |
| Extraction pre-filter | experimental | `off` | 否 | 开启后写 audit | 规则异常时 `error_fallback` 到正常提取 | 生产回放证明显著节省调用且事实漏失低于既定阈值 |
| Embedding | stable | `fake`（部署推荐显式设为 `real`） | real 模式是 | 是 | compatible/native 调用失败按 retry 策略报错；不写伪向量 | native 默认不传 `text_type`；query/document 可显式启用，sparse/instruct 实验变体默认关闭；维度、provider、重建和失败恢复持续受保护 |
| FTS + Dense + RRF 混合召回 | stable | `on` | Dense 查询本身否 | 是，受控更新访问统计 | 某个可选通道无候选时使用其余通道；核心错误明确失败 | 保持中文/英文、时间、作用域和排序回归 |
| SQLite 向量后端 | stable | `sqlite_scan` | 否 | `sqlite_vec` 写派生索引 | scan 使用两阶段精确回表；sqlite-vec dirty 时查询回退 scan，启动自动修复投影；缺少显式选择的 extra 时配置报错 | 两后端召回与时间可见性语义持续一致，规模化延迟、投影修复和扩展兼容性受回归保护 |
| Tag soft boost | stable | `on` | 否 | 否 | 无 tag 命中即零 boost | 离线排名不回退并保持可解释权重 |
| 独立 Tag channel | experimental | `off` | 否 | 否 | 关闭或无结果时保持 FTS + Dense 双通道 | 评测证明对主要数据集净增益且无显著延迟/噪声 |
| Reranker | beta | `off`（可配置 fake/on/real） | real 模式是 | 否 | retry/超时后保留融合前排序 | 相关性净增益、P95 和 API 故障率达到 SLO |
| 双时间与作用域过滤 | stable | `on` | 否 | 是 | 不降级；非法时间/作用域明确失败 | 保持历史查询、可见性与并发回归 |
| TTL / decay / archive | stable | `auto` | 否 | 是 | 单 Job 失败可重试，CAS/事务避免部分更新 | 保持扫描完整性、双时间和访问 bonus 回归 |
| Near-copy / semantic dedup | beta | 确定性摄入复用与召回折叠 `on`；LLM 灰区 `audit` | 仅旧灰区审计路径是 | 摄入可追加 evidence；维护写等价边；召回折叠不写库 | 任一结构或 protected-atom 守卫失败即保留独立 Claim；pending pair 轮转；LLM 失败保留 uncertain | 近重复 precision、Top-K 多样性、人工复核率和错误折叠/supersede 低于阈值 |
| 冲突处理 | stable | `auto`（确定性优先） | 灰区是 | 是 | LLM 失败保留未决 case；维护任务会回访全部未决状态，人工可从 CLI 审核并裁决 | 保持 supersede 链汇聚、胜败者终态、证据和事务回归 |
| 删除完整性 | stable | `on` | 否 | 是，主库 + 独立 tombstone sidecar | forget/cleanup/restore 共用删除闭包；账本失败、状态歧义、manifest/ledger 错配时 fail-closed，不静默降级 | P0 状态/共享 Event/关系两端矩阵、幂等 replay、恢复中断续跑和三入口 dangling=0 持续全绿 |
| Episode / Trace | stable | `on` | 否 | 是 | 不影响 Claim 主通道；非法状态转换明确失败 | 保持 API、状态机、reward 与 usefulness 回归 |
| Policy / Procedure 归纳 | beta | `auto`（定时 Job） | 是 | 是 | 归纳失败保留 Episode，Job 可重试且不发布新策略 | 多 Episode 支撑、成功率、退役与审计指标达到阈值 |
| Mental Model 维护 | beta | `auto`（定时 Job） | 是 | 是 | 刷新失败保留旧模型并标记 stale/记录 Job 错误 | 水位幂等、证据覆盖、刷新质量和 stale 恢复达到阈值 |
| MCP Server（stdio） | beta | 按需启动 | 否 | 依工具而定 | 委托 application 服务；预期业务错误返回 `isError=true`，内部异常保留协议级错误 | 工具契约、事务边界和 REST 行为持续一致，Codex/Claude/Cursor 兼容与长时间运行指标达到 SLO |
| Hermes Provider | beta | `off`（`hermes.enabled=false`） | 是，调用本地 HL-Mem HTTP | 间接写入 | timeout/circuit breaker 后不阻断 Agent 主任务 | 兼容矩阵、重连、超时和长时间运行指标达到 SLO |
| Audit / LLM spans | stable | `on` | 否 | 是 | 关键审计写入失败时明确报错；非关键 span 不改变业务结果 | 保持字段稳定、敏感信息脱敏和可查询性 |
| SQLite 备份与 migration | stable | `on` | 否 | 是 | migration 失败事务回滚；manifest v2/ledger 校验或 replay 失败不替换现有目标 | 空库升级、ledger identity、tombstone replay、历史快照升级与恢复演练持续受门禁保护 |
