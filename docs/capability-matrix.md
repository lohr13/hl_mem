# HL-Mem 能力成熟度矩阵

> 基线：v0.18.0。默认模式取自 `Settings` 的静态默认值；部署通过 `hl_mem.toml` 显式覆盖。`audit`/`observe` 表示会记录数据但不自动改变核心结果或生命周期。

## 成熟度定义

- **stable**：默认主路径，契约和降级行为受回归测试保护。
- **beta**：已可用且有安全默认值，仍需更多离线或生产观察才能扩大自动化范围。
- **experimental**：显式选择后使用，接口、质量阈值或运行边界仍可能调整。

## 六大特性

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| 多查询召回 | beta | `off` | 是，启用后调用 LLM | 是，仅 LLM span/audit | 超时、预算耗尽或解析失败时只使用原始 query | 固定评测集 Recall@K/MRR 不回退，P95 满足预算，连续两个版本无高优先级故障 |
| 关系候选发现 | beta | `off` | 是，启用后调用 LLM | 是，`audit` 写 proposal/audit；不写关系边 | API 失败时不生成 proposal，核心 Claim 写入继续 | proposal precision 达到发布阈值，重复运行/并发审计稳定，`auto` 灰度无错误边回归 |
| Benchmark suite | beta | `off`（CLI 按需） | 否；真实 provider 评测需显式配置 | 否，输出报告文件 | 数据集或 adapter 错误时明确失败，不影响服务运行 | 数据集版本化、结果可复现、CI/nightly 基线和回归阈值稳定 |
| 图片证据入口 | experimental | `off` | 是，开启后调用视觉 LLM | 是，成功描述后写 Event/Evidence/Claim | 描述失败则拒绝该图片提取并保留具体错误；不伪造文本证据 | 来源接入、SSRF/路径边界、安全与质量评测完成，失败率和延迟达到 SLO |
| 反馈驱动维护 | beta | `observe` | 否 | 是，写 feedback/usefulness；默认不改变 TTL/decay | 归因或聚合失败不影响 recall 主结果，记录错误并可重建 | usefulness 重建一致，离线证明生命周期收益且无错误延寿/衰减，再考虑默认 `on` |
| Tool/Procedure intent | beta | `keyword` | 否；`auto` 模式可调用 LLM | recall 会更新受控访问/观测数据 | LLM 路由失败回退 keyword；无候选时回退通用召回 | intent precision/recall、procedure 成功率和负向 outcome 处理达到阈值 |

## 核心功能

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| Event 幂等摄入与证据链 | stable | `on` | 否 | 是 | 写入或约束失败时事务回滚并返回具体错误 | 保持跨版本事务、幂等、并发和证据完整性回归 |
| LLM Claim 提取 | stable | `fake`（部署推荐显式设为 `llm`） | 是 | 是 | retry 后失败则 Job 失败，可重试；原始 Event 保留 | 保持 schema 兼容、解析质量和调用可观测性 |
| Extraction pre-filter | experimental | `off` | 否 | 开启后写 audit | 规则异常时 `error_fallback` 到正常提取 | 生产回放证明显著节省调用且事实漏失低于既定阈值 |
| Embedding | stable | `fake`（部署推荐显式设为 `real`） | real 模式是 | 是 | 外部调用失败按 retry 策略报错；不写伪向量 | 维度、provider、重建和失败恢复持续受保护 |
| FTS + Dense + RRF 混合召回 | stable | `on` | Dense 查询本身否 | 是，受控更新访问统计 | 某个可选通道无候选时使用其余通道；核心错误明确失败 | 保持中文/英文、时间、作用域和排序回归 |
| Tag soft boost | stable | `on` | 否 | 否 | 无 tag 命中即零 boost | 离线排名不回退并保持可解释权重 |
| 独立 Tag channel | experimental | `off` | 否 | 否 | 关闭或无结果时保持 FTS + Dense 双通道 | 评测证明对主要数据集净增益且无显著延迟/噪声 |
| Reranker | beta | `off`（可配置 fake/on/real） | real 模式是 | 否 | retry/超时后保留融合前排序 | 相关性净增益、P95 和 API 故障率达到 SLO |
| 双时间与作用域过滤 | stable | `on` | 否 | 是 | 不降级；非法时间/作用域明确失败 | 保持历史查询、可见性与并发回归 |
| TTL / decay / archive | stable | `auto` | 否 | 是 | 单 Job 失败可重试，CAS/事务避免部分更新 | 保持扫描完整性、双时间和访问 bonus 回归 |
| Semantic dedup | beta | `audit`（跨 subject） | 判断灰区时是 | 是，写 dedup audit；自动模式可 supersede | LLM 失败保留 distinct/uncertain，不自动删除 | precision、人工复核率和错误 supersede 低于阈值 |
| 冲突处理 | stable | `auto`（确定性优先） | 灰区是 | 是 | LLM 失败进入待处理状态，不吞异常 | 保持状态机终态收敛、证据和事务回归 |
| Episode / Trace | stable | `on` | 否 | 是 | 不影响 Claim 主通道；非法状态转换明确失败 | 保持 API、状态机、reward 与 usefulness 回归 |
| Policy / Procedure 归纳 | beta | `auto`（定时 Job） | 是 | 是 | 归纳失败保留 Episode，Job 可重试且不发布新策略 | 多 Episode 支撑、成功率、退役与审计指标达到阈值 |
| Mental Model 维护 | beta | `auto`（定时 Job） | 是 | 是 | 刷新失败保留旧模型并标记 stale/记录 Job 错误 | 水位幂等、证据覆盖、刷新质量和 stale 恢复达到阈值 |
| MCP Server（可嵌入工具套件） | beta | `on` | 否 | 依工具而定 | 委托 application 服务；服务错误按 MCP 错误返回；尚无独立 transport/runtime 入口 | 工具契约、事务边界和 REST 行为持续一致，并补齐 transport/runtime 入口 |
| Hermes Provider | beta | `off`（`hermes.enabled=false`） | 是，调用本地 HL-Mem HTTP | 间接写入 | timeout/circuit breaker 后不阻断 Agent 主任务 | 兼容矩阵、重连、超时和长时间运行指标达到 SLO |
| Audit / LLM spans | stable | `on` | 否 | 是 | 关键审计写入失败时明确报错；非关键 span 不改变业务结果 | 保持字段稳定、敏感信息脱敏和可查询性 |
| SQLite 备份与 migration | stable | `on` | 否 | 是 | migration 失败事务回滚；备份失败不替换现有备份 | 空库升级由 CI 门禁保护；历史快照升级与恢复演练仍需独立门禁 |
